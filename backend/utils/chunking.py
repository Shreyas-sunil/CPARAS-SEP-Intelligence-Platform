"""
Chunking & Budgeting
=====================
Row-safe text chunking (never cuts a table row in half) and per-chunk LLM
output token budgeting, split out of api/routes/analysis.py so it has no
FastAPI/httpx/SQLAlchemy dependency and can be unit tested in isolation.

Covers:
  ISSUE 2 — row-anchor regex missing comma (and general regex review)
  ISSUE 4 — chunking dropping document headers/cover pages
  ISSUE 5 — token budgeting that under-budgets on OCR under-detection
"""

from __future__ import annotations

import re

# ── ISSUE 2: row anchor regex ────────────────────────────────────────────────
#
# Matches a table row's leading "<row number> <optional project code>
# <TS|EN|TR spec code>" anchor, e.g. "1  5G  TS 38.331" (plain flowing
# text) or "1 | 5G Release 16 | TS 38.213" (pipe-delimited table row, which
# is what utils/pdf_parser.py emits for every page pdfplumber detects an
# actual table on).
#
# BUG (ISSUE 2): the project-code character class was `[A-Za-z0-9./\- ]`,
# which does not include a comma. A cell like "5G Release 15," (a perfectly
# ordinary way to cite a release with a trailing comma before the next
# column) breaks the match at the comma: the non-greedy `{0,20}?` group
# cannot consume it, so the regex is unable to reach the following TS/EN/TR
# token from that starting position at all, and the row silently fails to
# match — losing that row from row-count estimation (ISSUE 5's budget
# input), from row-safe chunk splitting (this row's boundary is invisible,
# so it can be cut mid-row), and from the phantom-row/wrap-tail guard in
# text_repair.py. Fixed by adding a comma to the allowed characters.
_ROW_ANCHOR_RE = re.compile(
    r'(?:^|\n)\s{0,4}\d{1,3}\s*\|?\s*(?:[A-Z0-9][A-Za-z0-9./,\- ]{0,20}?\s*\|?\s*)?(?:TS|EN|TR)\s?\d'
)

# Public accessor — kept as a compiled pattern (not just the string) so
# callers doing re.finditer/re.search don't each pay recompilation cost.
ROW_ANCHOR_RE = _ROW_ANCHOR_RE


def row_positions(text: str) -> list[int]:
    """Start offsets of every detected row anchor in `text`."""
    return [m.start() for m in _ROW_ANCHOR_RE.finditer(text)]


def estimate_row_count(text: str) -> int:
    """At least 1, so callers can safely divide/multiply by this."""
    return max(1, len(row_positions(text)))


# ── ISSUE 4: header-preserving chunking ─────────────────────────────────────

MAX_ROWS_PER_CHUNK = 40


