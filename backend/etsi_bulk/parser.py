"""
ETSI Bulk Declaration Export — Parser
=======================================
Reads the workbook (.xlsx/.xls/.csv) into a list of normalized row dicts
keyed by the internal field names in `schema.COLUMN_MAP`.

Reuses the same openpyxl/xlrd readers as `utils/xlsx_parser.py` rather than
re-implementing workbook I/O, but applies its own header-matching (against
`schema.resolve_header` — exact-alias-first, fuzzy fallback for a header
spelling that's never been seen before — not the fuzzy single-declaration
matcher in utils/csv_parser.py) because this format has a fundamentally
different shape: one row per patent/standard pair, not one row per
disclosure within a single declarant's table. See schema.py's module
docstring for how header resolution works.
"""

from __future__ import annotations

import csv
import io
import logging
import re
from datetime import datetime, date

from etsi_bulk.schema import COLUMN_MAP, REQUIRED_COLUMNS, resolve_header

logger = logging.getLogger(__name__)

MAX_ROWS = 100_000  # generous headroom above the 15,000-20,000 row "large/extra
# large" ISLD bulk-export scale this was explicitly sized for; see
# parse_bulk_workbook's truncation warning below if a file actually hits it.


def _forward_fill_merged(rows: list[list], merged_ranges) -> list[list]:
    """Fix for merged cells reading as blank/None.

    openpyxl (and xlrd) only store a merged cell's value in its top-left
    cell; every other cell in the merged range comes back as None/blank
    when read directly. Real ETSI/ISLD bulk exports commonly merge cells
    in exactly this way for readability -- e.g. merging the declaration
    reference or a repeated application/publication number down several
    consecutive rows that share it -- which is what was causing "some
    declaration numbers aren't displayed" / "some application and
    publication numbers aren't displayed": the values were never missing
    from the source document, they were just unreadable from the merged
    non-top-left cells.

    This forward-fills every cell in a merged range with its top-left
    cell's value, in place, before header resolution ever sees the data --
    so the fix applies uniformly to every column, not just the two
    reported, and to any future column that gets merged the same way.

    `rows` is 0-indexed relative to the sheet (row 0 = the header row);
    `merged_ranges` is an iterable of objects/tuples with
    (min_row, max_row, min_col, max_col) in openpyxl's 1-indexed
    convention (min_row/min_col == top-left).
    """
    for rng in merged_ranges:
        min_row, max_row = rng.min_row, rng.max_row
        min_col, max_col = rng.min_col, rng.max_col
        top_row_idx = min_row - 1
        top_col_idx = min_col - 1
        if top_row_idx < 0 or top_row_idx >= len(rows):
            continue
        top_row = rows[top_row_idx]
        if top_col_idx >= len(top_row):
            continue
        fill_value = top_row[top_col_idx]
        if fill_value is None:
            continue  # nothing to propagate
        for r in range(min_row - 1, max_row):
            if r >= len(rows):
                break
            row = rows[r]
            for c in range(min_col - 1, max_col):
                if c >= len(row):
                    continue
                if row[c] is None:
                    row[c] = fill_value
    return rows

# Some export variants (e.g. the "UIUX database" export) have no dedicated
# TGPP_NUMBER/TGPV_VERSION columns at all — the spec number and version
# only ever appear embedded in the free-text `Standard` column, e.g.
# "3GPP TS 48.008 version 7.5.0" or, for the longer ETSI-deliverable-plus-
# 3GPP form, "TS 145 008 (RTS/TSGG-0145008v860) version 8.6.0 -- 3GPP TS
# 45.008 version 8.6.0". `_SPEC_PAIR_RE` matches a "<number> version
# <version>" pair; `_SPEC_NUM_RE` matches a bare dotted spec number when no
# version is stated. Both search left-to-right and the LAST match is used
# (via `findall`), because the longer form repeats the same spec as a
# non-dotted ETSI deliverable number first (e.g. "145 008", space- not
# dot-separated, so `_SPEC_NUM_RE` doesn't match it at all) followed by the
# canonical dotted "3GPP TS 45.008" form after " -- " — the dotted form
# occurring last is deliberately the one this reads as authoritative.
_SPEC_PAIR_RE = re.compile(r"(\d{1,3}\.\d{2,3})\s+version\s+(\d+(?:\.\d+){0,2})", re.IGNORECASE)
_SPEC_NUM_RE = re.compile(r"(\d{1,3}\.\d{2,3})")


