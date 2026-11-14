"""
SEP Analysis Prompts
====================
Optimized for fast local inference on small models (qwen2.5:3b, phi3:mini).
Key design principle: minimal output schema = fast inference.

The schema is intentionally compact — deep enrichment is handled by the
research/engine.py post-processing layer, not the LLM.
"""

import json
import logging
import re

logger = logging.getLogger(__name__)

# ── Number isolation ──────────────────────────────────────────────────────────
# Small models (qwen2.5:3b) frequently fail to segment a dense OCR'd table row
# and dump the ENTIRE row (project + standard + applicant + number + country)
# into the "app"/"pub" field instead of just the number, e.g.:
#   "5G TS 38.300 Nokia 2411709.5 UNITED KINGDOM"
# instead of just "2411709.5".
#
# Downstream, verify_patent_fields() tries to match that whole contaminated
# string against the source text, fails (because those tokens are spread
# across different table columns, not one contiguous span), and the number
# gets logged as "likely fabricated" — even when the actual number is
# correct and present verbatim in the source. This isolates the real
# number-shaped token so verification checks the right thing.
_NUMBER_PATTERNS = re.compile(
    r'(?<!\+)\b\d{9,12}\b'                              # plain digit app numbers
    r'|PCT/[A-Z]{2}\d{4}/\d+(?:\s\d+)?'                  # PCT apps — the (?:\s\d+)?
                                                          # tail absorbs a line-wrap
                                                          # that splits the serial
                                                          # digits themselves (e.g.
                                                          # "1107" | "84" wrapping to
                                                          # "PCT/CN2024/1107 84"),
                                                          # which previously truncated
                                                          # to just "...1107"
    r'|\d{6,8}\.\d\b'                                    # UK-style e.g. 2411709.5
    r'|\b63/\d{6}\b'                                     # US provisional e.g. 63/680651
    r'|\b(?:US|IN|CN|EP|WO|JP|KR|DE|FR|GB|TW|AU|CA|RU|BR|ES|IT)\d{6,12}(?:[A-Z]\d?)?\b'
                                                          # country-prefixed numbers, with an
                                                          # optional kind code (B2, A1, A, ...):
                                                          # without it every normal publication
                                                          # number (US11000053B2) failed to match
                                                          # and was blanked as 'contaminated'
    r'|\b10-\d{4}-\d{7}\b'                             # Korean application e.g. 10-2019-0012345
    r'|\b\d{2,4}[A-Z]{2,4}\d{4}\b'                       # older Indian style e.g.
                                                          # 980DEL2001 — digits-letters-
                                                          # digits, not covered by any
                                                          # digit-only pattern above
)


def _isolate_number(raw: str) -> tuple[str, bool]:
    """
    Return (clean_value, was_contaminated).
    If `raw` already looks like a bare number, returns it unchanged.
    If `raw` contains a number-shaped token embedded in extra text (the
    field-mixing failure mode above), extracts just that token.
    If no number-shaped token is found at all, the field is empty rather
    than passing `raw` through: a real application/publication number
    always contains at least one digit, so a value with none (observed
    concretely: the model sometimes dumps the applicant's own name, e.g.
    "Nokia Technologies Oy", straight into this field when the source
    table's Applicant/holder column is filled in on that row) is not a
    number at all, just contamination we can't isolate a number from. This
    still isn't silently dropped — the caller stashes `raw` in
    `_raw_application_number`/`_raw_publication_number` for audit — but it
    no longer masquerades as a valid application number in the field the
    UI displays and the dedup safety-net keys off.
    """
    if not raw:
        return raw, False
    raw = raw.strip()
    matches = _NUMBER_PATTERNS.findall(raw)
    if not matches:
        return ("", True) if re.search(r"[A-Za-z]", raw) else (raw, False)
    first = re.sub(r"\s+", "", matches[0])  # collapse a PCT line-wrap space
    # If the field is already JUST the number (ignoring whitespace), nothing
    # was contaminated.
    if re.sub(r"\s+", "", raw) == first:
        return first, False
    # Contaminated: extra tokens (std/applicant/country) were mixed in.
    # Take the first number-shaped match — application/publication columns
    # only ever contain one such token per row.
    return first, True


_KIND_CODE_SUFFIX_RE = re.compile(r'[ ]?[A-Z]\d{1,2}$')


