"""
ETSI Bulk Declaration Export — Column Schema
=============================================
Column layout for the ETSI IPR-database bulk export workbook format (the
"Data_For_Testing.xlsx" shape): one row per patent-family member declared
against one or more standards, as opposed to the single-declaration-PDF
table shape the existing `utils/csv_parser.py` + `utils/xlsx_parser.py`
pipeline was built for.

This is a thin, explicit adapter — like `utils/xlsx_parser.py` is a format
adapter over `utils/csv_parser.py` — so the bulk-export path can reuse the
same underlying openpyxl/xlrd readers without forcing this very different
column layout through the fuzzy single-declaration header matcher (which
expects one applicant/declarant per sheet, not one per row).

HEADER RESOLUTION — exact-alias-first, fuzzy fallback
--------------------------------------------------------
Real bulk exports don't all use identical column names (two observed
variants already differ on ~5 columns — see CHANGELOG.md V0.3). Rather
than hard-coding every future spelling as another literal dict entry,
`resolve_header()` below matches any header string against `FIELD_ALIASES`
in two passes:

  1. Exact match after normalization (case/punctuation/whitespace
     differences only) — this is what handles every header spelling
     that's already been seen, including both known real exports.
  2. Token-overlap fuzzy match against the same alias list, for a header
     spelling that's never been seen before (e.g. "Applicant Name" for a
     workbook that calls it something other than "COMP_LEGAL_NAME" or
     "Company"). Deliberately conservative: requires a good token-overlap
     score AND an unambiguous single best-scoring field, so an unrelated
     or genuinely ambiguous header resolves to "no match" rather than a
     wrong field — a wrong silent mapping is worse than an unmapped
     column (the same design principle `utils/csv_parser.py`'s
     single-declaration header matcher already follows).

`COLUMN_MAP` (the original explicit exact-name table) is kept as-is for
backward compatibility with any code that reads it directly, and doubles
as the seed/backbone of `FIELD_ALIASES` below — every entry in it is
included as an alias automatically, so nothing already recognised
regresses.
"""

from __future__ import annotations

import re

# Columns that must ALL be present (case-insensitive, exact) for a sheet to
# be treated as an ETSI bulk declaration export rather than falling back to
# the generic single-declaration parser.
#
# Superseded by `looks_like_bulk_header()`'s semantic (fuzzy-resolved)
# check in parser.py, which is what's actually used for detection now —
# kept here only as a documented, human-readable "what this format is
# fundamentally about" reference and for any external code that imports
# it directly. See that function's docstring for the real detection logic.
#
# IPRD_ID was originally in this list but is dropped: it is mapped by
# COLUMN_MAP purely as a passthrough field that nothing downstream (DB
# model, scoring, excel export) ever reads, yet gating format-detection on
# it meant a workbook that carries every field actually used by the
# pipeline — just identified by IPRD_REFERENCE instead of a bare IPRD_ID —
# was rejected outright (see the "UIUX database" export variant, which has
# no IPRD_ID column at all). COMP_LEGAL_NAME replaces it: present in every
# known export variant and, unlike IPRD_ID, actually used (company facet
# throughout the dashboard), so it's a strictly better detection signal.
REQUIRED_COLUMNS = [
    "DIPG_DISPLAY_NUMBER",
    "PATT_APPLICATION_NUMBER",
    "COMP_LEGAL_NAME",
]

