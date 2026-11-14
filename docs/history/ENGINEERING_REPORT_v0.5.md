# SEP Platform v0.5 — Engineering Report
## Large-Dataset Crash Fix + Mandatory GITAS/CPARAS Scoring Fields

---

## 1. Root Cause of the 17,000-Dataset Crash

The similarity matrix itself was **not** the primary cause — the illustrative
standards corpus is small (54 entries), so even a full `17,000 × 54` raw
similarity matrix is only ~3.7 MB. The actual causes were architectural:

1. **The entire upload was one synchronous HTTP request.** `POST
   /api/sep/upload` parsed the workbook, embedded every patent text
   (combined with the corpus in a single call), built the similarity
   matrix, calibrated, bulk-inserted, and then **re-queried and
   JSON-serialized every persisted patent back into the response body**
   (`"patents": [_row_to_dict(p) for p in saved]`) — all before the
   request could return. At 200 rows this finishes in under a second; at
   17,000 rows the request runs for tens of seconds with **no progress
   feedback**, and is exactly the kind of long-lived, large-response
   request that times out at a reverse proxy, the browser, or the ASGI
   server itself.
2. **No progress reporting, no background job.** The frontend had no way
   to know the run was still alive, matching the tester's report of "200
   OK, 17000 crashes" — the smaller run finishes inside a normal request
   timeout window; the larger one doesn't.
3. **No per-row fault isolation.** A single malformed row's exception
   would propagate and abort the whole batch/request.
4. **The corpus was re-embedded on every call** (combined with the patent
   batch), and the whole dataset was embedded in one call rather than in
   bounded chunks — wasteful, and a contributing factor to how long the
   single request stayed open.

---

## 2. Exact Files Changed

### New files
| File | Purpose |
|---|---|
| `backend/research/three_signal_scoring.py` | Single shared `claim_match`/`standard_coverage` implementation used by both GITAS and CPARAS. |
| `backend/api/routes/cparas/excel_export.py` | New CPARAS Excel export (`CPARAS_Claim_Match` / `CPARAS_Semantic_Similarity` / `CPARAS_Standard_Coverage` + supporting columns) — did not exist before. |
| `backend/tests/test_v05_large_dataset_scoring.py` | Regression tests (see §11/§12). |

### Modified files
| File | What changed |
|---|---|
| `backend/etsi_bulk/scoring.py` | Rewritten: batched/chunked, fault-tolerant, bounded-memory scoring (`score_records_batched`). |
| `backend/research/embeddings.py` | Added `embed_corpus_for_batched_scoring()` / `embed_patent_batch_for_scoring()` — corpus embedded once per run; patents embedded in bounded chunks; correct handling of the TF-IDF fallback mode (fit once on corpus, transform each batch — never a fresh per-batch fit). |
| `backend/research/engine.py` | CPARAS's `analyze_patents()` now computes `claim_match`/`standard_coverage` via the shared module; exposes `semantic_similarity` as an explicit alias of the existing `similarity_score`. |
| `backend/etsi_bulk/excel_export.py` | Added `GITAS_Claim_Match` and `GITAS_Standard_Coverage` to `GITAS_COLUMNS`, the row builder, the error-row fallback, and number formatting. |
| `backend/api/routes/gitas/dashboard.py` | Upload endpoint now parses synchronously and hands scoring/persistence to a background task; added `GET /runs/{id}/status`; removed the all-patents dump from the upload response; batched idempotent persistence (`_persist_batch`). |
| `backend/api/routes/cparas/analysis.py` | Persists `claim_match`/`standard_coverage` on `Patent` rows (initial persist + recompute path); wires in the new Excel export endpoint. |
| `backend/api/routes/csaras/dataset.py` | Per-row `db.add()` loop replaced with batched bulk insert (2,000-row batches), consistent with GITAS's pattern. |
| `backend/core/database.py` | New columns: `SepBulkRun.{status,parsed_rows,processed_rows,failed_rows,error_message,started_at,completed_at}`; `SepBulkPatent.{row_status,row_error}`; `Patent.{claim_match,standard_coverage}` — all with SQLite `ALTER TABLE` migrations following the codebase's existing pattern. |
| `backend/core/config.py` | Added `SCORING_BATCH_SIZE` (default 256) and `SCORING_DB_COMMIT_BATCH_SIZE` (default 250). |
| `backend/tests/test_gitas_validation.py`, `test_excel_export.py`, `test_excel_export_robustness.py`, `test_excel_export_validation.py` | Updated fake-patent test fixtures to include the two new fields (they stand in for the real ORM row, which now has these columns). |