def _reconcile_app_pub(disclosure_n, app_clean: str, pub_clean: str) -> tuple[str, str]:
    """
    Deterministic backstop for the app/pub column-swap failure mode:
    small models extracting a table with no visible column header (e.g.
    the header row was stripped as repeated boilerplate — see
    utils/pdf_parser.py's _strip_repeated_boilerplate_lines) have nothing
    to anchor "app" vs "pub" on and can duplicate one number into both
    fields, or put a lone number in the wrong one. This runs regardless of
    whether the LLM followed the app-vs-pub prompt rule above, so a single
    row's mistake doesn't depend entirely on the model getting the rule
    right.

    Heuristic: a real publication number almost always ends in a
    kind-code suffix (a letter + 1-2 digits, e.g. "A1", "B2"); a real
    application number essentially never does. This mirrors, rather than
    replaces, the natural-language rule given to the LLM.

    - Identical non-empty values in both fields: keep it in whichever
      field the kind-code suffix indicates, clear the other.
    - app empty, pub filled, and pub has NO kind-code suffix: it's an
      application number filed in the wrong column — move it to app.
    - Otherwise: leave both fields as extracted.
    """
    if app_clean and pub_clean and app_clean == pub_clean:
        has_kind_code = bool(_KIND_CODE_SUFFIX_RE.search(pub_clean))
        logger.warning(
            f"Disclosure #{disclosure_n}: 'app' and 'pub' were extracted as "
            f"the identical value {pub_clean!r} — keeping it only in the "
            f"'{'pub' if has_kind_code else 'app'}' field (per kind-code suffix). "
            f"This is a field correction within this one disclosure; no row is removed."
        )
        return ("", pub_clean) if has_kind_code else (pub_clean, "")

    if not app_clean and pub_clean and not _KIND_CODE_SUFFIX_RE.search(pub_clean):
        logger.warning(
            f"Disclosure #{disclosure_n}: 'app' was empty but 'pub' "
            f"({pub_clean!r}) has no kind-code suffix, so it looks like an "
            "application number placed in the wrong column — moving it to 'app'."
        )
        return pub_clean, ""

    return app_clean, pub_clean


_COUNTRY_TAIL_RE = re.compile(
    r'\b(?:UNITED STATES|UNITED KINGDOM|CHINA|INDIA|JAPAN|FINLAND|KOREA|'
    r'GERMANY|FRANCE|EUROPEAN PATENT OFFICE|PATENT COOPERATION TREATY)\b.*$',
    re.IGNORECASE,
)


def _strip_trailing_country(raw: str) -> str:
    """
    Strip a trailing country name that leaked onto the end of a field that
    should never legitimately contain one (applicant name, patent title).
    Same row-dump contamination pattern as _isolate_number/_isolate_applicant
    below — when the OCR text loses column structure, the model has nothing
    to anchor field boundaries on, and the last column's value (country of
    registration) gets tacked onto whatever text came before it. Confirmed
    on a real declaration (Anirudh Singh And Company, ISLD-202106-004,
    scanned/OCR'd): "title" came back as "System which UNITED STATES"
    instead of the real "System which automatically disables or switches
    off a cellphone".
    """
    if not raw:
        return raw
    stripped = _COUNTRY_TAIL_RE.sub("", raw).strip(" ,|-")
    return stripped or raw


_STD_TOKEN_RE = re.compile(r'(?:TS|EN|TR|IEEE|ETSI\s?TS)\s?\d{1,3}[.\s]\d{1,3}(?:[.\s]\d{1,3})?')

# Same shape as _NUMBER_PATTERNS' country-prefixed/PCT/plain-digit alternatives,
# but WITHOUT \b boundaries. \b is a boundary between a word char and a
# non-word char, and both letters and digits are word chars — so when two
# number-shaped tokens are glued together with nothing but more word chars
# between them (e.g. "US202017083068US202017083049...", the real row-11
# bug), there is no internal boundary anywhere in the run and the \b-
# anchored _NUMBER_PATTERNS never fires at all. This variant is only used
# to detect "does this look like contamination", not to extract a single
# clean number, so the lack of boundaries is safe here — findall will
# happily walk across the whole glued run and split it back into its
# individual number-shaped pieces.
_NUMBER_LIKE_NO_BOUNDARY_RE = re.compile(
    r'(?:US|IN|CN|EP|WO|JP|KR|DE|FR|GB)\d{6,12}'
    r'|PCT/[A-Z]{2}\d{4}/\d+'
    r'|\d{9,12}'
)


