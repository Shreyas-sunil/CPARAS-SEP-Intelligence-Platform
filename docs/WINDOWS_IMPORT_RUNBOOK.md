# Windows runbook – memory-safe dataset import (v0.4.4.1)

All commands are Windows PowerShell. Replace the paths with yours. Nothing here downloads the embedding model
(`-SkipEmbeddingDownload` semantics: the model must already be cached, as in your v0.4.4 run).

## 0. One-time: install the one new dependency and (optionally) tune memory

```powershell
cd D:\path\to\CPARAS_Platform_Revamped\backend
.\.venv\Scripts\python.exe -m pip install -r requirements.txt     # adds psutil (free-RAM guard); everything else is already installed
```

Optional, in `backend\.env` (defaults are already conservative; shown for a CPU-only machine):

```
EMBEDDING_DEVICE=cpu
EMBEDDING_MAX_SEQ_LENGTH=1024      # longest sequence the importer gives the model; longer chunks are split into parts
EMBEDDING_CPU_BATCH_SIZE=8         # texts per forward pass on CPU
EMBEDDING_MEMORY_BUDGET_MB=1536    # planned working memory per forward pass (the model weights, ~2.3 GB, are extra)
ETSI_IMPORT_BATCH_SIZE=4           # documents prepared at once (embedding itself is always one batch at a time)
ETSI_INDEX_GROUP_CHUNKS=64         # chunks embedded + written per group
ETSI_MIN_FREE_RAM_MB=768           # below this much free RAM the importer waits, then fails that document as retryable
```

Lower `EMBEDDING_MEMORY_BUDGET_MB` / `EMBEDDING_CPU_BATCH_SIZE` first if Task Manager shows the machine running short.
These are planning limits based on a memory estimate, not measured guarantees; check Task Manager on your first run.

## 1. Small test import (30 records – this covers the early records where documents 2, 13 and 26 failed)

```powershell
cd D:\path\to\CPARAS_Platform_Revamped\backend
$env:CPARAS_DATASET_ROOT = "D:\3gpp\cparas_dataset\Build_V4"
.\.venv\Scripts\python.exe -m etsi_corpus.cli import-local --limit 30 --versions latest-per-release
"exit code: $LASTEXITCODE"
```

Exit code `0` means every selected record is indexed. `3` means something is NOT indexed – the summary printed at the end
says what and why, and the same facts are in `backend\data\etsi_corpus\output\reports\import_job_<id>.json`
(failed documents: `import_job_<id>_failures.jsonl`).

Documents left `index_failed` by your earlier v0.4.4 run are retried automatically by this command.

## 2. Verify the test import

```powershell
.\.venv\Scripts\python.exe -m etsi_corpus.cli failures                 # exit 0 = nothing failed; lists reason per document otherwise
.\.venv\Scripts\python.exe -m etsi_corpus.cli verify-index --query "random access preamble transmission"
"exit code: $LASTEXITCODE"
```

`verify-index` checks that every `completed` document has exactly the chunks the database says it has, counts documents that are not
indexed, and runs one real search. If it reports mismatches (for example documents marked completed by the old code whose chunks
never reached the index):

```powershell
.\.venv\Scripts\python.exe -m etsi_corpus.cli verify-index --fix       # marks only the mismatched documents index_failed
.\.venv\Scripts\python.exe -m etsi_corpus.cli import-local --limit 30 --versions latest-per-release   # re-indexes exactly those
```

## 3. Full import

```powershell
cd D:\path\to\CPARAS_Platform_Revamped\backend
$env:CPARAS_DATASET_ROOT = "D:\3gpp\cparas_dataset\Build_V4"
.\.venv\Scripts\python.exe -m etsi_corpus.cli import-local --all-rows --versions latest-per-release
"exit code: $LASTEXITCODE"
```

(`.\setup.ps1 -DatasetPath "D:\3gpp\cparas_dataset\Build_V4" -SkipEmbeddingDownload -ImportAll -Versions latest-per-release` runs the same
importer after the normal setup steps.)

* Safe to stop with Ctrl+C at any time and run the same command again: finished documents are skipped, an interrupted one is
  rolled back/overwritten by id – nothing is duplicated.
* If it ends with exit code 3, retry only the failures once you have changed the cause (more free RAM, a smaller budget):

```powershell
.\.venv\Scripts\python.exe -m etsi_corpus.cli import-local --all-rows --versions latest-per-release --only-failed
```

## 4. Verify the full import

```powershell
.\.venv\Scripts\python.exe -m etsi_corpus.cli failures
.\.venv\Scripts\python.exe -m etsi_corpus.cli verify-index --query "radio link failure detection"
"exit code: $LASTEXITCODE"
```

Treat the import as finished only when both `failures` and `verify-index` exit with `0` (`documents_not_indexed: 0`,
`chunk_count_mismatches: 0`). A document that is `document_too_large` needs `ETSI_MAX_DOC_CHARS` raised; every other failure type is retried
by re-running the import.
