# Using the tgpp-sqlite-retrieval index as the dataset (v0.4.5)

> **v0.5.0:** the API endpoints below require a signed-in user with the Data Entry or Super Admin role (changing reference data: Super Admin). Open them in the same browser in which you are signed in to CPARAS, or send `Authorization: Bearer <token>` (see `docs/AUTH.md`).

The `retrieval_data` folder produced by **tgpp-sqlite-retrieval** (`search_index.sqlite`, `manifest.json`, `schema_version.json`,
`build_report.json`) can now serve as the standards dataset for this application. It is opened **read-only, in place**; nothing is
copied, imported or written. It replaces the vector-store import (`index_3gpp.ps1 -ImportAll`) for standards evidence.

> **Status:** this integration was tested against a small synthetic fixture index (`backend/tests/fixtures/retrieval_data`) only.
> It has **not** been run against your 32,440-document index. Build the index, open `/api/retrieval-index/status?deep=true`
> and read `build_report.json` before relying on it.

## Setup (PowerShell)

**Default (v0.4.6): copy the folder into this project - nothing to configure.**

```
CPARAS_Platform_Revamped\
  backend\
  frontend\
  dist\
    retrieval_data\            <-- paste the files here
      search_index.sqlite       (~20 GB, required)
      schema_version.json       (required)
      manifest.json             (required; also used to verify the copy is complete)
      build_report.json, README.txt, tgpp_retrieval.py   (optional)
```

```powershell
# from the project folder, with the app stopped
robocopy D:\3gpp\tgpp-sqlite-retrieval\dist\retrieval_data .\dist\retrieval_data /E
.\start.ps1      # the summary prints "3GPP index : ... (32,440 documents)"
```

Leave `CPARAS_RETRIEVAL_DATA` **empty** in `backend\.env` (delete any old line pointing elsewhere). Then open
**Reference data → 3GPP Retrieval Index** in the app: it shows the folder it reads, a file checklist, whether the copy is
complete, the counts, and a search box. `GET /api/retrieval-index/status` returns the same as JSON.

**Using a copy elsewhere instead** (e.g. one copy shared with Initial Categoriser): set
`CPARAS_RETRIEVAL_DATA=D:\path\to\retrieval_data` in `backend\.env` (a relative path is relative to `backend\`).

Docker: `docker-compose.yml` mounts `./dist/retrieval_data` read-only at `/app/retrieval_data` (override with `CPARAS_RETRIEVAL_DATA_HOST`).

## What changes

* `retrieve()` (the single shared retrieval path used by text matching and the ETSI evidence bridge) queries the index when
  `RETRIEVAL_SOURCE=auto` (default) and the index is configured and usable; otherwise behaviour is exactly v0.4.4.1 (vector store).
  `sqlite` forces the index, `vector` forces the old path, `both` merges.
* **How scoring works.** FTS5/BM25 finds up to `RETRIEVAL_SQLITE_POOL` (40) candidates from distinctive query keywords. Those candidates are re-scored
  with the application's own embedder (cosine, normalised) so the existing thresholds (e.g. essentiality `semantic_score >= 0.35`)
  keep their meaning. No corpus embedding is needed, but **the embedder model must still load for semantic scores**.
* **Recall is lexical-first.** A relevant clause that shares no keyword with the query is never a candidate, so it cannot be re-scored.
  This is weaker than a dense vector search for paraphrases; raise `RETRIEVAL_SQLITE_POOL` for more recall.
* **Lexical fallback** (`RETRIEVAL_SQLITE_RERANK=false`, or the embedder is unavailable): hits carry `score_kind="lexical"`, scores are capped at 0.34 and
  ignored by the essentiality bridge. They are never counted as semantic evidence.
* **Provenance.** Only `full_text` rows are original specification text. Every other content type (clause summaries, normative
  requirements, technical summaries, classification, patent relevance...) is **model-generated** by the dataset's LLM and is labelled
  by `content_type` on each evidence item (`dataset_document_id` identifies the document). Unknown specification ids/versions stay unknown.
* New endpoints under `/api/retrieval-index`: `status[?deep=true]`, `search` (BM25 keywords), `evidence` (the hybrid evidence the analyses use), `specs`, `specs/{spec}/clauses`, `documents/{id}`, `bulk-corpus/summary` (query/filters only; no client-supplied paths).

## Settings (backend/.env)

| Setting | Default | Meaning |
|---|---|---|
| `CPARAS_RETRIEVAL_DATA` | empty | path to `retrieval_data`; empty = `<project>/dist/retrieval_data` (feature off if no index there) |
| `RETRIEVAL_SOURCE` | `auto` | `auto` / `sqlite` / `vector` / `both` |
| `RETRIEVAL_SQLITE_POOL` | `40` | FTS5 candidates re-scored per query |
| `RETRIEVAL_SQLITE_RERANK` | `true` | re-score with the app's embedder |

## CPARAS Bulk corpus from the index (Platform)

`BULK_CORPUS_SOURCE=auto` (default) now tries, in order: the retrieval index, the `standards_corpus_3gpp.json` of `CPARAS_DATASET_ROOT`, the
built-in illustrative corpus. `retrieval` requires the index (error if unusable); `dataset` and `illustrative` behave as in v0.4.4.1.

The corpus is **bounded** and built on first use (cached, fingerprinted by index + options; embeddings cached as before):

| Setting | Default | Meaning |
|---|---|---|
| `BULK_RETRIEVAL_VERSIONS` | `latest-per-release` | `latest-per-release` / `latest` / `all` versions per specification |
| `BULK_RETRIEVAL_MAX_DEPTH` | `3` | deepest clause level (`5.2.2` is 3, `5.2.2.1` is 4) |
| `BULK_RETRIEVAL_MAX_CHARS` | `1500` | characters kept per clause |
| `BULK_RETRIEVAL_MIN_CHARS` | `40` | shorter clause texts are dropped |
| `BULK_RETRIEVAL_MAX_ENTRIES` | `60000` | ceiling; clauses beyond it are counted in `/api/retrieval-index/bulk-corpus/summary` as omitted |

Only specifications with a known id **and** version are included; `year` is 0 (never guessed); original `full_text` is preferred over
summaries and each entry records its `text_basis`. The first build over the full index takes time that I have not measured.
