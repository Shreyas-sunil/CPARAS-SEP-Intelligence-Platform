"""
Standards Corpus API (v0.3, dataset-first architecture) -- backs GITAS's
"Standards Corpus" page.

v0.3-continuation: SEP Platform no longer downloads ETSI PDFs itself. The
standalone `etsi-scraper-pipeline` project produces a dataset
(etsi_dataset.jsonl + extracted text), and this API IMPORTS that
already-prepared dataset -- there is no code path here that reaches
etsi.org, calls an LLM to extract structured fields, or downloads a PDF.

Endpoint map:
  GET  /api/etsi-corpus/status              -- canonical live-corpus status
  GET  /api/etsi-corpus/stats                -- dashboard counters
  POST /api/etsi-corpus/dataset/import       -- upload a dataset zip and import it
  GET  /api/etsi-corpus/jobs                 -- recent import jobs
  GET  /api/etsi-corpus/jobs/{id}            -- one job's status
  GET  /api/etsi-corpus/jobs/{id}/failed     -- per-document failure detail
  POST /api/etsi-corpus/jobs/{id}/pause      -- cooperative stop
  POST /api/etsi-corpus/jobs/{id}/cancel     -- same mechanism as pause
  POST /api/etsi-corpus/jobs/{id}/resume     -- resume a stopped/interrupted job
  POST /api/etsi-corpus/jobs/{id}/retry      -- retry this job's failed documents
  GET  /api/etsi-corpus/documents            -- list/filter imported documents
  GET  /api/etsi-corpus/export               -- download ETSI_Series_Enriched_v0.3.xlsx / .jsonl
  GET  /api/etsi-corpus/package/*            -- portable Standards Corpus package (unchanged)

v0.4.4 - canonical 3GPP dataset (CPARAS_DATASET_ROOT, read-only, streamed):
  GET  /api/etsi-corpus/dataset/local/status      -- configured? files? build facts? what is imported? diagnosis
  POST /api/etsi-corpus/dataset/local/validate    -- streaming validation (schema, text pairing, history checksums)
  POST /api/etsi-corpus/dataset/local/import      -- import + index from the configured folder (no upload)
  GET  /api/etsi-corpus/dataset/jobs/{id}/report  -- counts, duration, skipped/failed samples of one import
  POST /api/etsi-corpus/dataset/recover           -- reset documents left half-processed by a crash
  GET  /api/etsi-corpus/standards/specs           -- imported specifications
  GET  /api/etsi-corpus/standards/{spec}/versions -- every imported version of a specification
  GET  /api/etsi-corpus/standards/{spec}          -- one version (exact / newest-in-release / latest known)
  GET  /api/etsi-corpus/standards/{spec}/clauses  -- clause-level retrieval with source references

Long-running imports happen via BackgroundTasks + the resumable
EtsiIngestionJob/EtsiDocument checkpoints -- this router only ever does
quick DB reads/writes and file-upload handling inline.
"""

from __future__ import annotations

import io
import logging
import shutil
import uuid
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, UploadFile, File
from fastapi.responses import StreamingResponse
from sqlalchemy import select, func

from core.config import settings
from core.database import AsyncSessionLocal, EtsiDocument, EtsiIngestionJob
from core import gpp_dataset as gd
from etsi_corpus.pipeline import (import_dataset, unpack_dataset_zip, request_stop, failed_documents, job_status,
                                  read_import_report, recover_interrupted_documents)
from etsi_corpus import dataset_local, gpp_lookup
from etsi_corpus.export import build_enriched_export, corpus_stats
from etsi_corpus.package import (
    build_package, install_package, load_package_manifest, platform_status, package_dir,
    _package_status_dict,
)

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/status")
async def get_platform_status():
    """The single status endpoint the frontend's Standards Corpus setup
    UX polls: {installed, version, documents, processed, indexed, failed,
    dataset_path, last_updated, source_manifest}. Distinct from /stats
    (raw etsi_documents counters) and /package/status (raw portable-
    package manifest) -- this is the merged, UI-ready shape."""
    return await platform_status()


@router.get("/package/status")
async def get_package_status():
    """Read-only: is a versioned, pre-built PORTABLE package installed at
    ETSI_DATASET_DIR? Distinct from "is the live corpus usable right now"
    (GET /status) -- see that endpoint for why C-SARAS/cPARAS read from
    there instead. Uses a silent (non-logging) read -- this is polled
    every few seconds by the Standards Corpus page."""
    return _package_status_dict()


