"""JSON parsing utilities — robust extraction from LLM output."""
import difflib
import json
import re
import logging

logger = logging.getLogger(__name__)

# Default similarity ratio (difflib.SequenceMatcher.ratio(), 0-1) above which
# a fuzzy match is accepted as "this code is genuinely in the source text,
# just OCR-mangled" rather than "the LLM invented this". Calibrated so a
# single-character OCR slip on a typical 7-20 char application/standard code
# still passes, while a wholesale fabrication (different digits throughout)
# does not. See verify_code_in_text() for the full matching strategy.
DEFAULT_FUZZY_THRESHOLD = 0.85


def _strip_code_fences(text: str) -> str:
    """
    Return the contents of the first ``` fenced block, or the text unchanged.

    The previous implementation ran two *global* substitutions
    (`re.sub(r"```json\\s*", ...)` and `re.sub(r"```\\s*", ...)`) across the
    whole response. That deletes backticks wherever they appear, including
    inside JSON string values — so a title like
        "Encoding using ```delta``` frames"
    silently parsed as "Encoding using deltaframes". Silent corruption of a
    field that then flows into the database and the report is worse than a
    parse failure, because nothing surfaces it.
    """
    m = re.search(r"```(?:json)?\s*\n?(.*?)```", text, flags=re.DOTALL | re.IGNORECASE)
    if m:
        return m.group(1).strip()
    # Unterminated fence (generation cut off before the closing ```).
    m = re.match(r"\s*```(?:json)?\s*\n?(.*)", text, flags=re.DOTALL | re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return text.strip()


def _decode_first_value(text: str, array_key: str) -> dict | None:
    """
    Decode the first complete JSON value in `text`, whether it is an object
    or an array.

    Handling the array case matters: the extraction prompts ask for an object,
    but models routinely answer with a bare top-level array instead. The old
    code searched only for the first "{", which inside "[{...},{...}]" is the
    first *element* — so a 40-patent response returned as an array silently
    yielded exactly one patent, with no error anywhere.
    """
    obj_at = text.find("{")
    arr_at = text.find("[")
    candidates = [i for i in (obj_at, arr_at) if i != -1]
    if not candidates:
        return None

    decoder = json.JSONDecoder()
    for idx in sorted(candidates):
        try:
            value, _ = decoder.raw_decode(text, idx=idx)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
        if isinstance(value, list):
            # Wrap so callers keep their existing `parsed.get(...)` contract.
            # "patents" is the slim-schema key both call sites already handle
            # (see slim_to_full in core/prompts.py).
            return {array_key: value}
    return None


def extract_json_safe(raw: str, array_key: str = "patents") -> dict | None:
    """
    Extract JSON from LLM output that may contain markdown fences,
    leading text, or trailing commentary.

    A bare top-level array is wrapped as {array_key: [...]} so callers get a
    dict either way.
    """
    if not raw:
        return None

    # Try the untouched text first. Fence-stripping is lossy, so it must be a
    # fallback rather than an unconditional preprocessing step.
    for cleaned in (raw.strip(), _strip_code_fences(raw)):
        if not cleaned:
            continue
        value = _decode_first_value(cleaned, array_key)
        if value is not None:
            return value

    # Last resort: first-brace-to-last-brace slice with trailing-comma repair,
    # for objects that need the comma fix before they will parse at all.
    cleaned = _strip_code_fences(raw)
    first = cleaned.find("{")
    last = cleaned.rfind("}")
    if first == -1 or last <= first:
        logger.warning("No complete JSON object found in LLM output")
        return None

    json_str = cleaned[first:last + 1]
    try:
        return json.loads(json_str)
    except json.JSONDecodeError as e:
        # NOTE: this substitution is not string-literal aware, so it is only
        # ever attempted after the well-formed paths above have failed.
        fixed = re.sub(r",\s*([}\]])", r"\1", json_str)
        try:
            return json.loads(fixed)
        except json.JSONDecodeError:
            logger.warning(f"JSON parse failed even after fixing: {e}")
            return None


def _find_balanced_objects(text: str, start_idx: int) -> list:
    """
    Scan forward from start_idx and return every top-level balanced {...}
    substring found, correctly tracking string/escape state so braces inside
    quoted strings don't throw off the depth count. An object that never
    closes (the generation was cut off mid-object) is dropped rather than
    included broken.
    """
    objects = []
    i = start_idx
    n = len(text)
    while i < n:
        if text[i] == '{':
            depth = 0
            j = i
            in_string = False
            escape = False
            start = i
            closed = False
            while j < n:
                ch = text[j]
                if escape:
                    escape = False
                elif ch == '\\' and in_string:
                    escape = True
                elif ch == '"':
                    in_string = not in_string
                elif not in_string:
                    if ch == '{':
                        depth += 1
                    elif ch == '}':
                        depth -= 1
                        if depth == 0:
                            objects.append(text[start:j + 1])
                            i = j + 1
                            closed = True
                            break
                j += 1
            if not closed:
                break  # ran off the end without closing — incomplete tail, stop
        else:
            i += 1
    return objects


def salvage_partial_array(raw: str, array_key: str = "patents") -> dict | None:
    """
    Last-resort recovery for a response that broke partway through a long
    array (e.g. a syntax error around patent #38 of 40 in a single very long
    generation). Rather than discarding the entire result, this extracts
    every individually well-formed {...} object inside the named array and
    returns whatever could be salvaged, plus the declarant/metadata fields
    if those parsed correctly before the break.

    This only kicks in after extract_json_safe has already failed — it's a
    deliberately narrower, more defensive parse than the main path.
    """
    cleaned = re.sub(r"```json\s*", "", raw, flags=re.IGNORECASE)
    cleaned = re.sub(r"```\s*", "", cleaned)

    key_idx = cleaned.find(f'"{array_key}"')
    if key_idx == -1:
        # try the full-schema key name too
        key_idx = cleaned.find('"patent_disclosures"')
        array_key = "patent_disclosures"
        if key_idx == -1:
            return None

    array_start = cleaned.find('[', key_idx)
    if array_start == -1:
        return None

    objs_raw = _find_balanced_objects(cleaned, array_start)
    salvaged = []
    for o in objs_raw:
        try:
            salvaged.append(json.loads(o))
        except json.JSONDecodeError:
            continue  # skip the one broken object, keep the rest

    if not salvaged:
        return None

    logger.warning(
        f"Salvaged {len(salvaged)} individually well-formed object(s) from "
        f"a malformed '{array_key}' array (the LLM output broke partway "
        f"through generation — this is the partial result recovered before "
        f"the break point, not necessarily the full table)."
    )

    # Try to also recover a declarant name if present before the break
    declarant_match = re.search(r'"declarant"\s*:\s*"([^"]*)"', cleaned[:array_start])
    result = {array_key: salvaged}
    if declarant_match:
        result["declarant"] = declarant_match.group(1)
    # ISSUE 5: a salvage recovery is, by definition, an incomplete
    # extraction — the generation broke partway through. Every previous
    # caller treated a successful salvage identically to a clean parse, with
    # nothing distinguishing "got all 40 patents" from "got 12 of 40 before
    # the response cut off" — that is the silent partial-acceptance this
    # flag exists to close off. Callers MUST check `_partial`/`_salvaged_count`
    # before treating this result as a complete extraction (see
    # _analyze_in_chunks' retry-on-incomplete logic in api/routes/analysis.py).
    result["_partial"] = True
    result["_salvaged_count"] = len(salvaged)
    return result


def _normalize_code(s: str) -> str:
    """
    Collapse whitespace/dashes/dots/underscores/slashes and uppercase, so
    pure formatting differences ('IN 2021 41005326' vs 'IN202141005326',
    'TS-38.331' vs 'TS 38.331') don't cause a false verification failure.
    """
    if not s:
        return ""
    return re.sub(r"[\s\-_./]", "", str(s).upper())


_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9./\-]*")