def _derive_spec_fields(standard_raw) -> tuple[str | None, str | None]:
    """Best-effort (spec_number, spec_version) extracted from a free-text
    declared-standard string. Returns (None, None) if nothing recognizable
    is found — callers only use this to fill a gap, never to overwrite a
    value a dedicated column already supplied."""
    if not standard_raw:
        return None, None
    text = str(standard_raw)
    pairs = _SPEC_PAIR_RE.findall(text)
    if pairs:
        number, version = pairs[-1]
        return number, version
    nums = _SPEC_NUM_RE.findall(text)
    if nums:
        return nums[-1], None
    return None, None


def _cell_to_py(value):
    """Preserve native types where useful (dates, numbers); stringify otherwise."""
    if value is None:
        return None
    if isinstance(value, (datetime, date)):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def _read_xlsx_sheets(data: bytes) -> tuple[list[tuple[str, list[list]]], bool]:
    """Returns (sheets, truncated). `truncated` is True if any sheet had
    more data rows than MAX_ROWS and had to be cut off.

    Deliberately NOT using read_only=True here (unlike the original
    version of this function): openpyxl's read-only worksheets don't
    expose `merged_cells` at all, and every non-top-left cell in a merged
    range reads back as None in that mode -- see `_forward_fill_merged`'s
    docstring for why that's a real, reproduced-and-confirmed data-loss
    bug for exactly the kind of columns (declaration/application/
    publication numbers) ETSI/ISLD bulk exports commonly merge. The
    non-read-only load is measurably slower (~4s vs ~2.5s measured on a
    synthetic 20,000-row workbook) but still comfortably fast at the
    15,000-20,000 row scale this is sized for, and this is a one-time
    ingestion step, not a hot path.
    """
    import openpyxl

    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=False, data_only=True, keep_links=False)
    try:
        sheets = []
        truncated = False
        for ws in wb.worksheets:
            rows = []
            for i, row in enumerate(ws.iter_rows(values_only=True)):
                if i > MAX_ROWS:
                    truncated = True
                    break
                rows.append([_cell_to_py(c) for c in row])
            rows = _forward_fill_merged(rows, ws.merged_cells.ranges)
            sheets.append((ws.title, rows))
        return sheets, truncated
    finally:
        wb.close()


def _read_xls_sheets(data: bytes) -> tuple[list[tuple[str, list[list]]], bool]:
    """Legacy .xls reader. xlrd exposes merged ranges via `ws.merged_cells`
    as (row_lo, row_hi, col_lo, col_hi) 0-indexed, half-open tuples --
    different indexing convention from openpyxl, so this uses its own
    forward-fill loop rather than reusing `_forward_fill_merged` (which
    expects openpyxl's 1-indexed, inclusive MergedCellRange objects)."""
    import xlrd

    wb = xlrd.open_workbook(file_contents=data)
    sheets = []
    truncated = False
    for si in range(wb.nsheets):
        ws = wb.sheet_by_index(si)
        n_rows = min(ws.nrows, MAX_ROWS + 1)
        if ws.nrows > MAX_ROWS + 1:
            truncated = True
        rows = [[_cell_to_py(ws.cell_value(r, c)) for c in range(ws.ncols)] for r in range(n_rows)]
        for row_lo, row_hi, col_lo, col_hi in getattr(ws, "merged_cells", []):
            if row_lo >= len(rows) or col_lo >= len(rows[row_lo]):
                continue
            fill_value = rows[row_lo][col_lo]
            if fill_value is None:
                continue
            for r in range(row_lo, min(row_hi, len(rows))):
                row = rows[r]
                for c in range(col_lo, min(col_hi, len(row))):
                    if row[c] is None:
                        row[c] = fill_value
        sheets.append((ws.name, rows))
    return sheets, truncated


