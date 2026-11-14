"""
Shared backend services (v0.3) -- item 17.

CPARAS Bulk and CPARAS ISLD call INTO these instead of each maintaining
their own retrieval logic:

  - services.retrieval_service  -- hybrid retrieval over the shared vector
    store (rag.vector_store), spanning the ETSI standards corpus
    (etsi_corpus) and the legacy 3GPP crawler corpus (ingestion/spec_corpus)
    where both exist.
"""