@router.post("/package/build")
async def build_package_endpoint():
    """Packages whatever is currently imported+indexed (+ the shared
    vector index, for the ChromaDB backend) into ETSI_DATASET_DIR as a
    versioned, portable directory."""
    manifest = await build_package()
    return {"status": "built", "manifest": manifest.__dict__}


@router.post("/package/install")
async def install_package_endpoint(source_dir: str = Query(..., description="Path to a package directory built on another machine")):
    """POST /api/etsi-corpus/package/install?source_dir= - install a Standards Corpus package built on
    another machine (copy + verify its manifest). 400 when the folder is not a valid package.
    Requires `reference.manage`.
    """
    try:
        manifest = await install_package(source_dir)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"status": "installed", "manifest": manifest.__dict__}


@router.get("/package/download")
async def download_package():
    """Zips the installed package directory for the 'Download Knowledge
    Package' button -- the portable artifact a user can copy to another
    machine and install with POST /package/install."""
    import tempfile as _tempfile

    manifest = load_package_manifest()
    if manifest is None:
        raise HTTPException(status_code=400, detail="No Standards Corpus package is installed yet -- build one first.")

    src = package_dir()
    with _tempfile.TemporaryDirectory() as tmp:
        archive_base = str(Path(tmp) / "etsi_knowledge_package")
        archive_path = shutil.make_archive(archive_base, "zip", root_dir=str(src))
        data = Path(archive_path).read_bytes()
    return StreamingResponse(
        io.BytesIO(data), media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename=ETSI_Knowledge_Package_{manifest.dataset_version}.zip"},
    )


@router.get("/stats")
async def get_stats():
    """GET /api/etsi-corpus/stats - document/chunk counts of the imported Standards Corpus. Requires
    `reference.view`.
    """
    return await corpus_stats()


@router.post("/dataset/import")
async def import_dataset_endpoint(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(..., description="A dataset zip produced by the standalone etsi-scraper-pipeline (etsi_dataset.jsonl + text/)"),
    limit: int = Query(default=None, description="Max records to import this run. Omit to use ETSI_DEFAULT_IMPORT_LIMIT."),
    all_rows: bool = Query(default=False, description="Explicit opt-in to import EVERY record in the dataset."),
):
    """Uploads and imports an already-prepared ETSI dataset (produced by
    the standalone etsi-scraper-pipeline project). This is the ONLY way
    the Standards Corpus grows -- there is no live-download
    trigger anywhere in this API."""
    upload_dir = Path(settings.ETSI_DATASET_IMPORT_DIR) / uuid.uuid4().hex[:12]
    upload_dir.mkdir(parents=True, exist_ok=True)
    zip_path = upload_dir / Path(file.filename or "dataset.zip").name       # never a client-chosen directory
    with open(zip_path, "wb") as out:                                          # streamed in 1 MB chunks, never held in RAM
        shutil.copyfileobj(file.file, out, 1 << 20)

    try:
        jsonl_path, text_dir = unpack_dataset_zip(zip_path, upload_dir / "unpacked")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    effective_limit = None if all_rows else (limit or settings.ETSI_DEFAULT_IMPORT_LIMIT)

    async with AsyncSessionLocal() as session:
        job = EtsiIngestionJob(manifest_path=str(jsonl_path), status="pending", limit_requested=effective_limit)
        session.add(job)
        await session.commit()
        await session.refresh(job)

    background_tasks.add_task(_run_import_job, jsonl_path, text_dir, effective_limit, job.id)
    return {"job_id": job.id, "status": "pending", "limit": effective_limit, "source_dataset": file.filename}


