"""
Tests for the GITAS-enriched Excel export feature
(etsi_bulk/excel_export.py + the GET /api/sep/runs/{run_id}/export/excel
endpoint in api/routes/sep.py).

Run: python -m pytest backend/tests/test_excel_export.py -q
 or: python3 backend/tests/_run_all.py

These exercise the real upload -> score -> export pipeline end-to-end
against the actual test workbook (Data_For_Testing.xlsx, provided
alongside this task) over a temporary SQLite database and an in-process
ASGI client — no network, no external services. The embedding step runs
in `tfidf-fallback` mode automatically when sentence-transformers isn't
installed (see research/embeddings.py), so these tests do not require the
full ML dependency stack.
"""

import asyncio
import importlib
import io
import os
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    import openpyxl
    HAVE_OPENPYXL = True
except ImportError:
    HAVE_OPENPYXL = False

try:
    import httpx
    HAVE_HTTPX = True
except ImportError:
    HAVE_HTTPX = False

TEST_XLSX_PATH = os.environ.get(
    "CPARAS_BULK_TEST_XLSX",
    os.path.join(os.path.dirname(__file__), "..", "..", "Data_For_Testing.xlsx"),
)


def _find_test_workbook() -> str | None:
    """Locate the real Data_For_Testing.xlsx wherever this checkout has it.
    Falls back gracefully (tests skip) if it isn't present in this
    environment, rather than failing the whole suite on missing fixture
    data that lives outside the repo."""
    candidates = [
        TEST_XLSX_PATH,
        os.path.join(os.path.dirname(__file__), "fixtures", "Data_For_Testing.xlsx"),
    ]
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    return None


def _fresh_app_and_db(db_path: str):
    """Builds a fresh FastAPI app bound to an isolated sqlite file. Reimports
    core.database under a new DATABASE_URL so each test gets its own engine
    (module-level `engine` in core.database is created once at import time
    from settings.DATABASE_URL) rather than sharing state across tests."""
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{db_path}"

    for mod_name in list(sys.modules):
        if mod_name in ("core.database", "core.config") or mod_name.startswith("api.routes"):
            del sys.modules[mod_name]

    core_database = importlib.import_module("core.database")
    sep_routes = importlib.import_module("api.routes.gitas.dashboard")

    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(sep_routes.router, prefix="/api/sep")
    return app, core_database, sep_routes