# Full known column set -> normalized field name used internally.
#
# Some fields have more than one observed header spelling across real ETSI
# bulk-export variants (e.g. the "UIUX database" export uses "Title" and
# "Legal_Status" where the original reference export uses "PBPA_TITLEEN"
# and "Legal Status"). Both spellings map to the same internal field name
# so a row's data lands in the same place regardless of which export
# variant produced it.
#
# For a header spelling that ISN'T listed here at all, use
# `resolve_header()` below rather than this dict directly — it falls
# through to fuzzy matching instead of returning None.
COLUMN_MAP: dict[str, str] = {
    "IPRD_ID": "iprd_id",
    "IPRD_REFERENCE": "iprd_reference",
    "IPRD_SIGNATURE_DATE": "signature_date",
    "Reflected_Date": "reflected_date",
    "COMP_LEGAL_NAME": "company",
    "DECL_IS_PROP_FLAG": "decl_is_prop_flag",
    "LICD_DEC_PREP_TO_GRANT_FLAG": "licd_dec_prep_to_grant_flag",
    "LICD_REC_CONDI_FLAG": "licd_rec_condi_flag",
    "DIPG_ID": "dipg_id",
    "DIPG_DISPLAY_NUMBER": "disclosure_number",
    "DIPG_EXTERNAL_ID": "dipg_external_id",
    "Standard": "standard_raw",
    "Ess_To_Standard": "ess_to_standard",
    "WI_Type": "wi_type",
    "WOIT_ETSI_DELIVERABLE_NUMBER": "etsi_deliverable_number",
    "WOIT_REFERENCE": "woit_reference",
    "WOIT_VERSION": "woit_version",
    "3GPP_Type": "gpp_type",
    "TGPP_NUMBER": "spec_number",
    "TGPV_VERSION": "spec_version",
    "Patent_Type": "patent_type",
    "PATT_ID": "patt_id",
    "PATT_APPLICATION_NUMBER": "application_number",
    "PUBL_NUMBER": "publication_number",
    "PBPA_TITLEEN": "title",
    "Title": "title",  # alt spelling (UIUX database export)
    "PBPA_PRIORITY_NUMBERS": "priority_numbers",
    "Country_Of_Registration": "country_raw",
    "PBPA_APP_DATE": "filing_date",
    "ETPR_ID": "etpr_id",
    "ETPR_ACRONYM": "project_acronym",
    "Ess_To_Project": "ess_to_project",
    "Illustrative_Part": "illustrative_part",
    "DIPG_PATF_ID": "family_id",
    "Original_Application_Number": "original_application_number",
    "Original_Publication_Number": "original_publication_number",
    "Explicitely_Disclosed": "explicit_disclosure",
    "Normalized_Patent": "normalized_patent",
    "2G": "gen_2g",
    "3G": "gen_3g",
    "4G": "gen_4g",
    "5G": "gen_5g",
    "6G": "gen_6g",
    "Legal Status": "legal_status",
    "Legal_Status": "legal_status",  # alt spelling (UIUX database export)
    "STD_URL": "standard_url",
    "PUB_URL": "publication_url",
}

# Extra alias phrases per field — plausible header spellings that haven't
# actually been seen in a real export yet, added so a genuinely new
# workbook has a fair chance of being read correctly on first upload
# instead of needing a code change first. These are hand-picked common
# IPR/patent-database naming conventions, not guesses at any specific
# vendor's schema.
_EXTRA_ALIASES: dict[str, list[str]] = {
    "company": ["Company", "Company Name", "Applicant", "Declarant", "Owner",
                "Assignee", "Patent Holder", "Patentee", "Legal Name", "Holder"],
    "disclosure_number": ["Disclosure Number", "Disclosure No", "Declaration Number",
                           "IPR Disclosure Number", "Declaration No"],
    "application_number": ["Application Number", "Application No", "App No",
                            "Patent Application Number", "Applic Number"],
    "publication_number": ["Publication Number", "Publication No", "Pub No"],
    "title": ["Patent Title", "Invention Title"],
    "legal_status": ["Patent Status", "Status"],
    "standard_raw": ["Declared Standard", "Standard Name"],
    "spec_number": ["Spec Number", "Specification Number", "TS Number",
                     "3GPP Spec Number"],
    "spec_version": ["Spec Version", "Specification Version", "TS Version"],
    "illustrative_part": ["Illustrative Part", "Claim", "Clause"],
    "explicit_disclosure": ["Explicitly Disclosed", "Explicit Disclosure"],
    "country_raw": ["Country of Registration", "Country"],
    "filing_date": ["Filing Date", "Application Date", "Priority Date"],
    "iprd_reference": ["Declaration Reference", "ISLD Reference"],
    "signature_date": ["Signature Date"],
    "family_id": ["Family ID", "Patent Family ID"],
    "project_acronym": ["Project Acronym", "Project"],
    "publication_url": ["Publication URL", "Patent URL"],
    "standard_url": ["Standard URL", "Spec URL"],
}


