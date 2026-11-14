# SEP Platform — Audit Round 2 Fixes

This document covers the 10 numbered issues plus the two "Additional Issue"
items (similarity score, confidence score) from the second audit prompt.
`FIXES.md`/`FIXES.patch` in this directory cover an earlier round (ETA
estimator, JSON-parsing data loss, XSS, CSV injection, etc.) and are
untouched by this round.

## Files changed

**New:**
- `backend/utils/text_repair.py` — wrapped-identifier / wrapped-country-name
  / column-bleed reconstruction (Issues 1, 3, 6, 7)
- `backend/utils/chunking.py` — row-anchor regex, header-preserving
  row-safe chunking, multi-signal token budgeting (Issues 2, 4, 5)
- `backend/tests/test_text_repair.py`
- `backend/tests/test_chunking.py`
- `backend/tests/test_verification.py`
- `backend/tests/test_similarity_confidence.py`
- `backend/tests/_run_all.py` — zero-dependency test runner (pytest is now
  in `requirements.txt`, but this lets the same test files run with
  nothing but the stdlib in environments where installing pytest isn't
  possible)

**Modified:**
- `backend/utils/json_utils.py` — `verify_code_in_text`/`verify_patent_fields`
  API redesign (Issue 10), OCR-only fuzzy gate removed (Issue 1),
  `compute_row_completeness` added (Issue 9), `salvage_partial_array` now
  flags partial results (Issue 5)
- `backend/api/routes/analysis.py` — wires all of the above into the
  live pipeline; row-anchor regex comma fix (Issue 2); retry-on-incomplete
  chunk logic (Issue 5); `ensure_ascii=False` on all `json.dumps` (Issue 8)
- `backend/utils/pdf_parser.py` — Tesseract CJK language pack support
  (Issue 8)
- `backend/core/llm_engine.py` — force UTF-8 decoding on all LLM backend
  HTTP responses (Issue 8)
- `backend/research/engine.py` — similarity calibration wiring, redesigned
  multi-signal confidence blend (Additional Issues)
- `backend/research/embeddings.py` — `calibrate_similarity_matrix` (Additional
  Issue: similarity)
- `backend/requirements.txt` — added `pytest` for the new test suite

## Running the tests

```
cd backend
python -m pytest tests/ -q          # preferred
python3 tests/_run_all.py           # stdlib-only fallback
```

All 57 tests pass as of this round (22 pre-existing + 35 new).

---

## ISSUE 1 — Verification rejects correct patent numbers

**Root cause.** `verify_code_in_text()`'s normalization (`_normalize_code`)
strips whitespace, dashes, dots and slashes — but a PDF that wraps
`PCT/CN2024/1107` onto one line and `84` onto the *next* line doesn't leave
those two fragments adjacent once a table row is flattened to text: the
row's other columns (or, in the reported case, unrelated boilerplate text)
sit between them. Normalizing whitespace inside each line doesn't reconnect
two separate lines. The correct, complete identifier (`PCT/CN2024/110784`)
was therefore never a literal substring of the normalized source text and
was rejected, while the truncated fragment (`PCT/CN2024/1107`), which *is* a
literal substring, passed.

**Fix.** `utils/text_repair.reconstruct_all()` runs on the extracted text
immediately after extraction (before chunking, before the LLM prompt, before
verification) and merges an identifier's wrapped tail back onto the line
where it started — for PCT, WO, EP, US, CN, KR, JP, and several other office
prefixes. `verify_code_in_text`'s docstring now states explicitly that
`source_text` must already be reconstructed. Also removed the previous
OCR-only fuzzy-matching gate (see below) since it was the wrong fix for this
specific bug — reconstruction turns the correct case into an *exact* match,
which is what should have been happening all along.

**Before:** correct wrapped identifier → rejected; truncated identifier →
accepted.
**After:** correct wrapped identifier → exact match, verified; a genuinely
truncated/fabricated number is not a substring of the reconstructed text and
is correctly rejected.

Tests: `tests/test_text_repair.py` (`test_wrapped_pct_number_is_merged`,
`test_wo_wo_and_ep_ids_reconstruct_too`), `tests/test_verification.py`
(`test_wrapped_pct_number_verifies_once_reconstructed`,
`test_truncated_patent_number_must_not_be_accepted`).

