"""
Vector Store
============
Unified interface over Qdrant, ChromaDB, and FAISS.
Local embeddings generated with sentence-transformers (no cloud needed).
"""

import asyncio
import contextlib
import contextvars
import gc
import logging
import threading
import weakref
from collections import OrderedDict
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional
import uuid

from core.config import settings
from rag import embedding_budget as eb

logger = logging.getLogger(__name__)
_vs_instance = None

# Longest sequence (tokens) the CURRENT caller wants the embedder to feed the model. Set by the ETSI
# indexer (see embedding_limits()); search queries and every other caller leave it unset and keep
# the model's own limit, so retrieval behaviour is unchanged.
_seq_limit: "contextvars.ContextVar[Optional[int]]" = contextvars.ContextVar("embedding_seq_limit", default=None)
_budget_scale: "contextvars.ContextVar[float]" = contextvars.ContextVar("embedding_budget_scale", default=1.0)


@contextlib.contextmanager
def embedding_limits(max_seq_length: Optional[int], budget_scale: float = 1.0):
    """Within this block every embed() call truncates/plans at ``max_seq_length`` tokens, and plans
    its batches against ``budget_scale`` x the configured memory budget (a retry after an
    out-of-memory error runs with a smaller scale)."""
    t1 = _seq_limit.set(max_seq_length)
    t2 = _budget_scale.set(float(budget_scale))
    try:
        yield
    finally:
        _seq_limit.reset(t1)
        _budget_scale.reset(t2)


class EmbeddingMemoryError(MemoryError):
    """Embedding ran out of memory and could not be made to fit even at a batch of one.
    The vector store was not touched by the failed call."""

    def __init__(self, message: str, *, tokens: Optional[int] = None):
        """`tokens` = the per-text token length that failed, so callers can retry with shorter inputs."""
        super().__init__(message)
        self.tokens = tokens


@dataclass
class EmbedStats:
    """What the last embed() call actually did - surfaced in import reports, never hidden."""
    texts: int = 0
    batches: int = 0
    max_batch: int = 0
    max_tokens: int = 0
    oom_retries: int = 0
    min_batch_used: int = 0
    truncated: int = 0            # texts longer than the limit that the model had to cut
    planned_peak_mb: float = 0.0


# ── Embedding Engine ───────────────────────────────────────────────────────────


def resolve_embedding_device(setting: str) -> str:
    """EMBEDDING_DEVICE -> a device torch can actually use.

    "auto" (default since v0.4.2): "cuda" when PyTorch sees an NVIDIA GPU,
    "mps" on Apple Silicon, else "cpu". An explicit "cuda"/"mps" that is
    not available falls back to "cpu" with a warning instead of crashing.
    (v0.4 hard-coded "cpu", which is why the embedding step stopped using
    the GPU.)
    """
    want = (setting or "auto").strip().lower()
    if want == "cpu":
        return "cpu"
    try:
        import torch
        cuda = torch.cuda.is_available()
        mps = bool(getattr(torch.backends, "mps", None) and torch.backends.mps.is_available())
        cuda_built = bool(getattr(torch.version, "cuda", None))
    except Exception:
        return "cpu"
    if want in ("auto", "cuda") and cuda:
        return "cuda"
    if want in ("auto", "mps") and mps:
        return "mps"
    if want != "auto":
        logger.warning(f"EMBEDDING_DEVICE={setting} is not available in this PyTorch build; using CPU.")
    elif not cuda_built:
        logger.info("Embeddings run on CPU: the installed PyTorch is the CPU-only build "
                    "(re-run setup.ps1 to install the CUDA build when an NVIDIA GPU is present).")
    return "cpu"

class EmbeddingModelUnavailable(RuntimeError):
    """The sentence-transformers model can't be loaded (not downloaded,
    no network, missing package). Callers fall back to the offline TF-IDF
    embedder (research/embeddings.py) where that is possible."""


def _embedding_model_is_local(name: str) -> bool:
    """True if `name` is a local directory, or is already in the Hugging
    Face cache — checked WITHOUT any network access."""
    import os
    if os.path.isdir(name):
        return True
    try:
        from huggingface_hub import snapshot_download
        snapshot_download(repo_id=name, local_files_only=True)
        return True
    except Exception:
        return False


def _flag(value, default: bool) -> bool:
    """Parse a true/false/auto setting value; 'auto' (or anything unrecognised) returns `default`."""
    v = str(value if value is not None else "auto").strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    return default


def _apply_fast_precision(model) -> str:
    """v0.4.7 speed: run the embedding model in the fastest precision the device supports.

    * CUDA: half precision (fp16) - about 2x faster and half the VRAM of fp32 (bge-m3 is published
      for fp16 inference). Matters on 4-6 GB laptop GPUs that also hold the Ollama model.
    * CPU: dynamic int8 quantisation of the Linear layers - typically 2-3x faster on a laptop CPU,
      with cosine scores within ~0.01 of fp32.
    Both are controlled by EMBEDDING_FP16 / EMBEDDING_CPU_INT8 (auto|true|false). Any failure keeps fp32.
    Returns the precision label used in on-disk vector cache keys."""
    dev = str(getattr(model, "device", "cpu"))
    try:
        if dev.startswith("cuda") and _flag(getattr(settings, "EMBEDDING_FP16", "auto"), True):
            model.half()
            return "fp16"
        if dev.startswith("cpu") and _flag(getattr(settings, "EMBEDDING_CPU_INT8", "auto"), True):
            import torch
            qd = getattr(getattr(torch, "ao", None), "quantization", None)
            qd = getattr(qd, "quantize_dynamic", None) or torch.quantization.quantize_dynamic
            import warnings
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                # in place: swaps every nn.Linear inside the model, whatever the module layout of the
                # installed sentence-transformers version
                qd(model, {torch.nn.Linear}, dtype=torch.qint8, inplace=True)
            return "int8"
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Fast embedding precision not applied ({type(e).__name__}: {e}); using fp32.")
    return "fp32"


