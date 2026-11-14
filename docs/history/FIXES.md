# SEP Platform — Audit Fixes

Applied to the uploaded `sep-platform` tree. 17 files changed, ~1100 insertions.
`FIXES.patch` in this directory is the full unified diff against the original.

## 1. The reported bug: "estimated time keeps going up"

**Root cause.** `Timer.tsx` computed `remaining = elapsed / fraction - elapsed`,
i.e. `elapsed * (1-f)/f`. That is only stable while `fraction` advances. When
it is flat, `elapsed` grows every tick while `f` stays constant, so the
estimate grows by `(1-f)/f` seconds per real second. At `f = 0.08` the
countdown ran backwards at ~11x real time.

`fraction` was flat for most of a run, for three compounding reasons:

1. **The backend never announced its longest stages until they finished.**
   `stage="analyzing"` and `stage="deep_enrichment"` were only published from
   the per-chunk `finally` block. In the common single-chunk case the UI sat
   on "OCR & Text Extraction" with a frozen fraction for the whole multi-minute
   LLM call.
2. **Stage weights did not match reality.** The two LLM stages held 56% of the
   bar but are ~80-85% of wall-clock; `verifying`/`research`/`persisting` held
   22% but are near-instant.
3. **Multi-file runs reset the fraction per file.** `overallFraction` ignored
   `current_file_index` entirely, so a 3-file run drove the bar 0->100% three
   times and blew the estimate up at each reset.

**Measured, against a simulated 694-second deep run:**

| | ticks where ETA increased | largest upward jump | mean abs error |
|---|---|---|---|
| before | 679 / 694 | 26.8 s | 171 s |
| after  | 11 / 694  | 30.0 s (one rounding bucket) | 109 s |

**Fix.** New `frontend/src/lib/etaEstimator.ts`:

- estimates the *rate* of progress over a 90 s sliding window, blended with
  the whole-run average and floored at 25% of it so a stall cannot send the
  estimate to infinity;
- asymmetric EMA — revises down fast, up slowly;
- rate-limits any upward revision to 0.35 s per real second;
- rounds to coarse buckets (15/30/60 s) so small revisions are invisible;
- withholds an estimate entirely until there is evidence for one, rather than
  showing a confident wrong number in the first seconds;
- resets on a genuine fraction regression (next file) instead of absorbing it
  as a silent jump;
- reports `finishing` rather than a stuck `00:00`.

Supporting fixes: backend now announces `analyzing`/`deep_enrichment` *before*
the work starts; per-file counters are reset so file N+1 does not inherit
file N's totals; stage weights recalibrated; `overallFraction` made file-aware
and stopped reading stale `chunk_index` in stages that never publish it; the
progress bar is clamped monotonic; the timer interval now stops on completion.

## 2. Other defects found and fixed

**Critical — silent data loss.** `extract_json_safe` searched for the first
`{`, which inside a top-level array `[{...},{...}]` is the first *element*. A
40-patent response returned as a bare array yielded exactly one patent, with
no error logged anywhere.

**Critical — silent data corruption.** Markdown-fence stripping ran two global
regex substitutions over the whole response, deleting backticks inside string
values. `"Encoding using ```delta``` frames"` parsed as
`"Encoding using deltaframes"` — wrong data straight into the DB and report.

**High — XSS in the downloaded report.** `esc()` escaped `&<>` but not quotes,
and is interpolated into `alt="..."`; the figure `src` was entirely unescaped.
Report content is LLM output derived from an uploaded PDF, opened from
`file://`. Quotes are now escaped and image sources are whitelisted to
base64 image data URIs.

**High — CSV formula injection (CWE-1236).** Cells were correctly CSV-quoted,
which does nothing for `=cmd|'/c calc'!A1`. Risky leading characters are now
prefixed.

**High — NumPy 2.x ABI break.** `numpy>=1.26.0` was unbounded while
`faiss-cpu==1.8.0`, `chromadb==0.5.15`, `scikit-learn==1.5.2` and
`sentence-transformers==3.1.1` are pinned to wheels built against the NumPy
1.x C ABI. A fresh build resolves NumPy 2.x and crashes at import — an image
that builds green and dies on first request. Bounded to `<2.0`.

**Major — no database indexes.** Nothing was indexed, despite
`Patent.declaration_id` being filtered in three places including the
cross-declaration similarity scan. Verified on a 20k-row SQLite database:
`SCAN patents` became `SEARCH patents USING INDEX ix_patents_declaration_id`.

**Major — SQLite locking defaults.** No WAL, no busy timeout. A progress poll
or search arriving during an analysis write failed instantly with "database is
locked". Added WAL, `busy_timeout=10000`, `synchronous=NORMAL`,
`foreign_keys=ON`. Also made `check_same_thread` SQLite-only — it raised
`TypeError` at startup for any other `DATABASE_URL`.

**Major — no upload validation.** No size, count or type check; `await
upload.read()` materialised the whole body. Added a chunked 100 MB cap, a file
count limit, a `%PDF-` signature check, and `job_id` format validation (it was
used directly as a dict key).

**Major — PyMuPDF native memory leaks.** All five `fitz.open()` sites called
`close()` on the straight-line path only, so any exception mid-parse leaked
the document. Moved to `finally` at the two most exposed sites.

**Major — failed stage never highlighted.** The backend carefully preserved
`failed_stage` when a file errored, but the frontend read only `stage` (which
is `'error'`, not a UI stage). No card was ever marked failed and the Retry
button never rendered.

**Docker.** Compose published `0.0.0.0:8000` while `AUTH_ENABLED` defaults to
false — an unauthenticated upload and inference endpoint open to the LAN. Now
`${BIND_ADDR:-127.0.0.1}`. Removed obsolete `version:` keys.

## 3. Tests added

- `frontend/src/lib/__tests__/etaEstimator.test.ts` — 13 tests.
  `node --experimental-strip-types --test frontend/src/lib/__tests__/etaEstimator.test.ts`
- `backend/tests/test_json_utils.py` — 12 tests.
  `python backend/tests/test_json_utils.py` or `pytest backend/tests/`

All 25 pass.

## 4. Verification caveats — please read

- **The frontend was never compiled.** No `node_modules` and no network were
  available, so `tsc` and `vite build` could not run. Frontend changes are
  validated by logic tests, not a type check. **Run a real build before
  shipping.**
- **The DB changes were not run through SQLAlchemy** — it could not be
  installed offline. They were validated by executing the identical SQL and
  PRAGMAs through the stdlib `sqlite3` driver.
- **Docker images were never built.**

## 5. Not audited

`research/` (~2,900 lines), `rag/vector_store.py`, `ingestion/pipeline.py`,
`core/prompts.py`, `core/llm_engine.py`, `utils/text_chunker.py`, and the
frontend research components. `vector_store.py` is the highest-value next
target — duplicate vectors, cache invalidation and repeated embedding
generation all live there.

## 6. Known gaps not fixed (would be new features)

- There is **no cancellation mechanism at all**, so "cancelled analysis cleans
  up properly" cannot hold — nothing can be cancelled.
- The completion screen's `await setTimeout(1500)` can `setState` after
  unmount.
- Stage weights are static heuristics. The highest-value improvement would be
  persisting observed per-stage durations and calibrating per machine; the
  estimator is already structured to accept that.
