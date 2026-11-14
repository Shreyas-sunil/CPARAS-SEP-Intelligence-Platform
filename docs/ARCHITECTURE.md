# CPARAS Platform — architecture (v0.5.0)

One offline application with two products on one FastAPI backend, one React frontend, one
SQLite database, one local LLM engine, one embedding model and one shared standards-retrieval
layer.

| Product | For | UI | API |
|---|---|---|---|
| **CPARAS Bulk** | ETSI bulk IPR-declaration exports (tens of thousands of rows): essentiality scoring per row, dashboards, registry, enriched Excel | `/bulk/*` | `/api/sep/*` |
| **CPARAS ISLD** | Individual declarations (PDF/CSV/Excel/HTML): extraction, LLM standards analysis, research evidence, reports, Text Matching | `/isld/*` | `/api/analysis/*`, `/api/text-match/*` |

Historical folder names are kept so existing databases, imports and scripts keep working:
`gitas` = CPARAS Bulk, `cparas` = CPARAS ISLD (both in `backend/api/routes/` and `frontend/src/`).

Earlier design notes: `docs/history/` (e.g. `ARCHITECTURE_v0.3.md`). Sign-in and roles:
`docs/AUTH.md`. Data sources: `DATASET_INTEGRATION.md`, `RETRIEVAL_INDEX_INTEGRATION.md`.

## Request path

```
browser ──► Vite dev server :5173 / nginx (Docker) ──/api──► FastAPI :8000 (backend/main.py)
                                                             │
                     app-wide dependency: auth.dependencies.enforce_access_policy
                       1. route template ──► auth.policy.required_permission  (deny by default)
                       2. session cookie / Bearer token ──► auth.service.resolve_session
                       3. role ──► auth.roles.permissions_for ──► allow · 401 · 403
                                                             │
                                                     route handler (api/routes/...)
```

The frontend is served from the same origin as `/api` (Vite proxy in development, nginx in
Docker), so the HttpOnly session cookie travels with every request, including plain download
links. The frontend's route guards and navigation filtering mirror the server's decision by
reading the permission list from `GET /api/auth/me`; they never decide access themselves.

## Backend modules

| Package | Responsibility |
|---|---|
| `main.py` | App factory: lifespan (DB init + migrations, auth housekeeping, vector store, Bulk run recovery, optional warm-up), CORS, the global access-policy dependency, router mounting. |
| `auth/` | **v0.5.0.** Roles & permissions (`roles.py`), scrypt passwords (`passwords.py`), accounts / sessions / lock-out / first-run token (`service.py`), route policy (`policy.py`), request guard (`dependencies.py`), CLI (`cli.py`). |
| `api/routes/shared/` | `auth.py` + `users.py` (sign-in, user admin), `health.py`, `models.py` (local LLM status / test / pull), `search.py`, `retrieval_index.py`. |
| `api/routes/gitas/` | CPARAS Bulk (`dashboard.py`: upload, background runs, dashboards, registry, exports) and reference data (`etsi_corpus.py`, `scrape.py`, `scrape_maintenance.py`, `datasets.py`). |
| `api/routes/cparas/` | CPARAS ISLD: `analysis.py` (upload → extraction → LLM → research → persistence, jobs, history), `text_match.py` (jobs with pause/stop), `excel_export.py`. |
| `core/` | `config.py` (all settings), `database.py` (ORM models + SQLite migrations), `llm_engine.py` (Ollama / llama.cpp / vLLM behind one interface), `ollama_client.py` (health contract), `ollama_runtime.py` (GPU-fitted context), `gpu_detect.py`, `prompts.py`, `gpp_dataset.py` (validated dataset contract), `version.py`. |
| `etsi_bulk/` | Bulk input parsing (`parser.py`, `schema.py`), batched scoring (`scoring.py`), enriched Excel export that preserves every source row (`excel_export.py`). |
| `research/` | Research engine for ISLD (`engine.py`), shared scoring signals and essentiality bands (`three_signal_scoring.py`), exact phrase matching (`phrase_match.py`), retrieval bridge (`etsi_kb_bridge.py`), Bulk corpus selection (`bulk_corpus.py`) and fast shortlist scoring (`bulk_fast.py`), embeddings, figures, the built-in reference clauses (`standards_corpus.py`, scoring fallback). |
| `services/` | Shared retrieval: `retrieval_service.py` (one entry point), `sqlite_retrieval.py` (3GPP Retrieval Index adapter), `tgpp_retrieval.py` (read-only index client). |
| `rag/` | Embedding model wrapper with memory-bounded batching, and the Standards Corpus vector store (ChromaDB default; Qdrant, FAISS). |
| `etsi_corpus/` | Standards Corpus import pipeline (resumable, checkpointed), indexing, packages, verification, CLI. |
| `ingestion/` | 3GPP archive crawler, local archive indexing, legacy public-source ingestion, start-up removal of pre-v0.5.0 demo chunks (`legacy_cleanup.py`). |
| `utils/` | PDF (PyMuPDF / pdfplumber / OCR tiers), CSV / XLSX / HTML declaration parsers, chunking, JSON repair, export safety. |

## Data-processing pipelines

**CPARAS Bulk** (`POST /api/sep/upload`)
1. `etsi_bulk/parser.py` reads the workbook (header row found even under a title block), one
   record per non-empty source row (`source_row_index`), original bytes stored with the run.
