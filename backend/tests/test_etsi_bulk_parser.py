"""
Tests for etsi_bulk/parser.py + etsi_bulk/schema.py.

Run: python -m pytest backend/tests/test_etsi_bulk_parser.py -q
 or: python3 backend/tests/_run_all.py

Covers three things:

  1. The two known real-world column layouts —
     REFERENCE (Data_For_Testing.xlsx: IPRD_ID, TGPP_NUMBER/TGPV_VERSION,
     PBPA_TITLEEN, "Legal Status") and UIUX-DATABASE
     (Sample_Data_for_UIUX_Database.xlsx: no IPRD_ID, no TGPP_NUMBER/
     TGPV_VERSION, "Title", "Legal_Status") — both must be recognized and
     produce fully-populated records.
  2. Header resolution is genuinely flexible, not a hardcoded two-format
     switch: a THIRD, never-before-seen header naming convention (see
     `NOVEL_HEADER` below) must also be recognized and correctly mapped,
     via `schema.resolve_header`'s exact-alias-first/fuzzy-fallback
     matching — this is what "any data" actually depends on.
  3. That flexibility doesn't create false positives: an unrelated
     spreadsheet, and — importantly — the *other* pipeline's own known
     header (the single-declaration format handled by
     utils/csv_parser.py) must NOT be misrouted into this bulk pipeline
     just because a few of its columns fuzzy-match loosely.
"""
import io
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from etsi_bulk.parser import (  # noqa: E402
    looks_like_bulk_header,
    parse_bulk_workbook,
    _derive_spec_fields,
)
from etsi_bulk.schema import (  # noqa: E402
    COLUMN_MAP, REQUIRED_COLUMNS, resolve_header,
)

try:
    import openpyxl
    HAVE_OPENPYXL = True
except ImportError:
    HAVE_OPENPYXL = False


def _make_xlsx(rows: list[list], sheet_title: str = "Sheet1") -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = sheet_title
    for row in rows:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


REFERENCE_HEADER = [
    "IPRD_ID", "IPRD_REFERENCE", "COMP_LEGAL_NAME", "DIPG_DISPLAY_NUMBER",
    "Standard", "TGPP_NUMBER", "TGPV_VERSION", "PATT_APPLICATION_NUMBER",
    "PUBL_NUMBER", "PBPA_TITLEEN", "Legal Status", "Illustrative_Part",
    "Explicitely_Disclosed", "2G", "3G", "4G", "5G",
]

UIUX_HEADER = [
    "IPRD_REFERENCE", "DIPG_DISPLAY_NUMBER", "COMP_LEGAL_NAME", "Standard",
    "PATT_APPLICATION_NUMBER", "PUBL_NUMBER", "Title", "Legal_Status",
    "Illustrative_Part", "Explicitely_Disclosed", "2G", "3G", "4G", "5G",
]

# A THIRD layout that shares no header string with either known export —
# every single column name is a plausible-but-different spelling. Nothing
# in this list appears verbatim in COLUMN_MAP or _EXTRA_ALIASES; if this
# still parses correctly, header resolution is genuinely generalizing
# rather than matching a hardcoded list of two known formats.
NOVEL_HEADER = [
    "Declaration Reference", "Applicant Name", "Disclosure No",
    "Declared Standard", "App No.", "Pub No", "Invention Title",
    "Patent Status", "Claim", "5G",
]

# The single-declaration pipeline's own known header (utils/csv_parser.py /
# xlsx_parser.py territory) — must NOT be recognized as a bulk export even
# though a couple of its columns loosely resemble bulk-export fields
# (an application/publication number column is common to both formats).
SINGLE_DECLARATION_HEADER = [
    "Disclosure Number", "Applicant/holder", "Application No.",
    "Publication No.", "Country of registration", "Patent/Application Title",
    "Work Item or Standard No.",
]

UNRELATED_HEADER = ["Employee Name", "Salary", "Department", "Start Date"]


# ── Format detection ─────────────────────────────────────────────────────────

def test_reference_header_detected():
    assert looks_like_bulk_header(REFERENCE_HEADER)


def test_uiux_header_without_iprd_id_detected():
    """The core regression: a header with no IPRD_ID column at all must
    still be recognized as the bulk-export format."""
    assert looks_like_bulk_header(UIUX_HEADER)
    assert "IPRD_ID" not in UIUX_HEADER


def test_novel_header_never_seen_before_is_detected():
    """Flexibility check: a header spelled entirely differently from both
    known real exports must still be recognized via fuzzy resolution."""
    assert looks_like_bulk_header(NOVEL_HEADER)


