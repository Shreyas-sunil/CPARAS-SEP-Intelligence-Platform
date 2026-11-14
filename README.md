# CPARAS — C-DOT SEP Intelligence Platform

One application with **two products** that share one FastAPI backend, one local LLM engine,
one embedding model and one shared standards retrieval layer (the 3GPP Retrieval Index, plus the
Standards Corpus vector store):

| Product | What it is for | UI | API |
|---|---|---|---|
| **CPARAS Bulk** | Bulk / portfolio workflow: upload an ETSI bulk IPR-declaration export (.xlsx/.xls/.csv, tens of thousands of rows), every row is scored for essentiality evidence in a background job; dashboards, patent registry, company profiles, enriched Excel export | `/bulk`, `/bulk/registry`, `/bulk/runs`, `/bulk/patent/:id`, `/bulk/company/:name` | `/api/sep/*` |
| **CPARAS ISLD** | Single-declaration deep analysis with a local LLM, plus clause-level Text Matching | `/isld`, `/isld/text-matching` | `/api/analysis/*`, `/api/text-match/*` |

Shared: 3GPP Retrieval Index (`/retrieval-index`), Standards Corpus (`/corpus`),
health (`/api/health`), models (`/api/models/*`), sign-in and user management (`/login`,
`/admin/users`, `/api/auth/*`).

### Sign-in and roles (v0.5.0)

Every page and API call requires a signed-in user; the backend checks the role on every request.

| Role | Can use |
|---|---|
| **Guest** | CPARAS ISLD only (Declaration Analysis, history, Text Matching). Lands directly on ISLD. |
| **Data Entry** | CPARAS ISLD **and** CPARAS Bulk with all their workflows; reference-data pages read-only. |
| **Super Admin** | Everything, plus **User Management** and reference-data administration (imports, crawls). |

There are no default accounts. After setup, create the first Super Admin once with
**`create_admin.cmd`** (Windows) / `./create_admin.sh`, or in the browser via **First-time setup**
(one-time token from a file on the server). For trying all three roles:
**`seed_test_users.cmd`** / `./seed_test_users.sh` creates `test_guest`, `test_data_entry` and
`test_super_admin` with random passwords printed once. Full guide: **`docs/AUTH.md`**.

The former C-SARAS module is **not** part of this platform; it is the separate
**Initial Categoriser** application. Internal code paths keep historical names
(`frontend/src/gitas/` and `backend/api/routes/gitas/` = CPARAS Bulk, `.../cparas/` = CPARAS ISLD)
so existing databases keep working; everything user-facing says CPARAS Bulk / CPARAS ISLD.
Old URLs (`/dashboard`, `/analyze`, `/patent/:id`, `/entity/company/:name`) redirect.

### CPARAS Bulk — source-row preservation

Every non-empty row of an uploaded sheet becomes exactly one stored record, in source order,
identified by `(run_id, source_row_index)`. Nothing is merged, skipped or overwritten because
rows share an application number, publication number, DIPG id, title or company — identical
rows are all kept. The enriched Excel export (`GET /api/sep/runs/{id}/export/excel`) returns the
original sheet (same rows, order, columns and values) with the `CPARAS_Bulk_*` columns appended
and a summary sheet. Runs are background jobs with Pause / Stop / Continue; a run interrupted by
a backend restart is shown as paused and resumes from the last saved row. CPARAS Bulk scoring
does not need the LLM.

### CPARAS ISLD

Upload one or more declarations - PDF (text or scanned), CSV, Excel or HTML - and CPARAS ISLD:

* extracts every declared disclosure row (application / publication number, declared 3GPP
  standard, country, title, version, clause). CSV / Excel / HTML annex tables are mapped
  directly from their columns; PDFs are read with PyMuPDF / pdfplumber, OCR (Tesseract) for
  scanned pages, and a local LLM turns the text into rows;
* maps each disclosure to 3GPP releases, technical domains and SEP strength (Deep mode, local LLM);
* adds research-style evidence: recommended standard / version / clause, similarity to the
  standards corpus, verification status and potential-SEP flags;
