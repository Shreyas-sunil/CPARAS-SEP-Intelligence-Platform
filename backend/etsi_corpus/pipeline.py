"""
Dataset import orchestrator (v0.3-continuation: dataset-first architecture).

SEP Platform no longer acquires ETSI PDFs itself. This module imports an
ALREADY-PREPARED dataset (JSONL records + extracted text files) produced
by the standalone `etsi-scraper-pipeline` project, and indexes it into the
shared vector store:

    uploaded dataset zip (etsi_dataset.jsonl + text/)
        -> unpacked into ETSI_DATASET_IMPORT_DIR
        -> per-record EtsiDocument checkpoint
        -> validate + copy text into SEP's own text store
        -> index (chunk + embed + upsert -- reuses etsi_corpus/indexer.py
           UNCHANGED, since indexing never cared how a document got its text)
        -> completed

No network call to etsi.org exists anywhere in this module or anything it
imports. Search this file (and the rest of backend/etsi_corpus/) for
"httpx"/"download"/"etsi.org" -- there is none.

STATE MACHINE (deliberately simpler than the old acquisition pipeline's:
there's no slow LLM call to wait for anymore -- the dataset already
contains the structured extraction result):

    pending -> importing -> import_failed
                          -> imported -> indexing -> index_failed
                                                   -> completed

CONCURRENCY: unchanged bounded-worker-queue architecture from the old
acquisition pipeline (kept because it's genuinely still the right design
for processing many records) -- a fixed pool of ETSI_IMPORT_BATCH_SIZE
workers pull document ids from an asyncio.Queue, never one Task per
record. Crash resilience is also unchanged: any single worker's
unexpected exception is caught and logged rather than propagating and
cascading into other workers (asyncio.gather(..., return_exceptions=True)).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from sqlalchemy import select, func, update

from core import gpp_dataset as gd
from core.config import settings
from core.database import AsyncSessionLocal, EtsiDocument, EtsiIngestionJob
from etsi_corpus.indexer import index_document, delete_document_index, pop_last_outcome, INFRASTRUCTURE_ERRORS  # noqa: F401 (delete_document_index re-exported: callers/tests patch it here)
from core.timeutil import utcnow as _utcnow

logger = logging.getLogger(__name__)

SEARCHABLE_STATUSES = ("completed",)
TERMINAL_STATUSES = ("completed",)


# ── Cooperative cancellation ("Pause"/"Cancel") ────────────────────────────
_cancel_events: dict[int, asyncio.Event] = {}


def request_stop(job_id: int) -> bool:
    """Ask a running dataset import to stop after the document in flight. False when the job is not
    running in this process.
    """
    ev = _cancel_events.get(job_id)
    if ev is None:
        return False
    ev.set()
    return True


def is_stop_requested(job_id: int) -> bool:
    """Whether a stop was requested for an import job."""
    ev = _cancel_events.get(job_id)
    return ev.is_set() if ev else False


def _text_output_dir() -> Path:
    """Folder where extracted document texts are written when copy-text-locally is on
    (ETSI_DATASET_OUTPUT/text).
    """
    p = Path(settings.ETSI_DATASET_OUTPUT) / "text"
    p.mkdir(parents=True, exist_ok=True)
    return p


@dataclass
class DatasetRecordRow:
    """One parsed line of the imported etsi_dataset.jsonl -- a plain
    dataclass (not a shared Pydantic model with the scraper project,
    which is a genuinely separate codebase). Tolerant of missing/extra
    fields rather than requiring an exact schema match; everything absent
    defaults to "Unknown"/empty, never invented."""
    document_id: str
    pdf_url: str
    raw: dict


def _iter_jsonl(path: Path):
    """Stream (line number, record) from a dataset JSONL file; malformed lines are logged and skipped,
    never fatal.
    """
    with open(path, "r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError as e:
                logger.warning(f"Skipping unparseable dataset line {line_no}: {e}")
                continue
            identity = data.get("identity", {})
            document_id = identity.get("document_id")
            pdf_url = identity.get("pdf_url")
            if not document_id or not pdf_url:
                logger.warning(f"Skipping dataset line {line_no}: missing identity.document_id or identity.pdf_url")
                continue
            yield DatasetRecordRow(document_id=document_id, pdf_url=pdf_url, raw=data)


def unpack_dataset_zip(zip_path: Path, dest_dir: Path) -> tuple[Path, Optional[Path]]:
    """Unpacks an uploaded dataset zip (etsi_dataset.jsonl + optional
    text/ directory) into `dest_dir`. Returns (jsonl_path, text_dir_or_None).
    Raises ValueError if the zip doesn't contain a recognizable JSONL
    dataset file -- never silently imports something that isn't a dataset."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    root = dest_dir.resolve()
    try:
        gd.check_zip(Path(zip_path), crc=False)   # structure + zip-slip check BEFORE extracting anything
    except gd.DatasetError as e:
        raise ValueError(e.message)
    try:
        with zipfile.ZipFile(zip_path) as zf:
            for info in zf.infolist():
                # Explicit zip-slip guard (zipfile also sanitises, but a
                # dataset archive has no business containing such paths).
                target = (root / info.filename).resolve()
                if info.filename.startswith(("/", "\\")) or ".." in Path(info.filename).parts \
                        or (target != root and root not in target.parents):
                    raise ValueError(f"Unsafe path inside the uploaded zip: {info.filename!r}")
            zf.extractall(dest_dir)
    except zipfile.BadZipFile as e:
        raise ValueError(f"The uploaded file is not a valid zip archive: {e}")

    jsonl_candidates = list(dest_dir.rglob("*.jsonl"))
    if not jsonl_candidates:
        raise ValueError("Uploaded zip does not contain a .jsonl dataset file (expected etsi_dataset.jsonl).")
    jsonl_path = jsonl_candidates[0]

    text_dir = jsonl_path.parent / "text"
    if not text_dir.exists():
        text_candidates = [p for p in dest_dir.rglob("text") if p.is_dir()]
        text_dir = text_candidates[0] if text_candidates else None
    return jsonl_path, text_dir


async def _get_or_create_document(session, row: DatasetRecordRow, job_id: int) -> EtsiDocument:
    """The EtsiDocument checkpoint row for a dataset record (see `_get_or_create_document_flag`)."""
    doc, _created = await _get_or_create_document_flag(session, row, job_id)
    return doc