def _clean_standards_list(std_list: list) -> tuple[list, bool]:
    """
    Same row-dump contamination failure mode as _isolate_number/
    _isolate_applicant, but for the "std" field: instead of stopping at the
    standard code(s) for a row, the model sometimes dumps other rows'
    application/publication numbers into "std" with no separator, e.g.
        ["US202017083068US202017083049US202016951665..."]
    instead of ["TS 38.213"]. This had no isolate/rescue step — std_list
    was passed straight through in slim_to_full — unlike app/pub/applicant.

    Strategy: keep only genuine standard-code-shaped tokens (TS/EN/TR/IEEE
    followed by a version-number-like pattern), extracting multiple
    concatenated codes from one string via findall. Any entry that instead
    matches the application/publication number shape or contains no
    standard-shaped token at all is dropped rather than kept as unreadable
    noise.

    Returns (clean_list, was_contaminated).
    """
    clean: list = []
    contaminated = False
    for raw in std_list:
        raw = str(raw or "").strip()
        if not raw:
            continue
        found = _STD_TOKEN_RE.findall(raw)
        normalized = re.sub(r"\s+", " ", raw)
        if found and len(found) == 1 and re.sub(r"\s+", "", raw) == re.sub(r"\s+", "", found[0]):
            clean.append(normalized)
            continue
        if found:
            contaminated = True
            for f in found:
                f = re.sub(r"\s+", " ", f).strip()
                if f not in clean:
                    clean.append(f)
            continue
        if _NUMBER_LIKE_NO_BOUNDARY_RE.search(raw):
            contaminated = True
            continue
        clean.append(raw)
    return clean, contaminated


def _isolate_applicant(raw: str) -> str:
    """
    Same row-dump contamination failure mode as _isolate_number, but for the
    applicant/holder ("who") field: a small model can spill the rest of the
    row — application number, publication number, country — into this field
    instead of stopping at the applicant name, e.g.
        "Qualcomm Incorporated US202016948557 US2021099988A1 UNITED STATES"
    instead of just "Qualcomm Incorporated". Unlike app/pub numbers, there's
    no separate "applicant" field to fall back on, so this strips rather
    than isolates: cut the string at the first number-shaped token (an
    applicant name never legitimately contains a 9+ digit run, a PCT-style
    path, or a country-code+digits token) or at a trailing country name if
    no number-shaped token was found first.
    """
    if not raw:
        return raw
    raw = raw.strip()
    m = _NUMBER_PATTERNS.search(raw)
    if m and m.start() > 0:
        return raw[:m.start()].strip(" ,|-")
    if m is None:
        return _strip_trailing_country(raw)
    return raw


# ── Minimal JSON schema — only what can't be inferred post-hoc ───────────────
# Keeping this small is critical for speed on 3-4B models.
# research/engine.py adds standard recommendations, versions, clauses, SEP
# classification, similarity scores, and all paper-style outputs automatically.

SLIM_PATENT = {
    "n": 1,                    # disclosure_number
    "std": [],                 # standards e.g. ["TS 38.331", "TS 38.214"]
    "app": "",                 # application_number
    "pub": "",                 # publication_number
    "who": "",                 # applicant/holder
    "cc": "",                  # country code e.g. "INDIA", "UNITED STATES"
    "title": "",               # patent title if present
    "ver": "",                 # version if present e.g. "18.1.0"
    "clause": "",              # clause/section if present e.g. "5.2"
}

SLIM_SCHEMA = {
    "declarant": "",
    "patents": [SLIM_PATENT]
}

SLIM_SCHEMA_STR = json.dumps(SLIM_SCHEMA, separators=(",", ":"))

# ── Full schema (kept for database compatibility) ─────────────────────────────

SCHEMA = {
    "declaration_metadata": {
        "declarant": "",
        "filing_type": "",
        "project": "",
        "overall_technology_domains": [],
        "portfolio_characterization": "",
        "likely_standard_generation": "",
        "confidence": 0.0
    },
    "patent_disclosures": [
        {
            "disclosure_number": 1,
            "explicit_data": {
                "project": "",
                "standards": [],
                "applicant": "",
                "application_number": "",
                "publication_number": "",
                "country": "",
                "title": ""
            },
            "standards_analysis": {
                "3gpp_release_mapping": [{"release": "", "confidence": 0.0, "reasoning": ""}],
                "technical_domains": [{"domain": "", "confidence": 0.0}],
                "network_layer_mapping": [{"layer": "", "confidence": 0.0}],
                "likely_protocol_components": [],
                "standard_descriptions": [{
                    "specification": "",
                    "description": "",
                    "functional_role": "",
                    "technology_area": ""
                }]
            },
            "sep_analysis": {
                "likely_sep_strength": "",
                "probable_implementation_scope": "",
                "implementation_dependency": "",
                "probable_device_impact": [],
                "probable_network_impact": [],
                "estimated_licensing_relevance": "",
                "confidence": 0.0
            },
            "geographic_analysis": {
                "jurisdiction_strength": "",
                "enforcement_significance": "",
                "regional_strategy_inference": ""
            },
            "inferred_fields": {
                "probable_title": "",
                "probable_technology_focus": "",
                "probable_use_cases": [],
                "probable_claim_scope": ""
            }
        }
    ],
    "portfolio_level_analysis": {
        "dominant_technology_domains": [],
        "release_focus": [],
        "most_targeted_specs": [],
        "cross_specification_patterns": [],
        "geographic_filing_strategy": "",
        "portfolio_strength_assessment": "",
        "frand_implications": "",
        "implementation_risk_summary": ""
    }
}