---

## ISSUE 2 — `_ROW_ANCHOR_RE` missing comma

**Root cause.** The row anchor regex's project-code character class was
`[A-Za-z0-9./\- ]` — no comma. `"5G Release 15, | TS 38.331"` breaks the
match at the comma: the non-greedy group can't consume it, so the regex
can't reach the `TS/EN/TR` token from that position and the row silently
fails to match.

**Fix.** `utils/chunking.ROW_ANCHOR_RE` — added `,` to the character class:
`[A-Za-z0-9./,\- ]`. Reviewed every other row-detection regex in the
codebase (the `_COUNT_DIAGNOSTIC` patent-number sanity check in
`analysis.py`, the `Disclosure N:` marker regex) — neither has an analogous
character-class omission, since neither tokenizes free-form project-code
text the way the row anchor does.

**Before:** `"5G Release 15,"` rows silently invisible to row detection
(row-count estimation, chunk splitting, token budgeting all undercounted).
**After:** matched correctly.

Tests: `tests/test_chunking.py` (`test_row_with_comma_in_project_code_matches`,
`test_multiple_comma_rows_all_detected`).

---

## ISSUE 3 — Wrapped PCT tail digits become phantom rows

**Root cause.** Same wrap artifact as Issue 1: an orphaned tail (`"84"`) at
the start of its own line is visually indistinguishable from a genuine row
number, and both the row-splitting logic and the LLM itself can (and did)
treat it as a new row/disclosure.

**Fix.** Same `reconstruct_wrapped_identifiers()` pass as Issue 1 — merging
the tail back onto its identifier removes the orphaned leading-digit line
entirely, so there's nothing left that looks like a row anchor. The
merge/no-merge decision uses **surrounding context** as required: a
preceding line ending mid-identifier (a known prefix immediately followed by
digits, nothing else) AND a following line's digits *not* immediately
followed by `" | "` (this codebase's actual table rows are always
pipe-delimited — see `utils/pdf_parser.py`). A genuine new row with a pipe
right after its row number is never merged.

**Before:** `"84 Cooperation Patent Treaty filing"` detected/treated as a
new row/patent.
**After:** merged into the identifier; the leftover boilerplate text
survives as ordinary prose, not a phantom row.

Tests: `tests/test_text_repair.py`
(`test_wrapped_pct_tail_does_not_survive_as_its_own_line`,
`test_genuine_new_row_with_pipe_is_not_merged`).

---

## ISSUE 4 — Chunking drops document headers

**Root cause.** In `_split_into_chunks`'s row-anchor strategy, `group_start`
was initialized to `row_positions[0]` — the position of the *first detected
row*, not `0`. Everything before it (`text[:row_positions[0]]`: declarant
name, cover page, filing metadata) was never included in *any* chunk,
including the first.

**Fix.** `utils/chunking.split_into_chunks()` now captures `header =
text[:first_marker_or_row_position]` once per splitting strategy and
prepends it (capped at 3000 chars, with a clear separator marker) to
**every** chunk it produces — including chunk 1, which lost the header the
same way every later chunk did.

**Before:** a large declaration's cover page/declarant/metadata never
reached the LLM in any chunk.
**After:** every independent chunk call carries the header, so
`declaration_metadata` can be populated correctly regardless of which chunk
happens to return it.

Tests: `tests/test_chunking.py` (`test_header_survives_into_every_chunk`,
`test_no_row_anchors_falls_back_to_paragraphs_and_keeps_lead_in`).

---

## ISSUE 5 — OCR under-detection causes token under-budgeting

**Root cause.** `chunk_max_tokens = min(18000, est_rows*300+1200)` depended
*entirely* on `est_rows` from the row-anchor regex. When OCR mangles
`TS`/`EN`/`TR` tokens (or the layout doesn't match the anchor pattern at
all), `est_rows` undercounts, the budget follows it down, the LLM truncates
mid-array, and `salvage_partial_array` silently recovers a partial result
indistinguishable from a complete one.

**Fix — three parts:**
1. `utils/chunking.estimate_chunk_token_budget()` — budgets from multiple
   signals (chunk character length as an independent floor, OCR
   method/confidence, page count, table density), and uncertainty only ever
   *increases* the budget relative to the old formula, never decreases it.
