"""
Tests for etsi_corpus/package.py -- the versioned, portable knowledge
package layer, and the "never auto-import on startup" architectural rule
(v0.3-continuation: dataset-first architecture, no live acquisition).

Run: python -m pytest backend/tests/test_etsi_package.py -q
"""
import asyncio
import importlib
import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _fresh_modules(db_path: str, dataset_output: str, dataset_dir: str, chromadb_path: str):
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{db_path}"
    os.environ["ETSI_DATASET_OUTPUT"] = dataset_output
    os.environ["ETSI_DATASET_DIR"] = dataset_dir
    os.environ["CHROMADB_PATH"] = chromadb_path
    os.environ["VECTOR_BACKEND"] = "chromadb"
    for mod_name in list(sys.modules):
        if mod_name in ("core.database", "core.config") or mod_name.startswith("etsi_corpus"):
            del sys.modules[mod_name]
    core_database = importlib.import_module("core.database")
    package_mod = importlib.import_module("etsi_corpus.package")
    export_mod = importlib.import_module("etsi_corpus.export")
    return core_database, package_mod, export_mod


def test_ensure_dataset_available_reports_not_installed_when_absent():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "test.db")
        dataset_dir = os.path.join(tmp, "etsi_v0.3")
        core_database, package_mod, _ = _fresh_modules(
            db_path, os.path.join(tmp, "output"), dataset_dir, os.path.join(tmp, "chroma"),
        )
        status = asyncio.run(package_mod.ensure_dataset_available())
        assert status["installed"] is False
        assert package_mod.load_package_manifest() is None


def test_build_package_writes_versioned_manifest_and_exports():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "test.db")
        dataset_output = os.path.join(tmp, "output")
        dataset_dir = os.path.join(tmp, "etsi_v0.3")
        chromadb_path = os.path.join(tmp, "chroma")
        os.makedirs(chromadb_path, exist_ok=True)
        (Path(chromadb_path) / "chroma.sqlite3").write_bytes(b"fake-chroma-data")

        core_database, package_mod, export_mod = _fresh_modules(
            db_path, dataset_output, dataset_dir, chromadb_path,
        )
        asyncio.run(core_database.init_db())

        source_dataset_path = Path(tmp) / "etsi_dataset.jsonl"
        source_dataset_path.write_bytes(b'{"identity": {"document_id": "x"}}\n')

        async def _seed():
            async with core_database.AsyncSessionLocal() as session:
                job = core_database.EtsiIngestionJob(
                    manifest_path=str(source_dataset_path), status="completed", manifest_row_count=1,
                )
                session.add(job)
                doc = core_database.EtsiDocument(
                    pdf_url="https://www.etsi.org/deliver/x.pdf", url_hash="abc123",
                    standard_name_raw="TS 100 220-1 V1.1.1", status="completed",
                    technology_domain="Short Range Devices",
                )
                session.add(doc)
                await session.commit()
        asyncio.run(_seed())

        manifest = asyncio.run(package_mod.build_package())

        assert manifest.dataset_version == "v0.3"
        assert manifest.unique_pdf_count == 1
        assert manifest.documents_processed == 1
        assert manifest.vector_index_included is True
        assert manifest.source_manifest_hash != "Unknown"
        assert manifest.source_manifest_filename == "etsi_dataset.jsonl"
        assert manifest.source_record_count == 1

        pkg_dir = Path(dataset_dir)
        assert (pkg_dir / "package_manifest.json").exists()
        assert (pkg_dir / package_mod.ENRICHED_XLSX_NAME).exists()
        assert (pkg_dir / package_mod.ENRICHED_JSONL_NAME).exists()
        assert (pkg_dir / "vector_index" / "chroma.sqlite3").exists()

        reloaded = package_mod.load_package_manifest()
        assert reloaded is not None
        assert reloaded.documents_processed == 1

        status = asyncio.run(package_mod.ensure_dataset_available())
        assert status["installed"] is True
        assert status["documents_processed"] == 1


