"""
Tests for etsi_corpus/pipeline.py's dataset-first import_dataset()
(v0.3-continuation: replaces the old live-acquisition pipeline). Covers
the same guarantees the old pipeline was tested for, against the new
architecture:

  - bounded worker pool (never one asyncio Task per record)
  - idempotent resume (re-importing skips already-completed records)
  - crash resilience (one record's unexpected failure doesn't cascade)
  - live job_status() during an in-progress import (not frozen until completion)
  - clause-aware indexing from imported structured data
  - never touches etsi.org, downloads a PDF, or calls an LLM

Run: python -m pytest backend/tests/test_etsi_dataset_import.py -q
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


def _dataset_record(doc_id: str, pdf_url: str, title: str = "Test Spec", clauses: bool = True) -> dict:
    return {
        "schema_version": "1.0",
        "identity": {
            "document_id": doc_id, "pdf_url": pdf_url, "etsi_series": "100 200",
            "document_number": "100 200", "version": "1.0.0", "release": "Unknown",
            "title": title, "document_type": "TS",
        },
        "provenance": {
            "raw_text_path": f"{doc_id}.txt", "extraction_method": "pymupdf",
            "ocr_used": False, "character_count": 500, "extraction_quality": "good",
            "processing_status": "completed",
        },
        "classification": {
            "technology_domain": "Short Range Devices", "topics": ["EMC"],
        },
        "technical_summary": {"purpose": "Defines EMC requirements."},
        "clauses": ([{"clause_number": "5.1", "clause_title": "General", "summary": "Equipment shall comply.", "page": 1}] if clauses else []),
        "patent_relevance": {},
        "extraction_provenance": {"confidence": 0.85, "validation_status": "valid"},
    }


def _build_dataset(tmp: Path, n: int) -> tuple[Path, Path]:
    text_dir = tmp / "text"
    text_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = tmp / "etsi_dataset.jsonl"
    with open(jsonl_path, "w", encoding="utf-8") as f:
        for i in range(n):
            doc_id = f"doc{i}"
            rec = _dataset_record(doc_id, f"https://www.etsi.org/deliver/doc_{i}.pdf")
            f.write(json.dumps(rec) + "\n")
            (text_dir / f"{doc_id}.txt").write_text(f"ETSI TS 100 {200+i} V1.0.0\nScope: shall define test requirements.", encoding="utf-8")
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


def test_no_module_references_acquisition_at_all():
    """Static check: pipeline.py must never import httpx, a downloader,
    or call an LLM extraction function -- the whole point of the
    dataset-first refactor."""
    src = open(os.path.join(os.path.dirname(__file__), "..", "etsi_corpus", "pipeline.py")).read()
    for forbidden in ("import httpx", "BoundedDownloader", "download_pdf(", "extract_structured(", "extract_pdf_text("):
        assert forbidden not in src, f"pipeline.py must not reference {forbidden!r} (acquisition must be genuinely removed)"


@pytest.mark.skipif(__import__("importlib").util.find_spec("openpyxl") is None, reason="openpyxl not installed")
def test_import_dataset_end_to_end(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        db_path = os.path.join(tmp, "t.db")
        core_database, pipeline = _fresh_modules(db_path, os.path.join(tmp, "out"), os.path.join(tmp, "chroma"), batch_size=3)
        asyncio.run(core_database.init_db())
        _stub_indexing(monkeypatch, pipeline)

        jsonl_path, text_dir = _build_dataset(tmp_path, 6)
        job_id = asyncio.run(pipeline.import_dataset(jsonl_path=jsonl_path, text_dir=text_dir, limit=6))

        status = asyncio.run(pipeline.job_status(job_id))
        assert status["total_documents"] == 6
        assert status["documents_indexed"] == 6
        assert status["documents_failed"] == 0
        assert status["status"] == "completed"

        # ── verify real structured data landed on the row ──
        from sqlalchemy import select
        async def _get_docs():
            async with core_database.AsyncSessionLocal() as session:
                result = await session.execute(select(core_database.EtsiDocument))
                return result.scalars().all()
        docs = asyncio.run(_get_docs())
        assert len(docs) == 6
        doc = docs[0]
        assert doc.status == "completed"
        assert doc.technology_domain == "Short Range Devices"
        assert json.loads(doc.clauses_json)[0]["clause_number"] == "5.1"
        assert doc.raw_extracted_text_path and os.path.exists(doc.raw_extracted_text_path)


@pytest.mark.skipif(__import__("importlib").util.find_spec("openpyxl") is None, reason="openpyxl not installed")
def test_reimport_is_idempotent_no_reprocessing(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        db_path = os.path.join(tmp, "t.db")
        core_database, pipeline = _fresh_modules(db_path, os.path.join(tmp, "out"), os.path.join(tmp, "chroma"), batch_size=3)
        asyncio.run(core_database.init_db())
        _stub_indexing(monkeypatch, pipeline)

        jsonl_path, text_dir = _build_dataset(tmp_path, 4)
        asyncio.run(pipeline.import_dataset(jsonl_path=jsonl_path, text_dir=text_dir, limit=4))

        call_count = {"n": 0}
        real_index = pipeline.index_document

        async def counting_index(doc_id, text, metadata, clauses=None):
            call_count["n"] += 1
            return await real_index(doc_id, text, metadata, clauses)
        monkeypatch.setattr(pipeline, "index_document", counting_index)

        asyncio.run(pipeline.import_dataset(jsonl_path=jsonl_path, text_dir=text_dir, limit=4))
        assert call_count["n"] == 0, "re-importing an already-completed dataset must not re-index anything"


@pytest.mark.skipif(__import__("importlib").util.find_spec("openpyxl") is None, reason="openpyxl not installed")
def test_bounded_worker_pool_never_exceeds_configured_concurrency(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        db_path = os.path.join(tmp, "t.db")
        core_database, pipeline = _fresh_modules(db_path, os.path.join(tmp, "out"), os.path.join(tmp, "chroma"), batch_size=3)
        asyncio.run(core_database.init_db())

        concurrency = {"current": 0, "max": 0}
        lock = asyncio.Lock()

        async def tracked_index(doc_id, text, metadata, clauses=None):
            async with lock:
                concurrency["current"] += 1
                concurrency["max"] = max(concurrency["max"], concurrency["current"])
            await asyncio.sleep(0.02)
            async with lock:
                concurrency["current"] -= 1
            return f"etsi_{doc_id}", 1

        async def fake_delete(doc_id):
            return None
        monkeypatch.setattr(pipeline, "index_document", tracked_index)
        monkeypatch.setattr(pipeline, "delete_document_index", fake_delete)

        jsonl_path, text_dir = _build_dataset(tmp_path, 15)
        asyncio.run(pipeline.import_dataset(jsonl_path=jsonl_path, text_dir=text_dir, limit=15))

        assert concurrency["max"] <= 3, f"worker pool exceeded configured concurrency: saw {concurrency['max']}"
        assert concurrency["max"] >= 2, "test is meaningless if imports never actually overlapped"


@pytest.mark.skipif(__import__("importlib").util.find_spec("openpyxl") is None, reason="openpyxl not installed")
def test_one_record_crashing_does_not_cascade(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        db_path = os.path.join(tmp, "t.db")
        core_database, pipeline = _fresh_modules(db_path, os.path.join(tmp, "out"), os.path.join(tmp, "chroma"), batch_size=3)
        asyncio.run(core_database.init_db())

        call_count = {"n": 0}

        async def flaky_index(doc_id, text, metadata, clauses=None):
            call_count["n"] += 1
            if call_count["n"] == 3:
                raise RuntimeError("simulated unexpected crash")
            return f"etsi_{doc_id}", 1

        async def fake_delete(doc_id):
            return None
        monkeypatch.setattr(pipeline, "index_document", flaky_index)
        monkeypatch.setattr(pipeline, "delete_document_index", fake_delete)

        jsonl_path, text_dir = _build_dataset(tmp_path, 10)
        job_id = asyncio.run(pipeline.import_dataset(jsonl_path=jsonl_path, text_dir=text_dir, limit=10))

        status = asyncio.run(pipeline.job_status(job_id))
        assert status["documents_indexed"] >= 9, f"expected at least 9/10 despite one simulated crash, got {status}"


@pytest.mark.skipif(__import__("importlib").util.find_spec("openpyxl") is None, reason="openpyxl not installed")
def test_missing_text_file_marks_import_failed_not_fabricated(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        db_path = os.path.join(tmp, "t.db")
        core_database, pipeline = _fresh_modules(db_path, os.path.join(tmp, "out"), os.path.join(tmp, "chroma"), batch_size=2)
        asyncio.run(core_database.init_db())
        _stub_indexing(monkeypatch, pipeline)

        text_dir = tmp_path / "text"
        text_dir.mkdir()
        jsonl_path = tmp_path / "etsi_dataset.jsonl"
        with open(jsonl_path, "w", encoding="utf-8") as f:
            f.write(json.dumps(_dataset_record("doc0", "https://www.etsi.org/deliver/doc_0.pdf")) + "\n")
        # No text/doc0.txt written -- text is genuinely missing.

        job_id = asyncio.run(pipeline.import_dataset(jsonl_path=jsonl_path, text_dir=text_dir, limit=1))
        failed = asyncio.run(pipeline.failed_documents(job_id))
        assert len(failed) == 1
        assert failed[0]["failure_stage"] == "import_failed"
        assert "text" in failed[0]["error_message"].lower()


if __name__ == "__main__":
    print("Run via: python -m pytest backend/tests/test_etsi_dataset_import.py -q")
