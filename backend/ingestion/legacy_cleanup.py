"""
Start-up clean-up of retired vector-store content (v0.5.0)
==========================================================

Up to v0.4.9 a switch on the Standards Corpus page and in CPARAS ISLD could copy
a small built-in demo set of clauses into the shared vector store (chunk ids
starting with ``LEGACY_DEMO_ID_PREFIX``). That switch was removed in v0.5.0.
So that an installation where it had been turned on does not keep returning
those demo chunks in Text Matching and RAG retrieval, ``remove_legacy_demo_chunks``
runs once per start-up (main.py) and deletes exactly those chunks - real
imported or crawled content is never touched.

Cheap when there is nothing to do: it first checks the ``knowledge_chunks``
bookkeeping table and only calls the vector store when such chunks exist.
Failures are logged and never stop the app.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

LEGACY_DEMO_ID_PREFIX = "illustrative_"


async def remove_legacy_demo_chunks() -> int:
    """Delete the retired demo chunks from the vector store and the bookkeeping
    table. Returns how many bookkeeping rows were removed (0 = nothing to do)."""
    try:
        from sqlalchemy import delete, func, select
        from core.database import AsyncSessionLocal, KnowledgeChunk
        async with AsyncSessionLocal() as session:
            n = (await session.execute(select(func.count()).select_from(KnowledgeChunk).where(
                KnowledgeChunk.source_id.like(f"{LEGACY_DEMO_ID_PREFIX}%")))).scalar_one() or 0
            if not n:
                return 0
            from rag.vector_store import get_vector_store
            removed = await get_vector_store().delete_by_id_prefix(LEGACY_DEMO_ID_PREFIX)
            await session.execute(delete(KnowledgeChunk).where(
                KnowledgeChunk.source_id.like(f"{LEGACY_DEMO_ID_PREFIX}%")))
            await session.commit()
        logger.info(f"Removed {removed} retired demo chunk(s) from the Standards Corpus vector store.")
        return int(n)
    except Exception as e:  # noqa: BLE001 - housekeeping must never block start-up
        logger.warning(f"Could not remove retired demo chunks from the vector store: {e}")
        return 0