* exports PDF, HTML, JSON and CSV reports, and an enriched Excel workbook;
* **Text Matching**: matches claim text, or an uploaded document, clause-by-clause against the
  standards text (the installed 3GPP Retrieval Index or the Standards Corpus).

Everything runs on this machine: local LLM (Ollama by default; llama.cpp and vLLM also supported),
local embeddings, SQLite. No cloud APIs.

Every extracted disclosure row is kept, in order. Rows that repeat an application or
publication number or a title are never merged or dropped.

## Requirements

| | |
|---|---|
| Python | 3.10 - 3.12 (3.12 recommended). On Windows install from python.org and tick "Add python.exe to PATH". |
| Node.js | 18 or newer (LTS) - for the frontend |
| Ollama | <https://ollama.com/download> + at least one text model (see below) |
| Tesseract OCR | optional; only for scanned (image-only) PDFs. Windows: <https://github.com/UB-Mannheim/tesseract/wiki> |
| Disk / RAM | ~6 GB for packages + embedding model, plus the LLM model (2-5 GB); 8 GB RAM minimum, 16 GB recommended |

## Quick start - Windows

```powershell
# 1. first time (creates backend\.venv, installs packages, backend\.env, npm ci,
#    checks Ollama + model + Tesseract, downloads the embedding model):
powershell -ExecutionPolicy Bypass -File .\setup.ps1 -PullModel     # or double-click setup.cmd

# 2. once: create the first Super Admin (prompts for username + password)
create_admin.cmd
#    optional: one test account per role, passwords printed once
seed_test_users.cmd

# 3. every time:
powershell -ExecutionPolicy Bypass -File .\start.ps1                # or double-click start.cmd
```

`start.ps1` checks Python and Node.js, **reuses Ollama if it is already running** (it never
starts a second instance), starts Ollama only if nothing answers on the port, lists the
installed models, validates `OLLAMA_MODEL`, starts the backend and waits until `/api/health`
answers, starts the frontend and waits for it, then prints:

```
  CPARAS Platform is running
    App        : http://127.0.0.1:5173
    API docs   : http://127.0.0.1:8000/api/docs
    Status     : READY
    LLM server : connected (http://127.0.0.1:11434)
    Models     : 3 text, 1 vision
    Default    : qwen2.5:7b (installed)
```

Ctrl+C stops the backend and frontend (Ollama keeps running). Logs are in `logs\`.
Options: `start.ps1 -BackendOnly`, `setup.ps1 -SkipEmbeddingDownload`.

## Quick start - Linux / macOS

```bash
./setup.sh --pull-model      # first time   (--skip-embedding-download to skip the ~2 GB model)
./create_admin.sh            # once: the first Super Admin   (./seed_test_users.sh: test accounts)
./start.sh                   # every time   (--backend-only for the API only)
```

## Manual start (any OS)

```bash
cd backend
python -m venv .venv
.venv\Scripts\activate            # Windows   |   source .venv/bin/activate   (Linux/macOS)
pip install -r requirements.txt
copy .env.example .env            # Windows   |   cp .env.example .env
python -m uvicorn main:app --host 127.0.0.1 --port 8000

cd ../frontend
npm ci
npm run dev                       # http://127.0.0.1:5173  (proxies /api to 127.0.0.1:8000)
```

`API_TARGET=http://host:port npm run dev` points the frontend at a backend elsewhere.
Production build: `npm run build` (output in `frontend/dist`, served by nginx in Docker).

## Ollama (local LLM)

### Install Ollama and models

1. Install Ollama from <https://ollama.com/download> (Windows installer, macOS app, or on Linux
   `curl -fsSL https://ollama.com/install.sh | sh`). On Windows and macOS Ollama starts
   automatically in the background (tray / menu-bar icon) after installation.
2. Install a **text** model. Tested models:

   | Model | Size | Notes |
   |---|---|---|
   | `qwen2.5:7b` | ~4.7 GB | default (`OLLAMA_MODEL`), best accuracy |
   | `qwen2.5:3b` | ~1.9 GB | faster, for machines with little RAM/VRAM |
   | `phi3.5:3.8b-mini-instruct-q4_K_M` | ~2.4 GB | alternative small model |
   | `moondream` | ~1.7 GB | **vision** model: OCR fallback for scanned PDFs only, never used for text analysis |

   ```
   ollama pull qwen2.5:7b
   ```

