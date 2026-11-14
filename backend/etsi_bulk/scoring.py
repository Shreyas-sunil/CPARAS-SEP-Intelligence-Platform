"""
ETSI Bulk Declaration Export — Essentiality Scoring
======================================================
Turns parsed bulk-export rows (schema.py / parser.py) into the same kind of
"Essentiality Determination" the existing single-declaration pipeline
produces via `research/engine.py` — reusing that module's embedder,
standards corpus, and status thresholds rather than inventing a second
scoring system.

LARGE-DATASET ARCHITECTURE (v0.5 fix — see CHANGELOG / engineering report)
-----------------------------------------------------------------------------
The original implementation embedded `corpus_texts + patent_texts` for the
WHOLE dataset in one call and built one `(n_patents, n_corpus)` similarity
matrix, all inside a single synchronous request. That doesn't scale to
17,000-row workbooks: the request runs for as long as the whole dataset
takes to embed+score+persist, with no progress feedback, and a single bad
row could raise partway through and lose the entire run.

`score_records_batched()` below fixes this:

  1. The standards corpus is embedded ONCE per run (`embed_corpus_for_
     batched_scoring`), never re-embedded per batch.
  2. Patents are embedded and scored in bounded chunks
     (`settings.SCORING_BATCH_SIZE`, default 256) — never all 17,000 at
     once, and never combined with the corpus text in one embedding call.
  3. A batch's own `(batch_size, n_corpus)` raw similarity matrix is
     discarded as soon as that batch's top-1 match + score has been
     extracted from it — the full `(n_patents, n_corpus)` matrix is never
     held in memory. Calibration (which needs the *distribution* of raw
     similarities across the whole run, not just one batch — see
     `research.embeddings.calibrate_similarity_matrix`) is computed from a
     size-bounded reservoir sample of raw values collected across batches,
     not the retained full matrix — see `_ReservoirSampler` below.
  4. A batch whose embedding call fails is retried per-record so ONE
     malformed row can't take down the other 255 rows in its batch (or the
     other 16,999 rows in the run); the failed record(s) come back with a
     `_row_failed=True` marker and no score fields, instead of raising.
  5. The function is an async generator: it YIELDS each scored batch (plus
     running progress/run_meta) as soon as that batch is ready, so the
     caller (api/routes/gitas/dashboard.py) can persist incrementally in
     batched DB transactions and report progress, instead of waiting for
     every one of 17,000 rows to be scored before writing anything.

`score_records()` is kept as a thin convenience wrapper over the batched
generator for callers (tests, small/interactive callers) that just want the
full scored list back — it does not reintroduce the old single-call/single-
matrix behavior; it simply drains the same batched generator.

THREE-SIGNAL BREAKDOWN (mirrors the "Score Breakdown" panel in the patent
detail UI: Claim Match / Semantic Similarity / Standard Coverage) — the
`claim_match`/`standard_coverage` formulas themselves now live in
`research.three_signal_scoring` (shared with the cPARAS pipeline in
`research/engine.py` — see that module's docstring for why the two
pipelines share the field DEFINITIONS but not necessarily identical input
evidence):

  semantic_similarity — calibrated cosine similarity between the patent's
    text (title + illustrative part + declared standard) and the single
    best-matching entry in the offline standards corpus.

  claim_match — a proxy for "how much of the patent's disclosed subject
    matter is reflected in the matched clause". The bulk export has no
    claim text, so this blends explicit-disclosure flag + lexical overlap
    + similarity smoothing. A heuristic stand-in — a real claim-text
    upload through /api/analysis/analyze produces an LLM-graded claim
    mapping instead, which is more authoritative.

  standard_coverage — how much of the *declared spec family* this patent's
    matched clause represents.

All three are heuristic estimates over an illustrative offline corpus (see
`research/standards_corpus.CORPUS_DISCLAIMER`) — surfaced to the UI with
the same "not a legal opinion" caveat already used elsewhere in this app.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
from datetime import datetime, date
from typing import AsyncIterator, Awaitable, Callable, Optional

import numpy as np

from core.config import settings
from research.embeddings import get_research_embedder, cosine_sim_matrix, calibrate_similarity_matrix
from research.standards_corpus import (
    STANDARDS_CORPUS, DOMAIN_KEYWORDS, family_of, domain_of_family,
    entries_by_family, clause_groups_by_family, CORPUS_DISCLAIMER,
)
# v0.4.4: which corpus Bulk scores against (the validated 3GPP dataset corpus when CPARAS_DATASET_ROOT is
# configured, otherwise the illustrative one - unchanged). The names above are kept for backward
# compatibility; scoring itself goes through the active BulkCorpus.
from research.bulk_corpus import get_bulk_corpus, embed_corpus_cached, BulkCorpus
from research.engine import _predicted_status  # reuse the exact same status thresholds
from research.three_signal_scoring import (
    tokenize, claim_match_score, standard_coverage_score, matched_key_terms,
    essentiality_from_status, ESSENTIAL, POTENTIAL,
)

logger = logging.getLogger(__name__)

ProgressCallback = Optional[Callable[[dict], Optional[Awaitable[None]]]]


async def _emit(progress_cb: ProgressCallback, payload: dict) -> None:
    """Send a progress payload to the optional callback (sync or async)."""
    if progress_cb is None:
        return
    result = progress_cb(payload)
    if hasattr(result, "__await__"):
        await result


class _ReservoirSampler:
    """Bounded-memory reservoir sample of raw similarity values, used to
    compute calibration percentiles (5th/95th) over the whole run's
    similarity distribution WITHOUT retaining the full (n_patents x
    n_corpus) matrix. Standard reservoir sampling (Algorithm R): every
    value seen has an equal probability of ending up in the final sample,
    regardless of how many values are streamed through, and memory is
    capped at `max_size` regardless of dataset size."""

    __slots__ = ("max_size", "_buf", "_seen", "_rng")

    def __init__(self, max_size: int = 200_000, seed: int = 42):
        """Bounded uniform sample (reservoir sampling, fixed seed) of similarity values seen across the
        whole run, so percentile calibration works on 100k+ rows without keeping every score.
        """
        self.max_size = max_size
        self._buf: list[float] = []
        self._seen = 0
        self._rng = random.Random(seed)

    # v0.4.4: a dataset corpus has thousands of entries, so one batch can hold millions of similarity
    # values; looping over every value in Python would dominate the run. Above this many values per call
    # a seeded uniform random subset is fed to the reservoir instead (the 5th-percentile baseline it
    # estimates is unchanged in expectation). The small illustrative corpus stays far below the cap, so
    # its results are bit-for-bit what they were.
    _PER_CALL_CAP = 20_000

    def add_many(self, values: np.ndarray) -> None:
        """Add a block of similarity values (a very large block is first subsampled to bound the per-
        call cost).
        """
        flat = values.reshape(-1)
        if flat.size > self._PER_CALL_CAP:
            pick = np.random.default_rng(self._seen + 1).choice(flat.size, self._PER_CALL_CAP, replace=False)
            flat = flat[pick]
        for v in flat:
            self._seen += 1
            if len(self._buf) < self.max_size:
                self._buf.append(float(v))
            else:
                j = self._rng.randint(0, self._seen - 1)
                if j < self.max_size:
                    self._buf[j] = float(v)

    def percentiles(self, low: float, high: float) -> tuple[float, float]:
        """The (low, high) percentiles of the sampled values; (0, 1) before any value was seen."""
        if not self._buf:
            return 0.0, 1.0
        arr = np.asarray(self._buf, dtype="float64")
        return float(np.percentile(arr, low)), float(np.percentile(arr, high))


# v0.4.1: declared-family tie-break. When the best clause from the family the
# declarant ACTUALLY declared scores within this raw-cosine margin of the
# overall best clause, prefer the declared one. Observed on the sample
# workbook: "conditional handover" (declared TS 38.331 / 5.3.5) scored 0.543
# against TS 38.331 5.3.5 "RRC Reconfiguration and Handover" but 0.552
# against TS 23.501 "Session and Mobility Management", purely on generic
# words like "network" -- a statistical tie decided against the declaration.
# A clearly better match in another family (margin exceeded) still wins, so
# genuine declaration conflicts remain visible.
DECLARED_FAMILY_TIEBREAK_MARGIN = 0.02

_FAMILY_INDEX: dict[str, list[int]] = {}      # kept for backward compatibility (illustrative corpus only)
for _i, _e in enumerate(STANDARDS_CORPUS):
    _FAMILY_INDEX.setdefault(_e.family, []).append(_i)


def _declared_family(row: dict) -> Optional[str]:
    """Specification family the row's declarant declared ('38.331'), from the Standard text or the
    TGPP_NUMBER column.
    """
    return family_of(row.get("standard_raw") or "") or (
        str(row.get("spec_number")) if row.get("spec_number") else None
    )


def _pick_best(raw_row: np.ndarray, row: dict, corpus: Optional["BulkCorpus"] = None) -> int:
    """Index of the best-matching clause for one row: the arg-max, except that on a near-tie a clause
    from the DECLARED family wins over a different family (declared-family tie-break).
    """
    corpus = corpus or get_bulk_corpus()
    entries = corpus.entries
    best = int(np.argmax(raw_row))
    idx = corpus.family_indices(_declared_family(row) or "")
    if idx and entries[best].family != entries[idx[0]].family:
        d_best = max(idx, key=lambda j: float(raw_row[j]))
        if float(raw_row[d_best]) >= float(raw_row[best]) - DECLARED_FAMILY_TIEBREAK_MARGIN:
            return d_best
    return best


def _truthy(v) -> bool:
    """Interpret Yes/No / 1/0 / True/False spreadsheet values."""
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "y")
    return False


def _release_label(spec_version, spec_number) -> str:
    """3GPP release numbering: the major version digit IS the release number
    from Rel-4 onward (e.g. TS 38.331 v16.5.0 -> Rel-16)."""
    if spec_version:
        m = re.match(r"[vV]?(\d{1,2})\.", str(spec_version).strip())
        if m:
            major = int(m.group(1))
            if major >= 4:
                return f"Rel-{major}"
            if major <= 3:
                return "Legacy (R99/Rel-4 era)"
    return "Unknown"


def _technology_label(row: dict) -> str:
    """Highest declared generation of the row as a label (6G > 5G NR > LTE > 3G / UMTS > 2G / GSM), or
    'Unknown'.
    """
    if _truthy(row.get("gen_6g")):
        return "6G"
    if _truthy(row.get("gen_5g")):
        return "5G NR"
    if _truthy(row.get("gen_4g")):
        return "LTE"
    if _truthy(row.get("gen_3g")):
        return "3G / UMTS"
    if _truthy(row.get("gen_2g")):
        return "2G / GSM"
    return "Other"


def _domain_from_keywords(text: str) -> str | None:
    """Technical domain whose keyword phrases best match the text (None when nothing matches)."""
    toks = tokenize(text)
    best_domain, best_score = None, 0
    for domain, phrases in DOMAIN_KEYWORDS.items():
        score = 0
        for phrase in phrases:
            if all(w in toks for w in phrase.split()):
                score += 1
        if score > best_score:
            best_domain, best_score = domain, score
    return best_domain


def _patent_text(row: dict) -> str:
    """The text embedded for a Bulk row: title, declared standard, illustrative part and spec
    number/version (bulk exports carry no claim text).
    """
    parts = [
        str(row.get("title") or ""),
        f"Declared standard: {row.get('standard_raw') or ''}",
        f"Illustrative part: {row.get('illustrative_part') or ''}",
        f"3GPP spec {row.get('spec_number') or ''} version {row.get('spec_version') or ''}",
    ]
    return ". ".join(p for p in parts if p.strip())


def _fmt_date(v):
    """ISO string for date/datetime values, anything else unchanged."""
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    return v


def _failed_row(row: dict, reason: str) -> dict:
    """A row that could not be scored (e.g. its batch's embedding call
    failed even on a per-record retry). Carries no score fields — NEVER a
    zero/fake fallback — and is explicitly flagged so the caller persists
    it as FAILED/NEEDS_REVIEW rather than silently dropping it. This is
    what keeps one malformed patent from taking down the other 16,999."""
    out = dict(row)
    out.update({
        "signature_date": _fmt_date(row.get("signature_date")),
        "filing_date": _fmt_date(row.get("filing_date")),
        "technology": _technology_label(row),
        "release": _release_label(row.get("spec_version"), row.get("spec_number")),
        "domain": None,
        "essentiality": None,
        "confidence": None,
        "claim_match": None,
        "semantic_similarity": None,
        "standard_coverage": None,
        "matched_standard": None,
        "matched_clause": None,
        "matched_clause_title": None,
        "matched_domain": None,
        "declared_family_match": None,
        "matched_key_terms": [],
        "matched_key_terms_count": 0,
        "total_key_terms": 0,
        "_row_failed": True,
        "_row_failure_reason": reason,
    })
    return out


def _finalize_row(row: dict, entry, sim_score: float, explicit: bool, corpus: Optional["BulkCorpus"] = None) -> dict:
    """Build the fully-scored row for one record, given its already-
    computed calibrated top-1 similarity and matched corpus entry. Pure/
    cheap (no embedding, no matrix work) — safe to call in the second pass
    over already-embedded batches."""
    declared_family = _declared_family(row)
    declared_match = bool(declared_family and declared_family == entry.family)

    patent_toks = tokenize(str(row.get("title") or "")) | tokenize(str(row.get("illustrative_part") or ""))
    claim_match = claim_match_score(
        patent_tokens=patent_toks,
        clause_title=entry.clause_title,
        clause_keywords=entry.keywords,
        semantic_similarity=sim_score,
        explicit_disclosure=explicit,
    )
    matched_phrases = matched_key_terms(patent_toks, entry.keywords)
    total_key_terms = max(len(entry.keywords), 1)

    corpus = corpus or get_bulk_corpus()
    groups = corpus.clause_groups_by_family(entry.family)
    standard_coverage = standard_coverage_score(
        semantic_similarity=sim_score,
        clause_group_count=len(groups),
        declared_family_match=declared_match,
    )

    # v0.4.1: overall confidence = confidence that this patent is an SEP,
    # blended from ALL the evidence this row has (similarity, claim match,
    # standard coverage, declared-family match) via the shared formula in
    # research.three_signal_scoring.overall_sep_confidence. Previously only
    # similarity was passed in, so "Overall Confidence" was just a copy of
    # Semantic Similarity. The Essential/Potential/Non-Essential label is
    # derived from that same number, so the two always agree.
    sep_conf = None  # no LLM-graded confidence available on this ingestion path
    status_label, blended_conf = _predicted_status(
        sim_score, declared_match, sep_conf,
        claim_match=claim_match,
        standard_coverage=standard_coverage,
        verification_ratio=None, completeness_score=None, ocr_confidence=None,
        has_collision=False,
    )
    essentiality = essentiality_from_status(status_label)

    # ── Declared-family evidence gate (bulk path only) ──────────────────
    # "Essential" asserts that this patent is essential to the standard the
    # declarant DECLARED it against. On the bulk path the only evidence for
    # that assertion is a top-1 argmax over a small illustrative corpus
    # (11 families), so a row whose best match is some OTHER family has no
    # evidence for the claim it would be making -- it has evidence of a
    # topical neighbour, which is what "Potential" means.
    #
    # Without this gate a 500-row sample came back 496 Essential while the
    # customer's own acceptance rule (Essential = evidence level 3 =
    # declared family confirmed) gave 9, and the two numbers were derived
    # from the same export. The gate makes CPARAS_Bulk_Essentiality agree
    # with CPARAS_Bulk_Evidence_Level instead of contradicting it.
    #
    # Deliberately applied HERE and not in research.three_signal_scoring /
    # research.engine._predicted_status: the single-declaration (ISLD)
    # pipeline in this same product has LLM-graded claim mappings as
    # additional evidence for the same assertion, so its thresholds are
    # its own and are left untouched.
    if essentiality == ESSENTIAL and not declared_match:
        essentiality = POTENTIAL

    # NOTE: an expired/ceased legal status no longer silently downgrades
    # "Essential" to "Potential" here. That downgrade changed the label
    # without changing the confidence -- the "Potential SEP / 100%"
    # contradiction. Essentiality is a technical determination; a lapsed
    # legal status is reported separately (LEGAL_STATUS_LAPSED reason code
    # and a validation note in the CPARAS Bulk export).

    domain = corpus.domain_of_family(entry.family) or _domain_from_keywords(_patent_text(row)) or "Other"

    scored = dict(row)
    scored.update({
        "signature_date": _fmt_date(row.get("signature_date")),
        "filing_date": _fmt_date(row.get("filing_date")),
        "technology": _technology_label(row),
        "release": _release_label(row.get("spec_version"), row.get("spec_number")),
        "domain": domain,
        "essentiality": essentiality,
        "confidence": round(blended_conf, 4),
        "claim_match": round(claim_match, 4),
        "semantic_similarity": round(sim_score, 4),
        "standard_coverage": round(standard_coverage, 4),
        "matched_standard": entry.standard_version,
        "matched_clause": entry.clause_label,
        "matched_clause_title": entry.clause_title,
        "matched_domain": entry.domain,
        "declared_family_match": declared_match,
        "matched_key_terms": matched_phrases,
        "matched_key_terms_count": len(matched_phrases),
        "total_key_terms": total_key_terms,
        "_row_failed": False,
    })
    return scored


def _pick_best_subset(raw: np.ndarray, cand: np.ndarray, row: dict, corpus: "BulkCorpus") -> tuple[int, float]:
    """_pick_best() over a shortlist: ``raw[k]`` is the similarity to clause ``cand[k]``. Same declared-family
    tie-break (DECLARED_FAMILY_TIEBREAK_MARGIN) as the exhaustive path."""
    entries = corpus.entries
    b = int(np.argmax(raw))
    best = int(cand[b])
    idx = corpus.family_indices(_declared_family(row) or "")
    if idx:
        dfam = entries[idx[0]].family
        if entries[best].family != dfam:
            ks = [k for k, j in enumerate(cand) if entries[int(j)].family == dfam]
            if ks:
                kb = max(ks, key=lambda k: float(raw[k]))
                if float(raw[kb]) >= float(raw[b]) - DECLARED_FAMILY_TIEBREAK_MARGIN:
                    return int(cand[kb]), float(raw[kb])
    return best, float(raw[b])


async def _choose_strategy(corpus: "BulkCorpus"):
    """("exhaustive", None) = v0.4.6 path (small corpus, illustrative corpus, no embedding model, or a
    complete legacy cache); ("exhaustive", vc) = every clause vector comes from the on-disk per-clause
    cache; ("shortlist", vc) = large corpus whose vectors are not all cached yet."""
    from research import bulk_fast
    mode = str(getattr(settings, "BULK_SCORING_STRATEGY", "auto") or "auto").lower()
    n = len(corpus.entries)
    if corpus.source not in ("dataset", "retrieval") or not corpus.fingerprint:
        return "exhaustive", None
    if mode == "auto" and n <= int(getattr(settings, "BULK_EXHAUSTIVE_MAX_ENTRIES", 4000)):
        return "exhaustive", None
    try:
        from rag.vector_store import _embedder as local, LocalEmbedder
        await local._get_model_async()
    except Exception:  # noqa: BLE001 - no model: the TF-IDF fallback path handles it
        return "exhaustive", None
    model_name = getattr(settings, "EMBEDDING_MODEL", "model")
    if mode != "shortlist":
        try:
            from research.bulk_corpus import _cache_file
            if _cache_file(corpus, model_name).exists():
                return "exhaustive", None      # complete v0.4.6 cache: reuse it
        except Exception:  # noqa: BLE001
            pass
    vc = await asyncio.to_thread(bulk_fast.vector_cache_for, corpus, model_name, LocalEmbedder.precision,
                                 int(getattr(settings, "BULK_CORPUS_EMBED_MAX_TOKENS", 256)))
    if mode == "exhaustive" or vc.complete():
        return "exhaustive", vc
    return "shortlist", vc


def _normed(v: np.ndarray) -> np.ndarray:
    """Row-wise L2-normalised float32 copy of a matrix (zero rows stay zero)."""
    v = np.asarray(v, dtype=np.float32)
    return v / np.clip(np.linalg.norm(v, axis=1, keepdims=True), 1e-9, None)


_SCAN_CHUNK = 32768


def _scan_best(bv: np.ndarray, vc) -> tuple[np.ndarray, np.ndarray]:
    """Best clause over the WHOLE cached corpus for each row of ``bv`` (normalised), scanning the float16
    vector file in chunks - never materialising an (n_corpus x dim) float32 copy (800 MB at 200,000
    clauses) or an (n_rows x n_corpus) matrix."""
    n = vc.n
    best_v = np.full(bv.shape[0], -np.inf, dtype=np.float32)
    best_i = np.zeros(bv.shape[0], dtype=np.int64)
    ar = np.arange(bv.shape[0])
    for c0 in range(0, n, _SCAN_CHUNK):
        chunk = np.asarray(vc._mm[c0:c0 + _SCAN_CHUNK], dtype=np.float32)
        S = bv @ chunk.T
        j = S.argmax(axis=1)
        v = S[ar, j]
        upd = v > best_v
        best_v[upd] = v[upd]
        best_i[upd] = j[upd] + c0
    return best_i, best_v


def _declared_tiebreak(bv_row: np.ndarray, best: int, best_v: float, row: dict, corpus, vc) -> tuple[int, float]:
    """The exhaustive path's declared-family tie-break (see _pick_best) for one row."""
    idx = corpus.family_indices(_declared_family(row) or "")
    if idx and corpus.entries[best].family != corpus.entries[idx[0]].family:
        fv = vc.get(idx) @ bv_row
        kb = int(np.argmax(fv))
        if float(fv[kb]) >= best_v - DECLARED_FAMILY_TIEBREAK_MARGIN:
            return int(idx[kb]), float(fv[kb])
    return best, best_v


