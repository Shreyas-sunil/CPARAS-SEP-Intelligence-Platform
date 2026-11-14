"""
CPARAS Bulk - fast scoring against a large clause corpus (v0.4.7)
===================================================================

Why this exists
---------------
With the retrieval index installed, the Bulk corpus holds up to 60,000 clause excerpts. v0.4.6 embedded
ALL of them (BAAI/bge-m3, up to 1,500 characters each) before scoring the first row: on a laptop that is
tens of minutes on the GPU and hours on the CPU, the run sat at "0 / N" the whole time, and - because the
embedder serves one call at a time - every ISLD analysis queued behind it.

What it does instead
--------------------
* ``CorpusVectorCache`` - one vector per clause, stored on disk (float16 memmap + "done" bitmap) under
  the corpus fingerprint, embedding model, precision and token limit. Any run embeds only the clauses it
  needs that are not cached yet; nothing is ever embedded twice, and an interrupted run loses nothing.
* ``LexicalIndex`` - TF-IDF over the clause texts (seconds to build, cached). For each row it proposes a
  shortlist: the best lexical matches in the whole corpus plus the best within the declared specification.
  The shortlist is then re-scored with the real embedding model and the best clause wins, with the same
  declared-family tie-break as the exhaustive path (etsi_bulk/scoring._pick_best).
* Calibration needs the floor (5th percentile) of raw similarities of the run's patents against the
  corpus; it is measured against a fixed, seeded random sample of clauses instead of every clause.
* When every clause has a cached vector, scoring automatically becomes exhaustive (exactly the v0.4.6
  comparison). On a GPU the remaining clauses are filled in the background after a run (yielding to
  interactive work), so the shortlist is only ever a first-run measure.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import pickle
import threading
from pathlib import Path
from typing import Callable, Optional

import numpy as np

logger = logging.getLogger(__name__)


def _settings():
    """Current settings (lazy import keeps this module importable in isolation)."""
    from core.config import settings
    return settings


def _safe(s: str, n: int = 60) -> str:
    """File-name-safe version of a model name or tag."""
    return "".join(ch if ch.isalnum() else "_" for ch in str(s))[:n]


def _cache_dir() -> Path:
    """CACHE_DIR, created if needed - where clause/title vector caches and the lexical index are
    stored.
    """
    d = Path(_settings().CACHE_DIR)
    d.mkdir(parents=True, exist_ok=True)
    return d


def corpus_texts(corpus) -> list[str]:
    """The text embedded for each clause of the corpus (entry.embedding_text())."""
    return [e.embedding_text() for e in corpus.entries]


# ── on-disk per-clause vector cache ──────────────────────────────────────────────────────────────
class CorpusVectorCache:
    """float16 vectors for every clause of one corpus, filled incrementally, persisted after each slice."""

    _prefix = "bulkvec"

    def __init__(self, corpus, model_name: str, precision: str, max_tokens: int, n: Optional[int] = None):
        """Open (or prepare) the on-disk float16 vector cache for this corpus fingerprint + embedding
        model + precision + token limit. Files: <tag>.f16 (memmap of vectors), <tag>.done.npy (which
        rows are filled) and a JSON meta file.
        """
        self.corpus = corpus
        self.n = len(corpus.entries) if n is None else int(n)
        tag = f"{corpus.fingerprint}_{_safe(model_name)}_{precision}_t{int(max_tokens)}"
        base = _cache_dir() / f"{self._prefix}_{tag}"
        self.vec_path = base.with_suffix(".f16")
        self.done_path = base.with_suffix(".done.npy")
        self.meta_path = base.with_suffix(".json")
        self.max_tokens = int(max_tokens)
        self.dim: Optional[int] = None
        self._mm: Optional[np.memmap] = None
        self.done = np.zeros(self.n, dtype=bool)
        self._locks: dict = {}
        self._texts: Optional[list[str]] = None
        self._load()

    # -- persistence
    def _load(self) -> None:
        """Reopen an existing cache when its row count matches; otherwise leave it empty (it is rebuilt
        on demand).
        """
        try:
            meta = json.loads(self.meta_path.read_text())
            if int(meta.get("n", -1)) != self.n or not self.vec_path.is_file() or not self.done_path.is_file():
                return
            dim = int(meta["dim"])
            done = np.load(self.done_path)
            if done.shape != (self.n,) or self.vec_path.stat().st_size != self.n * dim * 2:
                return
            self.dim = dim
            self.done = done.astype(bool)
            self._mm = np.memmap(self.vec_path, dtype=np.float16, mode="r+", shape=(self.n, dim))
        except FileNotFoundError:
            return
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Bulk vector cache {self.vec_path.name} unusable ({e}); starting a new one.")
            self.dim, self._mm = None, None
            self.done = np.zeros(self.n, dtype=bool)

    def _create(self, dim: int) -> None:
        """Create the memmap and the 'done' mask once the embedding dimension is known."""
        self.dim = dim
        self._mm = np.memmap(self.vec_path, dtype=np.float16, mode="w+", shape=(self.n, dim))
        self.done = np.zeros(self.n, dtype=bool)
        self.meta_path.write_text(json.dumps({"n": self.n, "dim": dim, "fingerprint": self.corpus.fingerprint,
                                              "max_tokens": self.max_tokens}))

    def _persist(self) -> None:
        """Flush vectors and atomically rewrite the 'done' mask (temp file + replace), so a crash never
        leaves a half-written mask.
        """
        try:
            if self._mm is not None:
                self._mm.flush()
            tmp = self.done_path.with_name(self.done_path.stem + ".tmp.npy")
            np.save(tmp, self.done)
            os.replace(tmp, self.done_path)
        except OSError as e:
            logger.warning(f"Could not persist the Bulk vector cache: {e}")

    @property
    def lock(self) -> asyncio.Lock:
        """One lock per running event loop (an asyncio.Lock must not be shared across loops)."""
        loop = asyncio.get_running_loop()
        lk = self._locks.get(id(loop))
        if lk is None:
            self._locks.clear()
            lk = self._locks[id(loop)] = asyncio.Lock()
        return lk

    # -- state
    @property
    def n_done(self) -> int:
        """How many clause vectors are already cached."""
        return int(self.done.sum())

    def complete(self) -> bool:
        """True when every clause has a vector (scoring can then switch to the exhaustive comparison)."""
        return self.n > 0 and bool(self.done.all())

    def texts(self) -> list[str]:
        """Clause texts, built once on first use."""
        if self._texts is None:
            self._texts = corpus_texts(self.corpus)
        return self._texts

    def get(self, idx) -> np.ndarray:
        """float32 vectors for the given row indices."""
        idx = np.asarray(idx, dtype=np.int64)
        return np.asarray(self._mm[idx], dtype=np.float32)

    def all_vectors(self) -> np.ndarray:
        """Every cached vector as float32 (only for complete caches of moderate size)."""
        return np.asarray(self._mm, dtype=np.float32)

    # -- filling
    async def ensure(self, idx, progress: Optional[Callable[[int, int], None]] = None, slice_size: int = 256) -> int:
        """Embed every clause in ``idx`` that has no vector yet. Returns how many were embedded."""
        from rag.vector_store import _embedder
        idx = np.unique(np.asarray(idx, dtype=np.int64))
        async with self.lock:
            missing = idx[~self.done[idx]] if self.dim is not None else idx
            total = int(missing.size)
            if not total:
                return 0
            texts = self.texts()
            done_n = 0
            for k in range(0, total, slice_size):
                part = missing[k:k + slice_size]
                vecs = await _embedder.embed_array([texts[i] for i in part], max_seq_length=self.max_tokens,
                                                   expect_truncation=True)
                if vecs.ndim != 2 or vecs.shape[0] != part.size:
                    raise ValueError(f"embedder returned {vecs.shape} for {part.size} clause texts")
                if self.dim is None:
                    await asyncio.to_thread(self._create, int(vecs.shape[1]))
                elif vecs.shape[1] != self.dim:
                    raise ValueError(f"embedding dimension changed ({vecs.shape[1]} != cached {self.dim})")
                self._mm[part] = vecs.astype(np.float16)
                self.done[part] = True
                done_n += int(part.size)
                await asyncio.to_thread(self._persist)
                if progress is not None:
                    try:
                        r = progress(done_n, total)
                        if hasattr(r, "__await__"):
                            await r
                    except Exception:  # noqa: BLE001
                        pass
            return done_n


_vc_lock = threading.Lock()
_vc_registry: dict = {}


def vector_cache_for(corpus, model_name: str, precision: str, max_tokens: int) -> CorpusVectorCache:
    """The shared CorpusVectorCache for (corpus, model, precision, token limit) - one instance per
    process, so background filling and scoring use the same object.
    """
    key = (corpus.fingerprint, len(corpus.entries), model_name, precision, int(max_tokens))
    with _vc_lock:
        vc = _vc_registry.get(key)
        if vc is None:
            vc = _vc_registry[key] = CorpusVectorCache(corpus, model_name, precision, max_tokens)
        return vc


# ── clause-title vectors (cheap semantic recall for the shortlist) ─────────────────────────────────
class TitleVectorCache(CorpusVectorCache):
    """One vector per DISTINCT clause title ("RRC reconfiguration", "Beam failure recovery" ...). Titles are
    ~10 tokens, so all of them cost a small fraction of the clause excerpts; they let the shortlist find
    clauses whose subject matches a patent even when they share few exact words with it."""
    _prefix = "bulktitle"

    def __init__(self, corpus, model_name: str, precision: str):
        """Vector cache over the DISTINCT clause titles (~10 tokens each, embedded once), with a map
        from title to the clauses that carry it - used to shortlist clauses under the semantically
        closest titles.
        """
        titles = [" ".join(str(e.clause_title or "").split()) or f"Clause {e.clause}" for e in corpus.entries]
        uniq: dict[str, int] = {}
        self.entry_title = np.fromiter((uniq.setdefault(t, len(uniq)) for t in titles), dtype=np.int64,
                                       count=len(titles))
        super().__init__(corpus, model_name, precision, 32, n=len(uniq))
        self._texts = list(uniq)
        order = np.argsort(self.entry_title, kind="stable")
        bounds = np.searchsorted(self.entry_title[order], np.arange(len(uniq) + 1))
        self._entries_of = (order, bounds)

    def entries_of(self, title_ids) -> np.ndarray:
        """Clause indices belonging to the given title ids."""
        order, bounds = self._entries_of
        parts = [order[bounds[t]:bounds[t + 1]] for t in np.asarray(title_ids, dtype=np.int64)]
        return np.concatenate(parts) if parts else np.zeros(0, dtype=np.int64)


_tc_registry: dict = {}


def title_cache_for(corpus, model_name: str, precision: str) -> TitleVectorCache:
    """The shared TitleVectorCache for (corpus, model, precision)."""
    key = (corpus.fingerprint, len(corpus.entries), model_name, precision)
    with _vc_lock:
        tc = _tc_registry.get(key)
        if tc is None:
            tc = _tc_registry[key] = TitleVectorCache(corpus, model_name, precision)
        return tc


def title_candidates(tc: TitleVectorCache, pvec: np.ndarray, entry_pool: Optional[np.ndarray], k_titles: int) -> np.ndarray:
    """Entries whose title is among the k most similar (dense) titles to the patent vector ``pvec``.
    ``entry_pool`` restricts to those entries (e.g. the declared specification); None = whole corpus,
    which needs every title vector (only used once the title cache is complete)."""
    if entry_pool is None:
        tids = np.arange(tc.n)
    else:
        tids = np.unique(tc.entry_title[entry_pool])
    if tids.size == 0 or k_titles <= 0:
        return np.zeros(0, dtype=np.int64)
    sims = tc.get(tids) @ pvec
    k = min(k_titles, tids.size)
    top = tids[np.argpartition(-sims, k - 1)[:k]] if k < tids.size else tids
    if entry_pool is None:
        # whole corpus: skip generic titles shared by many clauses ("General", "Scope", "Definitions" ...)
        order, bounds = tc._entries_of
        sizes = bounds[top + 1] - bounds[top]
        top = top[sizes <= 24]
    ents = tc.entries_of(top)
    if entry_pool is not None:
        ents = np.intersect1d(ents, entry_pool, assume_unique=False)
    return ents


def caches_for(corpus, vc) -> list:
    """[title cache, clause cache] for background filling (titles first: cheap, and they sharpen the
    shortlist for every later run)."""
    out = []
    try:
        from rag.vector_store import LocalEmbedder
        out.append(title_cache_for(corpus, getattr(_settings(), "EMBEDDING_MODEL", "model"), LocalEmbedder.precision))
    except Exception:  # noqa: BLE001
        pass
    out.append(vc)
    return out


# ── lexical shortlist ───────────────────────────────────────────────────────────────────────────
class LexicalIndex:
    """TF-IDF matrix over the clause corpus used for the fast lexical shortlist: for each Bulk row, the
    best clauses overall plus the best within the row's declared specification family.
    """
    def __init__(self, vectorizer, matrix, corpus):
        """Fitted vectorizer and its L2-normalised (clauses x vocabulary) matrix."""
        self.vectorizer = vectorizer
        self.matrix = matrix                      # (n_corpus, vocab) L2-normalised CSR
        self.matrix_t = matrix.T.tocsr()
        self.corpus = corpus

    def scores(self, texts: list[str]) -> np.ndarray:
        """Cosine scores of `texts` against every clause."""
        q = self.vectorizer.transform(texts)
        return np.asarray((q @ self.matrix_t).toarray(), dtype=np.float32)

    def shortlist(self, texts: list[str], families: list[Optional[str]], k_global: int, k_declared: int) -> list[np.ndarray]:
        """Per text: indices of the `k_global` best clauses overall plus the `k_declared` best in its
        declared family, computed 32 texts at a time to bound memory.
        """
        out: list = []
        for s0 in range(0, len(texts), 32):         # (32 x n_corpus) dense scores at a time - 25 MB at 200k clauses
            out.extend(self._shortlist(texts[s0:s0 + 32], families[s0:s0 + 32], k_global, k_declared))
        return out

    def _shortlist(self, texts, families, k_global, k_declared) -> list[np.ndarray]:
        """Shortlist for one block of texts (see `shortlist`)."""
        sims = self.scores(texts)
        n = sims.shape[1]
        out = []
        for r in range(sims.shape[0]):
            row = sims[r]
            kg = min(k_global, n)
            cand = np.argpartition(-row, kg - 1)[:kg] if kg < n else np.arange(n)
            fam_idx = self.corpus.family_indices(families[r] or "") if families[r] else []
            if fam_idx:
                fi = np.asarray(fam_idx, dtype=np.int64)
                kd = min(k_declared, fi.size)
                sub = row[fi]
                pick = fi[np.argpartition(-sub, kd - 1)[:kd]] if kd < fi.size else fi
                cand = np.concatenate([cand, pick])
            out.append(np.unique(cand.astype(np.int64)))
        return out


_lex_lock = threading.Lock()
_lex_cache: dict = {}


def _fit_lexical(corpus) -> LexicalIndex:
    """Fit (or load from the on-disk pickle cache) the TF-IDF lexical index for this corpus."""
    from sklearn.feature_extraction.text import TfidfVectorizer
    path = _cache_dir() / f"bulklex_{corpus.fingerprint}_{len(corpus.entries)}.pickle"
    if path.is_file():
        try:
            with open(path, "rb") as fh:
                vec, mat = pickle.load(fh)
            if mat.shape[0] == len(corpus.entries):
                return LexicalIndex(vec, mat, corpus)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Bulk lexical index cache unreadable ({e}); rebuilding.")
    vec = TfidfVectorizer(stop_words="english", sublinear_tf=True, max_df=0.3, dtype=np.float32,
                          token_pattern=r"(?u)\b[A-Za-z][A-Za-z0-9\-]{1,}\b")
    try:
        mat = vec.fit_transform(corpus_texts(corpus)).tocsr()
    except ValueError:      # tiny corpus where max_df removed every term
        vec = TfidfVectorizer(stop_words="english", sublinear_tf=True, dtype=np.float32)
        mat = vec.fit_transform(corpus_texts(corpus)).tocsr()
    try:
        tmp = path.with_suffix(".tmp")
        with open(tmp, "wb") as fh:
            pickle.dump((vec, mat), fh, protocol=pickle.HIGHEST_PROTOCOL)
        os.replace(tmp, path)
    except OSError as e:
        logger.warning(f"Could not write the Bulk lexical index cache: {e}")
    return LexicalIndex(vec, mat, corpus)


def lexical_index_for(corpus) -> LexicalIndex:
    """Blocking (seconds for 60,000 clauses; cached in memory and on disk) - call from a worker thread."""
    key = (corpus.fingerprint, len(corpus.entries))
    with _lex_lock:
        li = _lex_cache.get(key)
        if li is None:
            li = _lex_cache[key] = _fit_lexical(corpus)
        return li


def calibration_sample(corpus, size: int) -> np.ndarray:
    """Fixed pseudo-random sample of clause indices (seeded by the corpus fingerprint) used to measure
    the similarity floor - the same corpus always gets the same sample, so scores are reproducible.
    """
    n = len(corpus.entries)
    size = min(int(size), n)
    seed = int((corpus.fingerprint or "0")[:8], 16) if corpus.fingerprint else 0
    return np.sort(np.random.default_rng(seed).choice(n, size, replace=False)).astype(np.int64)


# ── background completion ───────────────────────────────────────────────────────────────────────
_bg_task: Optional[asyncio.Task] = None
_foreground = 0          # interactive jobs running (ISLD analyses, Bulk runs): background filling waits


def foreground_busy() -> bool:
    """True while an ISLD analysis or a Bulk run is in progress (or foreground_work is held)."""
    if _foreground > 0:
        return True
    try:
        from api.routes.cparas import analysis
        if analysis._control_events:
            return True
    except Exception:  # noqa: BLE001
        pass
    try:
        from api.routes.gitas import dashboard
        if dashboard._run_cancel_events:
            return True
    except Exception:  # noqa: BLE001
        pass
    return False


class foreground_work:
    """``with foreground_work():`` around interactive work - the background clause embedding pauses so the
    GPU (shared with Ollama) and the embedder are free for it."""
    def __enter__(self):
        """Mark foreground work (an ISLD analysis or a Bulk run) as in progress; background embedding
        pauses meanwhile.
        """
        global _foreground
        _foreground += 1
        return self

    def __exit__(self, *exc):
        """Foreground work finished."""
        global _foreground
        _foreground = max(0, _foreground - 1)
        return False

    async def __aenter__(self):
        """Async form of __enter__."""
        return self.__enter__()

    async def __aexit__(self, *exc):
        """Async form of __exit__."""
        return self.__exit__(*exc)


def precompute_enabled() -> bool:
    """Whether to fill clause vectors in the background (BULK_CORPUS_PRECOMPUTE: always / never / auto
    = only when a CUDA GPU is available).
    """
    v = str(getattr(_settings(), "BULK_CORPUS_PRECOMPUTE", "auto")).strip().lower()
    if v in ("1", "true", "yes", "on", "always"):
        return True
    if v in ("0", "false", "no", "off", "never"):
        return False
    try:
        from rag.vector_store import _embedder
        model = _embedder._model
        return model is not None and str(getattr(model, "device", "cpu")).startswith("cuda")
    except Exception:  # noqa: BLE001
        return False


def schedule_background_fill(*caches) -> None:
    """Embed what is still missing (clause titles first - cheap - then clause excerpts), slice by slice, in
    the background (GPU by default), pausing while an ISLD analysis or a Bulk run is in progress."""
    global _bg_task
    caches = [c for c in caches if c is not None and not c.complete()]
    if not caches or not precompute_enabled():
        return
    if _bg_task is not None and not _bg_task.done():
        return

    async def _run():
        """Background task: embed the remaining titles/clauses in small steps, pausing whenever
        foreground work is running, logging progress and stopping quietly on errors.
        """
        for vc in caches:
            logger.info(f"Bulk corpus: embedding the remaining {vc.n - vc.n_done:,} of {vc.n:,} "
                        f"{'clause titles' if isinstance(vc, TitleVectorCache) else 'clauses'} in the background.")
        try:
            for vc in caches:
                while not vc.complete():
                    while foreground_busy():
                        await asyncio.sleep(0.5)
                    missing = np.flatnonzero(~vc.done)[:512]
                    await vc.ensure(missing)
                    await asyncio.sleep(0.05)       # let interactive work in
            logger.info("Bulk corpus: every clause has a cached vector - Bulk runs now score exhaustively.")
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Bulk corpus background embedding stopped: {e}")

    try:
        _bg_task = asyncio.get_running_loop().create_task(_run())
    except RuntimeError:
        _bg_task = None