def test_required_columns_present_in_both_known_layouts():
    for col in REQUIRED_COLUMNS:
        assert col in REFERENCE_HEADER, f"{col} missing from reference layout fixture"
        assert col in UIUX_HEADER, f"{col} missing from UIUX layout fixture"


def test_unrelated_header_not_detected():
    assert not looks_like_bulk_header(UNRELATED_HEADER)


def test_single_declaration_header_not_misrouted():
    """Regression guard: fuzzy matching must not be loose enough to pull
    the *other* pipeline's own header format into this one."""
    assert not looks_like_bulk_header(SINGLE_DECLARATION_HEADER)


def test_empty_header_not_detected():
    assert not looks_like_bulk_header([])
    assert not looks_like_bulk_header(None)


# ── Header resolution (schema.resolve_header) ────────────────────────────────

def test_resolve_header_exact_known_aliases():
    assert resolve_header("PBPA_TITLEEN") == "title"
    assert resolve_header("Title") == "title"
    assert resolve_header("Legal Status") == "legal_status"
    assert resolve_header("Legal_Status") == "legal_status"


def test_resolve_header_case_and_punctuation_insensitive():
    assert resolve_header("  comp_legal_name  ") == "company"
    assert resolve_header("Application No.") == "application_number"


def test_resolve_header_fuzzy_novel_spellings():
    assert resolve_header("Applicant Name") == "company"
    assert resolve_header("Declaration Reference") == "iprd_reference"
    assert resolve_header("Invention Title") == "title"
    assert resolve_header("Patent Status") == "legal_status"
    assert resolve_header("Declared Standard") == "standard_raw"


def test_resolve_header_unrecognized_returns_none():
    assert resolve_header("Employee Salary") is None
    assert resolve_header("") is None
    assert resolve_header(None) is None


def test_resolve_header_does_not_misresolve_single_declaration_column():
    """The single-declaration format's own known header string must not
    get pulled into a bulk-export field."""
    assert resolve_header("Work Item or Standard No.") is None


# ── spec_number/spec_version derivation from free text ──────────────────────

def test_derive_spec_fields_simple_form():
    num, ver = _derive_spec_fields("3GPP TS 48.008 version 7.5.0")
    assert num == "48.008"
    assert ver == "7.5.0"


def test_derive_spec_fields_no_version():
    num, ver = _derive_spec_fields("3GPP TS 29.244")
    assert num == "29.244"
    assert ver is None


def test_derive_spec_fields_long_dual_reference_form():
    """The ETSI-deliverable-plus-3GPP form repeats the same spec: a
    space-separated (non-dotted) ETSI deliverable number first, then the
    canonical dotted 3GPP form after ' -- '. Only the dotted, canonical
    (later) one should be extracted."""
    text = ("TS 145 008 (RTS/TSGG-0145008v860) version 8.6.0 -- "
            "3GPP TS 45.008 version 8.6.0")
    num, ver = _derive_spec_fields(text)
    assert num == "45.008"
    assert ver == "8.6.0"


def test_derive_spec_fields_empty_input():
    assert _derive_spec_fields(None) == (None, None)
    assert _derive_spec_fields("") == (None, None)
    assert _derive_spec_fields("no spec info here") == (None, None)


# ── End-to-end parse (requires openpyxl) ─────────────────────────────────────

def test_parse_reference_layout_end_to_end():
    if not HAVE_OPENPYXL:
        return
    rows = [
        REFERENCE_HEADER,
        [1, "ISLD-1", "Nokia", 10, "3GPP TS 38.331 version 16.5.0", "38.331",
         "16.5.0", "US1234567", "US2020123456A1", "A method", "Active",
         "5.2.1", 1, 0, 0, 0, 1],
    ]
    data = _make_xlsx(rows)
    result = parse_bulk_workbook("reference.xlsx", data)
    assert result["ok"]
    assert len(result["records"]) == 1
    rec = result["records"][0]
    assert rec["title"] == "A method"
    assert rec["legal_status"] == "Active"
    assert rec["spec_number"] == "38.331"
    assert rec["spec_version"] == "16.5.0"