def split_into_chunks(text: str, chunk_size: int = 20000) -> list[str]:
    """
    Split declaration text into chunks that never cut across a patent row,
    and — this is the ISSUE 4 fix — that never drop the document's header
    (declarant name, cover page, filing metadata, any text before the first
    detected row) on the floor.

    Splitting strategy (fallback chain):
      1. Split on "Disclosure N:" boundaries (Vision OCR output).
      2. Split on detected row-number anchors (ROW_ANCHOR_RE).
      3. Character-based paragraph fallback if nothing else works.

    HEADER PRESERVATION (ISSUE 4): strategies 1 and 2 previously started
    their first chunk's slice at the position of the FIRST detected marker/
    row, which discards `text[:first_marker_pos]` entirely — not just from
    later chunks, but from EVERY chunk, including the first. On a large
    declaration (e.g. Qualcomm's annex) that prefix is the declarant name,
    cover page, and filing metadata: thousands of characters that then never
    reach the LLM at all, in any chunk. Both strategies now capture that
    leading header text once and prepend it to every chunk they produce, so
    each independent LLM call still has the declarant/metadata context it
    needs to populate `declaration_metadata` correctly — not just the first
    chunk.
    """
    if len(text) <= chunk_size:
        return [text]

    # Strategy 1: split on "Disclosure N:" markers (Vision OCR format)
    disclosure_positions = [m.start() for m in re.finditer(r'(?=Disclosure \d+:)', text, re.IGNORECASE)]
    if len(disclosure_positions) >= 2:
        header = text[:disclosure_positions[0]]
        chunks = []
        group_start = 0
        prev_pos = disclosure_positions[0]
        for pos in disclosure_positions[1:]:
            if pos - group_start > chunk_size:
                chunks.append(text[group_start:prev_pos])
                group_start = prev_pos
            prev_pos = pos
        chunks.append(text[group_start:])
        result = [c for c in chunks if c.strip()]
        if result:
            return _prepend_header_to_all(result, header)

    # Strategy 2: split on row-number anchors. Verified against the Nokia
    # sample: finds all 27 row boundaries; the middle "short project code"
    # token (e.g. "5G", "BRAN") is OPTIONAL since some declarant layouts
    # (Qualcomm) put the row number directly in front of the TS/EN/TR code.
    positions = row_positions(text)
    if len(positions) >= 2:
        header = text[:positions[0]]
        chunks = []
        group_start = positions[0]
        prev_pos = positions[0]
        rows_in_group = 1
        for pos in positions[1:]:
            if pos - group_start > chunk_size or rows_in_group >= MAX_ROWS_PER_CHUNK:
                chunks.append(text[group_start:prev_pos])
                group_start = prev_pos
                rows_in_group = 0
            prev_pos = pos
            rows_in_group += 1
        chunks.append(text[group_start:])
        result = [c for c in chunks if c.strip()]
        if result:
            return _prepend_header_to_all(result, header)

    # Strategy 3: split on paragraph/row boundaries (last resort — no row
    # anchor detected, e.g. unstructured OCR prose). This strategy already
    # starts group_start at 0, so the header is naturally included in
    # whichever chunk it falls into — but it's still only in ONE chunk, so
    # apply the same prepend treatment for consistency across all chunks.
    paragraphs = re.split(r'\n{2,}', text)
    chunks = []
    current: list = []
    current_len = 0
    for para in paragraphs:
        if current_len + len(para) > chunk_size and current:
            chunks.append("\n\n".join(current))
            current = [para]
            current_len = len(para)
        else:
            current.append(para)
            current_len += len(para)
    if current:
        chunks.append("\n\n".join(current))
    result = [c for c in chunks if c.strip()]
    if not result:
        return [text]
    if len(result) > 1:
        # Use the first chunk's own leading text as the "header" — it
        # already starts at position 0, so this is a no-op for chunk 1 and
        # brings the same lead-in context to every later chunk.
        approx_header_len = min(2000, len(result[0]))
        header = result[0][:approx_header_len]
        return _prepend_header_to_all(result, header)
    return result


def _prepend_header_to_all(chunks: list[str], header: str) -> list[str]:
    """
    Prepend `header` to EVERY chunk, including the first.

    Note this deliberately does NOT special-case chunk 0 as "already having
    the header": strategies 1 and 2 both slice their first chunk starting
    at `group_start = <first marker/row position>`, which is already past
    the header — chunk 0 loses it exactly the same way every later chunk
    does. (Only strategy 3's fallback naturally includes position-0 content
    in its first chunk; harmless to also run this over that case since the
    `header not in c` check below makes it a no-op there.)

    Capped so a pathologically long header can't blow the per-chunk
    character budget on its own — a capped header still gives every chunk
    the declarant/metadata signal without starving it of row content.
    """
    header = header.strip()
    if not header or not chunks:
        return chunks
    MAX_HEADER_CHARS = 3000
    if len(header) > MAX_HEADER_CHARS:
        header = header[:MAX_HEADER_CHARS] + "\n… [header truncated] …"
    marker = (
        "\n\n--- DOCUMENT HEADER (repeated for context; this is not part of "
        "the row data below) ---\n"
    )
    out = []
    for c in chunks:
        if header and header not in c:
            out.append(header + marker + c)
        else:
            out.append(c)
    return out


# ── ISSUE 5: token budgeting ─────────────────────────────────────────────────
#
# Previous formula: chunk_max_tokens = min(18000, est_rows * 300 + 1200)
# depended ENTIRELY on est_rows (from ROW_ANCHOR_RE, which only recognizes
# TS/EN/TR-anchored rows). When OCR under-detects rows — e.g. it reads 15 of
# a page's actual 40 rows because of noise, or the anchor pattern simply
# doesn't match that declarant's row format — the budget silently follows
# the undercount down (15*300+1200=5700 tokens for a chunk that actually
# needs ~40*300+1200=13200), the LLM truncates its output mid-array, and
# salvage_partial_array recovers a partial result that was previously
# indistinguishable from a complete one.