2. `utils/chunking.extraction_seems_incomplete()` + a retry loop in
   `_analyze_in_chunks`: a chunk whose result needed
   `salvage_partial_array` at all, or returned far fewer rows than expected,
   is retried once with a ~1.6x larger budget (capped at 22000 tokens)
   before being accepted.
3. If still incomplete after the retry, the result is **not** silently
   accepted — `_possibly_incomplete`/`_incomplete_chunk_count` flow through
   to the top-level parsed result and get logged, rather than looking
   identical to a fully-complete extraction.

**Before:** OCR detects 15/40 rows → budget ≈ 5700 tokens → truncated
output → salvaged partial result indistinguishable from complete.
**After:** the same scenario clears a much higher floor from chunk length +
low-OCR-confidence multiplier alone; if it's still short, one automatic
retry with a bigger budget runs; if that's still short, the result is
explicitly flagged rather than silently accepted.

Tests: `tests/test_chunking.py`
(`test_reproduces_the_reported_ocr_under_detection_case`,
`test_salvaged_result_always_flagged_incomplete`).

---

## ISSUE 6 / 7 — Wrapped country names / column bleeding

**Root cause.** Same PDF line-wrap artifact family as Issues 1/3, applied to
country names: `"UNITED"` / `"STATES"` split across a line break, sometimes
with another column's content (an applicant name) bled into the middle:
`"UNITED"` / `"Technologies Oy KINGDOM"`.

**Fix.** `utils/text_repair.repair_wrapped_country_names()` — a fixed,
reviewable list of country/territory head tokens known to wrap
(`UNITED`, `SOUTH`, `NORTH`, `NEW`, ...) mapped to their valid continuations,
matched across a line break with an optional "bled" middle group. The middle
group (if non-empty) is re-emitted as its own pipe-delimited cell ahead of
the reassembled country name, so the applicant text and the country name are
separated back into distinct fields rather than one swallowing the other.

**Before:** `CountryBadge` only ever saw `"UNITED"` and fell through its
lookup table; applicant text and country name fused into unusable text.
**After:** `"UNITED KINGDOM"` reconstructed whole; bled-in applicant text
separated back out (`"Technologies Oy | UNITED KINGDOM"`).

Tests: `tests/test_text_repair.py` (`test_wrapped_united_kingdom`,
`test_column_bleed_applicant_between_country_halves`).

---

## ISSUE 8 — CJK applicant names become `???????`

**Root cause — two independent bugs, both fixed:**

1. **OCR was English-only.** Every Tesseract call omitted `lang=`, which
   defaults to `eng`. Tesseract's English model has no Han/Hangul/Kana
   glyphs at all, so a Chinese/Japanese/Korean applicant name on a scanned
   page can't be recognized — not misread, structurally unrecognizable.
2. **HTTP responses weren't forced to UTF-8.** `httpx`'s streaming/`.json()`
   reads auto-detect response encoding when the server doesn't send an
   explicit `charset` (Ollama's NDJSON stream doesn't). That detection runs
   on whatever raw byte window happened to arrive, which can split a
   multi-byte UTF-8 sequence (any CJK character) mid-codepoint and get
   mis-identified as a single-byte encoding — corrupting the character
   irreversibly the moment it's decoded, before anything downstream (the
   database, the frontend) ever sees the original bytes.

**Fix.**
- `utils/pdf_parser._resolve_tesseract_lang()` — queries
  `pytesseract.get_languages()` once, builds `"eng+chi_sim+chi_tra+jpn+kor"`
  from whatever's actually installed, and warns (once) with the exact
  `apt install tesseract-ocr-<lang>` command for anything missing rather
  than silently OCR'ing CJK content as English.
- `core/llm_engine.py` — `resp.encoding = "utf-8"` set explicitly on every
  Ollama/vLLM HTTP response (streaming and non-streaming) before it's
  decoded, removing the encoding guess entirely (Ollama's JSON API is
  always UTF-8 per the JSON spec, so this is not a guess — it's a
  correction of a wrong default).
