"""
Tests for utils/xlsx_parser.

Run: python -m pytest backend/tests/test_xlsx_parser.py -q
 or: python3 backend/tests/_run_all.py

These target the format-adapter contract specifically: that reading a
workbook's cells and handing them to `csv_parser.parse_rows_declaration`
produces the same shape (and the same field mapping) a CSV of the same
data would, and that format detection and failure modes behave the way
`csv_parser`'s already do for CSV.
"""
import io
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from utils.xlsx_parser import (  # noqa: E402
    looks_like_xlsx,
    parse_xlsx_declaration,
    _cell_to_str,
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


HEADER = [
    "Disclosure Number", "Applicant/holder", "Application No.",
    "Publication No.", "Country of registration", "Patent/Application Title",
    "Work Item or Standard No.",
]

ROWS = [
    HEADER,
    [1, "Nokia Technologies Oy", "202441060506", "US7817618 B2", "INDIA",
     "Method and devices", "TS 38.331"],
    [2, "Nokia Technologies Oy", "202441059993", "", "INDIA",
     "Another method", "TS 38.214"],
]


# ── Detection ─────────────────────────────────────────────────────────────────

def test_looks_like_xlsx_by_extension_and_zip_magic():
    zip_head = b"PK\x03\x04" + b"\x00" * 4
    assert looks_like_xlsx("annex.xlsx", zip_head) == "xlsx"
    assert looks_like_xlsx("annex.xlsm", zip_head) == "xlsx"


def test_looks_like_xlsx_by_extension_and_ole2_magic():
    ole_head = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 4
    assert looks_like_xlsx("annex.xls", ole_head) == "xls"


def test_looks_like_xlsx_content_wins_over_mismatched_extension():
    """A .xls-named file that is actually zip-based (Excel re-saved it as
    .xlsx but kept the old name) should still be read as xlsx, not fail."""
    zip_head = b"PK\x03\x04" + b"\x00" * 4
    assert looks_like_xlsx("annex.xls", zip_head) == "xlsx"


def test_looks_like_xlsx_rejects_pdf_and_csv():
    assert looks_like_xlsx("annex.pdf", b"%PDF-1.4") is None
    assert looks_like_xlsx("annex.csv", b"a,b,c\n1,2,3") is None


def test_looks_like_xlsx_extensionless_ole2_content_sniff():
    ole_head = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 4
    assert looks_like_xlsx(None, ole_head) == "xls"


# ── Cell rendering ──────────────────────────────────────────────────────────────

def test_cell_to_str_integral_float_has_no_trailing_zero():
    """An application number Excel stored as a plain number (202441060506)
    comes back from openpyxl as a float; it must not gain a '.0'."""
    assert _cell_to_str(202441060506.0) == "202441060506"


def test_cell_to_str_non_integral_float_keeps_precision():
    assert _cell_to_str(15.13) == "15.13"


def test_cell_to_str_none_and_bool():
    assert _cell_to_str(None) == ""
    assert _cell_to_str(True) == "TRUE"
    assert _cell_to_str(False) == "FALSE"


# ── End-to-end parsing (requires openpyxl) ──────────────────────────────────

def test_parse_xlsx_maps_columns_like_csv_would():
    if not HAVE_OPENPYXL:
        return  # environment without openpyxl installed; covered by CI elsewhere
    data = _make_xlsx(ROWS)
    result = parse_xlsx_declaration(data, "annex.xlsx")
    assert result["ok"] is True
    assert result["slim"]["declarant"] == "Nokia Technologies Oy"
    patents = result["slim"]["patents"]
    assert len(patents) == 2
    assert patents[0]["app"] == "202441060506"
    assert patents[0]["pub"] == "US7817618 B2"
    assert patents[0]["std"] == ["TS 38.331"]
    assert result["meta"]["format"] == "xlsx"
    assert result["meta"]["rows_parsed"] == 2


def test_parse_xlsx_numeric_application_number_not_corrupted():
    """The whole point of the float-rendering helper: a workbook where
    Excel auto-typed the application number column as Number must not
    silently turn '202441060506' into '202441060506.0'."""
    if not HAVE_OPENPYXL:
        return
    data = _make_xlsx(ROWS)
    result = parse_xlsx_declaration(data, "annex.xlsx")
    assert ".0" not in result["slim"]["patents"][0]["app"]


def test_parse_xlsx_unrecognised_columns_falls_back_gracefully():
    if not HAVE_OPENPYXL:
        return
    rows = [["Foo", "Bar", "Baz"], ["x", "y", "z"]]
    data = _make_xlsx(rows)
    result = parse_xlsx_declaration(data, "junk.xlsx")
    assert result["ok"] is False
    assert result["slim"]["patents"] == []
    assert result["meta"]["warnings"]


def test_parse_xlsx_empty_workbook():
    if not HAVE_OPENPYXL:
        return
    wb = openpyxl.Workbook()
    buf = io.BytesIO()
    wb.save(buf)
    result = parse_xlsx_declaration(buf.getvalue(), "empty.xlsx")
    # An empty sheet still has at least the default blank row structure in
    # some openpyxl versions, or none at all — either way this must not ok.
    assert result["ok"] is False


def test_parse_xlsx_corrupt_bytes_do_not_raise():
    result = parse_xlsx_declaration(b"not a real workbook", "corrupt.xlsx")
    assert result["ok"] is False
    assert result["slim"] == {"declarant": "", "patents": []}
    assert result["meta"]["warnings"]
