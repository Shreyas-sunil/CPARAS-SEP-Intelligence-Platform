"""
Export safety helpers (CSV / XLSX downloads).

* Spreadsheet formula injection: a cell value taken from an uploaded file
  (a title, a company name) that starts with "=", "+", "-", "@" (or a
  tab/CR) is interpreted by Excel/LibreOffice as a formula when the CSV is
  opened. In XLSX, openpyxl itself turns any string starting with "=" into
  a formula. Both are neutralised here without changing what the user sees.
* Download filenames: the name is derived from the user's upload, so it may
  contain quotes, path separators or non-ASCII characters. Raw non-ASCII in
  a Content-Disposition header makes the response fail to encode (HTTP 500).
"""

from __future__ import annotations

import re
import unicodedata
from urllib.parse import quote

_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")
_NUMERIC_RE = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$")


def csv_safe(value):
    """Value for a CSV cell. Strings that Excel would evaluate as a formula
    are prefixed with a single quote (the OWASP-recommended mitigation);
    plain signed numbers such as "-5" or "+3.2" are left untouched."""
    if value is None:
        return ""
    if not isinstance(value, str):
        return value
    if value.startswith(_FORMULA_PREFIXES) and not _NUMERIC_RE.match(value):
        return "'" + value
    return value


def xlsx_set(cell, value) -> None:
    """Assign `value` to an openpyxl cell, forcing strings to be stored as
    text (openpyxl would otherwise store "=..." as a live formula)."""
    cell.value = value
    if isinstance(value, str) and value.startswith("="):
        cell.data_type = "s"


def content_disposition(filename: str, fallback: str = "export") -> str:
    """RFC 6266 / 5987 attachment header that is always encodable:
    an ASCII fallback plus the exact UTF-8 name."""
    name = (filename or fallback).replace("\\", "/").rsplit("/", 1)[-1].strip() or fallback
    name = re.sub(r"[\x00-\x1f\x7f\"]", "", name)
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    ascii_name = re.sub(r"[^A-Za-z0-9._ -]", "_", ascii_name).strip() or fallback
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name, safe='')}"