2. A background task (`dashboard.py`) scores rows in batches via `etsi_bulk/scoring.py`: the
   clause corpus is chosen by `research/bulk_corpus.py` (3GPP Retrieval Index → dataset →
   built-in reference clauses as fallback); `research/bulk_fast.py` shortlists clauses (TF-IDF + clause titles)
   and embeds only those (vectors cached on disk); best clause with a declared-family tie-break;
   claim match, semantic similarity, standard coverage → overall confidence → Essential /
   Potential / Non-Essential (`three_signal_scoring.py`).
3. Runs can be paused / stopped / continued; a restart resumes from the last saved row.
4. `etsi_bulk/excel_export.py` writes the original sheet plus the `CPARAS_Bulk_*` columns
   (including `CPARAS_Bulk_Essentiality_Confidence` as a percentage) and a summary sheet —
   strictly from stored results, never re-scoring.

**CPARAS ISLD** (`POST /api/analysis/analyze`)
1. Format detection and extraction (`utils/*_parser.py`): structured tables map columns
   directly; PDFs use the text layer, then OCR tiers for thin pages.
2. LLM extraction per chunk (quick mode, slim schema) and optional deep enrichment per disclosure
   batch (cached on disk per model + prompt), cooperative pause/stop between chunks/batches.
3. `research/engine.py` adds per-patent evidence: retrieval through `etsi_kb_bridge.py`
   (3GPP Retrieval Index / Standards Corpus) with the built-in reference clauses as fallback;
   recommended standard / version / clause, exact matched phrases, confidence and status,
   version evolution, verification, potential SEPs, metrics and figures.
4. Persisted per patent (`patents` table) and as raw JSON; exported as PDF / HTML / JSON / CSV
   (frontend) and Excel (`api/routes/cparas/excel_export.py`).

**Text Matching** (`/api/text-match/jobs/*`): input split into units (one per declared record,
or ~128-token segments of free text) → `services/retrieval_service.retrieve` per unit →
declared-standard re-ranking → pausable job with partial results.

## Standards retrieval and the 3GPP Retrieval Index

`services/retrieval_service.py` is the only retrieval entry point. With `RETRIEVAL_SOURCE=auto`
(default) it uses the **3GPP Retrieval Index** (`dist/retrieval_data`, or
`CPARAS_RETRIEVAL_DATA`) whenever one is installed — FTS5 candidates re-scored by the embedding
model — and the **Standards Corpus** vector store otherwise; `both` merges them. Results are
`EvidenceChunk`s with full provenance; only semantic scores count as evidence.

The index is read-only for CPARAS: it is never rebuilt, modified or copied by the app.
`GET /api/retrieval-index/status` reports it (home page card, 3GPP Retrieval Index page);
`GET /api/text-match/status` reports both sources to ISLD (counts only).

(Up to v0.4.9 parts of the UI called this layer the "ETSI Knowledge Base". Internal identifiers
such as `etsi_kb_bridge`, the stored evidence-source value `etsi_knowledge_base` and the
`knowledge_base_version` column keep their names for compatibility with stored analyses.)

## Configuration

All settings: `backend/core/config.py` (documented per setting), overridden by `backend/.env`
(template `backend/.env.example`) and environment variables. Relative paths are anchored to the
backend folder. Runtime data lives in `backend/data/` (database, caches, uploads, crawls,
first-run token) — never inside the source tree elsewhere.

## Error handling conventions

* User-facing API errors are `HTTPException` with a **string** `detail` naming the file, the cause
  and what to do; the frontend shows it verbatim (`apiErrorMessage`).
* Long jobs (Bulk runs, ISLD analyses, Text Matching, imports, crawls) never fail silently: the
  failure is stored on the job / row and shown; one bad file or row never aborts the rest.
* Optional components (LLM, embedding model, vector store, OCR, retrieval index) degrade
  gracefully and are reported by `GET /api/health` (READY / DEGRADED / UNAVAILABLE).
* Exports validate the generated workbook before returning it.
* Authentication errors: `401` / `403` with the `X-CPARAS-Auth` header (see `docs/AUTH.md`).

## Integration points for a larger platform

| Need | Where |
|---|---|
| Use the platform's identity / SSO | `auth/dependencies.resolve_user` (request → user + role); map roles in `auth/roles.py`; route permissions in `auth/policy.py`. |
| Call CPARAS from another service | Bearer token from `POST /api/auth/login`; OpenAPI at `/api/docs`. |
| Shared standards search | Replace `services/retrieval_service.retrieve(_many)` behind the `EvidenceChunk` contract. |
| A different / central LLM | Add a `BaseLLMBackend` subclass in `core/llm_engine.py` and select it with `LLM_BACKEND`. |
| A different vector database | Add a `BaseVectorStore` subclass in `rag/vector_store.py` and select it with `VECTOR_BACKEND`. |
| Another database server | `DATABASE_URL` (SQLAlchemy async URL); SQLite-specific migrations are skipped for other dialects. |
| Embed the UI in a host shell | Product routes are self-contained under `/bulk` and `/isld`; the shell listens for the `cparas:auth-expired` window event; navigation and permissions come from `frontend/src/platform/products.ts`. |

## Tests

`backend/tests/` (pytest; isolated temporary data folder via `conftest.py`), including
`test_v050_auth.py` (RBAC through the real app, route classification, CLI). Frontend: `npm run
build` (type-check + build). Live installation checks: `scripts/check_roles.py`,
`scripts/e2e_roles_ui.py`. Details: `docs/TESTING.md`.