async def _get_or_create_document_flag(session, row: DatasetRecordRow, job_id: int):
    """(EtsiDocument, created) for a record, keyed by the SHA-256 of its normalised PDF URL so a re-
    import or resume never duplicates documents; identity/provenance fields are refreshed from the
    record.
    """
    url_hash = hashlib.sha256(row.pdf_url.strip().lower().encode("utf-8")).hexdigest()
    result = await session.execute(select(EtsiDocument).where(EtsiDocument.url_hash == url_hash))
    doc = result.scalar_one_or_none()
    identity = row.raw.get("identity", {})
    provenance = row.raw.get("provenance", {})
    # Prefer the scraper's VERBATIM manifest cells (schema_version >= "1.1":
    # provenance.standard_name_raw / provenance.etsi_dir_raw -- see
    # etsi-scraper-pipeline's schemas/dataset_schema.py) over the
    # synthesized fallback below, since those are the actual manifest text
    # rather than a guess reconstructed from parsed identity fields. Older
    # datasets (schema_version "1.0") won't have these keys at all, so the
    # fallback keeps working for those unchanged.
    standard_name_raw = (
        _nz(provenance.get("standard_name_raw"))
        or identity.get("etsi_series") or identity.get("document_number")
    )
    etsi_dir_raw = _nz(provenance.get("etsi_dir_raw"))
    if doc is None:
        doc = EtsiDocument(
            pdf_url=row.pdf_url,
            url_hash=url_hash,
            etsi_dir=etsi_dir_raw,
            standard_name_raw=standard_name_raw,
            document_type=_nz(identity.get("document_type")),
            specification_number=_nz(identity.get("document_number")),
            version=_nz(identity.get("version")),
            status="pending",
            ingestion_job_id=job_id,
        )
        session.add(doc)
        await session.flush()
        return doc, True
    elif doc.ingestion_job_id is None:
        doc.ingestion_job_id = job_id
    return doc, False


def _nz(v) -> Optional[str]:
    """Empty values as None."""
    return v if v else None


def _parse_dt(v) -> Optional[datetime]:
    """Parses an ISO8601 timestamp string from the scraper's provenance
    (e.g. "2026-09-01T21:30:00.154369+00:00") into a naive UTC datetime for
    storage -- tolerant of None, empty, the literal "Unknown", or a
    malformed string (older/foreign datasets), in which case it returns
    None rather than raising or fabricating a time."""
    if not v or not isinstance(v, str) or v == "Unknown":
        return None
    try:
        dt = datetime.fromisoformat(v)
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt
    except ValueError:
        return None


import weakref as _weakref

_write_locks: "_weakref.WeakKeyDictionary" = _weakref.WeakKeyDictionary()


def _write_lock_for_loop() -> asyncio.Lock:
    """One write lock per running event loop. A module-level asyncio.Lock binds to the first loop
    that uses it and then breaks any later asyncio.run() (CLI re-runs, tests, a restarted worker loop)."""
    loop = asyncio.get_running_loop()
    lk = _write_locks.get(loop)
    if lk is None:
        lk = _write_locks[loop] = asyncio.Lock()
    return lk


async def _save_and_commit(session, doc: EtsiDocument, **fields) -> None:
    """Set fields on a document and commit under the per-event-loop write lock (SQLite allows one
    writer).
    """
    async with _write_lock_for_loop():
        for k, v in fields.items():
            setattr(doc, k, v)
        doc.updated_at = _utcnow()
        await session.flush()
        await session.commit()


def _apply_dataset_record(doc: EtsiDocument, record: dict) -> None:
    """Populates EtsiDocument fields directly from an already-extracted
    dataset record -- no LLM call here, the structured extraction already
    happened in the scraper. Never invents a value: any field absent from
    the record is left as "Unknown"."""
    identity = record.get("identity", {})
    classification = record.get("classification", {})
    technical_summary = record.get("technical_summary", {})
    patent_relevance = record.get("patent_relevance", {})
    extraction_provenance = record.get("extraction_provenance", {})
    provenance = record.get("provenance", {})
    # Clause records may carry their text under text/excerpt/content/... (the builder tolerates
    # these variants); the indexer reads ``summary``, so map it - verbatim, nothing rewritten.
    clauses = []
    for c in (record.get("clauses") or []):
        n = gd.normalise_clause(c)
        if n is not None:
            clauses.append({**c, **{k: v for k, v in n.items() if k != "summary" or not c.get("summary")}})

    doc.title = _nz(identity.get("title")) or "Unknown"
    doc.series = _nz(identity.get("etsi_series")) or doc.series or "Unknown"
    doc.specification_number = _nz(identity.get("document_number")) or doc.specification_number or "Unknown"
    doc.version = _nz(identity.get("version")) or doc.version or "Unknown"
    doc.release = _nz(identity.get("release")) or "Unknown"
    doc.document_type = _nz(identity.get("document_type")) or doc.document_type or "Unknown"
    doc.publication_date = _nz(identity.get("publication_date")) or "Unknown"
    # Backfill the verbatim manifest cells here too, in case this document
    # row was created by an OLDER import before these fields existed on the
    # incoming record (see _get_or_create_document's docstring above).
    doc.standard_name_raw = _nz(provenance.get("standard_name_raw")) or doc.standard_name_raw
    doc.etsi_dir = _nz(provenance.get("etsi_dir_raw")) or doc.etsi_dir

    doc.technology_domain = _nz(classification.get("technology_domain")) or "Unknown"
    doc.subdomain = _nz(classification.get("subdomain")) or "Unknown"
    doc.topics_json = json.dumps(classification.get("topics", []))
    doc.keywords_json = json.dumps(classification.get("technical_entities", []))
    doc.protocols_json = json.dumps(classification.get("protocols", []))
    doc.interfaces_json = json.dumps(classification.get("interfaces", []))
    doc.network_components_json = json.dumps(classification.get("network_components", []))

    doc.purpose = _nz(technical_summary.get("purpose")) or "Unknown"
    doc.scope = _nz(technical_summary.get("scope")) or "Unknown"
    doc.executive_summary = _nz(technical_summary.get("executive_summary")) or "Unknown"
    doc.technical_summary = _nz(technical_summary.get("technical_summary")) or "Unknown"
    doc.key_features_json = json.dumps(technical_summary.get("key_features", []))
    doc.major_procedures_json = json.dumps(technical_summary.get("important_procedures", []))
    doc.important_mechanisms_json = json.dumps(technical_summary.get("important_mechanisms", []))
    doc.important_parameters_json = json.dumps(technical_summary.get("important_parameters", []))
    doc.normative_requirements_json = json.dumps(technical_summary.get("normative_requirements", []))
    doc.clauses_json = json.dumps(clauses)

    doc.patent_relevant_concepts_json = json.dumps(patent_relevance.get("patent_relevant_concepts", []))
    doc.potentially_essential_features_json = json.dumps(patent_relevance.get("potentially_essential_features", []))
    doc.distinctive_mechanisms_json = json.dumps(patent_relevance.get("distinctive_mechanisms", []))
    doc.technical_terms_json = json.dumps(classification.get("terminology", []))

    doc.extraction_confidence = extraction_provenance.get("confidence", 0.0)
    doc.source_pages_json = json.dumps(extraction_provenance.get("source_pages", []))
    doc.llm_validation_status = extraction_provenance.get("validation_status", "not_attempted")
    doc.extraction_method = provenance.get("extraction_method") or "Unknown"

    # ── file / stage provenance (schema_version >= "1.1"; file_hash,
    #    file_size_bytes, local_pdf_path, retry_count already existed as
    #    columns from the old acquisition-pipeline schema but were never
    #    populated by the dataset-first import -- this is the fix for
    #    that). Absent on older ("1.0") datasets, in which case these
    #    simply stay whatever they already were (None/0 on a fresh row). ──
    doc.file_hash = _nz(provenance.get("file_hash")) or doc.file_hash
    doc.file_size_bytes = provenance.get("file_size_bytes") if provenance.get("file_size_bytes") is not None else doc.file_size_bytes
    doc.local_pdf_path = _nz(provenance.get("local_pdf_path")) or doc.local_pdf_path
    doc.structured_path = _nz(provenance.get("structured_path")) or doc.structured_path
    retry_count = provenance.get("retry_count")
    if isinstance(retry_count, int):
        doc.retry_count = retry_count
    doc.downloaded_at = _parse_dt(provenance.get("downloaded_at")) or doc.downloaded_at
    doc.extracted_at = _parse_dt(provenance.get("extracted_at")) or doc.extracted_at
    doc.completed_at = _parse_dt(provenance.get("completed_at")) or doc.completed_at


