"""
Scraper API (LEGACY -- v0.3)
=============================
v0.3 NOTE: the primary way to build the platform's standards knowledge
base is now the manifest-driven ETSI Standards Corpus pipeline (see
api/routes/gitas/etsi_corpus.py, backend/etsi_corpus/) -- neither GITAS's
dashboard, cPARAS, nor C-SARAS require this 3GPP-archive crawler to be run
first anymore (item 4 of the v0.3 spec). This module and its /corpus
frontend page are kept mounted for backward compatibility and as a
secondary/supplementary source (its output still indexes into the same
shared vector store under source_type="3gpp_spec", which
services/retrieval_service.py merges alongside the ETSI corpus), not
because either module requires it.

Backend for the (now secondary) "3GPP Corpus" page: lets the user download real
3GPP specification text (via ingestion/gpp_crawl.py) and automatically
index it (via ingestion/spec_corpus.py) so it becomes searchable by
/api/text-match and research/engine.py.

"Most common series" is not a guess -- it's computed from this repo's own
BUILTIN_3GPP_KNOWLEDGE list (ingestion/pipeline.py), which is heavily
weighted toward 38_series (5G NR: RRC/MAC/PHY/architecture specs). See
`_recommended_series()` below for the exact computation. If you have a
better signal for "most relevant to us" (e.g. actual declared-standard
counts from uploaded ETSI bulk exports), swap that in instead -- this is a
reasonable, documented default, not an authoritative claim about 3GPP as a
whole.

CONCURRENCY (v0.3)
-------------------
Only one ACTIVE job (status pending/crawling/indexing) may exist per
series at a time. Two layers enforce this:
  1. A per-series `asyncio.Lock()` (`_series_locks`), so a normal
     check-then-create sequence never races with itself inside this
     process -- this is what lets /start return a clean, friendly
     "already running" response instead of a raw error in the common case.
  2. A partial UNIQUE index on spec_crawl_jobs (see core/database.py's
     `_SPEC_CRAWL_JOB_DEDUP_DDL`), which is what actually GUARANTEES
     correctness even under a true simultaneous race or a multi-worker
     deployment -- the lock alone cannot do that across processes.
Both layers matter: the lock makes the common case pleasant, the index
makes the guarantee real. See ingestion/gpp_crawl.py for the matching
fix on the filesystem side (job-scoped temp filenames, retried atomic
finalize) -- that module's docstring explains the WinError 32 root cause
this whole set of changes addresses.

Endpoint map:
  GET  /api/scrape/series          — all series + which one is recommended
  POST /api/scrape/start           — start crawl+index jobs for chosen series
  GET  /api/scrape/jobs            — recent jobs (for polling)
  GET  /api/scrape/jobs/{id}       — single job detail
  POST /api/scrape/jobs/{id}/retry  — retry a finished/failed job
  POST /api/scrape/jobs/{id}/pause  — cooperative stop (alias of /cancel)
  POST /api/scrape/jobs/{id}/resume — resume a paused/cancelled/failed job (alias of /retry)
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import Counter
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.config import settings
from core.database import get_db, SpecCrawlJob, SpecCrawlFile, SpecCrawlBatch, engine
from ingestion.gpp_crawl import ALL_SERIES, crawl_series, default_out_root, request_cancel
from ingestion.pipeline import BUILTIN_3GPP_KNOWLEDGE
from ingestion.spec_corpus import build_corpus_from_crawl
from core.timeutil import utcnow as _utcnow

logger = logging.getLogger(__name__)
router = APIRouter()

# Background jobs need their own session (same pattern as
# ingestion/pipeline.py's run_ingestion_pipeline -- a request-scoped
# session from Depends(get_db) doesn't outlive the request/response).
_BgSession = async_sessionmaker(engine, expire_on_commit=False)

ACTIVE_STATUSES = ("pending", "crawling", "indexing")

# One asyncio.Lock per series, created lazily. Scoped to this process --
# see module docstring for why the DB-level unique index is what actually
# guarantees correctness beyond this process's own request handling.
_series_locks: dict[str, asyncio.Lock] = {}


def _lock_for(series: str) -> asyncio.Lock:
    """Per-series asyncio lock (created on first use) so two requests can never start a crawl for the
    same 3GPP series at once.
    """
    if series not in _series_locks:
        _series_locks[series] = asyncio.Lock()
    return _series_locks[series]


def _recommended_series() -> str:
    """The series that covers most of the built-in 3GPP specification list (BUILTIN_3GPP_KNOWLEDGE) -
    offered as the one-click 'Quick start' crawl; 38_series when the list is empty.
    """
    counts = Counter(
        item["spec"].split()[1].split(".")[0] + "_series"
        for item in BUILTIN_3GPP_KNOWLEDGE
        if len(item["spec"].split()) > 1
    )
    if not counts:
        return "38_series"
    return counts.most_common(1)[0][0]


RECOMMENDED_SERIES = _recommended_series()

SERIES_LABELS = {
    "21_series": "Requirements",
    "22_series": "Service aspects",
    "23_series": "Technical realization (architecture)",
    "24_series": "Signalling protocols (UE-network)",
    "25_series": "Radio aspects (UMTS/3G)",
    "26_series": "Codecs",
    "27_series": "Data terminal equipment",
    "28_series": "Signalling protocols (RC/OAM)",
    "29_series": "Signalling protocols (core network)",
    "31_series": "Subscriber Identity Module",
    "32_series": "OAM&P",
    "33_series": "Security",
    "34_series": "UE conformance",
    "35_series": "Security algorithms",
    "36_series": "LTE (radio technology)",
    "37_series": "Multi-radio access technology",
    "38_series": "5G / NR (radio technology)",
    "42_series": "GSM UE conformance",
    "43_series": "GSM RAN",
    "44_series": "GSM/EDGE signalling",
    "45_series": "GSM/EDGE radio",
    "48_series": "GSM signalling (BSS-MS)",
}


class SeriesInfo(BaseModel):
    """One crawlable 3GPP series as listed by GET /api/scrape/series."""
    series: str
    label: str
    recommended: bool


class StartCrawlRequest(BaseModel):
    """Body of POST /api/scrape/start."""
    series: list[str] = Field(default_factory=list, description="Series to download, e.g. ['38_series']. Empty = use the recommended series.")
    max_files: Optional[int] = Field(None, description="Cap files per series (useful for a quick test run).")
    auto_index: bool = Field(True, description="Automatically index each series into the vector store once its crawl finishes.")


@router.get("/series", response_model=list[SeriesInfo])
async def list_series():
    """GET /api/scrape/series - every 3GPP series the crawler can download, with labels and the
    recommended one flagged. Requires `reference.view`.
    """
    return [
        SeriesInfo(series=s, label=SERIES_LABELS.get(s, s), recommended=(s == RECOMMENDED_SERIES))
        for s in ALL_SERIES
    ]


async def _get_active_job(db: AsyncSession, series: str) -> Optional[SpecCrawlJob]:
    """The newest crawl job for `series` that is still pending/crawling/indexing, if any."""
    result = await db.execute(
        select(SpecCrawlJob).where(SpecCrawlJob.series == series, SpecCrawlJob.status.in_(ACTIVE_STATUSES))
        .order_by(SpecCrawlJob.created_at.desc())
    )
    return result.scalars().first()


async def _create_job_if_not_active(db: AsyncSession, series: str) -> tuple[SpecCrawlJob, bool]:
    """Returns (job, created). If an active job already exists for this
    series, returns it with created=False and starts nothing new. This is
    the check-then-create sequence the per-series lock protects from
    racing with itself -- see module docstring."""
    async with _lock_for(series):
        existing = await _get_active_job(db, series)
        if existing:
            return existing, False

        job = SpecCrawlJob(series=series, status="pending", current_operation="discovering",
                            started_at=_utcnow())
        db.add(job)
        try:
            await db.flush()
        except IntegrityError:
            # Belt-and-suspenders: the DB-level unique index caught a race
            # the in-process lock couldn't (e.g. a second worker process).
            # Roll back our attempted insert and return the row that won.
            await db.rollback()
            existing = await _get_active_job(db, series)
            if existing:
                return existing, False
            raise  # genuinely unexpected -- surface it rather than hide it
        return job, True


@router.post("/start")
async def start_crawl(
    body: StartCrawlRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    """POST /api/scrape/start - start a crawl (download + optional auto-index) for each requested
    series, skipping series that already have an active job. 400 for an unknown series. Requires
    `reference.manage`.
    """
    series_list = body.series or [RECOMMENDED_SERIES]
    invalid = [s for s in series_list if s not in ALL_SERIES]
    if invalid:
        raise HTTPException(400, f"Unknown series: {invalid}")

    started, already_running = [], []
    for series in series_list:
        job, created = await _create_job_if_not_active(db, series)
        await db.commit()
        if created:
            started.append({"id": job.id, "series": series})
        else:
            already_running.append({"id": job.id, "series": series, "status": job.status})

    for j in started:
        background_tasks.add_task(_run_crawl_and_index_job, j["id"], j["series"], body.max_files, body.auto_index)

    if started and not already_running:
        message = "Crawl started"
    elif started and already_running:
        message = f"{len(started)} crawl(s) started; {len(already_running)} series already had an active job"
    else:
        message = "A crawl for the requested series is already running"

    return {
        "message": message,
        "jobs": started,
        "already_running": already_running,
    }


@router.post("/jobs/{job_id}/retry")
async def retry_job(job_id: int, background_tasks: BackgroundTasks, db: AsyncSession = Depends(get_db)):
    """Retry a finished/failed job. Creates exactly one new job -- routed
    through the same _create_job_if_not_active check-then-create sequence
    as /start, so a retry can never itself create a duplicate active job
    (e.g. if the same series was independently restarted from the main
    "Download & Index" button moments earlier)."""
    result = await db.execute(select(SpecCrawlJob).where(SpecCrawlJob.id == job_id))
    job = result.scalar_one_or_none()
    if not job:
        raise HTTPException(404, "Job not found")
    if job.status in ACTIVE_STATUSES:
        raise HTTPException(409, {
            "code": "CRAWL_ALREADY_RUNNING",
            "message": f"Job {job_id} for {job.series} is still {job.status}; wait for it to finish before retrying.",
            "job_id": job.id,
        })

    series = job.series  # read before any commit/rollback can expire `job`
    new_job, created = await _create_job_if_not_active(db, series)
    new_job_id = new_job.id
    await db.commit()
    if not created:
        return {
            "message": f"A crawl for {series} is already running.",
            "existing_job_id": new_job_id,
            "series": series,
        }

    background_tasks.add_task(_run_crawl_and_index_job, new_job_id, series, None, True)
    return {"message": "Retry started", "job_id": new_job_id, "series": series}


@router.get("/jobs")
async def list_jobs(db: AsyncSession = Depends(get_db)):
    """GET /api/scrape/jobs - the 40 most recent crawl jobs. Requires `reference.view`."""
    result = await db.execute(select(SpecCrawlJob).order_by(SpecCrawlJob.created_at.desc()).limit(40))
    jobs = result.scalars().all()
    return {"jobs": [_job_to_dict(j) for j in jobs]}


@router.get("/jobs/{job_id}")
async def get_job(job_id: int, db: AsyncSession = Depends(get_db)):
    """GET /api/scrape/jobs/{job_id} - one crawl job with live progress; 404 if unknown."""
    result = await db.execute(select(SpecCrawlJob).where(SpecCrawlJob.id == job_id))
    job = result.scalar_one_or_none()
    if not job:
        raise HTTPException(404, "Job not found")
    return _job_to_dict(job)


def _file_to_dict(f: SpecCrawlFile) -> dict:
    """API shape of one crawled file row."""
    return {
        "id": f.id, "filename": f.filename, "relative_path": f.relative_path,
        "series": f.series, "status": f.status,
        "bytes_downloaded": f.bytes_downloaded, "file_size": f.file_size,
        "retry_count": f.retry_count, "error_code": f.error_code, "error_message": f.error_message,
        "checksum": f.checksum, "indexed": f.indexed, "chunks_indexed": f.chunks_indexed,
        "started_at": f.started_at.isoformat() if f.started_at else None,
        "completed_at": f.completed_at.isoformat() if f.completed_at else None,
    }


@router.get("/jobs/{job_id}/files")
async def list_job_files(
    job_id: int,
    status: Optional[str] = Query(None, description="Filter by file status."),
    search: Optional[str] = Query(None, description="Filter by filename substring."),
    sort: str = Query("filename", pattern="^(filename|status|newest|failed_first)$"),
    page: int = Query(0, ge=0),
    page_size: int = Query(50, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
):
    """File-level progress list for a job -- paginated so a series with
    hundreds of files never renders (or queries) them all at once. See
    core.database.SpecCrawlFile."""
    stmt = select(SpecCrawlFile).where(SpecCrawlFile.job_id == job_id)
    if status:
        stmt = stmt.where(SpecCrawlFile.status == status)
    if search:
        stmt = stmt.where(SpecCrawlFile.filename.like(f"%{search}%"))

    if sort == "filename":
        stmt = stmt.order_by(SpecCrawlFile.filename)
    elif sort == "status":
        stmt = stmt.order_by(SpecCrawlFile.status, SpecCrawlFile.filename)
    elif sort == "newest":
        stmt = stmt.order_by(SpecCrawlFile.created_at.desc())
    elif sort == "failed_first":
        stmt = stmt.order_by((SpecCrawlFile.status != "failed"), SpecCrawlFile.filename)

    count_stmt = select(func.count()).select_from(stmt.subquery())
    total = (await db.execute(count_stmt)).scalar_one()

    stmt = stmt.offset(page * page_size).limit(page_size)
    rows = (await db.execute(stmt)).scalars().all()

    counts_stmt = select(SpecCrawlFile.status, func.count()).where(SpecCrawlFile.job_id == job_id).group_by(SpecCrawlFile.status)
    counts = dict((await db.execute(counts_stmt)).all())

    return {
        "total": total, "page": page, "page_size": page_size,
        "counts_by_status": counts,
        "files": [_file_to_dict(f) for f in rows],
    }


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(job_id: int, db: AsyncSession = Depends(get_db)):
    """Cooperatively cancel an active job. The crawl finishes its current
    file write (never corrupts a partial download) and then stops
    scheduling new work -- see ingestion/gpp_crawl.py's request_cancel().
    Already-downloaded and already-indexed files are preserved; a
    cancelled job can be retried later and will skip everything it
    already completed (see the existing-file-skip / idempotent indexing
    behavior)."""
    result = await db.execute(select(SpecCrawlJob).where(SpecCrawlJob.id == job_id))
    job = result.scalar_one_or_none()
    if not job:
        raise HTTPException(404, "Job not found")
    if job.status not in ACTIVE_STATUSES:
        raise HTTPException(409, {
            "code": "JOB_NOT_ACTIVE",
            "message": f"Job {job_id} is {job.status}, not currently running.",
        })
    signalled = request_cancel(job_id)
    if not signalled:
        # The job is marked active in the DB but isn't actually mid-crawl
        # in this process (e.g. backend restarted after it started but
        # before it finished) -- mark it cancelled directly rather than
        # leaving it stuck "active" forever with nothing to signal.
        job.status = "cancelled"
        job.current_operation = "cancelled"
        job.completed_at = _utcnow()
        await db.commit()
        return {"message": "Job marked cancelled (was not actively running in this process).", "job_id": job_id}
    return {"message": "Cancellation requested. The job will stop after finishing its current file.", "job_id": job_id}


@router.post("/jobs/{job_id}/pause")
async def pause_job(job_id: int, db: AsyncSession = Depends(get_db)):
    """Alias of /cancel for a "Pause" button -- same cooperative stop
    mechanism (the crawl finishes its current file, then stops), and the
    job is equally resumable afterwards via /jobs/{job_id}/resume. Kept as
    a separate, explicitly-named action (rather than only /cancel) so the
    frontend can offer distinct Pause/Stop affordances that behave
    identically underneath, matching the platform's other job-control
    endpoints (CSARAS dataset jobs, GITAS SEP bulk runs, ETSI Corpus
    import jobs)."""
    return await cancel_job(job_id, db)


@router.post("/jobs/{job_id}/resume")
async def resume_job(job_id: int, background_tasks: BackgroundTasks, db: AsyncSession = Depends(get_db)):
    """Alias of /retry for a "Continue"/"Resume" button. A crawl job
    already skips files it already downloaded/indexed (see this module's
    docstring and ingestion/gpp_crawl.py) -- so "retry" and "resume" are
    the same operation here: start a fresh job for the same series that
    picks up wherever the paused/cancelled/failed job left off."""
    return await retry_job(job_id, background_tasks, db)


def _job_to_dict(j: SpecCrawlJob) -> dict:
    """API shape of a crawl job. `percent` and `total_files` stay null until the real file total is
    known, so the UI shows an indeterminate bar instead of an invented percentage.
    """
    percent = None
    if j.total_files:
        denom = j.total_files
        percent = round(100 * min(j.files_downloaded + j.files_skipped, denom) / denom, 1) if denom else None
    return {
        "id": j.id, "series": j.series, "status": j.status,
        "current_operation": j.current_operation,
        "files_downloaded": j.files_downloaded, "files_skipped": j.files_skipped,
        "files_failed": j.files_failed, "files_discovered": j.files_discovered,
        "total_files": j.total_files,  # null until the crawl is actually finished -- never fabricated
        "percent": percent,            # null unless total_files is known -- frontend must show indeterminate, not a fake %
        "bytes_downloaded": j.bytes_downloaded,
        "chunks_indexed": j.chunks_indexed, "current_path": j.current_path,
        "error": j.error_message, "error_code": j.error_code, "retryable": j.retryable,
        "started_at": j.started_at.isoformat() if j.started_at else None,
        "completed_at": j.completed_at.isoformat() if j.completed_at else None,
    }


async def _run_crawl_and_index_job(job_id: int, series: str, max_files: Optional[int], auto_index: bool):
    """Background task: crawl one series, then (optionally) index it.
    Uses its own DB session -- see _BgSession note above."""
    out_root = default_out_root()

    async def _update(status: str, **fields):
        """Persist a status change (and any counters) for this job in its own short DB session; stamps
        completed_at on done/failed.
        """
        async with _BgSession() as session:
            result = await session.execute(select(SpecCrawlJob).where(SpecCrawlJob.id == job_id))
            job = result.scalar_one_or_none()
            if not job:
                return
            job.status = status
            for k, v in fields.items():
                setattr(job, k, v)
            if status in ("done", "failed"):
                job.completed_at = _utcnow()
            await session.commit()

    async def on_file_event(evt: dict):
        """Upsert a SpecCrawlFile row for this (job_id, relative_path).
        Best-effort by design -- see gpp_crawl.py's _download_file, which
        already wraps every call to this in its own try/except so a
        failure here can never fail the crawl itself."""
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert
        async with _BgSession() as session:
            values = dict(
                job_id=job_id, series=series, filename=evt["filename"],
                relative_path=evt["relative_path"], url=evt["url"], status=evt["status"],
                local_path=evt.get("local_path"), bytes_downloaded=evt.get("bytes_downloaded"),
                file_size=evt.get("file_size"), retry_count=evt.get("retry_count", 0),
                error_code=evt.get("error_code"), error_message=evt.get("error_message"),
                checksum="unavailable",  # 3GPP's archive exposes no checksum -- never fabricated
            )
            if evt["status"] == "downloading" and evt.get("retry_count", 0) == 0:
                values["started_at"] = _utcnow()
            if evt["status"] in ("downloaded", "skipped", "failed"):
                values["completed_at"] = _utcnow()
            stmt = sqlite_insert(SpecCrawlFile).values(**values)
            update_cols = {k: v for k, v in values.items() if k not in ("job_id", "relative_path")}
            stmt = stmt.on_conflict_do_update(
                index_elements=["job_id", "relative_path"], set_=update_cols,
            )
            await session.execute(stmt)
            await session.commit()

    await _update("crawling", current_operation="discovering")

    last_flush = 0.0

    async def on_progress(stats, current_path):
        """Crawler progress callback, throttled to about one DB write per second."""
        nonlocal last_flush
        loop = asyncio.get_event_loop()
        now = loop.time()
        if now - last_flush < 1.0:  # throttle DB writes to ~1/sec
            return
        last_flush = now
        await _update(
            "crawling",
            current_operation="downloading",
            files_downloaded=stats.files_downloaded,
            files_skipped=stats.files_skipped_existing,
            files_failed=stats.errors,
            files_discovered=stats.files_found,
            bytes_downloaded=stats.bytes_downloaded,
            current_path=str(current_path)[:500],
        )

    try:
        stats = await crawl_series(series, out_root, job_id=job_id, max_files=max_files,
                                    on_progress=on_progress, on_file_event=on_file_event)
    except Exception as e:
        logger.error(f"[crawl job={job_id} series={series}] failed: {e}", exc_info=True)
        await _update("failed", current_operation="failed", error_message=str(e),
                      error_code="CRAWL_FAILED", retryable=True)
        return

    if stats.cancelled:
        await _update(
            "cancelled", current_operation="cancelled",
            files_downloaded=stats.files_downloaded, files_skipped=stats.files_skipped_existing,
            files_failed=stats.errors, files_discovered=stats.files_found,
            bytes_downloaded=stats.bytes_downloaded,
        )
        logger.info(f"[crawl job={job_id} series={series}] cancelled by user request")
        return

    # The crawl has now actually finished, so the true total is known --
    # this is the one point where total_files is allowed to be set,
    # per the "never fabricate a total" requirement.
    await _update(
        "indexing" if auto_index else "done",
        current_operation="indexing" if auto_index else "completed",
        files_downloaded=stats.files_downloaded,
        files_skipped=stats.files_skipped_existing,
        files_failed=stats.errors,
        files_discovered=stats.files_found,
        total_files=stats.files_found,
        bytes_downloaded=stats.bytes_downloaded,
    )

    if not auto_index:
        return

    try:
        idx_stats = await build_corpus_from_crawl(out_root, include=series)
    except Exception as e:
        logger.error(f"[crawl job {job_id}] indexing series {series} failed: {e}", exc_info=True)
        await _update("failed", current_operation="failed",
                      error_message=f"Specifications downloaded successfully, but indexing failed: {e}",
                      error_code="INDEXING_FAILED", retryable=True)
        return

    await _update("done", current_operation="completed", chunks_indexed=idx_stats.chunks_indexed)


# ══════════════════════════════════════════════════════════════════════════
# Batches (v0.3 Part C) — multi-series corpus operations
# ══════════════════════════════════════════════════════════════════════════
#
# A batch groups several per-series SpecCrawlJob rows (the existing,
# already-tested crawl implementation -- NOT reimplemented here) into one
# trackable operation. See core.database.SpecCrawlBatch's docstring for
# the owned-vs-attached distinction that keeps this compatible with the
# existing single-series duplicate-job protection.

BATCH_ACTIVE_STATUSES = ("queued", "running")

# Guards the whole "create batch row + attach/create each child job" step
# as one unit, exactly like _series_locks does for a single series --
# without this, two rapid clicks on "Start batch" with the same series
# selection could create two separate SpecCrawlBatch rows (each correctly
# attaching to the same underlying per-series jobs, so no duplicate CRAWL
# would happen -- the per-series lock+index still guarantee that -- but
# the user would see two redundant batch history entries for one click).
_batch_creation_lock = asyncio.Lock()


class StartBatchRequest(BaseModel):
    """Body of POST /api/scrape/batches - several series crawled as one tracked batch with bounded
    concurrency.
    """
    series: list[str] = Field(..., min_length=1, description="Series to download as one batch operation.")
    max_files: Optional[int] = None
    auto_index: bool = True
    max_concurrent: Optional[int] = Field(None, description="Override MAX_CONCURRENT_SERIES_JOBS for this batch.")


async def _attach_or_create_batch_child(db: AsyncSession, series: str) -> tuple[int, bool]:
    """Returns (job_id, owned). owned=True means this batch created the
    job and is responsible for running its crawl; owned=False means an
    active job for this series already existed and the batch merely
    observes it (see SpecCrawlBatch docstring) -- never a duplicate crawl
    either way, reusing the exact same _create_job_if_not_active dedup
    path /start uses."""
    job, created = await _create_job_if_not_active(db, series)
    return job.id, created


@router.post("/batches")
async def start_batch(body: StartBatchRequest, background_tasks: BackgroundTasks, db: AsyncSession = Depends(get_db)):
    """POST /api/scrape/batches - create a batch over several series (attaching to already-running
    series jobs instead of duplicating them) and run it in the background. Requires
    `reference.manage`.
    """
    invalid = [s for s in body.series if s not in ALL_SERIES]
    if invalid:
        raise HTTPException(400, f"Unknown series: {invalid}")

    async with _batch_creation_lock:
        batch = SpecCrawlBatch(
            series_requested=json.dumps(body.series),
            max_concurrent=body.max_concurrent or settings.MAX_CONCURRENT_SERIES_JOBS,
            status="queued", started_at=_utcnow(),
        )
        db.add(batch)
        await db.flush()

        child_ids: list[int] = []
        owned_ids: list[int] = []
        for series in body.series:
            job_id, owned = await _attach_or_create_batch_child(db, series)
            child_ids.append(job_id)
            if owned:
                owned_ids.append(job_id)
                result = await db.execute(select(SpecCrawlJob).where(SpecCrawlJob.id == job_id))
                job = result.scalar_one()
                job.batch_id = batch.id

        batch.child_job_ids = json.dumps(child_ids)
        await db.commit()
        batch_id = batch.id

    background_tasks.add_task(_run_batch, batch_id, owned_ids, body.max_files, body.auto_index)
    return {"message": "Batch started", "batch_id": batch_id, "child_job_ids": child_ids}


async def _run_batch(batch_id: int, owned_job_ids: list[int], max_files: Optional[int], auto_index: bool):
    """Runs every job THIS batch owns, at most `max_concurrent` at a time
    (a semaphore, not a naive gather -- extra owned series queue and start
    automatically as a slot frees, per the concurrency-limit requirement).
    Attached (not-owned) jobs are never (re)run here -- their own
    background task from whenever they were originally started is already
    driving them; this only waits for them to reach a terminal status so
    the batch's overall completion reflects their real state too."""
    async with _BgSession() as session:
        result = await session.execute(select(SpecCrawlBatch).where(SpecCrawlBatch.id == batch_id))
        batch = result.scalar_one_or_none()
        if not batch:
            return
        max_concurrent = batch.max_concurrent
        child_ids = json.loads(batch.child_job_ids)
        batch.status = "running"
        await session.commit()

    semaphore = asyncio.Semaphore(max(1, max_concurrent))
    owned_set = set(owned_job_ids)

    async def _run_owned(job_id: int, series: str):
        """Run one series job owned by this batch inside the concurrency semaphore, re-checking first
        that it was not cancelled while waiting for its slot.
        """
        async with semaphore:
            # Re-check before actually starting: a cancel_batch call could
            # have marked this job "cancelled" directly (its fallback path
            # for a job that hadn't started an actual crawl_series task
            # yet -- see cancel_job's docstring) while this coroutine was
            # still waiting for its semaphore slot. Without this check, the
            # crawl would run anyway and its own completion update would
            # silently overwrite the cancellation with done/failed.
            async with _BgSession() as session:
                result = await session.execute(select(SpecCrawlJob).where(SpecCrawlJob.id == job_id))
                job = result.scalar_one_or_none()
                if not job or job.status not in ("pending",):
                    return  # already cancelled/handled before its turn came up
            await _run_crawl_and_index_job(job_id, series, max_files, auto_index)

    async def _wait_attached(job_id: int):
        """Wait (polling every 2 s) for a series job this batch attached to but does not own."""
        # Poll rather than block on anything we don't control -- the job's
        # own background task updates it independently.
        while True:
            async with _BgSession() as session:
                result = await session.execute(select(SpecCrawlJob).where(SpecCrawlJob.id == job_id))
                job = result.scalar_one_or_none()
                if not job or job.status not in ACTIVE_STATUSES:
                    return
            await asyncio.sleep(2.0)

    tasks = []
    async with _BgSession() as session:
        result = await session.execute(select(SpecCrawlJob).where(SpecCrawlJob.id.in_(child_ids)))
        jobs_by_id = {j.id: j for j in result.scalars().all()}

    for job_id in child_ids:
        job = jobs_by_id.get(job_id)
        if not job:
            continue
        if job_id in owned_set:
            tasks.append(asyncio.create_task(_run_owned(job_id, job.series)))
        else:
            tasks.append(asyncio.create_task(_wait_attached(job_id)))

    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)

    # Recompute final batch status from real child state -- never assume.
    async with _BgSession() as session:
        result = await session.execute(select(SpecCrawlBatch).where(SpecCrawlBatch.id == batch_id))
        batch = result.scalar_one_or_none()
        if not batch:
            return
        result = await session.execute(select(SpecCrawlJob).where(SpecCrawlJob.id.in_(child_ids)))
        children = result.scalars().all()

        if batch.status == "cancelled":
            pass  # a cancel request during the run wins regardless of how children landed
        elif any(c.status == "cancelled" for c in children) and not any(c.status == "done" for c in children):
            batch.status = "cancelled"
        elif all(c.status == "done" for c in children):
            batch.status = "completed"
        elif any(c.status == "done" for c in children):
            batch.status = "completed_with_errors"
        else:
            batch.status = "failed"
            batch.error_message = "No series in this batch completed successfully."
        batch.completed_at = _utcnow()
        await session.commit()


