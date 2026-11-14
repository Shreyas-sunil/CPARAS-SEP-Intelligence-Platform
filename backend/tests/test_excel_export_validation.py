"""
Tests for the post-generation validation step in etsi_bulk/excel_export.py
(`_validate_generated_workbook` / `ExcelExportValidationError`).

Covers:
  - The validator accepting a well-formed workbook (unit-level, and via a
    real build_enriched_workbook() run).
  - Each individual failure mode the validator checks for: unreadable
    bytes, a missing required sheet, a missing GITAS_* column, a
    duplicate column name, and a data-row-count mismatch.
  - That `build_enriched_workbook` actually calls the validator and lets
    `ExcelExportValidationError` propagate as a controlled failure (not a
    silently returned corrupted/incomplete workbook).
  - That the export HTTP endpoint turns a validation failure into a
    controlled 500 response rather than streaming bad bytes.

Run: python -m pytest backend/tests/test_excel_export_validation.py -q
 or: python3 backend/tests/_run_all.py
"""

import asyncio
import io
import os
import sys
import tempfile
from datetime import datetime
from types import SimpleNamespace
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

if HAVE_OPENPYXL:
    from etsi_bulk.excel_export import (
        build_enriched_workbook, _validate_generated_workbook,
        ExcelExportValidationError, GITAS_COLUMNS,
    )


