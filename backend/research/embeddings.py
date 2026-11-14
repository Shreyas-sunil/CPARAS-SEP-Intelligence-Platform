"""
Research Embeddings Adapter
============================
The research/analytics module needs dense vector representations of patent
text and of the standards reference corpus, in the *same* vector space so
cosine similarity is meaningful.

Primary path: reuse the app's existing local embedding model
(`rag.vector_store._embedder`, e.g. BAAI/bge-m3 via sentence-transformers) —
this keeps "similar patent" / "standard recommendation" results consistent
with the rest of the platform and requires no new ML dependency.

Fallback path: if the embedding model isn't available (not pulled yet, no
GPU/CPU resources, offline first run, or — as in this sandbox — no access to
the model hub), fall back to a fully local TF-IDF + truncated-SVD embedding.
This keeps the research endpoints usable in degraded mode instead of hard
failing, and is what the bundled demo script uses.

IMPORTANT: callers should embed the standards corpus texts and the patent
texts *together in one call* (see `research.engine.analyze_patents`) so that,
in fallback mode, both live in the same fitted TF-IDF/SVD space.
"""

from __future__ import annotations

import asyncio
import logging

import numpy as np

logger = logging.getLogger(__name__)

_research_embedder: "ResearchEmbedder | None" = None
_fallback_logged = False