def _batch_to_dict(batch: SpecCrawlBatch, children: list[SpecCrawlJob]) -> dict:
    """API shape of a batch with totals aggregated from its child jobs; `files_total` stays null while
    any running child has not reported its total.
    """
    series_total = len(children)
    series_completed = sum(1 for c in children if c.status == "done")
    series_failed = sum(1 for c in children if c.status in ("failed", "cancelled"))
    files_processed = sum(c.files_downloaded + c.files_skipped for c in children)
    files_total = sum(c.total_files for c in children if c.total_files is not None) or None
    # files_total is None (not fabricated as 0) unless every child that's
    # going to report a total already has -- an in-progress series' true
    # file count isn't known yet, so a partial sum would understate the
    # real total and mislead the "X / Y files" display.
    if files_total is not None and any(c.total_files is None and c.status in ACTIVE_STATUSES for c in children):
        files_total = None
    chunks_indexed = sum(c.chunks_indexed for c in children)

    return {
        "id": batch.id, "status": batch.status,
        "series_requested": json.loads(batch.series_requested),
        "max_concurrent": batch.max_concurrent,
        "series_completed": series_completed, "series_total": series_total, "series_failed": series_failed,
        "files_processed": files_processed, "files_total": files_total,
        "chunks_indexed": chunks_indexed,
        "error": batch.error_message,
        "started_at": batch.started_at.isoformat() if batch.started_at else None,
        "completed_at": batch.completed_at.isoformat() if batch.completed_at else None,
        "children": [_job_to_dict(c) for c in children],
    }


