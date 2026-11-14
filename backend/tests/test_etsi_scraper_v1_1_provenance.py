"""
Tests for the etsi-scraper-pipeline schema_version "1.1" provenance fields
(file_hash, file_size_bytes, local_pdf_path, structured_path, retry_count,
downloaded_at/extracted_at/completed_at, and the verbatim
standard_name_raw/etsi_dir_raw manifest cells) actually landing on
EtsiDocument during import -- previously these were either absent as DB
columns (structured_path, the three per-stage timestamps) or present but
never populated by the dataset-first import (file_hash, file_size_bytes,
local_pdf_path, retry_count -- leftover columns from the old
acquisition-pipeline schema that _apply_dataset_record simply never wrote
to). See etsi_corpus/pipeline.py's _apply_dataset_record and
_get_or_create_document, and core/database.py's
_migrate_sqlite_etsi_document_columns.

Also verifies:
  - a schema_version "1.0" record (the OLD scraper output, missing all of
    these keys entirely) still imports cleanly with the new fields simply
    left None/unset -- never a crash, never a fabricated value.
  - the SQLite auto-migration actually adds the new columns to a database
    that was created BEFORE this change (simulated by creating the table
    with the old column set by hand, then running init_db()).

Run: python -m pytest backend/tests/test_etsi_scraper_v1_1_provenance.py -q
"""
import asyncio
import importlib
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _v11_dataset_record(doc_id: str, pdf_url: str) -> dict:
    """A record shaped like etsi-scraper-pipeline's CURRENT (schema_version
    "1.1") output -- includes every field added in that bump."""
    return {
        "schema_version": "1.1",
        "identity": {
            "document_id": doc_id, "pdf_url": pdf_url, "etsi_series": "100 220-1",
            "document_number": "100 220-1", "version": "1.1.1", "release": "Unknown",
            "title": "Measurement Specification for Wideband Transmitter Stability",
            "document_type": "TS",
        },
        "provenance": {
            "local_pdf_path": f"/data/pdf/{doc_id}.pdf",
            "file_size_bytes": 11338,
            "file_hash": "f0b7dc0ed270910c374ed16acd4b4b93b33302ba8e5d9f0357a32c2df85348d",
            "raw_text_path": f"{doc_id}.txt",
            "structured_path": f"/data/structured/{doc_id}.json",
            "extraction_method": "pymupdf",
            "ocr_used": False, "character_count": 11338, "extraction_quality": "good",
            "processing_status": "completed",
            "retry_count": 2,
            "created_at": "2026-09-01T21:29:00.000000+00:00",
            "downloaded_at": "2026-09-01T21:30:00.212100+00:00",
            "extracted_at": "2026-09-01T21:30:00.327503+00:00",
            "completed_at": "2026-09-01T21:30:05.554321+00:00",
            "standard_name_raw": "TS 100 220-1 V1.1.1",
            "etsi_dir_raw": "9/6/2011 1:06 PM <dir> 100200_100299",
        },
        "classification": {"technology_domain": "Radio Frequency (RF)", "topics": ["Wideband Transmitter Stability"]},
        "technical_summary": {"purpose": "Measurement specification."},
        "clauses": [],
        "patent_relevance": {},
        "extraction_provenance": {"confidence": 0.95, "validation_status": "valid"},
    }


def _v10_dataset_record(doc_id: str, pdf_url: str) -> dict:
    """A record shaped like the OLD scraper output (schema_version "1.0") --
    none of the 1.1 provenance keys exist at all, not even as empty
    strings. Must import without a KeyError/crash and leave the new
    EtsiDocument fields None rather than inventing anything."""
    return {
        "schema_version": "1.0",
        "identity": {
            "document_id": doc_id, "pdf_url": pdf_url, "etsi_series": "100 200",
            "document_number": "100 200", "version": "1.0.0", "release": "Unknown",
            "title": "Old-format Test Spec", "document_type": "TS",
        },
        "provenance": {
            "raw_text_path": f"{doc_id}.txt", "extraction_method": "pymupdf",
            "ocr_used": False, "character_count": 500, "extraction_quality": "good",
            "processing_status": "completed",
        },
        "classification": {"technology_domain": "Short Range Devices", "topics": ["EMC"]},
        "technical_summary": {"purpose": "Defines EMC requirements."},
        "clauses": [],
        "patent_relevance": {},
        "extraction_provenance": {"confidence": 0.85, "validation_status": "valid"},
    }


