"""
XLSX / XLS (Excel) Ingestion
============================
Accepts ETSI IPR annex data supplied as an Excel workbook instead of a PDF
or a CSV/TSV export. This is a thin format adapter, not a second parsing
engine: once a workbook's cells are read into `list[list[str]]`, everything
from header discovery onward — fuzzy header matching, family-column
demotion, boilerplate filtering, continuation-row handling — is the exact
same code path a CSV upload takes (`utils.csv_parser.parse_rows_declaration`).
That sharing matters here specifically because the real-world source for
both formats is usually the same ETSI annex spreadsheet; a user exporting
it as .xlsx instead of .csv should not get a different (and possibly worse)
result just because of that choice.

Two workbook formats are supported:
  * .xlsx / .xlsm — read via openpyxl (Office Open XML, a zip container).
  * .xls          — the legacy binary format, read via xlrd. xlrd 2.x
                    dropped .xlsx support entirely, which is exactly why
                    both libraries are needed rather than one.

Every worksheet is read, not just the first. Real-world declaration
workbooks routinely put a cover/instructions sheet ahead of the actual
disclosure table, so assuming sheet order matches data order was wrong —
this instead scores each sheet's header the same way `utils.html_parser`
scores each `<table>` on an HTML page, and uses whichever sheet maps the
most patent rows. A workbook with no sheet that maps usefully falls back
to a text rendering of its largest sheet, the same degrade-gracefully
contract every other tabular format in this codebase has.
"""

from __future__ import annotations

import io
import logging

from utils.csv_parser import parse_rows_declaration, render_rows_as_text

logger = logging.getLogger(__name__)

# Mirrors utils.csv_parser.MAX_CSV_ROWS — a pathological workbook shouldn't
# turn into an unbounded in-memory grid either.
MAX_XLSX_ROWS = 20000

# Office Open XML (.xlsx, .xlsm, .docx, .pptx, ...) is a zip container.
_ZIP_MAGIC = b"PK\x03\x04"
# Legacy OLE2 Compound File Binary Format — covers .xls (and .doc, .ppt).
_OLE2_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


# ── Detection ─────────────────────────────────────────────────────────────────

def looks_like_xlsx(filename: str | None, head: bytes) -> str | None:
    """
    Decide whether an upload should take the Excel path, and which variant.

    Returns "xlsx" for Office Open XML workbooks, "xls" for the legacy
    binary format, or None if this isn't an Excel file.

    As with `csv_parser.looks_like_csv`, extension is the primary signal
    (it's what the user controls) but content is authoritative where the
    two disagree: an .xls-named file with the zip magic is really an .xlsx
    (and vice versa), and a plain zip file that merely happens to be named
    .xlsx would fail to open as a workbook downstream anyway — that failure
    is handled by the caller falling back to LLM extraction, not by
    over-fitting the detection here.
    """
    name = (filename or "").lower().strip()
    has_ole2 = head.startswith(_OLE2_MAGIC)
    has_zip = head.startswith(_ZIP_MAGIC)

    if name.endswith((".xlsx", ".xlsm")):
        return "xls" if has_ole2 else "xlsx"
    if name.endswith(".xls"):
        return "xlsx" if has_zip else "xls"
    if has_ole2:
        return "xls"
    if has_zip and name.endswith((".xltx", ".xltm")):
        return "xlsx"
    return None


# ── Cell reading ──────────────────────────────────────────────────────────────

def _cell_to_str(value) -> str:
    """
    Render one cell's Python value as the string `parse_rows_declaration`
    expects, matching how a human reading the sheet would transcribe it.

    Floats that are integral (e.g. an application number Excel stored as a
    number) are rendered without a trailing ".0" — the single most common
    way a numeric-looking identifier column would otherwise get silently
    corrupted on the way through openpyxl/xlrd, which return Python floats
    for anything typed as a plain number.
    """
    if value is None:
        return ""
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        return repr(value)
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    return str(value)


def _read_xlsx_sheets(data: bytes) -> list[tuple[str, list[list[str]]]]:
    """Read every worksheet of an .xlsx/.xlsm workbook. Returns [(sheet_name, rows), ...]."""
    import openpyxl  # local import: keeps this an optional dependency at module load

    wb = openpyxl.load_workbook(
        io.BytesIO(data), read_only=True, data_only=True, keep_links=False
    )
    try:
        sheets: list[tuple[str, list[list[str]]]] = []
        for ws in wb.worksheets:
            rows: list[list[str]] = []
            for i, row in enumerate(ws.iter_rows(values_only=True)):
                if i > MAX_XLSX_ROWS:
                    break
                rows.append([_cell_to_str(c) for c in row])
            sheets.append((ws.title, rows))
        return sheets
    finally:
        wb.close()