@router.get("/batches")
async def list_batches(db: AsyncSession = Depends(get_db)):
    """GET /api/scrape/batches - the 20 most recent crawl batches with child-job totals. Requires
    `reference.view`.
    """
    result = await db.execute(select(SpecCrawlBatch).order_by(SpecCrawlBatch.created_at.desc()).limit(20))
    batches = result.scalars().all()
    out = []
    for b in batches:
        child_ids = json.loads(b.child_job_ids)
        result = await db.execute(select(SpecCrawlJob).where(SpecCrawlJob.id.in_(child_ids))) if child_ids else None
        children = result.scalars().all() if result else []
        out.append(_batch_to_dict(b, children))
    return {"batches": out}


@router.get("/batches/{batch_id}")
async def get_batch(batch_id: int, db: AsyncSession = Depends(get_db)):
    """GET /api/scrape/batches/{batch_id} - one batch; 404 if unknown."""
    result = await db.execute(select(SpecCrawlBatch).where(SpecCrawlBatch.id == batch_id))
    batch = result.scalar_one_or_none()
    if not batch:
        raise HTTPException(404, "Batch not found")
    child_ids = json.loads(batch.child_job_ids)
    result = await db.execute(select(SpecCrawlJob).where(SpecCrawlJob.id.in_(child_ids))) if child_ids else None
    children = result.scalars().all() if result else []
    return _batch_to_dict(batch, children)