### Check that Ollama is running

Open <http://127.0.0.1:11434/api/tags> in a browser (or run `ollama list`). A JSON list of
your models means Ollama is running and reachable. That is exactly the check the startup
scripts and the backend perform.

The app shows the result as separate facts, never as one "offline" light:

```
Ollama server      Connected
Installed models   3 text · 1 vision (OCR only)
Selected model     qwen2.5:7b
Model available    Yes
Generation         Ready            (only after a real generation succeeded - use "Test")
```

`GET /api/health` and `GET /api/models/status` return the same information as JSON
(`reachable`, `models`, `text_models`, `vision_models`, `configured_model`,
`configured_model_available`, `generation`, `error`, `hint`).

### Model selection

* Only installed **text** models are offered. Vision models (`moondream`, `llava`, ...) are
  never used for text analysis; `VISION_MODEL=moondream` is only an OCR fallback for scanned PDFs when Tesseract is not installed.
* The default comes from `OLLAMA_MODEL` in `backend/.env`; the model you pick in the UI is sent
  with each request and validated against Ollama first.
* A missing model is **reported, never silently replaced** by another one. Install it
  (`ollama pull <name>`) or pick an installed model.
* `POST /api/models/test` (the **Test** button) runs a tiny 8-token generation to prove the
  selected model really works. Health polling never runs a generation.

### "Ollama says port 11434 is already in use"

```
C:\> ollama serve
Error: listen tcp 127.0.0.1:11434: bind: Only one usage of each socket address ... is normally permitted.
```

This almost always means **Ollama is already running** (the tray app starts it at login).
It is not an error and you do not need a second `ollama serve`:

1. Open <http://127.0.0.1:11434/api/tags>. If you see your models, Ollama is fine: just start
   the app (`start.cmd` / `start.ps1` / `./start.sh`). The start scripts detect a running
   Ollama and never launch another one.
2. If that URL does not answer, the port is held by a stuck Ollama or by another program.
   The start script names the process that owns the port. Quit Ollama from the tray (or end
   `ollama.exe` in Task Manager) and start it again.
3. To run Ollama on another port: set `OLLAMA_HOST=127.0.0.1:11500` before starting it and
   `OLLAMA_BASE_URL=http://127.0.0.1:11500` in `backend/.env`.

### Other common Ollama problems

| Symptom in the app | Cause and fix |
|---|---|
| *Ollama server: Not reachable*, "Nothing is accepting connections" | Ollama is not running: open the Ollama app or run `ollama serve`. Check `OLLAMA_BASE_URL`. |
| Not reachable, but `ollama list` works | Wrong `OLLAMA_BASE_URL`, or a firewall/proxy. The backend bypasses HTTP proxies for 127.0.0.1; use `http://127.0.0.1:11434` rather than `localhost`. |
| *Port answered, but not Ollama* | Another program uses the port. See the section above. |
| *No text model installed* | `ollama pull qwen2.5:7b` (vision-only models don't count). |
| *Default model missing* | `OLLAMA_MODEL` is not installed: pull it or change it in `backend/.env`. |
| *Generation: Failed* - "out of memory" / HTTP 500 | The model does not fit in RAM/VRAM. Keep `OLLAMA_FIT_CONTEXT_TO_GPU=true`, set `OLLAMA_FLASH_ATTENTION=true` + `OLLAMA_KV_CACHE_TYPE=q8_0` and restart Ollama, or use `qwen2.5:3b`; keep `OLLAMA_CONCURRENCY=1`. |
| *GPU: 100% CPU* or a GPU/CPU split | See **Using the GPU** below. |
| First request very slow / Test takes a minute | The model is being loaded into memory the first time; later requests are faster. |

### Resource settings

* `OLLAMA_CONCURRENCY=1` (default): requests to Ollama are sent one at a time, enforced for
  the whole backend. Raise it only with spare VRAM **and** the same `OLLAMA_NUM_PARALLEL` set
  for the Ollama server; otherwise Ollama queues the requests anyway.
* `OLLAMA_NUM_CTX`: the MAXIMUM context window per request (24576 here). Each concurrent
  request needs its own KV cache of this size; since v0.4.2 the backend lowers it per model so
  the model fits in VRAM (see below).

### Using the GPU (v0.4.2)

Ollama uses an NVIDIA GPU automatically when the NVIDIA driver is installed — but only for as
many layers as fit in VRAM together with the context's KV cache. The backend therefore sizes the
context per model (`OLLAMA_FIT_CONTEXT_TO_GPU=true`): it reads the model's architecture from
Ollama, the card's VRAM from `nvidia-smi`, and picks the largest context (up to
`OLLAMA_NUM_CTX`) for which everything fits; long declarations are split into proportionally
smaller chunks. The **GPU** row of the Local model panel shows Ollama's own report after a
**Test** or an analysis: `100% GPU` is what you want.

