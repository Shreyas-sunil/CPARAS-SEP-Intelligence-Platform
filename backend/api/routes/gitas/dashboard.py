"""
SEP Dashboard API — CPARAS Bulk
================================
Serves the CPARAS Bulk product (formerly branded "GITAS"; the internal
module path api/routes/gitas/ is unchanged) — the bulk/portfolio
dashboard (company overview, essentiality breakdowns, legal view, patent
detail pages) from data ingested via the ETSI bulk-declaration-export
Excel format (backend/etsi_bulk/).

This is additive: it does not change the existing single-declaration
PDF/CSV/XLSX pipeline exposed under /api/analysis (see api/routes/analysis.py)
or its own XLSX support (utils/xlsx_parser.py). That pipeline is for
"upload one declarant's filing, get an LLM-graded analysis back"; this
router is for "upload/browse a bulk multi-company SEP dataset and explore
it as a dashboard". Both read the same underlying research/ scoring
primitives (see etsi_bulk/scoring.py) so results are consistent between
paths, and both are mounted side by side in main.py — the platform
supports either flow depending on what was uploaded.

Endpoint map (frontend integration):
  POST /api/sep/upload                 — upload a bulk-export workbook
  GET  /api/sep/runs                   — list uploaded runs
  GET  /api/sep/filters                — distinct filter values for the sidebar
  GET  /api/sep/dashboard/overview     — KPI tiles
  GET  /api/sep/dashboard/companies    — stacked bar: top companies by essentiality
  GET  /api/sep/dashboard/timeline     — SEP declarations over time
  GET  /api/sep/dashboard/countries    — donut: SEPs by country
  GET  /api/sep/dashboard/by-release   — bar: SEPs by release
  GET  /api/sep/dashboard/portfolio    — company portfolio table
  GET  /api/sep/dashboard/heatmap      — release x domain heatmap
  GET  /api/sep/patents                — filterable patent registry (legal view table)
  GET  /api/sep/patents/{id}           — single patent detail + essentiality breakdown
  GET  /api/sep/entity/company/{name}  — company profile page

All list/dashboard endpoints accept the same filter query params, mirroring
the dashboard's sidebar filter panel:
  run_id, technology (repeatable), release (repeatable),
  company (repeatable), country (repeatable), essentiality
"""

import asyncio
import io
import json
import logging
from collections import defaultdict
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File
from fastapi.responses import StreamingResponse
from sqlalchemy import select, func, insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from core.config import settings
from utils.export_safety import content_disposition, csv_safe
from core.database import get_db, AsyncSessionLocal, SepBulkRun, SepBulkPatent
from etsi_bulk.parser import parse_bulk_workbook
from etsi_bulk.scoring import score_records_batched
from etsi_bulk.excel_export import build_enriched_workbook, export_filename
from core.timeutil import utcnow as _utcnow

logger = logging.getLogger(__name__)
router = APIRouter()

ESSENTIALITY_COLORS = {"Essential": "#00c48c", "Non-Essential": "#ff4d6d", "Potential": "#c98a2f"}


# ── Filtering helper ─────────────────────────────────────────────────────────

def _apply_filters(
    stmt,
    run_id: Optional[int],
    technology: Optional[list[str]],
    release: Optional[list[str]],
    company: Optional[list[str]],
    country: Optional[list[str]],
    essentiality: Optional[str],
    standard: Optional[list[str]] = None,
    project: Optional[list[str]] = None,
    generation: Optional[list[str]] = None,
    q: Optional[str] = None,
):
    """Add the dashboard/registry filter conditions to a SELECT over SepBulkPatent: run, technology,
    release, company, country, essentiality, standard, project, declared generation and a free-text
    search over title/reference/application/publication/DIPG id. Every filter is optional; list
    filters match any of their values.
    """
    if run_id is not None:
        stmt = stmt.where(SepBulkPatent.run_id == run_id)
    if technology:
        stmt = stmt.where(SepBulkPatent.technology.in_(technology))
    if release:
        stmt = stmt.where(SepBulkPatent.release.in_(release))
    if company:
        stmt = stmt.where(SepBulkPatent.company.in_(company))
    if country:
        stmt = stmt.where(SepBulkPatent.country_code.in_(country))
    if essentiality:
        stmt = stmt.where(SepBulkPatent.essentiality == essentiality)
    if standard:
        stmt = stmt.where(SepBulkPatent.standard_raw.in_(standard))
    if project:
        stmt = stmt.where(SepBulkPatent.project_acronym.in_(project))
    if generation:
        gen_cols = {"2G": SepBulkPatent.gen_2g, "3G": SepBulkPatent.gen_3g,
                    "4G": SepBulkPatent.gen_4g, "5G": SepBulkPatent.gen_5g,
                    "6G": SepBulkPatent.gen_6g}
        clauses = [gen_cols[g] == True for g in generation if g in gen_cols]  # noqa: E712
        if clauses:
            from sqlalchemy import or_
            stmt = stmt.where(or_(*clauses))
    if q:
        from sqlalchemy import or_
        like = f"%{q}%"
        stmt = stmt.where(or_(
            SepBulkPatent.title.like(like),
            SepBulkPatent.iprd_reference.like(like),
            SepBulkPatent.application_number.like(like),
            SepBulkPatent.publication_number.like(like),
            SepBulkPatent.dipg_id.like(like),
        ))
    return stmt


class Filters:
    """Shared query-param dependency so every endpoint accepts the same filter set."""

    def __init__(
        self,
        run_id: Optional[int] = Query(None),
        technology: Optional[list[str]] = Query(None),
        release: Optional[list[str]] = Query(None),
        company: Optional[list[str]] = Query(None),
        country: Optional[list[str]] = Query(None),
        essentiality: Optional[str] = Query(None),
        standard: Optional[list[str]] = Query(None),
        project: Optional[list[str]] = Query(None, description="Filter by ETPR_ACRONYM (3GPP working item/project)."),
        generation: Optional[list[str]] = Query(None, description="Filter by declared generation: 2G/3G/4G/5G/6G."),
        q: Optional[str] = Query(None, description="Free-text search over title/reference/application/publication/dipg_id."),
    ):
        """Query parameters shared by every dashboard and registry endpoint (FastAPI fills them from
        the URL).
        """
        self.run_id = run_id
        self.technology = technology
        self.release = release
        self.company = company
        self.country = country
        self.essentiality = essentiality
        self.standard = standard
        self.project = project
        self.generation = generation
        self.q = q

    def apply(self, stmt):
        """Apply these filters to `stmt` (see `_apply_filters`)."""
        return _apply_filters(stmt, self.run_id, self.technology, self.release, self.company, self.country,
                               self.essentiality, self.standard, self.project, self.generation, self.q)


