"""
Configuration — CPARAS Platform backend (CPARAS Bulk + CPARAS ISLD).

Single source of truth for every setting. Values come from (highest first):
real environment variables, then backend/.env, then the defaults below.

Paths:
  * backend/.env is found relative to THIS FILE, not the current working
    directory, so the backend reads the same config whether it's started
    from backend/, the project root, a Windows shortcut or a service.
  * Relative data paths (./data/...) are anchored to the backend directory
    for the same reason.

Unknown keys in .env are ignored (a stale .env from an older version must
not stop the backend from starting).
"""

import os
from pathlib import Path
from typing import Literal, Optional
from urllib.parse import urlparse

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parent.parent
PROJECT_DIR = BACKEND_DIR.parent
# Default home of the tgpp-sqlite-retrieval index (v0.4.6): copy the whole `retrieval_data` folder here
# (search_index.sqlite, manifest.json, schema_version.json, build_report.json, README.txt, tgpp_retrieval.py).
# Used whenever CPARAS_RETRIEVAL_DATA is empty.
DEFAULT_RETRIEVAL_DATA_DIR = PROJECT_DIR / "dist" / "retrieval_data"


def _ollama_url_from_ollama_host(value: str) -> str:
    """Translate Ollama's own OLLAMA_HOST (e.g. "0.0.0.0", "127.0.0.1:11500",
    "http://localhost:11434") into a URL a client can connect to."""
    v = value.strip()
    if "://" not in v:
        v = "http://" + v
    u = urlparse(v)
    host = u.hostname or "127.0.0.1"
    if host in ("0.0.0.0", "::", ""):
        host = "127.0.0.1"
    port = u.port or 11434
    return f"{u.scheme or 'http'}://{host}:{port}"


