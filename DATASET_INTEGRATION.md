# CPARAS Platform (Bulk + ISLD) v0.4.4 - 3GPP dataset integration

> **v0.5.0:** the API endpoints below require a signed-in user with the Data Entry or Super Admin role (changing reference data: Super Admin). Open them in the same browser in which you are signed in to CPARAS, or send `Authorization: Bearer <token>` (see `docs/AUTH.md`).

**The dataset** is the output folder of the dataset builder (`build_cparas_dataset_chatgpt.py`), e.g.
`D:\3gpp\cparas_dataset\build_v4`. The dataset file itself is **`etsi_dataset.jsonl`** (its full text is in `etsi_dataset.zip`; CPARAS Bulk additionally reads `standards_corpus_3gpp.json`). Keep the whole folder together.
The dataset is **not** bundled in this ZIP and is never modified.

## Where the dataset goes

Put the build folder in the **same parent folder as the CPARAS app folders**; setup finds it by itself:

```
<parent folder>\
  CPARAS_Platform_Revamped\        (CPARAS Bulk + ISLD)
  CPARAS_ISLD\                     (standalone ISLD)
  Initial_Categoriser\
  cparas_dataset\build_v4\        <- the dataset build folder (read-only; shared by all three)
      etsi_dataset.jsonl           <- THE dataset (one JSON record per specification version)
      etsi_dataset.zip             <- the extracted full text of those records
      spec_history_blobs.jsonl.gz  <- externalised revision history (checksummed)
      spec_reference_3gpp.json     <- per-specification / per-series reference
      standards_corpus_3gpp.json   <- clause-level corpus
      build_report.json, validation_report.json (+ ledger/excluded/unidentified .csv)
```

It is configured by one setting, `CPARAS_DATASET_ROOT` in `backend\.env` (a folder, quotes optional, spaces allowed).
Priority: a `CPARAS_DATASET_ROOT` environment variable > `backend\.env`. No path is hard-coded anywhere.

## Setup (Windows PowerShell)

Keep the dataset build folder in the same parent folder as the app folders (see above), then:

```powershell
cd <parent folder>\CPARAS_Platform_Revamped
powershell -ExecutionPolicy Bypass -File .\setup.ps1 -PullModel     # or double-click setup.cmd
```

`setup.ps1` (via `setup.cmd`, which forwards every argument) now ends with a **3GPP dataset** step. It

1. looks for `etsi_dataset.jsonl` beside the app (parent folder, `..\cparas_dataset\build_v4`, `..\build_v4`,
   `..\dataset`, or any sibling folder / sub-folder that contains it),
2. writes `CPARAS_DATASET_ROOT=<that folder>` into `backend\.env` (fills a blank value; never replaces a value you set),
3. checks the files (required files present, builder version compatible, record count) using the app's own code,
4. prints how to import - it does **not** import by default.

If the dataset lives elsewhere: `.\setup.ps1 -DatasetPath "D:\3gpp\cparas_dataset\build_v4"` (this *does* update `.env`).
Skip the step with `-SkipDataset`. Without a dataset the app behaves exactly as v0.4.3.

Import (opt-in - indexing thousands of documents takes a long time; the setup step never starts it unless you ask):

```powershell
.\setup.ps1 -ImportDataset                          # 25-document trial batch
.\setup.ps1 -ImportDataset -ImportLimit 500         # first 500
.\setup.ps1 -ImportAll -Versions latest-per-release # everything selected (hours; resumable - just run it again)
.\setup.ps1 -ValidateDataset                        # full streaming validation of the 3 GB JSONL (minutes)
```

