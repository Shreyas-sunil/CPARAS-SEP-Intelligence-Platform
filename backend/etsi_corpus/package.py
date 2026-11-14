"""
Standards Corpus package (v0.3; formerly "Standards Corpus package") -- portability/versioning layer.

ARCHITECTURAL RULE (do not violate): the 54,000+ document ETSI ingestion
pipeline (etsi_corpus/pipeline.py) is a ONE-TIME, EXPLICIT build step --
triggered only via `python -m etsi_corpus.cli ingest` or the
POST /api/etsi-corpus/ingest/* endpoints. It NEVER runs automatically on
application startup. main.py's lifespan calls `ensure_dataset_available()`
below, which only *inspects and logs* whether a built package exists --
it never downloads a PDF, calls the LLM, or generates an embedding.

The output of one ingestion run is packaged into a single, versioned,
copyable directory (ETSI_DATASET_DIR, default ./data/etsi_v0.3):

    etsi_v0.3/
        package_manifest.json       <- version, hashes, counts, model versions
        ETSI_Series_Enriched_v0.3.xlsx   <- canonical human-readable dataset
        ETSI_Series_Enriched_v0.3.jsonl  <- structured runtime records
        text/                        <- extracted per-document text (evidence/provenance)
        vector_index/                <- copy of the persisted vector store
                                         (ChromaDB backend only -- see
                                         build_package()'s docstring for the
                                         Qdrant/FAISS caveat)

This whole directory is designed to be copied to another machine: point a
fresh install's ETSI_DATASET_DIR at a copy of it (or run `package install
--from <path>`) and GITAS/cPARAS/C-SARAS all become immediately usable
without re-downloading or re-processing anything.

At runtime, per the "important RAG consideration": the XLSX is the
canonical human-readable export, NOT what retrieval reads from -- the
vector index (already-shared across the whole platform, see
rag.vector_store) and the on-disk extracted text are what real retrieval
and evidence-backed claim-to-standard analysis actually use.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from core.config import settings

logger = logging.getLogger(__name__)

MANIFEST_FILENAME = "package_manifest.json"
ENRICHED_XLSX_NAME = "ETSI_Series_Enriched_v0.3.xlsx"
ENRICHED_JSONL_NAME = "ETSI_Series_Enriched_v0.3.jsonl"


@dataclass
class PackageManifest:
    """manifest.json of a portable Standards Corpus package: dataset version, the source manifest it
    was built from (name, hash, record count), counts of documents/chunks, embedding model and build
    time. Used to decide whether an installed package matches.
    """
    dataset_version: str
    source_manifest_filename: str
    source_manifest_hash: str
    source_record_count: int
    unique_pdf_count: int
    documents_processed: int
    documents_failed: int
    extraction_version: str
    llm_model: str
    embedding_model: str
    vector_backend: str
    vector_index_included: bool
    generated_at: str

    def to_json(self) -> str:
        """Pretty JSON form."""
        return json.dumps(asdict(self), indent=2)

    @staticmethod
    def from_json(raw: str) -> "PackageManifest":
        """Parse a manifest.json string."""
        return PackageManifest(**json.loads(raw))


def _hash_file(path: Path) -> str:
    """SHA-256 of a file in 1 MiB chunks."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def package_dir() -> Path:
    """Where the installed package lives (ETSI_DATASET_DIR)."""
    return Path(settings.ETSI_DATASET_DIR)


def package_manifest_path() -> Path:
    """Path of the installed package's manifest.json."""
    return package_dir() / MANIFEST_FILENAME


def load_package_manifest() -> Optional[PackageManifest]:
    """Reads the installed package's manifest, or None if no package is
    installed at ETSI_DATASET_DIR. Never raises -- a corrupt/partial
    manifest is treated as "not installed" rather than crashing startup."""
    path = package_manifest_path()
    if not path.exists():
        return None
    try:
        return PackageManifest.from_json(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, TypeError, OSError) as e:
        logger.warning(f"Standards Corpus package manifest at {path} is unreadable ({e}); treating as not installed.")
        return None