def test_parse_uiux_layout_end_to_end():
    if not HAVE_OPENPYXL:
        return
    rows = [
        UIUX_HEADER,
        ["ISLD-2", 20, "HMD Global Oy", "3GPP TS 48.008 version 7.5.0",
         "US20070225676", "US2009197602A1", "Indication of Content",
         None, "3.2.3.14", 1, 1, 0, 0, 0],
    ]
    data = _make_xlsx(rows)
    result = parse_bulk_workbook("uiux.xlsx", data)
    assert result["ok"]
    assert len(result["records"]) == 1
    rec = result["records"][0]
    # Title landed in "title" despite the alternate header spelling.
    assert rec["title"] == "Indication of Content"
    # No dedicated spec columns -> derived from the free-text Standard field.
    assert rec["spec_number"] == "48.008"
    assert rec["spec_version"] == "7.5.0"
    # No IPRD_ID column present at all, and that's fine.
    assert "iprd_id" not in rec or rec.get("iprd_id") is None


def test_parse_novel_layout_end_to_end():
    """The flexibility guarantee, exercised through the full parse path
    (not just header resolution in isolation): a workbook using entirely
    different column names than either known export still parses into
    the same internal field set with correct values."""
    if not HAVE_OPENPYXL:
        return
    rows = [
        NOVEL_HEADER,
        ["ISLD-9999", "Acme Wireless Corp.", 501,
         "3GPP TS 38.331 version 17.1.0", "US99988877", "US2023112233A1",
         "Beamforming control method", "Active", "clause 6.3.1", 1],
    ]
    data = _make_xlsx(rows)
    result = parse_bulk_workbook("novel.xlsx", data)
    assert result["ok"]
    assert len(result["records"]) == 1
    rec = result["records"][0]
    assert rec["company"] == "Acme Wireless Corp."
    assert rec["iprd_reference"] == "ISLD-9999"
    assert rec["disclosure_number"] == 501
    assert rec["application_number"] == "US99988877"
    assert rec["publication_number"] == "US2023112233A1"
    assert rec["title"] == "Beamforming control method"
    assert rec["legal_status"] == "Active"
    assert rec["spec_number"] == "38.331"
    assert rec["spec_version"] == "17.1.0"


def test_dedicated_spec_columns_win_over_derivation():
    """When TGPP_NUMBER/TGPV_VERSION ARE present, derivation must not
    override them, even if they disagree with the free-text Standard
    column (e.g. a stale or differently-formatted Standard string)."""
    if not HAVE_OPENPYXL:
        return
    rows = [
        REFERENCE_HEADER,
        [1, "ISLD-1", "Nokia", 10, "3GPP TS 99.999 version 1.0.0", "38.331",
         "16.5.0", "US1234567", "US2020123456A1", "A method", "Active",
         "5.2.1", 1, 0, 0, 0, 1],
    ]
    data = _make_xlsx(rows)
    result = parse_bulk_workbook("reference.xlsx", data)
    rec = result["records"][0]
    assert rec["spec_number"] == "38.331"
    assert rec["spec_version"] == "16.5.0"


def test_blank_rows_skipped_but_rows_without_identifiers_preserved():
    """Source-row preservation: a fully blank row carries no data and does
    not become a record, but a row WITHOUT an application/publication
    number is still a real source row and must be kept (it used to be
    dropped as "unkeyable")."""
    if not HAVE_OPENPYXL:
        return
    rows = [
        UIUX_HEADER,
        ["ISLD-2", 20, "HMD", "3GPP TS 48.008 version 7.5.0", "US123", "PUB123",
         "Title A", None, "part", 1, 1, 0, 0, 0],
        [None] * len(UIUX_HEADER),  # fully blank
        ["ISLD-3", 21, "HMD", "3GPP TS 48.008 version 7.5.0", None, None,
         "Title B", None, "part", 1, 1, 0, 0, 0],  # no app/pub number -> still kept
    ]
    data = _make_xlsx(rows)
    result = parse_bulk_workbook("uiux.xlsx", data)
    assert result["ok"]
    assert [r["title"] for r in result["records"]] == ["Title A", "Title B"]
    assert [r["_source_row_index"] for r in result["records"]] == [0, 2]
    assert result["skipped_blank_rows"] == 1


def test_unrecognized_workbook_degrades_gracefully():
    if not HAVE_OPENPYXL:
        return
    rows = [UNRELATED_HEADER, [1, 2, 3, 4]]
    data = _make_xlsx(rows)
    result = parse_bulk_workbook("not_bulk.xlsx", data)
    assert result["ok"] is False
    assert result["records"] == []
    assert result["warnings"]


