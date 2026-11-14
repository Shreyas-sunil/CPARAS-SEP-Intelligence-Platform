"""
CSV / Spreadsheet-Export Ingestion
===================================
Accepts ETSI IPR annex data supplied as a CSV instead of a PDF.

Why this is a separate path, not "just another extractor"
---------------------------------------------------------
A PDF annex has to be OCR'd and then read by an LLM because the table
structure is destroyed on the way in — that whole pipeline (tiered OCR,
confidence gating, row-safe chunking, text repair, per-field verification
against the source text) exists to recover structure that was lost.

A CSV has not lost its structure. Running one through the LLM extraction
prompt would be strictly worse: slower, non-deterministic, and capable of
inventing values that were sitting right there in a column. So a recognised
CSV bypasses extraction entirely and is mapped straight into the same slim
schema the LLM would have produced ({"declarant", "patents": [...]}),
after which every downstream stage — slim_to_full(), continuation-row
merging, deep enrichment, verification, research augmentation, persistence
— runs completely unchanged.

"No particular schema, but the column names are usually identical"
------------------------------------------------------------------
That is the actual contract this module implements, and it is the reason
header mapping is fuzzy rather than a fixed lookup:

  * Header text is normalised (case, punctuation, whitespace, Unicode
    dashes/NBSP) before matching, so "Application No.", "APPLICATION NO",
    "Application_Number" and "application  no" all land on the same field.
  * Matching falls back from exact-alias to token-overlap scoring, so
    near-miss headers ("Applicant / holder name") still map.
  * The header row is *found*, not assumed to be row 1 — spreadsheet
    exports of the ETSI annex routinely carry one or more title/banner
    rows above it, and the real header is itself often split across two
    physical rows ("STANDARD, TECHNICAL SPECIFICATION or ETSI Work Item"
    banner above "Work Item or Standard No."). Both single- and split-row
    headers are handled by scoring each candidate.
  * Duplicate columns are disambiguated positionally. The ETSI annex has
    "Application No.", "Publication No." and "Country of registration"
    TWICE — once for the disclosure, once under "FURTHER INFORMATION /
    Other members of this PATENT FAMILY". First occurrence wins the real
    field; later occurrences become family_* and are kept as metadata
    rather than silently overwriting the disclosure's own numbers, which
    is the single most damaging way this could fail quietly.

Anything genuinely unrecognisable is not forced. If the header scan can't
find enough identifiable columns, parse_csv_declaration() returns
ok=False along with a flattened text rendering of the file, and the caller
falls back to the normal LLM extraction path — a weird CSV still gets
analysed, just not deterministically.
"""

from __future__ import annotations

import csv
import io
import logging
import re
import unicodedata
from collections import Counter

logger = logging.getLogger(__name__)

# Spreadsheet exports get big; bound the row count so a pathological file
# can't turn into an unbounded in-memory list of dicts.
MAX_CSV_ROWS = 20000
# How far down the file to look for the header row before giving up. A CSV
# export of just the annex table needs only a handful of banner rows, but a
# full-declaration export (the legal IPR Information Statement/Licensing
# Declaration form on "page 1", with the annex table starting on "page 2")
# routinely runs 40-50 rows before the real header — real ETSI declarations
# seen in practice put the annex header as late as row 46. 150 gives that
# plenty of headroom while still bounding the worst case.
HEADER_SCAN_ROWS = 150


# ── Canonical fields and their aliases ────────────────────────────────────────
# Keys are the slim-schema field names consumed by core.prompts.slim_to_full;
# values are normalised alias strings (see _norm_header). Order matters only
# in that longer/more specific aliases should not be shadowed — matching tries
# exact alias first, so specificity is handled by _score_header's fallback.