def _row_to_dict(p: SepBulkPatent) -> dict:
    """API shape of one Bulk record for the registry and patent-detail views: source-row position,
    declaration fields, the stored essentiality result and matched clause. Read-only - nothing is
    recomputed here.
    """
    return {
        "id": p.id,
        "run_id": p.run_id,
        # Position of this record's row in the uploaded sheet: 0-based among
        # data rows, and as a spreadsheet row number (header = row 1).
        "source_row_index": p.source_row_index,
        "source_row_number": (p.source_row_index + 2) if p.source_row_index is not None else None,
        "iprd_reference": p.iprd_reference,
        "signature_date": p.signature_date,
        "company": p.company,
        "disclosure_number": p.disclosure_number,
        "family_id": p.family_id,
        "standard": p.standard_raw,
        "spec_number": p.spec_number,
        "spec_version": p.spec_version,
        "release": p.release,
        "technology": p.technology,
        "domain": p.domain,
        "patent_type": p.patent_type,
        "application_number": p.application_number,
        "publication_number": p.publication_number,
        "title": p.title,
        "illustrative_part": p.illustrative_part,
        "explicit_disclosure": p.explicit_disclosure,
        "country": p.country_raw,
        "country_code": p.country_code,
        "filing_date": p.filing_date,
        "legal_status": p.legal_status,  # null when not provided in the source file -- see frontend display, which shows "Not provided" rather than inventing a status
        "generations": {"2G": p.gen_2g, "3G": p.gen_3g, "4G": p.gen_4g, "5G": p.gen_5g, "6G": p.gen_6g},
        "essentiality": p.essentiality,
        "row_status": p.row_status,
        "row_error": p.row_error,
        "confidence": p.confidence,
        "claim_match": p.claim_match,
        "semantic_similarity": p.semantic_similarity,
        "standard_coverage": p.standard_coverage,
        "matched_standard": p.matched_standard,
        "matched_clause": p.matched_clause,
        "matched_clause_title": p.matched_clause_title,
        "matched_domain": p.matched_domain,
        "declared_family_match": p.declared_family_match,
        "matched_key_terms": json.loads(p.matched_key_terms_json) if p.matched_key_terms_json else [],
        "matched_key_terms_count": p.matched_key_terms_count,
        "total_key_terms": p.total_key_terms,
        # v0.3: fields from the "UIUX database" export variant.
        "dipg_id": p.dipg_id,
        "patt_id": p.patt_id,
        "normalized_patent_flag": p.normalized_patent,
        # ^ deliberately NOT named "normalized_patent" in the API response --
        # that name invites the false assumption that it's patent text. It's
        # a Yes/No/Error processing flag from the source file -- see
        # etsi_bulk/schema.py's note. Never used as text-matching input.
        "ess_to_project": p.ess_to_project,
        "ess_to_standard": p.ess_to_standard,
        "standard_url": p.standard_url,
        "publication_url": p.publication_url,
        "original_application_number": p.original_application_number,
        "original_publication_number": p.original_publication_number,
        "patent_text": p.patent_text,
        # ^ reserved for a future dataset with real claim/disclosure text;
        # always null for this "UIUX database" export -- see
        # core.database.SepBulkPatent's docstring for that column.
        "project": p.project_acronym,
    }


# ── Upload ────────────────────────────────────────────────────────────────────
# v0.5 large-dataset architecture: parsing is synchronous (fast — just
# reading the workbook into row dicts), but scoring + persistence run as a
# BACKGROUND TASK so the HTTP request returns immediately with a run_id the
# frontend polls (GET /runs/{run_id}/status) instead of the request itself
# blocking -- and potentially timing out -- until all 17,000 rows are
# scored. See etsi_bulk/scoring.py's `score_records_batched` docstring for
# the batched/bounded-memory scoring side of this fix.

from etsi_bulk.schema import country_code


# ── Source-row preservation ─────────────────────────────────────────────────
# Every non-blank row of the uploaded sheet is stored as its OWN record, in
# source order, identified internally by (run_id, source_row_index) -- see
# etsi_bulk/parser.py. There is deliberately NO identifier-based
# deduplication anywhere on this path: two rows that share an application
# number, publication number, DIPG id, title, company or even every single
# cell are two source rows and produce two records. Re-uploading a file
# creates a new run with its own complete set of records; it never updates
# or absorbs records belonging to an earlier run.


def _to_row_values(row: dict, run_id: int) -> dict:
    """Maps one scored (or FAILED — see etsi_bulk/scoring.py's
    `_failed_row`) record dict to SepBulkPatent column values. Score
    fields are already None on a failed row — never a fake zero — and
    `row_status`/`row_error` make that failure visible on the row itself."""
    return dict(
        run_id=run_id,
        iprd_reference=row.get("iprd_reference"),
        signature_date=row.get("signature_date"),
        company=row.get("company"),
        disclosure_number=str(row.get("disclosure_number")) if row.get("disclosure_number") is not None else None,
        family_id=str(row.get("family_id")) if row.get("family_id") is not None else None,
        standard_raw=row.get("standard_raw"),
        spec_number=str(row.get("spec_number")) if row.get("spec_number") is not None else None,
        spec_version=row.get("spec_version"),
        release=row.get("release"),
        technology=row.get("technology"),
        domain=row.get("domain"),
        patent_type=row.get("patent_type"),
        application_number=row.get("application_number"),
        publication_number=row.get("publication_number"),
        title=row.get("title"),
        illustrative_part=row.get("illustrative_part"),
        explicit_disclosure=bool(row.get("explicit_disclosure")),
        country_raw=row.get("country_raw"),
        country_code=country_code(row.get("country_raw")),
        filing_date=row.get("filing_date"),
        legal_status=row.get("legal_status"),
        gen_2g=bool(row.get("gen_2g")), gen_3g=bool(row.get("gen_3g")),
        gen_4g=bool(row.get("gen_4g")), gen_5g=bool(row.get("gen_5g")),
        gen_6g=bool(row.get("gen_6g")),
        essentiality=row.get("essentiality"),
        confidence=row.get("confidence"),
        claim_match=row.get("claim_match"),
        semantic_similarity=row.get("semantic_similarity"),
        standard_coverage=row.get("standard_coverage"),
        matched_standard=row.get("matched_standard"),
        matched_clause=row.get("matched_clause"),
        matched_clause_title=row.get("matched_clause_title"),
        matched_domain=row.get("matched_domain"),
        declared_family_match=row.get("declared_family_match"),
        matched_key_terms_json=json.dumps(row.get("matched_key_terms") or []),
        matched_key_terms_count=row.get("matched_key_terms_count"),
        total_key_terms=row.get("total_key_terms"),
        dipg_id=str(row.get("dipg_id")) if row.get("dipg_id") is not None else None,
        patt_id=str(row.get("patt_id")) if row.get("patt_id") is not None else None,
        project_acronym=row.get("project_acronym"),
        normalized_patent=row.get("normalized_patent"),
        ess_to_project=str(row.get("ess_to_project")) if row.get("ess_to_project") is not None else None,
        ess_to_standard=str(row.get("ess_to_standard")) if row.get("ess_to_standard") is not None else None,
        standard_url=row.get("standard_url"),
        publication_url=row.get("publication_url"),
        original_application_number=row.get("original_application_number"),
        original_publication_number=row.get("original_publication_number"),
        source_row_index=row.get("_source_row_index"),
        row_status="FAILED" if row.get("_row_failed") else "OK",
        row_error=row.get("_row_failure_reason"),
    )


