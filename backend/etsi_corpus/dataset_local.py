"""
Local canonical-dataset workflow (v0.4.4): status -> validate -> import/index -> report.

The validated 3GPP build (``CPARAS_DATASET_ROOT``, e.g. D:\\3gpp\\cparas_dataset\\build_v4) is READ-ONLY
here.  Nothing is copied into the application unless ``ETSI_COPY_TEXT_LOCALLY`` says so; the extracted
full text is read straight out of ``etsi_dataset.zip`` and the JSONL is streamed, so memory stays flat
for the 2.9 GB / 32,440-record build.

The server never accepts a client-supplied path for this workflow: the only folder it will read is the one
configured in CPARAS_DATASET_ROOT (path traversal is impossible by construction).
"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Optional

from sqlalchemy import func, select

from core import gpp_dataset as gd
from core.config import settings
from core.database import AsyncSessionLocal, EtsiDocument, EtsiIngestionJob

logger = logging.getLogger(__name__)

NEEDS_FOR_IMPORT = ("jsonl", "text")


def configured_root() -> Optional[Path]:
    """The configured dataset folder, or None. Raises gd.DatasetError if configured but unusable."""
    return gd.resolve_root(settings.CPARAS_DATASET_ROOT)


def _not_configured() -> dict:
    """Status result when CPARAS_DATASET_ROOT is not set, with the exact .env line to add."""
    return {"configured": False, "ok": False, "root": None,
            "problems": [{"code": "not_configured",
                          "message": "CPARAS_DATASET_ROOT is not set. Put it in backend\\.env, e.g. "
                                     "CPARAS_DATASET_ROOT=D:\\3gpp\\cparas_dataset\\build_v4"}]}


async def db_counts() -> dict:
    """How many dataset documents are in the database per status, and how many have an unknown version."""
    async with AsyncSessionLocal() as session:
        rows = (await session.execute(select(EtsiDocument.status, func.count()).group_by(EtsiDocument.status))).all()
        by_status = {st: int(n) for st, n in rows}
        unknown_v = (await session.execute(select(func.count()).select_from(EtsiDocument).where(
            func.lower(func.coalesce(EtsiDocument.version, "unknown")) == "unknown"))).scalar_one()
        specs = (await session.execute(select(func.count(func.distinct(EtsiDocument.specification_number))))).scalar_one()
    return {"documents": sum(by_status.values()), "by_status": by_status, "indexed": by_status.get("completed", 0),
            "distinct_specifications": int(specs or 0), "unknown_version_documents": int(unknown_v or 0)}


async def dataset_status() -> dict:
    """Cheap (reads only the small JSON reports + the DB): configured?, files present?, build facts,
    what is already imported, and a plain-language diagnosis of a partial / missing / stale import."""
    try:
        root = configured_root()
    except gd.DatasetError as e:
        return {"configured": True, "ok": False, "root": settings.CPARAS_DATASET_ROOT, "problems": [e.to_dict()]}
    if root is None:
        return _not_configured()
    info = gd.inspect_dataset(root, needs=NEEDS_FOR_IMPORT)
    info["configured"] = True
    counts = await db_counts()
    info["imported"] = counts
    expected = (info.get("build") or {}).get("records_written")
    diag = []
    if info["ok"]:
        if counts["documents"] == 0:
            diag.append({"level": "info", "code": "not_imported", "message": "Dataset found but nothing is imported yet - run the import."})
        elif expected and counts["indexed"] < expected:
            diag.append({"level": "warning", "code": "partially_indexed",
                         "message": f"{counts['indexed']} of {expected} dataset records are indexed. This is normal after a filtered/limited "
                                    f"import (versions=latest-per-release, --limit) or an interrupted run; re-run the import to continue."})
        elif expected and counts["indexed"] >= expected:
            diag.append({"level": "info", "code": "fully_indexed", "message": f"All {expected} dataset records are indexed."})
        failed = sum(v for k, v in counts["by_status"].items() if k.endswith("_failed"))
        if failed:
            diag.append({"level": "warning", "code": "failed_documents", "message": f"{failed} document(s) failed - see the job's failed-documents list; re-running retries them."})
    info["diagnostics"] = diag
    async with AsyncSessionLocal() as session:
        last = (await session.execute(select(EtsiIngestionJob).order_by(EtsiIngestionJob.created_at.desc()).limit(1))).scalar_one_or_none()
    info["last_job_id"] = last.id if last else None
    return info


def validate_dataset_sync(*, crc: bool = False, check_history: bool = True, max_problems: int = 50) -> dict:
    """Full streaming validation (CPU/disk bound - run in a worker thread). Reads the JSONL once; never loads it."""
    t0 = time.time()
    root = configured_root()
    if root is None:
        return _not_configured()
    lay = gd.layout(root)
    result = gd.inspect_dataset(root, needs=NEEDS_FOR_IMPORT)
    problems = list(result["problems"])
    warnings = list(result["warnings"])
    stats = gd.JsonlStats()
    c = {"records": 0, "missing_identity": 0, "missing_text": 0, "unknown_version": 0, "unknown_spec": 0,
         "duplicate_document_id": 0, "duplicate_pdf_url": 0, "history_pointers": 0}
    zip_info = None
    try:
        if lay.zip.is_file():
            zip_info = gd.check_zip(lay.zip, crc=crc)
            if not zip_info["has_jsonl"]:
                problems.append({"code": "zip_no_jsonl", "message": f"{gd.ZIP_NAME} contains no .jsonl file"})
    except gd.DatasetError as e:
        problems.append(e.to_dict())

    shas: set = set()
    if lay.jsonl.is_file():
        try:
            ts = gd.open_text_source(lay) if (lay.zip.is_file() or lay.text_dir.is_dir()) else None
        except gd.DatasetError as e:
            ts = None
            problems.append(e.to_dict())
        ids, urls = set(), set()
        try:
            for ln, rec in gd.iter_jsonl(lay.jsonl, stats):
                c["records"] += 1
                ident = rec.get("identity") if isinstance(rec.get("identity"), dict) else {}
                did, url = ident.get("document_id"), ident.get("pdf_url")
                if not did or not url:
                    c["missing_identity"] += 1
                    if len(problems) < max_problems:
                        problems.append({"code": "record_identity_missing", "message": f"line {ln}: missing identity.document_id / pdf_url"})
                else:
                    if did in ids: c["duplicate_document_id"] += 1
                    if url in urls: c["duplicate_pdf_url"] += 1
                    ids.add(did); urls.add(url)
                if gd.is_unknown(ident.get("version")): c["unknown_version"] += 1
                if gd.is_unknown(ident.get("document_number")): c["unknown_spec"] += 1
                if ts is not None and not ts.has((rec.get("provenance") or {}).get("raw_text_path"), did):
                    c["missing_text"] += 1
                    if c["missing_text"] <= 10:
                        problems.append({"code": "record_text_missing", "message": f"line {ln}: no extracted text for {did!r}"})
                for r in gd.history_refs(rec.get("spec_history")):
                    c["history_pointers"] += 1
                    if r.get("sha256"):
                        shas.add(r["sha256"])
        except gd.DatasetError as e:
            problems.append(e.to_dict())
        finally:
            if ts is not None:
                ts.close()
        if stats.malformed or stats.not_object:
            problems.append({"code": "malformed_lines", "message": f"{stats.malformed} malformed + {stats.not_object} non-object line(s) in {gd.JSONL_NAME}",
                             "sample": stats.errors[:5]})
        if c["duplicate_document_id"] or c["duplicate_pdf_url"]:
            warnings.append({"code": "duplicates", "message": f"duplicate document_id: {c['duplicate_document_id']}, duplicate pdf_url: {c['duplicate_pdf_url']} "
                                                              "(an import keeps the first and counts the rest)."})
        if c["missing_text"]:
            problems.append({"code": "records_without_text", "message": f"{c['missing_text']} record(s) have no extracted text and would fail import."})
        build = result.get("build") or {}
        if build.get("records_written") is not None and build["records_written"] != c["records"]:
            problems.append({"code": "record_count_mismatch",
                             "message": f"build_report says {build['records_written']} records, the JSONL has {c['records']}."})
        if c["unknown_version"]:
            warnings.append({"code": "unknown_versions", "message": f"{c['unknown_version']} record(s) have version 'Unknown' (kept; never guessed)."})

    history = None
    if check_history and (shas or lay.sidecar.is_file()):
        if not lay.sidecar.is_file():
            if shas:
                problems.append({"code": "sidecar_missing", "message": f"{len(shas)} history pointer(s) but {gd.SIDECAR_NAME} is missing"})
        else:
            try:
                history = gd.verify_sidecar(lay.sidecar, shas)
                if not history["ok"]:
                    problems.append({"code": "history_invalid", "message": f"history sidecar: {len(history['bad'])} bad blob(s), "
                                                                           f"{len(history['missing'])} referenced blob(s) missing"})
            except gd.DatasetError as e:
                problems.append(e.to_dict())

    result.update({"validated": True, "ok": not problems, "problems": problems[:max_problems], "warnings": warnings,
                   "counts": c, "zip": zip_info, "history": history, "crc_checked": crc,
                   "seconds": round(time.time() - t0, 1)})
    return result


async def validate_dataset(**kw) -> dict:
    """Async wrapper running the (multi-GB, streaming) dataset validation in a worker thread."""
    return await asyncio.to_thread(validate_dataset_sync, **kw)


def prepare_import_inputs() -> tuple[Path, gd.TextSource, dict]:
    """Resolve (jsonl, text_source, build_info) for the configured root or raise DatasetError."""
    root = configured_root()
    if root is None:
        raise gd.DatasetError("not_configured", _not_configured()["problems"][0]["message"])
    info = gd.inspect_dataset(root, needs=NEEDS_FOR_IMPORT)
    if not info["ok"]:
        raise gd.DatasetError(info["problems"][0]["code"], "; ".join(p["message"] for p in info["problems"]))
    lay = gd.layout(root)
    return lay.jsonl, gd.open_text_source(lay), info


async def run_local_import(job_id: int, *, limit: Optional[int], versions: str, series, specs, copy_text: bool,
                           only_failed: bool = False) -> None:
    """Background task body. Opens the canonical text source for the duration of the run."""
    from etsi_corpus.pipeline import import_dataset
    try:
        jsonl, ts, info = prepare_import_inputs()
    except gd.DatasetError as e:
        await _fail_job(job_id, e.message)
        return
    try:
        await import_dataset(jsonl_path=jsonl, text_dir=ts, limit=limit, job_id=job_id, versions=versions,
                             series=series, specs=specs, copy_text=copy_text, only_failed=only_failed,
                             total_hint=(info.get("build") or {}).get("records_written"),
                             fingerprint=info.get("fingerprint"), source_label=f"{gd.ZIP_NAME}" if ts.kind == "zip" else "text/")
    except Exception as e:  # noqa: BLE001
        logger.error(f"Local dataset import job {job_id} failed: {e}", exc_info=True)
        await _fail_job(job_id, str(e))
    finally:
        ts.close()


async def _fail_job(job_id: int, message: str) -> None:
    """Mark an import job failed with a (truncated) error message."""
    async with AsyncSessionLocal() as session:
        job = (await session.execute(select(EtsiIngestionJob).where(EtsiIngestionJob.id == job_id))).scalar_one_or_none()
        if job:
            job.status = "failed"
            job.error_message = message[:2000]
            await session.commit()


async def create_local_job(limit: Optional[int]) -> int:
    """Create the EtsiIngestionJob row for an in-place import from CPARAS_DATASET_ROOT and return its
    id.
    """
    root = configured_root()
    jsonl = gd.layout(root).jsonl if root else None
    async with AsyncSessionLocal() as session:
        job = EtsiIngestionJob(manifest_path=str(jsonl) if jsonl else None, status="pending", limit_requested=limit,
                               current_operation="queued")
        session.add(job)
        await session.commit()
        await session.refresh(job)
        return job.id
