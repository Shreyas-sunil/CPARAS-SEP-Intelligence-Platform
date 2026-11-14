"""
Dataset Ingestion API
Triggers autonomous crawling and indexing of public telecom data sources.
"""
import asyncio
import logging
from datetime import datetime
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from core.database import get_db, IngestionJob
from ingestion.pipeline import run_ingestion_pipeline
from core.timeutil import utcnow as _utcnow

logger = logging.getLogger(__name__)
router = APIRouter()

AVAILABLE_SOURCES = {
    "etsi_ipr": "ETSI IPR Database — public declaration filings",
    "3gpp_specs": "3GPP specification index (titles and abstracts)",
    "wipo_patentscope": "WIPO PatentScope public patent metadata",
    "itu_references": "ITU-T and ITU-R recommendation index",
    "frand_cases": "Public FRAND litigation case summaries",
}

class IngestionRequest(BaseModel):
    """Body of POST /api/datasets/start: which AVAILABLE_SOURCES to ingest and the per-source record
    cap.
    """
    sources: list[str]
    max_records: int = 500

@router.get("/sources")
async def list_sources():
    """GET /api/datasets/sources - the public IPR/standards sources the legacy ingestion pipeline can
    fetch. Requires `reference.view`.
    """
    return {"sources": AVAILABLE_SOURCES}

@router.post("/start")
async def start_ingestion(
    body: IngestionRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    """POST /api/datasets/start - create one IngestionJob per requested source and run the ingestion
    pipeline (ingestion/pipeline.py) as a background task. 400 for an unknown source. Requires
    `reference.manage` (it writes to the shared vector store).
    """
    invalid = [s for s in body.sources if s not in AVAILABLE_SOURCES]
    if invalid:
        raise HTTPException(400, f"Unknown sources: {invalid}")

    jobs = []
    for source in body.sources:
        job = IngestionJob(source=source, status="pending", started_at=_utcnow())
        db.add(job)
        await db.flush()
        jobs.append({"id": job.id, "source": source})

    await db.commit()

    background_tasks.add_task(run_ingestion_pipeline, [j["id"] for j in jobs], body.sources, body.max_records)

    return {"message": "Ingestion started", "jobs": jobs}

@router.get("/jobs")
async def list_jobs(db: AsyncSession = Depends(get_db)):
    """GET /api/datasets/jobs - the 20 most recent ingestion jobs with their counters. Requires
    `reference.view`.
    """
    result = await db.execute(select(IngestionJob).order_by(IngestionJob.created_at.desc()).limit(20))
    jobs = result.scalars().all()
    return {"jobs": [
        {"id": j.id, "source": j.source, "status": j.status,
         "records_fetched": j.records_fetched, "records_indexed": j.records_indexed,
         "error": j.error_message, "started_at": j.started_at.isoformat() if j.started_at else None}
        for j in jobs
    ]}

@router.get("/jobs/{job_id}")
async def get_job(job_id: int, db: AsyncSession = Depends(get_db)):
    """GET /api/datasets/jobs/{job_id} - one ingestion job, 404 if unknown. Requires `reference.view`."""
    result = await db.execute(select(IngestionJob).where(IngestionJob.id == job_id))
    job = result.scalar_one_or_none()
    if not job:
        raise HTTPException(404, "Job not found")
    return {"id": job.id, "source": job.source, "status": job.status,
            "records_fetched": job.records_fetched, "records_indexed": job.records_indexed,
            "error": job.error_message}