async def _persist_batch(db: AsyncSession, run_id: int, batch: list[dict]) -> int:
    """Persist one already-scored batch: exactly one INSERT per source row,
    in order. Nothing is compared against, merged into, or skipped because
    of rows already stored (in this run or any other) -- see the
    "Source-row preservation" note above.

    The only guard is internal and identifier-free: a row whose
    (run_id, source_row_index) is already stored is not inserted a second
    time. That can only happen when a paused/failed run is resumed from a
    slightly-too-early offset, i.e. it protects against processing the
    SAME source row twice, never against two different source rows that
    happen to look alike. Commits the batch's transaction and returns the
    number of rows inserted."""
    if not batch:
        return 0

    indices = [r.get("_source_row_index") for r in batch if r.get("_source_row_index") is not None]
    already_stored: set[int] = set()
    if indices:
        result = await db.execute(
            select(SepBulkPatent.source_row_index).where(
                SepBulkPatent.run_id == run_id,
                SepBulkPatent.source_row_index.in_(indices),
            )
        )
        already_stored = {v for (v,) in result.all()}

    insert_values = [
        _to_row_values(row, run_id)
        for row in batch
        if row.get("_source_row_index") is None or row.get("_source_row_index") not in already_stored
    ]
    if insert_values:
        await db.execute(insert(SepBulkPatent), insert_values)
    await db.commit()
    return len(insert_values)


async def _stored_row_count(db: AsyncSession, run_id: int) -> int:
    """Number of source rows already persisted for this run -- the exact,
    authoritative resume point (rows are persisted strictly in source
    order, batch by batch)."""
    return (await db.execute(
        select(func.count()).select_from(SepBulkPatent).where(SepBulkPatent.run_id == run_id)
    )).scalar_one()


# ── start / stop / pause / continue ─────────────────────────────────────────
# Same cooperative-stop mechanism as etsi_corpus/pipeline.py and
# csaras/dataset_pipeline.py: an asyncio.Event per running run (keyed by
# SepBulkRun.id), checked between scored batches so a pause/stop request
# always lands on a clean, resumable checkpoint (a batch already being
# scored is always finished and persisted first, never abandoned
# mid-batch). "Pause" and "Stop" share this mechanism -- both leave the
# run at status="paused", resumable via POST /runs/{run_id}/continue.
_run_cancel_events: dict[int, asyncio.Event] = {}


_run_detail: dict[int, str] = {}   # v0.4.7: live sub-status of a processing run (shown under its progress bar)


def request_run_stop(run_id: int) -> bool:
    """Ask a running Bulk scoring task to stop after its current batch. False when the run is not
    running in this process.
    """
    ev = _run_cancel_events.get(run_id)
    if ev is None:
        return False
    ev.set()
    return True


def is_run_stop_requested(run_id: int) -> bool:
    """Whether a stop was requested for `run_id` (checked by the scorer between batches)."""
    ev = _run_cancel_events.get(run_id)
    return ev.is_set() if ev else False


def is_run_running(run_id: int) -> bool:
    """Whether a scoring task for `run_id` is active in this backend process."""
    return run_id in _run_cancel_events


# Strong references to in-flight background runs: the event loop only keeps
# weak references to tasks, so a bare asyncio.create_task() result can be
# garbage-collected while the run is still in progress.
_background_tasks: set = set()


def _spawn(coro) -> "asyncio.Task":
    """Start a background task and keep a strong reference to it until it finishes (asyncio only holds
    weak references, so an unreferenced task can be garbage-collected mid-run).
    """
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


async def recover_interrupted_runs() -> int:
    """Called once at startup. A run still marked "processing"/"pending"
    can't actually be running — this process just started — so it was
    interrupted (backend closed, crash, reboot). If every source row had
    already been stored it is marked completed; otherwise paused, so the UI
    offers Continue (which resumes from the rows already persisted) instead
    of a spinner that never finishes."""
    async with AsyncSessionLocal() as db:
        runs = (await db.execute(
            select(SepBulkRun).where(SepBulkRun.status.in_(("processing", "pending")))
        )).scalars().all()
        for run in runs:
            stored = await _stored_row_count(db, run.id)
            run.processed_rows = stored
            if run.parsed_rows and stored >= run.parsed_rows:
                run.status = "completed"
                run.completed_at = run.completed_at or _utcnow()
                run.error_message = None
            else:
                run.status = "paused"
                run.error_message = ("Interrupted because the backend stopped while this run was in progress. "
                                     "Press Continue to resume from the last saved row.")
        await db.commit()
    if runs:
        logger.warning(f"Recovered {len(runs)} interrupted CPARAS Bulk run(s) after a restart.")
    return len(runs)


async def _process_bulk_run(run_id: int, records: list[dict], resume_offset: int = 0) -> None:
    """Background task: scores `records[resume_offset:]` in bounded
    batches (see etsi_bulk/scoring.py) and persists each batch as soon as
    it's ready, updating the run's progress counters along the way. Runs
    in its own DB session (the request's session is gone once the
    endpoint has already returned). A failure in the per-row scoring path
    never lands here -- score_records_batched isolates those as FAILED
    rows -- so anything caught here is a genuine pipeline-level failure
    (e.g. the embedder itself is unavailable in a way that isn't per-row),
    and marks the WHOLE run failed rather than leaving it stuck at
    "processing" forever.

    `resume_offset`: set by POST /runs/{run_id}/continue to skip rows
    already scored+persisted by an earlier (paused/stopped/failed) call.
    `records` is always the FULL parsed file; only the scoring loop starts
    partway through it. Every record carries its own `_source_row_index`,
    and `_persist_batch` never stores the same (run, source row) twice, so
    a resume can neither lose nor double-store a source row.
    """
    remaining = records[resume_offset:]

    async with AsyncSessionLocal() as db:
        run = (await db.execute(select(SepBulkRun).where(SepBulkRun.id == run_id))).scalar_one_or_none()
        if run is None:
            logger.error(f"Bulk run {run_id} vanished before background processing started.")
            return

        _run_cancel_events[run_id] = asyncio.Event()
        run.status = "processing"
        await db.commit()

        try:
            persisted_so_far = 0
            failed = 0
            stored_total = 0
            stopped = False

            async def _progress(payload: dict) -> None:
                # Lightweight progress ping during the (often slower)
                # embedding pass, ahead of persistence -- best-effort, a
                # failure here must never abort the run.
                nonlocal run
                try:
                    if payload.get("phase") == "preparing":
                        # v0.4.7: visible progress while reference clauses are embedded (first runs only)
                        _run_detail[run_id] = (f"Preparing reference clauses "
                                               f"{payload['processed']:,} / {payload.get('total_clauses', 0):,}")
                        return
                    _run_detail.pop(run_id, None)
                    if payload.get("phase") == "embedding":
                        run.processed_rows = resume_offset + payload["processed"]
                        run.failed_rows = payload["failed"]
                        await db.commit()
                except Exception as e:
                    logger.warning(f"Bulk run {run_id}: progress update failed (non-fatal): {e}")

            async for batch, run_meta in score_records_batched(remaining, progress_cb=_progress):
                stored_total += await _persist_batch(db, run_id, batch)
                persisted_so_far += len(batch)
                failed = run_meta.get("failed", failed)

                run.processed_rows = resume_offset + persisted_so_far
                run.failed_rows = failed
                run.embedder_mode = run_meta.get("embedder_mode")
                run.meta_json = json.dumps({
                    **run_meta,
                    "source_rows": len(records),
                    "stored_records": resume_offset + stored_total,
                }, default=str)

                if is_run_stop_requested(run_id):
                    stopped = True
                    run.status = "paused"
                    await db.commit()
                    logger.info(f"Bulk run {run_id} paused at {run.processed_rows}/{len(records)} rows.")
                    break

                await db.commit()

            if not stopped:
                run.status = "completed_with_errors" if failed > 0 else "completed"
                run.completed_at = _utcnow()
                await db.commit()
                logger.info(
                    f"Bulk run {run_id} completed: {len(records)} source row(s) parsed, "
                    f"{resume_offset + stored_total} record(s) stored, {failed} failed scoring."
                )
        except Exception as e:
            logger.error(f"Bulk run {run_id} failed: {e}", exc_info=True)
            try:
                run.status = "failed"
                run.error_message = str(e)
                run.completed_at = _utcnow()
                await db.commit()
            except Exception:
                logger.error(f"Bulk run {run_id}: could not even persist the failure state.", exc_info=True)
        finally:
            _run_cancel_events.pop(run_id, None)
            _run_detail.pop(run_id, None)


