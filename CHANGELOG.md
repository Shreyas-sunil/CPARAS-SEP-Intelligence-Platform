# CPARAS Platform (CPARAS Bulk + CPARAS ISLD) — Changelog

Earlier release notes and audit reports: `docs/history/`.

## v0.5.0 — sign-in and three roles, 3GPP Retrieval Index naming, documented code

**Upgrading from v0.4.x:** your database, datasets, caches and the 3GPP Retrieval Index are used as
they are; v0.5.0 only adds three tables (`users`, `user_sessions`, `auth_events`). After the first
start, create the first Super Admin once: `create_admin.cmd` (Windows) / `./create_admin.sh`, or
**First-time setup** in the browser. Nobody can use CPARAS until that account exists.

### Sign-in and role-based access control (`docs/AUTH.md`)
* Three roles: **Guest** (CPARAS ISLD only; lands on Declaration Analysis), **Data Entry** (ISLD +
  CPARAS Bulk with all workflows; reference-data pages read-only), **Super Admin** (everything +
  User Management + reference-data administration).
* Enforced on the backend for **every** route by one app-wide guard (`backend/auth/`): route →
  permission table, deny by default (unclassified routes are Super-Admin-only; a test fails on any
  unclassified route). 401 / 403 responses carry the `X-CPARAS-Auth` header and a readable message.
* Accounts in the local database; scrypt password hashing (standard library); password policy;
  random session tokens stored only as SHA-256; HttpOnly + SameSite=Strict cookie scoped to `/api`;
  idle and absolute expiry; same-origin check for cookie-authenticated writes; login lock-out after
  repeated failures; `Authorization: Bearer` for API clients; audit trail of sign-ins and account
  changes.
* First Super Admin: CLI on the server (`create_admin.*`, `python -m auth.cli create-superadmin`) or
  browser first-run setup with a one-time token file (deleted after use). No default credentials.
* **User Management** page (Super Admin): create users (generated one-time passwords), change roles,
  activate/deactivate, reset passwords, delete; recent activity. Role/status/password changes sign
  the user out immediately; the last active Super Admin is protected.
* Frontend: sign-in page, account menu (change password, sign out), forced password change after an
  admin reset, role-filtered navigation, help pages and links, "no access" page for direct URLs,
  expired sessions return to sign-in. Standards Corpus actions are disabled for roles that may not
  use them.
* **Illustrative Mode removed** (switch, `/api/illustrative/*`, UI and help text). Demo chunks a
  previous version indexed are deleted from the database and vector store once at start-up
  (`ingestion/legacy_cleanup.py`); real Standards Corpus content is untouched. The built-in
  reference clauses used as the Bulk / ISLD scoring fallback (`BULK_CORPUS_SOURCE`) are unchanged.
* Test tooling: `seed_test_users.*` (one account per role, random passwords printed once),
  `cparas_users.*` (all account commands), `scripts/check_roles.py` (stdlib role check of a running
  install), `scripts/e2e_roles_ui.py` (optional browser check), `backend/tests/test_v050_auth.py`.

### "Knowledge Base" removed
* The home page's "Standards knowledge base" card is now the **3GPP Retrieval Index** card with the
  index's real status, document / clause / version counts and size (read-only).
* ISLD's "Knowledge base" panel is now **Standards text**: 3GPP Retrieval Index documents next to
  Standards Corpus chunks. `GET /api/text-match/status` now reports the index too and counts it as
  ready — before, an installed index was shown as "Empty"/"No standards text indexed yet" and Text
  Matching warned about an empty knowledge base although it was searching the index.
* Explanations name the real evidence source ("3GPP Retrieval Index" / "Standards Corpus");
  "knowledge package" is now "Standards Corpus package"; docs and scripts updated. Stored
  identifiers (`etsi_knowledge_base`, `knowledge_base_version`) keep their names for compatibility.

### Documentation
* Docstrings / header comments for every first-party backend module, class, function and API
  endpoint and every frontend module and exported component/function; new `docs/ARCHITECTURE.md`
  (module map, pipelines, retrieval, configuration, error handling, integration points),
  `docs/AUTH.md`, `docs/TESTING.md`; the v0.3 architecture note moved to `docs/history/`.

### Other
* Version 0.5.0 everywhere (backend, frontend, launcher).
* nginx forwards `X-Forwarded-Host` (with port) so signed-in writes work behind Docker on a LAN address.
* `start.*` / `setup.*` print how to create the first account while none exists.

## v0.4.9 — Essentiality Confidence (Bulk) and exact matched phrases (ISLD)

* **CPARAS Bulk export: new `CPARAS_Bulk_Essentiality_Confidence` column**, right after `CPARAS_Bulk_Essentiality`:
  how essential each patent is, as a percentage (e.g. 63%). It is the overall SEP confidence shown as a %, the same
  for every label (a Non-Essential row at 0.27 shows 27%). The "CPARAS Bulk Summary" sheet gains an
  "Average essentiality (%)" row. Columns after it shift one to the right; header names are unchanged.