* Run `setup.cmd` once on a machine with an NVIDIA GPU: it installs the CUDA build of PyTorch
  (embeddings on the GPU) and stores `OLLAMA_FLASH_ATTENTION` / `OLLAMA_KV_CACHE_TYPE` for the
  Ollama app. Then **quit Ollama** (tray icon → Quit) and start with `start.cmd`, which starts
  Ollama with these settings. Flash attention + `q8_0` halve the context's memory — this is
  what makes phi3.5 (no grouped-query attention, ~384 KiB per token) fit on a 4–6 GB card.
* External GPU: connect it before starting Ollama; `start.cmd` lists the NVIDIA GPUs it sees.
* Check from a terminal: `ollama ps` shows `100% GPU` / `x%/y% CPU/GPU` for the loaded model.
* Results are reproducible: temperature 0, fixed `OLLAMA_SEED`, and the same context (hence the
  same GPU/CPU split) on every run.

| Variable | Default | Purpose |
|---|---|---|
| `OLLAMA_FIT_CONTEXT_TO_GPU` | `true` | Size the context per model to fit VRAM (`false` = always `OLLAMA_NUM_CTX`) |
| `OLLAMA_MIN_NUM_CTX` | `8192` | Never go below this context |
| `GPU_VRAM_RESERVE_GB` | `0.8` | VRAM left for Windows / the browser |
| `OLLAMA_FLASH_ATTENTION` / `OLLAMA_KV_CACHE_TYPE` | `true` / `q8_0` (.env.example) | Ollama server settings; restart Ollama after changing |
| `OLLAMA_SEED` | `42` | Fixed sampling seed |
| `OLLAMA_KEEP_ALIVE` | `30m` | Keep the model loaded between requests |
| `OLLAMA_NUM_GPU` | `-1` | Force GPU layers (`-1` = Ollama decides, `999` = all, `0` = CPU) |
| `EMBEDDING_DEVICE` | `auto` | `auto` (CUDA if available) / `cuda` / `cpu` / `mps` |

## Local 3GPP archive (Standards Corpus)

To give RAG enhancement and deep 3GPP enrichment real specification text:

1. Put all extracted 3GPP files in **one folder** — sub-folders are fine. Accepted:
   `3gpp_<spec>_v<x.y.z>.json` (e.g. `3gpp_21.111_v13.0.0.json`), extracted Word files
   (`21111-d00.docx`) and the original zips (`21111-d00.zip`).
2. Either use the default folder **`backend\data\3gpp_archive`** (created on first start) and
   double-click **`index_3gpp.cmd`**, or keep the files where they are and run
   `powershell -ExecutionPolicy Bypass -File .\index_3gpp.ps1 -Path "D:\3gpp_extracted"`.
3. The **Standards text** row in ISLD Analysis setup then shows the number of indexed Standards Corpus chunks
   (next to the 3GPP Retrieval Index document count).

Only the latest version of each spec is indexed (add `-AllVersions` for all). Re-running skips
files already indexed. With the app running, indexing happens inside the backend; otherwise it
runs directly (keep the app closed until it finishes). Embedding is much faster on the GPU.