---

## 3. How Large Datasets Are Now Processed

```
POST /upload
  → parse workbook (fast, in-memory row dicts)
  → create SepBulkRun (status="processing")
  → return immediately: {run_id, status, total_rows}
  → [background task] score_records_batched():
        embed corpus ONCE
        for each batch of records:
            build batch texts (per-row try/except)
            embed this batch only
            compute (batch_size × corpus_size) similarity — small, released after use
            record top-1 match + calibration sample
        compute calibration from a bounded reservoir sample
        for each batch again (cheap, no embedding):
            finalize scores, yield batch
        [caller] persist each yielded batch, update run.processed_rows, commit
  → run.status = "completed" | "completed_with_errors" | "failed"

GET /runs/{id}/status  → {parsed, queued, processing, processed, failed, remaining, ...}
GET /patents?run_id=…  → existing paginated endpoint (unchanged), used to page through results
```

A single bad row never aborts the run: text-construction failures and
embedding-call failures are both caught per-row (with a per-record retry
when a whole batch's embedding call fails), and the row is persisted as
`row_status="FAILED"` with no score fields — never a fabricated zero.

---

## 4. Batch / Chunk Size Used

- **Scoring batch size:** `settings.SCORING_BATCH_SIZE = 256` (embedding + similarity computation).
- **DB commit granularity:** one commit per scored batch as it's persisted (effectively ~256 rows/commit; the existing bulk-insert path inside that already batches in chunks of up to 2,000 for the raw `INSERT`).
- Both are configurable via environment/settings, not hard-coded limits on dataset size.

---

## 5. How Memory Usage Was Reduced

- The corpus is embedded **once per run**, not once per batch.
- No two batches' text or vectors are held simultaneously — each batch's
  `(batch_size × corpus_size)` matrix is discarded immediately after its
  top-1 match is extracted.
- The full `(n_patents × n_corpus)` matrix is **never** materialized,
  regardless of `n_patents`. Verified structurally by a test that spies on
  every call to `cosine_sim_matrix` across a 3,000-row run and asserts the
  first dimension never exceeds the batch size.
- Calibration, which needs the *distribution* of raw similarities across
  the whole run, uses a fixed-size (200,000-value) reservoir sample
  (Algorithm R) instead of retaining every value.
- The upload response no longer serializes every patent row inline.

Measured: a 17,000-row run completed with **peak traced Python memory of
~136 MB** and **~30 seconds** wall-clock time in this environment (offline
TF-IDF fallback embedder — a real sentence-transformers model would take
longer per batch but the memory profile is unaffected, since batching is
the same either way).

---

## 6. How Scoring Calibration Was Preserved

The original calibration (`calibrate_similarity_matrix`) rescales raw
cosine similarities against the 5th/95th percentile of the *whole run's*
similarity distribution. Two changes were needed to keep this correct
under batching:

1. **Calibration statistics** are now computed from a bounded reservoir
   sample collected across all batches (not the retained full matrix),
   preserving the same percentile-based, order-preserving rescale with
   bounded memory regardless of dataset size.
2. **TF-IDF fallback mode correctness fix:** the old code combined
   `corpus_texts + patent_texts` in one call, which is required for the
   TF-IDF fallback (each call independently fits a fresh vectorizer, so
   corpus and patents must share one fit to be comparable). Naively
   batching would have refit a new, incomparable vectorizer per batch. The
   fix: the corpus is fit once (`embed_corpus_for_batched_scoring`), and
   every batch is *transformed* — never refit — into that same space
   (`embed_patent_batch_for_scoring`). In sentence-transformers mode this
   distinction doesn't matter (the model itself defines a fixed space), so
   this only changes behavior for the offline fallback path, and makes it
   *more* correct than before (previously, single-declaration and bulk
   runs could each fit their own space; now the bulk run's corpus fit is
   stable across all of its own batches).

---

## 7. How the GITAS Three Score Fields Are Now Exported

`GITAS_Claim_Match` and `GITAS_Standard_Coverage` were added to
`GITAS_COLUMNS` (join `GITAS_Semantic_Similarity`, which already existed)
in this order:

```
GITAS_Status, GITAS_Evidence_Level, GITAS_Evidence_Label, GITAS_Mapping_Type,
GITAS_Claim_Match, GITAS_Semantic_Similarity, GITAS_Standard_Coverage,
GITAS_Extraction_Confidence, ...
```

Values are read **verbatim** from the already-persisted, already-scored
`SepBulkPatent.claim_match` / `.semantic_similarity` / `.standard_coverage`
columns — never recomputed at export time. A row with no analysis result
(`has_result == False`) gets `None` (a blank Excel cell), never `0`. The
existing post-generation structural validator
(`_validate_generated_workbook`) automatically now enforces the presence
of all three, since it reads its mandatory-column list off
`GITAS_COLUMNS` itself.

---

## 8. How the CPARAS Three Score Fields Are Now Exported

`research/engine.py`'s `analyze_patents()` computes `claim_match` and
`standard_coverage` via the shared `research/three_signal_scoring.py`
module, fed with CPARAS's richer available evidence (the full assembled
patent text — title + declared standards + technical domains + LLM
standard descriptions + probable claim scope + use cases — not just a
title). `semantic_similarity` is exposed as an explicit alias of the
existing, already-correct `similarity_score` (documented mapping — not a
duplicated field).