SCHEMA_STR = json.dumps(SCHEMA, separators=(",", ":"))


# ── System Prompts ────────────────────────────────────────────────────────────

# FAST prompt — used for both Quick and Deep modes on small models.
# Extracts only the explicit table data in a tiny schema.
# Post-processing in research/engine.py handles all enrichment.
FAST_SYSTEM_PROMPT = f"""You are an ETSI IPR table extractor.
Extract every patent row from the IPR Information Statement Annex table.

OUTPUT: Return ONLY valid JSON. No markdown. No explanation.
Schema: {SLIM_SCHEMA_STR}

Rules:
- Copy application numbers and standard codes EXACTLY as written — character
  for character, digit for digit. Never "clean up", auto-correct, or
  normalize a number or code toward a more familiar-looking value.
- std = list of standard codes actually printed in the input, e.g. a spec
  series and number in the general shape "TS 38.331" or a project label in
  the general shape "3GPP 5G NR"
- ver = version string if shown, general shape "MAJOR.MINOR.PATCH" e.g.
  "18.1.0", else ""
- clause = clause/section if shown, a short numeric label e.g. "5.2", else ""
- who = applicant name, "" if not shown
- cc = country of registration e.g. "INDIA", "UNITED STATES", "CHINA"
- title = patent title if shown, else ""
- n = the disclosure/row number from the table (1, 2, 3...)
- Include ALL rows, even if some fields are blank
- Input may be structured table text OR natural-language OCR descriptions of a page
- For OCR text: application numbers generally look like a short country-code
  prefix followed by a run of 10-15 digits — transcribe every visible digit
  from the page itself; do not shorten, pad, or "round" the digit string
- For OCR text: standard codes generally look like a spec series (e.g. "TS"
  or "TR") followed by digits, a dot, and more digits, or a 3GPP project
  label — read the digits exactly as printed, never substitute a well-known
  spec number just because the garbled text looks similar to it
- For OCR text: look for country names like "INDIA", "UNITED STATES", "CHINA"
- The formats above describe general shape only, not values to search for —
  none of the example values shown anywhere in this prompt should ever
  appear in your output unless that exact value is genuinely printed in the
  input text
- Each numbered row in the table = one patent entry
- One disclosure can declare against SEVERAL standards at once — its
  "Work Item or Standard No." cell may print multiple spec lines stacked
  vertically (e.g. "TS 101 539-1", "TS 122 173", "TS 124 305" all under one
  disclosure). In OCR'd text this often loses its table alignment: a line
  that contains ONLY a standard/spec code, with no applicant, no
  application/publication number, and no country anywhere near it, is
  virtually always an ADDITIONAL declared standard for the disclosure you
  were already building — add it to that disclosure's "std" list. Do NOT
  start a new patent entry for it. in the JSON

- "std" must contain ONLY standard/spec codes (e.g. "TS 38.331", "EN 301 893",
  a 3GPP project label). NEVER put an application number, publication
  number, or any other digit run into "std" — if you are unsure which
  column a number belongs to, put it in "app" or "pub", never "std". A
  "std" entry that is just a long digit string (with no TS/EN/TR/IEEE
  prefix) is always wrong.

- EP (European) filings often print an application number AND a separately
  formatted granted publication number, and the two look nothing alike —
  the application number is usually a long run of digits following the
  filing date/year (e.g. "EP20150722609"), while the publication number is
  a short run of digits with a kind-code letter+number suffix (e.g.
  "EP3134988 B1" or "EP 3134988 A1"). If BOTH appear in the input for the
  same disclosure, capture both — do not drop the publication number just
  because it doesn't match the application number's digit pattern.

- app vs. pub disambiguation — a genuine publication number (granted or
  published-application) almost always ends in a one-letter-plus-1-2-digit
  "kind code" (e.g. "A1", "B1", "B2", "A9"), because that kind code is what
  makes it a publication number rather than an application number. If a
  row shows only ONE number and it has NO kind-code suffix, that number is
  the application number — put it in "app" and leave "pub" as "". Do NOT
  copy a number into "pub" just because you're unsure which column it came
  from, and NEVER put the exact same value into both "app" and "pub" — a
  disclosure's application and publication numbers are always distinct
  values when both genuinely exist. If the input table's column headers
  ("Application No.", "Publication No.") are visible anywhere in the input,
  trust them over this heuristic; use this heuristic only when the column
  each number belongs to is otherwise ambiguous.

CRITICAL — accuracy over completeness:
- ONLY output a patent entry for a row you can actually see in the input text
- NEVER invent, duplicate, or guess additional rows to "round out" the table
- NEVER repeat the same application number twice
- If any part of an application number, publication number, or standard
  code is smudged, cut off, or otherwise unclear in the source text, do NOT
  guess, complete, or "correct" it to the nearest real-looking number or
  spec you know of. Either transcribe only the characters you can actually
  read, or leave the field as "" if you cannot read it with confidence — a
  blank field is correct, a guessed field is not
- If you are unsure whether two pieces of text are the same row or different
  rows, treat distinct application/publication numbers as distinct rows —
  but never split one row into two, and never output a row with no
  application number AND no publication number unless the original table
  truly leaves both blank
- Before finishing, count the distinct application/publication numbers you
  found in the input and make sure your output has exactly that many entries
  — no more, no fewer"""