@router.post("/upload")
async def upload_bulk_workbook(
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
):
    """
    Accepts an ETSI bulk-declaration-export workbook (.xlsx/.xls/.csv) in the
    shape shown in the platform's test data. Parses it synchronously (fast),
    creates a run row, and hands scoring + persistence off to a BACKGROUND
    TASK — see `_process_bulk_run` above — so this request returns
    immediately instead of blocking (and, at 17,000+ rows, potentially
    timing out) until every row is scored. Poll GET
    /runs/{run_id}/status for progress, and GET /patents?run_id=... (already
    paginated) for results as they land — this endpoint no longer returns
    every patent row inline, which was itself a major cost at large scale
    (a 17,000-row JSON response built and returned in the same request that
    was also doing all the scoring work).
    """
    # MAX_UPLOAD_MB (core/config.py, default 50) existed but was not
    # enforced on any upload path, so the whole request body was buffered in
    # memory, then parsed by openpyxl in non-read-only mode (whole workbook
    # resident), then stored again as `original_file_blob`. One oversized or
    # malformed upload could therefore exhaust the backend's memory -- the
    # same symptom ("it just crashes") that large-dataset uploads were
    # reported with. Reading one byte past the limit is enough to detect it
    # without buffering the rest.
    max_bytes = settings.MAX_UPLOAD_MB * 1024 * 1024
    data = await file.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise HTTPException(
            413,
            f"'{file.filename or 'upload'}' is larger than the {settings.MAX_UPLOAD_MB} MB upload "
            f"limit (MAX_UPLOAD_MB). Split the file or raise MAX_UPLOAD_MB in backend/.env.",
        )
    if not data:
        raise HTTPException(400, f"'{file.filename or 'upload'}' is empty.")
    # Parsing a 35,000-50,000+ row workbook with openpyxl is real CPU work
    # (a few seconds to tens of seconds) -- running it inline in this async
    # def would block the single event loop for that whole time, freezing
    # every other concurrent request this backend process is serving
    # (including /runs/{id}/status polling for an unrelated run, and any
    # other user's requests). That's what made large uploads look "hung"/
    # "crashed" even though the request would eventually complete. Running
    # it in the default thread-pool executor keeps the event loop free.
    loop = asyncio.get_running_loop()
    parsed = await loop.run_in_executor(None, parse_bulk_workbook, file.filename or "upload.xlsx", data)
    if not parsed["ok"]:
        raise HTTPException(422, {"message": "Unrecognized file format.", "warnings": parsed["warnings"]})
    if not parsed["records"]:
        raise HTTPException(422, "No usable rows found in the uploaded file.")

    records = parsed["records"]

    run = SepBulkRun(
        filename=file.filename or "upload.xlsx",
        sheet_name=parsed["sheet"],
        total_rows=len(records),
        parsed_rows=len(records),
        status="processing",
        started_at=_utcnow(),
        # Kept only so GET /runs/{id}/export/excel (etsi_bulk/excel_export.py)
        # can reproduce the original workbook's columns/order/Summary sheet
        # exactly. Not read anywhere in the scoring path.
        original_file_blob=data,
    )
    db.add(run)
    await db.commit()
    await db.refresh(run)

    _spawn(_process_bulk_run(run.id, records))

    return {
        "run_id": run.id,
        "filename": run.filename,
        "sheet": run.sheet_name,
        "total_rows": run.total_rows,
        "status": run.status,
        "warnings": parsed["warnings"],
        "skipped_blank_rows": parsed.get("skipped_blank_rows", 0),
        "skipped_unkeyable_rows": parsed.get("skipped_unkeyable_rows", 0),
        "message": (
            f"Processing {len(records):,} row(s) in the background in batches of "
            f"{settings.SCORING_BATCH_SIZE}. Poll GET /api/sep/runs/{run.id}/status "
            f"for progress, then GET /api/sep/patents?run_id={run.id} for results."
        ),
    }


@router.get("/runs/{run_id}/status")
async def run_status(run_id: int, db: AsyncSession = Depends(get_db)):
    """Progress polling for a bulk run's background scoring/persistence —
    parsed / processed / failed / remaining counts, per REQUIRED LARGE-
    DATASET ARCHITECTURE item 10. The frontend should poll this instead of
    blocking on the upload request for large workbooks."""
    run = (await db.execute(select(SepBulkRun).options(defer(SepBulkRun.original_file_blob))
                            .where(SepBulkRun.id == run_id))).scalar_one_or_none()
    if not run:
        raise HTTPException(404, "Run not found")
    has_blob = bool((await db.execute(select(SepBulkRun.id).where(
        SepBulkRun.id == run_id, SepBulkRun.original_file_blob.isnot(None)))).first())

    remaining = max(0, (run.parsed_rows or 0) - (run.processed_rows or 0))
    return {
        "run_id": run.id,
        "status": run.status,
        "filename": run.filename,
        "parsed": run.parsed_rows,
        "queued": remaining,
        "processing": run.status == "processing",
        "processed": run.processed_rows,
        "completed": run.processed_rows if run.status.startswith("completed") else None,
        "failed": run.failed_rows,
        "remaining": remaining,
        "error_message": run.error_message,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "completed_at": run.completed_at.isoformat() if run.completed_at else None,
        "run_meta": json.loads(run.meta_json) if run.meta_json else None,
        "stoppable": run.status == "processing" and is_run_running(run.id),
        "resumable": run.status in ("paused", "failed") and (run.parsed_rows or 0) > (run.processed_rows or 0)
                     and has_blob,
        "detail": _run_detail.get(run.id) if run.status == "processing" else None,
    }


