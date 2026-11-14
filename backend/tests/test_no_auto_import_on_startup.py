"""
Explicit regression guard: normal application startup (main.py's lifespan
-> ensure_dataset_available()) must NEVER trigger a dataset import or an
embedding call -- only inspect/log whether a package is already installed
(v0.3-continuation: dataset-first architecture, no live acquisition to
guard against either -- the acquisition-triggering surface doesn't exist
in this codebase at all anymore, see test_etsi_dataset_import.py's
test_no_module_references_acquisition_at_all).

Run: python -m pytest backend/tests/test_no_auto_import_on_startup.py -q
"""
import asyncio
import importlib
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def test_ensure_dataset_available_never_calls_import_or_indexing(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "t.db")
        os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{db_path}"
        os.environ["ETSI_DATASET_DIR"] = os.path.join(tmp, "etsi_v0.3")  # never created -- "not installed" case

        for mod_name in list(sys.modules):
            if mod_name in ("core.database", "core.config") or mod_name.startswith("etsi_corpus"):
                del sys.modules[mod_name]

        core_database = importlib.import_module("core.database")
        package_mod = importlib.import_module("etsi_corpus.package")
        pipeline_mod = importlib.import_module("etsi_corpus.pipeline")
        asyncio.run(core_database.init_db())

        def _poison(*a, **kw):
            raise AssertionError("ensure_dataset_available() must never trigger dataset import or indexing")

        monkeypatch.setattr(pipeline_mod, "import_dataset", _poison)
        monkeypatch.setattr(pipeline_mod, "index_document", _poison)

        status = asyncio.run(package_mod.ensure_dataset_available())
        assert status["installed"] is False
        # No AssertionError was raised above -> nothing was called.


def test_main_module_source_never_references_import_entrypoints():
    """Belt-and-braces static check: main.py's source text must not
    reference import_dataset/index_document directly -- the only
    sanctioned call path into dataset import is an explicit user action
    via the API routes, never anything in the startup lifespan."""
    backend_dir = os.path.join(os.path.dirname(__file__), "..")
    main_src = open(os.path.join(backend_dir, "main.py")).read()
    for forbidden in ("import_dataset(", "index_document("):
        assert forbidden not in main_src, f"main.py must not directly reference {forbidden}"


if __name__ == "__main__":
    test_ensure_dataset_available_never_calls_import_or_indexing(None)
    test_main_module_source_never_references_import_entrypoints()
    print("OK")
