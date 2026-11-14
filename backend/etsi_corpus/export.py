"""
Builds ETSI_Series_Enriched_v0.3.xlsx / .jsonl from the etsi_documents
table. Kept separate from enrichment.py (which is pure formatting logic,
DB-agnostic) so enrichment.py stays independently unit-testable against
plain dicts.
"""

from __future__ import annotations

from sqlalchemy import select

from core.database import AsyncSessionLocal, EtsiDocument
from etsi_corpus import enrichment

_COLUMNS = [c.name for c in EtsiDocument.__table__.columns]


def _document_to_record(doc: EtsiDocument) -> dict:
    """One EtsiDocument as a dict of the exported columns."""
    return {col: getattr(doc, col) for col in _COLUMNS}


async def fetch_records(status: str | None = None) -> list[dict]:
    """Every imported document (optionally of one status) as export records."""
    async with AsyncSessionLocal() as session:
        stmt = select(EtsiDocument)
        if status:
            stmt = stmt.where(EtsiDocument.status == status)
        result = await session.execute(stmt)
        return [_document_to_record(d) for d in result.scalars().all()]


async def build_enriched_export(status: str | None = None) -> tuple[bytes, bytes, int]:
    """Returns (xlsx_bytes, jsonl_bytes, row_count)."""
    records = await fetch_records(status=status)
    xlsx_bytes = enrichment.build_enriched_workbook(records)
    jsonl_bytes = enrichment.build_jsonl(records)
    return xlsx_bytes, jsonl_bytes, len(records)


async def corpus_stats() -> dict:
    """Powers the Standards Corpus dashboard (item 20) and the shared
    corpus-readiness status (v0.3-continuation Part 4 -- every caller uses
    THESE counts and SEARCHABLE_STATUSES rather than re-deriving its own
    idea of what counts as 'indexed'/'ready').

    Field names ("downloaded", "extracted") are kept even though the
    dataset-first architecture no longer downloads/extracts anything
    itself -- they now mean "imported" and "structured-fields-populated"
    respectively, preserving the existing API/frontend contract (item 18
    of the v0.3-continuation dataset-first refactor: preserve API
    contracts where practical) rather than renaming fields purely for
    their own sake.
    """
    from etsi_corpus.pipeline import SEARCHABLE_STATUSES

    async with AsyncSessionLocal() as session:
        result = await session.execute(select(EtsiDocument.status))
        statuses = [row[0] for row in result.all()]
    total = len(statuses)
    downloaded = sum(1 for s in statuses if s not in ("pending", "import_failed"))  # "downloaded" == imported
    extracted = sum(1 for s in statuses if s not in ("pending", "import_failed", "importing"))
    indexed = sum(1 for s in statuses if s in SEARCHABLE_STATUSES)
    enriched = sum(1 for s in statuses if s in ("completed",))
    processed = enriched
    failed = sum(1 for s in statuses if s.endswith("_failed"))
    pending = sum(1 for s in statuses if s == "pending")
    return {
        "total_documents": total,
        "downloaded": downloaded,
        "extracted": extracted,
        "processed": processed,
        "enriched": enriched,
        "indexed": indexed,
        "failed": failed,
        "pending": pending,
        "requiring_retry": failed + pending,
    }