FAST_USER_PROMPT = "Extract all patent rows from this ETSI IPR declaration table. Return only JSON."

# Deep prompt — used when the user explicitly wants enrichment AND the model
# is fast enough (i.e. a 7B+ model or GPU with enough VRAM).
DEEP_SYSTEM_PROMPT = f"""You are an expert 3GPP standards engineer and ETSI SEP analyst.
Analyze the ETSI IPR Information Statement Annex.

Reference mapping — applies ONLY to these exact specs when they appear in
the input. Do NOT extend this pattern to any other spec number; if a
declared spec is not in this list, leave its technical-domain/layer mapping
fields empty rather than guessing by analogy:
- TS 38.331=RRC/mobility, TS 38.321=MAC/HARQ, TS 38.214=PHY/MIMO
- TS 38.300=NR architecture, TS 38.423=Xn interface
- TS 23.501=5GC architecture, TS 24.501=NAS, TS 36.xxx=LTE

Grounding rule — applies to every field in the schema:
- Populate a field only if it is directly supported by the input text
  (for explicit_data) or by a defensible technical inference from the specs
  the patent actually declares (for analysis fields).
- If a field cannot be supported this way, output "" for strings, [] for
  lists, and leave its confidence at 0.0 — do not fabricate a plausible-
  sounding value just to fill the schema.
- Every confidence score must reflect how well-supported that specific
  field is. An inferred/reasoning field should generally carry a lower
  confidence than a field copied verbatim from the input — never assign a
  high confidence to a guess.
- Copy application numbers, publication numbers, and standard codes EXACTLY
  as written in the input, character for character. Never guess, complete,
  or "correct" an unclear character toward a familiar-looking number or
  spec — leave the field blank instead.

OUTPUT: Return ONLY valid JSON. No markdown. No explanation.
Schema: {SCHEMA_STR}"""

DEEP_USER_PROMPT = """Extract ALL patent disclosures. For each:
1. Copy all explicit table fields exactly — application/publication numbers
   and standard codes must be transcribed from the input, never guessed or
   auto-corrected
2. Map declared standards to 3GPP releases with confidence scores that
   reflect actual evidentiary support — leave unmapped, with low confidence,
   where the input doesn't clearly support a mapping
3. Identify technical domains (PHY/MAC/RRC/NAS/Core) only where the declared
   specs or explicit text support it; leave blank rather than guessing
4. Assess SEP strength from the specifications actually claimed, not from
   assumptions about the applicant or title
5. Populate portfolio_level_analysis for the whole declaration using only
   patterns actually observed across the extracted disclosures
Return ONLY valid JSON."""

# Quick prompt — alias to fast for backward compatibility
QUICK_SYSTEM_PROMPT = FAST_SYSTEM_PROMPT
QUICK_USER_PROMPT = FAST_USER_PROMPT


# ── Deep enrichment (second pass, batched per-patent) ─────────────────────────
# Deep Mode previously never actually ran DEEP_SYSTEM_PROMPT — analysis.py
# always used FAST_SYSTEM_PROMPT regardless of mode, so slim_to_full() left
# every standards_analysis/sep_analysis/inferred_fields block blank with a
# hardcoded 0.5 confidence placeholder. Two visible symptoms of that:
#   1. "SEP Strength" showed 50% for every single patent.
#   2. research/engine.py's build_patent_text() had nothing but the bare
#      declared standard codes to embed for similarity search, so every
#      patent sharing a standard collapsed onto the same nearest corpus
#      version/clause instead of getting a distinct match.
#
# This prompt fixes both by actually generating the enrichment fields, run
# as a SECOND call over the already-extracted slim patent list (not the raw
# declaration text) so it can't reintroduce the extraction-count problem.
# Batched at ENRICH_BATCH_SIZE patents per call — a 3B model's reliability
# drops off past a handful of patents needing a paragraph of new analysis
# each (unlike slim extraction, where each row is just a few short fields).

ENRICH_BATCH_SIZE = 6

