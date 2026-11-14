"""
Hybrid retrieval service (item 18): metadata filtering + lexical/keyword
overlap + embedding similarity over the shared vector store, returning
actual evidence chunks with full source metadata (document_id, standard,
specification, version, release, domain, topic, clause, page, source_url).

This is the ONE retrieval implementation CPARAS Bulk and CPARAS ISLD call
(item 17/18) -- no separate per-module retrieval engine.

Sources (v0.5.0 naming): the **3GPP Retrieval Index** (tgpp-sqlite-retrieval
output, services/sqlite_retrieval.py) and the **Standards Corpus** vector store
(rag/vector_store.py, filled by dataset imports and 3GPP crawls).
RETRIEVAL_SOURCE=auto uses the index whenever one is installed and the
vector store otherwise; "both" merges them.

Integration point: a larger platform with its own standards search can replace
`retrieve` / `retrieve_many` behind the same EvidenceChunk contract; ISLD
(research/etsi_kb_bridge.py), Text Matching and Bulk only consume that shape.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

import logging

from rag.vector_store import get_vector_store
import asyncio

from services import sqlite_retrieval as _sqlite

logger = logging.getLogger(__name__)

STANDARDS_SOURCE_TYPES = ("etsi_standard", "3gpp_spec")

_STOPWORDS = {
    "the", "a", "an", "of", "and", "or", "to", "for", "in", "on", "is",
    "are", "with", "by", "at", "as", "be", "this", "that", "such", "said",
}


def _keywords(text: str) -> set[str]:
    """Lower-cased content words (3+ characters, stopwords removed) used for the lexical-overlap
    signal.
    """
    tokens = re.findall(r"[a-zA-Z][a-zA-Z0-9\-]{2,}", text.lower())
    return {t for t in tokens if t not in _STOPWORDS}


def _lexical_overlap(query_kw: set[str], content: str) -> float:
    """Share of the query's keywords that also occur in `content` (0-1)."""
    if not query_kw:
        return 0.0
    content_kw = _keywords(content)
    if not content_kw:
        return 0.0
    overlap = len(query_kw & content_kw)
    return overlap / max(len(query_kw), 1)


@dataclass
class EvidenceChunk:
    """One piece of retrieved standards evidence with its scores and full provenance (document, spec,
    version, release, clause, page, source). `combined_score` blends semantic and lexical scores for
    ranking; callers that grade essentiality use `semantic_score` only.
    """
    content: str
    semantic_score: float
    lexical_score: float
    combined_score: float
    document_id: Optional[int] = None
    standard: Optional[str] = None
    specification_number: Optional[str] = None
    version: Optional[str] = None
    release: Optional[str] = None
    domain: Optional[str] = None
    topic: Optional[str] = None
    clause: Optional[str] = None
    clause_title: Optional[str] = None
    page: Optional[int] = None
    source_url: Optional[str] = None
    source_type: Optional[str] = None
    # v0.4.5 - provenance of retrieval-index hits (all None / "semantic" for vector-store hits)
    score_kind: str = "semantic"           # "semantic" | "semantic_rerank" | "lexical" (lexical is never semantic evidence)
    dataset_document_id: Optional[str] = None
    content_type: Optional[str] = None     # full_text = original text; clause_summary etc. = model-generated summaries
    origin: Optional[str] = None
    source_path: Optional[str] = None      # relative provenance label inside the index, never an absolute path

    @property
    def is_semantic(self) -> bool:
        """True when the score came from the embedding model (lexical-only hits are context, never
        evidence).
        """
        return self.score_kind.startswith("semantic")


def _to_evidence(hit: dict) -> EvidenceChunk:
    """Convert a vector-store / retrieval-index hit dict into an EvidenceChunk."""
    meta = hit.get("metadata") or {}
    return EvidenceChunk(
        content=hit.get("content", ""),
        semantic_score=hit.get("score", 0.0),
        lexical_score=0.0,
        combined_score=hit.get("score", 0.0),
        document_id=meta.get("etsi_document_id"),
        standard=meta.get("standard_name") or meta.get("spec"),
        specification_number=meta.get("specification_number"),
        version=meta.get("version"),
        release=meta.get("release"),
        domain=meta.get("technology_domain") or meta.get("domain"),
        topic=meta.get("topic"),
        clause=meta.get("clause"),
        clause_title=meta.get("clause_title"),
        page=meta.get("page"),
        source_url=meta.get("pdf_url") or meta.get("source_url"),
        source_type=meta.get("source_type"),
        score_kind=meta.get("score_kind") or "semantic",
        dataset_document_id=meta.get("dataset_document_id"),
        content_type=meta.get("content_type"),
        origin=meta.get("origin"),
        source_path=meta.get("source_path"),
    )


def _retrieval_mode() -> str:
    """RETRIEVAL_SOURCE setting: 'auto' (default), 'sqlite', 'vector' or 'both'."""
    from core.config import settings
    return (getattr(settings, "RETRIEVAL_SOURCE", "auto") or "auto").lower()


def _use_sqlite(mode: str) -> bool:
    """Whether to query the 3GPP Retrieval Index: always for 'sqlite'/'both', and for 'auto' whenever a
    usable index is installed.
    """
    if mode in ("sqlite", "both"):
        return True
    return mode == "auto" and _sqlite.available()


def _use_vector(mode: str, sqlite_on: bool) -> bool:
    """Whether to query the Standards Corpus vector store: always for 'vector'/'both', and for 'auto'
    only when no retrieval index is in use.
    """
    if mode in ("vector", "both"):
        return True
    return mode == "auto" and not sqlite_on


