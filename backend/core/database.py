"""
Database — SQLAlchemy async models.
Stores structured metadata alongside the vector store.
"""

from datetime import datetime
from typing import Optional, List
import json
import logging

from sqlalchemy import String, Integer, Float, DateTime, Text, Boolean, ForeignKey, JSON, LargeBinary, event, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from core.config import settings
from core.timeutil import utcnow as _utcnow

logger = logging.getLogger(__name__)

# ── Engine ─────────────────────────────────────────────────────────────────────
_IS_SQLITE = settings.DATABASE_URL.startswith("sqlite")

# check_same_thread is a SQLite-only DBAPI argument. Passing it unconditionally
# makes create_async_engine raise TypeError the moment DATABASE_URL points at
# anything else (e.g. postgresql+asyncpg://), so the setting was effectively
# undocumented-SQLite-only despite being configurable.
engine = create_async_engine(
    settings.DATABASE_URL,
    echo=settings.DEBUG,
    connect_args={"check_same_thread": False} if _IS_SQLITE else {},
)


@event.listens_for(engine.sync_engine, "connect")
def _set_sqlite_pragmas(dbapi_connection, _connection_record):
    """
    Apply the PRAGMAs that make SQLite usable under concurrent access.

    This backend serves progress polls (~1/sec) and search queries while a
    long analysis holds a write transaction. With SQLite's defaults —
    rollback journal, and a 0 ms busy timeout on this driver — a reader that
    arrives during a write fails immediately with "database is locked" rather
    than waiting, which surfaced as random 500s on unrelated endpoints during
    an analysis run.

      journal_mode=WAL  readers no longer block on a concurrent writer
      busy_timeout      wait for a lock instead of failing instantly
      synchronous=NORMAL safe with WAL, and far fewer fsyncs per commit
      foreign_keys=ON   SQLite ignores FK constraints unless asked; without
                        this the cascade deletes declared on the ORM
                        relationships are not enforced at the DB level
    """
    if not _IS_SQLITE:
        return
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=10000")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()


AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    """Declarative base for every ORM table. `init_db()` creates all tables registered on it and then
    runs the small SQLite column migrations below.
    """
    pass


# ── ORM Models ────────────────────────────────────────────────────────────────

class Declaration(Base):
    """One ETSI IPR declaration filing (one PDF)."""
    __tablename__ = "declarations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    filename: Mapped[str] = mapped_column(String(512))
    declarant: Mapped[Optional[str]] = mapped_column(String(512))
    filing_type: Mapped[Optional[str]] = mapped_column(String(128))
    project: Mapped[Optional[str]] = mapped_column(String(256))
    likely_standard_generation: Mapped[Optional[str]] = mapped_column(String(64))
    portfolio_characterization: Mapped[Optional[str]] = mapped_column(Text)
    overall_technology_domains: Mapped[Optional[str]] = mapped_column(Text)  # JSON list
    confidence: Mapped[Optional[float]] = mapped_column(Float)
    analysis_mode: Mapped[str] = mapped_column(String(32), default="deep")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)
    raw_json: Mapped[Optional[str]] = mapped_column(Text)  # full LLM output (incl. research_outputs)

    patents: Mapped[List["Patent"]] = relationship("Patent", back_populates="declaration", cascade="all, delete-orphan")
    portfolio: Mapped[Optional["PortfolioAnalysis"]] = relationship("PortfolioAnalysis", back_populates="declaration", uselist=False, cascade="all, delete-orphan")
    research_run: Mapped[Optional["ResearchRun"]] = relationship("ResearchRun", back_populates="declaration", uselist=False, cascade="all, delete-orphan")


class Patent(Base):
    """One patent disclosure row within a declaration."""
    __tablename__ = "patents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    declaration_id: Mapped[int] = mapped_column(ForeignKey("declarations.id"), index=True)
    disclosure_number: Mapped[Optional[int]] = mapped_column(Integer)

    # Explicit fields from the PDF table
    project: Mapped[Optional[str]] = mapped_column(String(256))
    standards: Mapped[Optional[str]] = mapped_column(Text)   # JSON list e.g. ["TS 38.331"]
    applicant: Mapped[Optional[str]] = mapped_column(String(512))
    application_number: Mapped[Optional[str]] = mapped_column(String(128), index=True)
    publication_number: Mapped[Optional[str]] = mapped_column(String(128))
    country: Mapped[Optional[str]] = mapped_column(String(128))
    title: Mapped[Optional[str]] = mapped_column(Text)

    # Analysis outputs (stored as JSON strings for flexibility)
    standards_analysis: Mapped[Optional[str]] = mapped_column(Text)  # JSON
    sep_analysis: Mapped[Optional[str]] = mapped_column(Text)        # JSON
    geographic_analysis: Mapped[Optional[str]] = mapped_column(Text) # JSON
    inferred_fields: Mapped[Optional[str]] = mapped_column(Text)     # JSON

    # Derived scalar fields for fast filtering
    sep_strength: Mapped[Optional[str]] = mapped_column(String(64))
    sep_confidence: Mapped[Optional[float]] = mapped_column(Float)
    primary_domain: Mapped[Optional[str]] = mapped_column(String(128))
    jurisdiction: Mapped[Optional[str]] = mapped_column(String(64))

    # ── Research / paper-style outputs (see research/engine.py) ────────────
    predicted_sep_status: Mapped[Optional[str]] = mapped_column(String(64))   # Confirmed/Likely/Potential SEP, Insufficient Evidence
    research_confidence: Mapped[Optional[float]] = mapped_column(Float)        # blended similarity+LLM confidence
    recommended_standard: Mapped[Optional[str]] = mapped_column(String(64))    # "TS 38.331"
    recommended_version: Mapped[Optional[str]] = mapped_column(String(32))     # "v16.2.0"
    standard_clause: Mapped[Optional[str]] = mapped_column(String(64))         # "Clause 5.3.5" / "Section 9"
    standard_similarity: Mapped[Optional[float]] = mapped_column(Float)        # cosine similarity to recommended clause
    # v0.5: the two remaining mandatory "Score Breakdown" signals (the third,
    # semantic_similarity, IS standard_similarity above -- same calibrated
    # cosine-to-matched-clause value already computed by research/engine.py;
    # exposed under the CPARAS_Semantic_Similarity export name rather than
    # duplicated as a second column -- see research/three_signal_scoring.py
    # and CPARAS_FIX in the v0.5 engineering report for why claim_match and
    # standard_coverage could not simply reuse/rename an existing field).
    claim_match: Mapped[Optional[float]] = mapped_column(Float)
    standard_coverage: Mapped[Optional[float]] = mapped_column(Float)
    similar_patent_ref: Mapped[Optional[str]] = mapped_column(String(128))     # nearest-neighbour patent label
    similar_patent_score: Mapped[Optional[float]] = mapped_column(Float)
    match_explanation: Mapped[Optional[str]] = mapped_column(Text)             # JSON list of bullet strings
    # v0.4.9: exact wording shared by the patent and its matched clause (research/phrase_match.py)
    matched_phrases: Mapped[Optional[str]] = mapped_column(Text)               # JSON list of phrases
    matched_terms: Mapped[Optional[str]] = mapped_column(Text)                 # JSON list of single shared terms
    verification_status: Mapped[Optional[str]] = mapped_column(String(64))     # Verified/Predicted/Stable/Declining
    version_evolution: Mapped[Optional[str]] = mapped_column(Text)             # JSON {points, trend, ...}
    is_potential_sep: Mapped[Optional[bool]] = mapped_column(Boolean, default=False)
    potential_sep_detail: Mapped[Optional[str]] = mapped_column(Text)          # JSON {topic, reason, ...}

    # v0.3 fix (cPARAS audit items 8-14): essentiality evidence now comes
    # from the shared standards retrieval layer -- the 3GPP Retrieval Index or the
    # Standards Corpus (services/retrieval_service.py)
    # where available, not only the static illustrative reference corpus
    # (research/standards_corpus.py) -- these columns make WHICH source
    # produced a given result, and the exact supporting text, persisted and
    # reproducible even after the retrieval index itself later changes.
    evidence_source: Mapped[Optional[str]] = mapped_column(String(32))
    # "etsi_knowledge_base" (historical id: real retrieved evidence) | "illustrative_reference_corpus" | "none"
    evidence_text: Mapped[Optional[str]] = mapped_column(Text)
    # Verbatim-adjacent excerpt that produced recommended_standard/
    # standard_clause above, or "No evidence found" -- never fabricated.
    knowledge_base_version: Mapped[Optional[str]] = mapped_column(String(32))
    # Snapshot of the installed ETSI package's dataset_version at analysis
    # time (item 14), "Unknown" if none was installed. NULL for analyses
    # run before this column existed.
    evidence_retrieved_at: Mapped[Optional[datetime]] = mapped_column(DateTime)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)

    declaration: Mapped["Declaration"] = relationship("Declaration", back_populates="patents")