@router.post("/runs/{run_id}/pause")
async def pause_run(run_id: int):
    """Cooperative pause: the run finishes scoring+persisting whatever
    batch is currently in flight, then stops before starting the next
    one, leaving every row at a consistent, resumable checkpoint. Resume
    with POST /runs/{run_id}/continue."""
    accepted = request_run_stop(run_id)
    if not accepted:
        raise HTTPException(
            409,
            "This run is not currently processing in this backend process. "
            "If it was left running before a restart, use /continue instead.",
        )
    return {"run_id": run_id, "status": "pausing"}


@router.post("/runs/{run_id}/stop")
async def stop_run(run_id: int):
    """Same cooperative-stop mechanism as /pause -- a separate, explicitly
    -named action for a "Stop" button. The run remains resumable via
    /continue afterwards; nothing already scored/persisted is discarded."""
    accepted = request_run_stop(run_id)
    if not accepted:
        raise HTTPException(409, "This run is not currently processing in this backend process.")
    return {"run_id": run_id, "status": "stopping"}


@router.post("/runs/{run_id}/continue")
async def continue_run(run_id: int, db: AsyncSession = Depends(get_db)):
    """Resumes a paused/stopped/failed run from wherever it left off
    (`processed_rows`), by re-parsing the original uploaded workbook
    (kept verbatim on the run for exactly this purpose -- see
    SepBulkRun.original_file_blob) and re-entering the same background
    scoring path with a resume offset."""
    if is_run_running(run_id):
        raise HTTPException(409, "This run is already processing.")
    run = (await db.execute(select(SepBulkRun).where(SepBulkRun.id == run_id))).scalar_one_or_none()
    if run is None:
        raise HTTPException(404, "Run not found")
    if run.status in ("completed", "completed_with_errors"):
        return {"run_id": run_id, "status": run.status, "message": "This run already finished."}
    if not run.original_file_blob:
        raise HTTPException(
            400,
            "The original uploaded file is no longer available to resume this run -- please re-upload it.",
        )

    loop = asyncio.get_running_loop()
    parsed = await loop.run_in_executor(
        None, parse_bulk_workbook, run.filename or "upload.xlsx", run.original_file_blob
    )
    if not parsed["ok"] or not parsed["records"]:
        raise HTTPException(400, "Could not re-parse the originally uploaded file to resume this run.")

    # Resume from the number of source rows actually persisted (rows are
    # stored strictly in source order), not from the progress counter --
    # the counter can run ahead of persistence during the embedding pass.
    resume_offset = await _stored_row_count(db, run.id)
    run.processed_rows = resume_offset
    run.error_message = None
    await db.commit()
    _spawn(_process_bulk_run(run.id, parsed["records"], resume_offset=resume_offset))
    return {"run_id": run_id, "status": "resuming", "resume_from_row": resume_offset}


@router.get("/runs")
async def list_runs(db: AsyncSession = Depends(get_db)):
    """GET /api/sep/runs - every Bulk upload run, newest first, with status and row counters. Polled
    while a run is processing, so it never loads the stored workbook BLOBs. Requires `bulk.use`.
    """
    # v0.4.7: polled every 2.5 s while a run is processing - never load the stored workbooks (one BLOB per
    # run, megabytes each) or the metadata JSON just to list the runs.
    result = await db.execute(select(SepBulkRun)
                              .options(defer(SepBulkRun.original_file_blob), defer(SepBulkRun.meta_json))
                              .order_by(SepBulkRun.created_at.desc()))
    runs = result.scalars().all()
    return {"runs": [
        {"id": r.id, "filename": r.filename, "sheet": r.sheet_name, "total_rows": r.total_rows,
         "embedder_mode": r.embedder_mode, "created_at": r.created_at.isoformat(),
         "status": r.status, "processed_rows": r.processed_rows, "failed_rows": r.failed_rows,
         "detail": _run_detail.get(r.id) if r.status == "processing" else None}
        for r in runs
    ]}


# ── Excel export ──────────────────────────────────────────────────────────────
# Downstream of scoring only: loads the already-persisted run + patents and
# hands them to etsi_bulk/excel_export.py, which does not call score_records
# (or any research/ primitive) again. See that module's docstring.

@router.get("/runs/{run_id}/export/excel")
async def export_run_excel(run_id: int, db: AsyncSession = Depends(get_db)):
    """Download the CPARAS Bulk enriched Excel workbook for a run: every
    original source row and column preserved verbatim, in the original
    order, with the CPARAS_Bulk_* enrichment columns appended to each row,
    the original Summary sheet (if any), and a generated 'CPARAS Bulk
    Summary' sheet of run-level aggregates — all read from the
    already-stored analysis results for this run."""
    run = (await db.execute(select(SepBulkRun).where(SepBulkRun.id == run_id))).scalar_one_or_none()
    if not run:
        raise HTTPException(404, "Run not found")

    # Source order: one record per source row, aligned back onto the
    # original sheet by source_row_index inside build_enriched_workbook.
    result = await db.execute(
        select(SepBulkPatent).where(SepBulkPatent.run_id == run_id)
        .order_by(SepBulkPatent.source_row_index, SepBulkPatent.id)
    )
    patents = result.scalars().all()

    try:
        # v0.4.6: off the event loop - it may (re)build the Bulk corpus from the retrieval index.
        xlsx_bytes, warnings = await asyncio.to_thread(build_enriched_workbook, run, patents)
    except Exception as e:
        logger.exception(f"CPARAS Bulk Excel export failed for run {run_id}")
        raise HTTPException(500, f"Excel export failed: {e}")

    for w in warnings:
        logger.warning(f"[run {run_id}] Excel export: {w}")

    filename = export_filename(run)
    return StreamingResponse(
        io.BytesIO(xlsx_bytes),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": content_disposition(filename, "CPARAS_Bulk_enriched.xlsx"),
            "X-CPARAS-Bulk-Export-Warnings": str(len(warnings)),
        },
    )


