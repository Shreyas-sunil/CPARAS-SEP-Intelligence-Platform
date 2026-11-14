"""
Text Repair — pre-processing pass run on extracted PDF text BEFORE it is
chunked, sent to the LLM, or used as the verification source text.

Why this has to happen this early (and not, say, only inside
verify_code_in_text): a PDF's text layer/OCR output is emitted per *visual*
line, not per logical field. A table cell whose content is wider than the
column wraps onto a second visual line with a hard newline in between. Three
concrete, observed failure modes follow directly from that, and all three
are fixed by putting the text back into its logical (unwrapped) form before
anything downstream looks at it:

1. A patent identifier's tail digits land on their own line
   ("PCT/CN2024/1107" / "84") — verify_code_in_text() then normalizes each
   side of the newline correctly, but the two fragments never sit next to
   each other in the *normalized source text* it's searching (the row's
   other table columns sit between them once cells are flattened), so a
   correct, complete identifier is reported as "not found" while a
   TRUNCATED identifier ("PCT/CN2024/1107") that happens to appear as a
   genuine substring is accepted. See ISSUE 1 in the audit.

2. That same orphaned tail ("84") sits at the start of its own line,
   indistinguishable at a glance from a genuine row-number anchor
   ("84  Cooperation Patent Treaty …" looks exactly like a new table row
   whose row number is 84). Left in place, this becomes a phantom row —
   either mis-split into its own chunk, or read as a new disclosure by the
   LLM. See ISSUE 3.

3. A country name wraps ("UNITED" / "STATES") or, worse, wraps around
   *another* column's content that bled into the same cell
   ("UNITED" / "Technologies Oy KINGDOM") — CountryBadge and the LLM both
   need the country name whole and in the right column. See ISSUES 6-7.

All functions here are pure str -> str transforms with no external
dependencies, so they're cheap to run on every document (OCR and native
alike) and cheap to unit test in isolation.
"""

from __future__ import annotations

import re

# ── Wrapped patent/application identifiers ──────────────────────────────────

# Prefixes this module actively reconstructs. Not exhaustive of every patent
# office in the world, but covers every family named in the audit (PCT, WO,
# EP, US, CN, KR, JP) plus a handful of other common ETSI-declarant offices.
# A prefix that isn't in this list is simply left alone rather than mis-merged
# — reconstruction is deliberately conservative (see module docstring).
_ID_PREFIXES = r"(?:PCT|WO|EP|US|CN|KR|JP|IN|DE|FR|GB|UK|CA|AU|BR|RU)"

# Matches a line that *ends* mid-identifier: a known prefix followed by the
# code-ish characters (digits, slashes, dots, dashes — the punctuation these
# offices actually use inside a number) with NO trailing delimiter/word
# boundary that would mark the identifier as already complete on this line
# (e.g. a following space-and-word, a pipe, or closing punctuation). Anchored
# to end-of-line ($) so it only matches genuine wraps, not an identifier that
# is simply followed by more text on the same line.
_WRAP_TAIL_RE = re.compile(
    rf"(?P<code>\b{_ID_PREFIXES}[A-Z]{{0,2}}[/\-]?[0-9./\-]{{2,30}}\d)\s*$",
    re.IGNORECASE,
)

# A continuation fragment: 1-4 bare digits at the very start of the next
# line. Deliberately narrow (digits only, short run) — this is what a
# wrapped numeric tail looks like. Capturing group 1 is the digits to
# reattach; group 2 is whatever follows them on that line (kept, not
# discarded — see docstring point 2: that's usually unrelated boilerplate
# text, not a new row, and must survive as ordinary prose once the leading
# digits that made it *look* like a row anchor are gone).
_CONTINUATION_RE = re.compile(r"^[ \t]*(\d{1,4})(?!\d)(?!\s*\|)(.*)$")


def reconstruct_wrapped_identifiers(text: str) -> str:
    """
    Merge a patent/application identifier's tail digits back onto the line
    where the identifier started, when a hard line-break split them.

    Distinguishing a genuine wrapped continuation from a genuine new table
    row (ISSUE 3's "using surrounding context" requirement) is done with two
    separate context checks, both of which must hold:

      (a) the PRECEDING line must end mid-identifier — i.e. it matches a
          known office prefix immediately followed by digits, with nothing
          else after it on that line. A line that ends in ordinary prose
          never matches this, so ordinary paragraph text is untouched.

      (b) the FOLLOWING line's leading digit run must NOT be immediately
          followed by a pipe ("|"). This codebase's own table extraction
          (utils/pdf_parser.py) emits genuine table rows as pipe-delimited
          text ("5 | 5G Release 15 | TS 38.331 | …"), so "<digits> |" is the
          signature of a real row, while "<digits> <prose, no pipe>" is the
          signature of an orphaned wrap tail sitting in front of unrelated
          text.

    Both conditions have to hold before anything is merged, so this cannot
    accidentally eat a real row that happens to start with a short number
    (real rows have a pipe right after their row-number cell).
    """
    if not text or "\n" not in text:
        return text

    lines = text.split("\n")
    out: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        wrap_match = _WRAP_TAIL_RE.search(line)
        if wrap_match and i + 1 < n:
            cont_match = _CONTINUATION_RE.match(lines[i + 1])
            if cont_match:
                digits, remainder = cont_match.group(1), cont_match.group(2)
                merged = line[: wrap_match.start("code")] + wrap_match.group("code") + digits
                out.append(merged)
                remainder = remainder.strip()
                # Only keep the remainder if it still has real content —
                # dropping a now-empty line avoids leaving a stray blank
                # line where the phantom row anchor used to be.
                if remainder:
                    out.append(remainder)
                i += 2
                continue
        out.append(line)
        i += 1
    return "\n".join(out)