class PortfolioAnalysis(Base):
    """Portfolio-level analysis for a declaration."""
    __tablename__ = "portfolio_analyses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    declaration_id: Mapped[int] = mapped_column(ForeignKey("declarations.id"), index=True)
    dominant_technology_domains: Mapped[Optional[str]] = mapped_column(Text)
    release_focus: Mapped[Optional[str]] = mapped_column(Text)
    most_targeted_specs: Mapped[Optional[str]] = mapped_column(Text)
    geographic_filing_strategy: Mapped[Optional[str]] = mapped_column(Text)
    portfolio_strength_assessment: Mapped[Optional[str]] = mapped_column(Text)
    frand_implications: Mapped[Optional[str]] = mapped_column(Text)
    implementation_risk_summary: Mapped[Optional[str]] = mapped_column(Text)

    declaration: Mapped["Declaration"] = relationship("Declaration", back_populates="portfolio")


class ResearchRun(Base):
    """
    Aggregate research/paper-style analysis results for one declaration
    (Requirements 7, 10, 11) — metrics and summary statistics. Per-patent
    results live as columns on `Patent`; rendered figures live in
    `Declaration.raw_json.research_outputs.figures`.
    """
    __tablename__ = "research_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    declaration_id: Mapped[int] = mapped_column(ForeignKey("declarations.id"), index=True)
    embedder_mode: Mapped[Optional[str]] = mapped_column(String(64))   # "sentence-transformers" | "tfidf-fallback"
    similarity_threshold: Mapped[Optional[float]] = mapped_column(Float)
    corpus_size: Mapped[Optional[int]] = mapped_column(Integer)
    metrics_json: Mapped[Optional[str]] = mapped_column(Text)      # accuracy/precision/recall/F1/kappa/confusion matrix
    aggregates_json: Mapped[Optional[str]] = mapped_column(Text)   # averages, top standards/applicants/versions
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)

    declaration: Mapped["Declaration"] = relationship("Declaration", back_populates="research_run")


class KnowledgeChunk(Base):
    """Telecom standards text chunks (for RAG)."""
    __tablename__ = "knowledge_chunks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_type: Mapped[str] = mapped_column(String(64))   # "3gpp_spec" | "etsi_ipr" | "patent" | "frand"
    source_id: Mapped[Optional[str]] = mapped_column(String(256))
    source_url: Mapped[Optional[str]] = mapped_column(String(1024))
    title: Mapped[Optional[str]] = mapped_column(Text)
    content: Mapped[str] = mapped_column(Text)
    extra_metadata: Mapped[Optional[str]] = mapped_column(Text)  # JSON (was 'metadata' — reserved by SQLAlchemy)
    vector_id: Mapped[Optional[str]] = mapped_column(String(128))  # ID in vector store
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)


class IngestionJob(Base):
    """Track dataset ingestion jobs."""
    __tablename__ = "ingestion_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(32), default="pending")  # pending|running|done|failed
    records_fetched: Mapped[int] = mapped_column(Integer, default=0)
    records_indexed: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[Optional[str]] = mapped_column(Text)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)


class SpecCrawlJob(Base):
    """Track a 3GPP-archive-crawl-and-index job started from the frontend's
    Scraper panel (see api/routes/scrape.py, ingestion/gpp_crawl.py).

    Deliberately separate from IngestionJob: that table's `source` is a
    fixed key into ingestion.pipeline.SOURCE_CRAWLERS (etsi_ipr,
    3gpp_specs, ...), one row per *source*. A spec crawl is one row per
    *3GPP series* (e.g. "38_series"), with byte/file counters a plain
    ingestion job has no use for, and a distinct downstream step (index
    via ingestion/spec_corpus.py) tracked in its own status field.
    """
    __tablename__ = "spec_crawl_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    series: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32), default="pending")
    # pending|crawling|indexing|done|failed
    files_downloaded: Mapped[int] = mapped_column(Integer, default=0)
    files_skipped: Mapped[int] = mapped_column(Integer, default=0)
    files_failed: Mapped[int] = mapped_column(Integer, default=0)
    files_discovered: Mapped[int] = mapped_column(Integer, default=0)
    # Live count of files SEEN so far while the crawl recurses the series'
    # directory tree -- grows during the crawl. Distinct from total_files
    # (only ever set once the crawl has actually finished and the true
    # total is known), so the frontend can show "N discovered so far"
    # without it being confused for a stable denominator mid-crawl.
    bytes_downloaded: Mapped[int] = mapped_column(Integer, default=0)
    chunks_indexed: Mapped[int] = mapped_column(Integer, default=0)
    current_path: Mapped[Optional[str]] = mapped_column(String(512))
    # Progress/UX fields (v0.3) -- see api/routes/scrape.py's job-to-dict
    # serialization and the frontend's progress dashboard. total_files is
    # nullable and left None until the crawl has actually discovered the
    # full file list for the series being crawled -- it is never guessed,
    # per the "don't fabricate a percentage" requirement.
    total_files: Mapped[Optional[int]] = mapped_column(Integer)
    current_operation: Mapped[Optional[str]] = mapped_column(String(32))
    # discovering|downloading|extracting|indexing|completed|failed
    error_message: Mapped[Optional[str]] = mapped_column(Text)
    error_code: Mapped[Optional[str]] = mapped_column(String(64))
    # Structured error classification, e.g. CRAWL_ALREADY_RUNNING,
    # DOWNLOAD_FINALIZE_FAILED -- see api/routes/scrape.py for the mapping.
    retryable: Mapped[bool] = mapped_column(Boolean, default=True)
    batch_id: Mapped[Optional[int]] = mapped_column(ForeignKey("spec_crawl_batches.id"), index=True)
    # v0.3 Part C: set only when THIS job was created/owns by a batch
    # operation (see SpecCrawlBatch below). A job started individually
    # (single-series Quick Start) has batch_id=None -- the existing
    # single-series flow is completely unaffected by batches existing.
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)


class SpecCrawlBatch(Base):
    """A multi-series corpus download operation (v0.3 Part C): one row per
    "download these N series together" request, with each series' actual
    crawl still tracked by its own SpecCrawlJob row (SpecCrawlJob.batch_id
    points back here for jobs the batch itself created).

    `child_job_ids` is the authoritative list of which job IDs make up
    this batch's series selection -- stored as JSON rather than relying
    solely on the reverse `batch_id` FK, because a series that already had
    an active job when the batch started is ATTACHED (referenced) rather
    than owned: that job might belong to a different batch (or no batch)
    and this batch must never "steal" control of it, only observe its
    progress as part of this batch's aggregate view. See
    api/routes/scrape.py's _start_or_attach_series_job.
    """
    __tablename__ = "spec_crawl_batches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    series_requested: Mapped[str] = mapped_column(Text)  # JSON list[str]
    child_job_ids: Mapped[str] = mapped_column(Text, default="[]")  # JSON list[int], ordered
    max_concurrent: Mapped[int] = mapped_column(Integer, default=2)
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    # queued|running|completed|completed_with_errors|failed|cancelled
    error_message: Mapped[Optional[str]] = mapped_column(Text)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)


class SpecCrawlFile(Base):
    """Per-file crawl state (v0.3) -- one row per specification file
    discovered under a SpecCrawlJob's series, so the frontend can show a
    real file-level progress list (see api/routes/scrape.py's
    GET /jobs/{id}/files) instead of only the job's aggregate counters.

    Written via a best-effort callback from ingestion/gpp_crawl.py's
    _download_file (see on_file_event) -- if a file-event write fails for
    any reason, the crawl itself must not fail; the aggregate counters on
    SpecCrawlJob remain the source of truth for job-level status either
    way, this table is purely an additional observability layer.
    """
    __tablename__ = "spec_crawl_files"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("spec_crawl_jobs.id"), index=True)
    series: Mapped[str] = mapped_column(String(32), index=True)
    filename: Mapped[str] = mapped_column(String(512))
    relative_path: Mapped[str] = mapped_column(String(1024))
    url: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    # queued|downloading|downloaded|skipped|failed|indexed
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    bytes_downloaded: Mapped[Optional[int]] = mapped_column(Integer)
    file_size: Mapped[Optional[int]] = mapped_column(Integer)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    error_code: Mapped[Optional[str]] = mapped_column(String(64))
    error_message: Mapped[Optional[str]] = mapped_column(Text)
    local_path: Mapped[Optional[str]] = mapped_column(String(1024))
    checksum: Mapped[Optional[str]] = mapped_column(String(128))
    # 3GPP's archive doesn't expose a checksum/hash for its files, so this
    # stays "unavailable" rather than a fabricated value -- see
    # ingestion/gpp_crawl.py's on_file_event. If a future source does
    # expose one, this column is ready to hold it.
    indexed: Mapped[bool] = mapped_column(Boolean, default=False)
    chunks_indexed: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