def _as_text_source(src) -> Optional["gd.TextSource"]:
    """Accept a TextSource, a text/ directory Path (legacy callers), or None."""
    if src is None or isinstance(src, gd.TextSource):
        return src
    return gd.DirTextSource(Path(src))


_DATASET_TEXT_MARK = "dataset-text:"


async def process_one_document(job_id: int, doc_id: int, record: dict, text_dir, copy_text: bool = True,
                               details: Optional[dict] = None) -> str:
    """Runs one EtsiDocument through import -> index. Never raises -- any
    failure is captured onto the row so the batch continues.

    ``details`` (optional dict) is filled with what happened - stage, error_type, error_message, retryable,
    attempts, chunk/part counts - so the importer can put it in the report instead of a bare outcome word.

    ``text_dir`` is a text/ folder (legacy), a gpp_dataset.TextSource (e.g. the canonical
    etsi_dataset.zip read in place) or None. Returns one of: completed | already_completed |
    import_failed | index_failed | error.
    """
    doc_id_for_log = doc_id
    pdf_url_for_log = "unknown"
    src = _as_text_source(text_dir)
    ident = record.get("identity", {}) or {}
    raw_text_name = (record.get("provenance", {}) or {}).get("raw_text_path")

    def _fetch_text() -> str:
        """Full text of the record from its text source (folder or ZIP), '' when unavailable."""
        return (src.get(raw_text_name, ident.get("document_id")) if src is not None else "") or ""

    async with AsyncSessionLocal() as session:
        result = await session.execute(select(EtsiDocument).where(EtsiDocument.id == doc_id))
        doc = result.scalar_one_or_none()
        if doc is None:
            return "error"
        if doc.status == "completed":
            return "already_completed"
        pdf_url_for_log = doc.pdf_url

        try:
            # ── stage: import (validate + copy text + populate fields) ──
            if doc.status in ("pending", "import_failed"):
                await _save_and_commit(session, doc, status="importing")

                text = _fetch_text()
                if not text.strip():
                    msg = "No extracted text found for this record in the uploaded dataset (missing or empty text file)"
                    await _save_and_commit(session, doc, status="import_failed", error_message=f"[no_text] {msg}",
                                           last_error_type="no_text", last_error_at=_utcnow())
                    if details is not None:
                        details.update(stage="import", error_type="no_text", error_message=msg, retryable=False)
                    return "import_failed"

                if copy_text:
                    local_text_path = str(_text_output_dir() / f"{doc.id}.txt")
                    Path(local_text_path).write_text(text, encoding="utf-8")
                else:
                    # canonical text stays in etsi_dataset.zip (read-only); remember only a reference
                    local_text_path = _DATASET_TEXT_MARK + str(raw_text_name or ident.get("document_id") or "")

                _apply_dataset_record(doc, record)
                await _save_and_commit(session, doc, status="imported", raw_extracted_text_path=local_text_path, error_message=None)

            # ── stage: index (clause-aware from the start -- no separate
            #    "basic then re-index" trick needed, since the dataset
            #    already contains the full structured extraction) ──
            if doc.status in ("imported", "index_failed"):
                await _save_and_commit(session, doc, status="indexing")
                stored = doc.raw_extracted_text_path or ""
                if stored.startswith(_DATASET_TEXT_MARK):
                    text = _fetch_text()
                else:
                    text_path = Path(stored)
                    text = text_path.read_text(encoding="utf-8") if stored and text_path.exists() else ""
                clauses = []
                try:
                    clauses = json.loads(doc.clauses_json) if doc.clauses_json else []
                except (json.JSONDecodeError, TypeError):
                    pass
                # NOTE (v0.4.4.1): the old chunks are NOT deleted up front any more. The indexer writes the
                # new chunks first and drops stale ones only after every group succeeded, so a failed
                # re-index can never leave the document with nothing.
                topics = []
                try:
                    topics = json.loads(doc.topics_json) if doc.topics_json else []
                except (json.JSONDecodeError, TypeError):
                    pass
                metadata = {
                    "standard_name": doc.standard_name_raw, "series": doc.series,
                    "specification_number": doc.specification_number, "release": doc.release,
                    "version": doc.version, "pdf_url": doc.pdf_url, "source_url": doc.pdf_url,
                    "technology_domain": doc.technology_domain,
                    "topic": topics[0] if topics else "Unknown",
                    "dataset_document_id": ident.get("document_id"),
                }
                pop_last_outcome()                       # clear any stale value from this task context
                vector_id, n_chunks = await index_document(doc.id, text, metadata=metadata, clauses=clauses)
                outcome = pop_last_outcome()
                attempts = (doc.index_attempts or 0) + (outcome.attempts if outcome else 1)
                if details is not None and outcome is not None:
                    details.update({k: v for k, v in outcome.as_dict().items()
                                    if k not in ("warnings",)}, warnings=list(outcome.warnings))
                if vector_id is None:
                    etype = (outcome.error_type if outcome else None) or "indexing_failed"
                    emsg = (outcome.error_message if outcome else None) or "Indexing failed"
                    await _save_and_commit(session, doc, status="index_failed", error_message=f"[{etype}] {emsg}"[:2000],
                                           last_error_type=etype, last_error_at=_utcnow(), index_attempts=attempts,
                                           text_chars=len(text))
                    if details is not None:
                        details.update(stage="index", error_type=etype, error_message=emsg,
                                       retryable=bool(outcome.retryable) if outcome else True)
                    return "index_failed"
                await _save_and_commit(session, doc, status="completed", vector_id=vector_id, chunks_indexed=n_chunks,
                                       error_message=None, last_error_type=None, last_error_at=None,
                                       index_attempts=attempts, text_chars=len(text))
                if details is not None:
                    details.update(stage="index", chunks_indexed=n_chunks)
                return "completed"
            return "error"
        except Exception as e:
            logger.error(f"Dataset import error for document {doc_id_for_log} ({pdf_url_for_log}): {e}", exc_info=True)
            try:
                await session.rollback()
                result2 = await session.execute(select(EtsiDocument).where(EtsiDocument.id == doc_id_for_log))
                doc2 = result2.scalar_one_or_none()
                if doc2 is not None:
                    # An unexpected exception used to leave the row in 'importing'/'indexing' (invisible
                    # to every report until a restart ran recover). Land it in the stage's failed state.
                    failed_state = {"importing": "import_failed", "indexing": "index_failed"}.get(doc2.status, doc2.status)
                    await _save_and_commit(session, doc2, status=failed_state,
                                           error_message=f"[unexpected_error] {type(e).__name__}: {e}"[:2000],
                                           last_error_type="unexpected_error", last_error_at=_utcnow())
                if details is not None:
                    details.update(stage="import" if (doc2 is not None and doc2.status == "import_failed") else "index",
                                   error_type="unexpected_error", error_message=f"{type(e).__name__}: {e}", retryable=True)
            except Exception as e2:
                logger.error(f"Document {doc_id_for_log}: could not even record the import error: {e2}")
            return "error"


