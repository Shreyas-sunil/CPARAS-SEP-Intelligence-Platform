# CHANGELOG_v0.3_PLATFORM_OVERHAUL.md

Full-platform overhaul: GITAS / SEP Intelligence Platform v0.3, introducing
a shared ETSI Standards Knowledge Base used by GITAS, cPARAS, and C-SARAS.
GITAS remains the main platform; cPARAS and C-SARAS remain modules inside
it -- see `ARCHITECTURE_v0.3.md` for the full picture.

All 325 backend tests pass (`cd backend && python -m pytest tests/ -q`);
frontend passes `tsc --noEmit` and `vite build` cleanly.

## 1. New: shared ETSI Standards Knowledge Base (`backend/etsi_corpus/`)

A resumable, checkpointed pipeline that turns `ETSI_Series_With_PDF_URLs.xlsx`
(a 54,323-row manifest of ETSI specification PDF links) into a structured,
embedded knowledge base:

```
manifest.py       -- streaming parse / validation / URL dedup
downloader.py      -- content-addressed PDF cache, bounded concurrency,
                       reuses ingestion/gpp_crawl.py's Windows-safe atomic
                       finalize instead of reimplementing it
pdf_extraction.py   -- thin wrapper over the existing OCR-aware extractor
                       (utils/pdf_parser.py) -- no separate OCR pipeline
llm_extraction.py   -- structured technical extraction via the existing
                       core/llm_engine.py, validated against Pydantic
                       schemas, with a validate -> repair -> retry ->
                       "incomplete" loop (never silently accepts malformed
                       output, never fabricates a value)
indexer.py          -- chunks + embeds into the SAME shared vector store
                       (rag/vector_store.py) cPARAS and C-SARAS already use,
                       under source_type="etsi_standard"
enrichment.py/export.py -- ETSI_Series_Enriched_v0.3.xlsx + matching JSONL
                       (full raw text is stored on disk, never in a
                       spreadsheet cell)
pipeline.py          -- resumable orchestrator: every EtsiDocument's stage
                       is a DB column, so a restarted run skips whatever
                       already succeeded; crash recovery rolls a document
                       back to its last completed checkpoint
package.py            -- versioned, portable knowledge package (see #2 below)
cli.py                -- `python -m etsi_corpus.cli ingest|export|stats|package`
```

New DB tables: `etsi_documents`, `etsi_ingestion_jobs` (`core/database.py`).

New shared services (`backend/services/`), so GITAS/cPARAS/C-SARAS never
each maintain their own retrieval or patent-analysis logic:

- `retrieval_service.py` -- hybrid (semantic + lexical) retrieval across the
  ETSI corpus and the legacy 3GPP-crawler corpus, returning real evidence
  chunks with full source metadata.
- `patent_workflow.py` -- deterministic patent-identity/claim extraction,
  LLM-assisted per-claim technical-element extraction, standard
  classification, and claim-to-standard mapping (never conflates semantic
  similarity with legal essentiality).

## 2. New: versioned, portable Knowledge Package (`etsi_corpus/package.py`)

**The 54k-document ingestion pipeline is a one-time, explicit build step --
it never runs automatically on application startup.** `main.py`'s lifespan
now calls `ensure_dataset_available()`, which only *inspects and logs*
whether a pre-built package exists at `ETSI_DATASET_DIR`; it never
downloads a PDF, calls the LLM, or generates an embedding.

```
ETSI_DATASET_DIR/ (default ./data/etsi_v0.3)
    package_manifest.json        -- version, source manifest hash, counts,
                                     LLM/embedding model versions, timestamp
    ETSI_Series_Enriched_v0.3.xlsx
    ETSI_Series_Enriched_v0.3.jsonl
    text/                          -- extracted per-document text (evidence)
    vector_index/                  -- copy of the persisted vector store
                                       (ChromaDB backend)
```

`python -m etsi_corpus.cli package build` packages whatever has been
ingested so far; `package install --from-dir <path>` copies a package built
on another machine and rewires the local vector store to it -- "Machine A
builds once, Machine B installs and uses immediately" without
re-downloading or re-processing anything. New API endpoints:
`GET /api/etsi-corpus/package/status`, `POST /api/etsi-corpus/package/build`,
`POST /api/etsi-corpus/package/install`.

## 3. New: C-SARAS patent claim-to-standard workflow

`backend/api/routes/csaras/patent_analysis.py` -- upload a patent PDF, or
paste an abstract + numbered claims, and get back: deterministic
patent-identity extraction, deterministic claim splitting (independent vs.
dependent), LLM-assisted technical-element extraction per claim, ranked
ETSI standard candidates, and a per-claim evidence-backed
claim-to-standard mapping (`explicit`/`strong`/`partial`/`weak`/`no_match`,
each carrying evidence text and source pages, never a bare score).

New DB tables: `patent_analyses`, `patent_claims`, `claim_standard_matches`.

New frontend page: `/csaras/patents` (`PatentAnalysisPage.tsx`).

## 4. New: GITAS Standards Corpus page (`/standards-corpus`)

Replaces "Download 3GPP Corpus" as the primary way to build the platform's
standards knowledge base -- manifest-driven rather than crawling a series
archive tree. Shows Knowledge Package status (installed/not, version,
document counts) distinctly from live ingestion-job progress; supports
manifest upload, bounded/full ingestion, retry-failed, and enriched
dataset export (xlsx/jsonl).

The legacy 3GPP-archive crawler (`ingestion/gpp_crawl.py`,
`api/routes/gitas/scrape.py`, `/corpus`) is kept mounted for backward
compatibility and as a secondary source (its output shares the same
vector store under `source_type="3gpp_spec_text"`) -- neither cPARAS nor
C-SARAS require it to be run first anymore.

## 5. Changed: cPARAS standards matching no longer requires the 3GPP crawler

`api/routes/cparas/text_match.py`'s default retrieval now hybrid-searches
both the ETSI Standards Corpus and the legacy 3GPP corpus via
`services/retrieval_service.py`, whichever is indexed, instead of being
hard-restricted to `source_type="3gpp_spec_text"`. Explicit
`source_type` requests are unaffected.

## 6. Rebranding

Product title updated throughout: `GITAS / SEP Intelligence Platform v0.3`
(`frontend/index.html`, GITAS nav brand). Nav reorganized to read as
GITAS (shell) + C-SARAS + Patent Analysis + Standards Corpus + cPARAS,
all clearly modules of one platform.

## Known limitations (see the delivery report for full detail)

- No real ETSI PDFs have been processed in this development environment --
  the sandbox this was built in cannot reach `etsi.org`. The pipeline is
  verified end-to-end against a synthetic PDF, a real temporary SQLite
  database, and stubbed download/LLM/embedding calls
  (`tests/test_etsi_pipeline_smoke.py`), plus a full package
  build/install/portability round-trip (`tests/test_etsi_package.py`).
  Running the real ingestion command against the real manifest is the
  next step, on a machine with internet access.
- Package portability is verified for the ChromaDB vector backend (the
  default); Qdrant/FAISS require a re-index step on the receiving machine
  instead of a file copy (documented in `package.py`).
- The C-SARAS patent workflow currently runs synchronously per request
  (fine for a handful of claims); a genuinely large claim set would want
  the same BackgroundTasks + polling pattern `documents.py` already uses
  for the general document pipeline.