class LocalEmbedder:
    """Generates embeddings locally using sentence-transformers.

    Offline behaviour: if the model is not already downloaded (by setup.ps1 /
    setup.sh, or by copying the Hugging Face cache from another machine) and
    EMBEDDING_ALLOW_DOWNLOAD is false, loading fails immediately with a clear
    message instead of retrying downloads (previously an offline machine
    hung the analysis on repeated connection attempts to huggingface.co).
    The failure is remembered for the process so every later call fails
    instantly; restart the backend after installing the model.
    """
    _model = None
    _load_lock: Optional["asyncio.Lock"] = None
    _load_error: Optional[str] = None
    precision: str = "fp32"          # set when the model loads (see _apply_fast_precision)

    # v0.4.7 speed: interactive calls (ISLD retrieval, a handful of texts) must never queue behind a bulk
    # job embedding tens of thousands of corpus clauses. Large calls are cut into slices and give way
    # between slices whenever a small call is waiting.
    _SLICE = 64
    _interactive_waiting = 0
    # v0.4.7 speed: in-process LRU of vectors for search/scoring calls (never for the indexer, which
    # embeds each chunk once). The same clause candidates, prior patents and standards entries are
    # re-embedded on every ISLD run otherwise.
    _vec_cache: "OrderedDict[tuple, object]" = OrderedDict()
    _vec_cache_model_id: Optional[int] = None
    _vec_cache_lock = threading.Lock()

    def _load_model_sync(self):
        """The actual blocking model construction -- always run via
        run_in_executor, never called directly from the event loop."""
        import os
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as e:
            raise EmbeddingModelUnavailable(f"sentence-transformers is not installed ({e}).")
        name = settings.EMBEDDING_MODEL
        local = _embedding_model_is_local(name)
        if not local and not settings.EMBEDDING_ALLOW_DOWNLOAD:
            raise EmbeddingModelUnavailable(
                f"Embedding model '{name}' is not downloaded on this machine and "
                "EMBEDDING_ALLOW_DOWNLOAD is false. Run the setup script (it pre-downloads it), "
                "or set EMBEDDING_ALLOW_DOWNLOAD=true in backend/.env once while online. "
                "Using the offline TF-IDF fallback where possible."
            )
        if local:
            # Never contact the Hub for an already-cached model (no version
            # checks on an air-gapped machine).
            os.environ.setdefault("HF_HUB_OFFLINE", "1")
        device = resolve_embedding_device(settings.EMBEDDING_DEVICE)
        logger.info(f"Loading embedding model: {name} (device={device}, "
                    f"{'local cache' if local else 'downloading'})")
        try:
            model = SentenceTransformer(name, device=device)
        except Exception as e:
            if device == "cpu":
                raise
            logger.warning(f"Embedding model failed to load on {device} ({type(e).__name__}: {e}); "
                           "falling back to CPU.")
            model = SentenceTransformer(name, device="cpu")
        LocalEmbedder.precision = _apply_fast_precision(model)
        dim = model.get_sentence_embedding_dimension()
        logger.info(f"Embedding model loaded: {name}, dim={dim}, precision={LocalEmbedder.precision}")
        if settings.EMBEDDING_DIM and dim != settings.EMBEDDING_DIM:
            logger.error(
                f"EMBEDDING_DIM setting ({settings.EMBEDDING_DIM}) does not "
                f"match the loaded model's actual dimension ({dim}). Qdrant/"
                f"FAISS indices are sized from EMBEDDING_DIM, so this mismatch "
                f"will cause insert/search failures or silently wrong indices."
            )
        return model

    async def _get_model_async(self):
        """v0.3-continuation fix: the FIRST call used to construct
        SentenceTransformer(...) directly on the event loop thread --
        loading model weights from disk is a multi-second, CPU/IO-bound
        blocking operation, and running it inline stalls EVERY other
        coroutine for that entire duration, including concurrent SQLite
        writes from other ingestion workers. Under load, that stall was
        long enough to push several pending writes past SQLite's
        busy_timeout window, producing the reported cascading 'database is
        locked' failures. Now always loaded via run_in_executor, guarded
        by a lock so concurrent callers await the same single load rather
        than racing to construct the model multiple times."""
        if self._model is not None:
            return self._model
        if self._load_lock is None:
            self._load_lock = asyncio.Lock()
        if LocalEmbedder._load_error:
            raise EmbeddingModelUnavailable(LocalEmbedder._load_error)
        async with self._load_lock:
            if self._model is None:  # re-check: another caller may have finished while we awaited the lock
                loop = asyncio.get_running_loop()
                try:
                    self._model = await loop.run_in_executor(None, self._load_model_sync)
                except Exception as e:
                    LocalEmbedder._load_error = str(e) if isinstance(e, EmbeddingModelUnavailable) \
                        else f"Could not load embedding model '{settings.EMBEDDING_MODEL}': {type(e).__name__}: {e}"
                    logger.warning(LocalEmbedder._load_error)
                    raise EmbeddingModelUnavailable(LocalEmbedder._load_error) from e
        return self._model

    # ── memory-bounded encoding (v0.4.4.1) ──────────────────────────────────────
    _encode_threads_lock = threading.Lock()          # one encode at a time touches model.max_seq_length
    last_stats: EmbedStats = EmbedStats()
    _sems: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()

    def _semaphore(self) -> asyncio.Semaphore:
        """One semaphore per running event loop (a module-level one breaks later asyncio.run() calls)."""
        loop = asyncio.get_running_loop()
        sem = LocalEmbedder._sems.get(loop)
        if sem is None:
            # One encode at a time: each call holds its own activations (v0.4.4 ran ETSI_IMPORT_BATCH_SIZE of
            # them at once), and model.max_seq_length is shared state.
            sem = LocalEmbedder._sems[loop] = asyncio.Semaphore(1)
        return sem

    @staticmethod
    def _model_limit(model) -> int:
        """The model's maximum sequence length in tokens (512 when it does not say)."""
        return int(getattr(model, "max_seq_length", None) or 512)

    def _count_tokens(self, model, texts: list[str], cap: int) -> list[int]:
        """Token count per text (special tokens included), capped at ``cap`` because the model
        truncates there.  Falls back to a deliberately pessimistic 1 token / 2 chars."""
        tok = getattr(model, "tokenizer", None)
        if tok is not None:
            try:
                enc = tok(texts, add_special_tokens=True, truncation=False, padding=False, verbose=False)
                return [min(len(ids), cap) for ids in enc["input_ids"]]
            except Exception as e:  # noqa: BLE001
                logger.debug(f"tokenizer length count failed ({e}); using the character estimate")
        return [min(cap, len(t) // 2 + 2) for t in texts]

    def _device_budget_bytes(self, model) -> int:
        """Memory available for one encode call: free CUDA memory on a GPU, free system RAM otherwise
        (with headroom), used to size batches so embedding never runs the machine out of memory.
        """
        dev = str(getattr(model, "device", "cpu"))
        free = eb.free_ram_mb()
        if dev.startswith("cuda"):
            try:
                import torch
                free_b, _tot = torch.cuda.mem_get_info()
                return int(free_b * 0.5)
            except Exception:  # noqa: BLE001
                pass
        return eb.resolve_budget_bytes(int(settings.EMBEDDING_MEMORY_BUDGET_MB), free)

    def _max_batch(self, model) -> int:
        """Upper bound on the batch size for the model's device (EMBEDDING_BATCH_SIZE; additionally
        EMBEDDING_CPU_BATCH_SIZE on CPU).
        """
        dev = str(getattr(model, "device", "cpu"))
        if dev.startswith("cpu"):
            return max(1, min(int(settings.EMBEDDING_BATCH_SIZE), int(settings.EMBEDDING_CPU_BATCH_SIZE)))
        return max(1, int(settings.EMBEDDING_BATCH_SIZE))

    def _encode_bounded(self, model, texts: list[str], limit: Optional[int], scale: float = 1.0, quiet: bool = False):
        """Encode ``texts`` in memory-planned batches; on an out-of-memory error split the batch and
        retry; raise EmbeddingMemoryError only when a single text cannot be encoded.  Output order
        equals input order.  Nothing is written anywhere, so a failure leaves no partial state."""
        import numpy as np
        stats = EmbedStats(texts=len(texts))
        model_limit = self._model_limit(model)
        eff = min(model_limit, int(limit)) if limit else model_limit
        full = self._count_tokens(model, texts, 10**9)       # tokenise once
        lengths = [min(n, eff) for n in full]                 # what the model will actually see
        stats.max_tokens = max(lengths) if lengths else 0
        stats.truncated = sum(1 for n in full if n > eff)     # texts the model has to cut
        if stats.truncated and not quiet:
            logger.warning(f"{stats.truncated} text(s) exceed the {eff}-token embedding limit and will be truncated "
                           f"(longest {max(full)} tokens).")
        shape = eb.model_shape_from(model)
        budget = int(self._device_budget_bytes(model) * max(0.05, min(1.0, scale)))
        batches = eb.plan_batches(lengths, self._max_batch(model), budget, shape, strict=False)
        stats.batches = len(batches)
        stats.max_batch = max((len(b) for b in batches), default=0)
        stats.min_batch_used = stats.max_batch
        stats.planned_peak_mb = max((eb.estimate_peak_bytes(len(b), lengths[b[0]], shape) for b in batches), default=0) / 2**20

        out = [None] * len(texts)

        def run(idx: list[int]):
            """Encode one group of texts; on an out-of-memory error split the group and retry the
            halves, down to single texts, before giving up.
            """
            try:
                vecs = model.encode([texts[i] for i in idx], batch_size=len(idx), normalize_embeddings=True,
                                    show_progress_bar=False, convert_to_numpy=True)
                for i, v in zip(idx, vecs):
                    out[i] = v
            except BaseException as e:  # noqa: BLE001 - classified just below
                if not eb.is_memory_error(e):
                    raise
                gc.collect()
                try:
                    import torch
                    if torch.cuda.is_available():
                        torch.cuda.empty_cache()
                except Exception:  # noqa: BLE001
                    pass
                if len(idx) == 1:
                    raise EmbeddingMemoryError(
                        f"out of memory embedding a single {lengths[idx[0]]}-token text "
                        f"({len(texts[idx[0]])} characters): {e}", tokens=lengths[idx[0]]) from e
                stats.oom_retries += 1
                half = len(idx) // 2
                stats.min_batch_used = min(stats.min_batch_used, half)
                logger.warning(f"Out of memory on a batch of {len(idx)} (longest {lengths[idx[0]]} tokens); "
                               f"retrying as two batches of {half} and {len(idx) - half}.")
                run(idx[:half])
                run(idx[half:])

        with LocalEmbedder._encode_threads_lock:
            prev = model.max_seq_length
            model.max_seq_length = eff
            try:
                for b in batches:
                    run(b)
            finally:
                model.max_seq_length = prev
        LocalEmbedder.last_stats = stats
        return np.asarray(out, dtype="float32")

    async def embed(self, texts: list[str], *, max_seq_length: Optional[int] = None) -> list[list[float]]:
        """Embeddings for ``texts`` (same order).  Memory is planned per batch and bounded; out-of-memory
        splits the batch rather than crashing.  ``max_seq_length`` (or embedding_limits()) caps the
        tokens per text; the default is the model's own limit (searches and bulk scoring unchanged)."""
        if not texts:
            return []
        arr = await self.embed_array(texts, max_seq_length=max_seq_length)
        return arr.tolist()

    async def embed_array(self, texts: list[str], *, max_seq_length: Optional[int] = None,
                          progress=None, expect_truncation: bool = False):
        """Same as embed() but returns a float32 numpy array (no Python-list round trip).

        v0.4.7: cached texts are served from the in-process LRU; the rest are encoded in slices so a
        small interactive call waiting for the model is served between slices of a large one.
        ``progress(done, total)`` (optional, sync) is called after each slice."""
        import numpy as np
        texts = list(texts)
        if not texts:
            return np.zeros((0, 0), dtype="float32")
        model = await self._get_model_async()
        limit = max_seq_length if max_seq_length is not None else _seq_limit.get()
        scale = _budget_scale.get()
        loop = asyncio.get_running_loop()
        cache_on = limit is None and int(getattr(settings, "EMBEDDING_CACHE_SIZE", 50000) or 0) > 0
        out: list = [None] * len(texts)
        todo: list[int] = []
        if cache_on:
            with LocalEmbedder._vec_cache_lock:
                if LocalEmbedder._vec_cache_model_id != id(model):
                    LocalEmbedder._vec_cache.clear()
                    LocalEmbedder._vec_cache_model_id = id(model)
                cache = LocalEmbedder._vec_cache
                for i, t in enumerate(texts):
                    v = cache.get(t)
                    if v is not None:
                        cache.move_to_end(t)
                        out[i] = v
                    else:
                        todo.append(i)
        else:
            todo = list(range(len(texts)))

        if todo:
            # identical texts inside one call are encoded once
            uniq: dict[str, list[int]] = {}
            for i in todo:
                uniq.setdefault(texts[i], []).append(i)
            utexts = list(uniq)
            big = len(utexts) > self._SLICE
            slices = [utexts[k:k + self._SLICE] for k in range(0, len(utexts), self._SLICE)] if big else [utexts]
            agg = EmbedStats(texts=len(utexts))
            done = 0
            for sl in slices:
                if big:
                    while LocalEmbedder._interactive_waiting > 0:
                        await asyncio.sleep(0.02)
                    async with self._semaphore():
                        arr = await loop.run_in_executor(None, lambda sl=sl: self._encode_bounded(model, sl, limit, scale, expect_truncation))
                else:
                    LocalEmbedder._interactive_waiting += 1
                    try:
                        sem = self._semaphore()
                        await sem.acquire()
                    finally:
                        LocalEmbedder._interactive_waiting -= 1
                    try:
                        arr = await loop.run_in_executor(None, lambda sl=sl: self._encode_bounded(model, sl, limit, scale, expect_truncation))
                    finally:
                        sem.release()
                st = LocalEmbedder.last_stats
                agg.batches += st.batches
                agg.max_batch = max(agg.max_batch, st.max_batch)
                agg.max_tokens = max(agg.max_tokens, st.max_tokens)
                agg.oom_retries += st.oom_retries
                agg.min_batch_used = st.min_batch_used if not agg.min_batch_used else min(agg.min_batch_used, st.min_batch_used)
                agg.truncated += st.truncated
                agg.planned_peak_mb = max(agg.planned_peak_mb, st.planned_peak_mb)
                for t, v in zip(sl, arr):
                    for i in uniq[t]:
                        out[i] = v
                if cache_on:
                    cap = int(getattr(settings, "EMBEDDING_CACHE_SIZE", 50000))
                    with LocalEmbedder._vec_cache_lock:
                        cache = LocalEmbedder._vec_cache
                        for t, v in zip(sl, arr):
                            cache[t] = v
                            cache.move_to_end(t)
                        while len(cache) > cap:
                            cache.popitem(last=False)
                done += len(sl)
                if progress is not None:
                    try:
                        progress(done, len(utexts))
                    except Exception:  # noqa: BLE001
                        pass
            if len(slices) > 1:
                LocalEmbedder.last_stats = agg
            # v0.4.7: hand freed activation memory back to the driver when idle - the GPU is shared with
            # Ollama, and memory the embedder merely keeps cached can push the LLM partly onto the CPU.
            if str(getattr(model, "device", "cpu")).startswith("cuda") and LocalEmbedder._interactive_waiting == 0:
                try:
                    import torch
                    torch.cuda.empty_cache()
                except Exception:  # noqa: BLE001
                    pass
        return np.asarray(out, dtype="float32")

    async def fit_to_limit(self, texts: list[str], max_tokens: int, overlap_tokens: int = 32) -> list[list[tuple[str, int, int]]]:
        """For each text return its parts as (part_text, char_start, char_end).  A text that fits
        ``max_tokens`` is returned whole (one part, offsets 0..len).  A longer one is cut into
        overlapping token windows - NOTHING is truncated or dropped.  Falls back to a pessimistic
        character window when the tokenizer cannot give offsets."""
        model = await self._get_model_async()
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: self._fit_sync(model, texts, int(max_tokens), int(overlap_tokens)))

    def _fit_sync(self, model, texts, max_tokens, overlap_tokens):
        """Make every text fit the model's token limit: texts within `max_tokens` are kept, longer ones
        are split into overlapping token windows (character spans from the tokenizer's offsets)
        whose vectors the caller averages.
        """
        from utils.text_chunker import hard_split_spans
        window = max(16, max_tokens - 2 - 8)            # 2 special tokens + a margin for re-tokenisation drift
        overlap = max(0, min(overlap_tokens, window // 4))
        tok = getattr(model, "tokenizer", None)
        res = []
        for text in texts:
            parts = None
            if tok is not None:
                try:
                    enc = tok(text, add_special_tokens=False, return_offsets_mapping=True, truncation=False, verbose=False)
                    offs = enc["offset_mapping"]
                    n = len(offs)
                    if n <= window:
                        parts = [(text, 0, len(text))]
                    else:
                        parts, start = [], 0
                        while start < n:
                            end = min(n, start + window)
                            a, b = offs[start][0], offs[end - 1][1]
                            parts.append((text[a:b], a, b))
                            if end >= n:
                                break
                            start = end - overlap
                except Exception as e:  # noqa: BLE001 - slow-tokenizer / no offsets
                    logger.debug(f"token-window split unavailable ({e}); using the character window")
                    parts = None
            if parts is None:
                max_chars = max(64, window * 2)
                parts = [(text[a:b], a, b) for a, b in hard_split_spans(text, max_chars)] if len(text) > max_chars else [(text, 0, len(text))]
            res.append(parts)
        return res

    async def embed_one(self, text: str) -> list[float]:
        """Vector for a single text."""
        results = await self.embed([text])
        return results[0]


_embedder = LocalEmbedder()


# ── Base Interface ─────────────────────────────────────────────────────────────

class BaseVectorStore(ABC):
    """Interface of the Standards Corpus vector store (ChromaDB default, Qdrant, FAISS). Chunks are
    dicts {id, content, metadata}; metadata carries source_type and provenance (spec, version,
    clause, etsi_document_id...) used for filtering and deletion.
    """
    _ready: bool = False

    async def ensure_ready(self) -> None:
        """initialize() exactly once per store instance.  The FastAPI app calls this at start-up, but
        the CLI import (python -m etsi_corpus.cli import-local, i.e. setup.ps1 -ImportAll) never
        did: get_vector_store() returned an UNINITIALISED store, so ChromaVectorStore.upsert failed
        with  'NoneType' object has no attribute 'upsert'  for every document (after embedding it)."""
        if not self._ready:
            await self.initialize()
            self._ready = True

    @abstractmethod
    async def upsert(self, chunks: list[dict]) -> int:
        """Insert/update chunks. Each chunk: {id, content, metadata}. Returns count."""
        ...

    @abstractmethod
    async def similarity_search(self, query: str, top_k: int = 8,
                                filter_source: Optional[str] = None,
                                min_score: float = 0.5) -> list[dict]:
        """Top-k chunks by cosine similarity to `query` (optionally one source_type) with score >=
        min_score: [{content, score, metadata}].
        """
        ...

    @abstractmethod
    async def get_stats(self) -> dict:
        """{backend, indexed_chunks, vector_dim, ...} for health and status displays."""
        ...

    async def delete_by_id_prefix(self, id_prefix: str) -> int:
        """Remove every indexed chunk whose id starts with `id_prefix`.
        Returns the number removed. Used by ingestion/legacy_cleanup.py to
        remove retired demo chunks without touching real crawled/uploaded
        chunks (which use different id prefixes -- see
        ingestion/spec_corpus.py's SOURCE_TYPE-based ids).
        Not @abstractmethod so this doesn't force every subclass to
        implement it before it existed; each backend below overrides it."""
        raise NotImplementedError

    async def list_chunk_ids_for_document(self, etsi_document_id: int) -> set[str]:
        """Ids of every chunk this store holds for one ETSI document (paged, bounded by that one
        document's chunk count - never the whole collection)."""
        raise NotImplementedError

    async def delete_chunk_ids(self, ids) -> int:
        """Delete exactly these chunk ids (no prefix matching). Returns how many were requested."""
        raise NotImplementedError

    @abstractmethod
    async def initialize(self):
        """Open/create the underlying collection or index files."""
        ...


# ── Qdrant Backend ─────────────────────────────────────────────────────────────

_QDRANT_NS = uuid.UUID("6f1c2f0e-5a0b-4a52-9a53-2f2d2c7a9b11")


def _stable_point_id(chunk_id: str) -> str:
    """Deterministic Qdrant point id for a chunk id.  v0.4.4 used abs(hash(chunk_id)) % 10**9, but
    Python randomises str hashes per process (PYTHONHASHSEED), so re-running an import produced NEW
    point ids and silently DUPLICATED every chunk; it also collides at ~10^9."""
    return str(uuid.uuid5(_QDRANT_NS, chunk_id))


class QdrantVectorStore(BaseVectorStore):
    """
    Uses Qdrant running locally (Docker or binary).
    docker run -p 6333:6333 qdrant/qdrant
    """

    def __init__(self):
        """Collection name from QDRANT_COLLECTION; the client connects lazily."""
        self.collection = settings.QDRANT_COLLECTION
        self._client = None

    def _get_client(self):
        """Async Qdrant client for QDRANT_URL (created on first use)."""
        if self._client is None:
            from qdrant_client import AsyncQdrantClient
            self._client = AsyncQdrantClient(url=settings.QDRANT_URL)
        return self._client

    async def initialize(self):
        """Create the collection (cosine distance, EMBEDDING_DIM) if it does not exist yet."""
        from qdrant_client.models import Distance, VectorParams
        client = self._get_client()
        collections = await client.get_collections()
        names = [c.name for c in collections.collections]
        if self.collection not in names:
            await client.create_collection(
                collection_name=self.collection,
                vectors_config=VectorParams(size=settings.EMBEDDING_DIM, distance=Distance.COSINE),
            )
            logger.info(f"Created Qdrant collection: {self.collection}")
        else:
            logger.info(f"Qdrant collection exists: {self.collection}")

    async def upsert(self, chunks: list[dict]) -> int:
        """Embed and upsert chunks (point id derived from the chunk id, the id kept in the payload);
        returns the number stored.
        """
        from qdrant_client.models import PointStruct
        client = self._get_client()
        texts = [c["content"] for c in chunks]
        vectors = await _embedder.embed(texts)

        points = []
        for chunk, vec in zip(chunks, vectors):
            point_id = chunk.get("id") or str(uuid.uuid4())
            points.append(PointStruct(
                id=point_id if isinstance(point_id, int) else _stable_point_id(str(point_id)),
                vector=vec,
                payload={
                    "content": chunk["content"],
                    "source_type": chunk.get("metadata", {}).get("source_type", "unknown"),
                    # Qdrant's numeric point id (above) is a hash, so the
                    # original string chunk id is kept in the payload too --
                    # needed for prefix-based lookups like
                    # delete_by_id_prefix(), since the numeric id alone
                    # can't be reversed back to it.
                    "chunk_id": str(point_id),
                    **chunk.get("metadata", {}),
                }
            ))

        await client.upsert(collection_name=self.collection, points=points)
        return len(points)

    async def similarity_search(self, query: str, top_k: int = 8,
                                filter_source: Optional[str] = None,
                                min_score: float = 0.5) -> list[dict]:
        """Vector search in Qdrant with an optional source_type payload filter."""
        from qdrant_client.models import Filter, FieldCondition, MatchValue
        client = self._get_client()
        query_vec = await _embedder.embed_one(query)

        query_filter = None
        if filter_source:
            query_filter = Filter(must=[FieldCondition(key="source_type", match=MatchValue(value=filter_source))])

        results = await client.search(
            collection_name=self.collection,
            query_vector=query_vec,
            limit=top_k,
            query_filter=query_filter,
            score_threshold=min_score,
        )
        return [
            {"content": r.payload.get("content", ""), "score": r.score, "metadata": r.payload}
            for r in results
        ]

    async def get_stats(self) -> dict:
        """Point count and vector size of the collection."""
        client = self._get_client()
        info = await client.get_collection(self.collection)
        return {
            "backend": "qdrant",
            "collection": self.collection,
            "indexed_chunks": info.points_count,
            "vector_dim": settings.EMBEDDING_DIM,
        }

    async def list_chunk_ids_for_document(self, etsi_document_id: int) -> set[str]:
        """Every chunk id stored for one imported ETSI document (scrolls through the payload filter)."""
        from qdrant_client.models import Filter, FieldCondition, MatchValue
        client = self._get_client()
        flt = Filter(must=[FieldCondition(key="etsi_document_id", match=MatchValue(value=int(etsi_document_id)))])
        ids: set[str] = set()
        offset = None
        while True:
            points, offset = await client.scroll(collection_name=self.collection, scroll_filter=flt, limit=500,
                                                 offset=offset, with_payload=["chunk_id"], with_vectors=False)
            for pt in points:
                cid = (pt.payload or {}).get("chunk_id")
                if cid:
                    ids.add(str(cid))
            if offset is None:
                break
        return ids

    async def delete_chunk_ids(self, ids) -> int:
        """Delete chunks by chunk id (batches of 500)."""
        from qdrant_client.models import Filter, FieldCondition, MatchAny
        ids = list(ids)
        client = self._get_client()
        for i in range(0, len(ids), 500):
            flt = Filter(must=[FieldCondition(key="chunk_id", match=MatchAny(any=ids[i:i + 500]))])
            await client.delete(collection_name=self.collection, points_selector=flt)
        return len(ids)

    async def delete_by_id_prefix(self, id_prefix: str) -> int:
        """Delete every chunk whose id starts with `id_prefix` (e.g. all chunks of one source),
        filtering on the stored id text.
        """
        from qdrant_client.models import Filter, FieldCondition, MatchText
        client = self._get_client()
        # Qdrant point ids here are derived via abs(hash(str_id)) % 10**9
        # (see upsert() above) -- the original string id isn't retrievable
        # from the numeric point id alone, so deletion filters on the
        # payload's own "chunk_id" field instead (added below).
        flt = Filter(must=[FieldCondition(key="chunk_id", match=MatchText(text=id_prefix))])
        result = await client.delete(collection_name=self.collection, points_selector=flt)
        return getattr(result, "operation_id", 0) or 0


# ── ChromaDB Backend ───────────────────────────────────────────────────────────

class ChromaVectorStore(BaseVectorStore):
    """Fully local ChromaDB — no server needed, just a directory."""

    def __init__(self):
        """Persistent ChromaDB client/collection, opened lazily."""
        self._client = None
        self._collection = None

    def _get_client(self):
        """PersistentClient at CHROMADB_PATH with telemetry disabled (offline deployments must never
        phone home).
        """
        if self._client is None:
            import chromadb
            # anonymized_telemetry=False: ChromaDB otherwise tries to phone
            # home (PostHog) on start-up — unacceptable for an offline /
            # air-gapped deployment, and noisy errors when there's no network.
            from chromadb.config import Settings as ChromaSettings
            self._client = chromadb.PersistentClient(
                path=settings.CHROMADB_PATH,
                settings=ChromaSettings(anonymized_telemetry=False, allow_reset=False),
            )
        return self._client

    async def initialize(self):
        """Get or create the cosine-space collection."""
        client = self._get_client()
        self._collection = client.get_or_create_collection(
            name=settings.QDRANT_COLLECTION,
            metadata={"hnsw:space": "cosine"},
        )
        logger.info(f"ChromaDB collection ready: {settings.QDRANT_COLLECTION}")

    async def upsert(self, chunks: list[dict]) -> int:
        """Embed and upsert chunks (ids kept; generated when missing); returns the number stored."""
        texts = [c["content"] for c in chunks]
        vectors = await _embedder.embed(texts)
        ids = [c.get("id") or str(uuid.uuid4()) for c in chunks]
        metadatas = [c.get("metadata", {}) for c in chunks]

        self._collection.upsert(
            ids=[str(i) for i in ids],
            embeddings=vectors,
            documents=texts,
            metadatas=metadatas,
        )
        return len(chunks)

    async def similarity_search(self, query: str, top_k: int = 8,
                                filter_source: Optional[str] = None,
                                min_score: float = 0.5) -> list[dict]:
        """Nearest chunks by cosine similarity, optionally filtered by source_type; distances are
        converted to similarity scores.
        """
        query_vec = await _embedder.embed_one(query)
        where = {"source_type": filter_source} if filter_source else None
        results = self._collection.query(
            query_embeddings=[query_vec],
            n_results=top_k,
            where=where,
            include=["documents", "distances", "metadatas"],
        )
        out = []
        for doc, dist, meta in zip(
            results["documents"][0],
            results["distances"][0],
            results["metadatas"][0],
        ):
            score = 1.0 - dist  # cosine distance → similarity
            if score >= min_score:
                out.append({"content": doc, "score": score, "metadata": meta})
        return out

    async def get_stats(self) -> dict:
        """Chunk count and vector dimension of the collection."""
        return {
            "backend": "chromadb",
            "collection": settings.QDRANT_COLLECTION,
            "indexed_chunks": self._collection.count() if self._collection else 0,
            "vector_dim": settings.EMBEDDING_DIM,
        }

    async def list_chunk_ids_for_document(self, etsi_document_id: int) -> set[str]:
        """Pages through ONE document's chunks via a metadata filter.  (v0.4.4 re-indexing called
        collection.get() with no filter - the id of EVERY chunk in the collection - once per
        document, an O(corpus) memory and time cost on each of 32,440 documents.)"""
        if not self._collection:
            return set()
        ids: set[str] = set()
        offset, page = 0, 1000
        while True:
            got = self._collection.get(where={"etsi_document_id": int(etsi_document_id)}, include=[],
                                       limit=page, offset=offset)
            batch = got.get("ids", []) or []
            ids.update(str(i) for i in batch)
            if len(batch) < page:
                break
            offset += page
        return ids

    async def delete_chunk_ids(self, ids) -> int:
        """Delete chunks by id (batches of 1000)."""
        ids = [str(i) for i in ids]
        if not self._collection or not ids:
            return 0
        for i in range(0, len(ids), 1000):
            self._collection.delete(ids=ids[i:i + 1000])
        return len(ids)

    async def delete_by_id_prefix(self, id_prefix: str) -> int:
        """Delete every chunk whose id starts with `id_prefix`; uses the etsi_document_id metadata
        filter for ETSI-document prefixes instead of listing all ids.
        """
        if not self._collection:
            return 0
        # ETSI document prefixes ("etsi_<n>_") have a metadata filter; use it instead of listing
        # every id in the collection.
        import re as _re
        m = _re.fullmatch(r"etsi_(\d+)_", id_prefix)
        if m:
            ids = [i for i in await self.list_chunk_ids_for_document(int(m.group(1))) if i.startswith(id_prefix)]
        else:
            existing = self._collection.get(include=[])
            ids = [i for i in existing.get("ids", []) if str(i).startswith(id_prefix)]
        if ids:
            await self.delete_chunk_ids(ids)
        return len(ids)


# ── FAISS Backend ──────────────────────────────────────────────────────────────

class FAISSVectorStore(BaseVectorStore):
    """Flat FAISS index — no server, pure in-memory with disk persistence."""

    def __init__(self):
        """In-memory FAISS index plus a parallel list of chunk documents, persisted at
        FAISS_INDEX_PATH.
        """
        self._index = None
        self._docs: list[dict] = []
        self._path = settings.FAISS_INDEX_PATH

    async def initialize(self):
        """Load the saved index and documents, or start an empty inner-product index."""
        import faiss
        import pickle
        import os
        if os.path.exists(f"{self._path}.index"):
            self._index = faiss.read_index(f"{self._path}.index")
            with open(f"{self._path}.docs", "rb") as f:
                self._docs = pickle.load(f)
            logger.info(f"Loaded FAISS index: {len(self._docs)} chunks")
        else:
            self._index = faiss.IndexFlatIP(settings.EMBEDDING_DIM)  # inner product = cosine with normed vecs
            logger.info("Created new FAISS index")

    async def upsert(self, chunks: list[dict]) -> int:
        """Embed and add chunks; an id already present is replaced (never duplicated), then the index
        is saved.
        """
        import faiss, numpy as np, pickle
        texts = [c["content"] for c in chunks]
        vectors = await _embedder.embed(texts)
        arr = np.array(vectors, dtype="float32")
        # Idempotent: a chunk id that is already present is replaced, never duplicated (a retried
        # import appended a second copy of every chunk before v0.4.4.1).
        incoming = {str(c.get("id")) for c in chunks if c.get("id")}
        if incoming and any(str(d.get("id")) in incoming for d in self._docs):
            self._drop_ids(incoming)
        self._index.add(arr)
        self._docs.extend(chunks)
        faiss.write_index(self._index, f"{self._path}.index")
        with open(f"{self._path}.docs", "wb") as f:
            pickle.dump(self._docs, f)
        return len(chunks)

    def _drop_ids(self, ids: set) -> int:
        """Remove chunks by exact id, rebuilding the flat index from its OWN stored vectors
        (reconstruct) - never by re-embedding the surviving corpus."""
        import faiss
        import numpy as np
        keep = [i for i, d in enumerate(self._docs) if str(d.get("id", "")) not in ids]
        removed = len(self._docs) - len(keep)
        if removed == 0:
            return 0
        new_index = faiss.IndexFlatIP(settings.EMBEDDING_DIM)
        if keep:
            allv = self._index.reconstruct_n(0, self._index.ntotal)
            new_index.add(np.ascontiguousarray(allv[keep], dtype="float32"))
        self._docs = [self._docs[i] for i in keep]
        self._index = new_index
        return removed

    async def list_chunk_ids_for_document(self, etsi_document_id: int) -> set[str]:
        """Chunk ids belonging to one imported ETSI document."""
        return {str(d.get("id")) for d in self._docs
                if d.get("metadata", {}).get("etsi_document_id") == int(etsi_document_id)}

    async def delete_chunk_ids(self, ids) -> int:
        """Remove chunks by id, rebuild the index and save it."""
        import faiss
        import pickle
        ids = {str(i) for i in ids}
        n = self._drop_ids(ids)
        if n:
            faiss.write_index(self._index, f"{self._path}.index")
            with open(f"{self._path}.docs", "wb") as f:
                pickle.dump(self._docs, f)
        return n

    async def similarity_search(self, query: str, top_k: int = 8,
                                filter_source: Optional[str] = None,
                                min_score: float = 0.5) -> list[dict]:
        """Inner-product search (normalised vectors = cosine), over-fetching when a source_type filter
        is applied.
        """
        import numpy as np
        if self._index.ntotal == 0:
            return []
        query_vec = await _embedder.embed_one(query)
        arr = np.array([query_vec], dtype="float32")
        scores, indices = self._index.search(arr, min(top_k * 2, self._index.ntotal))
        out = []
        for score, idx in zip(scores[0], indices[0]):
            if idx < 0 or score < min_score:
                continue
            doc = self._docs[idx]
            if filter_source and doc.get("metadata", {}).get("source_type") != filter_source:
                continue
            out.append({"content": doc["content"], "score": float(score), "metadata": doc.get("metadata", {})})
            if len(out) >= top_k:
                break
        return out

    async def get_stats(self) -> dict:
        """Document count and vector dimension."""
        return {
            "backend": "faiss",
            "indexed_chunks": len(self._docs),
            "vector_dim": settings.EMBEDDING_DIM,
        }

    async def delete_by_id_prefix(self, id_prefix: str) -> int:
        """FAISS's flat index has no native delete, so this rebuilds the index from its own stored
        vectors (v0.4.4.1: previously it RE-EMBEDDED every surviving chunk on each call)."""
        import faiss
        import pickle

        removed = self._drop_ids({str(d.get("id", "")) for d in self._docs if str(d.get("id", "")).startswith(id_prefix)})
        if removed:
            faiss.write_index(self._index, f"{self._path}.index")
            with open(f"{self._path}.docs", "wb") as f:
                pickle.dump(self._docs, f)
        return removed


# ── Factory + Init ─────────────────────────────────────────────────────────────

def _create_vector_store() -> BaseVectorStore:
    """Instantiate the backend named by VECTOR_BACKEND (chromadb / qdrant / faiss)."""
    backend = settings.VECTOR_BACKEND
    if backend == "qdrant":
        return QdrantVectorStore()
    elif backend == "chromadb":
        return ChromaVectorStore()
    elif backend == "faiss":
        return FAISSVectorStore()
    else:
        raise ValueError(f"Unknown vector backend: {backend}")


async def init_vector_store():
    """Start-up: create and initialise the vector store. A failure (e.g. chromadb not installed) is
    logged and leaves search disabled instead of stopping the app.
    """
    global _vs_instance
    _vs_instance = _create_vector_store()
    try:
        await _vs_instance.ensure_ready()
    except Exception as e:
        logger.warning(f"Vector store init failed (non-fatal, search disabled): {e}")


def get_vector_store() -> BaseVectorStore:
    """The process-wide vector store (created on first use)."""
    global _vs_instance
    if _vs_instance is None:
        _vs_instance = _create_vector_store()
    return _vs_instance