def _build_dataset(tmp: Path, records: list[dict]) -> tuple[Path, Path]:
    text_dir = tmp / "text"
    text_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = tmp / "etsi_dataset.jsonl"
    with open(jsonl_path, "w", encoding="utf-8") as f:
        for rec in records:
            doc_id = rec["identity"]["document_id"]
            f.write(json.dumps(rec) + "\n")
            (text_dir / f"{doc_id}.txt").write_text("Scope: shall define test requirements.", encoding="utf-8")
    return jsonl_path, text_dir


def _fresh_modules(db_path: str, dataset_output: str, chromadb_path: str, batch_size: int = 3):
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{db_path}"
    os.environ["ETSI_DATASET_OUTPUT"] = dataset_output
    os.environ["ETSI_IMPORT_BATCH_SIZE"] = str(batch_size)
    os.environ["CHROMADB_PATH"] = chromadb_path
    os.environ["VECTOR_BACKEND"] = "chromadb"
    for mod_name in list(sys.modules):
        if mod_name in ("core.database", "core.config") or mod_name.startswith("etsi_corpus"):
            del sys.modules[mod_name]
    core_database = importlib.import_module("core.database")
    pipeline = importlib.import_module("etsi_corpus.pipeline")
    return core_database, pipeline


def _stub_indexing(monkeypatch, pipeline_mod):
    async def fake_index(doc_id, text, metadata, clauses=None):
        return f"etsi_{doc_id}", (len(clauses) if clauses else 1)

    async def fake_delete(doc_id):
        return None
    monkeypatch.setattr(pipeline_mod, "index_document", fake_index)
    monkeypatch.setattr(pipeline_mod, "delete_document_index", fake_delete)


