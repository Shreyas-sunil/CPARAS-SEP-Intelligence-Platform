# Changelog

## v0.7.1 — "How it works" platform guide dropdown

### New: `frontend/src/gitas/components/PlatformGuideDropdown.tsx`
A dropdown in the main top nav bar (next to the theme toggle), visible on
every GITAS and C-SARAS page since both are mounted inside `gitas/layout/
Layout.tsx`. It has four tabs:

- **GITAS** — what the portfolio-scale SEP database/dashboard is, and how
  bulk scoring's pause/stop/continue works.
- **C-SARAS** — what the seven-field dataset classifier does, and how its
  background job + pause/stop/continue works.
- **cPARAS** — a short pointer to the separate deep-analysis tool, which
  keeps its own existing in-app Help modal for the full walkthrough.
- **Dataset** — explains where the built-in 3GPP reference data comes from
  (the curated `BUILTIN_3GPP_KNOWLEDGE` set added in v0.7.0) and that the
  Corpus page can crawl the live archive to extend it further.

Implemented as a self-contained, click-outside/Escape-to-close dropdown
(same pattern as cPARAS's existing `VersionDropdown`), with new `.g-guide-
dd*` styles in `gitas/styles/gitas.css` that follow the existing light/dark
theme variables and collapse to a full-width panel under 640px. No backend
or API changes.

## v0.7.0 — Broader built-in 3GPP TS 38-series coverage

### Expanded bootstrap knowledge base (`ingestion/pipeline.py`)
`BUILTIN_3GPP_KNOWLEDGE` — the curated spec list that gets indexed into the
vector store immediately at startup, with no crawl required, so GITAS's
Text Matching, CSARAS's related-document search and CPARAS's standards
mapping all have real 3GPP reference material to work against out of the
box — has been expanded from 12 to 34 TS 38.xxx entries (41 total with the
existing cross-series 23./24./33./36. specs kept).

The full `38_series` archive is ~266 documents (per-band RF-conformance
annexes and ~150 feasibility-study TRs among them), which is too large to
crawl and index just to seed a bootstrap set and adds little SEP-analysis
value on its own. Instead, the added entries are normative TS 38.xxx specs
most commonly cited in SEP declarations and patent claims, spanning:

- **RF** — 38.101-1/-2/-3 (UE transmission/reception, FR1/FR2/interworking), 38.104 (BS transmission/reception)
- **Physical layer** — 38.201, 38.202, 38.215 (measurements)
- **RRC / mobility / L2** — 38.305 (positioning), 38.306 (UE capabilities), 38.307 (band requirements), 38.322 (RLC), 38.323 (PDCP), 38.340 (IAB/BAP)
- **NG-RAN interfaces** — 38.410/38.413 (NGAP), 38.420 (Xn principles), 38.460/38.463 (E1AP, CU-CP/CU-UP split), 38.470/38.473 (F1AP, CU-DU split)
- **Conformance** — 38.508-1, 38.521-1, 38.523-1

Every spec number was checked against the real 3GPP archive listing
(`www.3gpp.org/ftp/specs/archive/38_series`) before being added, so none
are invented, and each can later be crawled for its real text via
`ingestion/spec_corpus.py` on top of this bootstrap layer.
`RECOMMENDED_SERIES` (`api/routes/gitas/scrape.py`) is computed from this
list's most-represented series, so `38_series` remains the default/
recommended crawl target — now backed by roughly 3x the prior built-in
38-series weight.

No API, schema, or frontend changes — this is data-only.

## v0.6.0 — Start / Stop / Pause / Continue + Large-File Performance

### Start / stop / pause / continue, platform-wide
Every long-running pipeline in GITAS, CSARAS and cPARAS can now be
paused, stopped, and resumed mid-run instead of only ever running to
completion or being abandoned:

- **CSARAS dataset classification** (`POST /api/csaras/dataset/upload`)
  no longer classifies the uploaded workbook inline before responding —
  it now runs as a chunked background job (`csaras/dataset_pipeline.
  run_dataset_job`), returning the `batch_id` immediately. New endpoints:
  `GET /jobs/{batch_id}`, `POST /jobs/{batch_id}/pause`, `/stop`,
  `/continue`. A pause/stop always lands between whole chunks (never
  mid-row), and `/continue` resumes exactly where it left off by
  re-parsing the originally uploaded file (kept on the batch row for this
  purpose) and picking up from `processed_records`.
- **GITAS SEP bulk essentiality scoring** (`POST /api/sep/upload`) gains
  the same controls: `POST /runs/{run_id}/pause`, `/stop`, `/continue`.
  Resuming re-parses the originally uploaded workbook (already kept on
  `SepBulkRun.original_file_blob` for Excel export) and continues scoring
  from `processed_rows`; already-scored rows are never re-duplicated
  (persistence is upsert-by-natural-key).
- **GITAS legacy 3GPP scraper** (`api/routes/gitas/scrape.py`) gains
  `POST /jobs/{job_id}/pause` and `/resume` as explicitly-named aliases
  of the existing `/cancel` and `/retry` — same cooperative-stop /
  skip-completed-files mechanism, now surfaced under the platform's
  standard pause/continue vocabulary.
- **cPARAS declaration analysis** (`POST /api/analysis/analyze`) gains
  `POST /jobs/{job_id}/pause` and `/stop`, checked between files, between
  extraction chunks, and between deep-enrichment batches. A stopped
  request returns early with `stopped: true` and `skipped_files`, so the
  frontend can re-submit just the remainder under the same `job_id`.
- The GITAS ETSI Corpus dataset importer already had full pause/cancel/
  resume (`etsi_corpus/pipeline.py`) — unchanged, and the template this
  release's other job-control endpoints follow for consistency.
- Frontend: CSARAS's dataset page, GITAS's SEP upload panel, and cPARAS's
  analysis progress tracker each show a live progress bar plus
  Pause/Stop/Continue controls wired to the endpoints above.

### Large-file performance
- **CSARAS classification** (`csaras/dataset_pipeline.classify_records`)
  no longer runs every row — including the large majority that resolve
  entirely deterministically, with no LLM call — through an
  `asyncio.Semaphore(4)`-gated coroutine. Deterministic classification
  is synchronous CPU work, so gating it behind a concurrency-4 semaphore
  never actually parallelized it; it just serialized the whole file
  behind 4 "slots" regardless of whether they needed to await anything.
  Fixed by splitting into two passes: deterministic classification runs
  for every row up front, unthrottled (with a cooperative `asyncio.
  sleep(0)` yield every 200 rows so the background job stays pause/stop-
  responsive on a large file); only the minority of rows that still need
  the LLM fallback go through the bounded-concurrency path, which is now
  independently tunable via `CSARAS_CLASSIFY_LLM_CONCURRENCY` (default
  8, up from the previous effective 4).
- Both CSARAS and GITAS's bulk classification/scoring now persist
  incrementally in `CSARAS_JOB_CHUNK_SIZE`-row (default 500) / existing
  `SCORING_BATCH_SIZE`-row checkpoints rather than only at the very end,
  which is also what makes pause/stop/continue possible without losing
  work.
- GITAS's SEP bulk scoring path (`etsi_bulk/scoring.py`) was already
  rewritten for large datasets in v0.5 (bounded-batch embedding, no full
  similarity matrix, incremental persistence, ~30s for 17,000 rows on the
  offline fallback embedder) — unaffected by this release beyond gaining
  pause/stop/continue.

See `ENGINEERING_REPORT_v0.5.md` for the prior large-dataset architecture
work this release builds on.

## v0.3.0 — "GITAS / SEP Intelligence Platform v0.3"

### Critical fix: scraper concurrency / WinError 32
The root cause of the reported `WinError 32` crashes and duplicate-job
behavior was that concurrent crawl jobs for the same 3GPP series shared
the same `.part` temporary download filename, so one job's finalize
(`rename .part -> .zip`) collided with another job still writing to (or
finalizing) the same file.

Fixed with defense in depth:
- **Database-level protection**: a partial `UNIQUE` index on
  `spec_crawl_jobs(series)` restricted to active statuses
  (`pending`/`crawling`/`indexing`) makes it impossible for more than one
  active job per series to exist, enforced by SQLite itself.
- **Per-series `asyncio.Lock`** in `api/routes/scrape.py` so the common
  case (a user double-clicking "Download") gets a clean, friendly
  "already running" response instead of a raw error.
- **Job-scoped temporary filenames** (`file.zip.part.<job_id>.<uuid>`) in
  `ingestion/gpp_crawl.py` so two jobs — even if they somehow both ran —
  could never collide on the same temp file.
- **Retried atomic finalize**: transient Windows file-lock errors during
  `Path.replace()` are now retried with exponential backoff (up to 5
  attempts) rather than treated as an immediate permanent failure.
- **Stale temp-file cleanup**: `.part.*` files older than 6 hours are
  removed at the start of a new crawl, without touching any file that
  could belong to a currently-active job.
- New `POST /api/scrape/jobs/{id}/retry` endpoint, itself routed through
  the same duplicate-prevention path, so retrying can't create a second
  active job either.
- Structured error responses (`error_code`, `retryable`) instead of raw
  Python tracebacks surfaced to the UI.

### 3GPP Corpus is now a first-class GITAS feature
- New GITAS route `/corpus` (`CorpusPage.tsx`), reachable from the main
  GITAS navigation — no longer buried inside cPARAS's `/analyze` screen.
- KPI summary, a "Quick start" card for the recommended series, a
  collapsible full series selector, and per-job progress cards (progress
  bar, downloaded/skipped/failed/discovered counts, current file, and a
  collapsible technical-details panel for failures).
- Calls the existing `/api/scrape/*` endpoints only — no second scraper
  implementation.
- The old Scraper tab inside cPARAS (`/analyze`) has been removed; the
  Text Matching tab's "corpus not indexed" hint now links to `/corpus`.

### Dashboard quick access
- New "Platform Intelligence" card row on the GITAS Dashboard (both the
  empty and populated states) linking to C-SARAS, 3GPP Corpus, and
  cPARAS.

### Versioning
- Backend: FastAPI app title/version → `GITAS / SEP Intelligence
  Platform`, `0.3.0`.
- Frontend: footer version pill, `package.json` name/version updated.

### Testing
- 7 new backend tests in `tests/test_scraper_concurrency.py`, including a
  genuinely concurrent 5-way race test and a rapid-double-click retry
  test, both run via real async HTTP requests against the FastAPI app
  (not simulated).
- Full backend suite: 252/252 passing, zero regressions.
- Frontend: `tsc --noEmit` and `npm run build` both pass cleanly.

### Known limitations in this release
- The C-SARAS page has **not** yet been redesigned to match cPARAS's
  visual language (pipeline stage cards, activity log, etc.) — it remains
  the existing GITAS-styled dashboard from the prior release.
- The 3GPP Corpus page shows aggregate per-job progress (files
  downloaded/skipped/failed, current file) but does **not** yet include
  the file-level row list, live activity feed, or multi-series combined
  progress bar described in the extended UX spec for this release.
- `total_files` (and therefore a determinate percent) is only known once
  a crawl finishes discovering the full series tree — during an active
  crawl the UI correctly shows an indeterminate progress state rather
  than a fabricated percentage, per the "don't guess a total" requirement.
- This was built and tested without network access to the real
  `3gpp.org` site; scraper/crawler behavior is verified against a local
  mock server reproducing the real site's structure, not the live site.

### How to run a small scraper smoke test
```
POST /api/scrape/start
{"series": ["38_series"], "max_files": 2}
```
Then poll `GET /api/scrape/jobs` (or open `/corpus` in the UI) and
confirm: one job is created, files download without a WinError 32,
duplicate `Start` requests attach to the same job, and re-running
indexing does not duplicate chunks.

## V0.6 — evidence-based GITAS_Validation_Status (QC gate fix)

**The bug.** `GITAS_Validation_Status` was effectively a passthrough of
`essentiality`: any scored row other than "Potential" was marked VALID,
even when `GITAS_Declaration_Conflict` was independently True — so a row
could carry `DECLARATION_CONFLICT` in `GITAS_Reason_Codes` while
`GITAS_Validation_Status` simultaneously said `VALID`. Reported example:
a patent declared against `3GPP TS 48.008` (a GSM-era spec) mapped to
`33.501` (5G Security, Clause 6.1) with `GITAS_Declaration_Conflict =
True`, still exported as `VALID`.

**Root cause.** The status logic checked only `has_result` and
`essentiality == "Potential"` — it never looked at the conflict/
inconsistency signals `gitas_fields_for_patent` was already computing a
few lines above it.

**Fix — `backend/etsi_bulk/excel_export.py` only** (no scoring, corpus
lookup, confidence blending, Excel structure, frontend, or API contract
changed):

- **New `_conflict_materiality()`.** Not every `GITAS_Declaration_Conflict
  = True` row deserves equal suspicion — the reference corpus only has
  11 spec families (all Rel-8+ LTE/5G), so a declared family simply not
  being in that small illustrative set (the common case: 115/189 rows
  on the sample dataset) is an inherent corpus-coverage gap, not a wrong
  answer. Flagging every such row the same as an actual contradiction
  would have made nearly everything NEEDS_REVIEW — the "please
  don't just lower the validation rate" failure mode explicitly warned
  against. A conflict is now MATERIAL — genuinely checkable and
  contradicted — only when: the declared family IS in the corpus but a
  different family was matched anyway; or the matched entry's technical
  domain differs from the declared family's own corpus domain; or the
  row was declared purely against a pre-4G standard (2G/3G) being
  matched to the corpus's exclusively-4G/5G content — a checkable
  cross-generation impossibility that directly captures the reported
  example's *type* of problem (a GSM-only disclosure cannot be essential
  to a 5G NR clause).
- **`gitas_fields_for_patent()` validation-status decision, rewritten**
  from a 2-branch passthrough to graded tiers:
  - `INVALID`: no result; or (Essential + material conflict + confidence
    below the platform's own `SIM_CONFIRMED` bar) — a checkable
    contradiction without evidence strong enough to override it.
  - `NEEDS_REVIEW`: `Potential` (unchanged); or (Essential + material
    conflict + confidence at/above `SIM_CONFIRMED` — strong enough that
    a human should decide, not an automatic rejection); or a version
    inconsistency (declared family confirmed but release/version
    diverges); or a non-material topical match below `SIM_LIKELY`.
  - `VALID`: no result problem, no result-undermining issue; or any
    `Non-Essential` result (a "no confident mapping" conclusion isn't
    asserting the declared family in the first place, so a mismatch
    against whichever corpus entry scored highest doesn't bear on it —
    this is also what keeps the fix from clobbering legitimately-correct
    negative determinations).
  - `SIM_CONFIRMED`/`SIM_LIKELY` are imported from `research/engine.py`
    (the platform's own existing similarity bands), not redefined, so
    "sufficient evidence" means the same thing here it means everywhere
    else scoring already uses it.
- **`GITAS_Reason_Codes`**: a declaration conflict now emits
  `DECLARATION_CONFLICT` only when material, `DECLARATION_UNCONFIRMED`
  otherwise — so the specific word "CONFLICT" and a `VALID` status can no
  longer co-occur (the exact contradiction reported), while a plain
  corpus-coverage gap no longer reads as the same severity as a real one.
- `GITAS_Declaration_Conflict` (the boolean column) and `corpus_coverage`
  are computed exactly as before — unchanged so nothing reading that
  column elsewhere changes meaning; only how validation status/reason
  codes *react* to it got smarter.

**Verified — real upload -> score -> export run, not simulated:**

| | before | after |
|---|---|---|
| `Sample_Data_for_UIUX_Database.xlsx` (189 rows) | VALID 189 / NEEDS_REVIEW 0 / INVALID 0 | VALID 107 / NEEDS_REVIEW 74 / INVALID 8 |
| Rows with `DECLARATION_CONFLICT` reason + `VALID` status | 181 (all of them) | **0** |
| Declaration-conflict rows (broad, any) | — | 181 (115 coverage-gap-only, 66 material) |
| Version-mismatch rows | — | 0 (none in this dataset; covered by a synthetic unit test instead) |
| `Data_For_Testing.xlsx` (25 rows, reference) | VALID 15 / NEEDS_REVIEW 10 | VALID 11 / NEEDS_REVIEW 14 / INVALID 0 |

Confirmed directly in the regenerated `.xlsx` (not just the DB layer):
opened the real output workbook, scanned all 189 `Sheet1` data rows —
zero rows have `DECLARATION_CONFLICT` in `GITAS_Reason_Codes` alongside
`GITAS_Validation_Status = VALID`. `GITAS Summary`'s "Rows requiring
human review" now reads 74 (was effectively unreachable/always-0 before,
since nothing could produce NEEDS_REVIEW except a bare `Potential`
essentiality with no conflict).

New `backend/tests/test_gitas_validation.py` (14 tests): clean
declared-family match -> VALID; the reported bug's exact shape (GSM
declared, matched to unrelated 5G Security, low confidence) -> not
VALID; the same conflict at high confidence -> NEEDS_REVIEW not INVALID
(proves conflicts aren't blanket-rejected); a non-material
corpus-coverage-gap conflict with good similarity -> stays VALID (proves
the fix doesn't collapse everything); the same but weak similarity ->
NEEDS_REVIEW; version inconsistency -> NEEDS_REVIEW; Potential ->
NEEDS_REVIEW regardless of conflict; Non-Essential -> VALID even with a
conflict; multiple simultaneous conflict signals; the
reason-codes-vs-status agreement invariant checked directly; a
missing-optional-attribute defensive-coding regression. Full suite:
`python3 backend/tests/_run_all.py` — **195 passed, 0 failed** (was 181;
+14 new, 0 regressions — one existing fixture-only test needed a
`getattr`-defensive fix in `_conflict_materiality` for two new field
reads, not a behavior change).

**Remaining limitations:**
- Materiality's "domain conflict" and "family-in-corpus" checks are only
  as good as the 11-family illustrative corpus itself — a much larger
  real corpus would let more coverage-gap rows resolve to genuine
  confirmation or genuine (material) contradiction instead of sitting in
  the "can't confirm, can't deny" middle tier.
- The cross-generation check is binary (2G/3G vs. everything else)
  because the corpus itself only spans Rel-8+; if pre-Rel-8 corpus
  content is ever added, this rule should be revisited so it doesn't
  start flagging genuinely-covered legacy content as a "generation
  conflict".
- This does not attempt LLM-based re-verification of any mapping — it's
  still a deterministic, rule-based QC layer over already-computed
  scores, consistent with this file's existing "never re-scores"
  contract.

## V0.5 — frontend upload timeout (client gave up before the server finished)

**Not a parsing bug — a client-side timeout.** Reported as "Upload
failed — check the file format." in the GITAS UI while the backend log
showed `POST /api/sep/upload` returning `200 OK` for the same request.
Root cause: `frontend/src/gitas/api.ts`'s shared axios client had a flat
60-second timeout on every request, including `/upload` — the one
endpoint that isn't a fast DB read, because it runs full essentiality
scoring synchronously before responding. With the real local embedding
model (`sentence-transformers`/BAAI-bge-m3) rather than the offline
TF-IDF fallback this sandbox's own testing used, embedding ~240 texts
(corpus + a ~190-row run) took over a minute on CPU — axios aborted the
request client-side around 60s, threw a timeout error the UI displayed
as a generic format-rejection message, and the backend went on to
finish the work and log 200 OK for a client that had already stopped
listening.

- **`frontend/src/gitas/api.ts`** — `uploadWorkbook()` now passes its own
  15-minute timeout, instead of inheriting the 60s default meant for the
  read-only dashboard endpoints.
- **`frontend/src/gitas/components/UploadPanel.tsx`** — a timeout error
  (`err.code === 'ECONNABORTED'` or a timeout message) is now shown as
  "still finishing on the server, check Runs" rather than the generic
  "check the file format" message, so the two failure modes (client gave
  up vs. server actually rejected the file) are never conflated again.
  Also swaps the busy label to "Scoring against standards corpus…" once
  the file transfer itself completes and the server-side scoring phase
  begins, so a long wait at "100%" doesn't read as stuck.
- Any run that already completed server-side during a client-timed-out
  upload is not lost — it's already in `sep_bulk_runs` and shows up in
  the dashboard's run selector on next load; re-uploading isn't
  necessary (though also isn't harmful — it just creates another run).

Frontend-only change; not re-verified with a real `npm run build` in
this sandbox (no network access to the npm registry / no node_modules
here) — same caveat prior fixes in this repo have flagged for frontend
edits. Run `npx tsc --noEmit && npm run build` before shipping.

## V0.4 — flexible header resolution ("any data" for the bulk export format)

**The ask:** don't just special-case the two known ETSI bulk-export
column layouts — make the parser handle a workbook whose columns are
spelled differently from both, without another code change.

**What changed, still confined to `backend/etsi_bulk/parser.py` +
`schema.py` (`scoring.py` untouched).**

- **`schema.py` — new `resolve_header()`.** Two-pass header matching for
  any column string against the known field set:
  1. Exact match after normalizing case/punctuation/whitespace — this is
     what recognizes every spelling already known (both real exports).
  2. Token-overlap fuzzy match (harmonic mean of how much of the header
     an alias explains and how much of the alias the header covers, with
     light plural-stemming) for a spelling that's never been seen
     before. Deliberately conservative: requires a clear, unambiguous
     best-scoring field — a genuinely uncertain or unrelated header
     resolves to "no match" rather than a wrong guess, the same
     principle `utils/csv_parser.py`'s single-declaration matcher
     already follows.
  `COLUMN_MAP` (the original explicit exact-name table) is kept as the
  seed of the alias list and for backward compatibility; a hand-picked
  set of additional plausible synonyms (`_EXTRA_ALIASES`) was added per
  field (e.g. `company` also matches "Applicant", "Assignee", "Owner",
  "Patent Holder"; `legal_status` also matches "Status"; etc.).
- **`parser.py` — `looks_like_bulk_header()` rewritten** from "contains
  all 3 literal REQUIRED_COLUMNS names" to a semantic check: the header
  must resolve at least one column into each of three buckets — an
  identifier (application/publication number), a declarant (company/
  disclosure number/declaration reference), and a standard
  (declared-standard text/spec number). This is what makes format
  detection itself flexible, not just per-field mapping.
- **`parser.py` — `_row_to_record()`** now calls `resolve_header()`
  per column instead of an exact `COLUMN_MAP` lookup.
- **`excel_export.py` — `_align_rows()`** updated to the same
  `resolve_header()` call (was a separate exact `COLUMN_MAP.get()`), so
  export-time row alignment gets the same flexibility as ingestion —
  a header-case mismatch that used to silently degrade to positional
  alignment now resolves directly.

**A real false positive was caught and fixed during this work**: an
initially-added alias ("Work Item or Standard No" for `standard_raw`)
turned out to be the *other* pipeline's (single-declaration,
`utils/csv_parser.py`) own known column name verbatim, which — combined
with the shared "application/publication number" and "applicant"-ish
columns both formats have — caused a single-declaration file's header to
get misrouted into this bulk pipeline. Removed; a dedicated regression
test (`test_single_declaration_header_not_misrouted`) now guards this.

**Verified:**
- Both real files (`Sample_Data_for_UIUX_Database.xlsx` 189 rows,
  `Data_For_Testing.xlsx` 25 rows) parse identically to before — no
  regression from generalizing the matcher.
- A **third, synthetic header layout** sharing no literal column name
  with either real export (`"Applicant Name"`, `"Disclosure No"`,
  `"Declared Standard"`, `"App No."`, `"Invention Title"`, `"Patent
  Status"`, ...) parses correctly end-to-end with every field landing in
  the right place — this is the actual "any data" proof, not just a
  claim.
- An unrelated spreadsheet, and the single-declaration pipeline's own
  header, both correctly do NOT get picked up by this parser (false-
  positive guards).
- `backend/tests/test_etsi_bulk_parser.py` grew from 17 to 23 tests;
  `test_excel_export_robustness.py`'s one now-obsolete assumption (case-
  only header differences used to force positional fallback; they no
  longer need to) was updated to a `_now_resolves_directly` test plus a
  new test for headers that are still genuinely unresolvable. Full
  suite: `python3 backend/tests/_run_all.py` — 181 passed, 0 failed.

**Known limits, deliberately not solved here (would need actual LLM/NLP,
not string matching):** a header in a language other than English, or
using domain jargon/abbreviations with no token overlap to any alias at
all (e.g. a single opaque code column), still won't resolve — it'll
either fail detection cleanly (if the missing signal is one of the three
required buckets) or leave that one field unmapped (if it's a secondary
field). Both fail safely (clear warning, or a blank column) rather than
mismapping data.

## V0.3 — GITAS bulk ingestion: second ETSI export column variant

**The bug.** `Sample_Data_for_UIUX_Database.xlsx` (189 rows) was rejected
outright by `POST /api/sep/upload` — "No sheet matched the expected ETSI
bulk-export column headers" — even though every field the pipeline
actually needs was present in the sheet, just under different header
names than `Data_For_Testing.xlsx` uses. Scope: `backend/etsi_bulk/`
only (parser + schema). **The scoring engine (`etsi_bulk/scoring.py`) was
not touched** — every essentiality/confidence/similarity value is
produced by the exact same code path as before.

- **`schema.py` — `REQUIRED_COLUMNS`.** Was `IPRD_ID, DIPG_DISPLAY_NUMBER,
  PATT_APPLICATION_NUMBER`. `IPRD_ID` is swapped for `COMP_LEGAL_NAME`:
  the old gate column is mapped by `COLUMN_MAP` but never read by
  anything downstream (not persisted on `SepBulkPatent`, not used by
  scoring or export), so it was rejecting workbooks on a column that
  serves no purpose beyond detection — while the UIUX-database export
  variant has no such column at all (it identifies declarations by
  `IPRD_REFERENCE` instead). `COMP_LEGAL_NAME` is present in every known
  export variant and is the company facet used throughout the dashboard,
  making it a strictly better detection signal.
- **`schema.py` — `COLUMN_MAP` header aliases.** The UIUX-database export
  spells two columns differently than the reference export: `Title`
  (vs. `PBPA_TITLEEN`) and `Legal_Status`, underscore (vs. `Legal
  Status`, space). Both were previously unmapped, silently dropping
  every title and legal-status value for that variant — title feeds the
  scoring engine's patent-text and domain inference, and legal status
  caps an expired/ceased patent's essentiality tier. Both spellings now
  map to the same internal field name (`title` / `legal_status`).
  Also mapped (previously ignored, still optional/informational):
  `PATT_ID`, `STD_URL`, `PUB_URL`.
- **`parser.py` — spec number/version derived from free text when no
  dedicated columns exist.** The UIUX-database export has no
  `TGPP_NUMBER`/`TGPV_VERSION` columns; the spec number and version only
  appear embedded in the free-text `Standard` column (e.g. `"3GPP TS
  48.008 version 7.5.0"`, or the longer form that repeats a non-dotted
  ETSI deliverable number before `" -- "` and the canonical dotted 3GPP
  form after it — only the latter is extracted). New
  `_derive_spec_fields()` fills `spec_number`/`spec_version` **only**
  when a row has neither already (never overrides a real
  `TGPP_NUMBER`/`TGPV_VERSION` column value). This is what lets the
  scoring engine's existing, unmodified `_release_label()` correctly
  derive `Rel-N` for this export variant instead of reporting `Unknown`
  for every row.

**Verified against both real files** (parse -> score -> persist -> the
same dashboard/export endpoints, via the real FastAPI route handlers and
a real SQLite DB, TF-IDF fallback embedder since no model hub access was
available in this sandbox):

|  | before | after |
|---|---|---|
| `Sample_Data_for_UIUX_Database.xlsx` (189 rows) | 0 rows parsed (hard rejection) | 189/189 parsed, scored, exported |
| `Data_For_Testing.xlsx` (25 rows, reference) | 25/25 (baseline) | 25/25 — byte-identical field values, unaffected |

Spec-number coverage on the UIUX-database file went from 0% to 100%
(84/189 rows have no version in the source text, so `spec_version` is
correctly left blank there — not fabricated). Title coverage went from
0% to 100%.

New `backend/tests/test_etsi_bulk_parser.py` (17 tests) covers both
header layouts, the alias mappings, the free-text spec derivation
(including the dual-reference long form and the "dedicated columns win"
case), skip/blank-row handling, and the not-a-bulk-export fallback path.
Full suite: `python3 backend/tests/_run_all.py` — 173 passed, 0 failed.

## V0.2 — added support for xlsx and html files

Declarations can now also be uploaded as **Excel workbooks (.xlsx/.xlsm/.xls)**
and **HTML pages**, on top of PDF and CSV/TSV — same drop zone, same
`/analyze` request, same result shape. Neither format gets its own parsing
engine: both are adapters onto the CSV path's existing header-mapping logic
(`utils/csv_parser.py`'s column matching, family-column disambiguation,
boilerplate filtering, continuation rows), so a declaration produces the
same result whether it arrives as a CSV export, an Excel export, or an
HTML export of the same annex.

- **`csv_parser.py` refactored**: the row-processing half (everything from
  header discovery onward) is now `parse_rows_declaration(rows, ...)`,
  taking a plain `list[list[str]]` grid instead of CSV bytes directly. CSV
  parsing itself is unchanged — it just decodes bytes into rows first and
  hands them to the same function XLSX and HTML now also call.
- **New `backend/utils/xlsx_parser.py`.** Reads `.xlsx`/`.xlsm` via
  `openpyxl` and legacy `.xls` via `xlrd` (xlrd 2.x dropped `.xlsx`
  support, hence needing both libraries). Only the first worksheet is
  read. Numeric cells are rendered without a spurious trailing `.0` — the
  single most likely way an application/publication number typed as a
  plain number in Excel would otherwise get silently corrupted on the way
  through. Format detection checks content (ZIP magic for `.xlsx`, OLE2
  magic for `.xls`) ahead of the filename, so a mislabeled file still
  takes the right path, and falls back to the other reader once before
  giving up if the first attempt fails.
- **New `backend/utils/html_parser.py`.** Parses every `<table>` on the
  page with BeautifulSoup/lxml (already a dependency, previously only used
  by the research corpus crawler) and picks the one that maps the most
  patent rows — handling pages with nav/title/footer decoration tables
  alongside the real annex table. A page with no usable table (or no
  `<table>` at all) falls back to the page's flattened visible text for
  LLM extraction, the same degrade-gracefully contract CSV already has.
  `<script>`/`<style>` content is stripped before both table parsing and
  the text fallback.
- **Unrecognisable Excel/HTML input degrades rather than fails**, exactly
  like CSV: falls back to the normal LLM path with a warning surfaced in
  the UI, rather than a hard error.
- `_read_upload_limited` gained Excel and HTML content-sniffing, checked
  ahead of the CSV sniff (an `.xlsx`'s ZIP container or an HTML page's
  markup would otherwise be misread as garbage CSV text). Upload rejection
  errors now mention all four accepted formats.
- Results' `_source.kind` is `"xlsx"` or `"html"` (as well as the existing
  `"pdf"`/`"csv"`) so the UI can label the provenance banner correctly; the
  `_source.csv` metadata key is shared across all three tabular formats
  rather than renamed per format, since the UI only needs one field to
  read regardless of which one produced it.
- Frontend: drop zone, file picker, help text and the source-provenance
  banners now accept and correctly label `.xlsx`/`.xlsm`/`.xls` and
  `.html`/`.htm` inputs alongside PDF and CSV/TSV.
- New `backend/tests/test_xlsx_parser.py` (13 tests) and
  `backend/tests/test_html_parser.py` (11 tests), covering format
  detection, the numeric-cell rendering fix, multi-table HTML pages, and
  malformed/corrupt input.

**Not covered:** only the first worksheet/table is read for multi-sheet
workbooks or multi-table HTML pages with more than one real data table.
Password-protected or corrupted workbooks fall back to LLM extraction over
no text (i.e. fail loudly with a warning) rather than being decrypted.

## V0.2 — CSV input support

Declarations can now be uploaded as **CSV or TSV** in addition to PDF, in
the same drop zone and the same `/analyze` request.

- **New `backend/utils/csv_parser.py`.** Maps arbitrary-but-similar column
  headings onto the existing slim extraction schema. Header text is
  normalised (case, punctuation, NBSP, Unicode dashes, parenthetical hints
  like `(V.X.X.X)`) and matched exact-alias-first, falling back to
  token-overlap scoring at a deliberately strict threshold — an
  unrecognised column is reported rather than guessed at, because a wrong
  mapping silently writes one field's data into another and still looks
  like a successful parse.
- **A recognised CSV skips the extraction model entirely.** Its structure
  was never lost, so there is nothing for the model to recover. Rows go
  straight into `slim_to_full()`, and everything after — continuation-row
  merging, deep enrichment, per-field verification, research augmentation,
  persistence — is shared verbatim with the PDF path. There is no parallel
  CSV implementation to drift out of sync.
- **Duplicate annex columns are disambiguated positionally.** The ETSI
  annex repeats *Application No.*, *Publication No.* and *Country of
  registration* under "Other members of this PATENT FAMILY". The first
  occurrence wins the real field; later ones are kept separately as family
  data. Letting the later columns win would have replaced every
  disclosure's own application number with a family member's — the most
  damaging silent failure available on this path.
- **The header row is found, not assumed.** Each of the first 25 rows is
  scored both alone and merged with the row below, which is what handles
  the real annex layout where the header is genuinely two physical rows.
- Handles UTF-16/BOM/cp1252 exports, delimiter sniffing that doesn't trust
  `csv.Sniffer` blindly, Excel `="0012345"` text guards, multi-standard
  cells, and skips blank/repeated-header/footnote rows.
- **Unrecognisable CSVs degrade rather than fail**: they fall back to the
  normal LLM path over a text rendering, with a warning surfaced in the UI.
- Results carry a `_source` block (`kind`, `extraction_method`,
  `used_extraction_model`, CSV row/column accounting) so the UI can
  distinguish rows transcribed from columns from rows a model read off an
  OCR'd scan. `_model` is deliberately left populated — deep enrichment
  still uses one — with `used_extraction_model: false` carrying the claim.
- PDF-specific stages are skipped for CSV: `reconstruct_all` (undoes PDF
  line-wrapping; would stitch real rows together), the page-confidence log
  line, and the under-extraction count heuristic (impossible when rows map
  1:1, so it would only manufacture false alarms).
- `_read_upload_limited` now classifies uploads by content — PDF magic
  number wins outright, so a PDF misnamed `.csv` still takes the PDF path.
  `/analyze/stream` also gained the upload size cap it previously lacked.
- Frontend: drop zone, file picker and copy accept CSV/TSV; a green
  provenance banner reports deterministic mapping (and is *not* the amber
  warning style, so amber keeps meaning "something is wrong").
- 30 new tests in `backend/tests/test_csv_parser.py`, weighted toward the
  failures that would otherwise be silent.

**Not covered:** CSVs whose header can't be found in the first 25 rows
fall back to LLM extraction. See V0.2 above for `.xlsx`/`.xls` and HTML.

## Frontend v0.1 (CPARAS rebrand)

- Rebranded frontend from "SEP Intelligence Platform" to **CPARAS** (C-DOT
  Patent Analysis & Research Assessment System), with the C-DOT logo added
  to the header.
- Added an in-app version-history dropdown (top-right corner) that will be
  updated with each future release; seeded with this v0.1 entry.
- Added a "Text Matching" tab outlining the planned pipeline steps for a
  future clause-level text-matching feature (not yet implemented).
- Fixed the Patent Disclosures table and downloadable report/CSV to show
  both **Application No.** and **Patent No. (Publication)** — previously
  only the application number was rendered even though publication number
  was already returned by the backend.
- Downloaded HTML report renamed to "CDOT ESSENTIALITY ANALYSIS REPORT".

## v2.6.0

### A note on scope

The fixes below were diagnosed and verified against the two sample
declarations (Samsung, Nokia). They were not tested against other
declarants' filings, since none were provided. Here's what should and
shouldn't be assumed to generalize:

- **Documents under ~20K characters of extracted text** (the majority of
  ETSI IPR declarations) now go through the LLM in a single call regardless
  of declarant, format, or extraction tier — this removes the chunk-boundary
  miscounting bug structurally, not just for these two samples.
- **Larger declarations** (Nokia's row format runs ~600 chars/row due to
  multi-line wrapping, so ~34+ rows would re-trigger chunking) fall back to
  a row-anchor splitting strategy. This was broadened from a cellular-only
  pattern (5G/4G/LTE/etc) to match any ETSI document reference (TS/EN/TR),
  so it shouldn't be blind to DVB, BRAN, ITS-G5, or other non-cellular
  declarants — but it's only verified against Nokia's specific table layout.
- **Scanned/photocopied declarations** depend on Tesseract's OCR accuracy,
  which varies with scan quality and table layout. Verified only against the
  Samsung sample (40/40 application numbers). A different declarant's scan
  could behave differently.
- The sanity-check diagnostic's number-pattern coverage was broadened to
  include more patent office formats (added JP/KR/DE/FR/GB prefixes,
  on top of the existing US/IN/CN/EP/WO/PCT/UK/provisional formats), but
  it's inherently a best-effort heuristic, not a complete spec of every
  patent numbering format in use globally.

If you run this against other declarations and see a count that looks off,
the backend log will show a "Possible count mismatch" warning — that's
worth checking manually, and worth sharing the specific document/log if the
issue is systematic rather than a one-off heuristic miss.

### Fixed: wrong patent counts (Samsung 50 vs 40, Nokia 26 vs 27)

Root cause, found by tracing the actual logs from a real run:

- Both declarations were being **split into ~4000-char chunks** before
  analysis (Samsung: 4 chunks, Nokia: 2 chunks), even though the model is
  configured with `num_ctx=32768` — comfortably enough context to read
  either document (Samsung ~11K chars OCR'd, Nokia ~6-16K chars depending on
  extraction tier) **in a single call**.
- With no shared context between chunks, the 3B model (`qwen2.5:3b`) had to
  independently judge "where does this fragment start/end" on every call,
  and reliably miscounted near those boundaries — inventing extra rows in
  one chunk (Samsung chunk 3 alone produced 27 patents from what should have
  been ~14-15 rows) and dropping rows in another (Nokia chunk 1 found only 2
  patents from what should have been ~16).
- The chunk-size constant was hardcoded to `4000` directly at the call site
  in `_analyze_in_chunks`, separately from the `_split_into_chunks` default
  — so an earlier attempt to raise the default didn't actually take effect
  during a real run.

**Fix:** raised the real, effective chunk threshold to **20,000 characters**.
Both sample documents now go through in a single LLM call, removing the
boundary-miscounting failure mode entirely rather than trying to patch
around it. Chunking is kept as a fallback for genuinely large declarations
that exceed that size, using a new row-anchor-aware splitting strategy
(detects table rows like "5  5G  TS 38.331..." or "3  BRAN  EN 301 893...",
anchored on the TS/EN/TR document-number pattern rather than a fixed list of
cellular standard names, so it isn't blind to non-cellular ETSI declarants)
in addition to the existing `Disclosure N:` and paragraph-based strategies,
verified against the real Nokia table to correctly find all 27 row
boundaries.

### Added: best-effort count sanity-check logging

After analysis, the backend now counts distinct application/publication-like
numbers found in the raw extracted text (several formats: US/IN/CN-prefixed,
PCT, UK-style, US provisional) and logs a warning if that count diverges
significantly from what the LLM returned. This is diagnostic only — it does
not alter the result — but makes future count mismatches visible in the
backend logs instead of silent. Verified against both samples: Samsung
detects 40/40 correctly; Nokia detects 25/27 (two PCT application numbers
wrap across a line break in the source PDF and lose trailing digits in this
particular check, which is a known limitation of the diagnostic, not of the
main extraction).

### Strengthened extraction prompt

Added explicit instructions against fabricating, duplicating, or guessing
additional rows, and asked the model to verify its output count against the
distinct application/publication numbers it can see in the input before
finishing.

### Other
- `max_tokens` for analysis calls raised from 4096 → 6000 as additional
  safety margin for larger single-shot documents.
- Removed "(part 1 of 1)" framing from the prompt when a document doesn't
  need chunking, since that phrasing could itself bias the model into
  expecting a fragment.

## v2.6.1

### Fixed: Samsung returning 0 patents despite correct OCR (your real log this time)

Confirmed from your actual run: Tesseract OCR worked perfectly (10,868 chars,
all 5 pages — matches what was verified earlier). Nokia's single-shot fix
from v2.6.0 also worked exactly as intended: **27/27 patents, one LLM call,
no chunking**. The remaining failure was new and specific to Samsung:

```
JSON parse failed even after fixing: Expecting ',' delimiter: line 608 column 6 (char 16512)
```

Root cause: asking `qwen2.5:3b` to extract all 40 patents in one very long
JSON generation occasionally causes it to lose syntactic discipline near the
end of the output (line 608 of ~645 expected lines — roughly patent #38 of
40), producing a malformed array. The previous JSON parser had no way to
recover anything from this — one broken comma anywhere meant the entire
response was discarded, dropping all 40 patents to 0, even though ~38 of
them were individually well-formed.

**Fix:** added a salvage parser (`salvage_partial_array` in `json_utils.py`)
that activates only after the normal parse fails. It scans the malformed
`patents` array and extracts every individually well-formed `{...}` object
using proper string/escape-aware brace matching, skipping only the specific
broken object(s) instead of the whole response. Verified against a
simulated 38-good/1-broken/1-incomplete scenario matching the real error
shape: recovered all 38 valid patents (plus the declarant name) instead of
returning zero.

This means a future partial-generation failure on any declaration will now
return as many correctly-parsed rows as the model actually got right,
with a log warning telling you it was a partial recovery — rather than
silently discarding everything.

### Fixed: silent Tesseract detection failures

`_tesseract_available()` was catching its real failure reason and logging
it at `DEBUG` level, which doesn't appear in the console at the app's
default `INFO` log level — so "Tesseract not found" gave no actionable
information beyond a generic install link. It now logs the actual
exception, the current `TESSERACT_CMD` value being used, and concrete
troubleshooting steps at `WARNING` level so it's visible by default.

### Note on remaining Samsung risk

The salvage parser recovers what the model got right, but a generation
that breaks at patent #38 of 40 still means 2 patents are genuinely lost
from that run — salvage is harm reduction, not a fix for the model
occasionally losing coherence on long outputs. If this happens repeatedly,
the more durable options are to: (a) split only the Samsung-style very long
declarations into two roughly-half-size chunks (re-introducing some
chunk-boundary risk, but bounded), or (b) try a model with more reliable
long-form structured output. Neither has been implemented here since
v2.6.0's single-shot approach is correct for the much more common case
(documents under ~20K chars), and over-correcting for one large document
risks reintroducing the Nokia-style boundary bug for everyone else.