_CHECKPOINT_COMMIT_BATCH = 250


async def _create_checkpoints(rows: list[DatasetRecordRow], job_id: int) -> list[int]:
    """Checkpoint rows (document ids) for a batch of records."""
    return [d for d, _c in await _create_checkpoints_flag(rows, job_id)]


async def _create_checkpoints_flag(rows: list[DatasetRecordRow], job_id: int) -> list[tuple[int, bool]]:
    """Checkpoint rows for a batch of records, with whether each one was newly created."""
    out: list[tuple[int, bool]] = []
    async with AsyncSessionLocal() as session:
        for i, row in enumerate(rows, start=1):
            doc, created = await _get_or_create_document_flag(session, row, job_id)
            out.append((doc.id, created))
            if i % _CHECKPOINT_COMMIT_BATCH == 0:
                await session.commit()
        await session.commit()
    return out


async def recover_interrupted_documents() -> int:
    """A backend crash / power loss can leave rows in the in-flight states 'importing' or
    'indexing', which no stage would ever pick up again. Reset them to their *_failed
    counterpart so the next import retries them. Skipped while any import runs in THIS process."""
    if _cancel_events:
        return 0
    n = 0
    async with AsyncSessionLocal() as session:
        for inflight, failed in (("importing", "import_failed"), ("indexing", "index_failed")):
            res = await session.execute(
                update(EtsiDocument).where(EtsiDocument.status == inflight).values(
                    status=failed, error_message="Interrupted before it finished (restart / crash) - retried on the next import"))
            n += res.rowcount or 0
        await session.commit()
    if n:
        logger.warning(f"Recovered {n} document(s) left in an in-flight state by an interrupted import")
    return n


def _reports_dir() -> Path:
    """Folder for per-job import reports and failure logs."""
    p = Path(settings.ETSI_DATASET_OUTPUT) / "reports"
    p.mkdir(parents=True, exist_ok=True)
    return p


def import_report_path(job_id: int) -> Path:
    """Path of the JSON report of one import job."""
    return _reports_dir() / f"import_job_{job_id}.json"


def read_import_report(job_id: int) -> Optional[dict]:
    """The JSON report of an import job, or None when absent/unreadable."""
    p = import_report_path(job_id)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _record_weight(data: dict) -> int:
    """Rough in-memory size of a record (its clause text dominates); used to flush a batch early."""
    w = 2048
    for c in (data.get("clauses") or []):
        if isinstance(c, dict):
            w += sum(len(v) for v in c.values() if isinstance(v, str))
    return w


FAILURE_REPORT_CAP = 200          # entries kept inline in the JSON report; the full list is streamed to a .jsonl file
_BREAKER_THRESHOLD = 25           # consecutive infrastructure failures (model/store down) before the job stops itself


def _memory_settings_snapshot() -> dict:
    """The memory/batching settings in effect, recorded in each import report so a run's behaviour can
    be reproduced.
    """
    keys = ("EMBEDDING_MODEL", "EMBEDDING_DEVICE", "EMBEDDING_MAX_SEQ_LENGTH", "EMBEDDING_BATCH_SIZE",
            "EMBEDDING_CPU_BATCH_SIZE", "EMBEDDING_MEMORY_BUDGET_MB",
            "ETSI_IMPORT_BATCH_SIZE", "ETSI_INDEX_GROUP_CHUNKS", "ETSI_LARGE_DOC_CHARS", "ETSI_MAX_DOC_CHARS",
            "ETSI_INDEX_DOC_RETRIES", "ETSI_MIN_FREE_RAM_MB", "ETSI_PROCESS_RSS_LIMIT_MB", "VECTOR_BACKEND")
    return {k: getattr(settings, k, None) for k in keys}