@router.post("/batches/{batch_id}/cancel")
async def cancel_batch(batch_id: int, db: AsyncSession = Depends(get_db)):
    """Stops queued-but-not-started owned children from starting, requests
    cancellation of active owned children (same cooperative mechanism as a
    single-job cancel -- finishes the current file, never corrupts a
    partial download), and leaves already-completed series and any
    attached (not-owned) jobs alone -- see cancel_job's docstring for why
    a batch never touches a job it doesn't own."""
    result = await db.execute(select(SpecCrawlBatch).where(SpecCrawlBatch.id == batch_id))
    batch = result.scalar_one_or_none()
    if not batch:
        raise HTTPException(404, "Batch not found")
    if batch.status not in BATCH_ACTIVE_STATUSES:
        raise HTTPException(409, {"code": "BATCH_NOT_ACTIVE", "message": f"Batch {batch_id} is {batch.status}, not currently running."})

    child_ids = json.loads(batch.child_job_ids)
    result = await db.execute(select(SpecCrawlJob).where(SpecCrawlJob.id.in_(child_ids), SpecCrawlJob.batch_id == batch_id))
    owned_children = result.scalars().all()
    for job in owned_children:
        if job.status in ACTIVE_STATUSES:
            if not request_cancel(job.id):
                # Not actively running in this process (e.g. still
                # "pending" and hasn't been picked up by its semaphore
                # slot yet) -- mark it cancelled directly.
                job.status = "cancelled"
                job.current_operation = "cancelled"
                job.completed_at = _utcnow()

    batch.status = "cancelled"
    batch.completed_at = _utcnow()
    await db.commit()
    return {"message": "Batch cancellation requested.", "batch_id": batch_id}