_MIN_CHUNK_TOKENS = 2500
_MAX_CHUNK_TOKENS = 18000
_TOKENS_PER_ROW = 300
_BASE_TOKENS = 1200


def estimate_chunk_token_budget(
    chunk_text: str,
    est_rows: int,
    *,
    ocr_confidence: float | None = None,
    is_ocr: bool = False,
    page_count: int | None = None,
    table_density: float | None = None,
) -> int:
    """
    Size the LLM output token budget for one chunk from MULTIPLE signals
    instead of `est_rows` alone (ISSUE 5).

    Never returns less than _MIN_CHUNK_TOKENS, and always at least enough to
    cover est_rows at the standard per-row rate — uncertainty only ever
    INCREASES the budget relative to the plain `est_rows` formula, never
    decreases it.
    """
    base = est_rows * _TOKENS_PER_ROW + _BASE_TOKENS

    # Length-based floor: independent of row detection entirely. A long
    # chunk realistically contains far more than 1-2 rows even if the
    # anchor regex found none of them (e.g. a table format the regex
    # doesn't recognize) — assume a conservative minimum row density
    # instead of trusting a zero/near-zero row count from a long chunk.
    CHARS_PER_ROW_FLOOR = 400  # conservative — real rows run ~150-600 chars
    length_implied_rows = max(1, len(chunk_text) // CHARS_PER_ROW_FLOOR)
    # Capped independently of est_rows (NOT relative to it) — the whole
    # point of this floor is to not trust est_rows when it's low/zero, so
    # scaling the cap by est_rows would undo that on exactly the case this
    # exists for.
    MAX_LENGTH_IMPLIED_ROWS = 60
    length_floor = min(length_implied_rows, MAX_LENGTH_IMPLIED_ROWS) * _TOKENS_PER_ROW + _BASE_TOKENS
    base = max(base, length_floor)

    # Page-count floor: expected rows/page is deliberately conservative
    # (ETSI annex tables commonly run 5-15 rows/page) so this only kicks in
    # when est_rows is clearly too low for the number of pages covered.
    if page_count and page_count > 0:
        EXPECTED_ROWS_PER_PAGE = 4
        page_floor_rows = page_count * EXPECTED_ROWS_PER_PAGE
        base = max(base, page_floor_rows * _TOKENS_PER_ROW + _BASE_TOKENS)

    # Uncertainty multiplier: OCR (or low OCR confidence, or high table
    # density with a suspiciously low row count) means est_rows is less
    # trustworthy, so scale the whole budget up rather than just the floor.
    multiplier = 1.0
    if is_ocr:
        multiplier += 0.35
    if ocr_confidence is not None:
        if ocr_confidence < 55:
            multiplier += 0.5
        elif ocr_confidence < 75:
            multiplier += 0.25
    if table_density is not None and table_density > 0.5 and est_rows <= 3:
        # Lots of table structure but almost no rows recognized — a strong
        # under-detection signal specifically.
        multiplier += 0.4

    budget = int(base * multiplier)
    budget = max(_MIN_CHUNK_TOKENS, budget)
    budget = min(_MAX_CHUNK_TOKENS, budget)
    return budget


def extraction_seems_incomplete(
    parsed_row_count: int, est_rows: int, *, was_salvaged: bool
) -> bool:
    """
    ISSUE 5: "never silently accept partial extraction". A chunk's output is
    treated as suspect (worth a retry with a bigger budget) when either:
      - it had to be recovered via salvage_partial_array at all (that only
        runs after a full JSON parse failed, i.e. generation broke), or
      - the number of rows actually parsed is well below what row detection
        expected for this chunk (a generation that "completed" cleanly but
        stopped emitting rows early is just as much data loss as a
        truncation salvage caught).
    """
    if was_salvaged:
        return True
    if est_rows <= 0:
        return False
    return parsed_row_count < max(1, int(est_rows * 0.6))