def failures_path(job_id: int) -> Path:
    """Path of the JSONL log of documents that failed in one import job."""
    return _reports_dir() / f"import_job_{job_id}_failures.jsonl"


class ImportPreflightError(ValueError):
    """The importer cannot index anything (embedding model or vector store unusable). Raised BEFORE
    any document is touched so 32,000 documents do not each fail for the same reason."""


async def _preflight_indexing() -> None:
    """Fail fast and clearly when indexing cannot work at all."""
    from rag.vector_store import get_vector_store, _embedder, EmbeddingModelUnavailable
    try:
        await get_vector_store().ensure_ready()
    except Exception as e:  # noqa: BLE001
        raise ImportPreflightError(f"The vector store ({settings.VECTOR_BACKEND}) could not be opened: {type(e).__name__}: {e}")
    try:
        await _embedder._get_model_async()
    except EmbeddingModelUnavailable as e:
        raise ImportPreflightError(f"The embedding model is unavailable: {e}")
    except Exception as e:  # noqa: BLE001
        raise ImportPreflightError(f"The embedding model could not be loaded: {type(e).__name__}: {e}")


async def import_dataset(
    jsonl_path: Path,
    text_dir=None,
    limit: Optional[int] = None,
    job_id: Optional[int] = None,
    *,
    versions: str = "all",
    series=(),
    specs=(),
    copy_text: Optional[bool] = None,
    total_hint: Optional[int] = None,
    fingerprint: Optional[str] = None,
    source_label: Optional[str] = None,
    only_failed: bool = False,
) -> int:
    """Imports an already-prepared dataset (JSONL + extracted text) into the shared knowledge
    base. Returns the job id.

    STREAMING: the JSONL (multi-GB for the full 3GPP build) is read one line at a time and
    processed in bounded batches (ETSI_STREAM_BATCH records), so memory stays flat whatever
    the file size. ``text_dir`` may be a text/ folder, a gpp_dataset.TextSource such as the
    canonical etsi_dataset.zip read in place, or None.

    ``versions`` = all | latest | latest-per-release; ``series`` / ``specs`` restrict the import.
    Records with an UNKNOWN version are always kept. Everything filtered out is counted in the
    job report - nothing is dropped silently. Re-running is idempotent (records are keyed by
    sha256(pdf_url); completed documents are not re-indexed).

    `limit=None` means "import every selected record" -- callers (CLI/API) must pass this
    explicitly; the API/CLI default to ETSI_DEFAULT_IMPORT_LIMIT rather than None, so a bare
    invocation can never accidentally kick off an unbounded import.
    """
    import time as _time
    t0 = _time.time()
    src = _as_text_source(text_dir)
    copy = settings.ETSI_COPY_TEXT_LOCALLY if copy_text is None else copy_text
    batch_size = max(1, int(settings.ETSI_STREAM_BATCH))
    batch_weight_cap = max(1, int(settings.ETSI_STREAM_BATCH_MAX_MB)) * 2**20
    jsonl_path = Path(jsonl_path)

    report: dict = {
        "contract": gd.CONTRACT_VERSION, "state": "starting", "jsonl": str(jsonl_path),
        "text_source": src.kind if src else "none", "source": source_label or jsonl_path.name,
        "dataset_fingerprint": fingerprint, "versions": versions, "series": list(series), "specs": list(specs),
        "limit": limit, "copy_text_locally": copy, "started_at": datetime.now(timezone.utc).isoformat(),
        "counts": {"lines_read": 0, "records_seen": 0, "malformed_lines": 0, "invalid_records": 0,
                   "filtered_out": 0, "duplicates_in_file": 0, "selected": 0, "new_documents": 0,
                   "already_present": 0, "completed": 0, "already_completed": 0, "import_failed": 0,
                   "index_failed": 0, "errors": 0, "unknown_version_records": 0, "history_pointers": 0,
                   "chunks_indexed": 0,
                   # v0.4.4.1 memory / indexing diagnostics
                   "large_documents": 0, "zero_chunk_documents": 0, "split_chunks": 0, "parts_created": 0,
                   "embedding_batch_splits": 0, "truncated_texts": 0, "retried_documents": 0,
                   "not_attempted": 0, "unindexed": 0},
        "selection": None, "errors_sample": [], "invalid_sample": [],
        # Every failed document, with why (first FAILURE_REPORT_CAP here; ALL of them in failures_file).
        "failures": [], "failures_by_error_type": {}, "failures_file": None,
        "warnings": [], "large_documents_sample": [], "zero_chunk_sample": [],
        "memory_settings": _memory_settings_snapshot(), "only_failed": bool(only_failed),
        "all_selected_indexed": None,
    }
    C = report["counts"]

    if not jsonl_path.is_file():
        raise ValueError(f"Dataset JSONL not found: {jsonl_path}")
    if src is None:
        logger.warning("No text source supplied -- every record will fail import (no extracted text).")

    await recover_interrupted_documents()

    async with AsyncSessionLocal() as session:
        if job_id is not None:
            result = await session.execute(select(EtsiIngestionJob).where(EtsiIngestionJob.id == job_id))
            job = result.scalar_one_or_none()
            if job is None:
                raise ValueError(f"Import job {job_id} not found")
        else:
            job = EtsiIngestionJob(manifest_path=str(jsonl_path), status="pending", limit_requested=limit)
            session.add(job)
            await session.flush()
        job.manifest_row_count = total_hint
        job.total_documents = min(total_hint, limit) if (total_hint and limit) else (total_hint or 0)
        job.status = "running"
        job.current_operation = "scanning"
        job.started_at = job.started_at or _utcnow()
        job_id = job.id
        await session.commit()
    report["job_id"] = job_id

    def _save_report(state: str) -> None:
        """Write the job report with its current state and duration (atomically; a reporting failure
        never fails the import).
        """
        report["state"] = state
        report["duration_seconds"] = round(_time.time() - t0, 2)
        try:
            gd.atomic_write_json(import_report_path(job_id), report)
        except OSError as e:  # reporting must never fail the import
            logger.warning(f"Could not write import report: {e}")

    _cancel_events[job_id] = asyncio.Event()
    _save_report("scanning")
    try:
        # Skipped only when the indexing function has been replaced (unit tests stub it); the real
        # indexer always needs a working model and store, and must say so BEFORE touching documents.
        if getattr(index_document, "__module__", "") == "etsi_corpus.indexer":
            await _preflight_indexing()
    except ImportPreflightError as pe:
        report["preflight_error"] = str(pe)
        _cancel_events.pop(job_id, None)
        _save_report("failed")
        async with AsyncSessionLocal() as session:
            j = (await session.execute(select(EtsiIngestionJob).where(EtsiIngestionJob.id == job_id))).scalar_one_or_none()
            if j is not None:
                j.status, j.error_message, j.current_operation = "failed", str(pe)[:2000], "failed"
                j.completed_at = _utcnow()
                await session.commit()
        raise

    stats = gd.JsonlStats()
    selected_ids = None
    fail_fh = None
    try:
        # Pre-pass only when a filter needs the whole picture (latest-per-spec, series, spec).
        if versions != "all" or series or specs:
            pre_stats = gd.JsonlStats()
            selected_ids, sel_counters = gd.select_document_ids(
                gd.scan_identities(jsonl_path, pre_stats), mode=versions, series=series, specs=specs)
            report["selection"] = sel_counters
            async with AsyncSessionLocal() as session:
                job = (await session.execute(select(EtsiIngestionJob).where(EtsiIngestionJob.id == job_id))).scalar_one()
                job.manifest_row_count = sel_counters["scanned"]
                job.total_documents = min(len(selected_ids), limit) if limit else len(selected_ids)
                await session.commit()

        async with AsyncSessionLocal() as session:
            job = (await session.execute(select(EtsiIngestionJob).where(EtsiIngestionJob.id == job_id))).scalar_one()
            job.current_operation = "importing+indexing"
            await session.commit()

        seen_hashes: set[str] = set()
        failed_hashes: Optional[set[str]] = None
        if only_failed:
            # retry mode: only documents currently in a *_failed state (interrupted ones were reset to
            # *_failed by recover_interrupted_documents above)
            async with AsyncSessionLocal() as session:
                failed_hashes = set((await session.execute(
                    select(EtsiDocument.url_hash).where(EtsiDocument.status.like("%_failed")))).scalars().all())
            report["retry_candidates"] = len(failed_hashes)
        n_workers_cfg = max(1, int(settings.ETSI_IMPORT_BATCH_SIZE))
        batch: list[DatasetRecordRow] = []
        batch_weight = 0
        stop_reading = False

        infra_streak = {"n": 0}

        def _record_failure(record: dict, etsi_id: int, outcome: str, d: dict) -> None:
            nonlocal fail_fh
            ident = (record.get("identity") or {})
            entry = {
                "dataset_document_id": ident.get("document_id"), "etsi_document_id": etsi_id,
                "specification": ident.get("document_number"), "version": ident.get("version"),
                "outcome": outcome, "stage": d.get("stage"), "error_type": d.get("error_type") or "unknown",
                "error_message": (d.get("error_message") or "")[:1000], "retryable": d.get("retryable", True),
                "attempts_this_run": d.get("attempts"), "text_chars": d.get("text_chars"),
                "chunks_planned": d.get("chunks_planned"), "chunks_rolled_back": d.get("rolled_back"),
                "group_size_used": d.get("group_size_used"), "max_tokens_seen": d.get("max_tokens_seen"),
                "retry_status": ("will be retried automatically by the next import run" if d.get("retryable", True)
                                 else "NOT retried until the cause is changed (see error_message)"),
                "at": datetime.now(timezone.utc).isoformat(),
            }
            bt = report["failures_by_error_type"]
            bt[entry["error_type"]] = bt.get(entry["error_type"], 0) + 1
            if len(report["failures"]) < FAILURE_REPORT_CAP:
                report["failures"].append(entry)
            try:
                if fail_fh is None:
                    fail_fh = open(failures_path(job_id), "a", encoding="utf-8")
                    report["failures_file"] = str(failures_path(job_id))
                fail_fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
                fail_fh.flush()
            except OSError as oe:  # reporting must never fail the import
                logger.warning(f"Could not append to the failures file: {oe}")
            if len(report["errors_sample"]) < 25:
                report["errors_sample"].append({"document_id": entry["dataset_document_id"], "outcome": outcome,
                                                "error_type": entry["error_type"], "error_message": entry["error_message"][:300]})

        def _account(d: dict, outcome: str) -> None:
            if d.get("large_document"):
                C["large_documents"] += 1
                if len(report["large_documents_sample"]) < 25:
                    report["large_documents_sample"].append({"etsi_document_id": None, "text_chars": d.get("text_chars"),
                                                             "chunks_planned": d.get("chunks_planned")})
            C["split_chunks"] += d.get("chunks_split") or 0
            C["parts_created"] += d.get("parts_created") or 0
            C["embedding_batch_splits"] += d.get("oom_retries") or 0
            C["truncated_texts"] += d.get("truncated_texts") or 0
            if (d.get("attempts") or 1) > 1:
                C["retried_documents"] += 1
            if outcome == "completed" and d.get("chunks_indexed") == 0:
                C["zero_chunk_documents"] += 1
                if len(report["zero_chunk_sample"]) < 25:
                    report["zero_chunk_sample"].append({"etsi_document_id": None, "text_chars": d.get("text_chars")})

        async def _run_batch(rows: list[DatasetRecordRow]) -> None:
            cps = await _create_checkpoints_flag(rows, job_id)
            for _id, created in cps:
                C["new_documents" if created else "already_present"] += 1
            queue: asyncio.Queue = asyncio.Queue()
            for row, (did, _c) in zip(rows, cps):
                queue.put_nowait((did, row.raw))

            async def _worker() -> None:
                while True:
                    try:
                        did, record = queue.get_nowait()
                    except asyncio.QueueEmpty:
                        return
                    try:
                        if is_stop_requested(job_id):
                            C["not_attempted"] += 1
                            continue
                        details: dict = {}
                        outcome = await process_one_document(job_id, did, record, src, copy_text=copy, details=details)
                        key = {"completed": "completed", "already_completed": "already_completed",
                               "import_failed": "import_failed", "index_failed": "index_failed"}.get(outcome, "errors")
                        C[key] += 1
                        _account(details, outcome)
                        if outcome in ("import_failed", "index_failed", "error"):
                            _record_failure(record, did, outcome, details)
                            infra_streak["n"] = infra_streak["n"] + 1 if details.get("error_type") in INFRASTRUCTURE_ERRORS else 0
                            if infra_streak["n"] >= _BREAKER_THRESHOLD and not is_stop_requested(job_id):
                                report["warnings"].append(
                                    f"Stopped after {infra_streak['n']} consecutive '{details.get('error_type')}' failures - "
                                    "the embedding model or vector store is not working. Fix it and re-run to resume.")
                                logger.error(report["warnings"][-1])
                                request_stop(job_id)
                        else:
                            infra_streak["n"] = 0
                    except Exception as e:
                        C["errors"] += 1
                        _record_failure(record, did, "error", {"stage": "worker", "error_type": "unexpected_error",
                                                              "error_message": f"{type(e).__name__}: {e}", "retryable": True})
                        logger.error(f"Import worker crashed processing document {did} (continuing with next): {e}", exc_info=True)
                    finally:
                        queue.task_done()

            workers = [asyncio.create_task(_worker()) for _ in range(max(1, min(n_workers_cfg, len(rows))))]
            results = await asyncio.gather(*workers, return_exceptions=True)
            for r in results:
                if isinstance(r, Exception):
                    logger.error(f"Import worker task ended with an unhandled exception: {r}", exc_info=r)
            await _update_job_columns(job_id, C)
            C["lines_read"], C["malformed_lines"] = stats.lines, stats.malformed + stats.not_object
            _save_report("running")

        for _line_no, data in gd.iter_jsonl(jsonl_path, stats):
            C["records_seen"] += 1
            identity = data.get("identity") if isinstance(data.get("identity"), dict) else {}
            document_id, pdf_url = identity.get("document_id"), identity.get("pdf_url")
            if not document_id or not pdf_url:
                C["invalid_records"] += 1
                if len(report["invalid_sample"]) < 25:
                    report["invalid_sample"].append({"line": _line_no, "problem": "missing identity.document_id or identity.pdf_url"})
                continue
            if selected_ids is not None and document_id not in selected_ids:
                C["filtered_out"] += 1
                continue
            h = hashlib.sha256(str(pdf_url).strip().lower().encode("utf-8")).hexdigest()
            if h in seen_hashes:
                C["duplicates_in_file"] += 1
                continue
            if failed_hashes is not None and h not in failed_hashes:
                C["filtered_out"] += 1
                continue
            seen_hashes.add(h)
            if limit is not None and C["selected"] >= limit:
                stop_reading = True
                break
            C["selected"] += 1
            if gd.is_unknown(identity.get("version")):
                C["unknown_version_records"] += 1
            C["history_pointers"] += sum(1 for _ in gd.history_refs(data.get("spec_history")))
            data.pop("spec_history", None)          # not used downstream; free the memory now
            batch.append(DatasetRecordRow(document_id=document_id, pdf_url=pdf_url, raw=data))
            batch_weight += _record_weight(data)
            if len(batch) >= batch_size or batch_weight >= batch_weight_cap:
                await _run_batch(batch)
                batch, batch_weight = [], 0
                if is_stop_requested(job_id):
                    stop_reading = True
                    break
        if batch and not is_stop_requested(job_id):
            await _run_batch(batch)
        C["lines_read"], C["malformed_lines"] = stats.lines, stats.malformed + stats.not_object
        if stats.errors:
            report["jsonl_errors_sample"] = stats.errors[:25]
        stopped = is_stop_requested(job_id)
        _ = stop_reading
    except Exception:
        _cancel_events.pop(job_id, None)
        _save_report("failed")
        raise
    finally:
        if fail_fh is not None:
            try:
                fail_fh.close()
            except OSError:
                pass
        if src is not None and not isinstance(text_dir, gd.TextSource):
            src.close()          # we created it (legacy folder path); caller-owned sources stay open

    _cancel_events.pop(job_id, None)
    await _finalize_job(job_id, stopped=stopped, run_counts=C)
    try:
        # chunk statistics straight from SQL (no row loading)
        async with AsyncSessionLocal() as session:
            tot = (await session.execute(select(func.coalesce(func.sum(EtsiDocument.chunks_indexed), 0)).where(
                EtsiDocument.ingestion_job_id == job_id))).scalar_one()
            C["chunks_indexed"] = int(tot or 0)
    except Exception:  # noqa: BLE001
        pass
    failed_n = C["import_failed"] + C["index_failed"] + C["errors"]
    C["unindexed"] = max(0, C["selected"] - C["completed"] - C["already_completed"])
    report["all_selected_indexed"] = (not stopped) and C["unindexed"] == 0
    if stopped:
        state = "paused"
        report["warnings"].append(f"Stopped before the end: {C['unindexed']} selected record(s) are not indexed yet, and records "
                                  "after the last one read were not read. Re-run the same command to resume.")
    elif failed_n:
        state = "completed_with_errors"
        report["warnings"].append(
            f"{failed_n} document(s) are NOT indexed (see 'failures' / {report['failures_file']}). "
            "Re-running the same command retries only those; completed documents are skipped, never duplicated.")
    elif C["zero_chunk_documents"] or C["truncated_texts"]:
        state = "completed_with_warnings"
        if C["zero_chunk_documents"]:
            report["warnings"].append(f"{C['zero_chunk_documents']} document(s) completed but produced 0 searchable chunks (text too short to chunk).")
        if C["truncated_texts"]:
            report["warnings"].append(f"{C['truncated_texts']} text(s) were truncated by the embedding model.")
    else:
        state = "completed"
    _save_report(state)
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    _save_report(report["state"])
    return job_id