class ResearchEmbedder:
    """Uniform `embed_texts(list[str]) -> np.ndarray` over the app's embedder.

    `mode` / `last_dim` reflect only the *most recent* call. Because callers
    may embed the corpus and the patents in separate calls, don't treat
    `mode` after the fact as proof of what happened on an earlier call for
    a different batch — use `embed_corpus_and_patents()` below when you need
    a single, verifiable answer covering both.
    """

    def __init__(self):
        """Embedder used by the research engine and Bulk scoring. `mode` records which path produced
        the last vectors ('sentence-transformers', 'tfidf-fallback', 'empty') so results report it
        honestly.
        """
        self._mode = "unknown"
        self._last_dim: int | None = None
        # Set only by embed_corpus_for_batched_scoring() when running in
        # TF-IDF fallback mode -- see that method's docstring. None means
        # "not in a batched-scoring run using the fallback embedder", in
        # which case embed_patent_batch_for_scoring() embeds each batch
        # independently via the normal embed_texts() path.
        self._fallback_vectorizer = None
        self._fallback_svd = None

    @property
    def mode(self) -> str:
        """Which embedding path produced the most recent vectors."""
        return self._mode

    @property
    def last_dim(self) -> int | None:
        """Dimensionality of the vectors returned by the most recent call.

        Cheap, objective sanity check: the TF-IDF fallback always produces
        <= 64 dims (see `_tfidf_fallback_embed`'s `n_components`), while a
        real sentence-transformers model like BAAI/bge-m3 produces 1024.
        If `mode == "sentence-transformers"` but `last_dim` is suspiciously
        small, that combination itself is worth investigating rather than
        trusting the mode string alone.
        """
        return self._last_dim

    async def embed_corpus_and_patents(
        self, corpus_texts: list[str], patent_texts: list[str]
    ) -> tuple[np.ndarray, np.ndarray]:
        """Embed the standards corpus and the patent texts together, in one
        call, so they are guaranteed to land in the same vector space (see
        module docstring). Returns (corpus_vectors, patent_vectors) split
        back out; updates `mode`/`last_dim` for the combined call.

        NOTE: only suitable for small patent batches. Bulk/large-dataset
        callers (etsi_bulk/scoring.py) do NOT use this — they embed the
        corpus once via `embed_corpus_cached()` and then embed patents in
        bounded chunks via `embed_texts()`, so a 17,000-row run never
        builds one combined `corpus + all_patents` text list / embedding
        call. This method remains for the single-declaration (cPARAS)
        path, whose batches are small enough (typically single-digit to
        low hundreds of disclosures per upload) that combining is fine and
        keeps the corpus/patent vectors from the fallback TF-IDF embedder
        (which must be fit jointly) in the same space.
        """
        n_corpus = len(corpus_texts)
        combined = list(corpus_texts) + list(patent_texts)
        vectors = await self.embed_texts(combined)
        return vectors[:n_corpus], vectors[n_corpus:]

    async def embed_corpus_for_batched_scoring(self, corpus_texts: list[str]) -> np.ndarray:
        """Prepare the standards corpus ONCE at the start of a batched/
        chunked bulk-scoring run (see etsi_bulk/scoring.py), so the corpus
        is never re-embedded alongside every patent batch and a 17,000-row
        run never builds one giant `corpus + all_patents` text list.

        In sentence-transformers mode this is just a normal `embed_texts`
        call — the model itself defines a fixed vector space, so any later
        `embed_patent_batch_for_scoring()` call naturally lands in the same
        space with no extra bookkeeping.

        In TF-IDF fallback mode (no local embedding model available) a raw
        per-call `TfidfVectorizer.fit_transform` — the single-declaration
        path's `_tfidf_fallback_embed` — CANNOT be reused per-batch: fitting
        a fresh vectorizer on each batch would place each batch's vectors
        in a different, mutually-incomparable space (there is no shared
        vocabulary/axes across independently-fit calls), silently producing
        meaningless cross-batch similarity scores. So in this mode, this
        method instead FITS the TF-IDF vectorizer + SVD once on the corpus
        text (a small, static reference set — exactly the set whose
        vocabulary should define the space) and caches the fitted
        transformer; `embed_patent_batch_for_scoring()` then TRANSFORMS
        (never re-fits) every later batch into that same space.

        Call this exactly once per run, before the first patent batch.
        """
        self._fallback_vectorizer = None
        self._fallback_svd = None
        try:
            from rag.vector_store import _embedder
        except ImportError:
            _embedder = None

        if _embedder is not None:
            try:
                vecs = await self.embed_texts(list(corpus_texts))
                if self._mode == "sentence-transformers":
                    return vecs
                # Model present but this call itself degraded to fallback
                # (e.g. a malformed-output guard tripped) -- fall through
                # to the fit-once fallback path below instead of returning
                # a fallback embedding that later batches can't share.
            except Exception:
                pass

        loop = asyncio.get_event_loop()
        vectorizer, svd, corpus_vecs = await loop.run_in_executor(
            None, _fit_tfidf_fallback, list(corpus_texts)
        )
        self._fallback_vectorizer = vectorizer
        self._fallback_svd = svd
        self._mode = "tfidf-fallback"
        self._last_dim = corpus_vecs.shape[1] if corpus_vecs.ndim == 2 else None
        logger.info(
            f"Research embeddings (batched scoring): tfidf-fallback mode, "
            f"corpus vectorizer fit once and cached for this run, "
            f"dim={self._last_dim}, n_corpus={len(corpus_texts)}"
        )
        return corpus_vecs

    async def embed_patent_batch_for_scoring(self, texts: list[str]) -> np.ndarray:
        """Embed one bounded batch of patent texts for batched/chunked bulk
        scoring, landing in the same vector space as the corpus embedded by
        `embed_corpus_for_batched_scoring()` immediately before this run's
        first call here. Never combines this batch with the full corpus
        text or with any other batch — each call only ever holds this one
        batch's texts/vectors in memory."""
        if self._fallback_vectorizer is not None:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(
                None, _transform_tfidf_fallback, list(texts), self._fallback_vectorizer, self._fallback_svd
            )
        return await self.embed_texts(list(texts))

    async def embed_texts(self, texts: list[str]) -> np.ndarray:
        """Normalised vectors for `texts` from the shared local embedding model (rag/vector_store.py);
        falls back to TF-IDF vectors when the model is unavailable, so analyses still run offline
        without it.
        """
        if not texts:
            self._mode = "empty"
            self._last_dim = 0
            return np.zeros((0, 1), dtype="float32")

        try:
            from rag.vector_store import _embedder  # app's LocalEmbedder singleton
        except ImportError as e:
            logger.warning(
                f"rag.vector_store._embedder not importable ({e}); using "
                f"offline TF-IDF fallback for research analytics."
            )
            return await self._fallback(texts)

        if _embedder is None:
            logger.warning(
                "rag.vector_store._embedder is None (model not yet "
                "initialized); using offline TF-IDF fallback for research "
                "analytics."
            )
            return await self._fallback(texts)

        try:
            vectors = await _embedder.embed(list(texts))
        except Exception as e:
            # Only the actual model call is treated as "maybe unavailable" -
            # a failure here plausibly means the model/hub is unreachable.
            # A known-unavailable model (not downloaded, offline) is logged
            # once per process, without a traceback; anything else is a real
            # error and keeps the traceback.
            global _fallback_logged
            try:
                from rag.vector_store import EmbeddingModelUnavailable
            except ImportError:  # pragma: no cover
                EmbeddingModelUnavailable = ()  # type: ignore
            if EmbeddingModelUnavailable and isinstance(e, EmbeddingModelUnavailable):
                if not _fallback_logged:
                    logger.warning(f"{e} Research analytics use the offline TF-IDF embedder.")
                    _fallback_logged = True
            else:
                logger.warning(
                    f"Local embedding model call failed ({e}); using offline "
                    f"TF-IDF fallback for research analytics.",
                    exc_info=True,
                )
            return await self._fallback(texts)

        arr = np.asarray(vectors, dtype="float32")
        if arr.ndim != 2 or arr.shape[1] == 0:
            # A malformed shape is a real bug in the model/adapter, not
            # "unavailability" - log it plainly instead of folding it into
            # the same warning as a missing model.
            logger.error(
                f"Embedding model returned malformed output (shape "
                f"{arr.shape}); falling back to TF-IDF, but this indicates "
                f"a bug that should be investigated, not just anisotropy."
            )
            return await self._fallback(texts)

        self._mode = "sentence-transformers"
        self._last_dim = arr.shape[1]
        logger.info(
            f"Research embeddings: sentence-transformers mode, "
            f"dim={arr.shape[1]}, n={arr.shape[0]}"
        )
        return arr

    async def _fallback(self, texts: list[str]) -> np.ndarray:
        """TF-IDF fallback embedding in a worker thread (mode becomes 'tfidf-fallback')."""
        self._mode = "tfidf-fallback"
        loop = asyncio.get_event_loop()
        arr = await loop.run_in_executor(None, _tfidf_fallback_embed, texts)
        self._last_dim = arr.shape[1] if arr.ndim == 2 else None
        logger.info(
            f"Research embeddings: tfidf-fallback mode, "
            f"dim={self._last_dim}, n={len(texts)}"
        )
        return arr