def build_candidate_index(source_text: str, max_join: int = 3) -> list:
    """
    Tokenize `source_text` once into candidate code/number strings, for use
    as the fuzzy-match search space in verify_code_in_text(). Splitting into
    tokens (rather than sliding a window over every character position) is
    what keeps this fast on a full declaration: comparisons only happen
    against word-like chunks that actually appear in the text, not every
    possible substring.

    Also produces short joined n-grams (2-3 consecutive tokens), because a
    standard code is often typeset/OCR'd with an internal space between the
    prefix and number ('TS' + '38.331'), or a long number can wrap across a
    line break and get tokenized in two pieces.

    Callers analyzing many fields against the same document (e.g. every
    disclosure in one declaration) should build this once and pass it to
    verify_code_in_text()/verify_patent_fields() via the `candidates` /
    `candidate_index` argument, instead of re-tokenizing per field.
    """
    if not source_text:
        return []
    raw_tokens = _TOKEN_RE.findall(source_text)
    candidates = list(raw_tokens)
    for n in range(2, max_join + 1):
        for i in range(len(raw_tokens) - n + 1):
            candidates.append("".join(raw_tokens[i:i + n]))
    return candidates


def default_fuzzy_threshold_for(is_ocr: bool) -> float:
    """
    ISSUE 1 requirement: fuzzy verification must not be gated on "is this
    document's text OCR or not" — a document can be a native-text-layer PDF
    and still fail exact match for reasons that have nothing to do with OCR
    noise (a wrapped identifier the caller forgot to reconstruct, a
    hyphenation variant, a stray control character). Turning fuzzy matching
    off entirely for native text traded a real bug (correct wrapped
    identifiers rejected) for a blunt fix (no safety net at all outside OCR).

    The actual OCR-vs-native distinction that matters is RISK TOLERANCE, not
    an on/off switch: fuzzy matching's failure mode is accepting a
    fabricated code that merely resembles a real one (measured on a real
    declaration: a single-digit-different fabrication scored 0.875 against
    the real number). OCR noise justifies tolerating that risk down to
    ~0.85; a native text layer has no OCR noise to excuse a near-miss, so it
    keeps fuzzy matching available (as a safety net for wrap/hyphenation
    artifacts reconstruction didn't catch) but at a much stricter bar.
    """
    return DEFAULT_FUZZY_THRESHOLD if is_ocr else 0.93