def _package_status_dict() -> dict:
    """Silent, no-logging read of the installed package's status -- used
    by the polled GET /package/status endpoint (v0.3-continuation fix,
    Step 26: the setup card's periodic refresh() was calling this every
    few seconds via the polled endpoint, and it previously reused
    ensure_dataset_available() -- which is documented as a one-time
    STARTUP check and logs a message on every call -- producing the
    reported repeated 'Standards Corpus package loaded...' log spam on every
    poll. Startup logging and polled reads are now separate functions."""
    manifest = load_package_manifest()
    if manifest is None:
        return {"installed": False, "dataset_dir": str(package_dir())}
    return {"installed": True, "dataset_dir": str(package_dir()), **asdict(manifest)}


async def ensure_dataset_available() -> dict:
    """Called ONCE at application startup (main.py's lifespan). Read-only:
    inspects whether a built package is present and logs a clear status
    message ONCE. NEVER triggers ingestion, downloads, or LLM calls -- per
    the architectural rule in this module's docstring, and NEVER called
    from a polled endpoint (see _package_status_dict() above for that).

    Returns a status dict the caller can log or expose via /api/health.
    """
    manifest = load_package_manifest()
    if manifest is None:
        logger.warning(
            "No Standards Corpus package found at %s. CPARAS Bulk/ISLD will run with "
            "an empty (or whatever ad-hoc) standards corpus until one is built or installed. "
            "Build one with `python -m etsi_corpus.cli ingest && python -m etsi_corpus.cli package build`, "
            "or point ETSI_DATASET_DIR at an existing package copied from another machine.",
            package_dir(),
        )
        return {"installed": False, "dataset_dir": str(package_dir())}

    logger.info(
        "Standards Corpus package loaded: version=%s documents=%s/%s generated=%s (from %s). "
        "NOTE: this reflects the last PORTABLE PACKAGE build/install, not necessarily the "
        "live corpus currently being ingested -- see GET /api/etsi-corpus/status for that.",
        manifest.dataset_version, manifest.documents_processed, manifest.source_record_count,
        manifest.generated_at, package_dir(),
    )
    return {"installed": True, "dataset_dir": str(package_dir()), **asdict(manifest)}


