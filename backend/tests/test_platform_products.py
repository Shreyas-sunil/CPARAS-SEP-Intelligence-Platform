"""
Product-structure checks for the main platform after the split:

  * the platform contains exactly CPARAS Bulk and CPARAS ISLD
  * CSARAS (now the separate "Initial Categoriser" project) is fully gone:
    no routes, no Python package, no database tables, no config
  * user-facing API metadata / export names use the new product names

Run: python -m pytest backend/tests/test_platform_products.py -q
"""
import asyncio
import importlib
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

BACKEND_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _fresh_main(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'p.db'}")
    for mod in list(sys.modules):
        if mod in ("core.config", "core.database", "main") or mod.startswith("api.routes"):
            del sys.modules[mod]
    return importlib.import_module("main")


def test_no_csaras_routes_and_new_product_names(tmp_path, monkeypatch):
    m = _fresh_main(tmp_path, monkeypatch)
    paths = {getattr(r, "path", "") for r in m.app.routes}
    assert not any("csaras" in p.lower() for p in paths)
    assert "/api/sep/upload" in paths            # CPARAS Bulk
    assert "/api/analysis/analyze" in paths      # CPARAS ISLD
    assert "CPARAS" in m.app.title
    text = (m.app.title + " " + (m.app.description or "")).lower()
    assert "gitas" not in text and "c-saras" not in text and "csaras" not in text
    assert "cparas bulk" in text and "cparas isld" in text


def test_csaras_package_and_tests_removed():
    assert not os.path.exists(os.path.join(BACKEND_DIR, "csaras"))
    assert not os.path.exists(os.path.join(BACKEND_DIR, "api", "routes", "csaras"))
    assert not [f for f in os.listdir(os.path.join(BACKEND_DIR, "tests")) if f.startswith("test_csaras")]
    import importlib.util
    assert importlib.util.find_spec("csaras") is None


def test_no_csaras_tables_or_settings(tmp_path, monkeypatch):
    _fresh_main(tmp_path, monkeypatch)
    core_database = importlib.import_module("core.database")
    core_config = importlib.import_module("core.config")
    assert not [t for t in core_database.Base.metadata.tables if t.startswith("csaras")]
    assert not [k for k in core_config.Settings.model_fields if k.startswith("CSARAS")]

    from sqlalchemy import text

    async def go():
        await core_database.init_db()
        async with core_database.engine.connect() as conn:
            rows = (await conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))).fetchall()
            return [r[0] for r in rows]

    tables = asyncio.run(go())
    assert "sep_bulk_patents" in tables and "patents" in tables
    assert not [t for t in tables if t.startswith("csaras")]


def test_export_names_use_new_product_names():
    from etsi_bulk import excel_export as bulk_export
    from api.routes.cparas import excel_export as isld_export

    class _Run:
        filename = "declarations.xlsx"

    class _Decl:
        filename = "nokia.pdf"

    assert bulk_export.export_filename(_Run()) == "declarations_CPARAS_Bulk_enriched.xlsx"
    assert isld_export.export_filename(_Decl()) == "nokia_CPARAS_ISLD_enriched.xlsx"
    assert bulk_export.SUMMARY_SHEET_NAME == "CPARAS Bulk Summary"
    assert all(c.startswith("CPARAS_Bulk_") for c in bulk_export.GITAS_COLUMNS)