async def _cached_pass(records, corpus, embedder, vc, batch_size, top_idx, top_raw, row_failed, reservoir,
                       progress_cb, prep_progress, *, exhaustive: bool) -> int:
    """Pass 1 when clause vectors come from the on-disk per-clause cache (research/bulk_fast.py).

    exhaustive=False: per row, a shortlist = TF-IDF best clauses (whole corpus + declared specification)
    + clauses whose TITLE is semantically closest (declared specification; whole corpus once every title
    has a vector), re-scored with the embedding model; only shortlisted clauses are embedded.
    exhaustive=True: every clause has a vector; the whole corpus is scanned in chunks (same comparison as
    v0.4.6, without holding the full matrix).
    Fills top_idx/top_raw/row_failed and the calibration reservoir (raw similarities against a fixed random
    clause sample). Returns the number of failed rows."""
    from research import bulk_fast
    from rag.vector_store import LocalEmbedder
    total = len(records)
    kg = int(getattr(settings, "BULK_SHORTLIST_GLOBAL", 16))
    kd = int(getattr(settings, "BULK_SHORTLIST_DECLARED", 24))
    kt = int(getattr(settings, "BULK_SHORTLIST_TITLES", 8))
    lex = tc = None
    if not exhaustive:
        lex = await asyncio.to_thread(bulk_fast.lexical_index_for, corpus)
        if kt > 0:
            tc = await asyncio.to_thread(bulk_fast.title_cache_for, corpus, getattr(settings, "EMBEDDING_MODEL", "model"),
                                         LocalEmbedder.precision)
    sample = bulk_fast.calibration_sample(corpus, int(getattr(settings, "BULK_CALIBRATION_SAMPLE", 96)))
    await vc.ensure(sample, progress=prep_progress)
    sample_vecs = vc.get(sample)
    embedder._fallback_vectorizer = None
    embedder._fallback_svd = None
    n_failed = 0

    for start in range(0, total, batch_size):
        batch_records = records[start:start + batch_size]
        batch_texts: list[str] = []
        embed_offsets: list[int] = []
        for offset, r in enumerate(batch_records):
            try:
                batch_texts.append(_patent_text(r))
                embed_offsets.append(offset)
            except Exception as e:
                row_failed[start + offset] = True
                n_failed += 1
                logger.warning(f"Bulk scoring: row {start + offset} could not be prepared for embedding ({e}); marking FAILED.")
        if batch_texts:
            # 1. patent vectors (whole batch; one by one only if the batch call fails)
            vecs: list = [None] * len(batch_texts)
            try:
                bv = await embedder.embed_patent_batch_for_scoring(batch_texts)
                if bv.shape[0] != len(batch_texts) or (vc.dim and bv.shape[1] != vc.dim):
                    raise ValueError(f"Embedder returned {bv.shape} for {len(batch_texts)} texts.")
                vecs = list(_normed(bv))
            except Exception as e:
                logger.warning(f"Bulk scoring: batch embedding failed for rows {start}-{start + len(batch_records) - 1} "
                               f"({e}); retrying this batch's rows individually.")
                for k, text in enumerate(batch_texts):
                    try:
                        v = await embedder.embed_patent_batch_for_scoring([text])
                        if v.shape[0] != 1 or (vc.dim and v.shape[1] != vc.dim):
                            raise ValueError("embedding dimension mismatch")
                        vecs[k] = _normed(v)[0]
                    except Exception as row_e:
                        i = start + embed_offsets[k]
                        row_failed[i] = True
                        n_failed += 1
                        logger.warning(f"Bulk scoring: row {i} could not be embedded/scored ({row_e}); marking FAILED.")
            ks = [k for k in range(len(batch_texts)) if vecs[k] is not None]
            if ks:
                V = np.stack([vecs[k] for k in ks]).astype(np.float32)
                cands = None
                if not exhaustive:
                    # 2. shortlist per row
                    fams = [_declared_family(batch_records[embed_offsets[k]]) for k in ks]
                    cands = await asyncio.to_thread(lex.shortlist, [batch_texts[k] for k in ks], fams, kg, kd)
                    if tc is not None:
                        pools = [np.asarray(corpus.family_indices(f or ""), dtype=np.int64) for f in fams]
                        need = [tc.entry_title[p] for p in pools if p.size]
                        if need:
                            await tc.ensure(np.concatenate(need), progress=prep_progress)
                        whole = tc.complete()

                        def _titles(V=V, pools=pools, whole=whole):
                            out = []
                            for n_k, pool in enumerate(pools):
                                parts = []
                                if pool.size:
                                    parts.append(bulk_fast.title_candidates(tc, V[n_k], pool, kt))
                                if whole:
                                    parts.append(bulk_fast.title_candidates(tc, V[n_k], None, kt))
                                out.append(np.concatenate(parts) if parts else np.zeros(0, dtype=np.int64))
                            return out
                        extra = await asyncio.to_thread(_titles)
                        cands = [np.unique(np.concatenate([c, e])) for c, e in zip(cands, extra)]
                    # 3. vectors for the shortlisted clauses (cached ones are free)
                    await vc.ensure(np.concatenate(cands), progress=prep_progress)

                # 4. best clause per row (+ calibration sample)
                def _match(V=V, ks=ks, cands=cands, start=start, batch_records=batch_records, embed_offsets=embed_offsets):
                    if exhaustive:
                        bi_all, bv_all = _scan_best(V, vc)
                    for n_k, k in enumerate(ks):
                        offset = embed_offsets[k]
                        row = batch_records[offset]
                        if exhaustive:
                            bi, br = _declared_tiebreak(V[n_k], int(bi_all[n_k]), float(bv_all[n_k]), row, corpus, vc)
                        else:
                            c = cands[n_k]
                            raw = vc.get(c) @ V[n_k]
                            bi, br = _pick_best_subset(raw, c, row, corpus)
                        top_idx[start + offset] = bi
                        top_raw[start + offset] = br
                    reservoir.add_many(V @ sample_vecs.T)
                await asyncio.to_thread(_match)
        await _emit(progress_cb, {"phase": "embedding", "processed": min(start + batch_size, total),
                                  "failed": n_failed, "total": total})
    return n_failed