ENRICH_ITEM_SCHEMA = {
    "n": 1,                          # disclosure_number — must match input
    "domains": [],                   # technical_domains, e.g. ["RRC"]
    "components": [],                # likely_protocol_components
    "release": "",                   # best-supported 3GPP release, e.g. "Rel-16"
    "release_confidence": 0.0,
    "strength": "",                  # likely_sep_strength: "Low" | "Medium" | "High"
    "strength_confidence": 0.0,
    "implementation_scope": "",      # probable_implementation_scope
    "licensing_relevance": "",       # estimated_licensing_relevance
    "jurisdiction_strength": "",     # geographic_analysis.jurisdiction_strength
    "technology_focus": "",          # inferred_fields.probable_technology_focus
    "claim_scope": "",               # inferred_fields.probable_claim_scope
}
ENRICH_SCHEMA_STR = json.dumps({"patents": [ENRICH_ITEM_SCHEMA]}, separators=(",", ":"))

DEEP_ENRICH_SYSTEM_PROMPT = f"""You are an expert 3GPP standards engineer and ETSI SEP analyst.
You are given a list of already-extracted patent disclosures (declared
standards, application number, title). For EACH one, produce a distinct
technical/SEP analysis grounded in what that specific patent actually
declares — do not reuse the same wording or the same field values across
different patents unless their declared standards are genuinely identical
AND nothing else (title, etc.) differentiates them.

Reference mapping — applies ONLY to these exact specs when they appear in
the input. Do NOT extend this pattern to any other spec number; if a
declared spec is not in this list, leave its domain/release fields empty
rather than guessing by analogy:
- TS 38.331=RRC/mobility, TS 38.321=MAC/HARQ, TS 38.214=PHY/MIMO
- TS 38.300=NR architecture, TS 38.423=Xn interface
- TS 23.501=5GC architecture, TS 24.501=NAS, TS 36.xxx=LTE

Grounding rule:
- Populate a field only if it is a defensible technical inference from the
  specs this specific patent declares (plus its title, if present). If a
  field cannot be supported this way, output "" / [] / 0.0 — do not
  fabricate a plausible-sounding value just to fill the schema, and do not
  copy another patent's answer just because it declares the same standard.
- strength_confidence and release_confidence must reflect how well the
  input actually supports that specific judgment for that specific patent —
  never assign high confidence to a guess, and never assign the exact same
  confidence to every patent in the batch as a default.

OUTPUT: Return ONLY valid JSON, one entry per input patent, "n" matching
the input disclosure_number exactly. No markdown. No explanation.
Schema: {ENRICH_SCHEMA_STR}"""


def build_enrich_user_prompt(batch: list[dict]) -> str:
    """
    batch: list of {"n": int, "standards": [...], "title": str} for one
    ENRICH_BATCH_SIZE-sized slice of already-extracted patents.
    """
    lines = ["Analyze each of these already-extracted patents:", ""]
    for item in batch:
        std = ", ".join(item.get("standards") or []) or "(none declared)"
        title = item.get("title") or "(no title)"
        lines.append(f"- n={item['n']}: standards=[{std}], title=\"{title}\"")
    lines.append("")
    lines.append("Return ONLY the JSON object described in the schema, one entry per patent above.")
    return "\n".join(lines)


def apply_enrichment(disclosures: list[dict], enrichment_items: list[dict]) -> None:
    """
    Merge enrichment output back into `disclosures` in place, keyed by
    disclosure_number. Only overwrites fields the model actually populated —
    a patent the enrichment pass skipped (e.g. dropped from a malformed
    batch response) keeps its slim_to_full() blanks rather than crashing.
    """
    by_n = {d.get("disclosure_number"): d for d in disclosures}
    for item in enrichment_items:
        disc = by_n.get(item.get("n"))
        if not disc:
            continue

        sa = disc.setdefault("standards_analysis", {})
        if item.get("domains"):
            sa["technical_domains"] = [{"domain": d, "confidence": item.get("release_confidence", 0.0)} for d in item["domains"]]
        if item.get("components"):
            sa["likely_protocol_components"] = item["components"]
        if item.get("release"):
            sa["3gpp_release_mapping"] = [{
                "release": item["release"],
                "confidence": item.get("release_confidence", 0.0),
                "reasoning": f"Inferred from declared standards for disclosure {item['n']}.",
            }]

        sep = disc.setdefault("sep_analysis", {})
        if item.get("strength"):
            sep["likely_sep_strength"] = item["strength"]
        # `is not None` rather than a truthy check: the enrichment prompt's
        # own grounding rule explicitly instructs the model to return 0.0
        # when a judgment isn't well-supported ("do not fabricate... output
        # 0.0"), and 0.0 is falsy in Python — `if item.get("strength_confidence"):`
        # silently discarded exactly the deliberate low-confidence answers
        # this field exists to capture, leaving sep_analysis.confidence at
        # its slim_to_full() None placeholder even though the enrichment
        # pass had already produced a real (if low) confidence value.
        if item.get("strength_confidence") is not None:
            sep["confidence"] = item["strength_confidence"]
        if item.get("implementation_scope"):
            sep["probable_implementation_scope"] = item["implementation_scope"]
        if item.get("licensing_relevance"):
            sep["estimated_licensing_relevance"] = item["licensing_relevance"]

        if item.get("jurisdiction_strength"):
            geo = disc.setdefault("geographic_analysis", {})
            geo["jurisdiction_strength"] = item["jurisdiction_strength"]

        inf = disc.setdefault("inferred_fields", {})
        if item.get("technology_focus"):
            inf["probable_technology_focus"] = item["technology_focus"]
        if item.get("claim_scope"):
            inf["probable_claim_scope"] = item["claim_scope"]


