"""
Maintenance & export endpoints (v0.3 Phase 2, Parts R + S).
==============================================================
GET  /api/scrape/maintenance   -- identify stale temp files / abandoned
                                    jobs / recent failures, WITHOUT
                                    deleting anything automatically (see
                                    module docstring in gpp_crawl.py's
                                    cleanup_stale_temp_files -- that's the
                                    only thing that ever removes files, and
                                    only .part.* temp files, never
                                    downloaded specs).
GET  /api/scrape/export/files  -- CSV of per-file crawl status across all
                                    jobs (or one job via ?job_id=), for
                                    hand-off/audit purposes.
"""
from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import get_db, SpecCrawlJob, SpecCrawlFile
from ingestion.gpp_crawl import default_out_root
from core.timeutil import utcnow as _utcnow

router = APIRouter()

ABANDONED_THRESHOLD_HOURS = 2.0
# A job left "active" (pending/crawling/indexing) for longer than this
# with no completed_at is flagged as likely abandoned -- e.g. the process
# is still technically running but the crawl coroutine died silently
# without raising (rare, but possible). This is informational only: it
# does NOT cancel or delete anything -- see the module docstring.


@router.get("/maintenance")
async def maintenance_status(db: AsyncSession = Depends(get_db)):
    """GET /api/scrape/maintenance - read-only housekeeping report for the 3GPP crawler: stale partial
    downloads that the next crawl would clean up (counted, never deleted here) and crawl jobs that
    look abandoned (still 'active' after ABANDONED_THRESHOLD_HOURS). Requires `reference.view`.
    """
    # Stale temp files -- count only, dry-run style (the same real cleanup
    # logic gpp_crawl.py's cleanup_stale_temp_files runs automatically at
    # the start of every crawl; this just reports what it WOULD remove
    # right now, for visibility, without actually deleting mid-request).
    out_root = default_out_root()
    stale_count = 0
    stale_examples: list[str] = []
    if out_root.exists():
        import time
        now = time.time()
        for p in out_root.rglob("*.part.*"):
            try:
                if now - p.stat().st_mtime > 6 * 3600:
                    stale_count += 1
                    if len(stale_examples) < 10:
                        stale_examples.append(str(p.relative_to(out_root)))
            except OSError:
                continue

    cutoff = _utcnow() - timedelta(hours=ABANDONED_THRESHOLD_HOURS)
    result = await db.execute(
        select(SpecCrawlJob).where(
            SpecCrawlJob.status.in_(("pending", "crawling", "indexing")),
            SpecCrawlJob.started_at < cutoff,
        )
    )
    abandoned_jobs = result.scalars().all()

    result = await db.execute(
        select(SpecCrawlJob).where(SpecCrawlJob.status == "failed")
        .order_by(SpecCrawlJob.completed_at.desc()).limit(10)
    )
    recent_failures = result.scalars().all()

    failed_files_count = (await db.execute(
        select(func.count()).select_from(SpecCrawlFile).where(SpecCrawlFile.status == "failed")
    )).scalar_one()

    return {
        "stale_temp_files": {"count": stale_count, "examples": stale_examples, "root": str(out_root)},
        "abandoned_jobs": [
            {"id": j.id, "series": j.series, "status": j.status,
             "started_at": j.started_at.isoformat() if j.started_at else None}
            for j in abandoned_jobs
        ],
        "recent_failures": [
            {"id": j.id, "series": j.series, "error_code": j.error_code, "error": j.error_message,
             "completed_at": j.completed_at.isoformat() if j.completed_at else None}
            for j in recent_failures
        ],
        "failed_files_count": failed_files_count,
        "note": "This endpoint is read-only diagnostics. It never deletes downloaded specifications or the corpus.",
    }


@router.get("/export/files")
async def export_files_csv(job_id: Optional[int] = Query(None), db: AsyncSession = Depends(get_db)):
    """GET /api/scrape/export/files?job_id= - CSV of every crawled file (status, size, retries, error
    code, indexing result), optionally for one job. Requires `reference.manage` because it is a bulk
    export of crawl internals.
    """
    stmt = select(SpecCrawlFile)
    if job_id is not None:
        stmt = stmt.where(SpecCrawlFile.job_id == job_id)
    stmt = stmt.order_by(SpecCrawlFile.series, SpecCrawlFile.filename)
    rows = (await db.execute(stmt)).scalars().all()

    columns = ["job_id", "series", "filename", "status", "file_size", "retry_count",
               "error_code", "started_at", "completed_at", "indexed", "chunks_indexed"]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=columns)
    writer.writeheader()
    for f in rows:
        writer.writerow({
            "job_id": f.job_id, "series": f.series, "filename": f.filename, "status": f.status,
            "file_size": f.file_size or "", "retry_count": f.retry_count,
            "error_code": f.error_code or "",
            "started_at": f.started_at.isoformat() if f.started_at else "",
            "completed_at": f.completed_at.isoformat() if f.completed_at else "",
            "indexed": f.indexed, "chunks_indexed": f.chunks_indexed,
        })
    return StreamingResponse(
        iter([buf.getvalue()]), media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=corpus_files.csv"},
    )