@router.post("/batches/{batch_id}/retry")
async def retry_batch(batch_id: int, background_tasks: BackgroundTasks, db: AsyncSession = Depends(get_db)):
    """Resume Batch: for every child series that didn't finish
    successfully (failed/cancelled), start a fresh job for that series
    only -- routed through the same duplicate-safe
    _create_job_if_not_active path /start and /batches use, so a rapid
    double-click on Resume can't create two jobs for the same series
    either. Series that already completed are left completely alone."""
    result = await db.execute(select(SpecCrawlBatch).where(SpecCrawlBatch.id == batch_id))
    batch = result.scalar_one_or_none()
    if not batch:
        raise HTTPException(404, "Batch not found")
    if batch.status in BATCH_ACTIVE_STATUSES:
        raise HTTPException(409, {"code": "BATCH_ALREADY_RUNNING", "message": f"Batch {batch_id} is already {batch.status}."})

    child_ids = json.loads(batch.child_job_ids)
    result = await db.execute(select(SpecCrawlJob).where(SpecCrawlJob.id.in_(child_ids)))
    children = {c.id: c for c in result.scalars().all()}

    new_child_ids: list[int] = []
    new_owned: list[int] = []
    async with _batch_creation_lock:
        for job_id in child_ids:
            job = children.get(job_id)
            if job and job.status == "done":
                new_child_ids.append(job_id)  # untouched, already succeeded
                continue
            series = job.series if job else None
            if not series:
                continue
            new_job_id, owned = await _attach_or_create_batch_child(db, series)
            new_child_ids.append(new_job_id)
            if owned:
                new_owned.append(new_job_id)
                result = await db.execute(select(SpecCrawlJob).where(SpecCrawlJob.id == new_job_id))
                new_job = result.scalar_one()
                new_job.batch_id = batch_id

        batch.child_job_ids = json.dumps(new_child_ids)
        batch.status = "queued"
        batch.error_message = None
        batch.completed_at = None
        await db.commit()

    background_tasks.add_task(_run_batch, batch_id, new_owned, None, True)
    return {"message": "Batch resumed", "batch_id": batch_id, "child_job_ids": new_child_ids}