class Settings(BaseSettings):
    """All backend settings (pydantic-settings). Each attribute is one environment variable /
    backend/.env key of the same name; the default after `=` applies when neither is set. Grouped by
    area (server, LLM, embeddings, storage paths, retrieval index, Bulk scoring, ETSI corpus, CORS,
    authentication). Relative paths are anchored to the backend folder by `_derive_and_anchor`, so
    the app behaves the same whatever the working directory.
    """
    model_config = SettingsConfigDict(
        env_file=str(BACKEND_DIR / ".env"),
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # ── Server ────────────────────────────────────────────────────────────────
    HOST: str = "127.0.0.1"
    PORT: int = 8000
    DEBUG: bool = False

    # ── LLM Backend ──────────────────────────────────────────────────────────
    # Options: "ollama" | "llamacpp" | "vllm"
    LLM_BACKEND: Literal["ollama", "llamacpp", "vllm"] = "ollama"

    # Ollama settings (default — easiest to install).
    # 127.0.0.1 rather than "localhost": on Windows "localhost" can resolve
    # to ::1 first, while Ollama listens on IPv4 127.0.0.1 only, which costs
    # a failed connection attempt (≈2 s on Windows) on every request. If
    # OLLAMA_BASE_URL is not set but Ollama's own OLLAMA_HOST is, it is used.
    OLLAMA_BASE_URL: str = "http://127.0.0.1:11434"
    # The default TEXT model. Must be installed (`ollama list`); it is
    # validated against Ollama at startup and on every analysis — a missing
    # model is reported, never silently replaced by another one.
    OLLAMA_MODEL: str = "qwen2.5:7b"
    # Per-request ceiling for one generation. A low value silently loses a
    # long generation; this only exists so a truly hung connection can't
    # block forever.
    OLLAMA_TIMEOUT: int = 1800  # seconds
    # Health probe (/api/tags) timeout — kept short: the probe is polled.
    OLLAMA_HEALTH_TIMEOUT: float = 5.0
    # Explicit generation smoke test (POST /api/models/test). Generous
    # because the first request after startup includes loading the model.
    OLLAMA_SMOKE_TEST_TIMEOUT: float = 180.0

    # Context window (tokens) sent as num_ctx on every /api/generate call.
    # This must be large enough to hold ONE whole declaration in a single
    # call — chunking a document across multiple independent LLM calls is
    # what actually causes the count-mismatch bug (a 3B model reliably
    # over/under-counts near chunk boundaries with no shared context between
    # calls; see the docstrings in api/routes/analysis.py). 24576 gives
    # headroom for declarations up to ~16-18K chars (covers both the Nokia
    # and Samsung reference samples single-shot) plus the system prompt and
    # JSON output tokens, while staying below the original 32768 to leave
    # more VRAM margin when OLLAMA_CONCURRENCY > 1. If you're on a very
    # small card (~3-4GB) and see OOM/timeouts from Ollama, lower this
    # rather than shrinking chunk sizes in analysis.py — smaller chunks
    # reintroduce the boundary-miscounting bug.
    OLLAMA_NUM_CTX: int = 24576

    # Maximum simultaneous requests this backend sends to Ollama (enforced
    # process-wide in core/llm_engine.py). Each concurrent request needs its
    # own num_ctx-sized KV cache, so the safe default is 1 (sequential).
    # Raise to 2+ only if you have spare VRAM AND start Ollama with
    # OLLAMA_NUM_PARALLEL set to the same value — otherwise Ollama queues
    # the extra requests anyway and they only risk timeouts.
    OLLAMA_CONCURRENCY: int = 1

    # ── GPU fit / reproducibility (v0.4.2) — see core/ollama_runtime.py ─────
    # OLLAMA_NUM_CTX is now the MAXIMUM context. Per model, the backend
    # lowers it (never below OLLAMA_MIN_NUM_CTX) so weights + KV cache fit
    # in the detected VRAM and Ollama keeps every layer on the GPU. Long
    # documents are then split into proportionally smaller chunks.
    # false = always send OLLAMA_NUM_CTX (the v0.4.1 behaviour).
    OLLAMA_FIT_CONTEXT_TO_GPU: bool = True
    OLLAMA_MIN_NUM_CTX: int = 8192
    # VRAM left free for the desktop / browser / other apps on the same GPU.
    GPU_VRAM_RESERVE_GB: float = 0.8
    # Must describe how the Ollama SERVER runs (start.ps1 / start.sh pass
    # these to Ollama when they start it; setup.ps1 stores them for the
    # Ollama tray app). Flash attention + q8_0 KV cache halve the context's
    # memory, which is what lets phi3.5 run on a 4–6 GB card.
    OLLAMA_FLASH_ATTENTION: bool = False
    OLLAMA_KV_CACHE_TYPE: str = "f16"  # f16 | q8_0 | q4_0
    # Fixed sampling seed: with temperature 0 this makes repeated runs of
    # the same file give the same answer.
    OLLAMA_SEED: int = 42
    # How long Ollama keeps the model loaded after a request (avoids a
    # reload between files / between the Test button and an analysis).
    OLLAMA_KEEP_ALIVE: str = "30m"
    # Force the number of layers Ollama puts on the GPU. -1 = let Ollama
    # decide (recommended); 999 = all layers; 0 = CPU only.
    OLLAMA_NUM_GPU: int = -1

    # Vision OCR model for scanned/image-based PDFs (uses Ollama)
    # moondream ~1 GB VRAM — fast, low-power; llava:7b ~4 GB — higher accuracy
    # Pull with: ollama pull moondream
    VISION_MODEL: str = "moondream"
    OCR_RENDER_DPI: int = 180
    # Upper bound on pages sent to the (slow) vision-model OCR fallback per
    # document; pages beyond it are reported as not OCR'd rather than
    # silently taking hours.
    VISION_OCR_MAX_PAGES: int = 60

    # Tesseract OCR (Tier 3 — preferred for scanned/photocopied PDFs, e.g.
    # Samsung-style declarations with zero embedded fonts). Verified to
    # extract all application numbers at 300 DPI on the Samsung sample.
    # TESSERACT_CMD is only needed if tesseract isn't on PATH and isn't in a
    # standard Windows install location (C:\Program Files\Tesseract-OCR —
    # auto-detected, see utils/pdf_parser.py).
    TESSERACT_CMD: str = ""
    # Per-page Tesseract timeout (seconds, 0 = none) so one pathological page
    # can't hang a whole analysis.
    TESSERACT_PAGE_TIMEOUT: int = 120
    TESSERACT_DPI_SCALE: float = 4.17  # ≈300 DPI (4.17 × 72 base DPI)
    TESSERACT_PSM: str = "6"
    # Minimum mean Tesseract word-confidence (0-100) for a page's OCR output
    # to be trusted without a low-confidence flag. Was previously read via
    # settings.TESSERACT_MIN_CONFIDENCE in utils/pdf_parser.py without being
    # declared here — pydantic-settings only picks up .env values for
    # declared fields, so this was silently unconfigurable and always fell
    # back to the hardcoded default in pdf_parser.py's except-block.
    TESSERACT_MIN_CONFIDENCE: float = 55.0

    # llama.cpp settings (alternative — pure local binary)
    LLAMACPP_MODEL_PATH: str = "./models/model.gguf"
    LLAMACPP_N_GPU_LAYERS: int = -1   # -1 = all layers on GPU
    LLAMACPP_N_CTX: int = 32768       # context window
    LLAMACPP_N_THREADS: int = 8

    # vLLM settings (alternative — best for multi-GPU)
    # Port 8010: 8000 is this backend and 8001 the standalone CPARAS ISLD backend.
    VLLM_BASE_URL: str = "http://127.0.0.1:8010/v1"
    VLLM_MODEL: str = "Qwen/Qwen2.5-7B-Instruct"

    # ── Embedding Model ───────────────────────────────────────────────────────
    # Runs locally via sentence-transformers — no API needed
    EMBEDDING_MODEL: str = "BAAI/bge-m3"          # best general + multilingual
    # "auto" (v0.4.2 default): CUDA if PyTorch can see an NVIDIA GPU, else
    # Apple "mps", else CPU — checked at load time, so a machine without a
    # GPU (or with the CPU-only PyTorch wheel) still works. Set "cpu" to keep
    # the GPU's VRAM entirely for the Ollama model.
    EMBEDDING_DEVICE: str = "auto"                  # "auto" | "cuda" | "cpu" | "mps"
    EMBEDDING_BATCH_SIZE: int = 32
    # Offline by default: the model must already be downloaded (the setup
    # scripts do it). true = allow a one-time download from huggingface.co
    # at runtime if it's missing.
    EMBEDDING_ALLOW_DOWNLOAD: bool = False
    EMBEDDING_DIM: int = 1024                       # matches bge-m3

    # ── Embedding memory control (v0.4.4.1) ─────────────────────────────────────
    # Why: bge-m3 accepts up to 8192 tokens per text, and a Transformer's attention-score
    # tensor is  batch x heads(16) x seq_len^2 x 4 bytes.  One unbroken 2,048-token piece in a
    # batch of 32 is 8.59 GB; one 8,192-token piece is 4.3 GB PER SEQUENCE.  These settings keep
    # that tensor small however odd the text is.
    # Longest sequence (tokens) the INDEXER feeds the model. Longer chunks are split into
    # traceable parts (never truncated). Search queries are NOT limited by this.
    EMBEDDING_MAX_SEQ_LENGTH: int = 1024
    # Batch-size ceiling on CPU (EMBEDDING_BATCH_SIZE above still applies on GPU/MPS).
    EMBEDDING_CPU_BATCH_SIZE: int = 8
    # Peak working memory (MB) one encode() call may be planned to use (activations only - the
    # model weights, ~2.3 GB for bge-m3 in fp32, are extra). Batches are sized to fit; a single
    # sequence larger than this is refused rather than attempted. 0 = auto (25% of free RAM,
    # clamped to 512-4096 MB).
    EMBEDDING_MEMORY_BUDGET_MB: int = 1536
    # ── v0.4.7 speed ────────────────────────────────────────────────────────
    # fp16 on an NVIDIA GPU (2x faster, half the VRAM) / int8 dynamic quantisation on CPU (2-3x faster).
    # auto = on; false = always fp32.
    EMBEDDING_FP16: str = "auto"
    EMBEDDING_CPU_INT8: str = "auto"
    # Vectors kept in memory for search/scoring texts (clause candidates, prior patents, corpus entries)
    # so they are not re-embedded on every run. 0 = off. ~4 KB each for bge-m3.
    EMBEDDING_CACHE_SIZE: int = 50000
    # Warm up the embedding model, retrieval index and Bulk corpus in the background at startup so the
    # first analysis does not pay for it.
    WARMUP_ON_STARTUP: bool = True
    # ISLD deep enrichment: ask the LLM once per distinct (title, standards) and remember the answer on disk.
    ISLD_ENRICH_CACHE: bool = True

    # ── Large-dataset bulk scoring (etsi_bulk/scoring.py) ───────────────────
    # How many patent rows are embedded/scored together per chunk when
    # processing a bulk ETSI declaration workbook. Chosen so a batch's own
    # (batch_size x corpus_size) similarity matrix and embedding buffer stay
    # small regardless of total dataset size (200 or 17,000+ rows), rather
    # than embedding/scoring the whole dataset in one call. 256 is a good
    # default for CPU sentence-transformers encode() throughput; lower it
    # (e.g. 64-128) on constrained memory, raise it on GPU.
    SCORING_BATCH_SIZE: int = 256
    # How many scored rows are persisted per SQLite transaction during a
    # bulk import. Batched commits (not one per row, not one giant commit
    # for the whole dataset) bound both lock hold time and the size of any
    # single in-flight transaction.
    SCORING_DB_COMMIT_BATCH_SIZE: int = 250


    # ── Vector Database ───────────────────────────────────────────────────────
    # Options: "chromadb" | "qdrant" | "faiss"
    # ChromaDB is the default: it's embedded (no separate server process,
    # no extra docker-compose service) and is what this project's own
    # testing actually exercised end-to-end. Qdrant is supported for
    # larger/shared deployments (see docker-compose.qdrant.yml) but requires
    # running and pointing at a separate Qdrant instance via QDRANT_URL.
    VECTOR_BACKEND: Literal["qdrant", "chromadb", "faiss"] = "chromadb"

    QDRANT_URL: str = "http://127.0.0.1:6333"
    QDRANT_COLLECTION: str = "sep_intelligence"

    CHROMADB_PATH: str = "./data/chromadb"

    FAISS_INDEX_PATH: str = "./data/faiss_index"

    # ── SQLite (metadata / structured data) ──────────────────────────────────
    DATABASE_URL: str = "sqlite+aiosqlite:///./data/sep_intel.db"

    # ── Storage paths ─────────────────────────────────────────────────────────
    DATA_DIR: Path = Path("./data")
    MODELS_DIR: Path = Path("./models")
    UPLOADS_DIR: Path = Path("./data/uploads")
    REPORTS_DIR: Path = Path("./data/reports")
    DATASETS_DIR: Path = Path("./data/datasets")
    CACHE_DIR: Path = Path("./data/cache")

    # ── RAG settings ──────────────────────────────────────────────────────────
    RAG_TOP_K: int = 8            # number of chunks to retrieve
    RAG_CHUNK_SIZE: int = 512     # tokens per chunk
    RAG_CHUNK_OVERLAP: int = 64

    # ── Dataset ingestion ─────────────────────────────────────────────────────
    ETSI_IPR_BASE_URL: str = "https://ipr.etsi.org"
    ENABLE_INGESTION: bool = True
    INGESTION_CONCURRENCY: int = 5
    CRAWL_DELAY_SECONDS: float = 1.5   # be polite to public servers

    # ── 3GPP archive crawler (ingestion/gpp_crawl.py) ────────────────────────
    # Externalized so a deployment can point at a mirror, tune for a slow/
    # flaky network, or lower the retry ceiling in CI -- none of this should
    # require a code change. gpp_crawl.py's crawl_series() still accepts
    # per-call overrides for these (e.g. the frontend's "quick test" flow
    # uses max_files independently), these are just the defaults.
    THREEGPP_BASE_URL: str = "https://www.3gpp.org/ftp/specs/archive/"
    # v0.4.2: default folder for a local 3GPP archive to index (crawler
    # zips, extracted .docx, or 3gpp_<spec>_v<x.y.z>.json files — any
    # sub-folder layout). Used by index_3gpp.ps1 / `python -m
    # ingestion.spec_corpus` and POST /api/scrape/index-local.
    THREEGPP_ARCHIVE_DIR: str = "./data/3gpp_archive"
    CRAWLER_REQUEST_TIMEOUT: float = 60.0     # seconds, per HTTP request
    CRAWLER_MAX_RETRIES: int = 3              # per-file download retry ceiling
    CRAWLER_THROTTLE_SECONDS: float = 0.3     # delay between file downloads
    CRAWLER_MAX_CONCURRENT_DOWNLOADS: int = 4  # per job, see gpp_crawl.py
    CRAWLER_FINALIZE_MAX_RETRIES: int = 5     # Windows file-lock retry ceiling
    CRAWLER_STALE_TEMP_FILE_HOURS: float = 6.0

    # How many series may crawl concurrently within one multi-series batch
    # (v0.3 Part C). Kept low by default -- this is a "be polite to
    # 3gpp.org and don't overwhelm a local Windows dev machine" limit, not
    # a performance knob to maximize. Extra selected series simply queue
    # and start automatically as a slot frees up.
    MAX_CONCURRENT_SERIES_JOBS: int = 2

    # ── ETSI Standards Corpus (v0.3, dataset-first architecture) ──────────────
    # v0.3-continuation: SEP Platform no longer acquires ETSI PDFs itself.
    # PDF download, extraction, and LLM structured extraction now live
    # entirely in the standalone `etsi-scraper-pipeline` project (a separate
    # repository/zip). SEP's job starts AFTER that: import the prepared
    # dataset (JSONL + extracted text) the scraper produced, index it into
    # the shared vector store, and serve retrieval/RAG/analysis to CPARAS Bulk,
    # CPARAS ISLD. See backend/etsi_corpus/dataset_import.py.
    ETSI_DATASET_IMPORT_DIR: str = "./data/etsi_corpus/imports"
    # Where an uploaded dataset zip (etsi_dataset.jsonl + text/) is unpacked
    # before import, and where each import's extracted text is copied to
    # (etsi_corpus/dataset_import.py never trusts a client-supplied path).
    ETSI_DATASET_OUTPUT: str = "./data/etsi_corpus/output"
    # Bounded worker-pool size for the import job (chunking/embedding many
    # documents is still real, non-trivial work -- see
    # etsi_corpus/pipeline.py's bounded-queue architecture, reused
    # unchanged from the old acquisition pipeline for this purpose).
    ETSI_IMPORT_BATCH_SIZE: int = 10
    # Safety valve for accidental full-dataset imports (a real dataset can
    # have 50k+ records) -- explicit --limit on the CLI/API overrides this
    # per-invocation; this is just the default a bare import call uses.
    ETSI_DEFAULT_IMPORT_LIMIT: int = 25

    # ── Canonical 3GPP dataset (builder output, "build_v4") — v0.4.4 ──────────
    # Folder holding etsi_dataset.jsonl / etsi_dataset.zip / spec_history_blobs.jsonl.gz /
    # standards_corpus_3gpp.json / spec_reference_3gpp.json / build_report.json ...
    # READ-ONLY for this application: it is never modified, copied or moved. Empty =
    # not configured (the app behaves exactly as in v0.4.3). Windows example:
    #   CPARAS_DATASET_ROOT=D:\3gpp\cparas_dataset\build_v4
    # The same folder can be shared by CPARAS Platform, CPARAS ISLD and Initial Categoriser.
    CPARAS_DATASET_ROOT: str = ""

    # ── SQLite / FTS5 retrieval index as the dataset (v0.4.5) ────────────────────
    # The `retrieval_data` folder built by tgpp-sqlite-retrieval (search_index.sqlite + manifest.json ...).
    # READ-ONLY, opened in place, never imported or copied.
    # Empty (default) = the project's own folder  <project>\dist\retrieval_data  (DEFAULT_RETRIEVAL_DATA_DIR);
    # when that folder holds no search_index.sqlite the feature is simply off (behaviour as v0.4.4.1).
    # Set it only to use a copy somewhere else, e.g. shared with Initial Categoriser:
    #   CPARAS_RETRIEVAL_DATA=D:\3gpp\tgpp-sqlite-retrieval\dist\retrieval_data
    # A relative path is relative to the backend folder.
    CPARAS_RETRIEVAL_DATA: str = ""
    # Where standards evidence comes from: auto = the retrieval index when it is configured and usable,
    # else the vector store | sqlite = retrieval index only | vector = vector store only (v0.4.4.1
    # behaviour) | both = merge the two.
    RETRIEVAL_SOURCE: Literal["auto", "sqlite", "vector", "both"] = "auto"
    # FTS5 candidates fetched per query before the embedder re-scores them (more = better recall, slower).
    RETRIEVAL_SQLITE_POOL: int = 24
    # v0.4.7: characters of each candidate the embedder reads when re-scoring (~250 tokens; was 1,500).
    RETRIEVAL_RERANK_MAX_CHARS: int = 1000
    # Re-score candidates with the application's embedder (cosine, like a vector-store hit). False = lexical
    # only: hits are flagged score_kind=lexical, capped below 0.35 and never used as semantic evidence.
    RETRIEVAL_SQLITE_RERANK: bool = True
    # CPARAS Bulk corpus built from the retrieval index (BULK_CORPUS_SOURCE=auto|retrieval):
    # which versions (latest-per-release | latest | all), deepest clause level, characters kept per
    # clause, and a ceiling on the number of clause entries (embedding time/memory).
    BULK_RETRIEVAL_VERSIONS: Literal["latest-per-release", "latest", "all"] = "latest-per-release"
    BULK_RETRIEVAL_MAX_DEPTH: int = 3
    BULK_RETRIEVAL_MAX_CHARS: int = 1500
    BULK_RETRIEVAL_MIN_CHARS: int = 40
    BULK_RETRIEVAL_MAX_ENTRIES: int = 250000
    # CPARAS Bulk reference corpus: auto = standards_corpus_3gpp.json from CPARAS_DATASET_ROOT when it is
    # present and valid, else the built-in illustrative corpus | dataset = require the dataset corpus |
    # illustrative = never use the dataset (exactly v0.4.3 behaviour).
    BULK_CORPUS_SOURCE: Literal["auto", "retrieval", "dataset", "illustrative"] = "auto"
    # ── v0.4.7 Bulk speed ────────────────────────────────────────────────────
    # A large corpus (retrieval index / dataset: up to 60,000 clauses) is no longer embedded in full before
    # the first row is scored. Each row is compared with a shortlist of clauses (best TF-IDF matches overall
    # + best in the declared specification), only those clauses are embedded, and their vectors are kept on
    # disk, so every later run reuses them. Once every clause has a vector the full exhaustive comparison
    # is used automatically.
    #   auto       = exhaustive for small corpora or when all vectors are cached, shortlist otherwise
    #   exhaustive = always embed the whole corpus first (v0.4.6 behaviour; slow first run)
    #   shortlist  = always use the shortlist
    BULK_SCORING_STRATEGY: Literal["auto", "exhaustive", "shortlist"] = "auto"
    BULK_EXHAUSTIVE_MAX_ENTRIES: int = 4000      # corpora up to this size are always scored exhaustively
    BULK_SHORTLIST_GLOBAL: int = 16              # TF-IDF candidates per row from the whole corpus
    BULK_SHORTLIST_DECLARED: int = 24           # ... plus this many from the declared specification
    BULK_SHORTLIST_TITLES: int = 8                # + clauses under the 8 closest clause titles (embedding)
    BULK_CALIBRATION_SAMPLE: int = 96           # random clauses used to measure the similarity floor
    # Tokens of each clause excerpt the embedder reads (patent texts are short; 256 keeps the match quality
    # and roughly halves the embedding time of a 1,500-character excerpt).
    BULK_CORPUS_EMBED_MAX_TOKENS: int = 160
    # Finish embedding the remaining clauses in the background after a run (yields to interactive work):
    # auto = only when the embedder runs on a GPU | true | false
    BULK_CORPUS_PRECOMPUTE: str = "auto"
    # ISLD retrieval: at most this many query words are searched, and words found in more than this share
    # of all index rows are skipped (their BM25 weight is negligible but they make FTS5 rank millions of rows).
    RETRIEVAL_FTS_MAX_TERMS: int = 10
    RETRIEVAL_FTS_COMMON_FRACTION: float = 0.03
    # Local dataset import: how many streamed records are checkpointed + processed per batch.
    ETSI_STREAM_BATCH: int = 200
    # ...and a batch is also flushed once its records' clause text adds up to this many MB, so a run of
    # very large records can never pile up in RAM just because the record COUNT is still small.
    ETSI_STREAM_BATCH_MAX_MB: int = 64
    # True  = copy each record's text into this app's own store (legacy behaviour).
    # False = keep the text only in the canonical etsi_dataset.zip and read it from there
    #         (no multi-GB duplicate). The local-dataset import uses False by default.
    ETSI_COPY_TEXT_LOCALLY: bool = True
    # The portable-package auto-build after an import copies the text + vector index and builds
    # an in-memory workbook; it is skipped automatically above this many documents.
    ETSI_AUTO_PACKAGE_MAX_DOCS: int = 2000

    # ── Memory-bounded indexing (v0.4.4.1) ──────────────────────────────────────
    # Chunks embedded + written to the vector store per group. Only one group of vectors is
    # ever in RAM, however large the document.
    ETSI_INDEX_GROUP_CHUNKS: int = 64
    # A document with more characters than this is flagged "large" in the report (it is
    # processed the same bounded way; the flag is for visibility).
    ETSI_LARGE_DOC_CHARS: int = 3_000_000
    # Hard ceiling: a document larger than this is NOT indexed; it is recorded as failed
    # (document_too_large) with the reason - never silently skipped. 0 = no ceiling.
    ETSI_MAX_DOC_CHARS: int = 60_000_000
    # Extra attempts for one document after a memory error (each retry halves the group size
    # and the embedding memory budget). The index is rolled back to its prior state between tries.
    ETSI_INDEX_DOC_RETRIES: int = 2
    # Before each group, if the machine has less than this much free RAM (MB) the importer
    # frees memory and waits; if it stays low the document fails as retryable
    # (low_memory) instead of risking the whole process. 0 = check off.
    ETSI_MIN_FREE_RAM_MB: int = 768
    # Optional ceiling on this process's resident memory (MB). 0 = off.
    ETSI_PROCESS_RSS_LIMIT_MB: int = 0

    # ── Standards Corpus package (portability/versioning, v0.3) ────────────────
    # A dataset import is itself a one-time-per-dataset step, run explicitly
    # via the CLI/API -- it never runs automatically on startup. The result
    # is packaged as a versioned, portable directory (enriched xlsx/jsonl +
    # vector index + manifest) that a normal `start.sh` run just loads.
    # See etsi_corpus/package.py.
    ETSI_DATASET_DIR: str = "./data/etsi_v0.3"
    ETSI_DATASET_VERSION: str = "v0.3"

    # ── Max upload size for the former categoriser module/analysis document uploads (MB) ────────────
    MAX_UPLOAD_MB: int = 50

    # ── CPARAS ISLD /analyze scale guard (v0.7) ────────────────────────────────────
    # CPARAS ISLD's single-declaration research augmentation (research.engine.
    # analyze_patents) embeds every disclosure + the whole standards corpus
    # + prior patents in ONE call and builds a full (n_disclosures x
    # n_corpus) similarity matrix in memory -- the same unbounded, single-
    # matrix shape CPARAS Bulk's own bulk scoring (etsi_bulk/scoring.py) moved
    # away from specifically because it doesn't scale past a few thousand
    # rows. CPARAS ISLD is architecturally for ONE declarant's filing (tens to a
    # few hundred rows); a bulk multi-company export (the 35,000-50,000+
    # row ETSI-export shape CPARAS Bulk are built for) fed into this
    # endpoint instead crashed/hung the backend well after extraction had
    # already finished (see CHANGELOG v0.7). Above this many disclosures,
    # research augmentation is skipped with a clear message pointing at
    # CPARAS Bulk instead of attempting the unbounded matrix -- the
    # extracted/cleaned tabular data itself is still returned and persisted.
    CPARAS_MAX_DISCLOSURES_FOR_RESEARCH: int = 3000

    # ── Logging ───────────────────────────────────────────────────────────────
    LOG_LEVEL: str = "INFO"  # DEBUG | INFO | WARNING | ERROR

    # ── Patent title lookup (research/patent_lookup.py) ──────────────────────
    # Free registration: https://developers.epo.org
    # Left blank, EPO OPS lookups are skipped (PatentsView-only fallback).
    EPO_OPS_KEY: str = ""
    EPO_OPS_SECRET: str = ""

    # ── CORS ──────────────────────────────────────────────────────────────────
    # Extra browser origins allowed to call the API (comma-separated), on
    # top of the local dev/preview/docker origins main.py always allows.
    CORS_EXTRA_ORIGINS: str = ""

    # ── Authentication & roles (v0.5.0, see auth/ and docs/AUTH.md) ───────────
    # Every API except /api/health and the login/first-run-setup endpoints
    # requires a signed-in user; what each role may do is in auth/roles.py.
    # Sessions are an HttpOnly cookie (browser) or "Authorization: Bearer"
    # (scripts/other services). There is no setting that turns auth off.
    AUTH_SESSION_IDLE_MINUTES: int = 480        # signed out after 8 h without activity
    AUTH_SESSION_MAX_HOURS: int = 72            # absolute session lifetime
    AUTH_COOKIE_NAME: str = "cparas_session"
    # Set true when the app is served over HTTPS (reverse proxy), so the
    # browser only ever sends the session cookie over TLS. Plain-HTTP offline
    # installs (127.0.0.1) need false.
    AUTH_COOKIE_SECURE: bool = False
    AUTH_PASSWORD_MIN_LENGTH: int = 10
    AUTH_LOGIN_MAX_FAILURES: int = 5            # failed logins (per user + client) before a lockout
    AUTH_LOGIN_LOCKOUT_MINUTES: int = 10
    # One-time token for creating the first super admin from the browser.
    # Written here (relative to backend/) at start-up while NO user exists,
    # and deleted once the first super admin is created. Read it on the
    # server machine; the CLI (python -m auth.cli create-superadmin) is the
    # alternative that needs no token.
    AUTH_SETUP_TOKEN_FILE: str = "./data/FIRST_ADMIN_SETUP_TOKEN.txt"

    @model_validator(mode="after")
    def _derive_and_anchor(self):
        """Post-validation fix-ups: derive OLLAMA_BASE_URL from Ollama's own OLLAMA_HOST when only that
        is set, strip trailing slashes, and turn every relative data path (and a relative SQLite
        DATABASE_URL) into an absolute path under backend/.
        """
        # OLLAMA_HOST (Ollama's own variable) -> OLLAMA_BASE_URL, only when
        # OLLAMA_BASE_URL was not given explicitly.
        if "OLLAMA_BASE_URL" not in self.model_fields_set and os.environ.get("OLLAMA_HOST"):
            self.OLLAMA_BASE_URL = _ollama_url_from_ollama_host(os.environ["OLLAMA_HOST"])
        self.OLLAMA_BASE_URL = self.OLLAMA_BASE_URL.rstrip("/")

        def anchor(p):
            """Absolute path for `p`, resolved against the backend directory when relative."""
            pp = Path(p)
            return pp if pp.is_absolute() else (BACKEND_DIR / pp).resolve()

        for name in ("DATA_DIR", "MODELS_DIR", "UPLOADS_DIR", "REPORTS_DIR", "DATASETS_DIR", "CACHE_DIR"):
            setattr(self, name, anchor(getattr(self, name)))
        for name in ("CHROMADB_PATH", "FAISS_INDEX_PATH", "ETSI_DATASET_IMPORT_DIR", "THREEGPP_ARCHIVE_DIR",
                     "ETSI_DATASET_OUTPUT", "ETSI_DATASET_DIR", "LLAMACPP_MODEL_PATH", "AUTH_SETUP_TOKEN_FILE"):
            setattr(self, name, str(anchor(getattr(self, name))))
        prefix = "sqlite+aiosqlite:///"
        if self.DATABASE_URL.startswith(prefix):
            db_path = self.DATABASE_URL[len(prefix):]
            if db_path and db_path != ":memory:" and not Path(db_path).is_absolute():
                self.DATABASE_URL = prefix + anchor(db_path).as_posix()
        return self


settings = Settings()

# Ensure all directories exist on import
for _dir in [
    settings.DATA_DIR,
    settings.MODELS_DIR,
    settings.UPLOADS_DIR,
    settings.REPORTS_DIR,
    settings.DATASETS_DIR,
    settings.CACHE_DIR,
    Path(settings.ETSI_DATASET_IMPORT_DIR),
    Path(settings.ETSI_DATASET_OUTPUT),
    Path(settings.ETSI_DATASET_DIR),
    Path(settings.THREEGPP_ARCHIVE_DIR),
]:
    _dir.mkdir(parents=True, exist_ok=True)
