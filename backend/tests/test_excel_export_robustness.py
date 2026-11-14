"""
Robustness tests for etsi_bulk/excel_export.py — the failure-mode and
degraded-input paths added on top of the happy-path tests in
test_excel_export.py: formula-preserving reopen, .csv/.xls blob fallback
with full column fidelity, malformed/unrecognisable headers, per-row error
containment, and the last-resort DB-only reconstruction.

These exercise `build_enriched_workbook` directly against fabricated
run/patent objects (SimpleNamespace) rather than going through the full
upload -> score -> export HTTP flow — they're testing the exporter's own
defensive behavior, not the scoring pipeline.

Run: python -m pytest backend/tests/test_excel_export_robustness.py -q
 or: python3 backend/tests/_run_all.py
"""

import io
import os
import sys
from datetime import datetime
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    import openpyxl
    HAVE_OPENPYXL = True
except ImportError:
    HAVE_OPENPYXL = False

if HAVE_OPENPYXL:
    from etsi_bulk.excel_export import (
        build_enriched_workbook, gitas_fields_for_patent, _error_gitas_row,
        _align_rows, export_filename, GITAS_COLUMNS, MAX_EXPORT_DATA_ROWS,
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
        # Remaining fields _RECONSTRUCT_COLUMNS (the last-resort DB-only
        # fallback) reads via getattr — a real SepBulkPatent always has
        # these (even if None), so the fake needs them too.
        iprd_reference="ISLD-1", signature_date="2020-01-01", company="Acme Corp",
        disclosure_number="1", family_id="1", spec_number="38.331",
        patent_type="Basis Patent", country_raw="US UNITED STATES", filing_date="2020-01-01",
        explicit_disclosure=True, gen_2g=False, gen_3g=False, gen_4g=False, gen_5g=True,
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def _make_xlsx_bytes(header: list[str], rows: list[list], formulas: dict[tuple[int, int], str] | None = None) -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    for j, h in enumerate(header, start=1):
        ws.cell(row=1, column=j, value=h)
    for i, row in enumerate(rows, start=2):
        for j, v in enumerate(row, start=1):
            ws.cell(row=i, column=j, value=v)
    for (r, c), formula in (formulas or {}).items():
        ws.cell(row=r, column=c, value=formula)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


_BULK_HEADER = [
    "IPRD_ID", "IPRD_REFERENCE", "IPRD_SIGNATURE_DATE", "Reflected_Date", "COMP_LEGAL_NAME",
    "DECL_IS_PROP_FLAG", "LICD_DEC_PREP_TO_GRANT_FLAG", "LICD_REC_CONDI_FLAG", "DIPG_ID",
    "DIPG_DISPLAY_NUMBER", "DIPG_EXTERNAL_ID", "Standard", "Ess_To_Standard", "WI_Type",
    "WOIT_ETSI_DELIVERABLE_NUMBER", "WOIT_REFERENCE", "WOIT_VERSION", "3GPP_Type",
    "TGPP_NUMBER", "TGPV_VERSION", "Patent_Type", "PATT_APPLICATION_NUMBER", "PUBL_NUMBER",
    "PBPA_TITLEEN", "PBPA_PRIORITY_NUMBERS", "Country_Of_Registration", "PBPA_APP_DATE",
    "ETPR_ID", "ETPR_ACRONYM", "Ess_To_Project", "Illustrative_Part", "DIPG_PATF_ID",
    "Original_Application_Number", "Original_Publication_Number", "Explicitely_Disclosed",
    "Normalized_Patent", "2G", "3G", "4G", "5G", "Legal Status",
]


def _bulk_row(app_number="US123", pub_number="US123A1"):
    row = [None] * len(_BULK_HEADER)
    row[_BULK_HEADER.index("PATT_APPLICATION_NUMBER")] = app_number
    row[_BULK_HEADER.index("PUBL_NUMBER")] = pub_number
    row[_BULK_HEADER.index("COMP_LEGAL_NAME")] = "Acme Corp"
    return row


# ── Formula preservation ─────────────────────────────────────────────────────

def test_formula_cells_preserved_on_export():
    if not HAVE_OPENPYXL:
        return
    header = _BULK_HEADER + ["Computed"]
    row = _bulk_row() + [None]
    blob = _make_xlsx_bytes(header, [row], formulas={(2, len(header)): "=1+1"})

    run = _fake_run(original_file_blob=blob, total_rows=1)
    patents = [_fake_patent(id=1)]

    xlsx_bytes, warnings = build_enriched_workbook(run, patents)
    wb = openpyxl.load_workbook(io.BytesIO(xlsx_bytes))  # no data_only: read formula text back
    ws = wb["Sheet1"]
    formula_cell = ws.cell(row=2, column=len(header))
    assert formula_cell.value == "=1+1", f"formula was not preserved, got {formula_cell.value!r}"


# ── CSV blob: full column fidelity fallback ──────────────────────────────────

def test_csv_blob_falls_back_with_full_column_fidelity():
    if not HAVE_OPENPYXL:
        return
    import csv
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(_BULK_HEADER)
    w.writerow([str(v) if v is not None else "" for v in _bulk_row()])
    csv_bytes = buf.getvalue().encode("utf-8")

    run = _fake_run(filename="declarations.csv", sheet_name="Sheet1",
                     original_file_blob=csv_bytes, total_rows=1)
    patents = [_fake_patent(id=1)]

    xlsx_bytes, warnings = build_enriched_workbook(run, patents)
    assert any("csv" in w.lower() or ".xlsx workbook" in w.lower() for w in warnings)

    wb = openpyxl.load_workbook(io.BytesIO(xlsx_bytes), data_only=True)
    ws = wb["Sheet1"]
    headers = [c.value for c in ws[1]]
    # Every original CSV column survived, not just the DB-persisted subset —
    # e.g. Reflected_Date/DECL_IS_PROP_FLAG are never persisted to the DB but
    # must still appear here because we re-parsed the raw bytes.
    assert headers[:len(_BULK_HEADER)] == _BULK_HEADER
    assert "CPARAS_Bulk_Status" in headers
    row2 = {h: c for h, c in zip(headers, [c.value for c in ws[2]])}
    assert row2["COMP_LEGAL_NAME"] == "Acme Corp"
    assert row2["CPARAS_Bulk_Status"] == "SUCCESS"


# ── Malformed / unmatched header: positional fallback ────────────────────────

def test_case_different_header_now_resolves_directly_not_positionally():
    """`_align_rows` used to look columns up in COLUMN_MAP by exact string
    match, so a header that only differed in case from the known column
    name (e.g. 'patt_application_number' vs 'PATT_APPLICATION_NUMBER')
    fell back to positional alignment — a degraded mode. It now goes
    through `schema.resolve_header`, which normalizes case, so this same
    header is resolved directly and no positional-fallback warning is
    produced. This is the flexibility improvement's export-side half —
    see test_etsi_bulk_parser.py for the parser-side half."""
    if not HAVE_OPENPYXL:
        return
    header = list(_BULK_HEADER)
    idx = header.index("PATT_APPLICATION_NUMBER")
    header[idx] = "patt_application_number"
    pub_idx = header.index("PUBL_NUMBER")
    header[pub_idx] = "publ_number"

    row = _bulk_row()
    blob = _make_xlsx_bytes(header, [row])

    run = _fake_run(original_file_blob=blob, total_rows=1)
    patents = [_fake_patent(id=1)]

    xlsx_bytes, warnings = build_enriched_workbook(run, patents)
    assert not any("positionally" in w.lower() for w in warnings)

    wb = openpyxl.load_workbook(io.BytesIO(xlsx_bytes), data_only=True)
    ws = wb["Sheet1"]
    headers = [c.value for c in ws[1]]
    row2 = dict(zip(headers, [c.value for c in ws[2]]))
    assert row2["CPARAS_Bulk_Status"] == "SUCCESS"
    assert row2["CPARAS_Bulk_Report_Link"] == "/api/sep/patents/1"


def test_unresolvable_header_falls_back_to_positional_alignment():
    """When the application/publication-number columns use header text
    that genuinely can't be resolved to any known field (not even via
    the fuzzy fallback — e.g. opaque internal column codes), export must
    still succeed, matching rows to patents positionally rather than
    leaving the GITAS_* block unaligned."""
    if not HAVE_OPENPYXL:
        return
    header = list(_BULK_HEADER)
    idx = header.index("PATT_APPLICATION_NUMBER")
    header[idx] = "Col_47"  # opaque, not resolvable to any known field
    pub_idx = header.index("PUBL_NUMBER")
    header[pub_idx] = "Col_48"

    row = _bulk_row()
    blob = _make_xlsx_bytes(header, [row])

    run = _fake_run(original_file_blob=blob, total_rows=1)
    patents = [_fake_patent(id=1)]

    xlsx_bytes, warnings = build_enriched_workbook(run, patents)
    assert any("positionally" in w.lower() for w in warnings)

    wb = openpyxl.load_workbook(io.BytesIO(xlsx_bytes), data_only=True)
    ws = wb["Sheet1"]
    headers = [c.value for c in ws[1]]
    row2 = dict(zip(headers, [c.value for c in ws[2]]))
    # Despite the header mismatch, the row still got its GITAS result —
    # this is exactly what the positional fallback is for.
    assert row2["CPARAS_Bulk_Status"] == "SUCCESS"
    assert row2["CPARAS_Bulk_Report_Link"] == "/api/sep/patents/1"


# ── Per-row error containment ─────────────────────────────────────────────────

def test_malformed_patent_row_does_not_abort_whole_export():
    if not HAVE_OPENPYXL:
        return
    blob = _make_xlsx_bytes(_BULK_HEADER, [_bulk_row("US1", "US1A1"), _bulk_row("US2", "US2A1")])
    run = _fake_run(original_file_blob=blob, total_rows=2)

    good = _fake_patent(id=1, application_number="US1")
    # matched_standard as a non-string triggers a regex .search() TypeError
    # inside gitas_fields_for_patent — this must degrade to a FAILED row,
    # not crash the export.
    bad = _fake_patent(id=2, application_number="US2", matched_standard=12345)
    patents = [good, bad]

    xlsx_bytes, warnings = build_enriched_workbook(run, patents)
    wb = openpyxl.load_workbook(io.BytesIO(xlsx_bytes), data_only=True)
    ws = wb["Sheet1"]
    headers = [c.value for c in ws[1]]
    row2 = dict(zip(headers, [c.value for c in ws[2]]))
    row3 = dict(zip(headers, [c.value for c in ws[3]]))
    assert row2["CPARAS_Bulk_Status"] == "SUCCESS"
    assert row3["CPARAS_Bulk_Status"] == "FAILED"
    assert row3["CPARAS_Bulk_Validation_Status"] == "INVALID"
    assert "CPARAS Bulk Summary" in wb.sheetnames  # summary still built despite the bad row


def test_gitas_fields_direct_exception_path():
    """Unit-level: gitas_fields_for_patent itself raises on a non-string
    matched_standard (regex .search expects str) — confirms the assumption
    the containment test above relies on, and that _error_gitas_row is a
    safe drop-in replacement."""
    if not HAVE_OPENPYXL:
        return
    bad = _fake_patent(id=9, matched_standard=12345)
    try:
        gitas_fields_for_patent(bad, run_id=1)
        raised = False
    except Exception:
        raised = True
    assert raised, "expected a non-string matched_standard to raise inside gitas_fields_for_patent"

    fallback = _error_gitas_row(bad, RuntimeError("boom"))
    assert fallback["CPARAS_Bulk_Status"] == "FAILED"
    assert fallback["CPARAS_Bulk_Report_Link"] == "/api/sep/patents/9"


# ── Last resort: no blob at all ──────────────────────────────────────────────

def test_no_blob_falls_back_to_db_only_reconstruction():
    if not HAVE_OPENPYXL:
        return
    run = _fake_run(original_file_blob=None, total_rows=1)
    patents = [_fake_patent(id=1)]

    xlsx_bytes, warnings = build_enriched_workbook(run, patents)
    assert any("no original workbook bytes" in w.lower() for w in warnings)
    wb = openpyxl.load_workbook(io.BytesIO(xlsx_bytes), data_only=True)
    ws = wb["Sheet1"]
    headers = [c.value for c in ws[1]]
    assert "COMP_LEGAL_NAME" in headers  # persisted field still present
    assert "CPARAS_Bulk_Status" in headers


def test_unreadable_blob_falls_back_to_db_only_reconstruction():
    if not HAVE_OPENPYXL:
        return
    run = _fake_run(original_file_blob=b"\x00\x01totally not a spreadsheet", total_rows=1)
    patents = [_fake_patent(id=1)]

    xlsx_bytes, warnings = build_enriched_workbook(run, patents)
    wb = openpyxl.load_workbook(io.BytesIO(xlsx_bytes), data_only=True)
    assert {"Sheet1", "CPARAS Bulk Summary"}.issubset(set(wb.sheetnames))
    ws = wb["Sheet1"]
    headers = [c.value for c in ws[1]]
    assert "CPARAS_Bulk_Status" in headers
    row2 = dict(zip(headers, [c.value for c in ws[2]]))
    assert row2["CPARAS_Bulk_Status"] == "SUCCESS"


# ── Row-count cap ─────────────────────────────────────────────────────────────

def test_row_cap_is_enforced_and_warned():
    if not HAVE_OPENPYXL:
        return
    # Cheap smoke test of the cap logic without generating 50k+ real rows:
    # temporarily shrink the module-level cap via monkeypatch-by-attribute.
    import etsi_bulk.excel_export as ee
    original_cap = ee.MAX_EXPORT_DATA_ROWS
    try:
        ee.MAX_EXPORT_DATA_ROWS = 1
        rows = [_bulk_row("US1", "US1A1"), _bulk_row("US2", "US2A1")]
        blob = _make_xlsx_bytes(_BULK_HEADER, rows)
        run = _fake_run(original_file_blob=blob, total_rows=2)
        patents = [_fake_patent(id=1, application_number="US1")]

        xlsx_bytes, warnings = ee.build_enriched_workbook(run, patents)
        assert any("exceeding this export" in w for w in warnings)
        wb = openpyxl.load_workbook(io.BytesIO(xlsx_bytes), data_only=True)
        ws = wb["Sheet1"]
        headers = [c.value for c in ws[1]]
        # The cap bounds how many rows get GITAS-enriched (an export-time
        # safety valve against pathological files) — it must NOT delete any
        # original row/data, since that would violate row preservation.
        # Row 2 (within the cap) gets a GITAS result; row 3 (beyond the cap)
        # keeps its original data but has no GITAS_Status.
        row2 = dict(zip(headers, [c.value for c in ws[2]]))
        row3 = dict(zip(headers, [c.value for c in ws[3]]))
        assert row2["CPARAS_Bulk_Status"] == "SUCCESS"
        assert row3.get("CPARAS_Bulk_Status") is None
        assert row3["COMP_LEGAL_NAME"] == "Acme Corp"  # original data still intact
    finally:
        ee.MAX_EXPORT_DATA_ROWS = original_cap


# ── export_filename sanitization ─────────────────────────────────────────────

def test_export_filename_sanitizes_header_unsafe_characters():
    if not HAVE_OPENPYXL:
        return
    run = _fake_run(filename='weird"name\r\nwith/slashes\\and.xlsx')
    name = export_filename(run)
    assert '"' not in name and "\r" not in name and "\n" not in name
    assert name.endswith("_CPARAS_Bulk_enriched.xlsx")


def test_export_filename_empty_falls_back():
    if not HAVE_OPENPYXL:
        return
    run = _fake_run(filename="")
    name = export_filename(run)
    assert name == "sep_run_CPARAS_Bulk_enriched.xlsx"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"OK: {name}")