def _compute_job_counters(docs: list) -> dict:
    """Job progress counters derived from a list of document rows."""
    return {
        "documents_downloaded": sum(1 for d in docs if d.status not in ("pending", "import_failed")),
        "documents_extracted": sum(1 for d in docs if d.status not in ("pending", "import_failed", "importing")),
        "documents_indexed": sum(1 for d in docs if d.status in SEARCHABLE_STATUSES),
        "documents_llm_processed": sum(1 for d in docs if d.status in ("imported", "indexing", "completed")),
        "documents_failed": sum(1 for d in docs if d.status.endswith("_failed")),
    }


def _counters_from_status_counts(counts: dict) -> dict:
    """Same numbers as _compute_job_counters, from a {status: n} map (one SQL GROUP BY, no row loading)."""
    def n(*sts): return sum(v for k, v in counts.items() if k in sts)
    total = sum(counts.values())
    return {
        "documents_downloaded": total - n("pending", "import_failed"),
        "documents_extracted": total - n("pending", "import_failed", "importing"),
        "documents_indexed": n(*SEARCHABLE_STATUSES),
        "documents_llm_processed": n("imported", "indexing", "completed"),
        "documents_failed": sum(v for k, v in counts.items() if k.endswith("_failed")),
    }


async def _status_counts(job_id: int) -> dict:
    """Document count per status for one import job (one GROUP BY query)."""
    async with AsyncSessionLocal() as session:
        rows = (await session.execute(
            select(EtsiDocument.status, func.count()).where(EtsiDocument.ingestion_job_id == job_id).group_by(EtsiDocument.status)
        )).all()
    return {st: int(n) for st, n in rows}