def _run_upload_and_export(xlsx_path: str):
    """Runs the full upload -> score -> export flow once and returns
    (upload_json, export_status, export_bytes, export_headers, sep_routes_module)."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "test.db")
        app, core_database, sep_routes = _fresh_app_and_db(db_path)

        async def main():
            await core_database.init_db()
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                data = open(xlsx_path, "rb").read()
                r = await client.post(
                    "/api/sep/upload",
                    files={"file": ("Data_For_Testing.xlsx", data,
                                     "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
                )
                assert r.status_code == 200, r.text
                upload_json = r.json()

                r2 = await client.get(f"/api/sep/runs/{upload_json['run_id']}/export/excel")
                return upload_json, r2.status_code, r2.content, dict(r2.headers)

        return asyncio.run(main()) + (sep_routes,)


# ── Test 1 & 2 — row and original-column preservation ───────────────────────

def test_row_and_column_preservation():
    if not (HAVE_OPENPYXL and HAVE_HTTPX):
        return
    xlsx_path = _find_test_workbook()
    if not xlsx_path:
        return  # fixture not available in this environment

    upload_json, status, content, headers, _ = _run_upload_and_export(xlsx_path)
    assert status == 200
    assert headers["content-type"] == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

    orig = openpyxl.load_workbook(xlsx_path, data_only=True)
    exp = openpyxl.load_workbook(io.BytesIO(content), data_only=True)

    assert set(["Sheet1", "Summary", "CPARAS Bulk Summary"]).issubset(set(exp.sheetnames))

    ows, ews = orig["Sheet1"], exp["Sheet1"]
    orig_headers = [c.value for c in ows[1]]
    n_orig = len(orig_headers)
    exp_headers = [c.value for c in ews[1]]

    # Test 2 — original columns present, in the same order.
    assert exp_headers[:n_orig] == orig_headers

    # Test 1 — every original data row is present with a corresponding output row.
    orig_data_rows = [r for r in ows.iter_rows(min_row=2, values_only=True) if any(v not in (None, "") for v in r)]
    exp_data_rows = [r for r in ews.iter_rows(min_row=2, values_only=True) if any(v not in (None, "") for v in r)]
    assert len(exp_data_rows) == len(orig_data_rows) == upload_json["total_rows"]

    # Original cell values are untouched (only appended columns should differ).
    for i, (orow, erow) in enumerate(zip(orig_data_rows, exp_data_rows), start=2):
        assert erow[:n_orig] == orow, f"row {i} original columns changed"


# ── Test 3 — GITAS columns present with exact contractual names ─────────────

def test_gitas_columns_present():
    if not (HAVE_OPENPYXL and HAVE_HTTPX):
        return
    xlsx_path = _find_test_workbook()
    if not xlsx_path:
        return

    from etsi_bulk.excel_export import GITAS_COLUMNS
    _, status, content, _, _ = _run_upload_and_export(xlsx_path)
    assert status == 200
    exp = openpyxl.load_workbook(io.BytesIO(content), data_only=True)
    ws = exp["Sheet1"]
    headers = [c.value for c in ws[1]]
    for col in GITAS_COLUMNS:
        assert col in headers, f"missing GITAS column: {col}"


# ── Test 4 — GITAS values are exported verbatim from the stored result ──────

def test_gitas_values_match_stored_result():
    if not (HAVE_OPENPYXL and HAVE_HTTPX):
        return
    xlsx_path = _find_test_workbook()
    if not xlsx_path:
        return

    upload_json, status, content, _, _ = _run_upload_and_export(xlsx_path)
    assert status == 200
    exp = openpyxl.load_workbook(io.BytesIO(content), data_only=True)
    ws = exp["Sheet1"]
    headers = [c.value for c in ws[1]]
    idx = {h: i for i, h in enumerate(headers)}

    patents = upload_json["patents"]
    assert len(patents) >= 1
    for i, patent in enumerate(patents):
        row = [c.value for c in ws[i + 2]]
        essentiality = patent["essentiality"]
        evidence_label = row[idx["CPARAS_Bulk_Evidence_Label"]]
        # Non-essential/no-result rows aside, Essential/Potential rows must
        # carry a non-trivial evidence label sourced from the stored result.
        assert row[idx["CPARAS_Bulk_Status"]] == "SUCCESS"
        if essentiality == "Essential":
            assert evidence_label in ("Strong topical match", "Declared family confirmed")
        elif essentiality == "Potential":
            assert evidence_label == "Weak topical overlap"
        # Semantic similarity / confidence are the exact stored floats.
        assert row[idx["CPARAS_Bulk_Semantic_Similarity"]] == patent["semantic_similarity"]
        # v0.4.1: the SEP confidence is CPARAS_Bulk_Overall_Confidence and
        # the label is CPARAS_Bulk_Essentiality; Extraction_Confidence is an
        # input-quality share in [0, 1].
        assert row[idx["CPARAS_Bulk_Overall_Confidence"]] == patent["confidence"]
        assert row[idx["CPARAS_Bulk_Essentiality"]] == essentiality
        assert essentiality in ("Essential", "Potential", "Non-Essential")
        ext = row[idx["CPARAS_Bulk_Extraction_Confidence"]]
        assert ext is not None and 0.0 <= ext <= 1.0
        assert row[idx["CPARAS_Bulk_Report_Link"]] == f"/api/sep/patents/{patent['id']}"
        validation = row[idx["CPARAS_Bulk_Validation_Status"]]
        assert validation in ("VALID", "NEEDS_REVIEW", "INVALID")


# ── Test 5 — export does not call the scoring engine ─────────────────────────

def test_export_does_not_rescore():
    if not (HAVE_OPENPYXL and HAVE_HTTPX):
        return
    xlsx_path = _find_test_workbook()
    if not xlsx_path:
        return

    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "test.db")
        app, core_database, sep_routes = _fresh_app_and_db(db_path)

        async def main():
            await core_database.init_db()
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                data = open(xlsx_path, "rb").read()
                r = await client.post(
                    "/api/sep/upload",
                    files={"file": ("Data_For_Testing.xlsx", data,
                                     "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
                )
                assert r.status_code == 200
                run_id = r.json()["run_id"]

                # Patch score_records where sep.py looks it up (post-import
                # binding) and assert the export path never touches it.
                with mock.patch.object(sep_routes, "score_records") as spy:
                    r2 = await client.get(f"/api/sep/runs/{run_id}/export/excel")
                    assert r2.status_code == 200
                    spy.assert_not_called()

        asyncio.run(main())


# ── Test 6 — GITAS Summary sheet exists with run metadata/aggregates ────────

def test_gitas_summary_sheet():
    if not (HAVE_OPENPYXL and HAVE_HTTPX):
        return
    xlsx_path = _find_test_workbook()
    if not xlsx_path:
        return

    upload_json, status, content, _, _ = _run_upload_and_export(xlsx_path)
    assert status == 200
    exp = openpyxl.load_workbook(io.BytesIO(content), data_only=True)
    assert "CPARAS Bulk Summary" in exp.sheetnames
    ws = exp["CPARAS Bulk Summary"]
    cells = {row[0]: row[1] for row in ws.iter_rows(values_only=True) if row[0]}

    assert cells.get("Batch ID") == upload_json["run_id"]
    assert cells.get("Source file") == "Data_For_Testing.xlsx"
    assert cells.get("Total input rows") == upload_json["total_rows"]
    assert cells.get("Successfully analyzed") == len(upload_json["patents"])
    # Evidence level buckets 0-3 must all be present (even if zero).
    for lvl in range(4):
        assert f"Evidence level {lvl}" in cells


# ── Test 7 — download endpoint returns a valid, openable .xlsx ──────────────

def test_download_endpoint_returns_valid_xlsx():
    if not (HAVE_OPENPYXL and HAVE_HTTPX):
        return
    xlsx_path = _find_test_workbook()
    if not xlsx_path:
        return

    _, status, content, headers, _ = _run_upload_and_export(xlsx_path)
    assert status == 200
    assert headers["content-type"] == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    assert content[:2] == b"PK"  # xlsx is a zip container
    wb = openpyxl.load_workbook(io.BytesIO(content))  # raises if not a valid workbook
    assert {"Sheet1", "Summary", "CPARAS Bulk Summary"}.issubset(set(wb.sheetnames))


# ── Test: unknown run returns 404 ────────────────────────────────────────────

def test_export_unknown_run_404():
    if not (HAVE_OPENPYXL and HAVE_HTTPX):
        return

    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "test.db")
        app, core_database, sep_routes = _fresh_app_and_db(db_path)

        async def main():
            await core_database.init_db()
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                r = await client.get("/api/sep/runs/999999/export/excel")
                assert r.status_code == 404

        asyncio.run(main())


# ── Unit tests for gitas_fields_for_patent — no DB/HTTP needed ──────────────

def _fake_patent(**kwargs):
    from types import SimpleNamespace
    defaults = dict(
        id=1, essentiality=None, confidence=None, semantic_similarity=None,
        claim_match=None, standard_coverage=None,
        declared_family_match=False, matched_standard=None, matched_clause=None,
        matched_clause_title=None, illustrative_part=None, title=None,
        standard_raw=None, spec_version=None, release=None, legal_status=None,
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def test_gitas_fields_no_result_row():
    from etsi_bulk.excel_export import gitas_fields_for_patent
    p = _fake_patent()
    f = gitas_fields_for_patent(p, run_id=1)
    assert f["CPARAS_Bulk_Status"] == "FAILED"
    assert f["CPARAS_Bulk_Validation_Status"] == "INVALID"
    assert f["CPARAS_Bulk_Evidence_Level"] == 0


def test_gitas_fields_declared_family_match_essential():
    from etsi_bulk.excel_export import gitas_fields_for_patent
    p = _fake_patent(
        id=42, essentiality="Essential", confidence=0.9, semantic_similarity=0.85,
        declared_family_match=True, matched_standard="TS 38.331 v16.2.0",
        matched_clause="Clause 5.3.3", matched_clause_title="RRC Connection Establishment",
        illustrative_part="Some claim text", standard_raw="3GPP TS 38.331",
        spec_version="16.2.0", release="Rel-16", legal_status="Active",
    )
    f = gitas_fields_for_patent(p, run_id=1)
    assert f["CPARAS_Bulk_Status"] == "SUCCESS"
    assert f["CPARAS_Bulk_Evidence_Level"] == 3
    assert f["CPARAS_Bulk_Mapping_Type"] == "declared_family_match"
    assert f["CPARAS_Bulk_Standard_Family"] == "38.331"
    assert f["CPARAS_Bulk_Standard_Version"] == "v16.2.0"
    assert f["CPARAS_Bulk_Corpus_Coverage"] is True
    assert f["CPARAS_Bulk_Declaration_Conflict"] is False
    assert f["CPARAS_Bulk_Validation_Status"] == "VALID"
    assert f["CPARAS_Bulk_Report_Link"] == "/api/sep/patents/42"


def test_gitas_fields_legal_status_lapsed_reason_code():
    from etsi_bulk.excel_export import gitas_fields_for_patent
    p = _fake_patent(
        id=7, essentiality="Potential", confidence=0.4, semantic_similarity=0.3,
        declared_family_match=False, matched_standard="TS 24.501 v15.3.0",
        standard_raw="3GPP TS 24.501", legal_status="Expired - Lifetime",
    )
    f = gitas_fields_for_patent(p, run_id=1)
    assert "LEGAL_STATUS_LAPSED" in f["CPARAS_Bulk_Reason_Codes"]
    assert f["CPARAS_Bulk_Validation_Status"] == "NEEDS_REVIEW"


if __name__ == "__main__":
    # Zero-dependency manual run, mirroring the other test_*.py files.
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"OK: {name}")