def _tfidf_fallback_embed(texts: list[str], n_components: int = 64) -> np.ndarray:
    """
    Deterministic, fully offline TF-IDF + truncated-SVD embedding.
    Fits on exactly the texts provided (corpus + patents together), so the
    resulting vectors share one consistent space for cosine similarity.
    """
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.decomposition import TruncatedSVD
    from sklearn.preprocessing import normalize

    vec = TfidfVectorizer(stop_words="english", max_features=4096, sublinear_tf=True)
    X = vec.fit_transform(texts)

    n_comp = max(1, min(n_components, X.shape[0] - 1, X.shape[1] - 1))
    svd = TruncatedSVD(n_components=n_comp, random_state=42)
    reduced = svd.fit_transform(X)

    return normalize(reduced).astype("float32")


def _fit_tfidf_fallback(
    corpus_texts: list[str], n_components: int = 64
) -> tuple["TfidfVectorizer", "TruncatedSVD", np.ndarray]:
    """Fit (not transform-only) the TF-IDF + truncated-SVD pipeline on the
    corpus text and return the fitted transformer pair alongside the
    corpus's own vectors, so a caller can reuse the exact same fitted
    vectorizer/SVD to TRANSFORM later patent batches into the same space
    (see `embed_corpus_for_batched_scoring`'s docstring for why a fresh
    per-batch fit would be incorrect)."""
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.decomposition import TruncatedSVD
    from sklearn.preprocessing import normalize

    vec = TfidfVectorizer(stop_words="english", max_features=4096, sublinear_tf=True)
    X = vec.fit_transform(corpus_texts)

    n_comp = max(1, min(n_components, X.shape[0] - 1, X.shape[1] - 1))
    svd = TruncatedSVD(n_components=n_comp, random_state=42)
    reduced = svd.fit_transform(X)

    return vec, svd, normalize(reduced).astype("float32")