# ══════════════════════════════════════════════════════════════════════════
# v0.4.2 — index a LOCAL 3GPP archive folder (zips / .docx / 3gpp_*.json)
# ══════════════════════════════════════════════════════════════════════════
# Runs inside the backend process, so the embedded vector store isn't
# opened by two processes at once (index_3gpp.ps1 uses this when the app is
# running, and the CLI `python -m ingestion.spec_corpus` when it isn't).

class LocalIndexRequest(BaseModel):
    """Body of POST /api/scrape/index-local - index an already-downloaded/extracted 3GPP archive
    folder.
    """
    path: Optional[str] = Field(default=None, description="Folder to index (default THREEGPP_ARCHIVE_DIR)")
    all_versions: bool = Field(default=False, description="Index every version, not only the latest per spec")
    include: Optional[str] = None
    exclude: Optional[str] = None


_local_index_state: dict = {"status": "idle"}
_local_index_task: Optional[asyncio.Task] = None


@router.post("/index-local")
async def start_local_index(body: LocalIndexRequest):
    """POST /api/scrape/index-local - index a local 3GPP archive folder (3gpp_*.json, .docx, .zip) into
    the Standards Corpus vector store in the background (what index_3gpp.cmd calls when the app is
    running). 409 if a run is already in progress, 400 for a missing folder. Requires
    `reference.manage`.
    """
    global _local_index_task
    from pathlib import Path as _Path
    if _local_index_task is not None and not _local_index_task.done():
        raise HTTPException(409, "A local 3GPP indexing run is already in progress.")
    root = _Path(body.path or settings.THREEGPP_ARCHIVE_DIR).expanduser()
    if not root.is_dir():
        raise HTTPException(400, f"Folder not found: {root}")

    _local_index_state.clear()
    _local_index_state.update(status="running", path=str(root), files_indexed=0, chunks_indexed=0,
                              current_file=None, started_at=_utcnow().isoformat(), error=None)

    def _progress(st, path):
        """Progress callback: copy the indexer's counters into the shared status dict."""
        _local_index_state.update(files_indexed=st.files_indexed, chunks_indexed=st.chunks_indexed,
                                  current_file=path.name)

    async def _run():
        """The background indexing run; records the final counters or the failure in the status dict."""
        try:
            st = await build_corpus_from_crawl(root, include=body.include, exclude=body.exclude,
                                               latest_only=not body.all_versions, progress=_progress)
            _local_index_state.update(
                status="done", files_seen=st.files_seen, files_indexed=st.files_indexed,
                chunks_indexed=st.chunks_indexed, skipped_already_indexed=st.files_skipped_already_indexed,
                skipped_legacy_doc=st.files_skipped_legacy_doc, skipped_no_document=st.files_skipped_no_document,
                skipped_error=st.files_skipped_error, errors=st.errors[:20], finished_at=_utcnow().isoformat())
        except Exception as e:
            logger.error(f"Local 3GPP indexing failed: {e}", exc_info=True)
            _local_index_state.update(status="failed", error=f"{type(e).__name__}: {e}")

    _local_index_task = asyncio.create_task(_run())
    return dict(_local_index_state)


@router.get("/index-local/status")
async def local_index_status():
    """GET /api/scrape/index-local/status - progress/result of the current or last local indexing run."""
    return dict(_local_index_state)