def _normalize_header(s: str) -> str:
    """Lowercase, collapse separators (_/-.) and punctuation to spaces,
    collapse whitespace. Deliberately simple/deterministic — no stemming
    here, so this is only used for the exact-alias-match pass; fuzzy
    matching below applies its own light stemming to the tokens."""
    s = str(s or "").lower()
    s = re.sub(r"[_\-/.]+", " ", s)
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


_STOPWORDS = {"of", "the", "or", "and", "for"}


def _stem(token: str) -> str:
    """Very light plural stemming for header matching ('numbers' -> 'number')."""
    if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def _tokens(s: str) -> frozenset[str]:
    """Normalised, stemmed, stopword-free token set of a header for fuzzy matching."""
    return frozenset(_stem(t) for t in _normalize_header(s).split() if t not in _STOPWORDS)


def _build_field_aliases() -> dict[str, list[str]]:
    """Field name -> every header spelling that maps to it (COLUMN_MAP plus the extra aliases)."""
    aliases: dict[str, list[str]] = {}
    for header, field in COLUMN_MAP.items():
        aliases.setdefault(field, []).append(header)
    for field, extra in _EXTRA_ALIASES.items():
        aliases.setdefault(field, []).extend(extra)
    return aliases


FIELD_ALIASES: dict[str, list[str]] = _build_field_aliases()

# normalized alias string -> field, for the exact-match pass.
_EXACT_LOOKUP: dict[str, str] = {}
for _field, _aliases in FIELD_ALIASES.items():
    for _alias in _aliases:
        _EXACT_LOOKUP[_normalize_header(_alias)] = _field

# field -> list of alias token sets, for the fuzzy pass.
_FIELD_ALIAS_TOKENS: dict[str, list[frozenset[str]]] = {
    field: [_tokens(a) for a in aliases] for field, aliases in FIELD_ALIASES.items()
}

_FUZZY_THRESHOLD = 0.6


def _fuzzy_resolve(header: str) -> str | None:
    """Token-overlap match (F1-style: harmonic mean of how much of the
    header the alias explains, and how much of the alias the header
    covers) against every known field's alias set. Returns the single
    best-scoring field if it clears `_FUZZY_THRESHOLD` and isn't tied
    with a different field — an ambiguous or weak match returns None
    rather than guessing."""
    h_tokens = _tokens(header)
    if not h_tokens:
        return None

    best_field, best_score = None, 0.0
    tie = False
    for field, alias_token_sets in _FIELD_ALIAS_TOKENS.items():
        field_best = 0.0
        for a_tokens in alias_token_sets:
            if not a_tokens:
                continue
            inter = h_tokens & a_tokens
            if not inter:
                continue
            containment = len(inter) / len(a_tokens)
            precision = len(inter) / len(h_tokens)
            score = 2 * containment * precision / (containment + precision)
            if score > field_best:
                field_best = score
        if field_best > best_score:
            best_field, best_score, tie = field, field_best, False
        elif field_best == best_score and field_best > 0 and field != best_field:
            tie = True

    if best_field is None or best_score < _FUZZY_THRESHOLD or tie:
        return None
    return best_field


def resolve_header(header) -> str | None:
    """Map any raw header cell value to an internal field name, or None
    if it doesn't recognizably correspond to one. Exact-normalized match
    first (fast, unambiguous, covers every spelling already seen);
    token-overlap fuzzy match as a fallback for a spelling that's never
    been seen before."""
    if header is None:
        return None
    norm = _normalize_header(header)
    if not norm:
        return None
    exact = _EXACT_LOOKUP.get(norm)
    if exact:
        return exact
    return _fuzzy_resolve(header)