## 3GPP dataset (optional)

Keep the validated dataset build folder (e.g. `cparas_dataset\build_v4`, containing `etsi_dataset.jsonl`) in the same parent
folder as the CPARAS app folders. `setup.ps1` finds it, saves `CPARAS_DATASET_ROOT` in `backend\.env`, checks it, and imports on request
(`-ImportDataset`, `-ImportAll`, `-Versions`, `-DatasetPath`, `-SkipDataset`, `-ValidateDataset`). Details: **`DATASET_INTEGRATION.md`**.

## Configuration (`backend/.env`)

All settings, with defaults and explanations, are in `backend/core/config.py`; `.env.example`
lists the ones you are likely to change. `backend/.env` is always read from the backend folder
(whatever the current directory), relative paths such as `./data` are inside `backend/`, and
unknown keys are ignored.

| Variable | Default | Purpose |
|---|---|---|
| `HOST` / `PORT` | `127.0.0.1` / `8000` | Backend address. Keep 127.0.0.1: the API has no authentication. |
| `LLM_BACKEND` | `ollama` | `ollama`, `llamacpp` (`LLAMACPP_MODEL_PATH`) or `vllm` (`VLLM_BASE_URL`, `VLLM_MODEL`) |
| `OLLAMA_BASE_URL` | `http://127.0.0.1:11434` | Ollama address (falls back to `OLLAMA_HOST` if unset) |
| `OLLAMA_MODEL` | `qwen2.5:7b` | Default text model (must be installed) |
| `OLLAMA_TIMEOUT` | `1800` | Seconds per generation request |
| `OLLAMA_NUM_CTX` | `24576` | Maximum context window per request (see *Using the GPU*) |
| `OLLAMA_CONCURRENCY` | `1` | Simultaneous Ollama requests |
| `VISION_MODEL` | `moondream` | Vision OCR fallback (only if Tesseract is missing) |
| `TESSERACT_CMD` | *(auto)* | Only if tesseract is not on PATH / in `C:\Program Files\Tesseract-OCR` |
| `TESSERACT_PAGE_TIMEOUT` | `120` | Seconds per page before OCR gives up on it |
| `EMBEDDING_MODEL` | `BAAI/bge-m3` | Local sentence-transformers model |
| `EMBEDDING_MAX_SEQ_LENGTH` | `1024` | Longest sequence (tokens) the importer feeds the model; longer chunks are split into numbered parts (searches are not limited) |
| `EMBEDDING_CPU_BATCH_SIZE` | `8` | Texts per forward pass on CPU (attention memory grows with batch x length²) |
| `EMBEDDING_MEMORY_BUDGET_MB` | `1536` | Planned working memory per forward pass, excluding model weights; `0` = auto |
| `ETSI_INDEX_GROUP_CHUNKS` | `64` | Chunks embedded and written to the index per group |
| `ETSI_MIN_FREE_RAM_MB` | `768` | Importer waits, then fails the document as retryable, below this much free RAM (`0` = off) |
| `ETSI_MAX_DOC_CHARS` | `60000000` | Documents above this are reported as `document_too_large`, never skipped silently (`0` = no ceiling) |
| `THREEGPP_ARCHIVE_DIR` | `./data/3gpp_archive` | Default folder for `index_3gpp.cmd` |
| `EMBEDDING_ALLOW_DOWNLOAD` | `false` | Allow a one-time model download at runtime (setup downloads it) |
| `VECTOR_BACKEND` | `chromadb` | `chromadb` (embedded), `qdrant` (`QDRANT_URL`), `faiss` |
| `DATABASE_URL` | `sqlite+aiosqlite:///./data/sep_intel.db` | SQLite database |
| `MAX_UPLOAD_MB` | `50` | Upload limit |
| `SCORING_BATCH_SIZE` | `256` | CPARAS Bulk rows scored per batch (lower on low-memory machines) |
| `CPARAS_DATASET_ROOT` | *(empty)* | Folder of the validated 3GPP dataset build (setup fills it in) |
| `BULK_CORPUS_SOURCE` | `auto` | CPARAS Bulk corpus: `auto` / `dataset` / `illustrative` |
| `CORS_EXTRA_ORIGINS` | *(empty)* | Extra browser origins allowed to call the API |