def _read_csv_rows(data: bytes) -> tuple[list[tuple[str, list[list]]], bool]:
    """Returns (sheets, truncated), same contract as the workbook readers.

    Previously this hard-coded `utf-8-sig` with `errors="replace"` and a
    comma delimiter, so two perfectly valid real-world exports were
    rejected outright with "Unrecognized file format":
      * a semicolon- or tab-separated CSV (what Excel writes on a machine
        whose list separator is not a comma, and what several ETSI
        exports use) parsed as ONE column, so no header was recognised;
      * a cp1252/UTF-16 file decoded with replacement characters, which
        corrupts company names and can break header matching.
    Both are handled by this product's own `utils/csv_parser` helpers,
    which the single-declaration path already used -- so this now reuses
    them instead of a second, weaker implementation. The MAX_ROWS ceiling
    the workbook readers apply is applied here too; a CSV used to be read
    without any ceiling at all.
    """
    from utils.csv_parser import _decode, _sniff_delimiter

    text, _enc = _decode(data)
    delimiter = _sniff_delimiter(text)
    rows: list[list] = []
    truncated = False
    for i, row in enumerate(csv.reader(io.StringIO(text), delimiter=delimiter)):
        if i > MAX_ROWS:
            truncated = True
            break
        rows.append(row)
    return [("Sheet1", rows)], truncated


# A header row must resolve at least one column into each of these
# "semantic buckets" to be treated as a bulk declaration-export sheet:
#   - an identifier a row's patent can be keyed by
#   - who declared it
#   - what standard it's declared against
# This is deliberately about MEANING, not literal column names, so a
# workbook that spells every one of these columns differently from both
# known real exports (e.g. "Applicant Name" / "Declared Standard" /
# "App No.") is still recognized — see schema.resolve_header(). It's also
# deliberately a 3-signal requirement (not just "has an application
# number column"), so a generic patent list or the single-declaration
# format's own header (handled by utils/csv_parser.py instead) doesn't
# get misrouted here.
_IDENTIFIER_FIELDS = {"application_number", "publication_number"}
_DECLARANT_FIELDS = {"company", "disclosure_number", "iprd_reference"}
_STANDARD_FIELDS = {"standard_raw", "spec_number"}

# NOTE: identifier columns are used ONLY to recognise the sheet's shape
# (see `looks_like_bulk_header`). They are never used to key, merge,
# skip or deduplicate individual rows -- see `parse_bulk_workbook`.


def _resolved_fields(header_row: list) -> set[str]:
    """Canonical field names recognised in a candidate header row (used to decide which row is the real
    header).
    """
    if not header_row:
        return set()
    resolved = set()
    for h in header_row:
        field = resolve_header(h)
        if field:
            resolved.add(field)
    return resolved


def looks_like_bulk_header(header_row: list) -> bool:
    """True if a header row semantically matches the ETSI bulk-export
    shape: at least one recognizable identifier column, one declarant
    column, and one standard column (see the field-bucket comment above).
    Column names are resolved via `schema.resolve_header` — exact-alias
    match first, then a conservative fuzzy fallback — so this isn't tied
    to the literal spelling of any one known export."""
    resolved = _resolved_fields(header_row)
    if not resolved:
        return False
    return bool(
        resolved & _IDENTIFIER_FIELDS
        and resolved & _DECLARANT_FIELDS
        and resolved & _STANDARD_FIELDS
    )


# A bulk export's header row is not always the first row of the sheet: real
# exports regularly carry a title/notes block ("ETSI IPR declarations
# export", a generated-on date, a blank spacer) above it. Only row 1 used to
# be tried, so such a file was rejected outright with "Unrecognized file
# format" even though every data row in it was perfectly readable. The same
# scan depth the categoriser uses for the same reason.
_HEADER_SCAN_ROWS = 15


def _find_header_row(rows: list[list]) -> int | None:
    """Index of the first row (within the first `_HEADER_SCAN_ROWS`) that
    semantically matches the bulk-export header shape, or None."""
    for i, row in enumerate(rows[:_HEADER_SCAN_ROWS]):
        if row and looks_like_bulk_header(row):
            return i
    return None


def _pick_sheet(sheets: list[tuple[str, list[list]]]) -> tuple[str, list[list], int] | None:
    """(sheet name, rows, header row index) for the first sheet whose
    header is recognisable, scanning past a leading title block."""
    for name, rows in sheets:
        if not rows:
            continue
        idx = _find_header_row(rows)
        if idx is not None:
            return name, rows, idx
    return None


def _row_to_record(header: list[str], row: list) -> dict:
    """Map a raw row to internal field names via the header.

    If two different header spellings in the same sheet resolve to the
    same internal field (e.g. a legacy export carrying both the old and
    new column name side by side during a transition), a later blank
    column must not silently clobber an earlier populated one -- so this
    keeps the first non-empty value seen for each field rather than
    always taking whichever column comes last.
    """
    rec: dict = {}
    for i, col in enumerate(header):
        if col is None or col == "":
            continue
        field = resolve_header(col)
        if not field:
            continue
        val = row[i] if i < len(row) else None
        if val is None or val == "":
            if field not in rec:
                rec[field] = val
            continue
        rec[field] = val
    return rec