def verify_code_in_text(code: str, source_text: str, *,
                         fuzzy_threshold: float = DEFAULT_FUZZY_THRESHOLD,
                         candidates: list = None, allow_fuzzy: bool = True) -> dict:
    """
    Check whether `code` — an application number, publication number, or
    declared standard code (e.g. 'IN202141005326', 'TS 38.331') — is
    actually present in `source_text`, instead of just trusting whatever the
    LLM extracted. This is the missing safety net: previously nothing ever
    checked an extracted field against the source document at all.

    ISSUE 10 (API redesign): `fuzzy_threshold`, `candidates`, and
    `allow_fuzzy` are now keyword-only (note the bare `*` after
    `source_text`). The previous all-positional-or-keyword signature meant a
    caller could pass a threshold where a candidate list was intended (both
    are optional and easy to reorder by accident) and get a wrong-but-not-
    obviously-wrong result instead of a clear error. Keyword-only forces
    every call site to name what it's passing.

    IMPORTANT: `source_text` should already have gone through
    utils.text_repair.reconstruct_all() before it reaches this function.
    Verification only looks at literal substrings of `source_text` — if a
    document's wrapped identifiers haven't been reconstructed yet, a "not
    found" result here may just mean the correct value is split across a
    line break in `source_text`, not that it's fabricated (see ISSUE 1's
    root cause). Reconstruction is a document-level, one-time operation;
    callers verifying many fields against the same document (e.g. every
    disclosure in a declaration) should run it once on the whole extracted
    text (see api/routes/analysis.py) rather than per-field here.

    `allow_fuzzy` should now almost always be True (see
    `default_fuzzy_threshold_for()` for how to size `fuzzy_threshold`
    appropriately instead of gating fuzzy matching on/off by extraction
    tier) — fuzzy matching is a bounded safety net for noise reconstruction
    didn't catch, not the primary correctness mechanism, which is now
    reconstruction + exact match.

    Raises:
        TypeError: if `fuzzy_threshold` isn't numeric, or `candidates` is
            neither `None` nor a list.
        ValueError: if `fuzzy_threshold` is outside `[0.0, 1.0]`.

    Returns:
        {
          "verified": bool,
          "match_type": "exact" | "fuzzy" | "not_found",
          "similarity": float,           # 1.0 for exact, else best ratio found
          "matched_snippet": str | None, # what actually matched, for audit
        }

    Strategy:
      1. Normalize both `code` and `source_text` (strip spaces/dashes/dots,
         uppercase) and look for an exact substring match. This alone
         resolves the overwhelming majority of "false mismatches", which are
         just formatting differences, not fabrication.
      2. If no exact hit and `allow_fuzzy` is True, fall back to a fuzzy
         search over tokenized candidates from the source text (see
         build_candidate_index), scored with difflib. This catches genuine
         OCR corruption (e.g. a scanned 'TS 38.331' rendered as 'TS 3B.33l')
         without being so loose that a wholesale fabrication can slip
         through — anything below `fuzzy_threshold` is rejected, and if
         `allow_fuzzy` is False this step is skipped entirely.
    """
    if not isinstance(fuzzy_threshold, (int, float)) or isinstance(fuzzy_threshold, bool):
        raise TypeError(f"fuzzy_threshold must be a number, got {type(fuzzy_threshold).__name__}")
    if not (0.0 <= float(fuzzy_threshold) <= 1.0):
        raise ValueError(f"fuzzy_threshold must be within [0.0, 1.0], got {fuzzy_threshold!r}")
    if candidates is not None and not isinstance(candidates, list):
        raise TypeError(f"candidates must be a list[str] or None, got {type(candidates).__name__}")

    not_found = {"verified": False, "match_type": "not_found", "similarity": 0.0, "matched_snippet": None}

    if not code or not source_text:
        return dict(not_found)

    norm_code = _normalize_code(code)
    if not norm_code:
        return dict(not_found)

    norm_text = _normalize_code(source_text)
    if norm_code in norm_text:
        return {"verified": True, "match_type": "exact", "similarity": 1.0, "matched_snippet": code}

    if not allow_fuzzy:
        return dict(not_found)

    if candidates is None:
        candidates = build_candidate_index(source_text)

    best_ratio, best_snippet = 0.0, None
    len_slack = max(3, len(norm_code) // 3)
    for cand in candidates:
        norm_cand = _normalize_code(cand)
        if not norm_cand or abs(len(norm_cand) - len(norm_code)) > len_slack:
            continue  # cheap pre-filter before the more expensive ratio() call
        ratio = difflib.SequenceMatcher(None, norm_code, norm_cand).ratio()
        if ratio > best_ratio:
            best_ratio, best_snippet = ratio, cand

    if best_ratio >= fuzzy_threshold:
        return {
            "verified": True,
            "match_type": "fuzzy",
            "similarity": round(best_ratio, 3),
            "matched_snippet": best_snippet,
        }
    return {
        "verified": False,
        "match_type": "not_found",
        "similarity": round(best_ratio, 3),
        "matched_snippet": best_snippet,
    }


def verify_patent_fields(explicit_data: dict, source_text: str, *,
                          fuzzy_threshold: float = DEFAULT_FUZZY_THRESHOLD,
                          candidate_index: list = None, allow_fuzzy: bool = True) -> dict:
    """
    Run verify_code_in_text() over every field on a single patent disclosure
    that's prone to hallucination: application_number, publication_number,
    and each entry in the declared `standards` list. This is what was
    missing from the pipeline — the only existing check compared aggregate
    counts and never looked at individual fields, and never touched
    standards at all.

    ISSUE 10: `fuzzy_threshold`, `candidate_index`, and `allow_fuzzy` are
    keyword-only, for the same reason as verify_code_in_text().

    `source_text` should already be text-repaired (see
    utils.text_repair.reconstruct_all) before it reaches this function —
    see verify_code_in_text's docstring for why.

    `candidate_index` should be build_candidate_index(source_text) computed
    once per document by the caller (e.g. in the loop over all disclosures
    in a declaration) so it isn't rebuilt from scratch for every patent.

    `allow_fuzzy`/`fuzzy_threshold`: now that identifiers are reconstructed
    before verification, `allow_fuzzy` should almost always be True
    regardless of extraction tier — use
    `default_fuzzy_threshold_for(is_ocr=...)` to size `fuzzy_threshold`
    appropriately instead of toggling `allow_fuzzy` on/off by tier (see
    verify_code_in_text's docstring for the full rationale; this replaces
    the previous "fuzzy only for OCR" behavior called out in ISSUE 1).

    Returns:
        {
          "application_number": {..verify_code_in_text result..} | None,
          "publication_number": {..verify_code_in_text result..} | None,
          "standards": [ {..verify_code_in_text result.., "value": str}, ... ],
          "all_verified": bool,   # True if every checked field was verified
          "any_checked": bool,    # False if there was nothing to check
        }
    """
    if candidate_index is None:
        candidate_index = build_candidate_index(source_text)

    result = {
        "application_number": None,
        "publication_number": None,
        "standards": [],
        "all_verified": True,
        "any_checked": False,
    }

    app_no = explicit_data.get("application_number")
    if app_no:
        r = verify_code_in_text(app_no, source_text, fuzzy_threshold=fuzzy_threshold,
                                 candidates=candidate_index, allow_fuzzy=allow_fuzzy)
        result["application_number"] = r
        result["any_checked"] = True
        result["all_verified"] = result["all_verified"] and r["verified"]

    pub_no = explicit_data.get("publication_number")
    if pub_no:
        r = verify_code_in_text(pub_no, source_text, fuzzy_threshold=fuzzy_threshold,
                                 candidates=candidate_index, allow_fuzzy=allow_fuzzy)
        result["publication_number"] = r
        result["any_checked"] = True
        result["all_verified"] = result["all_verified"] and r["verified"]

    for std in (explicit_data.get("standards") or []):
        if not std:
            continue
        r = verify_code_in_text(std, source_text, fuzzy_threshold=fuzzy_threshold,
                                 candidates=candidate_index, allow_fuzzy=allow_fuzzy)
        r = dict(r, value=std)
        result["standards"].append(r)
        result["any_checked"] = True
        result["all_verified"] = result["all_verified"] and r["verified"]

    return result


_COMPLETENESS_WEIGHTS = {
    "application_number": 0.30,
    "title": 0.20,
    "applicant": 0.20,
    "standards": 0.20,
    "publication_number": 0.05,
    "country": 0.05,
}
COMPLETENESS_FLAG_THRESHOLD = 0.6


def _field_present(value) -> bool:
    """Whether an extracted field actually has a value (non-empty string/collection, not an 'unknown'
    placeholder).
    """
    if value is None:
        return False
    if isinstance(value, (list, tuple, set, dict)):
        return len(value) > 0
    if isinstance(value, str):
        return bool(value.strip())
    return bool(value)


def compute_row_completeness(disclosure: dict) -> dict:
    """
    Score how completely a single extracted disclosure row was populated,
    instead of accepting any row the LLM returned at face value regardless
    of how many fields it actually filled in (ISSUE 9).

    Attempts one cheap, safe reconstruction before scoring: if `title` is
    missing but inferred_fields.probable_title was set by deep enrichment,
    that's used to fill the gap. Genuinely missing fields with no
    reconstructable source are left missing and flagged instead.

    Returns:
        {"score": float, "missing_fields": [str],
         "reconstructed_fields": [str], "flagged": bool}
    """
    ed = (disclosure or {}).get("explicit_data") or {}
    inf = (disclosure or {}).get("inferred_fields") or {}

    values = {
        "application_number": ed.get("application_number"),
        "publication_number": ed.get("publication_number"),
        "applicant": ed.get("applicant"),
        "title": ed.get("title"),
        "standards": ed.get("standards"),
        "country": ed.get("country") or ed.get("applicant_country"),
    }

    reconstructed = []
    if not _field_present(values["title"]) and _field_present(inf.get("probable_title")):
        values["title"] = inf["probable_title"]
        reconstructed.append("title")

    score = 0.0
    missing = []
    for field, weight in _COMPLETENESS_WEIGHTS.items():
        if _field_present(values.get(field)):
            score += weight
        else:
            missing.append(field)

    score = round(score, 3)
    return {
        "score": score,
        "missing_fields": missing,
        "reconstructed_fields": reconstructed,
        "flagged": score < COMPLETENESS_FLAG_THRESHOLD,
    }


def flatten_for_db(data: dict) -> dict:
    """Flatten nested analysis JSON into a flat dict for easy DB storage."""
    out = {}
    meta = data.get("declaration_metadata", {})
    out["declarant"] = meta.get("declarant", "")
    out["filing_type"] = meta.get("filing_type", "")
    out["confidence"] = meta.get("confidence", 0.0)
    out["likely_standard_generation"] = meta.get("likely_standard_generation", "")
    out["patent_count"] = len(data.get("patent_disclosures", []))
    return out