_SPEC_CRAWL_FILE_INDEX_DDL: tuple[str, ...] = (
    "CREATE INDEX IF NOT EXISTS ix_spec_crawl_files_job_status ON spec_crawl_files (job_id, status)",
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_spec_crawl_files_job_relpath ON spec_crawl_files (job_id, relative_path)",
)

_SPEC_CRAWL_BATCH_INDEX_DDL: tuple[str, ...] = (
    "CREATE INDEX IF NOT EXISTS ix_spec_crawl_jobs_batch_id ON spec_crawl_jobs (batch_id)",
)


# v0.3 concurrency fix: a partial UNIQUE index that makes it impossible,
# at the database level, for more than one ACTIVE (pending/crawling/
# indexing) spec_crawl_jobs row to exist for the same series -- regardless
# of how many processes/workers the backend runs, and regardless of any
# race between two nearly-simultaneous POST /api/scrape/start requests.
# api/routes/scrape.py also does a check-then-create with an in-process
# asyncio.Lock() per series as a fast, friendly first line of defense (so
# the normal case returns a clean "already running" response instead of a
# raw DB error) -- but this index is what actually GUARANTEES correctness
# even if two requests land in the same instant, because SQLite enforces
# uniqueness constraints atomically at commit time. Completed/failed jobs
# are excluded from the constraint (via the WHERE clause) so job history
# and retries are unaffected.
_SPEC_CRAWL_JOB_DEDUP_DDL: tuple[str, ...] = (
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_spec_crawl_jobs_active_series "
    "ON spec_crawl_jobs (series) WHERE status IN ('pending', 'crawling', 'indexing')",
)


class SepBulkRun(Base):
    """One uploaded ETSI bulk-declaration-export workbook (see backend/etsi_bulk/).

    Kept separate from `Declaration`/`Patent` above (the single-declaration
    PDF/CSV pipeline's tables) because the bulk-export shape is one row per
    patent-family-member-per-standard rather than one row per disclosure
    within a single declarant's filing — forcing it into the same tables
    would mean a fake 1:1 "declaration" per spreadsheet row.
    """
    __tablename__ = "sep_bulk_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    filename: Mapped[str] = mapped_column(String(512))
    sheet_name: Mapped[Optional[str]] = mapped_column(String(256))
    total_rows: Mapped[int] = mapped_column(Integer, default=0)
    embedder_mode: Mapped[Optional[str]] = mapped_column(String(64))
    meta_json: Mapped[Optional[str]] = mapped_column(Text)  # calibration info, warnings, disclaimer
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)

    # v0.5 large-dataset architecture: scoring/persistence now runs as a
    # background task (see api/routes/gitas/dashboard.py) instead of inline
    # in the upload request, so the frontend can poll progress on a run
    # that's still processing instead of the request itself blocking (and
    # potentially timing out) until all rows are done. See GET
    # /api/sep/runs/{run_id}/status.
    status: Mapped[str] = mapped_column(String(16), default="processing", index=True)
    # "processing" | "completed" | "completed_with_errors" | "failed"
    parsed_rows: Mapped[int] = mapped_column(Integer, default=0)      # rows the parser accepted (known up front)
    processed_rows: Mapped[int] = mapped_column(Integer, default=0)   # rows scored+persisted so far (success or failed)
    failed_rows: Mapped[int] = mapped_column(Integer, default=0)      # of processed_rows, how many failed scoring
    error_message: Mapped[Optional[str]] = mapped_column(Text)        # set only on status == "failed"
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime)

    # Verbatim bytes of the uploaded workbook, kept only so the Excel export
    # feature (etsi_bulk/excel_export.py) can reproduce the original Sheet1
    # columns/ordering and any existing Summary sheet exactly, without
    # reconstructing them from the (narrower) scored-column set stored on
    # SepBulkPatent below. Not read by scoring — export-only. May be NULL for
    # runs uploaded before this column existed, or for .csv uploads (which
    # have no "workbook" to preserve); the export endpoint degrades to a
    # reconstructed table in that case.
    original_file_blob: Mapped[Optional[bytes]] = mapped_column(LargeBinary)

    patents: Mapped[List["SepBulkPatent"]] = relationship(
        "SepBulkPatent", back_populates="run", cascade="all, delete-orphan"
    )


class SepBulkPatent(Base):
    """One scored row from a bulk-export run — see `etsi_bulk/scoring.py`."""
    __tablename__ = "sep_bulk_patents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("sep_bulk_runs.id"), index=True)

    iprd_reference: Mapped[Optional[str]] = mapped_column(String(128))
    signature_date: Mapped[Optional[str]] = mapped_column(String(64))
    company: Mapped[Optional[str]] = mapped_column(String(512), index=True)

    disclosure_number: Mapped[Optional[str]] = mapped_column(String(64))
    family_id: Mapped[Optional[str]] = mapped_column(String(64), index=True)

    # Indexed (v0.7.2): both are queried heavily -- standard_raw backs the
    # dashboard's standard filter and spec_number backs
    # the "3GPP Specification Context" panel's per-spec aggregate query
    # (_spec_context) -- at 50k+ rows an unindexed scan of either becomes
    # the dominant cost on upload and on every patent-detail page view.
    standard_raw: Mapped[Optional[str]] = mapped_column(String(512), index=True)
    spec_number: Mapped[Optional[str]] = mapped_column(String(32), index=True)
    spec_version: Mapped[Optional[str]] = mapped_column(String(32))
    release: Mapped[Optional[str]] = mapped_column(String(32), index=True)
    technology: Mapped[Optional[str]] = mapped_column(String(32), index=True)
    domain: Mapped[Optional[str]] = mapped_column(String(64), index=True)

    patent_type: Mapped[Optional[str]] = mapped_column(String(64))
    application_number: Mapped[Optional[str]] = mapped_column(String(128), index=True)
    publication_number: Mapped[Optional[str]] = mapped_column(String(256))
    title: Mapped[Optional[str]] = mapped_column(Text)
    illustrative_part: Mapped[Optional[str]] = mapped_column(Text)
    explicit_disclosure: Mapped[Optional[bool]] = mapped_column(Boolean)

    country_raw: Mapped[Optional[str]] = mapped_column(String(128))
    country_code: Mapped[Optional[str]] = mapped_column(String(8), index=True)
    filing_date: Mapped[Optional[str]] = mapped_column(String(64))
    legal_status: Mapped[Optional[str]] = mapped_column(String(64))

    # v0.3: fields present in the "UIUX database" bulk-export variant that
    # were mapped by etsi_bulk/schema.py's COLUMN_MAP but not yet
    # persisted -- added so every original column survives into the DB
    # (see PART 2 "preserve every original column" requirement) and so
    # dipg_id can serve as the canonical cross-module record identity
    # (GITAS -> C-SARAS -> cPARAS Text Matching).
    dipg_id: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    patt_id: Mapped[Optional[str]] = mapped_column(String(64))
    project_acronym: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    # ETPR_ACRONYM in the source file -- the 3GPP working item/project this
    # patent's essentiality declaration is tied to (distinct from
    # `standard_raw`, which is the specific TS/TR document+version).
    normalized_patent: Mapped[Optional[str]] = mapped_column(String(32))
    # NOTE: despite the name, this field is a Yes/No/Error FLAG in every
    # real export observed so far, not patent claim/disclosure text --
    # see api/routes/sep.py's text-matching-source-text selection, which
    # deliberately does NOT use this field as free text.
    ess_to_project: Mapped[Optional[str]] = mapped_column(String(16))
    ess_to_standard: Mapped[Optional[str]] = mapped_column(String(16))
    standard_url: Mapped[Optional[str]] = mapped_column(Text)
    publication_url: Mapped[Optional[str]] = mapped_column(Text)
    original_application_number: Mapped[Optional[str]] = mapped_column(String(128))
    original_publication_number: Mapped[Optional[str]] = mapped_column(String(256))
    natural_key: Mapped[Optional[str]] = mapped_column(String(512), index=True)
    # DEPRECATED / no longer populated. Previously an identifier-derived
    # dedup key used to collapse rows that shared an application/
    # publication number; source rows are no longer deduplicated (every
    # uploaded row is stored as its own record), so this column is kept
    # only so existing databases keep working. It carries no uniqueness
    # constraint and nothing reads it.
    source_row_index: Mapped[Optional[int]] = mapped_column(Integer, index=True)
    # 0-based position of this record's row among the uploaded sheet's
    # data rows (see etsi_bulk/parser.py's `_source_row_index`). Together
    # with run_id this is the record's source identity: one record per
    # source row, in source order, never merged by any identifier. The
    # enriched Excel export uses it to write each record's enrichment back
    # onto exactly the row it came from.
    patent_text: Mapped[Optional[str]] = mapped_column(Text)
    # Reserved for an actual claim/disclosure text column in a future
    # dataset. Deliberately NEVER populated from Title or Normalized_Patent
    # (the current "UIUX database" export has no real claim text -- see
    # etsi_bulk/schema.py's Normalized_Patent note). Left NULL until a
    # workbook that genuinely has one is imported, so cPARAS Text Matching
    # can use it directly once available without a schema change.

    gen_2g: Mapped[Optional[bool]] = mapped_column(Boolean, default=False)
    gen_3g: Mapped[Optional[bool]] = mapped_column(Boolean, default=False)
    gen_4g: Mapped[Optional[bool]] = mapped_column(Boolean, default=False)
    gen_5g: Mapped[Optional[bool]] = mapped_column(Boolean, default=False)
    # v0.7: 6G is now a real declared generation in some exports (including
    # datasets exported from the separate Initial Categoriser application)
    # -- added so those uploads don't silently lose 6G declarations. New installs get this from
    # create_all; existing sqlite databases get it via
    # _migrate_sqlite_sep_bulk_patent_columns below.
    gen_6g: Mapped[Optional[bool]] = mapped_column(Boolean, default=False)

    essentiality: Mapped[Optional[str]] = mapped_column(String(32), index=True)  # Essential/Potential/Non-Essential
    confidence: Mapped[Optional[float]] = mapped_column(Float)
    claim_match: Mapped[Optional[float]] = mapped_column(Float)
    semantic_similarity: Mapped[Optional[float]] = mapped_column(Float)
    standard_coverage: Mapped[Optional[float]] = mapped_column(Float)

    matched_standard: Mapped[Optional[str]] = mapped_column(String(128))
    matched_clause: Mapped[Optional[str]] = mapped_column(String(64))
    matched_clause_title: Mapped[Optional[str]] = mapped_column(String(256))
    matched_domain: Mapped[Optional[str]] = mapped_column(String(64))
    declared_family_match: Mapped[Optional[bool]] = mapped_column(Boolean)
    matched_key_terms_json: Mapped[Optional[str]] = mapped_column(Text)  # JSON list, for UI term-highlighting
    matched_key_terms_count: Mapped[Optional[int]] = mapped_column(Integer)
    total_key_terms: Mapped[Optional[int]] = mapped_column(Integer)

    # v0.5 fault-tolerant large-dataset scoring: per-row outcome, so one
    # malformed row's failure is visible on that row alone rather than
    # aborting or being indistinguishable from a genuine "Non-Essential"
    # result. "OK" for every row scored before this column existed.
    row_status: Mapped[str] = mapped_column(String(16), default="OK", index=True)  # "OK" | "FAILED"
    row_error: Mapped[Optional[str]] = mapped_column(Text)  # failure reason, only set when row_status == "FAILED"

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)

    run: Mapped["SepBulkRun"] = relationship("SepBulkRun", back_populates="patents")