def detect(filename: str, data: bytes) -> bool:
    """Cheap pre-check used by the upload endpoint to route to this parser."""
    try:
        sheets, _truncated = _load_any(filename, data)
    except Exception:
        return False
    return _pick_sheet(sheets) is not None


def _load_any(filename: str, data: bytes) -> tuple[list[tuple[str, list[list]]], bool]:
    """Returns (sheets, truncated) -- see _read_xlsx_sheets/_read_xls_sheets."""
    name = (filename or "").lower()
    head = data[:8]
    if head.startswith(b"PK\x03\x04") or name.endswith((".xlsx", ".xlsm")):
        return _read_xlsx_sheets(data)
    if head.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1") or name.endswith(".xls"):
        return _read_xls_sheets(data)
    return _read_csv_rows(data)


def parse_bulk_workbook(filename: str, data: bytes) -> dict:
    """
    Returns {"ok": bool, "sheet": str, "records": list[dict], "warnings": list[str]}.

    Every sheet is scanned (a bulk export can carry a "Summary" sheet ahead
    of/after the data sheet, as in the reference file); the first sheet
    whose header semantically matches the bulk-export shape wins (see
    `looks_like_bulk_header`).
    """
    sheets, truncated = _load_any(filename, data)
    warnings: list[str] = []
    if truncated:
        warnings.append(
            f"This file has more than {MAX_ROWS:,} data rows in at least one "
            f"sheet; only the first {MAX_ROWS:,} rows were processed. Split "
            f"the file or contact support if you need the full set ingested."
        )

    picked = _pick_sheet(sheets)
    if picked is None:
        return {
            "ok": False, "sheet": None, "records": [],
            "warnings": warnings + [
                "No sheet's column headers could be recognized as an ETSI "
                "bulk-export declaration table (needs at least one "
                "identifier column such as an application/publication "
                "number, one declarant column such as a company or "
                "disclosure number, and one standard column). This file "
                "may need the single-declaration ingestion path "
                "(/api/analysis/analyze) instead."
            ],
        }

    sheet_name, rows, header_idx = picked
    header = [str(h).strip() if h is not None else "" for h in rows[header_idx]]
    if header_idx:
        warnings.append(
            f'Header row found on row {header_idx + 1} of sheet "{sheet_name}"; the '
            f'{header_idx} row(s) above it were treated as a title block.'
        )
    records = []
    blank_rows = 0
    # SOURCE-ROW PRESERVATION: every non-blank data row in the source sheet
    # becomes exactly one record, in source order, regardless of whether it
    # carries an application/publication/disclosure number and regardless
    # of whether its identifiers repeat elsewhere in the file. Rows are
    # never keyed, merged or discarded by identifier -- each source row is
    # authoritative. `_source_row_index` is the row's 0-based position
    # among the sheet's data rows (the rows below the header); it is persisted with the
    # record and is what the enriched Excel export uses to put each row's
    # enrichment back onto the exact source row it came from.
    #
    # Only rows with NO cell content at all are not turned into records
    # (there is no data in them to enrich). They are not deleted from
    # anything: the enriched export re-emits the original sheet, so such
    # rows keep their original position there, just without enrichment.
    for data_idx, row in enumerate(rows[header_idx + 1:]):
        if row is None or all(c in (None, "") for c in row):
            blank_rows += 1
            continue
        rec = _row_to_record(header, row)
        rec["_source_row_index"] = data_idx
        # Fill spec_number/spec_version from the declared-standard free
        # text only when the workbook had no dedicated TGPP_NUMBER/
        # TGPV_VERSION columns to supply them — never overrides an actual
        # column value. See `_derive_spec_fields` docstring.
        if not rec.get("spec_number") and not rec.get("spec_version"):
            derived_number, derived_version = _derive_spec_fields(rec.get("standard_raw"))
            if derived_number:
                rec["spec_number"] = derived_number
            if derived_version:
                rec["spec_version"] = derived_version
        records.append(rec)

    return {
        "ok": True, "sheet": sheet_name, "records": records, "warnings": warnings,
        "skipped_blank_rows": blank_rows,
        # Kept for API-response compatibility; rows are no longer dropped
        # for lacking an identifier, so this is always 0.
        "skipped_unkeyable_rows": 0,
    }
