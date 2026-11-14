"""
CPARAS ISLD <-> standards retrieval bridge (v0.3-continuation, cPARAS audit
items 8-11; wording updated v0.5.0).

Naming note (v0.5.0): earlier versions called the retrieval layer the "ETSI
Knowledge Base". What it actually searches is the installed **3GPP Retrieval
Index** (services/sqlite_retrieval.py) when one is present, otherwise the
**Standards Corpus** vector store (rag/vector_store.py) — see
``EtsiKbEvidence.source_label``. The module, class and stored identifiers
(``etsi_kb_bridge``, ``EtsiKbEvidence``, evidence_source
``"etsi_knowledge_base"``) keep their historical names so stored analyses and
exports stay compatible.

research/engine.py's essentiality scoring previously compared patent text
ONLY against the small illustrative static corpus in
research/standards_corpus.py. This module adds a REAL evidence lookup
against the shared standards retrieval layer (services/retrieval_service.py --
the SAME retrieval layer C-SARAS uses, source_type="etsi_standard"/
"3gpp_spec_text") and is used by analyze_patents() as the PRIMARY source
when real evidence is available, falling back to the illustrative corpus
only when the retrieval layer has nothing indexed yet (or returns no match for a
given patent) -- graceful degradation, never a hard dependency, and never
silently treating the illustrative corpus as authoritative once real data
exists (per the audit's explicit requirement).

Deliberately kept as its own small module rather than folded into
research/engine.py directly: engine.py's existing per-patent loop is
synchronous/vectorized (numpy) and does not want to become async top-to-
bottom just to support this; this module does the async retrieval lookups as one
bounded batch up front, and engine.py consumes plain dicts.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

# Small bound on concurrent retrieval lookups per declaration batch -- a
# declaration is typically tens of disclosures, not thousands, but this
# keeps the same "never unbounded concurrency" discipline as the ingestion
# pipeline (etsi_corpus/pipeline.py) rather than assuming batch size stays
# small forever.
_MAX_CONCURRENT_LOOKUPS = 8


@dataclass
class EtsiKbEvidence:
    """One patent's real retrieved-evidence match (3GPP Retrieval Index or
    Standards Corpus), or None if nothing was found (nothing installed/indexed,
    or genuinely no relevant standard was retrieved above the minimum score)."""
    standard: str
    specification_number: str
    version: str
    release: str
    domain: str
    clause: str
    clause_title: str
    page: Optional[int]
    similarity: float          # semantic_score -- see services/retrieval_service.py's
                                # conservative-scoring rationale; never the lexically-
                                # boosted combined_score, for the same reason C-SARAS's
                                # relationship tiering avoids it (audit item 6).
    evidence_text: str
    etsi_document_id: Optional[int]
    source_url: Optional[str]
    # v0.4.5 - where the evidence text came from when the retrieval index is the dataset
    content_type: Optional[str] = None          # full_text = original specification text; others = model-generated summaries
    dataset_document_id: Optional[str] = None
    # v0.4.9 - the whole retrieved excerpt (evidence_text above is cut at 600 characters for display);
    # used to find the exact phrases the patent shares with the clause (research/phrase_match.py).
    full_text: Optional[str] = None
    # v0.5.0 - which store produced the hit ("3gpp_spec" = 3GPP Retrieval Index; others = Standards Corpus)
    source_type: Optional[str] = None

    @property
    def source_label(self) -> str:
        """User-facing name of where this evidence came from."""
        from_index = self.source_type == "3gpp_spec" or bool(self.dataset_document_id)
        return "3GPP Retrieval Index" if from_index else "Standards Corpus"


async def _lookup_one(patent_text: str, min_score: float) -> Optional[EtsiKbEvidence]:
    """Retrieve evidence for one patent (fallback path when the batched lookup fails); None on any
    retrieval error, so that patent uses the illustrative corpus.
    """
    if not patent_text.strip():
        return None
    try:
        from services.retrieval_service import retrieve_for_standards_classification
        hits = await retrieve_for_standards_classification(patent_text, top_k=3)
    except Exception as e:
        logger.warning(f"Standards retrieval failed for a patent (falling back to the illustrative corpus): {e}")
        return None
    return _evidence_from_hits(hits, min_score)


def _evidence_from_hits(hits, min_score: float) -> Optional[EtsiKbEvidence]:
    """Best semantic hit above `min_score` as EtsiKbEvidence (lexical-only hits are ignored); None when
    nothing qualifies.
    """
    if not hits:
        return None
    # v0.4.5: a lexical-only hit (retrieval index without a usable embedder) is context, not semantic evidence.
    hits = [h for h in hits if getattr(h, "is_semantic", True)]
    if not hits:
        return None
    best = hits[0]
    if best.semantic_score < min_score:
        return None
    return EtsiKbEvidence(
        standard=best.standard or "Unknown",
        specification_number=best.specification_number or "Unknown",
        version=best.version or "Unknown",
        release=best.release or "Unknown",
        domain=best.domain or "Unknown",
        clause=best.clause or "Unknown",
        clause_title=best.clause_title or "Unknown",
        page=best.page,
        similarity=round(best.semantic_score, 4),
        evidence_text=best.content[:600] if best.content else "No evidence found",
        etsi_document_id=best.document_id,
        source_url=best.source_url,
        content_type=getattr(best, "content_type", None),
        dataset_document_id=getattr(best, "dataset_document_id", None),
        full_text=best.content or None,
        source_type=getattr(best, "source_type", None),
    )


async def lookup_etsi_kb_evidence(patent_texts: list[str], min_score: float = 0.35) -> list[Optional[EtsiKbEvidence]]:
    """Bounded-concurrency batch lookup: one real retrieval match attempt per
    patent text, in the same order as `patent_texts`. Returns None at a
    given index if retrieval found nothing relevant (or isn't installed) for
    that patent -- callers fall back to the illustrative corpus for that
    one patent only, not the whole batch."""
    if not patent_texts:
        return []
    # v0.4.7 speed: one batched look-up for every patent (one embedding call for all candidates) instead
    # of one look-up per patent. Falls back to the per-patent path if anything about it fails.
    try:
        from services.retrieval_service import retrieve_many, STANDARDS_SOURCE_TYPES
        live = [i for i, t in enumerate(patent_texts) if (t or "").strip()]
        many = await retrieve_many([patent_texts[i] for i in live], top_k=3, source_types=STANDARDS_SOURCE_TYPES)
        out: list[Optional[EtsiKbEvidence]] = [None] * len(patent_texts)
        for i, hits in zip(live, many):
            out[i] = _evidence_from_hits(hits, min_score)
        return out
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Batched standards retrieval failed ({e}); looking up patents one by one.")
    semaphore = asyncio.Semaphore(_MAX_CONCURRENT_LOOKUPS)

    async def _bounded(text: str) -> Optional[EtsiKbEvidence]:
        """Per-patent lookup limited by the concurrency semaphore."""
        async with semaphore:
            return await _lookup_one(text, min_score)

    return await asyncio.gather(*[_bounded(t) for t in patent_texts])