_SEP_BULK_INDEX_DDL: tuple[str, ...] = (
    "CREATE INDEX IF NOT EXISTS ix_sep_bulk_patents_run_id ON sep_bulk_patents (run_id)",
    "CREATE INDEX IF NOT EXISTS ix_sep_bulk_patents_company ON sep_bulk_patents (company)",
    "CREATE INDEX IF NOT EXISTS ix_sep_bulk_patents_essentiality ON sep_bulk_patents (essentiality)",
    # v0.7.2 scale-hardening: standard_raw/spec_number are now index=True on
    # the model (so create_all() adds them for new DBs), but an existing
    # sqlite file created before this change won't get a new column's index
    # from create_all() alone -- these two entries make sure a pre-existing
    # DB (migrated in-place via _migrate_sqlite_sep_bulk_patent_columns)
    # still ends up indexed on both.
    "CREATE INDEX IF NOT EXISTS ix_sep_bulk_patents_standard_raw ON sep_bulk_patents (standard_raw)",
    "CREATE INDEX IF NOT EXISTS ix_sep_bulk_patents_spec_number ON sep_bulk_patents (spec_number)",
    # Internal record identity for source-row preservation: at most one
    # stored record per (run, source row). This is a processing safeguard
    # (a resumed run can never store the same source row twice) -- it is
    # deliberately NOT keyed on any patent/application/publication number,
    # so repeated identifiers across different source rows are all kept.
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_sep_bulk_patents_run_source_row ON sep_bulk_patents (run_id, source_row_index)",
)


# ═══════════════════════════════════════════════════════════════════════════
# v0.3 — Shared ETSI Standards Corpus (backend/etsi_corpus/)
# ═══════════════════════════════════════════════════════════════════════════
#
# EtsiDocument is ONE ROW PER MANIFEST ENTRY (one ETSI_Dir/PDF_URL/Standard
# Name row from ETSI_Series_With_PDF_URLs.xlsx) -- deliberately kept
# separate from KnowledgeChunk (which stores the resulting RAG chunks) and
# from SepBulkPatent/Patent (which are DECLARATION data, not the standards'
# own content -- see PART 36 "important distinction" in the v0.3 spec).
# EtsiDocument is the checkpoint/status row that makes ingestion resumable:
# every stage (downloaded / extracted / llm_extracted / indexed) is a
# column here so a restarted ingestion run can pick up exactly where it
# left off instead of re-downloading or re-extracting anything already
# done -- see etsi_corpus/pipeline.py.