def _transform_tfidf_fallback(texts: list[str], vectorizer, svd) -> np.ndarray:
    """Transform (never re-fit) a later batch of texts into the vector
    space already fit on the corpus by `_fit_tfidf_fallback`."""
    from sklearn.preprocessing import normalize

    if not texts:
        return np.zeros((0, svd.n_components), dtype="float32")
    X = vectorizer.transform(texts)
    reduced = svd.transform(X)
    return normalize(reduced).astype("float32")


def get_research_embedder() -> ResearchEmbedder:
    """Process-wide ResearchEmbedder."""
    global _research_embedder
    if _research_embedder is None:
        _research_embedder = ResearchEmbedder()
    return _research_embedder


def cosine_sim_matrix(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Cosine similarity between every row of `a` and every row of `b`."""
    if a.size == 0 or b.size == 0:
        return np.zeros((a.shape[0], b.shape[0]), dtype="float32")
    a_n = a / np.clip(np.linalg.norm(a, axis=1, keepdims=True), 1e-9, None)
    b_n = b / np.clip(np.linalg.norm(b, axis=1, keepdims=True), 1e-9, None)
    return a_n @ b_n.T


def calibrate_similarity_matrix(
    raw: np.ndarray, *, low_percentile: float = 5.0, high_percentile: float = 95.0
) -> tuple[np.ndarray, dict]:
    """
    Rescale a matrix of raw cosine similarities into a statistically
    meaningful 0-1 range, instead of reporting the raw cosine value as if it
    were already a calibrated "percent similar".

    WHY THE OLD BEHAVIOR PRODUCED 0% / 50% FOR ALMOST EVERYTHING (root
    cause, as required by the audit):

    Sentence embedding models are anisotropic — the vector space they
    produce is not uniformly spread around the origin the way "cosine
    similarity" intuitively suggests. In practice this means:

      - Cosine similarity between two UNRELATED texts embedded by the same
        model does not cluster near 0; it clusters around a high, roughly
        constant baseline (commonly 0.3-0.6, depending on the model). This
        is a well-documented property of transformer sentence embeddings
        (their representations occupy a narrow cone in vector space rather
        than being spread across the full unit hypersphere).
      - Cosine similarity between two genuinely NEAR-DUPLICATE texts rarely
        approaches 1.0 either — it's compressed toward that same baseline
        from the other direction.

    The net effect: real, meaningfully-different similarity signals (a
    strong clause match vs. a weak one) live inside a very narrow raw-cosine
    band, e.g. 0.45-0.62. Reporting that raw value directly as a percentage
    means almost every patent's "similarity" rounds to a number close to
    the model's baseline — which is exactly the reported "50% for almost
    everything" symptom. Separately, when `build_patent_text()` falls back
    to its generic placeholder sentence (fast-mode extraction with no
    enrichment fields populated — see that function's docstring), several
    unrelated patents can embed to literally the same vector, and if that
    generic sentence happens to sit in a low-similarity region of the
    corpus space for every entry, the (uncalibrated) reported score for
    all of them floors out near the observed minimum — the "0% for almost
    everything" symptom.

    THE FIX: rather than applying an arbitrary fixed rescale (e.g. a
    hand-picked "0.3 -> 0%, 0.9 -> 100%" mapping, which is just a
    differently-arbitrary constant), calibrate each run's OWN observed
    distribution of raw cosine values. The Nth/100-Nth percentile of the
    actual comparisons made in this run become the empirical floor/ceiling,
    and every score is linearly rescaled against those — a statistically
    grounded approach (order statistics of the observed sample) rather than
    a hand-tuned constant, and one that stays correct regardless of which
    embedding model/backend is in use (each has its own baseline/spread).

    Ordering (which patent is "more similar" than which) is fully
    preserved — this is a monotonic affine transform, not a re-ranking.

    Args:
        raw: matrix of raw cosine similarities (any shape).
        low_percentile / high_percentile: which percentiles of the
            observed distribution anchor the 0%/100% ends. 5th/95th by
            default — deliberately not min/max, so a single outlier
            comparison can't single-handedly stretch or compress the whole
            scale.

    Returns:
        (calibrated, meta) where `calibrated` is same-shape as `raw` with
        values clipped to [0, 1], and `meta` is
        {"low": float, "high": float, "degenerate": bool} for audit/display
        (e.g. showing the calibration anchors in the report) and so callers
        can tell when calibration fell back to a pass-through because the
        observed distribution had no meaningful spread.
    """
    if raw.size == 0:
        return raw, {"low": 0.0, "high": 1.0, "degenerate": True}

    flat = raw.reshape(-1).astype("float64")
    lo = float(np.percentile(flat, low_percentile))
    hi = float(np.percentile(flat, high_percentile))

    if hi - lo < 1e-6:
        # Degenerate spread — e.g. every patent embedded to (near-)identical
        # vectors (the fast-mode generic-fallback-text collapse described
        # above), or a corpus/patent set too small for percentiles to be
        # meaningful. Rescaling against a ~0 range would amplify floating-
        # point noise into huge, meaningless swings, so fall back to a
        # simple clip of the raw values instead of dividing by ~0.
        return np.clip(raw, 0.0, 1.0).astype("float32"), {"low": lo, "high": hi, "degenerate": True}

    calibrated = (flat - lo) / (hi - lo)
    calibrated = np.clip(calibrated, 0.0, 1.0).reshape(raw.shape).astype("float32")
    return calibrated, {"low": round(lo, 4), "high": round(hi, 4), "degenerate": False}


def calibrate_match_scores(
    raw: np.ndarray, *, low_percentile: float = 5.0
) -> tuple[np.ndarray, dict]:
    """
    Calibrate patent -> standard-clause similarities WITHOUT saturating the
    best matches at 100% (v0.4.1 fix — use this, not
    `calibrate_similarity_matrix`, for any score reported as a patent's
    "Semantic Similarity" to its matched clause).

    THE BUG THIS FIXES: `calibrate_similarity_matrix` maps the 95th
    percentile of ALL patent x corpus pairs to 100%. The corpus has ~54
    clause entries, and the score that gets reported is each patent's BEST
    match (top-1 of 54, i.e. its top ~2%). The top-1 of a row is therefore
    above the whole matrix's 95th percentile essentially always, so it was
    clipped to 1.0 — every patent reported "Semantic Similarity 100%",
    regardless of how well it actually matched. (Measured on
    sample_data/sample_bulk_export.xlsx: 60 / 60 rows at exactly 1.0.)

    THE FIX: keep the anisotropy correction at the bottom (the run's own
    `low_percentile` of raw cosines = the "unrelated text" baseline -> 0%),
    but anchor the top at the true maximum of cosine similarity, 1.0 (an
    identical-meaning match), instead of at a percentile that the reported
    values sit above by construction:

        calibrated = clip((raw - floor) / (1 - floor), 0, 1)

    Monotonic (ordering preserved), continuous, model-agnostic (the floor is
    measured per run), and it only reaches 100% for a genuinely
    near-identical match.
    """
    if raw.size == 0:
        return raw, {"low": 0.0, "high": 1.0, "degenerate": True, "method": "floor-to-unity"}
    flat = raw.reshape(-1).astype("float64")
    lo = float(np.percentile(flat, low_percentile))
    lo = min(lo, 0.999)  # guard: a run where (almost) everything is identical
    calibrated = np.clip((raw.astype("float64") - lo) / (1.0 - lo), 0.0, 1.0).astype("float32")
    return calibrated, {"low": round(lo, 4), "high": 1.0, "degenerate": False, "method": "floor-to-unity"}