- `ensure_ascii=False` added to every `json.dumps()` call that serializes
  extracted data (DB storage, SSE events) for readability/audit — this
  alone wasn't the corruption bug (Python's default `ensure_ascii=True`
  produces lossless `\uXXXX` escapes, not literal `?`), but keeping the
  data human-readable end-to-end removes one more place a naive
  byte-level operation downstream could clobber an escape sequence.

**Before:** CJK applicant names silently unrecognized by OCR, and any CJK
text that *did* reach the LLM could be corrupted in transit before storage.
**After:** installed CJK language packs are used automatically; response
decoding is pinned to UTF-8 end-to-end.

*(No automated test for the Tesseract binary itself — this environment has
no OCR binary/CJK traineddata to exercise. `_resolve_tesseract_lang()` is
structured to be safe to unit test with a mocked `pytesseract` if a CI
environment has Tesseract installed; the UTF-8-forcing change in
`llm_engine.py` has no independent unit-testable surface since it's a
one-line `httpx.Response.encoding` assignment guarding a live HTTP call.)*

---

## ISSUE 9 — Partially populated rows accepted silently

**Root cause.** No code anywhere scored or flagged field completeness. A row
missing applicant, country, and title flowed into the database
indistinguishable from a fully-populated one.

**Fix.** `utils/json_utils.compute_row_completeness()` — a weighted 0-1
completeness score across `application_number` (0.30), `title`/`applicant`/
`standards` (0.20 each), `publication_number`/`country` (0.05 each).
Attempts one safe reconstruction (pulling `title` from
`inferred_fields.probable_title` if the explicit field is blank but deep
enrichment already inferred one) before scoring what's still missing. Rows
scoring below 0.6 are flagged (`flagged: true`). Wired into
`analysis.py`'s per-disclosure loop, attached to
`inferred_fields.completeness`, with a summary warning logged for the run.

**Before:** no visibility into which rows were under-populated.
**After:** every disclosure carries a completeness score and explicit
missing-field list; the confidence score (Additional Issue, below) also now
factors this in.

Tests: `tests/test_verification.py`
(`test_row_missing_applicant_and_country_is_flagged`,
`test_missing_title_is_reconstructed_from_inferred_fields`).

---

## ISSUE 10 — `verify_code_in_text` API redesign

**Fix.** `fuzzy_threshold`, `candidates`, `allow_fuzzy` are now
keyword-only (`*` after `source_text`) on both `verify_code_in_text` and
`verify_patent_fields`. Added explicit `TypeError`/`ValueError` validation
with clear messages for a non-numeric or out-of-range `fuzzy_threshold` and
a non-list `candidates`. Full docstrings cover the contract (including that
`source_text` should be pre-repaired — see Issue 1) rather than leaving
callers to infer parameter order from the previous positional signature.

**Before:** `verify_code_in_text(code, text, 0.9)` and a caller passing
`candidates` before `fuzzy_threshold` by accident both "worked" syntactically
with silently wrong results.
**After:** every optional parameter must be named; a caller passing the
wrong type gets an immediate, specific error instead of a silently wrong
comparison.

Tests: `tests/test_verification.py`
(`test_verify_code_in_text_is_keyword_only_after_source_text`,
`test_verify_code_in_text_rejects_bad_threshold_type`,
`test_verify_code_in_text_rejects_out_of_range_threshold`).

---

## ADDITIONAL ISSUE — Similarity score (0% / 50% for almost everything)

**Root cause.** Sentence embedding models are anisotropic: cosine similarity
between two *unrelated* texts from the same model doesn't cluster near 0 —
it clusters around a model-specific baseline (commonly 0.3-0.6), and
similarity between *near-duplicate* texts rarely approaches 1.0 either. Real
signal (a strong clause match vs. a weak one) lives inside a narrow raw-
cosine band. Reporting that raw value directly as a percentage means almost
every score rounds toward the same baseline — the reported "50% for almost
everything." Separately, `build_patent_text()`'s generic fallback sentence
(used when fast-mode extraction leaves every enrichment field blank) made
several unrelated patents embed identically; if that generic text happened
to sit in a low-similarity region of the corpus space, their scores floored
out near the observed minimum — "0% for almost everything."