async def retrieve(
    query: str,
    top_k: int = 8,
    min_score: float = 0.35,
    source_types: tuple[str, ...] = STANDARDS_SOURCE_TYPES,
    lexical_boost: float = 0.10,
) -> list[EvidenceChunk]:
    """Hybrid retrieval: semantic similarity search per source_type (the
    underlying vector store only supports one source_type filter per call,
    so this fans out across `source_types` and merges), re-scored with a
    lexical-overlap boost, then re-ranked by combined_score.

    v0.3-continuation conservative-scoring fix (C-SARAS audit item 6):
    `lexical_boost` default lowered from 0.15 to 0.10, and -- more
    importantly -- `combined_score` (which includes this boost) is used
    ONLY for ranking/relevance ordering. Callers that need to classify a
    match's RELATIONSHIP TIER (explicit/strong/partial/weak/no_match, see
    etsi_corpus/models.py's relationship_from_score) must use
    `semantic_score` alone for that decision, never `combined_score`.
    Lexical overlap is a legitimate ranking tiebreaker (two chunks with
    similar embedding similarity, but one shares the query's actual
    terminology, is a reasonable one to prefer) but must never by itself
    be the deciding factor in a label that implies technical
    correspondence.

    Returns actual evidence chunks (never a bare score with no supporting
    text) -- item 18/19: retrieval must return traceable evidence.
    """
    query_kw = _keywords(query)
    mode = _retrieval_mode()
    # v0.4.6: the first availability check opens the (multi-GB) index - keep it off the event loop.
    sqlite_on = await asyncio.to_thread(_use_sqlite, mode)
    vector_on = _use_vector(mode, sqlite_on)

    all_hits: list[EvidenceChunk] = []
    if vector_on:
        vs = get_vector_store()
        for source_type in source_types:
            try:
                hits = await vs.similarity_search(query, top_k=top_k, filter_source=source_type, min_score=min_score)
            except Exception:
                continue
            for h in hits:
                ev = _to_evidence(h)
                ev.lexical_score = _lexical_overlap(query_kw, ev.content)
                ev.combined_score = min(1.0, ev.semantic_score + lexical_boost * ev.lexical_score)
                all_hits.append(ev)
    # v0.4.5: the SQLite/FTS5 retrieval index serves the standards source types (it holds no user documents).
    if sqlite_on and _sqlite.SOURCE_TYPE in source_types:
        try:
            from core.config import settings
            hits = await _sqlite.similarity_search(
                query, top_k=top_k, min_score=min_score,
                pool=int(getattr(settings, "RETRIEVAL_SQLITE_POOL", 40)),
                rerank=bool(getattr(settings, "RETRIEVAL_SQLITE_RERANK", True)))
        except Exception as e:  # noqa: BLE001
            logger.error(f"Retrieval index search failed ({type(e).__name__}: {e})")
            hits = []
        for h in hits:
            ev = _to_evidence(h)
            ev.lexical_score = _lexical_overlap(query_kw, ev.content)
            ev.combined_score = min(1.0, ev.semantic_score + lexical_boost * ev.lexical_score)
            all_hits.append(ev)

    all_hits.sort(key=lambda e: e.combined_score, reverse=True)
    return all_hits[:top_k]


async def retrieve_many(
    queries: list[str],
    top_k: int = 8,
    min_score: float = 0.35,
    source_types: tuple[str, ...] = STANDARDS_SOURCE_TYPES,
    lexical_boost: float = 0.10,
) -> list[list[EvidenceChunk]]:
    """retrieve() for many queries (v0.4.7 speed). With only the retrieval index in use (the usual set-up),
    every query's candidates are re-scored in ONE embedding call; otherwise each query goes through
    retrieve() concurrently. Same ranking rules as retrieve()."""
    if not queries:
        return []
    mode = _retrieval_mode()
    sqlite_on = await asyncio.to_thread(_use_sqlite, mode)
    vector_on = _use_vector(mode, sqlite_on)
    if vector_on or not sqlite_on or _sqlite.SOURCE_TYPE not in source_types:
        return list(await asyncio.gather(*[retrieve(q, top_k=top_k, min_score=min_score, source_types=source_types,
                                                    lexical_boost=lexical_boost) for q in queries]))
    from core.config import settings
    try:
        many = await _sqlite.similarity_search_many(
            queries, top_k=top_k, min_score=min_score,
            pool=int(getattr(settings, "RETRIEVAL_SQLITE_POOL", 24)),
            rerank=bool(getattr(settings, "RETRIEVAL_SQLITE_RERANK", True)))
    except Exception as e:  # noqa: BLE001
        logger.error(f"Retrieval index search failed ({type(e).__name__}: {e})")
        many = [[] for _ in queries]
    out = []
    for q, hits in zip(queries, many):
        kw = _keywords(q)
        evs = []
        for h in hits:
            ev = _to_evidence(h)
            ev.lexical_score = _lexical_overlap(kw, ev.content)
            ev.combined_score = min(1.0, ev.semantic_score + lexical_boost * ev.lexical_score)
            evs.append(ev)
        evs.sort(key=lambda e: e.combined_score, reverse=True)
        out.append(evs[:top_k])
    return out


async def retrieve_for_standards_classification(query: str, top_k: int = 10) -> list[EvidenceChunk]:
    """Convenience wrapper used by C-SARAS's patent classification (item
    15) and cPARAS's standards matching -- ETSI corpus first, falling back
    to the legacy 3GPP crawler corpus only where an ETSI record isn't
    available yet, so the transition off the crawler (item 4) is gradual
    rather than a hard cutover that breaks cPARAS the moment it ships."""
    return await retrieve(query, top_k=top_k, source_types=STANDARDS_SOURCE_TYPES)