## Using it

Open <http://127.0.0.1:5173> and sign in. Guests go straight to CPARAS ISLD; Data Entry users and
Super Admins land on the platform home. Account menu (top right): change password, sign out.
Super Admins manage accounts under **Administration → User Management**.

**CPARAS Bulk** (Data Entry, Super Admin): open <http://127.0.0.1:5173/bulk/runs>, drop an ETSI bulk export (try
`sample_data/sample_bulk_export.xlsx`, synthetic, 60 rows incl. repeated identifiers), follow the run
(processed / failed / remaining, Pause / Stop / Continue), then explore the dashboard and registry
and download the enriched Excel (or the failed-rows CSV).

**CPARAS ISLD** (every role):

1. Open <http://127.0.0.1:5173/isld> -> **Declaration Analysis**.
2. Check the **Local model** panel: server connected, a text model selected. Press **Test** once
   to confirm generation works.
3. Add `sample_data/sample_isld_declaration.csv` (or `.pdf`), choose **Quick** or **Deep**, press
   **Analyze**. Progress shows each stage (reading / OCR, LLM analysis per chunk, enrichment,
   verification, research, saving) with a live activity log. **Pause / Stop** end the run after
   the current file or chunk; finished files stay saved.
4. Results: disclosures table (**Details** per row), standards / jurisdiction breakdown,
   research metrics. Warnings explain OCR quality, possibly-missing rows or failed parts.
5. Export **PDF**, **HTML**, **JSON**, **CSV**, or **Excel** (the saved declaration's rows plus
   the `CPARAS_*` enrichment columns, `GET /api/analysis/history/{id}/export/excel`).
6. **Text Matching** needs standards text: the installed **3GPP Retrieval Index**, Standards Corpus
   content (Standards Corpus page). The ISLD setup panel shows which is
   available.