# ── Schema converter — slim → full ────────────────────────────────────────────

def _coerce_str(val) -> str:
    """Coerce any value to a plain string — handles LLM returning lists."""
    if val is None:
        return ""
    if isinstance(val, list):
        return ", ".join(str(v) for v in val if v)
    return str(val)


def _merge_continuation_patents(patents: list) -> list:
    """
    ETSI annex tables let one disclosure declare against MULTIPLE standards
    via a multi-line "Work Item or Standard No." cell, while
    Applicant/Application No./Publication No./Country are printed only ONCE
    (visually top-aligned next to the first standard line). Once a scanned
    filing goes through Tesseract/vision OCR, that vertical alignment is
    lost — OCR just returns flat text — so each extra standard line reads,
    with no other row data at all, as a candidate for its own "patent"
    entry to a model working with no table structure to anchor on. Left
    alone, this turns one real disclosure into N fabricated ones, each
    missing application/publication number, applicant and country.

    Confirmed on a real declaration (Anirudh Singh And Company,
    ISLD-202106-004, fully scanned/rasterized): one 2-row filing across 7
    standards came back as 6 numberless, applicant-less, single-standard
    disclosures, with the second declarant row dropped entirely.

    A "continuation fragment" is any patent entry with no application
    number, publication number, applicant, country, OR title, but at least
    one standard code — it is folded into the immediately preceding entry's
    `std` list instead of kept as a separate disclosure. This runs BEFORE
    any other slim_to_full processing so downstream logic never sees the
    fragments at all.
    """
    merged: list = []
    for p in patents:
        app = str(p.get("app") or "").strip()
        pub = str(p.get("pub") or "").strip()
        who = str(p.get("who") or "").strip()
        cc = str(p.get("cc") or "").strip()
        title = str(p.get("title") or "").strip()
        std = p.get("std") or []
        if isinstance(std, str):
            std = [s.strip() for s in std.split(",") if s.strip()]

        is_continuation = bool(
            merged and std and not app and not pub and not who and not cc and not title
        )
        if is_continuation:
            prev_std = merged[-1].get("std") or []
            if isinstance(prev_std, str):
                prev_std = [s.strip() for s in prev_std.split(",") if s.strip()]
            for s in std:
                if s not in prev_std:
                    prev_std.append(s)
            merged[-1]["std"] = prev_std
        else:
            p = dict(p)
            p["std"] = std
            merged.append(p)
    return merged