def test_build_package_uses_most_recent_job_not_stale_history():
    """The dataset-first architecture reads the MOST RECENT import job's
    manifest_row_count -- "most recent" is the semantically correct
    source of truth for a dataset-import model (a later, smaller retry/
    subset import genuinely reflects a smaller current dataset)."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "test.db")
        dataset_output = os.path.join(tmp, "output")
        dataset_dir = os.path.join(tmp, "etsi_v0.3")
        chromadb_path = os.path.join(tmp, "chroma")

        core_database, package_mod, _ = _fresh_modules(db_path, dataset_output, dataset_dir, chromadb_path)
        asyncio.run(core_database.init_db())

        old_dataset = Path(tmp) / "old_dataset.jsonl"
        old_dataset.write_bytes(b"old\n")
        new_dataset = Path(tmp) / "new_dataset.jsonl"
        new_dataset.write_bytes(b"new\n")

        async def _seed():
            async with core_database.AsyncSessionLocal() as session:
                old_job = core_database.EtsiIngestionJob(manifest_path=str(old_dataset), status="completed", manifest_row_count=5)
                session.add(old_job)
                await session.flush()
                new_job = core_database.EtsiIngestionJob(manifest_path=str(new_dataset), status="completed", manifest_row_count=200)
                session.add(new_job)
                await session.commit()
        asyncio.run(_seed())

        manifest = asyncio.run(package_mod.build_package())
        assert manifest.source_record_count == 200
        assert manifest.source_manifest_filename == "new_dataset.jsonl"


def test_install_package_copies_and_rewires_vector_path():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "a.db")
        dataset_output = os.path.join(tmp, "a_output")
        machine_a_dir = os.path.join(tmp, "a_etsi_v0.3")
        chromadb_path = os.path.join(tmp, "a_chroma")
        os.makedirs(chromadb_path, exist_ok=True)
        (Path(chromadb_path) / "chroma.sqlite3").write_bytes(b"fake-chroma-data")

        core_database, package_mod, _ = _fresh_modules(db_path, dataset_output, machine_a_dir, chromadb_path)
        asyncio.run(core_database.init_db())

        async def _seed():
            async with core_database.AsyncSessionLocal() as session:
                doc = core_database.EtsiDocument(
                    pdf_url="https://www.etsi.org/deliver/x.pdf", url_hash="abc123", status="completed",
                )
                session.add(doc)
                await session.commit()
        asyncio.run(_seed())
        asyncio.run(package_mod.build_package())

        db_path_b = os.path.join(tmp, "b.db")
        machine_b_dir = os.path.join(tmp, "b_etsi_v0.3")
        chromadb_path_b = os.path.join(tmp, "b_chroma")
        core_database_b, package_mod_b, _ = _fresh_modules(db_path_b, os.path.join(tmp, "b_output"), machine_b_dir, chromadb_path_b)
        asyncio.run(core_database_b.init_db())

        assert package_mod_b.load_package_manifest() is None

        installed_manifest = asyncio.run(package_mod_b.install_package(machine_a_dir))
        assert installed_manifest.documents_processed == 1
        assert (Path(machine_b_dir) / "vector_index" / "chroma.sqlite3").exists()

        from core.config import settings as settings_b
        assert str(Path(machine_b_dir) / "vector_index") in settings_b.CHROMADB_PATH


def test_install_package_rejects_directory_without_manifest():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "test.db")
        dataset_dir = os.path.join(tmp, "etsi_v0.3")
        core_database, package_mod, _ = _fresh_modules(
            db_path, os.path.join(tmp, "output"), dataset_dir, os.path.join(tmp, "chroma"),
        )
        not_a_package = os.path.join(tmp, "just_some_dir")
        os.makedirs(not_a_package)
        with pytest.raises(ValueError):
            asyncio.run(package_mod.install_package(not_a_package))


if __name__ == "__main__":
    print("Run via: python -m pytest backend/tests/test_etsi_package.py -q")