@pytest.mark.skipif(__import__("importlib").util.find_spec("openpyxl") is None, reason="openpyxl not installed")
def test_v1_1_provenance_fields_land_on_the_row(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        db_path = os.path.join(tmp, "t.db")
        core_database, pipeline = _fresh_modules(db_path, os.path.join(tmp, "out"), os.path.join(tmp, "chroma"))
        asyncio.run(core_database.init_db())
        _stub_indexing(monkeypatch, pipeline)

        rec = _v11_dataset_record("docA", "https://www.etsi.org/deliver/docA.pdf")
        jsonl_path, text_dir = _build_dataset(tmp_path, [rec])
        job_id = asyncio.run(pipeline.import_dataset(jsonl_path=jsonl_path, text_dir=text_dir, limit=1))

        status = asyncio.run(pipeline.job_status(job_id))
        assert status["documents_indexed"] == 1, f"expected the record to import+index cleanly, got {status}"

        from sqlalchemy import select
        async def _get_doc():
            async with core_database.AsyncSessionLocal() as session:
                result = await session.execute(select(core_database.EtsiDocument))
                return result.scalars().first()
        doc = asyncio.run(_get_doc())

        # ── columns that already existed but were never populated by the
        #    dataset-first import before this change ──
        assert doc.file_hash == "f0b7dc0ed270910c374ed16acd4b4b93b33302ba8e5d9f0357a32c2df85348d"
        assert doc.file_size_bytes == 11338
        assert doc.local_pdf_path == "/data/pdf/docA.pdf"
        assert doc.retry_count == 2

        # ── brand new columns added this change ──
        assert doc.structured_path == "/data/structured/docA.json"
        assert doc.downloaded_at is not None and doc.downloaded_at.year == 2026 and doc.downloaded_at.month == 9
        assert doc.extracted_at is not None
        assert doc.completed_at is not None
        assert doc.extracted_at >= doc.downloaded_at, "stage timestamps should be in the order the scraper recorded them"
        assert doc.completed_at >= doc.extracted_at

        # ── verbatim manifest cells now preferred over the synthesized fallback ──
        assert doc.standard_name_raw == "TS 100 220-1 V1.1.1"
        assert doc.etsi_dir == "9/6/2011 1:06 PM <dir> 100200_100299"


@pytest.mark.skipif(__import__("importlib").util.find_spec("openpyxl") is None, reason="openpyxl not installed")
def test_v1_0_dataset_still_imports_cleanly_with_new_fields_left_unset(monkeypatch):
    """Backward compatibility: a dataset produced by the OLD scraper (no
    schema_version 1.1 keys at all) must not crash on import, and must not
    have any of the new fields fabricated -- they should simply stay at
    their column default (None / 0)."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        db_path = os.path.join(tmp, "t.db")
        core_database, pipeline = _fresh_modules(db_path, os.path.join(tmp, "out"), os.path.join(tmp, "chroma"))
        asyncio.run(core_database.init_db())
        _stub_indexing(monkeypatch, pipeline)

        rec = _v10_dataset_record("docB", "https://www.etsi.org/deliver/docB.pdf")
        jsonl_path, text_dir = _build_dataset(tmp_path, [rec])
        job_id = asyncio.run(pipeline.import_dataset(jsonl_path=jsonl_path, text_dir=text_dir, limit=1))

        status = asyncio.run(pipeline.job_status(job_id))
        assert status["documents_indexed"] == 1, f"an old-format (1.0) dataset must still import cleanly, got {status}"

        from sqlalchemy import select
        async def _get_doc():
            async with core_database.AsyncSessionLocal() as session:
                result = await session.execute(select(core_database.EtsiDocument))
                return result.scalars().first()
        doc = asyncio.run(_get_doc())

        assert doc.status == "completed"
        assert doc.title == "Old-format Test Spec"
        # None of the 1.1-only fields were in this record -- must not be fabricated:
        assert doc.file_hash is None
        assert doc.file_size_bytes is None
        assert doc.structured_path is None
        assert doc.downloaded_at is None
        assert doc.extracted_at is None
        assert doc.completed_at is None
        assert doc.retry_count == 0  # column default, untouched


@pytest.mark.skipif(__import__("importlib").util.find_spec("openpyxl") is None, reason="openpyxl not installed")
def test_sqlite_auto_migration_adds_new_columns_to_a_pre_existing_database():
    """Simulates upgrading an install that already had a FULL etsi_documents
    table WITHOUT just the four columns added by this change
    (structured_path/downloaded_at/extracted_at/completed_at) -- init_db()'s
    _migrate_sqlite_etsi_document_columns must ALTER TABLE them in, without
    dropping or corrupting any existing row or any other column.

    Built by creating the CURRENT full schema via create_all() and then
    DROPping exactly those four columns (SQLite 3.35+ supports DROP COLUMN)
    -- this accurately simulates "a database created one version before
    this change", rather than a hand-typed guess at old columns that could
    drift from the real historical schema.
    """
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "old.db")
        core_database, _ = _fresh_modules(db_path, os.path.join(tmp, "out"), os.path.join(tmp, "chroma"))

        # Build the full current schema first (this also creates every
        # OTHER table the app needs, avoiding "no such table" elsewhere).
        asyncio.run(core_database.init_db())

        import sqlite3
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO etsi_documents (pdf_url, url_hash, title, status, retry_count, chunks_indexed, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 0, 0, datetime('now'), datetime('now'))",
            ("https://www.etsi.org/deliver/pre-existing.pdf", "somehash", "Pre-existing row", "completed"),
        )
        conn.commit()
        for col in ("structured_path", "downloaded_at", "extracted_at", "completed_at"):
            conn.execute(f"ALTER TABLE etsi_documents DROP COLUMN {col}")
        conn.commit()
        cols_before = {row[1] for row in conn.execute("PRAGMA table_info(etsi_documents)").fetchall()}
        conn.close()
        for col in ("structured_path", "downloaded_at", "extracted_at", "completed_at"):
            assert col not in cols_before, "test setup didn't actually remove the column"

        # Re-run init_db() against this now-downgraded database, exactly as
        # would happen on app startup against an old install.
        asyncio.run(core_database.init_db())

        conn = sqlite3.connect(db_path)
        cols_after = {row[1] for row in conn.execute("PRAGMA table_info(etsi_documents)").fetchall()}
        for expected in ("structured_path", "downloaded_at", "extracted_at", "completed_at"):
            assert expected in cols_after, f"auto-migration did not add column {expected!r}: has {cols_after}"

        # the pre-existing row must have survived the migration untouched
        row = conn.execute("SELECT title, status FROM etsi_documents WHERE pdf_url = ?", ("https://www.etsi.org/deliver/pre-existing.pdf",)).fetchone()
        assert row == ("Pre-existing row", "completed")
        conn.close()


if __name__ == "__main__":
    print("Run via: python -m pytest backend/tests/test_etsi_scraper_v1_1_provenance.py -q")