@router.get("/runs/{run_id}/export/failed-rows")
async def export_run_failed_rows(run_id: int, db: AsyncSession = Depends(get_db)):
    """Downloadable failed-row/error report (REQUIRED LARGE-DATASET
    ARCHITECTURE item 4): every row this run's scoring pass marked
    row_status='FAILED' (see etsi_bulk/scoring.py's per-row isolation),
    with its row_error reason, as a CSV. A run can have thousands of
    failures at 50k+ scale, so this is a SQL-filtered streamed query --
    it never loads the run's successful rows, only the (usually much
    smaller) failed subset."""
    run = (await db.execute(select(SepBulkRun).where(SepBulkRun.id == run_id))).scalar_one_or_none()
    if not run:
        raise HTTPException(404, "Run not found")

    stmt = (
        select(
            SepBulkPatent.id, SepBulkPatent.source_row_index, SepBulkPatent.dipg_id, SepBulkPatent.company,
            SepBulkPatent.application_number, SepBulkPatent.publication_number,
            SepBulkPatent.disclosure_number, SepBulkPatent.title, SepBulkPatent.row_error,
        )
        .where(SepBulkPatent.run_id == run_id, SepBulkPatent.row_status == "FAILED")
        .order_by(SepBulkPatent.source_row_index, SepBulkPatent.id)
    )
    rows = (await db.execute(stmt)).all()

    import csv as _csv
    buf = io.StringIO()
    writer = _csv.writer(buf)
    writer.writerow(["row_id", "source_row_number", "dipg_id", "company", "application_number", "publication_number", "disclosure_number", "title", "error_reason"])
    for r in rows:
        writer.writerow([csv_safe(v) for v in (
            r.id, (r.source_row_index + 2) if r.source_row_index is not None else "", r.dipg_id or "", r.company or "",
            r.application_number or "", r.publication_number or "", r.disclosure_number or "", r.title or "",
            r.row_error or "")])

    body = buf.getvalue().encode("utf-8-sig")
    return StreamingResponse(
        io.BytesIO(body),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="cparas_bulk_run_{run_id}_failed_rows.csv"',
            "X-CPARAS-Bulk-Failed-Row-Count": str(len(rows)),
        },
    )


# ── Filters ───────────────────────────────────────────────────────────────────
# v0.7.2 PERFORMANCE FIX: every endpoint below used to load every matching
# SepBulkPatent row as a full ORM object (`.scalars().all()`) and then
# aggregate it in a Python loop. That's fine at a few hundred rows; at
# 50,000-100,000+ rows it means EVERY dashboard tile load -- and this
# dashboard fires 7+ of these in parallel on first paint, then again on
# every filter change -- deserializes the entire (filtered) table into
# Python objects just to compute a handful of counts/sums SQL can compute
# directly. That's the dominant cost in "dashboard load time" at scale (see
# the v0.7.2 scale-test report) and, run concurrently across several
# browser tabs, real memory pressure. Every endpoint below now aggregates
# in SQL (COUNT/SUM/GROUP BY) and only pulls back as many rows as there are
# distinct groups (companies, releases, years, ...) -- never the full
# patent table. No endpoint's response shape or values changed.

def _essentiality_case(column=None):
    """SUM(CASE WHEN essentiality = 'X' THEN 1 ELSE 0 END) building block,
    shared by every endpoint below that needs a per-essentiality count
    alongside a GROUP BY key.

    Matches the old Python-side aggregation's `p.essentiality or "Potential"`
    exactly: a NULL or empty-string essentiality is bucketed as "Potential",
    not dropped from all three buckets. We do this with COALESCE(NULLIF(col,
    ''), 'Potential') rather than comparing the raw column, so NULL/'' rows
    still land in `potential` instead of silently vanishing from the
    essential/non_essential/potential breakdown while still counting toward
    `total` (a discrepancy that must not be introduced by this refactor)."""
    from sqlalchemy import case, func as _func
    col = column if column is not None else SepBulkPatent.essentiality
    normalized = _func.coalesce(_func.nullif(col, ""), "Potential")
    return (
        func.sum(case((normalized == "Essential", 1), else_=0)),
        func.sum(case((normalized == "Non-Essential", 1), else_=0)),
        func.sum(case((normalized == "Potential", 1), else_=0)),
    )


@router.get("/filters")
async def get_filters(run_id: Optional[int] = None, db: AsyncSession = Depends(get_db)):
    """Distinct values to populate the sidebar filter panel (Technology, Release, Company, Country) --
    via SQL DISTINCT so this stays cheap regardless of how many patents the run has.

    v0.7.2 FRONTEND-RENDERING FIX (REQUIRED LARGE-DATASET ARCHITECTURE item
    2): technology/release/country have small, effectively-bounded
    vocabularies in practice, but `company` does not -- a real bulk
    multi-declarant ETSI export can carry hundreds to thousands of
    distinct declarant names, and FilterSidebar.tsx renders one checkbox
    per value with no virtualization (only a client-side search box that
    filters an already-fully-rendered list). distinct_company_values below
    bounds that list at the source: most-frequent-first (so the values a
    user actually wants to filter by are the ones kept) instead of an
    unbounded alphabetical dump, capped by GITAS_FILTER_COMPANY_LIMIT."""
    async def distinct_values(column) -> list[str]:
        """Sorted distinct non-empty values of `column` (optionally within one run) for a filter
        checklist.
        """
        stmt = select(column).distinct().where(column.isnot(None)).where(column != "")
        if run_id is not None:
            stmt = stmt.where(SepBulkPatent.run_id == run_id)
        result = await db.execute(stmt)
        return sorted(v for (v,) in result.all() if v)

    async def distinct_company_values(limit: int = 1000) -> list[str]:
        """The `limit` companies with the most records (alphabetical), keeping the company filter list
        bounded on very large exports.
        """
        stmt = (
            select(SepBulkPatent.company, func.count().label("n"))
            .where(SepBulkPatent.company.isnot(None)).where(SepBulkPatent.company != "")
        )
        if run_id is not None:
            stmt = stmt.where(SepBulkPatent.run_id == run_id)
        stmt = stmt.group_by(SepBulkPatent.company).order_by(func.count().desc()).limit(limit)
        result = await db.execute(stmt)
        return sorted(c for c, _n in result.all())  # alphabetical within the bounded top-N, for a stable checkbox order

    return {
        "technology": await distinct_values(SepBulkPatent.technology),
        "release": await distinct_values(SepBulkPatent.release),
        "company": await distinct_company_values(),
        "country": await distinct_values(SepBulkPatent.country_code),
    }


# ── Dashboard: overview KPIs ─────────────────────────────────────────────────

@router.get("/dashboard/overview")
async def dashboard_overview(f: Filters = Depends(), db: AsyncSession = Depends(get_db)):
    """GET /api/sep/dashboard/overview - KPI tiles for the filtered records: totals per essentiality
    class, average confidence, specifications covered and related counts.
    """
    ess_essential, ess_non, ess_potential = _essentiality_case()
    stmt = f.apply(
        select(
            func.count().label("total"),
            ess_essential.label("essential"),
            ess_non.label("non_essential"),
            ess_potential.label("potential"),
            func.avg(SepBulkPatent.confidence).label("avg_confidence"),
            func.count(func.distinct(SepBulkPatent.spec_number)).label("specs_covered"),
        ).select_from(SepBulkPatent)
    )
    row = (await db.execute(stmt)).one()
    total = row.total or 0

    # top_release still needs its own GROUP BY -- a second query, but one
    # that returns at most a few dozen rows (distinct releases), not the
    # whole table.
    release_stmt = f.apply(
        select(SepBulkPatent.release, func.count().label("n")).select_from(SepBulkPatent)
    ).where(SepBulkPatent.release.isnot(None)).group_by(SepBulkPatent.release).order_by(func.count().desc()).limit(1)
    top_release_row = (await db.execute(release_stmt)).first()

    return {
        "total_seps": total,
        "essential": int(row.essential or 0),
        "non_essential": int(row.non_essential or 0),
        "potential": int(row.potential or 0),
        "avg_essentiality_score": round((row.avg_confidence or 0) * 100, 1) if total else 0.0,
        "top_release": top_release_row[0] if top_release_row else None,
        "specs_covered": row.specs_covered or 0,
    }