async def score_records_batched(
    records: list[dict],
    *,
    batch_size: Optional[int] = None,
    progress_cb: ProgressCallback = None,
) -> AsyncIterator[tuple[list[dict], dict]]:
    """
    Async generator over `records`: embeds, scores and yields results in
    bounded chunks instead of processing the whole dataset in one pass.

    Yields `(scored_batch, run_meta)` once per chunk. `run_meta` reflects
    the run's cumulative state as of that yield; the FINAL yielded
    `run_meta` is the authoritative summary for the run.

    `progress_cb`, if given, is called after each embedding batch AND
    after each scoring batch with a dict:
        {"phase": "embedding"|"scoring", "processed": int, "failed": int,
         "total": int}
    May be a plain function or an async function (awaited if so).
    """
    total = len(records)
    # v0.4.6: never on the event loop. With the retrieval index installed the first call builds the clause
    # corpus from a multi-GB SQLite file (minutes); run inline it froze every other request, so the UI
    # reported "Backend offline" while a Bulk upload was being scored.
    corpus = await asyncio.to_thread(get_bulk_corpus)
    if not records:
        yield [], {"embedder_mode": "n/a", "corpus_size": len(corpus.entries), "total": 0}
        return

    batch_size = batch_size or settings.SCORING_BATCH_SIZE
    embedder = get_research_embedder()

    # Only ever holds: one batch's (batch_size, n_corpus) raw matrix (released at the end of each loop
    # iteration), plus two O(n_patents) arrays (top_idx/top_raw) and a size-bounded reservoir sample for
    # calibration -- never the full (n_patents, n_corpus) matrix.
    top_idx = np.full(total, -1, dtype=np.int32)
    top_raw = np.zeros(total, dtype=np.float32)
    row_failed = [False] * total
    reservoir = _ReservoirSampler(max_size=200_000)
    n_embed_failed = 0

    # v0.4.7: how to compare rows with the corpus (see research/bulk_fast.py)
    strategy, vc = await _choose_strategy(corpus)

    async def _prep_progress(done: int, todo: int) -> None:
        """Progress callback while clause vectors are being prepared ('Preparing reference clauses x /
        y' in the Runs page).
        """
        await _emit(progress_cb, {"phase": "preparing", "processed": done, "total_clauses": todo,
                                  "failed": n_embed_failed, "total": total})

    if vc is not None:
        if strategy == "exhaustive" and not vc.complete():
            await vc.ensure(np.arange(len(corpus.entries)), progress=_prep_progress)
        n_embed_failed = await _cached_pass(records, corpus, embedder, vc, batch_size, top_idx, top_raw,
                                            row_failed, reservoir, progress_cb, _prep_progress,
                                            exhaustive=(strategy == "exhaustive"))
    else:
        # ── Corpus embedded ONCE for the whole run, never per batch ─────────
        # (a dataset corpus is large, so its vectors are cached on disk per dataset fingerprint + model)
        corpus_vecs = await embed_corpus_cached(embedder, corpus)

        # ── Pass 1: embed + top-1 match every patent in bounded batches ─────
        for start in range(0, total, batch_size):
            batch_records = records[start:start + batch_size]

            # Build this batch's texts first, isolating any row whose text
            # construction itself fails (e.g. malformed/non-string field data)
            # BEFORE it ever reaches the embedder -- a bad row must not stop
            # `_patent_text()` from running for its batch-mates either.
            batch_texts: list[str] = []
            embed_offsets: list[int] = []  # positions within batch_texts that map back to batch_records
            for offset, r in enumerate(batch_records):
                i = start + offset
                try:
                    batch_texts.append(_patent_text(r))
                    embed_offsets.append(offset)
                except Exception as e:
                    row_failed[i] = True
                    n_embed_failed += 1
                    logger.warning(f"Bulk scoring: row {i} could not be prepared for embedding ({e}); marking FAILED.")

            if not batch_texts:
                await _emit(progress_cb, {
                    "phase": "embedding", "processed": min(start + batch_size, total),
                    "failed": n_embed_failed, "total": total,
                })
                continue

            try:
                batch_vecs = await embedder.embed_patent_batch_for_scoring(batch_texts)
                if batch_vecs.shape[0] != len(batch_texts):
                    raise ValueError(
                        f"Embedder returned {batch_vecs.shape[0]} vectors for "
                        f"{len(batch_texts)} texts."
                    )
            except Exception as e:
                # Whole-batch embedding call failed -- retry each of this
                # batch's (already-text-validated) records individually so ONE
                # malformed row can't fail its batch-mates too. This is the
                # isolation the acceptance criteria require ("if patent #8432
                # fails, the rest continue").
                logger.warning(
                    f"Bulk scoring: batch embedding failed for rows "
                    f"{start}-{start + len(batch_records) - 1} ({e}); retrying "
                    f"this batch's rows individually."
                )
                for offset, text in zip(embed_offsets, batch_texts):
                    i = start + offset
                    try:
                        single_vec = await embedder.embed_patent_batch_for_scoring([text])
                        raw_row = cosine_sim_matrix(single_vec, corpus_vecs)[0]
                        bi = _pick_best(raw_row, batch_records[offset], corpus)
                        top_idx[i] = bi
                        top_raw[i] = float(raw_row[bi])
                        reservoir.add_many(raw_row)
                    except Exception as row_e:
                        row_failed[i] = True
                        n_embed_failed += 1
                        logger.warning(f"Bulk scoring: row {i} could not be embedded/scored ({row_e}); marking FAILED.")
                await _emit(progress_cb, {
                    "phase": "embedding", "processed": min(start + batch_size, total),
                    "failed": n_embed_failed, "total": total,
                })
                continue

            def _match_batch(batch_vecs=batch_vecs, start=start, batch_records=batch_records, embed_offsets=embed_offsets):
                raw_batch = cosine_sim_matrix(batch_vecs, corpus_vecs)  # (len(batch_texts), n_corpus) -- small, released below
                for k, offset in enumerate(embed_offsets):
                    i = start + offset
                    bk = _pick_best(raw_batch[k], batch_records[offset], corpus)
                    top_idx[i] = bk
                    top_raw[i] = raw_batch[k, bk]
                reservoir.add_many(raw_batch)

            # v0.4.6: a retrieval-index corpus has tens of thousands of entries, so this matrix work is CPU-heavy;
            # run it in a worker thread so the API keeps answering while a large run is scored.
            await asyncio.to_thread(_match_batch)
            del batch_vecs  # release this batch's vectors before the next one is built

            await _emit(progress_cb, {
                "phase": "embedding", "processed": min(start + batch_size, total),
                "failed": n_embed_failed, "total": total,
            })

    # ── Calibration: percentiles over a bounded sample of the run's raw
    #    similarity distribution, matching calibrate_similarity_matrix's
    #    5th/95th-percentile behavior without retaining the full matrix. ──
    # v0.4.1 FIX (semantic similarity always 100%): the old ceiling was the
    # 95th percentile of ALL patent x corpus pairs. Every value calibrated
    # here is a patent's BEST match out of ~54 corpus entries (its top ~2%),
    # so it sat above that ceiling essentially always and clipped to 1.0 --
    # every row reported 100%. Same floor as before (run's 5th percentile =
    # the unrelated-text baseline), but the ceiling is now a perfect cosine
    # match (1.0). Mirrors research.embeddings.calibrate_match_scores.
    lo, _hi95 = reservoir.percentiles(5.0, 95.0)
    lo = min(lo, 0.999)
    calibrated_top = np.clip((top_raw.astype("float64") - lo) / (1.0 - lo), 0.0, 1.0).astype("float32")
    calib_meta = {"low": round(lo, 4), "high": 1.0, "degenerate": False, "method": "floor-to-unity"}

    # ── Pass 2: build + yield the final scored rows, batch by batch ─────
    # No embedding/matrix work here -- just cheap per-row arithmetic and
    # corpus-entry lookups, so this pass is fast even for 17,000+ rows.
    n_scored = 0
    n_failed = n_embed_failed
    for start in range(0, total, batch_size):
        batch_records = records[start:start + batch_size]
        out_batch = []
        for offset, row in enumerate(batch_records):
            i = start + offset
            if row_failed[i] or top_idx[i] < 0:
                out_batch.append(_failed_row(row, "Embedding/similarity computation failed for this row."))
                continue
            entry = corpus.entries[int(top_idx[i])]
            sim_score = float(calibrated_top[i])
            explicit = _truthy(row.get("explicit_disclosure"))
            out_batch.append(_finalize_row(row, entry, sim_score, explicit, corpus))
            n_scored += 1

        run_meta = {
            "embedder_mode": embedder.mode,
            "embedding_dim": embedder.last_dim,
            "corpus_size": len(corpus.entries),
            "corpus_source": corpus.source,
            "corpus_fingerprint": corpus.fingerprint,
            "corpus_info": corpus.info,
            "calibration": calib_meta,
            "corpus_disclaimer": corpus.disclaimer,
            "total": total,
            "scored": n_scored,
            "failed": n_failed,
            "scoring_strategy": strategy,
            "corpus_vectors_cached": (vc.n_done if vc is not None else None),
        }
        await _emit(progress_cb, {"phase": "scoring", "processed": start + len(batch_records), "failed": n_failed, "total": total})
        if vc is not None and start + batch_size >= total:
            from research import bulk_fast
            bulk_fast.schedule_background_fill(*bulk_fast.caches_for(corpus, vc))
        yield out_batch, run_meta


async def score_records(records: list[dict]) -> tuple[list[dict], dict]:
    """
    Convenience wrapper over `score_records_batched()` for callers that
    want the full scored list back at once (small datasets, tests,
    interactive/CLI use). Internally still processes in bounded batches —
    this does NOT reintroduce a single (n_patents x n_corpus) matrix or a
    single combined embedding call; it just drains the batched generator
    before returning. For large datasets (thousands of rows), callers
    should use `score_records_batched()` directly and persist per batch —
    see api/routes/gitas/dashboard.py.
    """
    if not records:
        return [], {"embedder_mode": "n/a", "corpus_size": len(get_bulk_corpus().entries)}

    out: list[dict] = []
    run_meta: dict = {}
    async for batch, meta in score_records_batched(records):
        out.extend(batch)
        run_meta = meta
    return out, run_meta