These are persisted onto new `Patent.claim_match` / `Patent.
standard_coverage` columns (`standard_similarity` already served as
semantic similarity). A **new** export endpoint,
`GET /api/analysis/history/{declaration_id}/export/excel`, was added
(none existed before) producing a `CPARAS`-sheet workbook with:

```
CPARAS_Claim_Match, CPARAS_Semantic_Similarity, CPARAS_Standard_Coverage
```

plus supporting context columns, read verbatim from persisted rows, with
its own structural validator mirroring GITAS's.

---

## 9. How CSARAS Was Made Large-Dataset Safe

CSARAS's classification path (`csaras/dataset_pipeline.py`) was already
well-hardened: bounded concurrency (`asyncio.Semaphore(4)`) for the LLM
fallback, a circuit breaker (`LLMAvailabilityGate`) so an unreachable LLM
fails fast rather than timing out per row, per-record caching, and
per-record try/except with graceful `NA` fallback — no changes were needed
there.

The one real gap: `api/routes/csaras/dataset.py`'s upload endpoint built
one ORM object via `db.add()` per row (up to 17,000+ individually
session-tracked objects) before a single commit. This is now a batched
`INSERT` (2,000-row batches, executemany-style), matching the pattern
already used by GITAS's bulk insert — same fix, same rationale, applied
for consistency and lower memory/CPU overhead at scale.

---

## 10. Test Results

All tests below were run against this environment's offline TF-IDF
fallback embedder (no `sentence-transformers`/`torch` installed here) —
this fallback path is exactly what the existing test suite already relies
on for CI-without-the-ML-stack, and is exercised identically by real
model-backed runs (the batching/memory/fault-isolation logic is embedder-
agnostic).