@router.post("/dataset/import/retry")
async def retry_failed_dataset_import(background_tasks: BackgroundTasks):
    """Re-runs only records currently in a *_failed status, across all
    import jobs -- reuses import_dataset's existing per-document status
    checks, so this doesn't need separate retry logic. Requires the most
    recent job's source JSONL/text still be present on disk (they are,
    under ETSI_DATASET_IMPORT_DIR, unless manually cleaned up)."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(EtsiIngestionJob).order_by(EtsiIngestionJob.created_at.desc()).limit(1)
        )
        latest_job = result.scalar_one_or_none()
    if latest_job is None or not latest_job.manifest_path:
        raise HTTPException(status_code=400, detail="No prior dataset import found to retry.")
    jsonl_path = Path(latest_job.manifest_path)
    if not jsonl_path.exists():
        raise HTTPException(status_code=400, detail=f"Source dataset file no longer present at {jsonl_path} -- re-upload it via /dataset/import.")
    prev = read_import_report(latest_job.id) or {}

    async with AsyncSessionLocal() as session:
        job = EtsiIngestionJob(manifest_path=str(jsonl_path), status="pending", current_operation="retrying_failed")
        session.add(job)
        await session.commit()
        await session.refresh(job)
    background_tasks.add_task(_run_import_job, jsonl_path, _text_dir_for(jsonl_path), None, job.id, _opts_from_report(prev))
    return {"job_id": job.id, "status": "pending"}


def _text_dir_for(jsonl_path: Path):
    """text/ folder beside the JSONL (uploaded zips, legacy) or the canonical etsi_dataset.zip beside it."""
    td = jsonl_path.parent / "text"
    if td.is_dir():
        return td
    z = jsonl_path.parent / gd.ZIP_NAME
    if z.is_file():
        return gd.ZipTextSource(z)
    return None


def _opts_from_report(prev: dict) -> dict:
    """Import options (version filter, series/spec selection, copy-text flag) recorded in a previous
    job's report, so a resume/retry uses exactly the same selection.
    """
    return {"versions": prev.get("versions", "all"), "series": prev.get("series", []), "specs": prev.get("specs", []),
            "copy_text": prev.get("copy_text_locally")}


async def _run_import_job(jsonl_path: Path, text_dir, limit: Optional[int], job_id: int, opts: Optional[dict] = None) -> None:
    """Background task for a dataset import/resume: run import_dataset and, on any exception, mark the
    EtsiIngestionJob failed with the error so the UI never shows a job stuck 'running'.
    """
    opts = opts or {}
    try:
        await import_dataset(jsonl_path=jsonl_path, text_dir=text_dir, limit=limit, job_id=job_id, **opts)
    except Exception as e:
        logger.error(f"ETSI dataset import job {job_id} failed: {e}", exc_info=True)
        async with AsyncSessionLocal() as session:
            result = await session.execute(select(EtsiIngestionJob).where(EtsiIngestionJob.id == job_id))
            job = result.scalar_one_or_none()
            if job:
                job.status = "failed"
                job.error_message = str(e)
                await session.commit()
    finally:
        if isinstance(text_dir, gd.TextSource):
            text_dir.close()


@router.get("/jobs")
async def list_jobs(limit: int = 20):
    """GET /api/etsi-corpus/jobs - most recent Standards Corpus import jobs with progress counters.
    Requires `reference.view`.
    """
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(EtsiIngestionJob).order_by(EtsiIngestionJob.created_at.desc()).limit(limit)
        )
        jobs = result.scalars().all()
        return [
            {
                "id": j.id, "status": j.status, "current_operation": j.current_operation,
                "total_documents": j.total_documents, "documents_indexed": j.documents_indexed,
                "documents_failed": j.documents_failed, "limit_requested": j.limit_requested,
                "started_at": j.started_at.isoformat() if j.started_at else None,
                "completed_at": j.completed_at.isoformat() if j.completed_at else None,
            }
            for j in jobs
        ]


@router.get("/jobs/{job_id}")
async def get_job(job_id: int):
    """GET /api/etsi-corpus/jobs/{job_id} - live status of one import job; 404 if unknown."""
    status = await job_status(job_id)
    if not status:
        raise HTTPException(status_code=404, detail="Job not found")
    return status


@router.get("/jobs/{job_id}/failed")
async def get_job_failed_documents(job_id: int):
    """Per-document failure detail: standard name, URL, failure stage,
    error message, retry count -- so a partial failure never just shows
    an opaque aggregate count."""
    return {"failed_documents": await failed_documents(job_id)}


@router.post("/jobs/{job_id}/pause")
async def pause_job(job_id: int):
    """Cooperative stop: the running job finishes whatever document(s) are
    currently mid-stage, then stops scheduling new ones, leaving every
    document at a consistent, resumable checkpoint."""
    accepted = request_stop(job_id)
    if not accepted:
        raise HTTPException(status_code=409, detail="This job is not currently running in this backend process. If it was left running before a restart, use /resume instead.")
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(EtsiIngestionJob).where(EtsiIngestionJob.id == job_id))
        job = result.scalar_one_or_none()
        if job:
            job.current_operation = "pausing"
            await session.commit()
    return {"job_id": job_id, "status": "pausing"}


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(job_id: int):
    """Same safe stop mechanism as /pause -- kept as a separate, explicitly
    -named action for the frontend's Cancel button."""
    accepted = request_stop(job_id)
    if not accepted:
        raise HTTPException(status_code=409, detail="This job is not currently running in this backend process.")
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(EtsiIngestionJob).where(EtsiIngestionJob.id == job_id))
        job = result.scalar_one_or_none()
        if job:
            job.current_operation = "cancelling"
            await session.commit()
    return {"job_id": job_id, "status": "cancelling"}