async def platform_status() -> dict:
    """Combined status for the frontend's Standards Corpus setup UX
    (GET /api/etsi-corpus/status): merges the installed package's manifest
    (if any) with LIVE counts from the database (etsi_documents), so the
    UI reflects real progress even mid-ingestion, before a package has
    been (re-)built. `installed` is true once there is anything usable to
    retrieve from -- either a built package, or at least one indexed
    document -- not only once a formal `package build` has been run.

    Shape matches what the frontend expects:
    {installed, version, documents, processed, indexed, failed,
     dataset_path, last_updated, source_manifest}
    """
    from etsi_corpus.export import corpus_stats
    from core.database import AsyncSessionLocal, EtsiIngestionJob
    from sqlalchemy import select

    manifest = load_package_manifest()
    stats = await corpus_stats()

    # Prefer the most recent ingestion job's known manifest size for
    # "documents" (the true denominator, e.g. 54,323) -- corpus_stats()
    # only counts EtsiDocument rows actually created so far, which can
    # under-report the total while a bounded/partial run is in progress.
    documents_total = stats["total_documents"]
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(EtsiIngestionJob).order_by(EtsiIngestionJob.created_at.desc()).limit(1)
        )
        latest_job = result.scalar_one_or_none()
        if latest_job and latest_job.manifest_row_count:
            # Only meaningful once the job is running against the WHOLE
            # manifest (limit_requested is None) -- a bounded sample run's
            # manifest_row_count still reflects the full manifest's row
            # count, not the sample size, so don't use it to claim more
            # documents exist than were actually targeted this run.
            if latest_job.limit_requested is None:
                documents_total = max(documents_total, latest_job.manifest_row_count)
            elif latest_job.total_documents:
                documents_total = max(documents_total, latest_job.total_documents)

    installed = manifest is not None or stats["indexed"] > 0
    source_manifest_name = manifest.source_manifest_filename if manifest else None

    # ── single canonical readiness label (v0.3-continuation Part 4) ──
    # This is THE definition every feature (Standards Corpus, C-SARAS,
    # cPARAS, the Standards Corpus status banner) reads -- none of them compute
    # their own "is the KB ready" guess independently.
    if documents_total == 0 and stats["indexed"] == 0:
        readiness = "NOT_STARTED"
    elif stats["failed"] > 0 and stats["indexed"] == 0 and documents_total > 0 and stats["downloaded"] == documents_total:
        readiness = "FAILED"
    elif stats["indexed"] >= documents_total and documents_total > 0:
        readiness = "READY"
    elif stats["indexed"] > 0:
        readiness = "PARTIALLY_READY"
    else:
        readiness = "BUILDING"

    return {
        "installed": installed,
        "readiness": readiness,
        "version": manifest.dataset_version if manifest else (settings.ETSI_DATASET_VERSION if installed else None),
        "documents": documents_total,
        "processed": stats["processed"],
        "enriched": stats.get("enriched", stats["processed"]),
        "indexed": stats["indexed"],
        "failed": stats["failed"],
        "pending": stats.get("pending", 0),
        "dataset_path": str(package_dir()) if manifest else None,
        "last_updated": manifest.generated_at if manifest else None,
        "source_manifest": source_manifest_name,
    }