def _read_xls_sheets(data: bytes) -> list[tuple[str, list[list[str]]]]:
    """Read every worksheet of a legacy .xls workbook. Returns [(sheet_name, rows), ...]."""
    import xlrd  # local import: keeps this an optional dependency at module load

    wb = xlrd.open_workbook(file_contents=data)
    sheets: list[tuple[str, list[list[str]]]] = []
    for si in range(wb.nsheets):
        ws = wb.sheet_by_index(si)
        n_rows = min(ws.nrows, MAX_XLSX_ROWS + 1)
        rows = [
            [_cell_to_str(ws.cell_value(r, c)) for c in range(ws.ncols)]
            for r in range(n_rows)
        ]
        sheets.append((ws.name, rows))
    return sheets


# ── Main entry point ────────────────────────────────────────────────────────

def parse_xlsx_declaration(data: bytes, filename: str = "upload.xlsx") -> dict:
    """
    Parse Excel workbook bytes into the slim extraction schema.

    Same {"ok", "slim", "text", "meta"} return shape as
    `csv_parser.parse_csv_declaration` — see that function's docstring for
    field semantics. Callers should treat the two interchangeably; the only
    difference is where the rows came from.

    Every sheet is tried (see this module's docstring); the one whose
    header row maps the most patent fields wins, exactly like
    `html_parser.parse_html_declaration` picks the best `<table>` on a
    page. If no sheet maps usefully, this returns ok=False with a text
    rendering of the largest sheet, so the caller falls back to LLM
    extraction exactly as it would for an unrecognised CSV.

    Never raises on a workbook it can't open: openpyxl/xlrd failures (a
    corrupt file, a password-protected workbook, a format neither library
    recognises) come back as ok=False with an explanatory warning, so a bad
    upload degrades to "the caller falls back to LLM extraction over
    whatever text we could recover" rather than a 500.
    """
    variant = looks_like_xlsx(filename, data[:8]) or "xlsx"
    reader = _read_xlsx_sheets if variant == "xlsx" else _read_xls_sheets

    try:
        sheets = reader(data)
    except Exception as e:
        # A .xlsx-named file with .xls content (or vice versa) is exactly
        # the case `looks_like_xlsx` tries to resolve via magic bytes, but
        # a mislabeled/corrupt file can still slip through — if the first
        # attempt fails, try the other reader once before giving up.
        other_variant = "xls" if variant == "xlsx" else "xlsx"
        other_reader = _read_xls_sheets if other_variant == "xls" else _read_xlsx_sheets
        try:
            sheets = other_reader(data)
            variant = other_variant
        except Exception:
            logger.warning(f"[{filename}] Excel read failed: {e}")
            return {
                "ok": False,
                "slim": {"declarant": "", "patents": []},
                "text": "",
                "meta": {
                    "format": variant,
                    "warnings": [
                        f"Could not read '{filename}' as an Excel workbook ({e}). "
                        "The file may be corrupt, password-protected, or not a "
                        "spreadsheet at all."
                    ],
                },
            }

    if not sheets:
        return {
            "ok": False,
            "slim": {"declarant": "", "patents": []},
            "text": "",
            "meta": {"format": variant, "warnings": ["Workbook has no sheets."]},
        }

    best: dict | None = None
    best_score = -1
    for sheet_name, rows in sheets:
        if not rows:
            continue
        result = parse_rows_declaration(
            rows, filename,
            base_meta={"format": variant, "sheet": sheet_name, "sheets_found": len(sheets)},
        )
        score = result["meta"].get("rows_parsed", 0) if result.get("ok") else 0
        if score > best_score:
            best, best_score = result, score

    if best is not None and best.get("ok"):
        return best

    # No sheet mapped usefully — fall back to a text rendering of the
    # largest sheet (by row count) for LLM extraction, the same
    # degrade-gracefully contract `parse_csv_declaration` and
    # `parse_html_declaration` offer.
    largest_name, largest_rows = max(sheets, key=lambda s: len(s[1]))
    text = render_rows_as_text([], largest_rows) if largest_rows else ""

    warnings = []
    if len(sheets) > 1:
        warnings.append(
            f"Found {len(sheets)} sheet(s) but none mapped enough recognised "
            "columns (an application number, publication number or title "
            "column is required). Falling back to language-model extraction "
            f"over the '{largest_name}' sheet's contents."
        )
    else:
        warnings.append(
            "Could not identify the expected columns (an application number, "
            "publication number or title column is required). Falling back "
            "to language-model extraction over the raw file contents."
        )
    if not text:
        warnings.append("No content could be extracted from this workbook either.")

    return {
        "ok": False,
        "slim": {"declarant": "", "patents": []},
        "text": text,
        "meta": {
            "format": variant, "sheets_found": len(sheets), "sheet": largest_name,
            "warnings": warnings,
        },
    }