@router.get("/dashboard/companies")
async def dashboard_companies(top_n: int = 10, f: Filters = Depends(), db: AsyncSession = Depends(get_db)):
    """Stacked bar: top companies by SEP count, stacked by essentiality."""
    ess_essential, ess_non, ess_potential = _essentiality_case()
    stmt = f.apply(
        select(
            SepBulkPatent.company,
            ess_essential.label("essential"), ess_non.label("non_essential"), ess_potential.label("potential"),
            func.count().label("total"),
        ).select_from(SepBulkPatent)
    ).where(SepBulkPatent.company.isnot(None)).group_by(SepBulkPatent.company).order_by(func.count().desc()).limit(top_n)
    rows = (await db.execute(stmt)).all()

    return {"companies": [
        {"company": r.company, "essential": int(r.essential or 0), "non_essential": int(r.non_essential or 0),
         "potential": int(r.potential or 0), "total": r.total}
        for r in rows
    ]}


@router.get("/dashboard/timeline")
async def dashboard_timeline(f: Filters = Depends(), db: AsyncSession = Depends(get_db)):
    """SEP declarations over time, bucketed by signature-date year (cumulative)."""
    year_expr = func.substr(SepBulkPatent.signature_date, 1, 4)
    stmt = f.apply(
        select(year_expr.label("year"), func.count().label("n")).select_from(SepBulkPatent)
    ).where(SepBulkPatent.signature_date.isnot(None)).group_by(year_expr)
    rows = (await db.execute(stmt)).all()

    by_year = {int(r.year): r.n for r in rows if r.year and str(r.year).isdigit()}
    years = sorted(by_year.keys())
    cumulative, running = [], 0
    for y in years:
        running += by_year[y]
        cumulative.append({"year": y, "count": by_year[y], "cumulative": running})
    return {"timeline": cumulative}


@router.get("/dashboard/countries")
async def dashboard_countries(f: Filters = Depends(), db: AsyncSession = Depends(get_db)):
    """GET /api/sep/dashboard/countries - record count and share per country code for the filtered
    records.
    """
    stmt = f.apply(
        select(SepBulkPatent.country_code, func.count().label("n")).select_from(SepBulkPatent)
    ).where(SepBulkPatent.country_code.isnot(None)).where(SepBulkPatent.country_code != "").group_by(SepBulkPatent.country_code)
    rows = (await db.execute(stmt)).all()

    total = sum(r.n for r in rows)
    return {"total": total, "countries": [
        {"country": r.country_code, "count": r.n, "pct": round(r.n / total * 100, 1) if total else 0}
        for r in sorted(rows, key=lambda r: r.n, reverse=True)
    ]}


@router.get("/dashboard/by-release")
async def dashboard_by_release(f: Filters = Depends(), db: AsyncSession = Depends(get_db)):
    """GET /api/sep/dashboard/by-release - essentiality split per 3GPP release for the filtered
    records.
    """
    ess_essential, ess_non, ess_potential = _essentiality_case()
    stmt = f.apply(
        select(
            SepBulkPatent.release, func.count().label("n"),
            ess_essential.label("Essential"), ess_non.label("Non-Essential"), ess_potential.label("Potential"),
        ).select_from(SepBulkPatent)
    ).where(SepBulkPatent.release.isnot(None)).group_by(SepBulkPatent.release).order_by(SepBulkPatent.release)
    rows = (await db.execute(stmt)).all()

    return {"by_release": [
        {"release": r.release, "count": r.n, "Essential": int(r.Essential or 0),
         "Non-Essential": int(r._mapping["Non-Essential"] or 0), "Potential": int(r.Potential or 0)}
        for r in rows
    ]}


@router.get("/dashboard/portfolio")
async def dashboard_portfolio(top_n: int = 200, f: Filters = Depends(), db: AsyncSession = Depends(get_db)):
    """Company portfolio table: total SEPs / essential / potential / non-essential per company.

    v0.7.2 FRONTEND-RENDERING FIX (REQUIRED LARGE-DATASET ARCHITECTURE item
    2): unlike /dashboard/companies (which always took a top_n), this
    endpoint used to return EVERY distinct company with no limit, and
    PortfolioTable (frontend/src/gitas/components/charts.tsx) renders every
    row it's given with no pagination/virtualization of its own. A small
    synthetic test fixture only has a couple dozen companies so this never
    showed up in this test harness's own dashboards, but a real bulk
    multi-declarant ETSI export can legitimately carry hundreds to
    thousands of distinct declarant companies, and that combination
    (unbounded backend result x un-virtualized frontend table) is exactly
    the unbounded-DOM-rendering pattern item 2 asks to be audited for.
    Bounded the same way /dashboard/companies already is, with a higher
    default (200, vs. companies' top-10 "leaderboard" use case) since the
    portfolio table is meant to be a fuller browse view, not just a top-N
    summary -- callers that need the true tail can raise top_n explicitly."""
    ess_essential, ess_non, ess_potential = _essentiality_case()
    stmt = f.apply(
        select(
            SepBulkPatent.company,
            # A company's country_code should be consistent across its own
            # rows in practice; MIN() picks a single deterministic value
            # per group without needing the full row set, same semantics
            # as the old code's "whichever row we saw" (also arbitrary).
            func.min(SepBulkPatent.country_code).label("country"),
            func.count().label("total"),
            ess_essential.label("essential"), ess_potential.label("potential"), ess_non.label("non_essential"),
        ).select_from(SepBulkPatent)
    ).where(SepBulkPatent.company.isnot(None)).group_by(SepBulkPatent.company).order_by(func.count().desc()).limit(top_n)
    rows = (await db.execute(stmt)).all()

    portfolio = [
        {"company": r.company, "country": r.country, "total": r.total,
         "essential": int(r.essential or 0), "potential": int(r.potential or 0), "non_essential": int(r.non_essential or 0)}
        for r in rows
    ]
    return {"portfolio": portfolio}


@router.get("/dashboard/heatmap")
async def dashboard_heatmap(f: Filters = Depends(), db: AsyncSession = Depends(get_db)):
    """SEP coverage heatmap: release (x) by technical domain (y), values = SEP counts.

    SQL GROUP BY (domain, release) instead of pulling every row into Python --
    the result set is bounded by (distinct domains x distinct releases), which
    is at most a few hundred cells even at 100k+ rows, not the whole table."""
    stmt = f.apply(
        select(
            SepBulkPatent.domain, SepBulkPatent.release, func.count().label("n"),
        ).select_from(SepBulkPatent)
    ).where(SepBulkPatent.release.isnot(None)).where(SepBulkPatent.release != "") \
     .where(SepBulkPatent.domain.isnot(None)).where(SepBulkPatent.domain != "") \
     .group_by(SepBulkPatent.domain, SepBulkPatent.release)
    rows = (await db.execute(stmt)).all()

    matrix: dict[str, dict[str, int]] = defaultdict(dict)
    releases, domains = set(), set()
    for r in rows:
        matrix[r.domain][r.release] = r.n
        releases.add(r.release)
        domains.add(r.domain)

    releases = sorted(releases)
    domains = sorted(domains)
    return {
        "releases": releases,
        "domains": domains,
        "cells": [
            {"domain": d, "release": r, "value": matrix[d].get(r, 0)}
            for d in domains for r in releases
        ],
    }