FIELD_ALIASES: dict[str, list[str]] = {
    "n": [
        "disclosure number", "disclosure no", "disclosure",
        "disclosure ure number", "sr no", "serial number", "serial no",
        "s no", "no", "number", "item", "item no", "row", "index", "id",
    ],
    "project": [
        "project or standard name", "project standard name", "project",
        "standard name", "project name", "technology", "generation",
    ],
    "std": [
        "work item or standard no", "work item or standard number",
        "work item standard no", "standard technical specification or etsi work item",
        "work item", "standard no", "standard number", "standards",
        "specification", "specifications", "spec", "specs",
        "technical specification", "ts", "etsi work item", "standard",
    ],
    "clause": [
        "illustrative specific part of the standard", "illustrative specific part",
        "specific part of the standard", "specific part", "section",
        "clause", "illustrative specific part of the standard e g section",
    ],
    "ver": [
        "version", "version v x x x", "spec version", "standard version", "release",
    ],
    "who": [
        "applicant holder", "applicant or holder", "applicant", "holder",
        "proprietor", "assignee", "owner", "patent owner", "applicant name",
        "applicant holder name",
    ],
    "app": [
        "application no", "application number", "application",
        "appln no", "appl no", "app no", "app number", "filing number",
        "application no s", "patent application number",
    ],
    "pub": [
        "publication no", "publication number", "publication",
        "patent number", "patent no", "grant number", "grant no",
        "publication grant no",
    ],
    "title": [
        "patent application title", "patent applicationtitle",
        "patent or application title", "title of patent", "title",
        "patent title", "invention title", "application title",
    ],
    "cc": [
        "country of registration", "country", "jurisdiction",
        "country code", "registration country", "office", "patent office",
    ],
    "declarant": [
        "declarant", "ipr holder", "ipr holder organisation",
        "ipr holder organization", "legal name", "declaring company",
        "declarant name", "company",
    ],
}

# Fields that may legitimately appear twice in an ETSI annex (disclosure
# columns, then the same columns again under "FURTHER INFORMATION / Other
# members of this PATENT FAMILY"). Second and later occurrences are demoted
# to family_* instead of overwriting the disclosure's own value.
FAMILY_DEMOTABLE = {"app", "pub", "cc"}

# A row must supply at least one of these to be a real disclosure rather
# than a continuation/blank/footnote line.
IDENTIFYING_FIELDS = ("app", "pub", "title")

# Rows whose first non-empty cell matches these are annex boilerplate, not data.
_BOILERPLATE_RE = re.compile(
    r"^\s*(\*+\s*information on other members|please return this form|"
    r"etsi director-?general|etsi rules of procedure|ipr information statement|"
    r"page \d+ \(version|ipr declaration reference)",
    re.IGNORECASE,
)

# Excel's text-guard prefix on numeric-looking cells: ="0012345"
_EXCEL_GUARD_RE = re.compile(r'^\s*=\s*"(.*)"\s*$', re.DOTALL)

_STD_IN_PROJECT_RE = re.compile(r"\b(?:TS|TR|EN|ES|EG)\s?\d{2,3}[.\s]\d{2,3}", re.IGNORECASE)

# Splits a multi-standard cell. Newlines are the common case (a merged cell
# in the source table); comma/semicolon/pipe/slash cover hand-made CSVs.
# v0.4: also split space-separated lists ("TS 38.331 TS 38.213"), which is
# how a multi-line standards cell arrives once its line breaks are lost
# (copy/paste, PDF->CSV conversion). Only splits after a digit and before a
# spec prefix, so "3GPP TS 38.331" and "TS 123 501" stay whole.
_STD_SPLIT_RE = re.compile(
    r"[\n\r;|]+|,(?=\s*(?:TS|TR|ETS|EN|GS|3GPP|\d))|(?<=\d)\s*/\s*(?=[A-Z])"
    r"|(?<=\d)\s+(?=(?:TS|TR|ES|EN|EG|GS)\s*\d)",
    re.IGNORECASE,
)


# ── Detection ─────────────────────────────────────────────────────────────────