class EtsiDocument(Base):
    """One row from the ETSI manifest (ETSI_Series_With_PDF_URLs.xlsx),
    tracked through download -> extraction -> structured LLM extraction ->
    embedding/indexing. This is the shared Standards Corpus GITAS,
    cPARAS, and C-SARAS all read from -- see etsi_corpus/pipeline.py.
    """
    __tablename__ = "etsi_documents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # ── manifest / identity (from ETSI_Series_With_PDF_URLs.xlsx) ──
    etsi_dir: Mapped[Optional[str]] = mapped_column(String(512))
    pdf_url: Mapped[str] = mapped_column(Text, index=True)
    url_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    # sha256(pdf_url) -- stable dedup/idempotency key across re-imports of
    # the same manifest (or a manifest with duplicate rows), independent of
    # row order. See etsi_corpus/manifest.py's dedup step.
    standard_name_raw: Mapped[Optional[str]] = mapped_column(String(512))   # "TS 100 220-1 V1.1.1" as given

    # ── parsed identity ──
    document_type: Mapped[Optional[str]] = mapped_column(String(16))       # TS | TR | EN | ES | Unknown
    series: Mapped[Optional[str]] = mapped_column(String(16), index=True)  # "100 220-1"
    specification_number: Mapped[Optional[str]] = mapped_column(String(32), index=True)
    version: Mapped[Optional[str]] = mapped_column(String(32))             # "1.1.1"
    release: Mapped[Optional[str]] = mapped_column(String(16), index=True)
    title: Mapped[Optional[str]] = mapped_column(Text)
    publication_date: Mapped[Optional[str]] = mapped_column(String(64))

    # ── classification ──
    technology_domain: Mapped[Optional[str]] = mapped_column(String(128), index=True)
    subdomain: Mapped[Optional[str]] = mapped_column(String(128))
    topics_json: Mapped[Optional[str]] = mapped_column(Text)          # JSON list[str]
    keywords_json: Mapped[Optional[str]] = mapped_column(Text)        # JSON list[str]
    protocols_json: Mapped[Optional[str]] = mapped_column(Text)       # JSON list[str]
    interfaces_json: Mapped[Optional[str]] = mapped_column(Text)      # JSON list[str]
    network_components_json: Mapped[Optional[str]] = mapped_column(Text)  # JSON list[str]

    # ── technical content ──
    purpose: Mapped[Optional[str]] = mapped_column(Text)
    scope: Mapped[Optional[str]] = mapped_column(Text)
    executive_summary: Mapped[Optional[str]] = mapped_column(Text)
    technical_summary: Mapped[Optional[str]] = mapped_column(Text)
    key_features_json: Mapped[Optional[str]] = mapped_column(Text)          # JSON list[str]
    major_procedures_json: Mapped[Optional[str]] = mapped_column(Text)      # JSON list[str]
    important_mechanisms_json: Mapped[Optional[str]] = mapped_column(Text)  # JSON list[str]
    important_parameters_json: Mapped[Optional[str]] = mapped_column(Text)  # JSON list[str]
    normative_requirements_json: Mapped[Optional[str]] = mapped_column(Text)  # JSON list[str]
    clauses_json: Mapped[Optional[str]] = mapped_column(Text)             # JSON list[{number,title,summary}]

    # ── patent relevance (technical correspondence only -- never a legal
    #    essentiality opinion, see etsi_corpus/models.py docstring) ──
    patent_relevant_concepts_json: Mapped[Optional[str]] = mapped_column(Text)  # JSON list[str]
    potentially_essential_features_json: Mapped[Optional[str]] = mapped_column(Text)  # JSON list[str]
    distinctive_mechanisms_json: Mapped[Optional[str]] = mapped_column(Text)   # JSON list[str]
    technical_terms_json: Mapped[Optional[str]] = mapped_column(Text)         # JSON list[str]

    # ── provenance / anti-hallucination ──
    extraction_method: Mapped[Optional[str]] = mapped_column(String(64))   # "pdf_text" | "ocr_tesseract" | ...
    extraction_confidence: Mapped[Optional[float]] = mapped_column(Float)
    source_pages_json: Mapped[Optional[str]] = mapped_column(Text)         # JSON list[int]
    llm_validation_status: Mapped[Optional[str]] = mapped_column(String(16))  # valid | repaired | incomplete
    raw_extracted_text_path: Mapped[Optional[str]] = mapped_column(String(1024))
    structured_path: Mapped[Optional[str]] = mapped_column(String(1024))
    # Points at the scraper-side structured-extraction JSON for this
    # document (etsi-scraper-pipeline's output/structured/<id>.json), when
    # the imported record's provenance included one -- kept as a reference
    # path, same "full content stays on disk, never inline in a DB row"
    # rule as raw_extracted_text_path above.
    # Full extracted text is stored on disk (see ETSI_DATASET_OUTPUT/text/),
    # never inline in this row or in the enriched Excel -- item 8 of the
    # v0.3 spec explicitly forbids huge raw text in spreadsheet cells.

    # ── pipeline / status (what makes ingestion resumable) ──
    status: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    # pending|downloading|downloaded|download_failed|extracting|extracted|
    # extraction_failed|llm_extracting|llm_extracted|llm_extraction_failed|
    # indexing|completed|failed
    file_hash: Mapped[Optional[str]] = mapped_column(String(64))           # sha256 of downloaded bytes
    local_pdf_path: Mapped[Optional[str]] = mapped_column(String(1024))
    file_size_bytes: Mapped[Optional[int]] = mapped_column(Integer)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[Optional[str]] = mapped_column(Text)
    vector_id: Mapped[Optional[str]] = mapped_column(String(128))
    chunks_indexed: Mapped[int] = mapped_column(Integer, default=0)
    # v0.4.4.1 indexing diagnostics (what an import report needs to say WHY a document is not indexed):
    index_attempts: Mapped[Optional[int]] = mapped_column(Integer, default=0)          # indexing attempts, all runs
    last_error_type: Mapped[Optional[str]] = mapped_column(String(48))         # e.g. out_of_memory
    last_error_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    text_chars: Mapped[Optional[int]] = mapped_column(Integer)                 # size of the indexed text
    ingestion_job_id: Mapped[Optional[int]] = mapped_column(ForeignKey("etsi_ingestion_jobs.id"), index=True)

    # ── per-stage timestamps, straight from the scraper's provenance
    #    (schema_version >= "1.1") -- when the imported record predates
    #    these fields (older scraper output, schema_version "1.0"), they
    #    simply stay NULL rather than being backfilled with a guess. Not
    #    the same thing as created_at/updated_at below, which are THIS
    #    ROW's own bookkeeping timestamps, not the source document's. ──
    downloaded_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    extracted_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)


class EtsiIngestionJob(Base):
    """One manifest-driven ingestion run (checkpointed, resumable). Distinct
    from the older SpecCrawlJob (3GPP-archive-crawl, per-series) -- this is
    manifest-row-driven and source-agnostic to any single series."""
    __tablename__ = "etsi_ingestion_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    manifest_path: Mapped[Optional[str]] = mapped_column(String(1024))
    manifest_row_count: Mapped[Optional[int]] = mapped_column(Integer)
    limit_requested: Mapped[Optional[int]] = mapped_column(Integer)
    # None => whole manifest (dangerous at 50k+ rows; CLI/API both require
    # an explicit override to run without a limit, see etsi_corpus/cli.py).
    status: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    # pending|running|paused|completed|completed_with_errors|failed|cancelled
    total_documents: Mapped[int] = mapped_column(Integer, default=0)
    documents_downloaded: Mapped[int] = mapped_column(Integer, default=0)
    documents_extracted: Mapped[int] = mapped_column(Integer, default=0)
    documents_llm_processed: Mapped[int] = mapped_column(Integer, default=0)
    documents_indexed: Mapped[int] = mapped_column(Integer, default=0)
    documents_failed: Mapped[int] = mapped_column(Integer, default=0)
    current_operation: Mapped[Optional[str]] = mapped_column(String(32))
    error_message: Mapped[Optional[str]] = mapped_column(Text)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)


_ETSI_DOCUMENT_INDEX_DDL: tuple[str, ...] = (
    "CREATE INDEX IF NOT EXISTS ix_etsi_documents_status ON etsi_documents (status)",
    "CREATE INDEX IF NOT EXISTS ix_etsi_documents_series ON etsi_documents (series)",
    "CREATE INDEX IF NOT EXISTS ix_etsi_documents_release ON etsi_documents (release)",
    "CREATE INDEX IF NOT EXISTS ix_etsi_documents_domain ON etsi_documents (technology_domain)",
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_etsi_documents_url_hash ON etsi_documents (url_hash)",
)


# ── Patents analysed through C-SARAS's patent-claim workflow (v0.3) ────────
# Distinct from `Patent`/`SepBulkPatent` above (which come from ETSI IPR
# DECLARATIONS) -- a PatentAnalysis is created when a patent PDF or a pasted
# abstract+claims is submitted directly to C-SARAS for claim-to-standard
# analysis, and may or may not correspond to any declared patent.

class PatentAnalysis(Base):
    """One patent submitted to C-SARAS's claim-to-standard workflow."""
    __tablename__ = "patent_analyses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[Optional[str]] = mapped_column(Text)
    patent_number: Mapped[Optional[str]] = mapped_column(String(128))
    application_number: Mapped[Optional[str]] = mapped_column(String(128))
    publication_number: Mapped[Optional[str]] = mapped_column(String(128))
    abstract: Mapped[Optional[str]] = mapped_column(Text)
    source_filename: Mapped[Optional[str]] = mapped_column(String(512))
    input_method: Mapped[Optional[str]] = mapped_column(String(24))  # "pdf" | "abstract_claims_text"

    classification_release: Mapped[Optional[str]] = mapped_column(String(16))
    classification_series: Mapped[Optional[str]] = mapped_column(String(16))
    classification_domain: Mapped[Optional[str]] = mapped_column(String(128))
    classification_topic: Mapped[Optional[str]] = mapped_column(String(256))

    # v0.3 fix (audit item C-SARAS #1): the FULL ranked candidate list from
    # classify_standards(), not just the top-1 candidate flattened into the
    # four columns above. Those columns stay (as a fast/simple "primary
    # classification" summary for list views) but are now derived FROM this
    # list rather than being the only place it's recorded.
    ranked_standards_json: Mapped[Optional[str]] = mapped_column(Text)  # JSON list[StandardCandidate]
    knowledge_base_version: Mapped[Optional[str]] = mapped_column(String(32))
    # Snapshot of etsi_corpus.package's dataset_version at analysis time
    # (item 14: corpus versioning) -- "Unknown" if no package was
    # installed when this ran, never blank.

    status: Mapped[str] = mapped_column(String(24), default="queued", index=True)
    # queued|extracting_patent|parsing_claims|extracting_concepts|
    # retrieving_standards|classifying|mapping_clauses|completed|failed
    error_message: Mapped[Optional[str]] = mapped_column(Text)

    # Link back into cPARAS/GITAS when this patent corresponds to an
    # already-declared one (see PatentDetailPage's "Analyse with C-SARAS").
    declaration_patent_id: Mapped[Optional[int]] = mapped_column(ForeignKey("patents.id"))
    sep_bulk_patent_id: Mapped[Optional[int]] = mapped_column(ForeignKey("sep_bulk_patents.id"))

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)

    claims: Mapped[List["PatentClaim"]] = relationship("PatentClaim", back_populates="analysis", cascade="all, delete-orphan")
    matches: Mapped[List["ClaimStandardMatch"]] = relationship("ClaimStandardMatch", back_populates="analysis", cascade="all, delete-orphan")