@router.post("/jobs/{job_id}/resume")
async def resume_job(background_tasks: BackgroundTasks, job_id: int):
    """POST /api/etsi-corpus/jobs/{job_id}/resume - continue a paused/failed import from its checkpoint
    with the original options. 400 when the source dataset file is gone. Requires
    `reference.manage`.
    """
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(EtsiIngestionJob).where(EtsiIngestionJob.id == job_id))
        job = result.scalar_one_or_none()
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found")
    jsonl_path = Path(job.manifest_path)
    if not jsonl_path.exists():
        raise HTTPException(status_code=400, detail=f"Source dataset file no longer present at {jsonl_path} -- re-upload it via /dataset/import.")
    prev = read_import_report(job_id) or {}
    background_tasks.add_task(_run_import_job, jsonl_path, _text_dir_for(jsonl_path), job.limit_requested, job_id, _opts_from_report(prev))
    return {"job_id": job_id, "status": "resuming"}


@router.post("/jobs/{job_id}/retry")
async def retry_job(background_tasks: BackgroundTasks, job_id: int):
    """Retries only this job's failed documents."""
    return await resume_job(background_tasks, job_id)


# ── v0.4.4: canonical dataset workflow (configured folder, read-only, streamed) ─────────────────
@router.get("/dataset/local/status")
async def local_dataset_status():
    """Is CPARAS_DATASET_ROOT configured and usable, what does the build contain, how much is
    imported/indexed, and what (if anything) is wrong."""
    return await dataset_local.dataset_status()


@router.post("/dataset/local/validate")
async def local_dataset_validate(
    crc: bool = Query(default=False, description="Also read every ZIP member to verify CRCs (slow on multi-GB archives)."),
    history: bool = Query(default=True, description="Verify spec_history sidecar blob references and SHA-256 checksums."),
):
    """Streaming validation of the configured dataset: JSON lines, identity, text pairing, duplicates,
    unknown versions, ZIP integrity, history sidecar checksums. Reads - never changes - the dataset."""
    return await dataset_local.validate_dataset(crc=crc, check_history=history)


@router.post("/dataset/local/import")
async def local_dataset_import(
    background_tasks: BackgroundTasks,
    limit: int = Query(default=None, description="Max records this run. Omit to use ETSI_DEFAULT_IMPORT_LIMIT."),
    all_rows: bool = Query(default=False, description="Explicit opt-in: import EVERY selected record."),
    versions: str = Query(default="all", description="all | latest | latest-per-release (unknown versions are always kept)"),
    series: str = Query(default="", description="Comma-separated series filter, e.g. 36,38"),
    specs: str = Query(default="", description="Comma-separated specification filter, e.g. 38.331,38.211"),
    copy_text: bool = Query(default=False, description="Copy text into this app's store instead of reading etsi_dataset.zip in place."),
):
    """Import + index the dataset from CPARAS_DATASET_ROOT (no upload). Runs as a background job:
    watch GET /jobs/{id}; the final numbers are in GET /dataset/jobs/{id}/report."""
    if versions not in gd.VERSION_MODES:
        raise HTTPException(status_code=400, detail=f"versions must be one of {list(gd.VERSION_MODES)}")
    try:
        dataset_local.prepare_import_inputs()[1].close()          # fail fast, before a job exists
    except gd.DatasetError as e:
        raise HTTPException(status_code=400, detail=e.message)
    effective_limit = None if all_rows else (limit or settings.ETSI_DEFAULT_IMPORT_LIMIT)
    job_id = await dataset_local.create_local_job(effective_limit)
    background_tasks.add_task(
        dataset_local.run_local_import, job_id, limit=effective_limit, versions=versions,
        series=[x for x in series.split(",") if x.strip()], specs=[x for x in specs.split(",") if x.strip()], copy_text=copy_text)
    return {"job_id": job_id, "status": "pending", "limit": effective_limit, "versions": versions,
            "note": "Indexing (chunking + embedding) is the slow part; progress is in /jobs/{id}."}


@router.get("/dataset/jobs/{job_id}/report")
async def import_job_report(job_id: int):
    """GET /api/etsi-corpus/dataset/jobs/{job_id}/report - the per-job import report (selection,
    counts, skipped records); 404 when none exists.
    """
    rep = read_import_report(job_id)
    if rep is None:
        raise HTTPException(status_code=404, detail="No report for this job (it has not started, or predates v0.4.4).")
    return rep