# ── Spec context (3GPP Specification Context panel) ─────────────────────────

async def _spec_context(db: AsyncSession, run_id: int, spec_number: Optional[str], spec_version: Optional[str]) -> dict:
    """Aggregate stats for one TS spec across the whole run — the '234 Total
    SEPs / 156 Essential / 54 Potential / 89 Families' panel on the patent
    detail page. Scoped to the same run as the patent being viewed, and to
    all versions of the spec (a spec accrues SEPs across releases)."""
    if not spec_number:
        return {}
    stmt = select(SepBulkPatent).where(SepBulkPatent.run_id == run_id, SepBulkPatent.spec_number == spec_number)
    rows = (await db.execute(stmt)).scalars().all()
    if not rows:
        return {}
    families = {r.family_id for r in rows if r.family_id}
    return {
        "spec_number": spec_number,
        "spec_version": spec_version,
        "total_seps": len(rows),
        "essential": sum(1 for r in rows if r.essentiality == "Essential"),
        "potential": sum(1 for r in rows if r.essentiality == "Potential"),
        "non_essential": sum(1 for r in rows if r.essentiality == "Non-Essential"),
        "families": len(families) or None,
    }


# ── Patent registry (legal view table) ───────────────────────────────────────

@router.get("/patents")
async def list_patents(
    f: Filters = Depends(),
    limit: int = Query(50, le=500),
    offset: int = 0,
    order: str = Query("company", description='"company" (default) or "source" (original upload row order).'),
    db: AsyncSession = Depends(get_db),
):
    """GET /api/sep/patents - the Patent Registry: filtered records, paged (limit <= 500), ordered by
    company (default) or original upload row order, plus the total count.
    """
    count_stmt = f.apply(select(func.count()).select_from(SepBulkPatent))
    total = (await db.execute(count_stmt)).scalar_one()

    order_cols = {
        "company": (SepBulkPatent.company, SepBulkPatent.run_id, SepBulkPatent.source_row_index, SepBulkPatent.id),
        "source": (SepBulkPatent.run_id, SepBulkPatent.source_row_index, SepBulkPatent.id),
    }.get(order, (SepBulkPatent.company, SepBulkPatent.run_id, SepBulkPatent.source_row_index, SepBulkPatent.id))
    stmt = f.apply(select(SepBulkPatent)).order_by(*order_cols).offset(offset).limit(limit)
    result = await db.execute(stmt)
    rows = result.scalars().all()

    return {"total": total, "count": len(rows), "offset": offset, "limit": limit,
            "patents": [_row_to_dict(p) for p in rows]}


@router.get("/patents/{patent_id}")
async def get_patent(patent_id: int, db: AsyncSession = Depends(get_db)):
    """
    Single patent detail page — essentiality determination + score breakdown,
    claim-to-standard mapping, and related patents (same domain, same run).
    """
    result = await db.execute(select(SepBulkPatent).where(SepBulkPatent.id == patent_id))
    p = result.scalar_one_or_none()
    if not p:
        raise HTTPException(404, "Patent not found")

    related_stmt = (
        select(SepBulkPatent)
        .where(SepBulkPatent.run_id == p.run_id)
        .where(SepBulkPatent.domain == p.domain)
        .where(SepBulkPatent.id != p.id)
        .limit(6)
    )
    related = (await db.execute(related_stmt)).scalars().all()

    family_count_stmt = select(func.count()).select_from(SepBulkPatent).where(
        SepBulkPatent.run_id == p.run_id, SepBulkPatent.family_id == p.family_id
    )
    family_count = (await db.execute(family_count_stmt)).scalar_one() if p.family_id else 1

    detail = _row_to_dict(p)
    detail["family_count"] = family_count
    detail["spec_context"] = await _spec_context(db, p.run_id, p.spec_number, p.spec_version)
    detail["essentiality_determination"] = {
        "status": p.essentiality,
        "overall_confidence": round((p.confidence or 0) * 100, 1),
        "score_breakdown": {
            "claim_match": round((p.claim_match or 0) * 100, 1),
            "semantic_similarity": round((p.semantic_similarity or 0) * 100, 1),
            "standard_coverage": round((p.standard_coverage or 0) * 100, 1),
        },
        "disclaimer": "Determined by NLP & ML analysis over an offline reference "
                       "corpus — not a legal opinion. Verify against live ETSI/3GPP "
                       "specification text before relying on this for FRAND, "
                       "licensing, or litigation purposes.",
    }
    detail["claim_to_standard_mapping"] = {
        "patent_claim_simplified": p.illustrative_part or p.title,
        "matched_standard_section": f"{p.matched_standard} — {p.matched_clause}: {p.matched_clause_title}",
        "match_confidence": round((p.claim_match or 0) * 100, 1),
        "matched_key_terms": json.loads(p.matched_key_terms_json) if p.matched_key_terms_json else [],
        "matched_key_terms_count": p.matched_key_terms_count or 0,
        "total_key_terms": p.total_key_terms or 0,
        "summary": f"{p.matched_key_terms_count or 0} of {p.total_key_terms or 0} key technical terms aligned",
    }
    detail["declaration"] = {
        "isld_reference": p.iprd_reference,
        "note": "Build the ETSI IPR database search link from this reference "
                "on the frontend — no fixed URL pattern is assumed here.",
    }
    detail["related_patents"] = [_row_to_dict(r) for r in related]
    return detail


# ── Entity: company profile ──────────────────────────────────────────────────

@router.get("/entity/company/{company_name}")
async def company_profile(company_name: str, db: AsyncSession = Depends(get_db)):
    """GET /api/sep/entity/company/{company_name} - one company's portfolio: record counts per
    essentiality class, standards, releases and countries. 404 if the company has no records.
    """
    stmt = select(SepBulkPatent).where(SepBulkPatent.company == company_name)
    result = await db.execute(stmt)
    rows = result.scalars().all()
    if not rows:
        raise HTTPException(404, "Company not found")

    essential = sum(1 for p in rows if p.essentiality == "Essential")
    potential = sum(1 for p in rows if p.essentiality == "Potential")
    non_essential = sum(1 for p in rows if p.essentiality == "Non-Essential")

    by_release = defaultdict(int)
    by_domain = defaultdict(int)
    for p in rows:
        if p.release:
            by_release[p.release] += 1
        if p.domain:
            by_domain[p.domain] += 1

    return {
        "company": company_name,
        "country": rows[0].country_code,
        "total_seps": len(rows),
        "essential": essential,
        "potential": potential,
        "non_essential": non_essential,
        "by_release": dict(sorted(by_release.items())),
        "by_domain": dict(sorted(by_domain.items(), key=lambda kv: kv[1], reverse=True)),
        "patents": [_row_to_dict(p) for p in rows],
    }