Errors name the file and the cause (e.g. "`x.pdf` is password-protected", "Model 'mistral' is
not installed ... Install it with: ollama pull mistral", "LLM analysis failed for `x.pdf`: out of
memory"). One bad file never stops the others in the same batch.

## API

Interactive docs: <http://127.0.0.1:8000/api/docs>

Every endpoint except `/api/health`, `POST /api/auth/login` and the first-run setup endpoints needs
a session: the browser's cookie, or `Authorization: Bearer <token>` from `POST /api/auth/login`.
Which role may call what: `docs/AUTH.md` (and `backend/auth/policy.py`).

| Method & path | Purpose |
|---|---|
| `POST /api/auth/login`, `/logout`; `GET /api/auth/me`; `POST /api/auth/password` | sign-in, session, own password |
| `/api/auth/users`, `/api/auth/events` | user management, audit trail (Super Admin) |
| `GET /api/health` | READY / DEGRADED / UNAVAILABLE plus database, LLM (full contract), embeddings, OCR, vector store, storage |
| `GET /api/models/status` | LLM health contract |
| `GET /api/models/available` | installed models by kind + configured default |
| `POST /api/models/test` | tiny generation smoke test `{ "model": "qwen2.5:7b" }` |
| `GET /api/models/recommended` | model suggestion for this machine's GPU/VRAM |
| `POST /api/models/pull` | background `ollama pull` (`GET /api/models/pull/status/{model}`) |
| `POST /api/analysis/analyze` | multipart `files`, `mode` (quick/deep), `model`, `job_id` |
| `GET /api/analysis/progress/{job_id}` | live progress |
| `POST /api/analysis/jobs/{job_id}/pause` / `stop` | cooperative stop |
| `GET /api/analysis/history`, `/history/{id}`, `/history/{id}/export/excel` | saved analyses |
| `POST /api/text-match/match`, `/match-file`; `GET /api/text-match/status` | Text Matching |
| `GET /api/retrieval-index/status`, `/search`, `/specs`, ... | 3GPP Retrieval Index (read-only) |
| `/api/etsi-corpus/*`, `/api/scrape/*` | Standards Corpus |
| `POST /api/sep/upload` | CPARAS Bulk: upload an ETSI bulk export (background run) |
| `GET /api/sep/runs`, `/runs/{id}/status`; `POST /runs/{id}/pause` / `stop` / `continue` | Bulk run control |
| `GET /api/sep/runs/{id}/export/excel`, `/runs/{id}/export/failed-rows` | Bulk exports |
| `GET /api/sep/dashboard/*`, `/patents`, `/patents/{id}`, `/entity/company/{name}`, `/filters` | Bulk dashboards and registry |

## Docker

```bash
cp backend/.env.example backend/.env
docker compose up --build            # http://localhost:3000 (Ollama runs on the host)
```

GPU / in-container llama.cpp: `docker compose -f docker-compose.gpu.yml up --build`.
Qdrant: `docker compose -f docker-compose.yml -f docker-compose.qdrant.yml up --build`.
Ports are bound to 127.0.0.1; set `BIND_ADDR=0.0.0.0` only on a trusted network (and serve
HTTPS with `AUTH_COOKIE_SECURE=true` when CPARAS is reachable from other machines).
First Super Admin in Docker: `docker compose exec backend python -m auth.cli create-superadmin`
(or use **First-time setup** with the token file path printed in `docker compose logs backend`).

## Ports (all three C-DOT applications can run side by side)

| Application | Backend | Frontend (dev) | Docker |
|---|---|---|---|
| **CPARAS Platform** | **8000** | **5173** | **3000** |
| CPARAS ISLD | 8001 | 5174 | 3001 |
| Initial Categoriser | 8100 | 5175 | 3002 |

## Troubleshooting

* **Scripts won't run** ("running scripts is disabled"): use `start.cmd` / `setup.cmd`, or
  `powershell -ExecutionPolicy Bypass -File .\start.ps1`. Nothing changes the system policy.
* **`py`/`python` not found or opens the Microsoft Store**: install Python from python.org with
  "Add to PATH"; the scripts prefer the `py` launcher.
* **Port 8000 / 5173 already in use**: the start script names the process. Usually an earlier
  copy of the app is still running.
* **Header shows "Embedding model missing"**: run `setup.ps1` (downloads it) or set
  `EMBEDDING_ALLOW_DOWNLOAD=true` once while online. Analysis still works (TF-IDF fallback);
  semantic search / Text Matching need the model.
* **Scanned PDF gives little text**: install Tesseract (auto-detected in
  `C:\Program Files\Tesseract-OCR`), or set `TESSERACT_CMD`. `GET /api/health` -> `ocr` shows
  what the backend found. CJK names need the Tesseract language packs.
* **"is password-protected"**: remove the password (print to PDF) and upload again.
* **Analysis stops with "out of memory"**: use a smaller model (`qwen2.5:3b`), lower
  `OLLAMA_NUM_CTX`, keep `OLLAMA_CONCURRENCY=1`.
* **Everything else**: `logs\backend.err.log` (script start) or the uvicorn console; each line
  names the file, model and stage.

## Tests

```bash
cd backend
python -m pytest tests -q          # uses a temporary data folder; no Ollama needed (incl. test_v050_auth.py)
cd ../frontend
npm run build                      # type-check + production build
node --experimental-strip-types --test src/cparas/lib/__tests__/etaEstimator.test.ts
```

Role checks against a running installation (after `seed_test_users`):
`python scripts/check_roles.py --guest test_guest:<pw> --data-entry test_data_entry:<pw> --super-admin test_super_admin:<pw>`
(standard library only) and the optional browser check `scripts/e2e_roles_ui.py`. See `docs/TESTING.md`.

## Project layout

| Area | Where |
|---|---|
| Frontend (React + Vite + TypeScript) | `frontend/src/gitas/` (CPARAS Bulk + Standards Corpus page), `frontend/src/cparas/` (CPARAS ISLD), `frontend/src/platform/` (shell, home, help, 3GPP Retrieval Index page), `frontend/src/platform/auth/` + `platform/admin/` (sign-in, route guards, user management), `frontend/src/shared/` (LLM status) |
| Backend entry point | `backend/main.py` (FastAPI) |
| Authentication & roles | `backend/auth/` (roles, passwords, sessions, route policy, CLI), `backend/api/routes/shared/auth.py`, `users.py`; scripts `create_admin.*`, `seed_test_users.*`, `cparas_users.*` |
| CPARAS Bulk API / bulk parsing, scoring, Excel export | `backend/api/routes/gitas/`, `backend/etsi_bulk/` |
| CPARAS ISLD analysis / Excel export / Text Matching | `backend/api/routes/cparas/` |
| LLM engine, Ollama health contract, prompts | `backend/core/llm_engine.py`, `backend/core/ollama_client.py`, `backend/core/prompts.py` |
| Models / health routes | `backend/api/routes/shared/` |
| PDF / OCR / CSV / Excel / HTML parsing | `backend/utils/` |
| Research engine, embeddings, vector store | `backend/research/`, `backend/rag/`, `backend/services/` |
| Standards Corpus | `backend/api/routes/gitas/` (etsi_corpus, scrape), `backend/etsi_corpus/`, `backend/ingestion/` |
| Database (SQLite, created on first start) | `backend/core/database.py` |
| Start / setup scripts | `start.ps1`, `setup.ps1`, `*.cmd` (Windows); `start.sh`, `setup.sh` (Linux/macOS); `scripts/` (incl. `check_roles.py`, `e2e_roles_ui.py`) |
| Architecture, sign-in, testing; earlier reports | `docs/ARCHITECTURE.md`, `docs/AUTH.md`, `docs/TESTING.md`; `docs/history/` |

See `CHANGELOG.md` for what changed in this release, and `docs/ARCHITECTURE.md` for the module map, retrieval design and integration points.

## Using the tgpp-sqlite-retrieval index as the dataset
Copy the files of `tgpp-sqlite-retrieval\dist\retrieval_data\` into **`dist\retrieval_data\`** in this project (next to `backend\`) - no `.env` change needed; the **3GPP Retrieval Index** page in the app confirms it. Details: [RETRIEVAL_INDEX_INTEGRATION.md](RETRIEVAL_INDEX_INTEGRATION.md).

---

## Documentation

| Document | What it covers |
|----------|----------------|
| [`docs/CPARAS_Doc.docx`](docs/CPARAS_Doc.docx) | User and admin guide for CPARAS v0.5.0: install, roles, ISLD, Bulk, scoring, 3GPP retrieval index |
| [`docs/CPARAS_Overview.pptx`](docs/CPARAS_Overview.pptx) | Slide overview of the platform |
| [`docs/CPARAS_Intelligence_Platform_Paper_v3.pdf`](docs/CPARAS_Intelligence_Platform_Paper_v3.pdf) | Paper: *CPARAS: An Offline, LLM-Assisted Platform for Reading ETSI IPR Declarations and Checking Them Against 3GPP Specifications* |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md), [`docs/AUTH.md`](docs/AUTH.md), [`docs/TESTING.md`](docs/TESTING.md), [`docs/WINDOWS_IMPORT_RUNBOOK.md`](docs/WINDOWS_IMPORT_RUNBOOK.md) | Technical notes |
| [`DATASET_INTEGRATION.md`](DATASET_INTEGRATION.md), [`RETRIEVAL_INDEX_INTEGRATION.md`](RETRIEVAL_INDEX_INTEGRATION.md), [`CHANGELOG.md`](CHANGELOG.md) | Integration notes and release history |

### Embeddings

[`embeddings/`](embeddings/) holds `embed_cparas.py`, the tool that turns the CPARAS 3GPP dataset into vector embeddings. Setup and usage: [`embeddings/README.md`](embeddings/README.md). Full documentation: [`embeddings/DOCUMENTATION.html`](embeddings/DOCUMENTATION.html). Paper: [*Benchmarking Sentence Embedding Models for Patent-Standard Semantic Alignment*](embeddings/docs/Benchmarking_Embedding_Models_Patent_Standard.pdf).