class PatentClaim(Base):
    """One parsed claim from a PatentAnalysis (item 14 of the v0.3 spec)."""
    __tablename__ = "patent_claims"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    analysis_id: Mapped[int] = mapped_column(ForeignKey("patent_analyses.id"), index=True)
    claim_number: Mapped[str] = mapped_column(String(16))
    claim_type: Mapped[str] = mapped_column(String(16))          # "independent" | "dependent"
    depends_on: Mapped[Optional[str]] = mapped_column(String(16))  # claim_number this depends on, if dependent
    claim_text: Mapped[str] = mapped_column(Text)
    technical_elements_json: Mapped[Optional[str]] = mapped_column(Text)  # JSON list[str]
    entities_json: Mapped[Optional[str]] = mapped_column(Text)
    actions_json: Mapped[Optional[str]] = mapped_column(Text)
    parameters_json: Mapped[Optional[str]] = mapped_column(Text)
    protocols_json: Mapped[Optional[str]] = mapped_column(Text)
    mechanisms_json: Mapped[Optional[str]] = mapped_column(Text)
    limitations_json: Mapped[Optional[str]] = mapped_column(Text)
    distinctive_concepts_json: Mapped[Optional[str]] = mapped_column(Text)

    analysis: Mapped["PatentAnalysis"] = relationship("PatentAnalysis", back_populates="claims")


class ClaimStandardMatch(Base):
    """One claim-to-standard mapping result (item 16 of the v0.3 spec) --
    a first-class, evidence-carrying data structure, never a bare score."""
    __tablename__ = "claim_standard_matches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    analysis_id: Mapped[int] = mapped_column(ForeignKey("patent_analyses.id"), index=True)
    claim_id: Mapped[int] = mapped_column(ForeignKey("patent_claims.id"), index=True)
    claim_number: Mapped[str] = mapped_column(String(16))

    etsi_document_id: Mapped[Optional[int]] = mapped_column(ForeignKey("etsi_documents.id"))
    standard_name: Mapped[Optional[str]] = mapped_column(String(256))
    specification_number: Mapped[Optional[str]] = mapped_column(String(32))
    version: Mapped[Optional[str]] = mapped_column(String(32))
    release: Mapped[Optional[str]] = mapped_column(String(16))
    clause: Mapped[Optional[str]] = mapped_column(String(64))
    clause_title: Mapped[Optional[str]] = mapped_column(String(256))
    page: Mapped[Optional[int]] = mapped_column(Integer)

    claim_elements_json: Mapped[Optional[str]] = mapped_column(Text)     # JSON list[str]
    standard_elements_json: Mapped[Optional[str]] = mapped_column(Text)  # JSON list[str]
    matching_concepts_json: Mapped[Optional[str]] = mapped_column(Text)  # JSON list[str]
    match_relationship: Mapped[str] = mapped_column(String(16))
    # Named match_relationship (not `relationship`) to avoid shadowing
    # SQLAlchemy's own `relationship()` function within this class body --
    # a plain `relationship: Mapped[str] = mapped_column(...)` column would
    # rebind that name in the class namespace before the `analysis =
    # relationship("PatentAnalysis", ...)` line below runs, breaking it.
    # explicit | strong | partial | weak | no_match -- see
    # etsi_corpus/models.py ClaimRelationship for the exact thresholds.
    similarity_score: Mapped[float] = mapped_column(Float, default=0.0)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    evidence: Mapped[Optional[str]] = mapped_column(Text)             # verbatim-adjacent excerpt, or "No evidence found"
    source_pages_json: Mapped[Optional[str]] = mapped_column(Text)    # JSON list[int]

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    analysis: Mapped["PatentAnalysis"] = relationship("PatentAnalysis", back_populates="matches")


_PATENT_ANALYSIS_INDEX_DDL: tuple[str, ...] = (
    "CREATE INDEX IF NOT EXISTS ix_patent_claims_analysis_id ON patent_claims (analysis_id)",
    "CREATE INDEX IF NOT EXISTS ix_claim_matches_analysis_id ON claim_standard_matches (analysis_id)",
    "CREATE INDEX IF NOT EXISTS ix_claim_matches_claim_id ON claim_standard_matches (claim_id)",
)


# ── Authentication & RBAC (v0.5.0) ───────────────────────────────────────────
# Accounts, login sessions and an authentication audit trail. Business logic
# lives in auth/service.py; roles/permissions in auth/roles.py. These tables are
# created by `init_db()` (create_all) on first start of v0.5.0, so an existing
# database simply gains them -- no other table is touched.

class User(Base):
    """One CPARAS account. `role` is one of auth.roles.Role ("guest",
    "data_entry", "super_admin"); `username` is stored lower-cased and is
    unique. `password_hash` is an scrypt hash (auth/passwords.py) -- the plain
    password is never stored."""
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    display_name: Mapped[Optional[str]] = mapped_column(String(128))
    role: Mapped[str] = mapped_column(String(32), index=True)
    password_hash: Mapped[str] = mapped_column(String(256))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Set when an administrator created/reset the password: the UI asks the
    # user to choose their own password after signing in.
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    last_login_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    created_by: Mapped[Optional[str]] = mapped_column(String(64))  # username, "cli" or "first-run-setup"


class UserSession(Base):
    """A signed-in browser/API client. Only the SHA-256 of the random session
    token is stored, so a copy of the database cannot be used to hijack a
    session. A session ends at `expires_at` (absolute), after
    AUTH_SESSION_IDLE_MINUTES without use, on logout, or when an administrator
    changes the user's role/password/active flag (`revoked_at`)."""
    __tablename__ = "user_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    client_ip: Mapped[Optional[str]] = mapped_column(String(64))
    user_agent: Mapped[Optional[str]] = mapped_column(String(256))


class AuthEvent(Base):
    """Append-only audit trail of authentication and user-administration
    events (login success/failure, logout, user created/changed/deleted,
    password changed, first-run setup). Shown to super admins in the UI."""
    __tablename__ = "auth_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)
    event: Mapped[str] = mapped_column(String(48), index=True)
    username: Mapped[Optional[str]] = mapped_column(String(64))   # the account affected
    actor: Mapped[Optional[str]] = mapped_column(String(64))      # who did it (same as username for self-service)
    client_ip: Mapped[Optional[str]] = mapped_column(String(64))
    detail: Mapped[Optional[str]] = mapped_column(Text)


# ── Session dependency ─────────────────────────────────────────────────────────

async def get_db():
    """FastAPI dependency: one AsyncSession per request, closed afterwards. Handlers commit explicitly;
    nothing is committed implicitly.
    """
    async with AsyncSessionLocal() as session:
        yield session


# Columns added to `patents` after the initial 2.0.0 release, for the
# research/paper-style augmentation. `create_all` only creates *new* tables,
# so existing SQLite databases need these added via ALTER TABLE.
_NEW_PATENT_COLUMNS: dict[str, str] = {
    "predicted_sep_status": "VARCHAR(64)",
    "research_confidence": "FLOAT",
    "recommended_standard": "VARCHAR(64)",
    "recommended_version": "VARCHAR(32)",
    "standard_clause": "VARCHAR(64)",
    "standard_similarity": "FLOAT",
    "similar_patent_ref": "VARCHAR(128)",
    "similar_patent_score": "FLOAT",
    "match_explanation": "TEXT",
    "verification_status": "VARCHAR(64)",
    "version_evolution": "TEXT",
    "is_potential_sep": "BOOLEAN",
    "potential_sep_detail": "TEXT",
    # v0.3 essentiality-evidence-provenance columns (see Patent docstring):
    "evidence_source": "VARCHAR(32)",
    "evidence_text": "TEXT",
    "knowledge_base_version": "VARCHAR(32)",
    "evidence_retrieved_at": "DATETIME",
    # v0.5: CPARAS three-signal mandatory fields (see research/
    # three_signal_scoring.py) -- standard_similarity above already served
    # as semantic_similarity; these two were genuinely missing.
    "claim_match": "FLOAT",
    "standard_coverage": "FLOAT",
    # v0.4.9: exact matched phrases / terms (research/phrase_match.py)
    "matched_phrases": "TEXT",
    "matched_terms": "TEXT",
}