def looks_like_csv(filename: str | None, head: bytes) -> bool:
    """
    Decide whether an upload should take the CSV path.

    Extension is the primary signal because it is what the user actually
    controls, but it is not sufficient on its own: a file named .csv that
    is really a PDF should not be handed to the CSV reader. So a PDF magic
    number vetoes regardless of name, and an extensionless upload can still
    qualify on content (a delimiter appearing consistently in the first
    couple of lines).
    """
    if head.lstrip().startswith(b"%PDF-"):
        return False
    # Zip containers (.docx/.xlsx) and legacy OLE files (.doc/.xls) are
    # binary; their bytes can contain "consistent" commas purely by chance,
    # which previously routed a .docx into the CSV reader.
    if head.startswith(b"PK\x03\x04") or head.startswith(b"\xd0\xcf\x11\xe0"):
        return False

    name = (filename or "").lower().strip()
    if name.endswith((".csv", ".tsv", ".txt")):
        return True
    if name.endswith((".docx", ".docm", ".doc", ".xlsx", ".xlsm", ".xls", ".pdf", ".zip", ".html", ".htm")):
        return False

    # Content sniff for uploads with no useful extension. NUL bytes mean
    # binary content (a UTF-16 CSV would have been accepted by extension).
    if b"\x00" in head[:4096]:
        return False
    try:
        sample = head[:4096].decode("utf-8", errors="replace")
    except Exception:
        return False
    lines = [ln for ln in sample.splitlines()[:5] if ln.strip()]
    if len(lines) < 2:
        return False
    for delim in (",", ";", "\t", "|"):
        counts = [ln.count(delim) for ln in lines]
        if counts[0] >= 2 and len(set(counts)) <= 2:
            return True
    return False


# ── Decoding and dialect ──────────────────────────────────────────────────────

def _decode(data: bytes) -> tuple[str, str]:
    """
    Decode CSV bytes, trying the encodings that actually show up in the wild.

    utf-8-sig first because Excel writes a BOM by default and a stray
    "\ufeff" glued to the first header cell would break exact-alias matching
    on the very column most likely to be the disclosure number. UTF-16 is
    checked by BOM because Excel's "Unicode Text" export uses it, and a
    UTF-16 file decoded as latin-1 becomes NUL-riddled mojibake that looks
    superficially parseable — the worst kind of failure.
    """
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        try:
            return data.decode("utf-16"), "utf-16"
        except UnicodeDecodeError:
            pass
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return data.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace"), "utf-8/replace"


def _sniff_delimiter(text: str) -> str:
    """
    Pick the delimiter. csv.Sniffer is tried first but is not trusted blindly:
    it regularly picks a character that merely appears often inside free-text
    patent titles. The frequency check that follows only accepts the sniffed
    result if it is also the most consistent separator across the sample.
    """
    sample = "\n".join(text.splitlines()[:HEADER_SCAN_ROWS] or [text[:4096]])
    candidates = [",", ";", "\t", "|"]

    sniffed = None
    try:
        sniffed = csv.Sniffer().sniff(sample, delimiters="".join(candidates)).delimiter
    except Exception:
        pass

    lines = [ln for ln in sample.splitlines() if ln.strip()][:15]
    best, best_score = ",", -1.0
    for delim in candidates:
        counts = [ln.count(delim) for ln in lines]
        if not counts or max(counts) == 0:
            continue
        # Prefer a delimiter that appears often AND with a stable count per
        # line — a delimiter that varies wildly row to row is punctuation.
        mean = sum(counts) / len(counts)
        variance = sum((c - mean) ** 2 for c in counts) / len(counts)
        score = mean / (1.0 + variance)
        if delim == sniffed:
            score *= 1.25
        if score > best_score:
            best, best_score = delim, score
    return best


# ── Header normalisation and matching ─────────────────────────────────────────

