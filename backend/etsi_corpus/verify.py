"""
Post-import verification: compare what the database SAYS is indexed with what the vector store
actually holds.  Read-only unless ``fix=True``, which only flips mismatched documents back to
``index_failed`` so the next import re-indexes exactly those (idempotently).

Run from backend\\:   python -m etsi_corpus.cli verify-index [--limit N] [--fix] [--query "text"]

What it can and cannot tell you
-------------------------------
* It checks every `completed` document: chunk count in the store == chunks_indexed in the database.
* It counts documents that are NOT completed (pending / failed / in flight) - those are unindexed.
* It does NOT enumerate vector-store entries that have no database row (orphans).
* The optional --query runs one real similarity search; it shows the search path works, it is not a
  relevance benchmark.
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import select, func, update

from core.config import settings
from core.database import AsyncSessionLocal, EtsiDocument
from core.timeutil import utcnow as _utcnow

_PAGE = 500
_SAMPLE_CAP = 200


async def verify_index(*, limit: Optional[int] = None, fix: bool = False, query: Optional[str] = None) -> dict:
    """Check imported documents against the vector store: each document marked searchable must have its
    chunks stored. Reports documents with missing/partial vectors; with `fix=True` resets them so
    the next import run re-indexes them; `query` additionally runs a test search.
    """
    from rag.vector_store import get_vector_store
    vs = get_vector_store()
    await vs.ensure_ready()

    async with AsyncSessionLocal() as session:
        rows = (await session.execute(select(EtsiDocument.status, func.count()).group_by(EtsiDocument.status))).all()
        by_status = {st: int(n) for st, n in rows}
        db_chunks = int((await session.execute(select(func.coalesce(func.sum(EtsiDocument.chunks_indexed), 0))
                         .where(EtsiDocument.status == "completed"))).scalar_one() or 0)

    try:
        store_stats = await vs.get_stats()
    except Exception as e:  # noqa: BLE001
        store_stats = {"error": f"{type(e).__name__}: {e}"}

    checked = 0
    mismatches: list[dict] = []
    n_mismatch = 0
    last_id = 0
    done = False
    while not done:
        async with AsyncSessionLocal() as session:
            page = (await session.execute(
                select(EtsiDocument.id, EtsiDocument.standard_name_raw, EtsiDocument.chunks_indexed)
                .where(EtsiDocument.status == "completed", EtsiDocument.id > last_id)
                .order_by(EtsiDocument.id).limit(_PAGE))).all()
        if not page:
            break
        bad_ids: list[int] = []
        for doc_id, name, expected in page:
            last_id = doc_id
            if limit is not None and checked >= limit:
                done = True
                break
            checked += 1
            ids = await vs.list_chunk_ids_for_document(doc_id)
            if len(ids) != int(expected or 0):
                n_mismatch += 1
                bad_ids.append(doc_id)
                if len(mismatches) < _SAMPLE_CAP:
                    mismatches.append({"etsi_document_id": doc_id, "standard_name": name,
                                       "chunks_in_database": int(expected or 0), "chunks_in_store": len(ids)})
        if fix and bad_ids:
            async with AsyncSessionLocal() as session:
                await session.execute(update(EtsiDocument).where(EtsiDocument.id.in_(bad_ids)).values(
                    status="index_failed", last_error_type="index_mismatch", last_error_at=_utcnow(),
                    error_message="[index_mismatch] chunk count in the vector store differs from the database; re-index on the next import"))
                await session.commit()

    not_completed = {k: v for k, v in by_status.items() if k != "completed"}
    unindexed = sum(not_completed.values())

    search = None
    if query:
        try:
            hits = await vs.similarity_search(query, top_k=3, filter_source="etsi_standard", min_score=-1.0)
            search = [{"score": round(float(h["score"]), 4), "etsi_document_id": h["metadata"].get("etsi_document_id"),
                       "dataset_document_id": h["metadata"].get("dataset_document_id"),
                       "specification": h["metadata"].get("specification_number"), "version": h["metadata"].get("version"),
                       "clause": h["metadata"].get("clause"), "chunk_chars": len(h["content"])} for h in hits]
        except Exception as e:  # noqa: BLE001
            search = {"error": f"{type(e).__name__}: {e}"}

    ok = n_mismatch == 0 and unindexed == 0
    return {
        "ok": ok,
        "documents_by_status": by_status,
        "documents_not_indexed": unindexed,
        "completed_documents_checked": checked,
        "completed_documents_total": by_status.get("completed", 0),
        "checked_all_completed": limit is None or checked >= by_status.get("completed", 0),
        "chunk_count_mismatches": n_mismatch,
        "mismatch_sample": mismatches,
        "chunks_in_database_for_completed": db_chunks,
        "vector_store": store_stats,
        "fixed_mismatches": bool(fix and n_mismatch),
        "search_check": search,
        "limits": ("orphan vector-store entries without a database row are not enumerated; "
                   "the search check only proves the search path works"),
        "backend": settings.VECTOR_BACKEND,
    }