async def _migrate_sqlite_research_columns(conn) -> None:
    """Best-effort ALTER TABLE for SQLite so older databases pick up the new
    research columns on `patents` without a full migration tool."""
    if engine.dialect.name != "sqlite":
        return
    try:
        result = await conn.execute(text("PRAGMA table_info(patents)"))
        existing = {row[1] for row in result.fetchall()}
        for col, coltype in _NEW_PATENT_COLUMNS.items():
            if col not in existing:
                await conn.execute(text(f"ALTER TABLE patents ADD COLUMN {col} {coltype}"))
                logger.info(f"Migrated patents table: added column {col} ({coltype})")
    except Exception as e:
        logger.warning(f"Could not auto-migrate 'patents' table for research columns: {e}")


# Columns added to `sep_bulk_runs` after the initial GITAS bulk-dashboard
# release, for the Excel export feature. Same rationale/mechanism as
# _NEW_PATENT_COLUMNS/_migrate_sqlite_research_columns above.
_NEW_SEP_BULK_RUN_COLUMNS: dict[str, str] = {
    "original_file_blob": "BLOB",
    # v0.5 large-dataset architecture (background processing + progress):
    "status": "VARCHAR(16) DEFAULT 'completed'",
    # ^ existing runs predate the background-job model and finished
    # synchronously, so they default to "completed" rather than being
    # stuck showing "processing" forever.
    "parsed_rows": "INTEGER DEFAULT 0",
    "processed_rows": "INTEGER DEFAULT 0",
    "failed_rows": "INTEGER DEFAULT 0",
    "error_message": "TEXT",
    "started_at": "DATETIME",
    "completed_at": "DATETIME",
}



async def _migrate_sqlite_sep_bulk_columns(conn) -> None:
    """Best-effort ALTER TABLE for SQLite so older databases pick up new
    sep_bulk_runs columns (currently just original_file_blob, for Excel
    export) without a full migration tool."""
    if engine.dialect.name != "sqlite":
        return
    try:
        result = await conn.execute(text("PRAGMA table_info(sep_bulk_runs)"))
        existing = {row[1] for row in result.fetchall()}
        for col, coltype in _NEW_SEP_BULK_RUN_COLUMNS.items():
            if col not in existing:
                await conn.execute(text(f"ALTER TABLE sep_bulk_runs ADD COLUMN {col} {coltype}"))
                logger.info(f"Migrated sep_bulk_runs table: added column {col} ({coltype})")
    except Exception as e:
        logger.warning(f"Could not auto-migrate 'sep_bulk_runs' table for export columns: {e}")


# v0.3: SepBulkPatent gained several columns from the "UIUX database"
# bulk-export variant (dipg_id, patt_id, normalized_patent, ess_to_project,
# ess_to_standard, standard_url, publication_url, original_application_
# number, original_publication_number) -- present in etsi_bulk/schema.py's
# COLUMN_MAP for a while but not previously persisted. Same ALTER TABLE
# pattern as sep_bulk_runs above.
_NEW_SEP_BULK_PATENT_COLUMNS: dict[str, str] = {
    "dipg_id": "VARCHAR(64)",
    "patt_id": "VARCHAR(64)",
    "project_acronym": "VARCHAR(64)",
    "normalized_patent": "VARCHAR(32)",
    "ess_to_project": "VARCHAR(16)",
    "ess_to_standard": "VARCHAR(16)",
    "standard_url": "TEXT",
    "publication_url": "TEXT",
    "original_application_number": "VARCHAR(128)",
    "original_publication_number": "VARCHAR(256)",
    "natural_key": "VARCHAR(512)",
    # ^ deprecated, no longer populated (see SepBulkPatent.natural_key).
    "source_row_index": "INTEGER",
    # ^ per-run source row position -- see SepBulkPatent.source_row_index.
    "patent_text": "TEXT",
    # v0.5 fault-tolerant large-dataset scoring:
    "row_status": "VARCHAR(16) DEFAULT 'OK'",
    "row_error": "TEXT",
    # v0.7: 6G declared-generation flag -- see the SepBulkPatent.gen_6g
    # column comment above.
    "gen_6g": "BOOLEAN DEFAULT 0",
}


async def _migrate_sqlite_sep_bulk_patent_columns(conn) -> None:
    """SQLite only: add any `_NEW_SEP_BULK_PATENT_COLUMNS` missing from an older `sep_bulk_patents`
    table (best effort; logs instead of failing start-up).
    """
    if engine.dialect.name != "sqlite":
        return
    try:
        result = await conn.execute(text("PRAGMA table_info(sep_bulk_patents)"))
        existing = {row[1] for row in result.fetchall()}
        for col, coltype in _NEW_SEP_BULK_PATENT_COLUMNS.items():
            if col not in existing:
                await conn.execute(text(f"ALTER TABLE sep_bulk_patents ADD COLUMN {col} {coltype}"))
                logger.info(f"Migrated sep_bulk_patents table: added column {col} ({coltype})")
    except Exception as e:
        logger.warning(f"Could not auto-migrate 'sep_bulk_patents' table for v0.3 columns: {e}")


# v0.3: SpecCrawlJob gained total_files/current_operation/error_code/
# retryable for the redesigned progress UI and structured error handling.
# An install created before v0.3 has a spec_crawl_jobs table without these
# columns -- same ALTER TABLE pattern as sep_bulk_runs above.
_NEW_SPEC_CRAWL_JOB_COLUMNS: dict[str, str] = {
    "total_files": "INTEGER",
    "files_discovered": "INTEGER DEFAULT 0",
    "current_operation": "VARCHAR(32)",
    "error_code": "VARCHAR(64)",
    "retryable": "BOOLEAN DEFAULT 1",
    "batch_id": "INTEGER",
}


async def _migrate_sqlite_spec_crawl_job_columns(conn) -> None:
    """SQLite only: add any `_NEW_SPEC_CRAWL_JOB_COLUMNS` missing from an older `spec_crawl_jobs` table
    (best effort).
    """
    if engine.dialect.name != "sqlite":
        return
    try:
        result = await conn.execute(text("PRAGMA table_info(spec_crawl_jobs)"))
        existing = {row[1] for row in result.fetchall()}
        for col, coltype in _NEW_SPEC_CRAWL_JOB_COLUMNS.items():
            if col not in existing:
                await conn.execute(text(f"ALTER TABLE spec_crawl_jobs ADD COLUMN {col} {coltype}"))
                logger.info(f"Migrated spec_crawl_jobs table: added column {col} ({coltype})")
    except Exception as e:
        logger.warning(f"Could not auto-migrate 'spec_crawl_jobs' table for v0.3 progress columns: {e}")


# Indexes for databases created before these were declared on the models.
# Base.metadata.create_all(checkfirst=True) skips tables that already exist,
# and therefore never adds an index to an existing table — so an upgraded
# install would silently keep full-scanning `patents` on every similarity
# pass (see the cross-declaration scan in api/routes/analysis.py).
_INDEX_DDL: tuple[str, ...] = (
    "CREATE INDEX IF NOT EXISTS ix_patents_declaration_id ON patents (declaration_id)",
    "CREATE INDEX IF NOT EXISTS ix_patents_application_number ON patents (application_number)",
    "CREATE INDEX IF NOT EXISTS ix_portfolio_analyses_declaration_id ON portfolio_analyses (declaration_id)",
    "CREATE INDEX IF NOT EXISTS ix_research_runs_declaration_id ON research_runs (declaration_id)",
    "CREATE INDEX IF NOT EXISTS ix_declarations_created_at ON declarations (created_at)",
)


async def _create_missing_indexes(conn) -> None:
    """Best-effort index creation. Each statement is independent so one
    failure (e.g. a table that doesn't exist yet on a fresh install) cannot
    prevent the others from being applied."""
    for ddl in _INDEX_DDL:
        try:
            await conn.execute(text(ddl))
        except Exception as e:
            logger.warning(f"Could not create index ({ddl.split()[5]}): {e}")


_NEW_PATENT_ANALYSIS_COLUMNS: dict[str, str] = {
    "ranked_standards_json": "TEXT",
    "knowledge_base_version": "VARCHAR(32)",
}

_NEW_CLAIM_STANDARD_MATCH_COLUMNS: dict[str, str] = {
    "clause_title": "VARCHAR(256)",
    "page": "INTEGER",
}