**Fix.** `research/embeddings.calibrate_similarity_matrix()` — rescales raw
cosine similarities using the 5th/95th percentile of the *actual observed
distribution in that run* as the 0%/100% anchors (order-statistics-based
calibration, not a hand-picked constant), clipped to [0, 1]. This is a
monotonic transform (ordering is fully preserved — verified by test) and
degrades gracefully (falls back to a plain clip, doesn't divide by ~0) when
a run's similarity distribution has no meaningful spread. Wired into both
the patent-vs-corpus matrix (`analyze_patents`) and the patent-vs-patent
matrix (`_similar_patents`). The calibration anchors are exposed in
`meta.similarity_calibration` for audit rather than being a hidden
transform.

**Before:** most similarity scores clustered near a fixed baseline
regardless of true match quality.
**After:** scores span the genuinely-observed range for that run; verified
against a synthetic anisotropic-band distribution that a real embedding
model's output resembles.

Tests: `tests/test_similarity_confidence.py`
(`test_calibration_spreads_a_compressed_anisotropic_band`,
`test_calibration_preserves_ordering`,
`test_degenerate_zero_spread_falls_back_gracefully`,
`test_calibration_is_not_a_fixed_arbitrary_constant`).

---

## ADDITIONAL ISSUE — Confidence score (0% / 50% for almost everything)

**Root cause.** `_predicted_status()` blended only `top_score` (itself
uncalibrated — see above) with `sep_analysis.confidence`, which the default
fast/slim extraction pass never populates (it's `None`). With one real,
compressed input and no independent second signal for most runs, there was
nowhere near enough information left to produce a continuous, varying
number.

**Fix.** Confidence is now a weighted blend over **every signal actually
available** for that disclosure, renormalized over just the signals present
(so a missing one doesn't silently default to a constant):
similarity (0.45, now calibrated) + `sep_conf` when present (0.20) +
verification pass ratio (0.15, from Issue 1/10's per-field verification) +
completeness score (0.10, from Issue 9) + document OCR confidence (0.10,
when this document went through OCR). A verification failure or a flagged
number collision then applies a visible multiplicative penalty rather than
being diluted into invisibility inside an average of several good signals.

**Before:** confidence values clustered at ~0.5-0.56 or floored near 0
across an entire declaration.
**After:** confidence varies continuously per-patent based on that patent's
actual verification/completeness/similarity — verified with synthetic
inputs producing 3 distinct values from 3 distinct signal combinations, and
a >0.5 spread between a low-similarity/no-evidence patent and a
high-similarity/high-evidence one with no `sep_conf` in either case (the old
one-signal fallback path).

Tests: `tests/test_similarity_confidence.py`
(`test_confidence_is_continuous_not_clustered`,
`test_verification_failure_visibly_lowers_confidence`,
`test_collision_flag_applies_a_visible_penalty`,
`test_sep_conf_alone_no_longer_pulls_everything_to_the_same_constant`).

---

## Performance

- Text repair (`reconstruct_all`) is a handful of linear regex passes over
  the document text, run once per document (not per-row/per-field) —
  negligible relative to LLM inference time.
- The retry-on-incomplete logic (Issue 5) only fires when a chunk's result
  already looks suspect (needed salvage, or a clear row-count shortfall), so
  it adds no extra LLM calls to the common case where extraction succeeds
  cleanly on the first attempt.
- `calibrate_similarity_matrix` is a single `numpy.percentile` + vectorized
  affine transform over an already-computed similarity matrix — no
  additional embedding calls, no repeated work.
- No existing caching (embedding reuse, OCR reuse) was removed or bypassed
  by any of these changes.

## Backward compatibility

- `verify_code_in_text`/`verify_patent_fields`: existing call sites in this
  repo already used keyword arguments for the now-keyword-only parameters
  (confirmed by grep across the codebase) and needed no changes beyond the
  two internal call sites inside `verify_patent_fields` itself.
- `analyze_patents(..., ocr_confidence=None)`: new parameter is optional
  with a safe default; the bundled `research/demo.py` script and any other
  caller not passing it continue to work unchanged.
- `extract_text_from_pdf()` / `extract_text_from_pdf_with_confidence()`:
  unchanged signatures; CJK language resolution is internal and transparent.
- `salvage_partial_array()`: return dict gained two new keys
  (`_partial`, `_salvaged_count`); no existing key was removed or renamed.