# Jurisdiction names as they actually appear in ETSI bulk declaration
# exports -> the code the dashboard groups and labels by. Before this
# table, `country_code` just took the first whitespace token and truncated
# it to three characters, which assumed every cell looked like
# "US UNITED STATES". The shipped sample export does NOT: it carries
# "UNITED STATES" and "EUROPEAN PATENT OFFICE", which became "UNI" and
# "EUR" -- so the SEPs-by-country donut and the country filter were keyed
# on fragments of words, and a file mixing "US" with "UNITED STATES"
# produced two separate buckets for one jurisdiction.
_COUNTRY_NAME_TO_CODE: dict[str, str] = {
    "united states": "US", "united states of america": "US", "usa": "US",
    "european patent office": "EP", "european patent organisation": "EP",
    "european patent organization": "EP", "epo": "EP", "europe": "EP",
    "united kingdom": "GB", "great britain": "GB",
    "germany": "DE", "federal republic of germany": "DE",
    "france": "FR", "italy": "IT", "spain": "ES", "netherlands": "NL",
    "sweden": "SE", "finland": "FI", "denmark": "DK", "norway": "NO",
    "switzerland": "CH", "austria": "AT", "belgium": "BE", "ireland": "IE",
    "poland": "PL", "portugal": "PT", "greece": "GR", "turkey": "TR",
    "russia": "RU", "russian federation": "RU",
    "china": "CN", "peoples republic of china": "CN",
    "people s republic of china": "CN", "hong kong": "HK", "taiwan": "TW",
    "japan": "JP", "india": "IN", "australia": "AU", "new zealand": "NZ",
    "canada": "CA", "mexico": "MX", "brazil": "BR", "argentina": "AR",
    "south korea": "KR", "korea": "KR", "republic of korea": "KR",
    "korea republic of": "KR", "singapore": "SG", "malaysia": "MY",
    "indonesia": "ID", "thailand": "TH", "vietnam": "VN", "viet nam": "VN",
    "israel": "IL", "south africa": "ZA", "egypt": "EG",
    "saudi arabia": "SA", "united arab emirates": "AE",
    "world intellectual property organization": "WO",
    "world intellectual property organisation": "WO", "wipo": "WO", "pct": "WO",
    "eurasian patent organization": "EA", "eurasian patent office": "EA",
    "african regional intellectual property organization": "AP",
    "african intellectual property organization": "OA",
}

_CODE_RE = re.compile(r"^[A-Z]{2}$")


def country_code(raw: str | None) -> str:
    """ISO-2-style jurisdiction code for a Country_Of_Registration cell.

    Handles, in order: a bare code ("US", "GB"); the
    "<code> <NAME>" form ("US UNITED STATES"); a full jurisdiction name
    ("UNITED STATES", "EUROPEAN PATENT OFFICE") via
    `_COUNTRY_NAME_TO_CODE`. An unrecognised value is returned normalised
    and intact (upper-cased, whitespace collapsed) rather than silently
    truncated to its first three letters -- a value the dashboard shows
    verbatim is better than a fragment that looks like a code but isn't.
    """
    if not raw:
        return ""
    text = re.sub(r"\s+", " ", str(raw).replace(",", " ")).strip()
    if not text:
        return ""
    upper = text.upper()

    if _CODE_RE.match(upper):
        return upper

    key = re.sub(r"[^a-z0-9 ]+", " ", text.lower())
    key = re.sub(r"\s+", " ", key).strip()
    if key in _COUNTRY_NAME_TO_CODE:
        return _COUNTRY_NAME_TO_CODE[key]

    # "US UNITED STATES" / "DE GERMANY": a leading 2-letter code wins, but
    # only when what follows also names a jurisdiction (so a genuine
    # two-word name is not mistaken for code + name).
    parts = upper.split(" ")
    if len(parts) > 1 and _CODE_RE.match(parts[0]):
        rest = " ".join(key.split(" ")[1:])
        if rest in _COUNTRY_NAME_TO_CODE or not rest:
            return parts[0]
        return parts[0]

    return upper


LEGACY_STATUSES = {"expired - fee related", "expired - lifetime", "expired", "ceased"}
ACTIVE_STATUSES = {"active", "pending"}