def slim_to_full(slim: dict) -> dict:
    """
    Convert the compact fast-extraction schema to the full schema
    expected by the frontend and database layer.
    This runs in <1ms locally — much faster than asking the LLM to
    produce the full schema directly.
    """
    declarant = slim.get("declarant", "")
    patents = _merge_continuation_patents(slim.get("patents") or [])

    # 'std' field contamination is logged as ONE summary warning below,
    # not once per disclosure. At single-declaration scale (tens of rows)
    # a per-row WARNING was harmless noise; at bulk scale -- a 35,000-
    # 50,000+ row ETSI export fed through this same conversion -- a
    # contaminated minority of rows could mean tens of thousands of
    # individual log lines, which is both unreadable and (Python logging
    # isn't free) a real per-row cost that adds up across a large dataset.
    # Same rationale already applied elsewhere in this pipeline for the
    # analogous "many rows share one issue" cases -- see
    # api/routes/cparas/analysis.py's `dropped_as_loop_garbage`/
    # `flagged_collisions` summary warnings.
    contaminated_examples: list[tuple[object, list]] = []
    contaminated_count = 0

    disclosures = []
    for p in patents:
        std_list = p.get("std") or []
        if isinstance(std_list, str):
            std_list = [s.strip() for s in std_list.split(",") if s.strip()]
        std_list, std_contaminated = _clean_standards_list(std_list)
        if std_contaminated:
            contaminated_count += 1
            if len(contaminated_examples) < 5:
                contaminated_examples.append((p.get("n"), std_list))

        app_raw = _coerce_str(p.get("app"))
        pub_raw = _coerce_str(p.get("pub"))
        app_clean, app_contaminated = _isolate_number(app_raw)
        pub_clean, pub_contaminated = _isolate_number(pub_raw)
        app_clean, pub_clean = _reconcile_app_pub(p.get("n"), app_clean, pub_clean)

        title_clean = _strip_trailing_country(_coerce_str(p.get("title")))

        disclosures.append({
            "disclosure_number": p.get("n", len(disclosures) + 1),
            "explicit_data": {
                # The declaration's own project/technology value when the
                # source has one — never a hard-coded "5G" (an LTE TS 36.x
                # disclosure used to be labelled 5G).
                "project": _coerce_str(p.get("project")),
                "standards": std_list,
                "applicant": _isolate_applicant(_coerce_str(p.get("who"))),
                "application_number": app_clean,
                "publication_number": pub_clean,
                "country": _coerce_str(p.get("cc")),
                "title": title_clean,
                "version": _coerce_str(p.get("ver")),
                "clause": _coerce_str(p.get("clause")),
                "_raw_application_number": app_raw if app_contaminated else None,
                "_raw_publication_number": pub_raw if pub_contaminated else None,
            },
            "standards_analysis": {
                "3gpp_release_mapping": [],
                "technical_domains": [],
                "network_layer_mapping": [],
                "likely_protocol_components": [],
                "standard_descriptions": []
            },
            "sep_analysis": {
                "likely_sep_strength": "",
                "probable_implementation_scope": "",
                "implementation_dependency": "",
                "probable_device_impact": [],
                "probable_network_impact": [],
                "estimated_licensing_relevance": "",
                # None (not 0.0/0.5) is deliberate: research/engine.py's
                # _predicted_status() checks `isinstance(sep_conf, (int, float))`
                # to decide whether a real LLM-derived confidence exists to
                # blend in. The fast/slim extraction pass never computes this
                # value, so it must stay None here — a numeric placeholder
                # (previously 0.5) gets treated as "real" signal and blended
                # into every patent's confidence at 35% weight, which is what
                # dragged the whole declaration's confidence scores toward a
                # single ~50% constant regardless of actual similarity.
                "confidence": None
            },
            "geographic_analysis": {
                "jurisdiction_strength": "",
                "enforcement_significance": "",
                "regional_strategy_inference": ""
            },
            "inferred_fields": {
                "probable_title": title_clean,
                "probable_technology_focus": "",
                "probable_use_cases": [],
                "probable_claim_scope": ""
            }
        })

    if contaminated_count:
        logger.warning(
            f"slim_to_full: {contaminated_count} disclosure(s) had a 'std' "
            f"field containing non-standard tokens (likely application/"
            f"publication numbers dumped into the wrong column by the "
            f"extraction model) — cleaned in place. First few "
            f"(disclosure #, cleaned value): {contaminated_examples}"
        )

    return {
        "declaration_metadata": {
            "declarant": declarant,
            "filing_type": "ETSI IPR Declaration",
            "project": "5G",
            "overall_technology_domains": [],
            "portfolio_characterization": "",
            "likely_standard_generation": "5G NR",
            "confidence": 0.9
        },
        "patent_disclosures": disclosures,
        "portfolio_level_analysis": {
            "dominant_technology_domains": [],
            "release_focus": [],
            "most_targeted_specs": list({s for p in patents for s in (p.get("std") or [])}),
            "cross_specification_patterns": [],
            "geographic_filing_strategy": "",
            "portfolio_strength_assessment": "",
            "frand_implications": "",
            "implementation_risk_summary": ""
        }
    }


# ── RAG-Enhanced Prompt Builder ───────────────────────────────────────────────

def build_rag_prompt(user_query: str, retrieved_context: str) -> tuple[str, str]:
    """(system, user) prompt pair for a retrieval-augmented question: the retrieved standards passages
    are placed in the system prompt and the model is told to answer only from them.
    """
    system = f"""You are an expert SEP analyst with access to a telecom standards knowledge base.

Retrieved context:
---
{retrieved_context}
---

Use this context to enhance your analysis.
{DEEP_SYSTEM_PROMPT}"""
    return system, user_query


# ── Fine-tuning helpers ───────────────────────────────────────────────────────

FINETUNE_SYSTEM_PROMPT = "You are a telecom SEP analyst. Analyze the ETSI IPR declaration and return structured JSON."

def build_training_example(document_text: str, expected_json: dict) -> dict:
    """One instruction-tuning record (instruction / input / output) from a declaration text and its
    expected JSON - for building a fine-tuning set offline; not used at runtime.
    """
    return {
        "instruction": FINETUNE_SYSTEM_PROMPT,
        "input": f"Analyze this ETSI IPR declaration:\n\n{document_text}",
        "output": json.dumps(expected_json, indent=2)
    }