def test_single_declaration_workbook_not_misrouted_end_to_end():
    if not HAVE_OPENPYXL:
        return
    rows = [
        SINGLE_DECLARATION_HEADER,
        [1, "Nokia Technologies Oy", "202441060506", "US7817618 B2", "INDIA",
         "Method and devices", "TS 38.331"],
    ]
    data = _make_xlsx(rows)
    result = parse_bulk_workbook("single_decl.xlsx", data)
    assert result["ok"] is False


def test_summary_sheet_is_skipped_in_favor_of_data_sheet():
    """A bulk export can carry a non-data 'Summary' sheet alongside the
    real data sheet (as in Data_For_Testing.xlsx) — the parser must find
    and use the data sheet regardless of sheet order."""
    if not HAVE_OPENPYXL:
        return
    wb = openpyxl.Workbook()
    ws1 = wb.active
    ws1.title = "Summary"
    ws1.append(["Summary Type", "Count"])
    ws1.append(["Total Publications", 1])
    ws2 = wb.create_sheet("Sheet1")
    ws2.append(UIUX_HEADER)
    ws2.append(["ISLD-2", 20, "HMD", "3GPP TS 48.008 version 7.5.0", "US123",
                "PUB123", "Title A", None, "part", 1, 1, 0, 0, 0])
    buf = io.BytesIO()
    wb.save(buf)
    result = parse_bulk_workbook("with_summary.xlsx", buf.getvalue())
    assert result["ok"]
    assert result["sheet"] == "Sheet1"
    assert len(result["records"]) == 1


def test_merged_declaration_and_publication_number_cells_forward_fill():
    """Root-cause regression test for the 2025-08-26 fix: 'some declaration
    numbers aren't displayed' / 'some application and publication numbers
    aren't displayed'.

    Real ETSI/ISLD bulk exports commonly merge cells down several
    consecutive rows that share a value (e.g. one declaration covering
    several patent-family members). openpyxl only stores a merged cell's
    value in its top-left cell -- every other cell in the range used to
    read back as None (worse: the parser used read_only=True, which
    doesn't even expose merge info to compensate). This must now
    forward-fill every cell in a merged range instead of losing the data.
    """
    if not HAVE_OPENPYXL:
        return
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["IPRD_REFERENCE", "PATT_APPLICATION_NUMBER", "PUBL_NUMBER",
               "COMP_LEGAL_NAME", "Standard"])
    ws.append(["ISLD-2020-001", "US2007001", "US2009001A1", "Nokia", "3GPP TS 38.331"])
    ws.append([None, "US2007002", "US2009002A1", "Nokia", "3GPP TS 38.331"])
    ws.append([None, "US2007003", None, "Nokia", "3GPP TS 38.331"])
    ws.merge_cells("A2:A4")  # declaration reference merged across 3 rows
    ws.merge_cells("C2:C4")  # publication number ALSO merged
    buf = io.BytesIO()
    wb.save(buf)

    result = parse_bulk_workbook("merged.xlsx", buf.getvalue())
    assert result["ok"], result["warnings"]
    records = result["records"]
    assert len(records) == 3

    # Every row's declaration reference must be recovered from the merge,
    # not just the top row's.
    assert [r["iprd_reference"] for r in records] == ["ISLD-2020-001"] * 3
    # application_number was never merged/blank in this fixture — sanity
    # check it's still read correctly per-row (not accidentally forward-
    # filled when it shouldn't be).
    assert [r["application_number"] for r in records] == ["US2007001", "US2007002", "US2007003"]
    # publication_number was merged across all 3 rows.
    assert [r["publication_number"] for r in records] == ["US2009001A1"] * 3


def test_duplicate_alias_columns_first_nonblank_value_wins():
    """If a workbook (e.g. mid-migration to a new export format) carries
    two different header spellings that both resolve to the same field,
    a later BLANK column must not silently overwrite an earlier populated
    one — see _row_to_record's docstring."""
    header = ["IPRD_REFERENCE", "Declaration Reference", "PATT_APPLICATION_NUMBER",
              "COMP_LEGAL_NAME", "Standard", "PUBL_NUMBER"]
    row = ["ISLD-2020-099", None, "US2007099", "Nokia", "3GPP TS 38.331", "US2009099A1"]
    from etsi_bulk.parser import _row_to_record
    rec = _row_to_record(header, row)
    assert rec["iprd_reference"] == "ISLD-2020-099"


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(list(globals().items())):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception as e:
                failures += 1
                print(f"FAIL {name}: {e}")
    raise SystemExit(1 if failures else 0)
