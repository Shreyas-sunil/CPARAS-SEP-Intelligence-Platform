"""
API-level tests for the dataset-import endpoints
(api/routes/gitas/etsi_corpus.py, v0.3-continuation dataset-first
architecture). Uses a real FastAPI TestClient against the actual router,
with a real (temporary) SQLite DB and stubbed indexing -- never touches
etsi.org or a real LLM/embedding backend.

Run: python -m pytest backend/tests/test_etsi_dataset_import_api.py -q
"""
import asyncio
import importlib
import io
import json
import os
import sys
import tempfile
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _dataset_record(doc_id: str, pdf_url: str) -> dict:
    return {
        "schema_version": "1.0",
        "identity": {"document_id": doc_id, "pdf_url": pdf_url, "etsi_series": "100 200", "document_number": "100 200", "version": "1.0.0", "title": "Test Spec", "document_type": "TS"},
        "provenance": {"raw_text_path": f"{doc_id}.txt", "extraction_method": "pymupdf", "processing_status": "completed"},
        "classification": {"technology_domain": "Short Range Devices"},
        "technical_summary": {"purpose": "Defines EMC requirements."},
        "clauses": [{"clause_number": "5.1", "clause_title": "General", "summary": "Equipment shall comply.", "page": 1}],
        "patent_relevance": {},
        "extraction_provenance": {"confidence": 0.85, "validation_status": "valid"},
    }


def _build_dataset_zip(n: int) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        lines = []
        for i in range(n):
            doc_id = f"doc{i}"
            lines.append(json.dumps(_dataset_record(doc_id, f"https://www.etsi.org/deliver/doc_{i}.pdf")))
            zf.writestr(f"text/{doc_id}.txt", f"ETSI TS 100 {200+i} V1.0.0\nScope: shall define test requirements.")
        zf.writestr("etsi_dataset.jsonl", "\n".join(lines) + "\n")
    return buf.getvalue()


def _fresh_app(db_path: str, output_dir: str, import_dir: str, chromadb_path: str, dataset_dir: str):
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{db_path}"
    os.environ["ETSI_DATASET_OUTPUT"] = output_dir
    os.environ["ETSI_DATASET_IMPORT_DIR"] = import_dir
    os.environ["ETSI_DATASET_DIR"] = dataset_dir
    os.environ["ETSI_IMPORT_BATCH_SIZE"] = "3"
    os.environ["VECTOR_BACKEND"] = "chromadb"
    os.environ["CHROMADB_PATH"] = chromadb_path

    for mod_name in list(sys.modules):
        if (mod_name in ("core.database", "core.config")
                or mod_name.startswith("etsi_corpus")
                or mod_name.startswith("api.routes.gitas.etsi_corpus")):
            del sys.modules[mod_name]

    core_database = importlib.import_module("core.database")
    etsi_route_mod = importlib.import_module("api.routes.gitas.etsi_corpus")
    pipeline = importlib.import_module("etsi_corpus.pipeline")

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI()
    app.include_router(etsi_route_mod.router, prefix="/api/etsi-corpus")
    client = TestClient(app)
    return client, core_database, pipeline


def _stub_indexing(monkeypatch, pipeline_mod):
    async def fake_index(doc_id, text, metadata, clauses=None):
        return f"etsi_{doc_id}", (len(clauses) if clauses else 1)

    async def fake_delete(doc_id):
        return None
    monkeypatch.setattr(pipeline_mod, "index_document", fake_index)
    monkeypatch.setattr(pipeline_mod, "delete_document_index", fake_delete)


def test_status_reports_not_installed_when_nothing_imported():
    with tempfile.TemporaryDirectory() as tmp:
        client, core_database, _ = _fresh_app(
            os.path.join(tmp, "t.db"), os.path.join(tmp, "out"), os.path.join(tmp, "imports"), os.path.join(tmp, "chroma"), os.path.join(tmp, "etsi_v0.3"),
        )
        asyncio.run(core_database.init_db())
        r = client.get("/api/etsi-corpus/status")
        assert r.status_code == 200
        assert r.json()["installed"] is False


def test_full_import_flow_via_api(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        client, core_database, pipeline = _fresh_app(
            os.path.join(tmp, "t.db"), os.path.join(tmp, "out"), os.path.join(tmp, "imports"), os.path.join(tmp, "chroma"), os.path.join(tmp, "etsi_v0.3"),
        )
        asyncio.run(core_database.init_db())
        _stub_indexing(monkeypatch, pipeline)

        zip_bytes = _build_dataset_zip(4)
        r = client.post(
            "/api/etsi-corpus/dataset/import",
            files={"file": ("etsi_dataset.zip", zip_bytes, "application/zip")},
            params={"limit": 4},
        )
        assert r.status_code == 200, r.text
        job_id = r.json()["job_id"]

        # BackgroundTasks doesn't execute inside TestClient's synchronous
        # request/response cycle -- drive the same import_dataset() call
        # the background task would have made, using the JSONL path the
        # endpoint already unpacked and recorded on the job row.
        async def _get_job_manifest_path():
            from sqlalchemy import select
            async with core_database.AsyncSessionLocal() as session:
                result = await session.execute(select(core_database.EtsiIngestionJob).where(core_database.EtsiIngestionJob.id == job_id))
                return result.scalar_one().manifest_path
        jsonl_path = Path(asyncio.run(_get_job_manifest_path()))
        text_dir = jsonl_path.parent / "text"
        asyncio.run(pipeline.import_dataset(jsonl_path=jsonl_path, text_dir=text_dir, limit=4, job_id=job_id))

        r2 = client.get(f"/api/etsi-corpus/jobs/{job_id}")
        assert r2.status_code == 200
        assert r2.json()["status"] == "completed"
        assert r2.json()["documents_indexed"] == 4

        r3 = client.get("/api/etsi-corpus/status")
        assert r3.json()["installed"] is True
        assert r3.json()["indexed"] == 4

        r4 = client.get("/api/etsi-corpus/export", params={"format": "xlsx"})
        assert r4.status_code == 200


def test_import_rejects_zip_without_jsonl():
    with tempfile.TemporaryDirectory() as tmp:
        client, core_database, _ = _fresh_app(
            os.path.join(tmp, "t.db"), os.path.join(tmp, "out"), os.path.join(tmp, "imports"), os.path.join(tmp, "chroma"), os.path.join(tmp, "etsi_v0.3"),
        )
        asyncio.run(core_database.init_db())

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("readme.txt", "not a dataset")
        r = client.post(
            "/api/etsi-corpus/dataset/import",
            files={"file": ("not_a_dataset.zip", buf.getvalue(), "application/zip")},
        )
        assert r.status_code == 400


def test_pause_returns_409_for_job_not_running_in_process():
    with tempfile.TemporaryDirectory() as tmp:
        client, core_database, _ = _fresh_app(
            os.path.join(tmp, "t.db"), os.path.join(tmp, "out"), os.path.join(tmp, "imports"), os.path.join(tmp, "chroma"), os.path.join(tmp, "etsi_v0.3"),
        )
        asyncio.run(core_database.init_db())
        r = client.post("/api/etsi-corpus/jobs/999/pause")
        assert r.status_code == 409


if __name__ == "__main__":
    print("Run via: python -m pytest backend/tests/test_etsi_dataset_import_api.py -q")