async def _migrate_sqlite_patent_analysis_columns(conn) -> None:
    """Same ALTER TABLE pattern as _migrate_sqlite_research_columns, for the
    v0.3 C-SARAS fixes (ranked standards list + corpus versioning,
    clause_title/page on claim_standard_matches)."""
    if engine.dialect.name != "sqlite":
        return
    for table, columns in (
        ("patent_analyses", _NEW_PATENT_ANALYSIS_COLUMNS),
        ("claim_standard_matches", _NEW_CLAIM_STANDARD_MATCH_COLUMNS),
    ):
        try:
            result = await conn.execute(text(f"PRAGMA table_info({table})"))
            existing = {row[1] for row in result.fetchall()}
            for col, coltype in columns.items():
                if col not in existing:
                    await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}"))
                    logger.info(f"Migrated {table} table: added column {col} ({coltype})")
        except Exception as e:
            logger.warning(f"Could not auto-migrate '{table}' table for v0.3 columns: {e}")


_NEW_ETSI_DOCUMENT_COLUMNS: dict[str, str] = {
    "structured_path": "VARCHAR(1024)",
    "downloaded_at": "DATETIME",
    "extracted_at": "DATETIME",
    "completed_at": "DATETIME",
    # v0.4.4.1
    "index_attempts": "INTEGER DEFAULT 0",
    "last_error_type": "VARCHAR(48)",
    "last_error_at": "DATETIME",
    "text_chars": "INTEGER",
}


async def _migrate_sqlite_etsi_document_columns(conn) -> None:
    """Same ALTER TABLE pattern as the other _migrate_sqlite_* helpers, for
    the etsi-scraper-pipeline schema_version 1.1 fields (structured_path
    plus per-stage downloaded_at/extracted_at/completed_at timestamps).
    file_hash/file_size_bytes/local_pdf_path/retry_count already existed
    as columns from the old acquisition-pipeline schema and don't need a
    migration -- see etsi_corpus/pipeline.py's _apply_dataset_record for
    where they're now populated from the dataset-first import instead."""
    if engine.dialect.name != "sqlite":
        return
    try:
        result = await conn.execute(text("PRAGMA table_info(etsi_documents)"))
        existing = {row[1] for row in result.fetchall()}
        for col, coltype in _NEW_ETSI_DOCUMENT_COLUMNS.items():
            if col not in existing:
                await conn.execute(text(f"ALTER TABLE etsi_documents ADD COLUMN {col} {coltype}"))
                logger.info(f"Migrated etsi_documents table: added column {col} ({coltype})")
    except Exception as e:
        logger.warning(f"Could not auto-migrate 'etsi_documents' table for scraper schema 1.1 columns: {e}")


async def init_db():
    """Create all tables on startup, and migrate older SQLite databases."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await _migrate_sqlite_research_columns(conn)
        await _migrate_sqlite_patent_analysis_columns(conn)
        await _migrate_sqlite_sep_bulk_columns(conn)
        await _migrate_sqlite_sep_bulk_patent_columns(conn)
        await _migrate_sqlite_spec_crawl_job_columns(conn)
        await _migrate_sqlite_etsi_document_columns(conn)
        await _create_missing_indexes(conn)
        for ddl in _SEP_BULK_INDEX_DDL:
            try:
                await conn.execute(text(ddl))
            except Exception as e:
                logger.warning(f"Could not create sep_bulk index: {e}")
        for ddl in _SPEC_CRAWL_JOB_DEDUP_DDL:
            try:
                await conn.execute(text(ddl))
            except Exception as e:
                logger.warning(f"Could not create spec_crawl_jobs dedup index: {e}")
        for ddl in _SPEC_CRAWL_FILE_INDEX_DDL:
            try:
                await conn.execute(text(ddl))
            except Exception as e:
                logger.warning(f"Could not create spec_crawl_files index: {e}")
        for ddl in _SPEC_CRAWL_BATCH_INDEX_DDL:
            try:
                await conn.execute(text(ddl))
            except Exception as e:
                logger.warning(f"Could not create spec_crawl_batches index: {e}")
        for ddl in _ETSI_DOCUMENT_INDEX_DDL:
            try:
                await conn.execute(text(ddl))
            except Exception as e:
                logger.warning(f"Could not create etsi_documents index: {e}")
        for ddl in _PATENT_ANALYSIS_INDEX_DDL:
            try:
                await conn.execute(text(ddl))
            except Exception as e:
                logger.warning(f"Could not create patent analysis index: {e}")

    await recover_interrupted_crawl_jobs()
    await recover_interrupted_etsi_jobs()


async def recover_interrupted_crawl_jobs() -> int:
    """Called once at startup (see init_db, invoked from main.py's
    lifespan). If the backend was killed/crashed mid-crawl, any
    SpecCrawlJob/SpecCrawlBatch row left in an "active" status
    (pending/crawling/indexing, or queued/running for batches) has no
    real asyncio task backing it anymore in this fresh process -- nothing
    is going to update it further, and worse, the per-series active-job
    uniqueness constraint would otherwise block starting a NEW crawl for
    that series forever, since the DB still thinks one is running.

    This marks every such orphaned row as failed/interrupted (never as
    "completed" -- we have no way to know how far it actually got), with
    a distinct error_code so the UI can show "interrupted by restart"
    rather than a generic failure, and set retryable=True so the user's
    next click on that series (or "Resume Batch") starts a clean new job.
    Already-downloaded files and already-indexed chunks are untouched --
    only the job/batch bookkeeping rows are corrected, and a resumed crawl
    will skip everything it already has (existing-file-skip, stable chunk
    IDs) as usual.

    Returns the number of jobs recovered this way.
    """
    from sqlalchemy import select as _select
    recovered = 0
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            _select(SpecCrawlJob).where(SpecCrawlJob.status.in_(("pending", "crawling", "indexing")))
        )
        for job in result.scalars().all():
            job.status = "failed"
            job.current_operation = "failed"
            job.error_code = "INTERRUPTED_BY_RESTART"
            job.error_message = "This job was still active when the backend restarted and could not be resumed automatically. Retry to start a fresh crawl for this series (already-downloaded files will be skipped)."
            job.retryable = True
            job.completed_at = _utcnow()
            recovered += 1

        result = await session.execute(
            _select(SpecCrawlBatch).where(SpecCrawlBatch.status.in_(("queued", "running")))
        )
        for batch in result.scalars().all():
            batch.status = "failed"
            batch.error_message = "Interrupted by a backend restart. Use Resume Batch to continue any series that didn't finish."
            batch.completed_at = _utcnow()

        if recovered:
            await session.commit()
    if recovered:
        logger.warning(f"Recovered {recovered} crawl job(s) left active by a previous process (marked failed/interrupted, retryable).")
    return recovered


async def recover_interrupted_etsi_jobs() -> int:
    """Same rationale as recover_interrupted_crawl_jobs(), for the v0.3
    dataset import pipeline (etsi_corpus/pipeline.py). A job/document left
    'running'/'importing'/'indexing' when the process died has nothing
    backing it anymore -- mark it failed/retryable so 'Resume' on the
    Standards Corpus page starts a clean continuation rather than
    appearing to hang forever. Already-imported/indexed documents are
    untouched; only in-flight status is corrected, and per-document
    progress (EtsiDocument.status) is what makes the actual resume skip
    already-completed work."""
    from sqlalchemy import select as _select
    recovered = 0
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            _select(EtsiIngestionJob).where(EtsiIngestionJob.status.in_(("pending", "running", "paused")))
        )
        for job in result.scalars().all():
            job.status = "failed"
            job.current_operation = "interrupted"
            job.error_message = "Interrupted by a backend restart. Resume to continue from the last checkpointed document."
            job.completed_at = _utcnow()
            recovered += 1

        result = await session.execute(
            _select(EtsiDocument).where(EtsiDocument.status.in_(("importing", "indexing")))
        )
        for doc in result.scalars().all():
            # Roll back to the last known-good checkpoint stage rather than
            # a blanket "failed" -- e.g. a document interrupted mid-
            # "indexing" already has its structured fields populated from
            # the imported record, so it only needs re-indexing repeated,
            # not the whole import stage redone.
            rollback = {
                "importing": "pending",
                "indexing": "imported",
            }
            doc.status = rollback.get(doc.status, "pending")
        if recovered:
            await session.commit()
    if recovered:
        logger.warning(f"Recovered {recovered} ETSI ingestion job(s) left active by a previous process (marked failed, resumable).")
    return recovered
