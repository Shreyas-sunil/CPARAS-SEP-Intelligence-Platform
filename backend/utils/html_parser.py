"""
HTML Ingestion
==============
Accepts ETSI IPR annex data supplied as an HTML page (a saved "export to
web page" from a spreadsheet, a scraped web table, or an HTML rendering of
the annex) instead of a PDF, CSV or Excel file.

Like `utils.xlsx_parser`, this is a format adapter rather than a second
parsing engine. An HTML `<table>` is structurally identical to a CSV/XLSX
grid once its rows and cells are pulled out, so the same header-mapping
engine in `utils.csv_parser.parse_rows_declaration` is reused here too —
fuzzy header matching, family-column demotion, boilerplate filtering and
continuation-row handling stay in exactly one place regardless of which of
the three tabular formats a declaration arrives as.

HTML differs from CSV/XLSX in one respect this module has to handle: a page
can contain more than one `<table>` (nav chrome, a title block, the real
annex table, a footer). Every table on the page is parsed and scored the
same way `csv_parser._find_header` scores header candidates within a single
table; the table that maps the most patent rows wins. A page with no
usable table (or no `<table>` at all — e.g. the annex was exported as plain
paragraphs) falls back to the page's flattened visible text, exactly like
an unrecognised CSV falls back to LLM extraction over its rendered text.
"""

from __future__ import annotations

import logging
import re

from bs4 import BeautifulSoup

from utils.csv_parser import parse_rows_declaration

logger = logging.getLogger(__name__)

# Mirrors utils.csv_parser.MAX_CSV_ROWS.
MAX_HTML_ROWS = 20000

_WHITESPACE_RE = re.compile(r"\s+")


# ── Detection ─────────────────────────────────────────────────────────────────

def looks_like_html(filename: str | None, head: bytes) -> bool:
    """
    Decide whether an upload should take the HTML path.

    Extension is the primary signal, as with the other format detectors.
    For an extensionless upload (or one mislabeled with a generic
    extension), a case-insensitive scan of the first bytes for `<html`,
    `<!doctype html` or an opening `<table` catches the common cases
    without risking a false positive on a PDF or CSV, neither of which
    starts with a `<`.
    """
    name = (filename or "").lower().strip()
    if name.endswith((".html", ".htm", ".xhtml")):
        return True

    sample = head[:2048].lstrip()
    if not sample.startswith(b"<"):
        return False
    lowered = sample[:512].lower()
    return (
        b"<!doctype html" in lowered
        or b"<html" in lowered
        or b"<table" in lowered
    )


# ── Cell / table extraction ────────────────────────────────────────────────────

def _cell_text(cell) -> str:
    """
    Flatten one <td>/<th>'s text, collapsing internal whitespace the way a
    human transcribing the visible page would — HTML's own whitespace is
    not significant, so a cell wrapped across several lines in the markup
    must not become a multi-line cell value.
    """
    text = cell.get_text(separator=" ", strip=True)
    return _WHITESPACE_RE.sub(" ", text).strip()


def _table_to_rows(table) -> list[list[str]]:
    """
    Convert one BeautifulSoup <table> into list[list[str]].

    rowspan/colspan are not expanded — a spanned cell is read once, at its
    origin row, and the rows it visually covers are left short rather than
    padded with a repeated value. This is a deliberate simplification (real
    ETSI annex exports are not merged-cell tables); `parse_rows_declaration`
    already tolerates ragged rows via its `idx < len(cells)` guards, so a
    rare spanned cell degrades to a missing value rather than a crash.
    """
    rows: list[list[str]] = []
    trs = table.find_all("tr")
    for tr in trs[: MAX_HTML_ROWS + 1]:
        cells = tr.find_all(["td", "th"])
        if not cells:
            continue
        rows.append([_cell_text(c) for c in cells])
    return rows


# ── Main entry point ────────────────────────────────────────────────────────

def parse_html_declaration(data: bytes, filename: str = "upload.html") -> dict:
    """
    Parse HTML bytes into the slim extraction schema.

    Same {"ok", "slim", "text", "meta"} return shape as
    `csv_parser.parse_csv_declaration` and `xlsx_parser.parse_xlsx_declaration`
    — see the former's docstring for field semantics.

    Every `<table>` on the page is tried; the one whose header row maps the
    most patent fields wins (see this module's docstring). If no table
    maps usefully — including pages with no `<table>` at all — this
    returns ok=False with the page's flattened visible text, so the caller
    falls back to LLM extraction exactly as it would for an unrecognised
    CSV.

    Never raises: a malformed/truncated HTML document is still something
    BeautifulSoup's lenient parsers can extract *something* from, and a
    genuinely empty result (no text at all) is reported as a warning
    rather than an exception.
    """
    try:
        soup = BeautifulSoup(data, "lxml")
    except Exception:
        # lxml is unavailable or the document confused it; html.parser
        # ships with the stdlib and is far more forgiving of broken markup.
        soup = BeautifulSoup(data, "html.parser")

    # Script/style text is never part of the declaration's content and
    # would otherwise leak into both the table cells (if a table happens to
    # contain one) and the flattened-text fallback.
    for tag in soup(["script", "style"]):
        tag.decompose()

    tables = soup.find_all("table")
    best: dict | None = None
    best_score = -1
    for i, table in enumerate(tables):
        rows = _table_to_rows(table)
        if not rows:
            continue
        result = parse_rows_declaration(
            rows, filename, base_meta={"format": "html", "table_index": i, "tables_found": len(tables)}
        )
        score = result["meta"].get("rows_parsed", 0) if result.get("ok") else 0
        if score > best_score:
            best, best_score = result, score

    if best is not None and best.get("ok"):
        return best

    # No table mapped usefully (or there was no table at all) — fall back
    # to the page's visible text, the same degrade-gracefully contract
    # `parse_csv_declaration` and `parse_xlsx_declaration` offer.
    text = _WHITESPACE_RE.sub(" ", soup.get_text(separator="\n", strip=True)).strip()
    warnings = []
    if tables:
        warnings.append(
            f"Found {len(tables)} table(s) but none mapped enough recognised "
            "columns (an application number, publication number or title "
            "column is required). Falling back to language-model extraction "
            "over the page's visible text."
        )
    else:
        warnings.append(
            "No table found in this HTML file. Falling back to language-model "
            "extraction over the page's visible text."
        )
    if not text:
        warnings.append("No visible text could be extracted from this file either.")

    return {
        "ok": False,
        "slim": {"declarant": "", "patents": []},
        "text": text,
        "meta": {"format": "html", "tables_found": len(tables), "warnings": warnings},
    }