async def build_package(extraction_version: str = "v0.3") -> PackageManifest:
    """ONE-TIME build step (explicit CLI/API call only -- see
    `python -m etsi_corpus.cli package build`). Gathers whatever has been
    ingested into etsi_documents so far, writes the enriched xlsx/jsonl,
    copies the extracted-text cache and (ChromaDB only) the persisted
    vector index into ETSI_DATASET_DIR, and writes a versioned manifest.

    NOTE on vector index portability: only the ChromaDB backend (the
    default) is a plain on-disk directory that can be copied wholesale.
    Qdrant (a separate server process) and FAISS (a single flat index file
    shared with non-ETSI content) are not copied here -- for those
    backends, `vector_index_included` is False in the manifest and the
    receiving machine must re-run indexing (etsi_corpus.pipeline's index
    stage) against its own vector store instead of copying files, which
    only re-embeds already-extracted text (fast) rather than re-downloading
    or re-running the LLM (slow).
    """
    from etsi_corpus.export import build_enriched_export, fetch_records
    from core.database import AsyncSessionLocal, EtsiIngestionJob
    from sqlalchemy import select

    out_dir = package_dir()
    out_dir.mkdir(parents=True, exist_ok=True)

    xlsx_bytes, jsonl_bytes, n = await build_enriched_export()
    (out_dir / ENRICHED_XLSX_NAME).write_bytes(xlsx_bytes)
    (out_dir / ENRICHED_JSONL_NAME).write_bytes(jsonl_bytes)

    # Extracted text (evidence/provenance) -- copy the whole per-document
    # text cache rather than re-pointing at ETSI_DATASET_OUTPUT, so the
    # package is self-contained and movable on its own.
    text_src = Path(settings.ETSI_DATASET_OUTPUT) / "text"
    text_dst = out_dir / "text"
    if text_src.exists():
        if text_dst.exists():
            shutil.rmtree(text_dst)
        shutil.copytree(text_src, text_dst)

    vector_index_included = False
    if settings.VECTOR_BACKEND == "chromadb":
        chroma_src = Path(settings.CHROMADB_PATH)
        if chroma_src.exists():
            chroma_dst = out_dir / "vector_index"
            if chroma_dst.exists():
                shutil.rmtree(chroma_dst)
            shutil.copytree(chroma_src, chroma_dst)
            vector_index_included = True

    records = await fetch_records()
    from etsi_corpus.pipeline import SEARCHABLE_STATUSES
    failed = sum(1 for r in records if str(r.get("status", "")).endswith("_failed"))
    completed = sum(1 for r in records if r.get("status") in SEARCHABLE_STATUSES)

    # v0.3-continuation (dataset-first architecture): "source manifest"
    # is now "source dataset" -- the imported etsi_dataset.jsonl file, not
    # a URL manifest SEP itself would download from. The most recent
    # import job's `manifest_path` field (kept as-is for schema/back-
    # compat -- see database.py's EtsiIngestionJob) holds the JSONL path
    # actually imported.
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(EtsiIngestionJob).order_by(EtsiIngestionJob.created_at.desc()).limit(1)
        )
        latest_job = result.scalar_one_or_none()
    source_dataset_path = Path(latest_job.manifest_path) if latest_job and latest_job.manifest_path else None
    source_hash = _hash_file(source_dataset_path) if source_dataset_path and source_dataset_path.exists() else "Unknown"
    source_record_count = latest_job.manifest_row_count if latest_job and latest_job.manifest_row_count else len(records)

    manifest = PackageManifest(
        dataset_version=settings.ETSI_DATASET_VERSION,
        source_manifest_filename=source_dataset_path.name if source_dataset_path else "Unknown",
        source_manifest_hash=source_hash,
        source_record_count=source_record_count,
        unique_pdf_count=len(records),
        documents_processed=completed,
        documents_failed=failed,
        extraction_version=extraction_version,
        llm_model=settings.OLLAMA_MODEL if settings.LLM_BACKEND == "ollama" else settings.LLM_BACKEND,
        embedding_model=settings.EMBEDDING_MODEL,
        vector_backend=settings.VECTOR_BACKEND,
        vector_index_included=vector_index_included,
        generated_at=datetime.now(timezone.utc).isoformat(),
    )
    package_manifest_path().write_text(manifest.to_json(), encoding="utf-8")
    logger.info(f"Standards Corpus package built at {out_dir}: {completed}/{len(records)} documents, vector_index_included={vector_index_included}")
    return manifest


async def install_package(source_dir: str) -> PackageManifest:
    """Installs a package built on another machine: copies `source_dir`'s
    contents into ETSI_DATASET_DIR (device-sharing workflow -- "Machine A
    builds, Machine B installs and uses immediately"). Validates the
    source has a manifest before copying anything."""
    src = Path(source_dir)
    src_manifest = src / MANIFEST_FILENAME
    if not src_manifest.exists():
        raise ValueError(f"{source_dir} does not look like an Standards Corpus package (no {MANIFEST_FILENAME})")

    dst = package_dir()
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)

    manifest = load_package_manifest()
    if manifest is None:
        raise ValueError("Copied package but its manifest could not be read back -- installation failed.")

    # If this backend uses ChromaDB and the installed package included a
    # vector index, point CHROMADB_PATH at it so retrieval works
    # immediately without a separate re-index step -- this only rewires
    # the in-process setting for the CURRENT run; persist the equivalent
    # change in backend/.env for it to survive a restart.
    if settings.VECTOR_BACKEND == "chromadb" and manifest.vector_index_included:
        installed_index = dst / "vector_index"
        if installed_index.exists():
            settings.CHROMADB_PATH = str(installed_index)
            logger.info(f"Vector index installed from package; CHROMADB_PATH now points at {installed_index}. "
                        f"Set CHROMADB_PATH={installed_index} in backend/.env to persist this across restarts.")

    logger.info(f"Standards Corpus package installed from {source_dir} -> {dst} (version {manifest.dataset_version})")
    return manifest