# ── Wrapped / column-bled country names ─────────────────────────────────────

# First token of a country/territory name that is long enough, and common
# enough in ETSI declarant addresses, to legitimately wrap onto its own line
# — mapped to the acceptable continuation token(s). Deliberately a fixed,
# reviewable list rather than a generic "any two capitalized words" heuristic
# (which would false-positive constantly on applicant company names in caps).
_COUNTRY_WRAP_HEAD = (
    "UNITED", "SOUTH", "NORTH", "NEW", "SAUDI", "RUSSIAN", "CZECH", "HONG",
    "SRI", "COSTA", "PUERTO", "DOMINICAN",
)
_COUNTRY_WRAP_TAIL = (
    "STATES(?: OF AMERICA)?", "KINGDOM", "KOREA", "AFRICA", "ZEALAND",
    "ARABIA", "FEDERATION", "REPUBLIC", "KONG", "LANKA", "RICA", "REPUBLIC",
)

# Spans a line break (optionally with OTHER column content — e.g. an
# applicant name — bled in between the two halves of the country name; see
# ISSUE 7). `mid` is kept and re-emitted ahead of the reassembled country
# name, separated with a pipe, so a downstream table-aware reader still sees
# it as a distinct cell instead of it silently vanishing or fusing into the
# country string.
_COUNTRY_BLEED_RE = re.compile(
    r"\b(" + "|".join(_COUNTRY_WRAP_HEAD) + r")\s*\n\s*"
    r"(?P<mid>[A-Za-z0-9.,&'\-\s]{0,80}?)\s*"
    r"\b(" + "|".join(_COUNTRY_WRAP_TAIL) + r")\b",
)


def repair_wrapped_country_names(text: str) -> str:
    """
    Reassemble a country/territory name that wrapped across a line break,
    repairing simple wraps ("UNITED" / "STATES") and column-bled wraps where
    another cell's content landed between the two halves ("UNITED" /
    "Technologies Oy KINGDOM" -> "Technologies Oy | UNITED KINGDOM").

    Without this, CountryBadge only ever sees the first fragment ("UNITED")
    and falls through its lookup table (ISSUE 6), and the bled-in applicant
    text and the country name are never separated back into distinct table
    cells before the LLM sees them (ISSUE 7).
    """
    if not text or "\n" not in text:
        return text

    def _fix(m: re.Match) -> str:
        """Re-join a country name split across two table lines ('UNITED' + 'STATES'), keeping any text
        that was between them.
        """
        country = f"{m.group(1)} {m.group(3)}".upper()
        mid = (m.group("mid") or "").strip(" \t.,")
        return f"{mid} | {country}" if mid else country

    return _COUNTRY_BLEED_RE.sub(_fix, text)


# ── Column-bleed repair for wrapped table cells more generally ─────────────

def repair_column_bleed(text: str) -> str:
    """
    Best-effort repair for a wrapped table row whose continuation line has
    another column's content mixed into it before the pipe-delimited table
    structure resumes. Currently this is the country-name case handled by
    repair_wrapped_country_names(); kept as a separate, named entry point
    so additional column-bleed patterns (once observed on other declarant
    layouts) can be added here without changing every call site.
    """
    return repair_wrapped_country_names(text)


def reconstruct_all(text: str) -> str:
    """
    Run every text-repair pass, in the order that matters: identifiers
    first (so a wrapped identifier's tail digits are gone from the next
    line before anything else looks at that line's leading characters),
    then country-name / column-bleed repair.

    This is the single entry point every caller (analysis.py's chunking,
    verification, and the LLM prompt) should use instead of the raw
    extracted text, for both OCR and native extractions alike — the wrap
    problem is a layout artifact of the PDF, not an OCR artifact, so the
    fix must not be gated on which extraction tier produced the text
    (see ISSUE 1's explicit requirement to remove that gating for fuzzy
    verification; the same logic applies here, and reconstruction is what
    actually makes the OCR-only fuzzy gate unnecessary in the first place).
    """
    if not text:
        return text
    text = reconstruct_wrapped_identifiers(text)
    text = repair_column_bleed(text)
    return text