def _fake_run(**kwargs):
    defaults = dict(
        id=1, filename="Data_For_Testing.xlsx", sheet_name="Sheet1", total_rows=0,
        embedder_mode="tfidf-fallback", meta_json=None,
        created_at=datetime(2026, 1, 1), original_file_blob=None,
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def _fake_patent(**kwargs):
    defaults = dict(
        id=1, essentiality="Essential", confidence=0.8, semantic_similarity=0.75,
        claim_match=0.7, standard_coverage=0.6,
        declared_family_match=True, matched_standard="TS 38.331 v16.2.0",
        matched_clause="Clause 5.3.3", matched_clause_title="RRC Connection Establishment",
        illustrative_part="Some claim text", title="A patent title",
        standard_raw="3GPP TS 38.331", spec_version="16.2.0", release="Rel-16",
        legal_status="Active", application_number="US123", publication_number="US123A1",
        iprd_reference="ISLD-1", signature_date="2020-01-01", company="Acme Corp",
        disclosure_number="1", family_id="1", spec_number="38.331",
        patent_type="Basis Patent", country_raw="US UNITED STATES", filing_date="2020-01-01",
        explicit_disclosure=True, gen_2g=False, gen_3g=False, gen_4g=False, gen_5g=True,
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def _minimal_valid_bytes(n_data_rows: int = 1) -> bytes:
    """Builds the smallest workbook that should pass every validation check:
    Sheet1 (header + all 19 GITAS_* columns + n_data_rows non-blank rows)
    and a GITAS Summary sheet."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    header = ["COMP_LEGAL_NAME"] + GITAS_COLUMNS
    for j, h in enumerate(header, start=1):
        ws.cell(row=1, column=j, value=h)
    for i in range(n_data_rows):
        ws.cell(row=2 + i, column=1, value=f"Company {i}")
        ws.cell(row=2 + i, column=2, value="SUCCESS")
    wb.create_sheet("CPARAS Bulk Summary").cell(row=1, column=1, value="Run Overview")
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ── Success path ──────────────────────────────────────────────────────────────

def test_validator_accepts_well_formed_workbook():
    if not HAVE_OPENPYXL:
        return
    xlsx_bytes = _minimal_valid_bytes(n_data_rows=3)
    _validate_generated_workbook(xlsx_bytes, expected_data_rows=3)  # must not raise


def test_validator_accepts_real_build_enriched_workbook_output():
    """End-to-end-ish: build_enriched_workbook() itself calls the validator
    internally, so a normal successful call proves the success path works
    against real output, not just a hand-crafted fixture."""
    if not HAVE_OPENPYXL:
        return
    run = _fake_run(original_file_blob=None, total_rows=2)
    patents = [_fake_patent(id=1), _fake_patent(id=2, application_number="US456")]
    xlsx_bytes, warnings = build_enriched_workbook(run, patents)  # would raise if invalid
    # Re-validate independently too, to double-check the contract explicitly.
    _validate_generated_workbook(xlsx_bytes, expected_data_rows=2)


# ── Failure paths — unit level against _validate_generated_workbook ─────────

def test_validator_rejects_unreadable_bytes():
    if not HAVE_OPENPYXL:
        return
    try:
        _validate_generated_workbook(b"not an xlsx file at all", expected_data_rows=0)
        assert False, "expected ExcelExportValidationError"
    except ExcelExportValidationError as e:
        assert "reopened" in str(e)


def test_validator_rejects_missing_gitas_summary_sheet():
    if not HAVE_OPENPYXL:
        return
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    header = ["COMP_LEGAL_NAME"] + GITAS_COLUMNS
    for j, h in enumerate(header, start=1):
        ws.cell(row=1, column=j, value=h)
    ws.cell(row=2, column=1, value="Acme")
    ws.cell(row=2, column=2, value="SUCCESS")
    buf = io.BytesIO()
    wb.save(buf)

    try:
        _validate_generated_workbook(buf.getvalue(), expected_data_rows=1)
        assert False, "expected ExcelExportValidationError"
    except ExcelExportValidationError as e:
        assert "CPARAS Bulk Summary" in str(e)


def test_validator_rejects_missing_sheet1():
    if not HAVE_OPENPYXL:
        return
    wb = openpyxl.Workbook()
    wb.active.title = "NotSheet1"
    wb.create_sheet("CPARAS Bulk Summary")
    buf = io.BytesIO()
    wb.save(buf)

    try:
        _validate_generated_workbook(buf.getvalue(), expected_data_rows=0)
        assert False, "expected ExcelExportValidationError"
    except ExcelExportValidationError as e:
        assert "Sheet1" in str(e)


def test_validator_rejects_missing_gitas_column():
    if not HAVE_OPENPYXL:
        return
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    # Omit one canonical GITAS_* column (GITAS_Report_Link).
    header = ["COMP_LEGAL_NAME"] + [c for c in GITAS_COLUMNS if c != "CPARAS_Bulk_Report_Link"]
    for j, h in enumerate(header, start=1):
        ws.cell(row=1, column=j, value=h)
    ws.cell(row=2, column=1, value="Acme")
    wb.create_sheet("CPARAS Bulk Summary")
    buf = io.BytesIO()
    wb.save(buf)

    try:
        _validate_generated_workbook(buf.getvalue(), expected_data_rows=1)
        assert False, "expected ExcelExportValidationError"
    except ExcelExportValidationError as e:
        assert "CPARAS_Bulk_Report_Link" in str(e)


def test_validator_rejects_duplicate_column_names():
    if not HAVE_OPENPYXL:
        return
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    header = ["COMP_LEGAL_NAME", "COMP_LEGAL_NAME"] + GITAS_COLUMNS  # duplicated on purpose
    for j, h in enumerate(header, start=1):
        ws.cell(row=1, column=j, value=h)
    ws.cell(row=2, column=1, value="Acme")
    wb.create_sheet("CPARAS Bulk Summary")
    buf = io.BytesIO()
    wb.save(buf)

    try:
        _validate_generated_workbook(buf.getvalue(), expected_data_rows=1)
        assert False, "expected ExcelExportValidationError"
    except ExcelExportValidationError as e:
        assert "duplicate" in str(e).lower()
        assert "COMP_LEGAL_NAME" in str(e)


def test_validator_rejects_row_count_mismatch():
    if not HAVE_OPENPYXL:
        return
    xlsx_bytes = _minimal_valid_bytes(n_data_rows=2)
    try:
        _validate_generated_workbook(xlsx_bytes, expected_data_rows=5)
        assert False, "expected ExcelExportValidationError"
    except ExcelExportValidationError as e:
        assert "2 data row" in str(e) and "expected 5" in str(e)


# ── build_enriched_workbook actually calls the validator ────────────────────

def test_build_enriched_workbook_propagates_validation_failure():
    """Forces the validator to fail (via monkeypatch) and confirms
    build_enriched_workbook does not swallow the error or return bytes —
    it must raise ExcelExportValidationError, a controlled failure type."""
    if not HAVE_OPENPYXL:
        return
    import etsi_bulk.excel_export as ee

    run = _fake_run(original_file_blob=None, total_rows=1)
    patents = [_fake_patent(id=1)]

    with mock.patch.object(ee, "_validate_generated_workbook",
                            side_effect=ee.ExcelExportValidationError("forced failure for test")):
        try:
            ee.build_enriched_workbook(run, patents)
            assert False, "expected ExcelExportValidationError to propagate"
        except ee.ExcelExportValidationError as e:
            assert "forced failure" in str(e)


def test_build_enriched_workbook_raises_on_genuine_row_count_mismatch():
    """A more organic failure trigger: patch _build_reconstructed_sheet1 so
    it physically writes fewer rows than `len(patents)` (the count the
    validator is told to expect for this last-resort tier), proving the
    validator catches a real internal inconsistency — not just the
    monkeypatched-error case above."""
    if not HAVE_OPENPYXL:
        return
    import etsi_bulk.excel_export as ee

    run = _fake_run(original_file_blob=None, total_rows=2)
    patents = [_fake_patent(id=1), _fake_patent(id=2, application_number="US456")]

    def _reconstruct_truncated(wb, patents_arg):
        from etsi_bulk.excel_export import _RECONSTRUCT_COLUMNS
        ws = wb.active
        ws.title = "Sheet1"
        for j, h in enumerate([h for h, _ in _RECONSTRUCT_COLUMNS], start=1):
            ws.cell(row=1, column=j, value=h)
        for i, p in enumerate(patents_arg[:1], start=2):  # only ever writes 1 row
            for j, (_, attr) in enumerate(_RECONSTRUCT_COLUMNS, start=1):
                ws.cell(row=i, column=j, value=(getattr(p, attr) if attr else None))
        return ["(test) truncated reconstruction"]

    original_write = ee._write_gitas_columns

    def _write_gitas_truncated(ws, header_row, first_data_row, n_orig_cols, aligned, run_id):
        # Also drop the GITAS_* columns for the same row the reconstruction
        # above omitted, so that row ends up genuinely, entirely blank —
        # not just missing its GITAS_* half.
        return original_write(ws, header_row, first_data_row, n_orig_cols, aligned[:1], run_id)

    with mock.patch.object(ee, "_build_reconstructed_sheet1", side_effect=_reconstruct_truncated), \
         mock.patch.object(ee, "_write_gitas_columns", side_effect=_write_gitas_truncated):
        try:
            ee.build_enriched_workbook(run, patents)
            assert False, "expected a row-count validation failure"
        except ee.ExcelExportValidationError as e:
            assert "data row" in str(e)


# ── Endpoint-level: validation failure becomes a controlled 500 ─────────────

def test_export_endpoint_returns_controlled_error_on_validation_failure():
    if not (HAVE_OPENPYXL and HAVE_HTTPX):
        return

    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "test.db")
        os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{db_path}"
        for mod_name in list(sys.modules):
            if mod_name in ("core.database", "core.config") or mod_name.startswith("api.routes") or mod_name == "etsi_bulk.excel_export":
                del sys.modules[mod_name]

        import importlib
        core_database = importlib.import_module("core.database")
        sep_routes = importlib.import_module("api.routes.gitas.dashboard")

        from fastapi import FastAPI
        app = FastAPI()
        app.include_router(sep_routes.router, prefix="/api/sep")

        async def main():
            await core_database.init_db()
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                xlsx_path = os.environ.get("CPARAS_BULK_TEST_XLSX") or os.path.join(
                    os.path.dirname(__file__), "fixtures", "Data_For_Testing.xlsx")
                if not os.path.isfile(xlsx_path):
                    return  # fixture not available in this environment
                data = open(xlsx_path, "rb").read()
                r = await client.post(
                    "/api/sep/upload",
                    files={"file": ("Data_For_Testing.xlsx", data,
                                     "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
                )
                assert r.status_code == 200
                run_id = r.json()["run_id"]

                from etsi_bulk.excel_export import ExcelExportValidationError
                with mock.patch.object(sep_routes, "build_enriched_workbook",
                                        side_effect=ExcelExportValidationError("simulated corruption")):
                    r2 = await client.get(f"/api/sep/runs/{run_id}/export/excel")
                    assert r2.status_code == 500
                    assert "Excel export failed" in r2.json().get("detail", "")
                    # A 500 with a JSON error body — never a bad "success"
                    # response carrying corrupted/partial xlsx bytes.
                    assert r2.headers.get("content-type", "").startswith("application/json")

        asyncio.run(main())


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"OK: {name}")