@router.post("/dataset/recover")
async def recover_interrupted():
    """Reset documents a crash left in 'importing'/'indexing' so the next import retries them."""
    return {"recovered": await recover_interrupted_documents()}


@router.get("/standards/specs")
async def standards_specs(series: Optional[str] = None, q: Optional[str] = None, limit: int = 50, offset: int = 0):
    """GET /api/etsi-corpus/standards/specs - 3GPP specifications known to the imported dataset (filter
    by series / text, paged).
    """
    return await gpp_lookup.list_specs(series=series, q=q, limit=min(limit, 500), offset=offset)


@router.get("/standards/{spec}/versions")
async def standards_versions(spec: str):
    """GET /api/etsi-corpus/standards/{spec}/versions - every imported version of one specification."""
    return await gpp_lookup.spec_versions(spec)


@router.get("/standards/{spec}/clauses")
async def standards_clauses(spec: str, version: Optional[str] = None, release: Optional[str] = None,
                            clause: Optional[str] = None, limit: int = 200):
    """GET /api/etsi-corpus/standards/{spec}/clauses - clauses of a specification, optionally for one
    version/release/clause.
    """
    return await gpp_lookup.get_clauses(spec, version=version, release=release, clause=clause, limit=min(limit, 1000))


@router.get("/standards/{spec}")
async def standards_spec(spec: str, version: Optional[str] = None, release: Optional[str] = None):
    """GET /api/etsi-corpus/standards/{spec} - metadata of one specification (optionally a specific
    version/release).
    """
    return await gpp_lookup.get_spec(spec, version=version, release=release)


@router.get("/documents")
async def list_documents(
    status: Optional[str] = None,
    series: Optional[str] = None,
    domain: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
):
    """GET /api/etsi-corpus/documents - imported ETSI documents filtered by status / series / domain,
    paged, with the total count.
    """
    async with AsyncSessionLocal() as session:
        stmt = select(EtsiDocument)
        if status:
            stmt = stmt.where(EtsiDocument.status == status)
        if series:
            stmt = stmt.where(EtsiDocument.series == series)
        if domain:
            stmt = stmt.where(EtsiDocument.technology_domain == domain)
        count_stmt = select(func.count()).select_from(stmt.subquery())
        total = (await session.execute(count_stmt)).scalar_one()
        stmt = stmt.order_by(EtsiDocument.id.desc()).offset(offset).limit(limit)
        rows = (await session.execute(stmt)).scalars().all()
        return {
            "total": total,
            "documents": [
                {
                    "id": d.id, "pdf_url": d.pdf_url, "standard_name": d.standard_name_raw,
                    "title": d.title, "series": d.series, "specification_number": d.specification_number,
                    "version": d.version, "release": d.release, "technology_domain": d.technology_domain,
                    "status": d.status, "extraction_confidence": d.extraction_confidence,
                    "chunks_indexed": d.chunks_indexed, "error_message": d.error_message,
                    # ── file / stage provenance from the scraper (schema_version >= "1.1") --
                    #    "Unknown"/None on older-schema imports, never fabricated ──
                    "file_hash": d.file_hash, "file_size_bytes": d.file_size_bytes,
                    "local_pdf_path": d.local_pdf_path, "structured_path": d.structured_path,
                    "retry_count": d.retry_count,
                    "downloaded_at": d.downloaded_at.isoformat() if d.downloaded_at else None,
                    "extracted_at": d.extracted_at.isoformat() if d.extracted_at else None,
                    "completed_at": d.completed_at.isoformat() if d.completed_at else None,
                }
                for d in rows
            ],
        }


@router.get("/export")
async def export_enriched_dataset(format: str = Query(default="xlsx", pattern="^(xlsx|jsonl)$"), status: Optional[str] = None):
    """GET /api/etsi-corpus/export?format=xlsx|jsonl - download the enriched dataset of all imported
    documents. 400 when nothing is imported. Requires `reference.manage` (bulk export).
    """
    xlsx_bytes, jsonl_bytes, n = await build_enriched_export(status=status)
    if n == 0:
        raise HTTPException(status_code=400, detail="No ETSI documents have been imported yet -- import a dataset first.")
    if format == "jsonl":
        return StreamingResponse(
            io.BytesIO(jsonl_bytes), media_type="application/x-ndjson",
            headers={"Content-Disposition": "attachment; filename=ETSI_Series_Enriched_v0.3.jsonl"},
        )
    return StreamingResponse(
        io.BytesIO(xlsx_bytes), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=ETSI_Series_Enriched_v0.3.xlsx"},
    )