Or directly (same engine, from `backend\` with the venv):

```powershell
cd backend
.\.venv\Scripts\python.exe -m etsi_corpus.cli status-local
.\.venv\Scripts\python.exe -m etsi_corpus.cli validate-local [--crc]
.\.venv\Scripts\python.exe -m etsi_corpus.cli import-local --limit 25
.\.venv\Scripts\python.exe -m etsi_corpus.cli import-local --all-rows --versions latest-per-release
.\.venv\Scripts\python.exe -m etsi_corpus.cli recover        # after a crash, then re-run import-local
```

## What the dataset does in this product

| Part | Uses | Effect |
|---|---|---|
| **CPARAS ISLD** (Standards Corpus / Text Matching) | `etsi_dataset.jsonl` + `etsi_dataset.zip` (+ `spec_history_blobs.jsonl.gz`) | Streaming, resumable import into the existing Standards Corpus (`EtsiDocument`) and vector index. Full text is read **in place from the ZIP** (no `text\` folder is needed). Text Matching behaves as before; they simply find more specifications. |
| **CPARAS Bulk** | `standards_corpus_3gpp.json` | Bulk scores each declaration row against the **real clause corpus** instead of the 54-entry illustrative one. The 41 source columns and 21 `CPARAS_Bulk_*` columns keep their names, order and meaning; the run metadata sheet records `corpus_source` and `corpus_fingerprint`. |

`BULK_CORPUS_SOURCE` in `backend\.env`: `auto` (default - dataset corpus when `CPARAS_DATASET_ROOT` holds a valid
`standards_corpus_3gpp.json`, otherwise the illustrative corpus, with the reason in the run metadata) /
`dataset` (require it; error if unusable) / `illustrative` (never use it = exactly v0.4.3 behaviour).
The first Bulk run against the dataset corpus embeds all clause entries once; the vectors are cached in
`backend\data\cache` per dataset fingerprint and embedding model.

Scores remain heuristic screening signals. **A semantic match is never proof of essentiality.**

## API additions (prefix `/api/etsi-corpus`)

| Endpoint | Purpose |
|---|---|
| `GET /dataset/local/status` | Configured? files present? build facts, what is imported, plain-language diagnosis of a partial / stale import |
| `POST /dataset/local/validate?crc=&history=` | Streaming validation (schema, text pairing, duplicates, unknown versions, history checksums) |
| `POST /dataset/local/import?limit=&all_rows=&versions=&series=&specs=&copy_text=` | Background import + index from the **configured** folder (no client-supplied path is ever accepted) |
| `GET /dataset/jobs/{id}/report` | Per-job JSON report (counts for every outcome - nothing is dropped silently) |
| `POST /dataset/recover` | Reset documents left half-processed by a crash; the next import retries them |
| `GET /standards/specs`, `/standards/{spec}`, `/standards/{spec}/versions`, `/standards/{spec}/clauses?version=&release=&clause=` | Specification-, version- and clause-aware lookup (database reads - no similarity score) |
| `POST /dataset/import` | The existing upload import - now streamed to disk with safe-ZIP checks instead of held in memory |

## Guarantees

* The dataset folder is **read-only**; nothing is copied into the app unless you pass `--copy-text` / `copy_text=true`.
* **Streaming, bounded memory**: the 2.9 GB JSONL is read line by line; ZIP members are read one at a time.
* **Idempotent and resumable**: documents are keyed by SHA-256 of their `pdf_url`; re-running skips completed documents,
  retries failed ones, and an interrupted run continues where it stopped.
* **Nothing silently dropped**: malformed lines, duplicates, missing text, records filtered by `--versions` / `--series` /
  `--specs`, and unknown-version records are each counted in the job report. Records with an **Unknown** version are
  always kept (and shown as `Unknown`, never guessed; "latest" is chosen only among known versions).
* **History sidecar** (`spec_history_blobs.jsonl.gz`): every pointer is resolved by SHA-256; a missing or tampered blob
  is an error for that record, never replaced by a stand-in.
* **Safe ZIP handling**: path-traversal / absolute / drive-letter member names are rejected; integrity is checked.
* Builder-version / schema mismatches are reported, not guessed around.

## Tests

```powershell
cd backend
.\.venv\Scripts\python.exe -m pytest tests -q
```

Added: `test_gpp_dataset_integration.py` (22), `test_bulk_dataset_corpus.py` (7), `test_setup_dataset_step.py` (3).
All of them use small synthetic fixtures built by the real dataset builder (`tests\fixtures\build_sample`); none is
a test against the full 32,440-record dataset. Tests that drive `setup.ps1` helpers need PowerShell and are skipped otherwise.

## Known limits

* The full 32,440-record dataset was **not** imported or scored in development; behaviour at that scale is covered by
  streaming design and synthetic tests only. Start with `-ImportDataset` (25 documents), then `-ImportAll`.
* A full import embeds every document: expect hours, and use the GPU build of PyTorch (setup installs it when an NVIDIA GPU is present).
* There is no UI card yet for the local-dataset workflow; use `setup.ps1 -Import...`, the CLI or the API above.
