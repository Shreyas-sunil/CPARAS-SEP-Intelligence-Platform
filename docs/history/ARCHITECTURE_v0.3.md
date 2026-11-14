> **Historical document (v0.3).** Kept for reference only; it describes the v0.3 module layout and uses
> the old "Knowledge Base" naming. The current architecture is in `docs/ARCHITECTURE.md`.

> **v0.4 note (current):** the products were renamed and the platform split.
> GITAS is now **CPARAS Bulk**, cPARAS is now **CPARAS ISLD**, and C-SARAS has
> been removed from this repository — it is the standalone **Initial
> Categoriser** project. The diagram below is kept for the shared knowledge-base
> architecture, which is unchanged; read "GITAS" as CPARAS Bulk and "cPARAS" as
> CPARAS ISLD, and ignore C-SARAS. See README.md and CHANGELOG_v0.4.md.

# GITAS / SEP Intelligence Platform v0.3 — Architecture

```
                    GITAS / SEP PLATFORM v0.3
                              |
              +---------------+---------------+
              |               |               |
            GITAS           cPARAS         C-SARAS
      (main dashboard,  (deep single-  (document intelligence,
       declarations,    declaration    patent claim understanding,
       Standards Corpus  analysis)      claim-to-standard mapping)
       admin UI)
              |               |               |
              +---------------+---------------+
                              |
                    SHARED AI SERVICES  (backend/services/)
                              |
             +----------------+----------------+
             |                |                |
             LLM          Embeddings       Retrieval
        (core/llm_engine) (rag/vector_store) (services/retrieval_service.py)
             |                |                |
             +----------------+----------------+
                              |
                 ETSI STANDARDS KNOWLEDGE BASE (backend/etsi_corpus/)
                              |
                   IMPORTED DATASET (etsi_dataset.jsonl + text/)
                              |
                validate -> checkpoint -> index (chunk + embed)
                              |
                     Enriched Dataset
              (ETSI_Series_Enriched_v0.3.xlsx + .jsonl)
                              |
                        Vector Index
                              |
              Versioned, Portable Knowledge Package
                    (etsi_corpus/package.py)
```

GITAS, cPARAS, and C-SARAS are three modules sharing one FastAPI backend,
one LLM engine, one embedding model, one vector store, and one database —
not three separate applications. C-SARAS is a document-intelligence and
patent-claim module *inside* the platform, not a replacement for it.

## Dataset-first architecture (v0.3-continuation)

**SEP Platform does not acquire ETSI PDFs itself.** PDF download, text
extraction (with OCR), and structured LLM extraction all live in a
separate, standalone project:

```
                    ┌─────────────────────────────┐
                    │   ETSI SCRAPER PIPELINE     │
                    │   (separate repository)     │
                    │                             │
                    │ ETSI Excel URL manifest      │
                    │       ↓                     │
                    │ PDF acquisition               │
                    │       ↓                     │
                    │ PDF extraction (+ OCR)        │
                    │       ↓                     │
                    │ Free LLM API structured       │
                    │ extraction                    │
                    │       ↓                     │
                    │ etsi_dataset.jsonl + text/    │
                    └──────────────┬──────────────┘
                                   │
                                   │  dataset zip (JSONL + text/)
                                   ▼
                    ┌─────────────────────────────┐
                    │   SEP PLATFORM V0.3          │
                    │                             │
                    │ POST /api/etsi-corpus/       │
                    │      dataset/import          │
                    │       ↓                     │
                    │ validate + checkpoint         │
                    │       ↓                     │
                    │ index (chunk + embed)         │
                    │       ↓                     │
                    │ GITAS / cPARAS / C-SARAS      │
                    └─────────────────────────────┘
```

No module under `backend/etsi_corpus/` makes an HTTP request to
`etsi.org`, downloads a PDF, or calls an LLM to extract structured fields
from a document — that entire pipeline is the standalone
`etsi-scraper-pipeline` project. SEP's job starts at "already-prepared
dataset" and ends at "indexed, searchable, feeding GITAS/cPARAS/C-SARAS".

## The one-time import vs. normal operation

**Importing a dataset is an explicit, one-time-per-dataset step.** The
application never triggers it automatically:

```
ONE-TIME (explicit CLI/API call, whenever a new/updated dataset exists):

etsi_dataset.jsonl + text/  (produced by etsi-scraper-pipeline)
        |
   validate -> checkpoint -> index (chunk + embed)
        |
   ETSI_Series_Enriched_v0.3.xlsx + .jsonl + vector index
        |
   `package build`  ->  versioned, portable package at ETSI_DATASET_DIR

NORMAL STARTUP (every time, on any machine):

Start backend -> init DB -> init vector store
        |
   ensure_dataset_available()   [READ-ONLY: logs package status, never
                                  imports/indexes/embeds anything]
        |
   Ready
```

If no package is installed, the platform still starts — GITAS's dashboard,
cPARAS's declaration analysis, and C-SARAS's document classification all
work against whatever's already indexed (which may be nothing); standards
retrieval and claim-to-standard matching simply return no evidence until a
dataset is imported. The UI never claims a match it can't back with a real
chunk (`services/retrieval_service.py`).

## Portability ("Machine A imports, Machine B installs")

```
Machine A                          Machine B
----------                         ----------
python -m etsi_corpus.cli import --jsonl etsi_dataset.jsonl --text-dir text/
python -m etsi_corpus.cli package build
        |
  ./data/etsi_v0.3/  (self-contained directory)
        |
   copy or share  ------------->  python -m etsi_corpus.cli package install \
                                     --from-dir /path/to/copied/etsi_v0.3
                                          |
                                   GITAS + cPARAS + C-SARAS usable
                                   immediately -- no re-import, no
                                   re-running the LLM
```

The package is versioned (`package_manifest.json`: dataset version, source
dataset filename/hash, record counts, extraction/LLM/embedding model
versions, generation timestamp) so a re-import of an updated dataset can
reason about what's already current.

## Two distinct datasets — never merge them

- **GITAS declaration data** (`Declaration`/`Patent`/`SepBulkPatent`
  tables): what companies have *declared* about their patents against
  standards.
- **ETSI standards corpus** (`EtsiDocument` table + vector index): what the
  standards *actually contain*, extracted from the real specification
  text by the standalone scraper and imported here.

C-SARAS's claim-to-standard mapping is the connective layer between them —
it does not collapse the two into one table.

## Where things live

| Concern | Module |
|---|---|
| Dataset import (validate, checkpoint, populate fields) | `backend/etsi_corpus/pipeline.py` |
| Dataset zip unpacking | `etsi_corpus/pipeline.py::unpack_dataset_zip()` |
| Clause-aware chunking + embedding | `backend/etsi_corpus/indexer.py` |
| Enriched dataset export | `backend/etsi_corpus/enrichment.py`, `export.py` |
| Structured record schema (shared shape with the scraper) | `backend/etsi_corpus/models.py` |
| Versioned/portable package | `backend/etsi_corpus/package.py` |
| Shared hybrid retrieval | `backend/services/retrieval_service.py` |
| Patent claim workflow | `backend/services/patent_workflow.py` |
| Standards Corpus API (dataset import, jobs, export) | `backend/api/routes/gitas/etsi_corpus.py` |
| C-SARAS patent workflow API | `backend/api/routes/csaras/patent_analysis.py` |
| Standards Corpus UI (dataset upload/import) | `frontend/src/gitas/pages/StandardsCorpusPage.tsx`, `frontend/src/gitas/components/KnowledgeBaseCard.tsx` |
| C-SARAS patent analysis UI | `frontend/src/csaras/PatentAnalysisPage.tsx` |
| **ETSI acquisition (download/extract/LLM)** | **NOT in this repository — see the separate `etsi-scraper-pipeline` project** |

## Illustrative Mode — independent of ETSI acquisition

`ingestion/illustrative_corpus.py` + the shared `/api/illustrative`
endpoint + `frontend/src/shared/IllustrativeModeToggle.tsx` (used
identically by GITAS's Corpus page, cPARAS, and C-SARAS) index a small,
self-contained offline reference corpus — entirely independent of
`etsi_corpus/` and the dataset-import pipeline. Removing/changing ETSI
acquisition never affects Illustrative Mode; it has no import dependency
on `etsi_corpus/` at all. cPARAS's essentiality-analysis fallback corpus
(`research/standards_corpus.py`) is separately self-contained the same way.

## Legacy 3GPP crawler

`ingestion/gpp_crawl.py` and `/corpus` (`api/routes/gitas/scrape.py`) are
kept mounted for backward compatibility and as a secondary source — their
output shares the same vector store (`source_type="3gpp_spec_text"`), and
`services/retrieval_service.py` merges it alongside the imported ETSI
corpus. Not required for GITAS, cPARAS, or C-SARAS to function, and
unaffected by the dataset-first refactor (it was already a pre-existing,
separate ingestion path).