async def _update_job_columns(job_id: int, run_counts: dict) -> None:
    """Live job counters for the UI: this run's outcomes (so a re-import job over an already
    indexed dataset shows its records as indexed, not 0)."""
    done = run_counts.get("completed", 0) + run_counts.get("already_completed", 0)
    failed = run_counts.get("import_failed", 0) + run_counts.get("index_failed", 0) + run_counts.get("errors", 0)
    async with AsyncSessionLocal() as session:
        job = (await session.execute(select(EtsiIngestionJob).where(EtsiIngestionJob.id == job_id))).scalar_one_or_none()
        if job is None:
            return
        job.documents_indexed = done
        job.documents_failed = failed
        job.documents_downloaded = job.documents_extracted = job.documents_llm_processed = done + run_counts.get("index_failed", 0)
        if not job.total_documents or job.total_documents < run_counts.get("selected", 0):
            job.total_documents = run_counts.get("selected", 0)
        await session.commit()


async def _refresh_job_counters(job_id: int) -> None:
    """Recompute and store a job's progress counters from its documents."""
    counters = _counters_from_status_counts(await _status_counts(job_id))
    async with AsyncSessionLocal() as session:
        job = (await session.execute(select(EtsiIngestionJob).where(EtsiIngestionJob.id == job_id))).scalar_one_or_none()
        if job is None:
            return
        for k, v in counters.items():
            setattr(job, k, v)
        await session.commit()