def _norm_header(raw: str) -> str:
    """
    Normalise a header cell to a comparable token string.

    Unicode normalisation runs first so that a non-breaking space, an
    en-dash, or a full-width character copied out of a PDF-derived
    spreadsheet doesn't produce a header that looks identical on screen but
    fails every string comparison.
    """
    if raw is None:
        return ""
    s = unicodedata.normalize("NFKD", str(raw))
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.replace("\u00a0", " ")
    s = s.lower()
    # Drop parenthetical format hints like "(V.X.X.X)" or "(e.g. Section)".
    s = re.sub(r"\([^)]*\)", " ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


_NORMALISED_ALIASES: dict[str, dict[str, str]] = {
    field: {_norm_header(a): a for a in aliases}
    for field, aliases in FIELD_ALIASES.items()
}


def _match_field(header: str) -> tuple[str | None, float]:
    """
    Map one normalised header to a canonical field, with a confidence score.

    Two-stage on purpose. Exact alias match is the overwhelmingly common
    case ("the column names are usually identical") and must never be
    outranked by a fuzzy coincidence. Only when nothing matches exactly does
    token-overlap scoring run, which is what catches the genuine variants
    ("Applicant / holder name", "Patent / Application Title") without
    opening the door to matching arbitrary text.
    """
    if not header:
        return None, 0.0

    for field, aliases in _NORMALISED_ALIASES.items():
        if header in aliases:
            return field, 1.0

    tokens = set(header.split())
    if not tokens:
        return None, 0.0

    best_field, best_score = None, 0.0
    for field, aliases in _NORMALISED_ALIASES.items():
        for alias_norm in aliases:
            alias_tokens = set(alias_norm.split())
            if not alias_tokens:
                continue
            overlap = tokens & alias_tokens
            if not overlap:
                continue
            # Jaccard, biased toward covering the alias: a header that
            # contains every word of a short alias plus extra words is a
            # good match ("patent application title no" -> title), whereas
            # a header sharing one word out of six is not.
            score = len(overlap) / len(tokens | alias_tokens)
            coverage = len(overlap) / len(alias_tokens)
            combined = 0.4 * score + 0.6 * coverage
            if combined > best_score:
                best_field, best_score = field, combined

    # 0.75 is deliberately strict. A false positive here is worse than an
    # unmapped column: an unmapped column is reported and can be added to
    # FIELD_ALIASES, whereas a wrong mapping silently writes one field's
    # data into another's and looks like a successful parse.
    if best_score >= 0.75:
        return best_field, best_score
    return None, best_score


def _map_header_row(cells: list[str]) -> tuple[dict[int, str], list[str]]:
    """
    Map a physical header row to {column_index: canonical_field}.

    Duplicate canonical hits are demoted rather than dropped or overwritten
    (see FAMILY_DEMOTABLE and this module's docstring) — the ETSI annex's
    trailing PATENT FAMILY block reuses the same three column names, and
    letting the later ones win would replace each disclosure's real
    application number with a family member's.
    """
    mapping: dict[int, str] = {}
    unmapped: list[str] = []
    seen: set[str] = set()

    for idx, cell in enumerate(cells):
        norm = _norm_header(cell)
        field, _score = _match_field(norm)
        if not field:
            if norm:
                unmapped.append(str(cell).strip())
            continue
        if field in seen:
            if field in FAMILY_DEMOTABLE:
                mapping[idx] = f"family_{field}"
            else:
                unmapped.append(str(cell).strip())
            continue
        mapping[idx] = field
        seen.add(field)

    return mapping, unmapped


def _header_score(mapping: dict[int, str]) -> int:
    """
    Score a candidate header row. Weighted, not a raw count: a row that
    happens to contain "No" and "Country" is not a header, whereas one
    carrying an application/publication/title column almost certainly is.
    """
    fields = set(mapping.values())
    score = len(fields)
    score += 3 * len(fields & set(IDENTIFYING_FIELDS))
    score += 2 * len({"std", "who"} & fields)
    return score


def _find_header(rows: list[list[str]]) -> tuple[int, dict[int, str], list[str], int]:
    """
    Locate the header row, trying each candidate alone and merged with the
    one or two rows beneath it.

    The merge attempt is what handles the real ETSI annex layout, where the
    header spans multiple physical rows: a banner row ("STANDARD, TECHNICAL
    SPECIFICATION or ETSI Work Item", "FURTHER INFORMATION") occupying a
    merged Excel cell that visually spans several columns, with the actual
    per-column names ("Work Item or Standard No.", "Version", "Application
    No.") on the row(s) beneath it. Real-world exports have shown this
    running to a THIRD row too: the "FURTHER INFORMATION / Other members of
    this PATENT FAMILY" banner spans its own group of columns, whose
    individual "Application No." / "Publication No." / "Country of
    registration" sub-labels sit a row below the *other* group's sub-labels.

    Rows are combined by column-wise coalesce (last non-empty cell in the
    window wins for each column), not string concatenation. This matters
    specifically for Excel-derived input: `openpyxl` (read in `read_only`
    mode) only returns real text at the anchor cell of a merged range —
    every other column that range visually spans comes back empty. String-
    concatenating the banner row onto the row below would therefore glue
    the WHOLE banner phrase onto column 0 alone (added to that column's own
    real label) while every other column the banner visually spans gets
    nothing from it — which then makes column 0 the single strongest match
    for whichever field the banner's wording resembles (frequently a
    different field than what column 0 actually is), while the column that
    should have matched that field is left with no signal for it at all.
    Coalescing avoids that: a column with a real label in only one row of
    the window uses just that value, so column 0 gets its own row's
    "Disclosure Number" and the banner's field-shaped phrase doesn't
    contaminate a column it was never really describing to begin with.

    Returns (header_idx, mapping, unmapped, header_span), where header_span
    is how many physical rows the winning header candidate occupies (1, 2
    or 3) — the caller uses this to know how many rows to skip before data
    starts.

    Wider windows are only preferred over narrower ones when they score
    strictly higher; on an exact tie, the narrowest candidate wins. Without
    that tie-break, a wide window starting earlier can reach the same score
    as the true (narrower) header purely by absorbing preceding banner rows
    that contribute nothing (a banner row with real text in only one column
    that the header row itself also populates coalesces away to nothing) —
    which still finds the right columns, but reports the header as starting
    several rows too early, and that error compounds downstream:
    `_infer_declarant` searches `rows[:header_idx]` for a "Legal Name:"-style
    label, and a header_idx that swallowed that exact row would search too
    little of the preamble and miss it.
    """
    best = (-1, {}, [], 1)
    best_score = 0
    best_span = 1

    def _consider(idx: int, mapping: dict[int, str], unmapped: list[str],
                  span: int, score: int) -> None:
        """Keep a candidate header row if it maps more columns (or the same number over fewer lines)
        than the best so far.
        """
        nonlocal best, best_score, best_span
        if score > best_score or (score == best_score and span < best_span):
            best, best_score, best_span = (idx, mapping, unmapped, span), score, span

    for i in range(min(HEADER_SCAN_ROWS, len(rows))):
        row = rows[i]
        if not any(str(c).strip() for c in row if c is not None):
            continue

        mapping, unmapped = _map_header_row(row)
        _consider(i, mapping, unmapped, 1, _header_score(mapping))

        # Try progressively wider windows, coalescing column-wise rather
        # than concatenating (see this docstring's second paragraph for
        # why concatenation misattributes a spanning banner's field-shaped
        # wording to whichever column happens to be its anchor).
        for span in (2, 3):
            if i + span > len(rows):
                break
            window = rows[i:i + span]
            width = max(len(r) for r in window)
            coalesced = []
            for c in range(width):
                value = ""
                for r in window:
                    if c < len(r) and r[c] is not None and str(r[c]).strip():
                        value = str(r[c])
                coalesced.append(value)
            w_mapping, w_unmapped = _map_header_row(coalesced)
            _consider(i, w_mapping, w_unmapped, span, _header_score(w_mapping))

    return best


# ── Cell cleaning ─────────────────────────────────────────────────────────────

def _clean_cell(raw) -> str:
    """Cell value as clean text: Excel formula-guard wrappers (="...") removed, whitespace normalised."""
    if raw is None:
        return ""
    s = str(raw)
    m = _EXCEL_GUARD_RE.match(s)
    if m:
        s = m.group(1)
    s = s.replace("\u00a0", " ")
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\s*\n\s*", "\n", s)
    s = s.strip().strip('"').strip()
    # Spreadsheet null-ish placeholders that must not become literal values.
    if s.lower() in {"n/a", "na", "none", "null", "-", "--", "nil", "#n/a"}:
        return ""
    return s


def _split_standards(raw: str) -> list[str]:
    """
    Split a standards cell into individual codes.

    Merged cells arrive newline-separated; hand-made CSVs use commas. The
    comma rule is lookahead-guarded to a following spec token so that a
    single standard written with a comma inside it isn't chopped in half.
    """
    if not raw:
        return []
    parts = _STD_SPLIT_RE.split(raw)
    out: list[str] = []
    for p in parts:
        p = re.sub(r"\s+", " ", (p or "")).strip(" ,;.|")
        if not p:
            continue
        if p not in out:
            out.append(p)
    return out


def _is_boilerplate(cells: list[str]) -> bool:
    """True for rows that are declaration boilerplate (notes, page footers) rather than data."""
    joined = " ".join(c for c in cells if c).strip()
    if not joined:
        return False
    return bool(_BOILERPLATE_RE.match(joined))


# ── Declarant inference ───────────────────────────────────────────────────────

def _infer_declarant(preamble: list[list[str]], patents: list[dict],
                     column_declarant: str, filename: str) -> tuple[str, str]:
    """
    Work out whose declaration this is, returning (declarant, how_it_was_found).

    Reported alongside the value because the sources differ a lot in
    reliability: an explicit column is authoritative, the modal applicant is
    a reasonable inference, and the filename is a last resort that the user
    should be able to see and correct rather than have presented as fact.
    """
    if column_declarant:
        return column_declarant, "declarant column"

    # Preamble rows above the header often carry "Legal Name: Nokia
    # Technologies Oy" or a bare company name in the banner.
    label_re = re.compile(
        r"(?:legal\s*name|declarant|ipr\s*holder(?:\s*/\s*organisation)?)\s*[:\-]\s*(.+)",
        re.IGNORECASE,
    )
    for row in preamble:
        joined = " ".join(_clean_cell(c) for c in row).strip()
        if not joined:
            continue
        m = label_re.search(joined)
        if m:
            value = m.group(1).strip(" \"'|,;")
            if value:
                return value, "preamble label"

    applicants = [p["who"] for p in patents if p.get("who")]
    if applicants:
        top, count = Counter(applicants).most_common(1)[0]
        if count >= max(2, 0.5 * len(applicants)):
            return top, "most common applicant"

    stem = re.sub(r"\.(csv|tsv|txt)$", "", filename or "", flags=re.IGNORECASE)
    return (stem or "Unknown declarant"), "filename (unverified)"


# ── Text rendering (for downstream verification) ──────────────────────────────

def render_rows_as_text(header_fields: list[str], rows: list[list[str]]) -> str:
    """
    Render the file as plain text.

    This is not cosmetic. _run_analysis verifies every extracted application
    number, publication number and standard code by searching for it in the
    "source text" (utils.json_utils.build_candidate_index /
    verify_patent_fields). With no text the CSV path would report every
    field as unverified, which reads identically to a hallucinating LLM. By
    rendering the CSV's own cells, verification still means something real
    on this path: it confirms the value in the structured output is the
    value that was in the file.
    """
    lines: list[str] = []
    if header_fields:
        lines.append(" | ".join(header_fields))
    for row in rows:
        cells = [_clean_cell(c).replace("\n", " ") for c in row]
        if any(cells):
            lines.append(" | ".join(cells))
    return "\n".join(lines)


# ── Main entry point ──────────────────────────────────────────────────────────

def parse_csv_declaration(data: bytes, filename: str = "upload.csv") -> dict:
    """
    Parse CSV bytes into the slim extraction schema.

    Returns:
      {
        "ok":    bool  — True if headers were recognised well enough to map
                         deterministically; False means the caller should
                         fall back to the LLM extraction path using "text".
        "slim":  {"declarant": str, "patents": [ {...slim row...} ]}
        "text":  str   — plain-text rendering, used for field verification
                         (and as LLM input on the ok=False fallback path)
        "meta":  dict  — encoding, delimiter, header location, which columns
                         mapped to what, which didn't, row accounting, and
                         any warnings worth surfacing to the user
      }

    Never raises on malformed input; a file it cannot understand comes back
    as ok=False with an explanatory warning rather than an exception, so a
    bad CSV degrades to "analysed by the LLM like a PDF would be" instead of
    failing the whole upload.
    """
    text, encoding = _decode(data)
    if not text.strip():
        return {
            "ok": False, "slim": {"declarant": "", "patents": []}, "text": "",
            "meta": {"encoding": encoding, "warnings": ["File is empty."]},
        }

    delimiter = _sniff_delimiter(text)

    try:
        reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
        rows = [row for _, row in zip(range(MAX_CSV_ROWS + 1), reader)]
    except Exception as e:
        logger.warning(f"[{filename}] CSV read failed: {e}")
        return {
            "ok": False, "slim": {"declarant": "", "patents": []}, "text": text,
            "meta": {"encoding": encoding, "delimiter": delimiter,
                     "warnings": [f"Could not read as CSV ({e}); falling back to text analysis."]},
        }

    return parse_rows_declaration(
        rows, filename, base_meta={"encoding": encoding, "delimiter": delimiter}
    )


def parse_rows_declaration(rows: list[list[str]], filename: str = "upload",
                            base_meta: dict | None = None) -> dict:
    """
    Parse an already-tabular grid of cells (rows of strings) into the slim
    extraction schema.

    This is the format-agnostic half of `parse_csv_declaration`: everything
    from header discovery onward operates on `list[list[str]]` and never
    looks at how those rows were obtained. `parse_csv_declaration` gets here
    via `csv.reader` over decoded text; `utils.xlsx_parser` gets here via
    openpyxl/xlrd reading an .xlsx/.xls workbook's cells directly. Sharing
    this function (rather than each format having its own header-mapping
    pass) means fuzzy header matching, family-column demotion, boilerplate
    filtering and continuation-row handling can't drift apart between a CSV
    export and an Excel export of the same annex.

    `base_meta` lets a caller seed format-specific meta fields (encoding/
    delimiter for CSV, sheet name for xlsx) that are merged into the
    returned "meta" dict alongside the fields computed here.

    Returns the same {"ok", "slim", "text", "meta"} shape as
    `parse_csv_declaration` — see its docstring for field semantics.
    """
    warnings: list[str] = []
    base_meta = dict(base_meta or {})

    truncated = len(rows) > MAX_CSV_ROWS
    if truncated:
        rows = rows[:MAX_CSV_ROWS]
        warnings.append(
            f"File exceeds {MAX_CSV_ROWS} rows; only the first {MAX_CSV_ROWS} were read."
        )

    header_idx, mapping, unmapped, header_span = _find_header(rows)
    data_start = header_idx + header_span

    mapped_fields = set(mapping.values())
    usable = (
        header_idx >= 0
        and bool(mapped_fields & set(IDENTIFYING_FIELDS))
        and len(mapped_fields) >= 2
    )

    rendered = render_rows_as_text(
        [f"{mapping.get(i, '?')}" for i in range(max(mapping) + 1)] if mapping else [],
        rows[data_start:] if usable else rows,
    )

    if not usable:
        warnings.append(
            "Could not identify the expected columns (an application number, "
            "publication number or title column is required). Falling back to "
            "language-model extraction over the raw file contents."
        )
        return {
            "ok": False,
            "slim": {"declarant": "", "patents": []},
            "text": rendered,
            "meta": {
                **base_meta,
                "header_row": header_idx if header_idx >= 0 else None,
                "mapped_columns": {}, "unmapped_columns": unmapped,
                "rows_total": len(rows), "rows_parsed": 0, "rows_skipped": len(rows),
                "warnings": warnings,
            },
        }

    # ── Row extraction ────────────────────────────────────────────────────
    patents: list[dict] = []
    skipped = 0
    column_declarant = ""
    seq = 0

    for row in rows[data_start:]:
        cells = [_clean_cell(c) for c in row]
        if not any(cells):
            skipped += 1
            continue
        if _is_boilerplate(cells):
            skipped += 1
            continue

        rec: dict[str, str] = {}
        family: dict[str, str] = {}
        for idx, field in mapping.items():
            value = cells[idx] if idx < len(cells) else ""
            if not value:
                continue
            if field.startswith("family_"):
                family[field] = value
            else:
                rec[field] = value

        # A repeated header row (common when a multi-page table is exported
        # page by page) maps cleanly onto its own field names — detect it by
        # checking whether the cells still read as headers.
        repeat_mapping, _ = _map_header_row(cells)
        if _header_score(repeat_mapping) >= _header_score(mapping) * 0.8:
            skipped += 1
            continue

        if rec.get("declarant") and not column_declarant:
            column_declarant = rec["declarant"]

        std_list = _split_standards(rec.get("std", ""))
        # ETSI's "Project or Standard name" column often holds the standard
        # itself ("TS 38.331") when there is no separate standard-number
        # column; use it as the declared standard rather than losing it.
        if not std_list and _STD_IN_PROJECT_RE.search(rec.get("project", "")):
            std_list = _split_standards(rec["project"])
        has_identity = any(rec.get(f) for f in IDENTIFYING_FIELDS) or bool(rec.get("who"))

        if not has_identity and not std_list:
            skipped += 1
            continue

        # Continuation rows (extra standards for the row above, with every
        # other cell blank) are emitted as-is: core.prompts's
        # _merge_continuation_patents already folds exactly this shape into
        # the preceding disclosure, and reusing it keeps CSV and PDF inputs
        # producing identical structures rather than two near-duplicate
        # merge implementations that can drift apart.
        if not has_identity:
            patents.append({"n": None, "std": std_list})
            continue

        seq += 1
        raw_n = rec.get("n", "")
        n_match = re.search(r"\d+", raw_n)
        patents.append({
            "n": int(n_match.group()) if n_match else seq,
            "std": std_list,
            "project": rec.get("project", ""),
            "who": rec.get("who", ""),
            "app": rec.get("app", ""),
            "pub": rec.get("pub", ""),
            "cc": rec.get("cc", ""),
            "title": rec.get("title", ""),
            "ver": rec.get("ver", ""),
            "clause": rec.get("clause", ""),
            **({"_family": family} if family else {}),
        })

    declarant, declarant_source = _infer_declarant(
        rows[:header_idx], patents, column_declarant, filename
    )
    if declarant_source == "filename (unverified)":
        warnings.append(
            "No declarant column or label was found; the declarant name was "
            "taken from the filename and should be confirmed."
        )

    if not patents:
        warnings.append("Headers were recognised but no data rows contained patent details.")

    logger.info(
        f"[{filename}] Tabular input parsed: {len(patents)} row(s) from {len(rows)} "
        f"line(s) ({', '.join(f'{k}={v!r}' for k, v in base_meta.items())}, "
        f"header row {header_idx}, span {header_span})"
    )

    return {
        "ok": bool(patents),
        "slim": {"declarant": declarant, "patents": patents},
        "text": rendered,
        "meta": {
            **base_meta,
            "header_row": header_idx,
            "merged_header": header_span > 1,
            "header_span": header_span,
            "mapped_columns": {str(i): f for i, f in sorted(mapping.items())},
            "unmapped_columns": unmapped,
            "rows_total": len(rows),
            "rows_parsed": len(patents),
            "rows_skipped": skipped,
            "declarant_source": declarant_source,
            "truncated": truncated,
            "warnings": warnings,
        },
    }