* **CPARAS ISLD: exact phrases matched.** Next to the recommended standard, version and clause, each patent now lists
  the word sequences it shares verbatim with the matched clause text (`research/phrase_match.py`: case- and
  punctuation-insensitive, same word order, stopwords trimmed, longest first, in the standard's own wording), plus
  shared single technical terms. Shown on the patent's Research Analysis card, in the Standard Mapping table, in the
  HTML/PDF reports and the CSV, and exported as `CPARAS_Matched_Phrases` / `CPARAS_Matched_Terms` in the ISLD Excel.
  Matched against the whole retrieved excerpt (not only the 600-character display cut). Scores are unchanged.
  New `patents.matched_phrases` / `matched_terms` columns are added to existing databases automatically; declarations
  analysed before v0.4.9 show blank until re-analysed (or recomputed).
* Tests: `tests/test_v049_matched_phrases.py`; essentiality-confidence tests in `tests/test_v041_overall_confidence.py`.

## v0.4.8 — speed, round 2: built for a 25 GB index with ~2 lakh clauses

* **Clause corpus built 7–18x faster from the index, same result** (`services/sqlite_retrieval.build_corpus_json`): row
  metadata is read first and text is fetched only for the rows a clause actually uses, and only until the excerpt can be
  cut at `BULK_RETRIEVAL_MAX_CHARS` (v0.4.6 read every chunk of every clause, both content types, all sources). Output is
  byte-identical (checked on 72k and 168k-clause test indexes: 68 s → 3.8 s / 68 s → 9.6 s). Cached on disk afterwards.
* **All clauses by default:** `BULK_RETRIEVAL_MAX_ENTRIES` 60,000 → 250,000 (the 60k cap cut off whole specifications
  alphabetically, i.e. most TS 36/38 ones). **If your `backend/.env` still says 60000, change it.**
* **Exhaustive scoring scales to 200k clauses:** the cached float16 vectors are scanned in chunks (no 800 MB float32
  copy, no rows × clauses matrix); the declared-family tie-break is applied to the true best clause.
* **Better shortlist, fewer clauses embedded:** patents are embedded first; the shortlist = TF-IDF (16 overall + 24 in the
  declared spec) + clauses under the 8 semantically closest clause *titles* (titles are ~10 tokens, embedded once and
  cached, `bulktitle_*`). Clause excerpts are embedded at 160 tokens (was 256). Calibration sample 96 clauses.
* **Background embedding** fills titles first, then clauses, and now pauses while an ISLD analysis or a Bulk run is in
  progress so it never slows the LLM or your run. It starts at backend start-up (GPU only by default).
* **ISLD: one batched look-up for all patents** (`retrieve_many` / `similarity_search_many`): FTS queries run in
  parallel and every candidate of every patent is re-scored in ONE embedding call; candidates are read at 1,000
  characters (was 1,500) and the pool is 24 (was 40). 27-patent look-up on a 1.8 M-row index: 119 s → 14 s (CPU,
  fp32 test model), 0.3 s when repeated.
* **ISLD deep enrichment asked once per distinct (title, standards)** — family members (US/EP/CN rows of one invention)
  share the answer — and answers are remembered on disk per model + prompt (`isld_enrichment_cache.json`), so re-runs
  and other declarations listing the same patents skip the LLM. `ISLD_ENRICH_CACHE=false` turns it off.
* **Ollama model pre-loaded at start-up** with the analysis options, before the embedder takes its VRAM share; the
  embedder hands freed GPU memory back to the driver when idle.
* Tests: `tests/test_v047_speed.py` extended (enrichment cache/dedup).

## v0.4.7 — speed: Bulk runs no longer stall at "0 / N", ISLD no longer waits behind them

**Root cause of both screenshots.** With the retrieval index installed, the first CPARAS Bulk run embedded the *whole*
clause corpus (up to 60,000 excerpts of 1,500 characters, BAAI/bge-m3, fp32) before scoring row 1 — tens of minutes on a
laptop GPU, hours on a CPU, with no progress shown. The embedder serves one call at a time and that was **one** call, so
an ISLD analysis started meanwhile sat at "OCR & Text Extraction" (its standards look-up needs the embedder) until the
Bulk corpus was done. Separately, each ISLD retrieval query ranked millions of FTS5 rows on the 20 GB index.

* **Bulk: shortlist scoring + per-clause vector cache** (`research/bulk_fast.py`, `etsi_bulk/scoring.py`). Each row is
  compared with ~24 TF-IDF-best clauses from the whole corpus + ~12 from its declared specification; only those clauses
  are embedded (bge-m3), the best wins with the unchanged declared-family tie-break, and the similarity floor is measured
  against a fixed random sample of 160 clauses. Clause vectors are stored on disk (`data/cache/bulkvec_*`) and never
  embedded twice. When every clause has a vector, `auto` scores **exhaustively** (the v0.4.6 comparison) automatically;
  on a GPU the remaining clauses are filled in the background after a run. A complete v0.4.6 cache is still used as is.
  Benchmark (2-core CPU, 8-layer bge-m3-sized test model, 60,000-clause synthetic corpus, 25 rows): first run 52 s
  (264 clauses embedded) instead of an estimated 2 h for all 60,000; the same run again 7 s, mostly model loading. `BULK_SCORING_STRATEGY=exhaustive` restores the old behaviour (now resumable, with progress).
* **Bulk progress while preparing:** the Runs page shows "Preparing reference clauses x / y" under the progress bar.
* **Corpus from the retrieval index cached on disk** (`bulk_corpus_<fingerprint>.corpus.pickle`): rebuilt only when
  the index changes — not after every backend restart (14 s → 0.4 s on a 1 GB test index; minutes on the 20 GB one).
* **Embedder** (`rag/vector_store.py`): fp16 on CUDA (2x faster, half the VRAM next to Ollama), int8 dynamic
  quantisation on CPU (measured 2.1x; pairwise cosine drift ≤ 0.005), in-memory LRU of vectors for search/scoring texts,
  and large calls are sliced so a small interactive call (ISLD retrieval) is served between slices instead of waiting
  for the whole Bulk job. `EMBEDDING_FP16` / `EMBEDDING_CPU_INT8` / `EMBEDDING_CACHE_SIZE`.
* **ISLD retrieval** (`services/sqlite_retrieval.py`): words found in >3 % of all rows are not searched (negligible BM25
  weight, but they made FTS5 score millions of rows), FTS5 ranks rowids alone and only the top rows are joined, and the
  connection is reused per worker thread. 3–30x faster per query on a 770k-row index; same results on the fixture index.
* **Runs list** no longer loads every stored workbook (BLOB) on each 2.5 s poll.
* **Startup warm-up** (`WARMUP_ON_STARTUP=true`): embedding model, retrieval index, Bulk corpus and its lexical index are
  loaded in the background right after start, so the first analysis does not pay for them.
* ISLD activity log shows "Retrieving relevant standards context…" during the look-up.
* Tests: `tests/test_v047_speed.py` (shortlist vs exhaustive agreement, cache reuse/restart, fair scheduling, fast
  candidates = standard search).

## v0.4.6.1 — fix: "Backend offline" while a Bulk upload is scored

* **Bug:** with the retrieval index installed, the first CPARAS Bulk run builds its clause corpus from the 22 GB
  `search_index.sqlite` (minutes). That ran on the server's event loop, so every other request — `/api/health`, the
  uploads list — waited behind it and the UI showed **Backend offline** / **Could not reach the backend** although the
  backend was running.
* **Fix:** the corpus build, the per-batch similarity matching and the Excel export's corpus access run in worker
  threads (`etsi_bulk/scoring.py`, `api/routes/gitas/dashboard.py`); the ISLD evidence path's first index check too
  (`services/retrieval_service.py`). Regression test: `tests/test_bulk_event_loop.py`.
* The first Bulk run after a start is still slow (corpus build + embedding), but the app stays responsive meanwhile.

## v0.4.6 — retrieval index read from the project's own `dist/retrieval_data` folder

* **Default location.** With `CPARAS_RETRIEVAL_DATA` empty the backend now reads `<project>/dist/retrieval_data/` (next to
  `backend/` and `frontend/`). Copy the files of `tgpp-sqlite-retrieval/dist/retrieval_data/` there; no `.env` edit needed.
  Files pasted directly into `dist/` are found too. An explicit `CPARAS_RETRIEVAL_DATA` still wins; relative values are now
  relative to `backend/` (they were relative to the current directory).
* **Fast status on the 20 GB index.** Counts come from `manifest.json` instead of `COUNT(*)` over 11 M rows.
* **Incomplete copy detected.** `search_index.sqlite` smaller/larger than `manifest.json`'s `database_size_bytes` is reported
  as `incomplete_copy` instead of a confusing SQLite error.
* `/api/retrieval-index/status` also returns `expected_dir`, `default_dir`, `using_default_folder` and a per-file checklist.
* **Frontend:** new page **Reference data → 3GPP Retrieval Index** (`/retrieval-index`): folder to copy into, file
  checklist, contents, optional deep check, keyword search. Home page shows the index state.
* `docker-compose.yml` mounts `./dist/retrieval_data`; `start.ps1` / `start.sh` print the index state.

## v0.4.5 — tgpp-sqlite-retrieval output usable as the dataset

* New `CPARAS_RETRIEVAL_DATA`: the SQLite/FTS5 `retrieval_data` folder is read-only, opened in place, and used by `retrieve()` (FTS5 candidates re-scored by the app's embedder; lexical fallback is flagged and never counts as semantic evidence).
* `EvidenceChunk` / `EtsiKbEvidence` carry `content_type` and `dataset_document_id`; non-`full_text` rows are model-generated summaries.
* New `/api/retrieval-index/*` endpoints; vendored client `services/tgpp_retrieval.py` (v1.1.0), adapter `services/sqlite_retrieval.py`.
* CPARAS Bulk: `BULK_CORPUS_SOURCE` gains `retrieval`; bounded corpus built from the index (`BULK_RETRIEVAL_*`).
* Default behaviour is unchanged when `CPARAS_RETRIEVAL_DATA` is empty. Tested on a synthetic fixture only, not on the real 32,440-document index.

## v0.4.4.1 — memory-bounded, resumable dataset indexing (fix for the Windows import failures)

**Reported:** `setup.ps1 -SkipEmbeddingDownload -ImportAll -Versions latest-per-release` on a CPU-only PyTorch machine, BAAI/bge-m3,
32,440-record dataset: ETSI documents 2, 13 and 26 failed with memory allocation errors of about 1.17 GB and 8.59 GB.

### Root cause (read from the source; reproduced, not assumed)

It was not "too little RAM". Several defects combined so that the amount of memory requested depended on how oddly one chunk was shaped:

1. **No size limit on what reaches the model.** bge-m3 accepts up to 8,192 tokens and `LocalEmbedder.embed()` called
   `model.encode(all_texts_of_the_document, batch_size=32)` without lowering that limit. A transformer's attention-score tensor is
   `batch x 16 heads x tokens² x 4 bytes` – quadratic. sentence-transformers pads every text in a batch to the longest one, so one oversized chunk
   makes the whole batch of 32 pay for it: **32 x 16 x 2,048² x 4 = 8,589,934,592 bytes – exactly the "8.59 GB" in your log** (as is
   2 x 8,192; one 8,192-token text alone is 4.29 GB). I reproduced that exact figure with real PyTorch (`tests/test_memory_bounded_indexing.py`,
   address space capped at 5 GiB). I cannot derive the "1.17 GB" from the number alone – it is a smaller padded length or a different
   activation tensor – and I did not have your traceback or the dataset, so I cannot say which chunk of documents 2/13/26 was the large one.
2. **Chunks of unlimited size existed.** (a) Clause chunks: the dataset's clause text is carried into the chunk `content` verbatim
   (`gpp_dataset.normalise_clause`) with no length cap, in both apps. (b) The Platform copy of `utils/text_chunker.py` lacked the hard ceiling that the
   standalone ISLD copy got in v0.4.3, so text with no blank line and no sentence punctuation (ASN.1, table dumps, dotted contents lines) was emitted
   whole at any length.
3. **Everything for a document in one call.** All chunks of a document were chunked, embedded and upserted at once, so peak memory grew with document size.
4. **Parallel encodes.** `ETSI_IMPORT_BATCH_SIZE=10` workers each ran their own `encode()` in an executor thread at the same time (bounded only by the thread pool), multiplying the above.
5. **The failure was invisible.** `index_document()` swallowed every exception and returned `(None, 0)`; the report said only `outcome: index_failed`
   and the row said "Indexing failed" – no type, no message, no attempt count.

### A second defect found on the way (affects every document on the CLI path)

6. **The CLI never initialised the vector store.** `init_vector_store()` runs only in the FastAPI start-up. `python -m etsi_corpus.cli import-local`
   (what `setup.ps1 -ImportAll` runs) used an uninitialised `ChromaVectorStore`, so after embedding a document, `upsert` died with
   `'NoneType' object has no attribute 'upsert'` – swallowed and reported as "Indexing failed". Reproduced; fixed by a lazy `ensure_ready()`.
   If your log also contains `'NoneType' object has no attribute 'upsert'`, this is why. I could not see your full log, so I do not know how many of your
   documents hit it.

### Other defects fixed because they break resume/idempotency

7. Chroma re-indexing called `collection.get()` with no filter – every chunk id in the whole collection – for **each** document (`delete_by_id_prefix`).
8. Re-index deleted the old chunks first, then wrote the new ones: a failure in between left the document with nothing.
9. Qdrant point ids were `abs(hash(chunk_id)) % 10**9`; Python randomises string hashes per process, so a re-run created new ids and **duplicated every chunk**.
10. FAISS `upsert` appended duplicates on retry, and `delete_by_id_prefix` **re-embedded the whole remaining corpus** on every call.
11. An unexpected exception left a row in `importing`/`indexing` (invisible in reports until a later `recover`).

### Fixes

| # | Change | Where |
|---|---|---|
| 1 | Index-time sequence cap `EMBEDDING_MAX_SEQ_LENGTH` (default 1024 tokens). Chunks above it are **split into numbered, overlapping parts – never truncated**: id `<chunk>_p<k>`, metadata `parent_chunk_id`, `part_index`, `part_count`, `parent_char_start/end`, plus all parent metadata. Ordinary chunks keep their v0.4.4 ids/content. Search queries and CPARAS Bulk scoring are **not** capped. | `etsi_corpus/indexer.py`, `rag/vector_store.py` (`fit_to_limit`, `embedding_limits`) |
| 2 | Memory planner: batches are sized from token lengths so the estimated peak (scores + softmax + activations) stays inside `EMBEDDING_MEMORY_BUDGET_MB`; CPU batch ceiling `EMBEDDING_CPU_BATCH_SIZE` (8). Longest-first, so one long text no longer drags 31 others into a huge padded batch. | `rag/embedding_budget.py`, `rag/vector_store.py` |
| 3 | Out-of-memory handling: a failing batch is halved and retried; a text that cannot be embedded alone raises `EmbeddingMemoryError`; nothing has been written at that point. | `rag/vector_store.py` |
| 4 | One `encode()` at a time (semaphore + lock). | `rag/vector_store.py` |
| 5 | Streaming indexing: chunks are produced lazily and embedded + written `ETSI_INDEX_GROUP_CHUNKS` (64) at a time; only one group of vectors is in RAM. Record batches also flush by size (`ETSI_STREAM_BATCH_MAX_MB`). | `indexer.py`, `pipeline.py` |
| 6 | Large-document handling: documents above `ETSI_LARGE_DOC_CHARS` are flagged in the report and processed the same bounded way; above `ETSI_MAX_DOC_CHARS` they are **recorded as failed (`document_too_large`)**, not skipped. | `indexer.py` |
| 7 | All-or-nothing per document: new chunks first, stale chunks of the previous version removed only after every group succeeded; on any failure the chunks written by that attempt are removed. Memory errors are retried (`ETSI_INDEX_DOC_RETRIES`) with halved groups and a halved budget; free-RAM guard `ETSI_MIN_FREE_RAM_MB` / optional `ETSI_PROCESS_RSS_LIMIT_MB`. | `indexer.py` |
| 8 | Failure recording: every failed document gets `error_type`, message, stage, attempts, text size, chunks rolled back and a `retry_status` in the report (first 200 inline; **all** in `import_job_<id>_failures.jsonl`); rows store `[type] message`, `last_error_type`, `last_error_at`, `index_attempts`, `text_chars` (new nullable columns, auto-migrated). Counters for large docs, split chunks/parts, batch halvings, retried documents, zero-chunk documents, truncated texts. | `pipeline.py`, `core/database.py` |
| 9 | Honest outcome: report states `completed` / `completed_with_warnings` / `completed_with_errors` / `paused` / `failed`, `all_selected_indexed`, `unindexed` count; the CLI prints a summary and exits **0 only if every selected record is indexed**, 3 otherwise. | `pipeline.py`, `cli.py` |
| 10 | Pre-flight: if the embedding model or vector store cannot be opened the job fails immediately with the reason, before touching a document; 25 consecutive infrastructure failures stop the job. | `pipeline.py` |
| 11 | Resume/idempotency: completed documents are skipped; deterministic ids make a retry overwrite; `--only-failed` retries just the failed ones; unexpected exceptions now land in `import_failed`/`index_failed`. | `pipeline.py`, `cli.py` |
| 12 | New commands `verify-index [--limit N] [--fix] [--query ...]` (database vs vector store, per document) and `failures`. | `etsi_corpus/verify.py`, `cli.py` |
| 13 | Vector stores: lazy `ensure_ready()`; per-document `list_chunk_ids_for_document` / `delete_chunk_ids` (Chroma metadata filter, Qdrant scroll, FAISS); Qdrant ids deterministic (uuid5); FAISS idempotent upsert and delete via stored vectors. | `rag/vector_store.py` |
| 14 | Platform `text_chunker`: hard ceiling (`chunk_size x 5` characters) ported from ISLD, plus `hard_split_spans`. Output for ordinary text is byte-identical to v0.4.4 (tested against a verbatim copy). | `utils/text_chunker.py` |
| 15 | Every chunk now also carries `dataset_document_id` (the builder's `identity.document_id`). | `indexer.py`, `pipeline.py` |

### Tests

`tests/test_memory_bounded_indexing.py` (34 tests) + `tests/embedding_fakes.py`: the arithmetic of the observed numbers; real PyTorch reproducing the
8,589,934,592-byte allocation under a 5 GiB cap and the planned batches avoiding it; a 30-document import with oversized clauses in documents 2, 13, 26
(old path dies, new path completes, simulated attention allocation never above the machine's limit); OOM → halve/retry/rollback; first-index failure leaves no
chunks; idempotent retry (no duplicates), stale removal; resume after failure (only failed documents re-processed); `--only-failed`; crash recovery; fail-fast
pre-flight; failure report contents; the CLI exit code; verify-index detecting damage and `--fix` repairing it; size-based batch flush; FAISS/Qdrant fixes;
a real (random-weight) SentenceTransformer against the bounded encoder.

### Not verified (be aware)

* **No run against the real BAAI/bge-m3 weights or your 32,440-record dataset, and no Windows run.** The model could not be downloaded here and your dataset was
  not supplied. The embedding step is exercised with a simulated-allocation fake and with a real tiny random-weight SentenceTransformer.
* **No throughput numbers are claimed.** The defaults (`EMBEDDING_CPU_BATCH_SIZE=8`, 1536 MB budget, 1024-token cap) come from the memory arithmetic above,
  not from timing runs; tune them against Task Manager on your first run.
* The memory planner is an estimate (deliberately high). The model weights (~2.3 GB for bge-m3 in fp32) are additional.
* Qdrant list/delete (`scroll`, `MatchAny`) are written against the client API but were not run against a live Qdrant server; only the stable-id fix is tested.
* `verify-index` does not enumerate vector-store entries that have no database row.
* Parts of a split chunk do not repeat the clause heading in their text (the clause number/title are in metadata).
* `setup.ps1` / `common.ps1` are unchanged; use the CLI commands in `docs/WINDOWS_IMPORT_RUNBOOK.md`.

## v0.4.4 — validated 3GPP dataset integration

Added (all optional - with `CPARAS_DATASET_ROOT` unset the product behaves exactly as v0.4.3):

- **Shared dataset contract** `backend/core/gpp_dataset.py` (stdlib only): layout/inspection, streaming JSONL, safe in-place ZIP text
  reads, version selection that never drops unknown versions, history-sidecar checksum verification, corpus/reference schema checks.
- **Streaming, idempotent, resumable importer** (`etsi_corpus/pipeline.py`, `dataset_local.py`): reads `etsi_dataset.jsonl` +
  `etsi_dataset.zip` from `CPARAS_DATASET_ROOT`, per-job JSON reports, crash recovery, SQL-side counters, `--versions/--series/--specs/--limit`.
- **Lookup API** by specification / version / release / clause (`etsi_corpus/gpp_lookup.py`) and local-dataset endpoints
  (`/api/etsi-corpus/dataset/local/*`); the upload import now streams to disk with safe-ZIP checks.
- **CPARAS Bulk can score against the real clause corpus** (`research/bulk_corpus.py`, `BULK_CORPUS_SOURCE=auto|dataset|illustrative`),
  embeddings cached per dataset fingerprint. Bulk's 41 source columns and 21 `CPARAS_Bulk_*` columns are unchanged; run metadata gains
  `corpus_source` / `corpus_fingerprint`. With 2G/3G clauses in the corpus, a legacy-generation declaration that genuinely matches a
  legacy clause is no longer flagged as a generation conflict (the flag is unchanged for the illustrative corpus).
- **Setup integration**: `setup.ps1` finds the dataset beside the app folders, writes `CPARAS_DATASET_ROOT` to `backend\.env`, checks it and
  can import (`-DatasetPath`, `-SkipDataset`, `-ValidateDataset`, `-ImportDataset`, `-ImportLimit`, `-ImportAll`, `-Versions`).
  `setup.cmd` forwards all arguments. CLI: `status-local`, `validate-local`, `import-local`, `recover`.
- Tests: `test_gpp_dataset_integration.py`, `test_bulk_dataset_corpus.py`, `test_setup_dataset_step.py`. See `DATASET_INTEGRATION.md`.

## v0.4.3 (2026-10-09) — CPARAS Bulk ingestion & essentiality-evidence audit

An independent verification pass over the **CPARAS Bulk** functionality
(`api/routes/gitas/`, `etsi_bulk/`). The CPARAS ISLD pipeline in this product
(`api/routes/cparas/`, `research/engine.py`) was deliberately left untouched —
see the note under *Scope* below.

### Fixed — files that were rejected although every row in them was readable
- **A header below a title block was not found.** Only row 1 of a sheet was ever tried as
  the header, so a real export carrying a title/notes block above it (`ETSI IPR declarations
  export` / a generated-on date / a blank spacer) was rejected outright with
  "Unrecognized file format" (HTTP 422). The first 15 rows are now scanned, the title block
  is reported as a warning and preserved verbatim in the enriched export, and each record's
  `source_row_index` counts from below the real header. `etsi_bulk/excel_export.py` was
  updated in the same change — all three of its fidelity tiers *and* its post-generation
  validator had the header hard-coded to row 1, so without this the file would have ingested
  correctly and then had every row's enrichment written one row off.
- **Semicolon-, tab- and pipe-separated CSVs were rejected.** `_read_csv_rows` hard-coded a
  comma, so a CSV written by an Excel whose list separator is not a comma parsed as a single
  column and no header was recognised. It also hard-coded `utf-8-sig` with
  `errors="replace"`, corrupting cp1252/UTF-16 company names. Both now use this product's own
  `utils/csv_parser` helpers (`_decode`, `_sniff_delimiter`), which the single-declaration
  path already used.
- **A CSV had no row ceiling at all**, unlike the `.xlsx`/`.xls` readers. `MAX_ROWS` now
  applies to all three, with the same truncation warning.

### Fixed — essentiality could be asserted without evidence for it
- **"Essential" no longer requires nothing.** `Essential` asserts that a patent is essential
  to the standard its declarant *declared* it against. On the bulk path the only evidence for
  that is a top-1 match over a small illustrative corpus (11 spec families), so a row whose
  best match was a *different* family had no evidence for the claim the label makes. A 500-row
  sample came back 496 `Essential` while the customer's own acceptance rule (`Essential` =
  evidence level 3 = declared family confirmed) gave 9 — both derived from the same export.
  `etsi_bulk/scoring.py` now gates the label: without declared-family confirmation the
  strongest available label is `Potential`. The confidence number is **not** rewritten, only
  the label, so the evidence stays inspectable.
- **`CPARAS_Bulk_Evidence_Level` and `CPARAS_Bulk_Essentiality` now agree.** Level 2 keeps its
  meaning ("scored as strongly as an Essential row, but against a family other than the one
  declared") and is now labelled accordingly instead of sitting next to the word "Essential".
- **`CPARAS_Bulk_Corpus_Coverage` reported the wrong thing.** It was set to
  `declared_family_match`, which collapsed two different situations into one: "the reference
  corpus has no entry for your declared standard at all" (no confident verdict is possible)
  and "the corpus does cover it but the scorer matched a different family" (a real
  disagreement, already reported as `DECLARATION_CONFLICT`). Coverage is now an independent
  property of the corpus, and the first case gets its own reason code,
  `DECLARED_FAMILY_NOT_IN_CORPUS`.

### Fixed — other
- **The country donut and country filter were keyed on word fragments.**
  `schema.country_code` took the first whitespace token and truncated it to three characters,
  assuming every cell read `"US UNITED STATES"`. The export shipped in `sample_data/` does
  not: it carries `"UNITED STATES"` and `"EUROPEAN PATENT OFFICE"`, which became `"UNI"` and
  `"EUR"`, and a file mixing `"US"` with `"UNITED STATES"` produced two buckets for one
  jurisdiction. Names and the `"<code> <NAME>"` form now resolve to a real code; an
  unrecognised value is kept verbatim rather than cut to a fragment that looks like a code.
- **`MAX_UPLOAD_MB` was configured but enforced nowhere.** `POST /api/sep/upload` buffered the
  entire request body, parsed it with the whole workbook resident, and stored it again as
  `original_file_blob` — one oversized upload could exhaust the backend's memory, the same
  "it just crashes" symptom large uploads were reported with. The limit is now enforced
  (HTTP 413), and an empty upload returns 400 instead of a format error.
- **`tests/_run_all.py` reported false failures.** The stdlib runner called every `test_*`
  function with no arguments, so every fixture-using or parametrized test raised `TypeError`
  and printed as `[FAIL]`. They are now reported as skipped-needs-pytest, and async tests
  actually run.
- **`test_batch_cancellation_stops_queued_children_and_prevents_late_start` was flaky.** It
  slept a fixed 0.15 s before cancelling; under full-suite load the first child had not always
  started, and the test failed with nothing wrong in the code under test. It now polls.

### Verified, deliberately unchanged
- Source-row preservation: every non-blank row becomes exactly one record, in order, with
  repeated identifiers kept (reproduced at parser, storage and export level).
- Batched scoring, incremental persistence, pause/stop, resume-from-stored-rows, and
  restart recovery; one bad row is isolated as FAILED without taking down its batch.
- `schema.resolve_header`'s token-overlap fuzzy matching with its tie guard — no substring
  hazard, left alone.
- Embeddings honour `EMBEDDING_DEVICE` (v0.4.2) through `rag/vector_store.resolve_embedding_device`.
  The bulk scoring path uses the embedder only; it makes no Ollama calls, so the v0.4.2
  `num_ctx`/seed work applies to the ISLD path, not here.
- Scoring is reproducible for a given file: the same workbook scored twice gives identical
  confidences (measured).

### Scope
Changes are confined to the bulk path (`etsi_bulk/*`, `api/routes/gitas/dashboard.py`). The
declared-family gate was deliberately applied in `etsi_bulk/scoring.py` and **not** in the
shared `research/three_signal_scoring.py` / `research/engine._predicted_status`, because the
single-declaration ISLD pipeline in this same product has LLM-graded claim mappings as
additional evidence for the same assertion and its thresholds are its own.

### Known limitations (measured, not fixed)
- The offline reference corpus holds **54 clause entries across 11 spec families**
  (`research/standards_corpus.py`). A real declaration set spans the whole 3GPP catalogue, so
  most rows are matched against a topical neighbour rather than their declared standard. The
  new `DECLARED_FAMILY_NOT_IN_CORPUS` reason code makes that visible per row, but the only
  real remedy is indexing the full archive.
- `semantic_similarity` is calibrated against the run's own 5th-percentile floor, so a row's
  score depends slightly on what else was in the same upload (measured: 0.6715 alone vs
  0.6721 in a 500-row file — a difference too small to move a label, but not zero). Left as
  the authors designed it; a fixed floor would shift every historical label.
- `_ReservoirSampler.add_many` iterates in Python over every patent×corpus similarity. At the
  current corpus size that is 0.58 s on a 17,000-row run (measured) — negligible. At a
  2,000-entry corpus it is 22 s, and it scales linearly, so it will need vectorising before
  the full 3GPP archive is indexed.

### Tests
382 passed, 2 skipped (was 349 passed, 2 skipped) — 33 new regression tests in
`backend/tests/test_bulk_ingestion_audit.py`.

## v0.4.2 (2026-10-07) — GPU use, phi3.5 HTTP 500, reproducible results, local 3GPP archive indexing

### Fixed — GPU not used
- **The model was pushed off the GPU.** Every request asked Ollama for a fixed 24,576-token
  context. Ollama reserves the KV cache for the whole window when it loads a model, so on a
  4–6 GB laptop / external RTX the model no longer fit and Ollama moved layers to the CPU.
  New `core/ollama_runtime.py` reads each model's architecture from `/api/show`, and sizes
  `num_ctx` so weights + KV cache + compute buffer fit in the detected VRAM (never below
  `OLLAMA_MIN_NUM_CTX`, never above `OLLAMA_NUM_CTX` or the model's own maximum). Long
  declarations are split into proportionally smaller chunks so nothing is truncated.
  `OLLAMA_FIT_CONTEXT_TO_GPU=false` restores the old behaviour.
- **"No GPU detected"** although an NVIDIA GPU was present. `nvidia-smi` is now also looked up
  outside PATH (System32, `NVIDIA Corporation\NVSMI`, DriverStore, WSL), run in a way that works
  on every asyncio event loop, tolerated when it exits non-zero with valid output, and the
  largest GPU is used when several are present (external RTX + internal GPU). Windows registry
  fallback for NVIDIA/AMD cards. The panel now says *why* nothing was found.
- **Embeddings always ran on the CPU** (`EMBEDDING_DEVICE=cpu`, CPU-only PyTorch wheel). New
  default `EMBEDDING_DEVICE=auto`; `setup.ps1` installs the CUDA build of PyTorch when an NVIDIA
  driver is present (`-NoCudaTorch` to skip).
- New **GPU row** in the Local model panel: Ollama's own report (`/api/ps`) of how much of the
  loaded model is in VRAM ("100% GPU", "60% GPU / 40% CPU"), plus the planned context.
  `GET /api/models/gpu` returns the same data.

### Fixed — phi3.5 crashes with HTTP 500
- phi3.5 has no grouped-query attention: ~384 KiB of KV cache per token, i.e. ~9 GiB at 24,576
  tokens, which Ollama could not allocate → HTTP 500. Its context is now sized to the card.
  `.env.example` enables `OLLAMA_FLASH_ATTENTION=true` + `OLLAMA_KV_CACHE_TYPE=q8_0`, which halve
  the context's memory; `start.ps1`/`start.sh` pass them to Ollama when they start it and
  `setup.ps1` stores them for the Ollama tray app (restart Ollama once).
- Output length (`num_predict`) is clamped so prompt + answer stay inside the window.
- An HTTP 500 from Ollama now comes with an actionable hint.

### Fixed — results differ between runs
- Every request carries a fixed `seed` (`OLLAMA_SEED`, default 42) and greedy sampling at
  temperature 0.
- The context size is a function of the model and the card's total VRAM only (not of momentary
  free memory), so the model is loaded the same way, with the same GPU/CPU split, every time.
- `keep_alive` (`OLLAMA_KEEP_ALIVE=30m`) keeps the model loaded between files.
- The **Test** button used to load the model with a 2,048-token context; the analysis then
  reloaded it at 24,576 and could fail — "Ready" in the panel, HTTP 500 in the analysis. The test
  now uses exactly the analysis' context and options and reports the GPU/CPU split.

### Added — local 3GPP archive indexing
- `index_3gpp.cmd` / `index_3gpp.ps1` and `python -m ingestion.spec_corpus` index a folder of
  extracted 3GPP files: `3gpp_<spec>_v<x.y.z>.json`, extracted `.docx`, or the original `.zip`,
  in any sub-folder layout. Default folder `backend/data/3gpp_archive` (`THREEGPP_ARCHIVE_DIR`).
  Only the latest version of each spec is indexed unless `-AllVersions`. Resumable.
- `POST /api/scrape/index-local`, `GET /api/scrape/index-local/status` (used by the script when
  the app is running).

### Upgrade note
Run `setup.cmd` once: it adds `OLLAMA_FLASH_ATTENTION=true` and `OLLAMA_KV_CACHE_TYPE=q8_0` to an
existing `backend/.env` (other values are left alone), installs CUDA PyTorch and stores the Ollama
settings. If `backend/.env` says `EMBEDDING_DEVICE=cpu`, change it to `auto`. Then quit Ollama
(tray icon → Quit) and start the app with `start.cmd`.

## v0.4.1 (2026-10-01) — Overall Confidence & Essentiality columns, Semantic Similarity fix

### Fixed — Overall Confidence, Essentiality and Semantic Similarity
- **Semantic Similarity was 100% for (almost) every patent.** The 0–100% scale was anchored on
  the 95th percentile of *all* patent × standard-clause pairs, but the reported value is each
  patent's *best* match out of ~54 corpus clauses (its top ~2%), which sits above that anchor by
  construction and was clipped to 100%. Measured on `sample_data/sample_bulk_export.xlsx`: 60/60
  rows at exactly 100%. New `research.embeddings.calibrate_match_scores`: same run-specific
  floor (5th percentile = unrelated-text baseline → 0%), ceiling = a perfect cosine match (1.0).
  Ordering is unchanged; best matches on the sample now range ~43–64%.
- **Overall Confidence now means "confidence this patent is an SEP".** One shared formula,
  `research.three_signal_scoring.overall_sep_confidence`: weighted blend of semantic similarity
  (0.40), claim match (0.25), standard coverage (0.15), declared-family match (0.20) and — deep
  mode only — the LLM's SEP-analysis confidence (0.20), renormalised over the signals present,
  with bounded penalties for an unverified (×0.90) or colliding (×0.85) application number.
  Extraction-quality signals (completeness, OCR) no longer move it.
- **The Essential / Potential / Non-Essential label is derived from Overall Confidence**
  (≥ 0.60 Essential, ≥ 0.40 Potential, else Non-Essential), so the label and the percentage can
  no longer contradict each other (the "Potential SEP / 100%" case). ISLD statuses map onto the
  same bands: Confirmed/Likely SEP = Essential, Potential SEP = Potential, Insufficient
  Evidence = Non-Essential.
- **CPARAS Bulk: Overall Confidence was a copy of Semantic Similarity** — only similarity was fed
  into the blend on the bulk path. Claim match, standard coverage and declared-family match now
  count too.
- **CPARAS Bulk: expired/ceased patents are no longer silently relabelled Essential → Potential**
  (that changed the label but not the percentage). Essentiality is technical; the lapsed legal
  status is still flagged via the `LEGAL_STATUS_LAPSED` reason code and now also a validation note.
- **CPARAS Bulk: declared-family tie-break.** When the best clause in the family the patent was
  *declared* against scores within 0.02 (raw cosine) of the overall best clause, the declared one
  is used. A clearly better match elsewhere still wins, so real declaration conflicts stay visible.

### Added — export columns
- CPARAS Bulk export (`*_CPARAS_Bulk_enriched.xlsx`): **`CPARAS_Bulk_Essentiality`**
  (Essential / Potential / Non-Essential) and **`CPARAS_Bulk_Overall_Confidence`** (0–1), right
  after `CPARAS_Bulk_Status` (which remains the processing status SUCCESS/FAILED). Summary sheet
  gains an "Essentiality Distribution" section and an average overall confidence.
- CPARAS Bulk export: **`CPARAS_Bulk_Classification`**, placed directly after
  `CPARAS_Bulk_Validation_Notes` — one word (Essential / Potential / Non-Essential) extracted from
  the note ("Classified Potential — …" → Potential, "Classified Essential but …" → Essential);
  rows whose note is blank or names no class use the row's stored essentiality.
- CPARAS ISLD export (`*_CPARAS_ISLD_enriched.xlsx`) and the CSV export:
  **`CPARAS_Essentiality`** and **`CPARAS_Overall_Confidence`**.

### Changed
- **`*_Extraction_Confidence` now means extraction quality, as the name says.** Previously it held
  the SEP confidence (now `*_Overall_Confidence`). ISLD: mean of the row's field-completeness
  score and the share of application/publication/standard values verified against the source
  text. Bulk (already structured, no extraction model): share of the row's key source fields
  present (patent number, title, declared standard, spec number, spec version, company,
  illustrative part).
- Bulk validation: the "material declaration conflict" override bar is now `HIGH_CONFIDENCE`
  (0.75); the old bar (0.60) coincides with the new Essential threshold and could never trigger.

### Upgrade note
Runs scored before 0.4.1 keep their old (saturated) similarity / confidence values in the
database. **Re-upload a workbook / re-run a declaration** to get the corrected values.

## v0.4 (2026-09-26) — Text Matching, layout and Windows launcher fixes

### Fixed
- **Windows: frontend failed to start when the folder path contains a space** (e.g. a user
  folder like `C:\Users\FIRST LAST\...`): "Cannot find module 'C:\Users\FIRST'". Windows
  PowerShell 5.1 passed Vite's absolute path to Node unquoted; `start.ps1` now passes a quoted
  relative path as one argument string (launcher r2, shown in the start banner). Verified by running
  `start.ps1` end-to-end from a folder whose name contains a space and `(1)`.
- **Page no longer runs past the right edge during an analysis.** The Pause / Stop buttons in
  Analysis Progress reused the retry button's `width: 100%`, so each took the full panel width
  and made the whole page ~100px wider than the window (horizontal scrollbar, sidebar pushed
  off-screen). They now have their own compact style, and Stop is shown in red.
- **Analysis setup panel is now scrollable.** On shorter screens the bottom of the panel
  (model status rows, Analyze button, capability list) was cut off with no way to reach it:
  `.isld-scope .panel { overflow: hidden }` overrode the panel's own `overflow-y: auto`. The
  header now stays fixed while the sections below scroll (mouse wheel, trackpad or keyboard),
  with a visible thin scrollbar in light and dark themes.
- **Uploaded ISLD annexes (CSV/XLSX/XLS) were matched as one blob.** All records were joined
  into one string and re-chunked, so a "segment" mixed several records. Each declared record
  is now its own query, labelled `Record N · <application/publication no.>`.
- **Standard-only records gave meaningless matches.** Many ETSI annexes (e.g. Nokia
  ISLD-202409-029) carry no title or clause for Indian applications, so the query was just
  "Standard: TS 38.331". These records now query with the declared spec's scope (title +
  description from the built-in 3GPP catalog) and results are restricted to the declared
  standard(s). A warning says how many records had no title/clause.
- **Continuation rows** (a row carrying only extra standards) are merged into the disclosure
  above, the same as Declaration Analysis, instead of becoming extra standard-only records.
- **Space-separated standards in one cell** ("TS 38.331 TS 38.213 TS 38.214") were kept as a
  single standard. They are now split (also fixes Declaration Analysis / Bulk CSV parsing).
- `/text-match/match-file` defaulted to the 3GPP corpus only, silently skipping the ETSI
  Standards Corpus; it now uses hybrid search like `/match`.
- Matches from a declared standard are tagged **declared** and listed first (scores unchanged).
- Starting a new run clears the previous run's matches (they used to stay on screen under
  "Matching…").

### Added
- **Collapsible sidebar.** A button at the left of the top bar collapses the sidebar to a 64px
  icon rail (icons show their names on hover) and expands it again; the choice is remembered
  in the browser. On narrow screens the existing menu button is used instead.
- **Pause / Resume / Stop for Text Matching.** Matching runs as a backend job
  (`POST /api/text-match/jobs/text|file`, `GET /jobs/{id}`, `POST /jobs/{id}/pause|resume|stop`)
  with a live progress bar ("Matching 12 of 27 records…") and results that appear as each
  record finishes. Control applies between records/segments; Stop keeps the results so far and
  the PDF report can still be generated. The one-shot `/match` and `/match-file` endpoints are
  kept for API compatibility.
- `backend/tests/test_text_match_jobs.py` (9 tests: per-record units, continuation merge,
  declared-spec restriction, ETSI spec-number normalisation, pause / resume / stop, failures).

## v0.4 (2026-09-26, part 2) — reliability, Ollama and Windows release

### 1. Critical bugs fixed
- **"Local LLM offline" while Ollama was running** — `health_check()` returned
  `{"status": "ok", ...}` but `/api/health` read `result.get("available")`. Replaced by one
  health contract used everywhere (§2).
- **Bulk runs stalled / 3 failing tests in the original package**: embedding-model loading
  retried downloads from huggingface.co indefinitely on an offline machine. The model now loads
  only from the local cache (setup downloads it); otherwise scoring uses the offline TF-IDF
  embedder immediately and `/api/health` says so. Test suite: 16 min → 45 s.
- **A run interrupted by a restart stayed "processing" forever** (neither stoppable nor
  resumable). At startup such runs become *paused* (resumable from the last stored row) or
  *completed* if every row was already stored; the stale message is cleared on Continue.
- **CPARAS ISLD**: publication numbers with kind codes (`US11000053B2`) were erased; every
  disclosure was labelled "5G"; the CSV "Project or Standard name" value was discarded; model
  failures on every chunk produced an empty "successful" analysis. All fixed (details in the
  CPARAS ISLD notes below).
- `.docx` Text Matching uploads were mis-detected as CSV.

### 2. Ollama fixes
- Shared contract (`core/ollama_client.py`): `reachable`, `available`, `state`, classified
  `models` (text / vision / embedding), configured-model availability, `generation`
  (untested / ready / failed from real generations only), `error`, `hint`.
- Text vs vision separation (`moondream` = OCR only), `:latest` resolution, validation of the
  model that will actually run, never a silent substitute; `POST /api/models/test` smoke test.
- Ollama errors (404 model not found, 500 out of memory, mid-stream errors) surfaced verbatim.
- `127.0.0.1` default, loopback proxy bypass, `OLLAMA_HOST` honoured, `qwen2.5:7b` default
  everywhere (setup used to pull Mistral), `OLLAMA_CONCURRENCY` 2 → 1 enforced process-wide.
- Vision OCR fallback verifies the model is installed first, caps pages, stops after repeated
  failures.

### 3. Windows fixes
- New `setup.ps1` / `start.ps1` (+ `.cmd` wrappers; PowerShell 5.1 & 7): reuse a running
  Ollama, start it only if nothing answers, identify a non-Ollama owner of port 11434, poll
  readiness with timeouts, validate the model, print URLs + health, stop children on Ctrl+C.
  Linux/macOS `setup.sh` / `start.sh` rewritten with the same logic (no Qdrant/ChromaDB
  flip-flopping, no network "bootstrap" crawl, no `sleep`-based readiness).
- Tesseract auto-detection in standard Windows install folders; `.env`/data paths resolved
  relative to the backend folder; non-ASCII filenames in download headers.

### 4. Frontend fixes
- CPARAS ISLD local-model panel with separate server / models / selected / available /
  generation rows and a **Test** button; text models only; server default labelled; no silent
  model swap; 20 s re-check.
- Header status chip names the real problem (LLM unreachable, default model missing, embedding
  model missing) with details in the tooltip.
- CPARAS ISLD: **Excel** export button, "Extraction may be incomplete" and "Not saved" banners.
- Removed dead SSE client; version labels and release notes updated; favicon added.

### 5. Backend fixes
- Removed `services/patent_workflow.py` — leftover C-SARAS claim-workflow code that no route
  mounted (categorisation now lives in the separate Initial Categoriser).
- Response headers renamed `X-GITAS-*` → `X-CPARAS-Bulk-*`; Docker container names
  `sep_*` → `cparas_platform_*` (volumes unchanged, data kept).
- Settings ignore unknown `.env` keys; removed unimplemented `AUTH_ENABLED`/`SECRET_KEY`;
  vLLM default port 8010; startup LLM status line; service name in every log line; health /
  progress polls no longer flood the access log; background runs held by strong references.
- `/api/health` adds `embeddings` and `ocr`; ChromaDB telemetry disabled; `posthog` pinned
  (a fresh install otherwise logged ChromaDB telemetry errors on every start).
- CORS limited to this app's origins (5173 / 4173 / 3000 + `CORS_EXTRA_ORIGINS`).

### 6. Pipeline fixes
- PDFs: password-protected / corrupt / empty files rejected with a specific message; real page
  count; one Tesseract pass per page (was two) with a per-page timeout.
- CPARAS ISLD chunk failures are collected: all failed → per-file error with the cause; some
  failed → flagged incomplete with reasons.
- Save failures are reported on the result instead of being swallowed.

### 7. Export fixes
- CPARAS Bulk enriched Excel: appended `CPARAS_Bulk_*` values, CSV-rebuilt sheets and
  reconstructed sheets store `=...` strings as text (no formula injection); failed-rows CSV is
  formula-neutralised; RFC 5987 filenames.
- CPARAS ISLD Excel: declaration fields first, then `CPARAS_*` columns; formula-safe.

### 8. Security / reliability fixes
- Dataset zip import rejects `..` / absolute paths and non-zip files; model-pull names
  validated; Docker nginx timeout raised to 3600 s for long analyses; embedding cache volume.
- Tests run in a temporary data folder; removed bundled test artefacts (`backend/data`,
  `data/` with a test-generated "knowledge package" and `chroma.sqlite3`), `FIXES.patch`, and a
  hard-coded developer path (`/mnt/user-data/...`) in tests.

### 9. Tests performed
- `pytest` 312 passed, 2 skipped (original: 306 passed, 3 failed, 16 min) — incl. the new
  health-contract tests.
- Bulk end-to-end over HTTP: 1,201-row workbook with 500 repeated identifiers → paused at row
  256 → continued → 1,201 stored rows, export Sheet1 with all rows in source order and the
  original custom column intact, formula-like title stored as text; crash simulation (status
  forced to processing, rows deleted past 600) → recovered as paused → continued → 1,201 unique
  rows.
- CPARAS ISLD flows as in the CPARAS ISLD notes (fake Ollama with the four reference models).
- Frontend: `npm ci`, `tsc` + production build, eta tests 13/13, all routes incl. legacy
  redirects rendered in headless Chromium without console errors.
- Scripts executed (PowerShell 7 / bash) for Ollama running / not running / port conflict /
  not installed.

### 10. Remaining limitations
- LLM tested against an Ollama-compatible fake server; embedding-model-loaded path not
  exercised (no Hugging Face access here); scripts not executed on an actual Windows host.
- Internal module names (`gitas/`, `cparas/`, `/api/sep`, `CPARAS_Bulk_*` columns built by
  `GITAS_COLUMNS`) keep their historical names on purpose (database and integration
  compatibility); only user-facing names changed.

### CPARAS ISLD module — details
Identical to the standalone CPARAS ISLD v0.4 fixes: kind-code publication numbers, no
fabricated "5G" project, project/standard column kept, chunk-failure surfacing, save-failure
banner, Excel button, model validation before analysis, PDF hardening.
