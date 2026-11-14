"""
ETSI Standards Corpus (v0.3, dataset-first architecture)
==================================================================
The shared Standards Corpus backing the Standards Corpus page,
cPARAS's standards retrieval, and C-SARAS's patent-to-standard
classification.

v0.3-continuation: SEP Platform no longer acquires ETSI PDFs itself. PDF
download, text extraction, and LLM structured extraction now live entirely
in the standalone `etsi-scraper-pipeline` project (a separate repository).
This package's job starts AFTER that:

    etsi_dataset.jsonl + text/ (produced by etsi-scraper-pipeline)
        -> pipeline.py     (import: validate, checkpoint, populate fields)
        -> indexer.py      (semantic chunking, embeddings, vector store)
        -> enrichment.py   (ETSI_Series_Enriched_v0.3.xlsx + JSONL export)
        -> package.py      (versioned, portable Standards Corpus package)

Orchestrated end to end, resumably, by pipeline.py's import_dataset().
Never a from-scratch reimplementation of the embedding model or vector
store -- those are the existing rag.vector_store module, reused as-is.

No module in this package makes an HTTP request to etsi.org, calls an LLM
to extract structured fields from a PDF, or downloads a PDF -- that is by
design (see the "Dataset-first architecture" note in each module's
docstring, and ARCHITECTURE_v0.3.md at the repo root for the full picture).

This package also does NOT depend on ingestion/gpp_crawl.py (the legacy
3GPP archive crawler, kept mounted separately for backward compatibility)
-- it is a self-contained dataset-import pipeline.
"""