| Test | Result |
|---|---|
| **200 patents** | ✅ Completes in <1s. Excel export: all 3 mandatory GITAS columns present, 200/200 rows populated. |
| **1,000 / 3,000 patents** | ✅ Completes; verified (via test spy) that no similarity computation during the run ever exceeds the configured batch size (256) — i.e. no full matrix is ever allocated, at any scale. |
| **5,000 patents** | Not run as a standalone case in this environment; the 3,000- and 17,000-row runs exercise the identical code path, so behavior at 5,000 is the same by construction. |
| **17,000 patents** | ✅ Completes in ~30s, **peak traced Python memory ~136 MB**, no crash, incremental progress reported throughout, 17,000/17,000 rows persisted, Excel export succeeds with all 3 mandatory columns populated for all 17,000 rows. |
| **Fault isolation** | ✅ A deliberately poisoned row (failing at both the text-construction stage and the embedder-call stage, tested separately) is marked `FAILED` with no fabricated score; every neighboring row in the *same batch* scores normally. |
| **Full existing test suite** (`backend/tests/`) | **366 passed**, 1 failed. The 1 failure (`test_scraper_concurrency.py::test_rapid_double_click_retry_creates_exactly_one_new_job`) is in `api/routes/gitas/scrape.py`, a file untouched by this work, and fails with an unrelated SQLAlchemy/greenlet async error — a pre-existing environment issue, not a regression from this change. |

### New regression tests added (`tests/test_v05_large_dataset_scoring.py`)
1. Score values stay within `[0, 1]`.
2. 200-patent processing still works.
3. A malformed row doesn't abort its batch (fault isolation).
4. No `O(n_patents × n_corpus)` matrix is ever allocated, at any dataset size.
5. The batched generator yields multiple chunks for a multi-batch dataset.
6. Shared `claim_match`/`standard_coverage` formulas are bounded and monotonic in their inputs.
7. GITAS Excel export contains all 3 mandatory columns end-to-end (real upload → background scoring → download).

---

## 11. Limitations That Still Exist

- **Background tasks are in-process** (`asyncio.create_task`), not a
  durable job queue — if the backend process restarts mid-run, that run's
  background task is lost (though its partially-persisted rows and
  `status="processing"` remain in the DB; there is no automatic
  "resume" or "mark orphaned" sweep for this specific job type, unlike the
  existing `spec_crawl_jobs` restart-recovery logic elsewhere in the
  codebase, which this could be extended to follow).
- **Excel export itself** (`build_enriched_workbook`) still builds the
  output workbook via openpyxl's normal (in-memory) API rather than a
  streaming writer; it was not rewritten in this pass. At 17,000 rows this
  worked without issue in testing, but it is not memory-bounded the way
  the scoring path now is, and could be revisited if datasets grow another
  order of magnitude.
- **PDF report generation** was not specifically audited/rewritten in this
  pass (the request's Excel/CSV and crash-path items were prioritized).
- **5,000-row case** was not run as its own standalone measurement in this
  environment (see §10) — inferred from the 3,000- and 17,000-row runs
  exercising the same code path.
- **Real embedding-model timing** (sentence-transformers/GPU) was not
  measured here, since that stack isn't installed in this sandbox; only
  the offline TF-IDF fallback path was exercised. The batching/memory/
  fault-isolation architecture is identical either way, but actual
  wall-clock throughput on 17,000 rows with a real model should be
  measured in a full deployment before relying on the ~30s figure above.
- **CPARAS's `claim_match`/`standard_coverage`** are heuristic estimates
  over the same illustrative offline corpus GITAS uses (same disclaimer
  already shown elsewhere in the app) — a real claim-text upload doesn't
  yet get an LLM-graded version of these two specific signals (only
  `semantic_similarity`/`confidence_score` benefit from the full LLM
  extraction pipeline today).