async def _finalize_job(job_id: int, stopped: bool = False, run_counts: Optional[dict] = None) -> None:
    """Final counters and status of an import job: completed, completed with errors, stopped or failed."""
    run_counts = run_counts or {}
    if run_counts:
        await _update_job_columns(job_id, run_counts)
    else:
        await _refresh_job_counters(job_id)
    async with AsyncSessionLocal() as session:
        job = (await session.execute(select(EtsiIngestionJob).where(EtsiIngestionJob.id == job_id))).scalar_one_or_none()
        if job is None:
            return
        pending_left = 0
        if stopped:
            counts = await _status_counts(job_id)
            pending_left = sum(v for k, v in counts.items() if k in ("pending", "importing", "imported", "indexing"))
        if stopped:
            all_done = pending_left == 0 and (run_counts.get("selected", 0) <= (job.documents_indexed + job.documents_failed))
            job.status = "completed_with_errors" if (all_done and job.documents_failed) else ("completed" if all_done else "paused")
        else:
            job.status = "completed" if job.documents_failed == 0 else "completed_with_errors"
        job.current_operation = job.status
        job.completed_at = _utcnow()
        total_docs = job.documents_indexed
        await session.commit()

    if total_docs > settings.ETSI_AUTO_PACKAGE_MAX_DOCS:
        logger.info(f"Skipping the automatic portable-package build after job {job_id}: {total_docs} documents exceeds "
                    f"ETSI_AUTO_PACKAGE_MAX_DOCS={settings.ETSI_AUTO_PACKAGE_MAX_DOCS} (run `python -m etsi_corpus.cli package build` explicitly if wanted).")
        return
    try:
        from etsi_corpus.package import build_package
        await build_package()
    except Exception as e:
        logger.warning(f"Auto-build of the Standards Corpus package after job {job_id} failed (non-fatal): {e}")


async def job_status(job_id: int) -> dict:
    """API view of one import job: status, counters, timing and the current operation ({} when
    unknown).
    """
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(EtsiIngestionJob).where(EtsiIngestionJob.id == job_id))
        job = result.scalar_one_or_none()
        if job is None:
            return {}
        phase_counts = await _status_counts(job_id)
        counters = _counters_from_status_counts(phase_counts)
        # A run-level counter (kept live by the importer) wins when larger: a re-import job over
        # already-indexed documents owns no DB rows but did process them.
        counters["documents_indexed"] = max(counters["documents_indexed"], job.documents_indexed or 0)
        counters["documents_failed"] = max(counters["documents_failed"], job.documents_failed or 0) if job.status != "completed" else counters["documents_failed"]
        return {
            "id": job.id, "status": job.status, "current_operation": job.current_operation,
            "total_documents": job.total_documents,
            **counters,
            "phase_counts": phase_counts,
            "started_at": job.started_at.isoformat() if job.started_at else None,
            "completed_at": job.completed_at.isoformat() if job.completed_at else None,
            "error_message": job.error_message,
            "stoppable": job.status in ("pending", "running") and job_id in _cancel_events,
        }


async def failed_documents(job_id: Optional[int] = None) -> list[dict]:
    """Documents in a *_failed state (optionally of one job) with their error details."""
    async with AsyncSessionLocal() as session:
        stmt = select(EtsiDocument).where(EtsiDocument.status.like("%_failed"))
        if job_id is not None:
            stmt = stmt.where(EtsiDocument.ingestion_job_id == job_id)
        rows = (await session.execute(stmt)).scalars().all()
        return [
            {
                "id": d.id, "standard_name": d.standard_name_raw, "pdf_url": d.pdf_url,
                "failure_stage": d.status, "error_message": d.error_message,
                "retry_count": d.retry_count,
            }
            for d in rows
        ]
