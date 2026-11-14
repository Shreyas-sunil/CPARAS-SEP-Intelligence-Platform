"""
Analysis API Routes
===================
Core analysis endpoints. Replaces Gemini API calls with local LLM inference.
Supports streaming responses, batch processing, and RAG-enhanced analysis.
"""

import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime
from io import BytesIO
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, BackgroundTasks
from fastapi.responses import StreamingResponse, JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from core.config import settings
from core.database import get_db, Declaration, Patent, PortfolioAnalysis, ResearchRun
from core.llm_engine import get_llm
from core.ollama_client import validate_model_choice
from core.prompts import (
    FAST_SYSTEM_PROMPT, FAST_USER_PROMPT,
    DEEP_ENRICH_SYSTEM_PROMPT, ENRICH_BATCH_SIZE,
    slim_to_full,
    build_enrich_user_prompt, apply_enrichment,
)
from research.engine import analyze_patents
from utils.pdf_parser import extract_text_from_pdf, extract_text_from_pdf_with_confidence
from utils.csv_parser import looks_like_csv, parse_csv_declaration
from utils.xlsx_parser import looks_like_xlsx, parse_xlsx_declaration
from utils.html_parser import looks_like_html, parse_html_declaration
from utils.json_utils import (
    extract_json_safe, flatten_for_db, salvage_partial_array,
    build_candidate_index, verify_patent_fields, default_fuzzy_threshold_for,
    compute_row_completeness, COMPLETENESS_FLAG_THRESHOLD,
)
from utils.text_repair import reconstruct_all
from utils.export_safety import content_disposition
from utils.chunking import (
    ROW_ANCHOR_RE as _ROW_ANCHOR_RE_COMPILED,
    split_into_chunks as _split_into_chunks,
    estimate_chunk_token_budget,
    extraction_seems_incomplete,
)
from core.timeutil import utcnow as _utcnow

logger = logging.getLogger(__name__)
router = APIRouter()

# Row-anchor regex, row-safe chunking (with header preservation), and token
# budgeting all now live in utils/chunking.py (ISSUES 2, 4, 5) so they can be
# unit tested without a FastAPI/httpx/SQLAlchemy import chain. `_ROW_ANCHOR_RE`
# is kept as a module-level name here (a compiled pattern, not the raw
# string it used to be) purely so the handful of `re.findall(_ROW_ANCHOR_RE,
# ...)` call sites below don't all need touching individually.
_ROW_ANCHOR_RE = _ROW_ANCHOR_RE_COMPILED


# ── Progress tracking ────────────────────────────────────────────────────────
# Single-user local pipeline, so a plain in-memory dict is sufficient — no
# need for redis/celery-style job infra. Keyed by a job_id the frontend
# generates client-side and passes in on /analyze; polled via
# GET /progress/{job_id} while the (synchronous) /analyze call is in flight.
# Entries are pruned lazily on write so this can't grow unbounded across a
# long session.

# ── Upload limits ─────────────────────────────────────────────────────────────
# The analyze endpoint reads each upload fully into memory before parsing, so
# without a ceiling a single oversized POST is an out-of-memory kill for the
# whole backend process (one worker, shared with the LLM client and the vector
# store). These bounds are deliberately generous for real ETSI IPR annexes —
# the largest observed declarations are a few MB — while still bounding the
# worst case. Override via .env if a genuinely larger corpus needs processing.
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_BYTES", 100 * 1024 * 1024))  # 100 MB
MAX_UPLOAD_FILES = int(os.getenv("MAX_UPLOAD_FILES", 50))
_UPLOAD_CHUNK = 1024 * 1024
_PDF_MAGIC = b"%PDF-"
# job_id is client-generated and used directly as a dict key in the in-memory
# progress store. Bound its shape so a caller can't wedge megabytes of
# arbitrary text (or a huge number of distinct keys) into that store.
_JOB_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def _validate_job_id(job_id: Optional[str]) -> Optional[str]:
    """Reject malformed job ids rather than letting them into the store."""
    if job_id is None:
        return None
    if not _JOB_ID_RE.match(job_id):
        raise HTTPException(400, "job_id must be 1-128 chars of [A-Za-z0-9._-]")
    return job_id


# ── start / stop / pause / continue ─────────────────────────────────────────
# CPARAS's /analyze runs synchronously inside the request coroutine (see
# that endpoint's docstring for why), not as a fire-and-forget background
# task -- but since it's still just one coroutine on the same event loop,
# a *separate* concurrent request (POST .../pause or .../stop) can set a
# cooperative flag this one checks between units of work, exactly the same
# "finish the current unit, then stop before starting the next" pattern
# used by GITAS/CSARAS's background jobs (etsi_corpus/pipeline.py,
# csaras/dataset_pipeline.py, api/routes/gitas/dashboard.py). Checked at
# three granularities, each a clean stopping point that never leaves a
# document half-written: between files (the coarsest, always available),
# between extraction chunks, and between deep-enrichment batches.
#
# "Pause" and "Stop" share this one mechanism (same as the rest of the
# platform's job controls) -- both leave whatever's been fully processed
# so far persisted/returned, and the response is marked "stopped": true.
# There is no separate "Continue" state to resume mid-file from (CPARAS
# doesn't checkpoint partial-chunk state to disk) -- "continue" is simply
# re-submitting the files that weren't reached yet (the response's
# "skipped_files" list says exactly which those are) under the same
# job_id, so progress polling stays continuous across the two calls.
_control_events: dict[str, asyncio.Event] = {}


def _register_job(job_id: Optional[str]) -> None:
    """Create the stop/pause control event for an ISLD analysis job id (no-op without an id)."""
    if job_id:
        _control_events[job_id] = asyncio.Event()


def _unregister_job(job_id: Optional[str]) -> None:
    """Forget a finished analysis job's control event."""
    if job_id:
        _control_events.pop(job_id, None)


def request_analysis_stop(job_id: str) -> bool:
    """Ask a running analysis to stop at the next chunk/batch boundary. False when no such job is
    running in this process.
    """
    ev = _control_events.get(job_id)
    if ev is None:
        return False
    ev.set()
    return True


def is_analysis_stop_requested(job_id: Optional[str]) -> bool:
    """Whether a stop was requested for `job_id` (checked cooperatively between LLM chunks and
    enrichment batches).
    """
    if not job_id:
        return False
    ev = _control_events.get(job_id)
    return ev.is_set() if ev else False


def is_analysis_job_running(job_id: str) -> bool:
    """Whether `job_id` is an analysis currently running in this process."""
    return job_id in _control_events


async def _read_upload_limited(upload: UploadFile, max_bytes: int) -> tuple[bytes, str]:
    """
    Read an upload into memory, aborting once it exceeds `max_bytes`.

    Reading in bounded chunks matters: `await upload.read()` with no argument
    materialises the entire body regardless of size, so checking the length
    afterwards is already too late to prevent the memory spike.

    Returns (bytes, kind) where kind is "pdf", "csv", "xlsx", or "html"
    ("xlsx" covers both .xlsx/.xlsm and legacy .xls workbooks — see
    utils.xlsx_parser for the split between the two). The kind is decided
    from content plus filename here, at the boundary, rather than being
    re-derived at each downstream call site — one classification, made once,
    on the bytes we actually received.
    """
    buf = bytearray()
    while True:
        chunk = await upload.read(_UPLOAD_CHUNK)
        if not chunk:
            break
        buf.extend(chunk)
        if len(buf) > max_bytes:
            raise HTTPException(
                413,
                f"'{upload.filename}' exceeds the {max_bytes // (1024 * 1024)} MB "
                "upload limit.",
            )
    if not buf:
        raise HTTPException(400, f"'{upload.filename}' is empty.")

    data = bytes(buf)
    head = data[:4096]

    # Content-type is attacker-controlled; check the actual file signature.
    # A file that isn't one of the accepted formats otherwise reaches the
    # parser tiers and surfaces as an opaque low-level error several stages
    # later. The PDF magic number is checked first and wins outright, so a
    # PDF misnamed .csv still takes the PDF path rather than being fed to
    # the CSV reader as mojibake. Excel and HTML are checked next, ahead of
    # the CSV content-sniff, because an .xlsx's zip container or an HTML
    # page's markup would otherwise be read as (garbage) CSV text rather
    # than as the document it is.
    if head.lstrip().startswith(_PDF_MAGIC):
        return data, "pdf"
    if looks_like_xlsx(upload.filename, head):
        return data, "xlsx"
    if looks_like_html(upload.filename, head):
        return data, "html"
    if looks_like_csv(upload.filename, head):
        return data, "csv"

    raise HTTPException(
        415,
        f"'{upload.filename}' is not a supported file type. Upload a PDF "
        "declaration (%PDF- signature), a CSV/TSV annex export, an Excel "
        "workbook (.xlsx/.xls), or an HTML page.",
    )


# Structured tabular inputs share one downstream pipeline (see
# `_run_analysis`'s "Tabular (CSV/XLSX/HTML) front end" section); this maps
# each recognised tabular `kind` to the parser that turns its bytes into
# the same {"ok", "slim", "text", "meta"} shape.
_TABULAR_PARSERS = {
    "csv": parse_csv_declaration,
    "xlsx": parse_xlsx_declaration,
    "html": parse_html_declaration,
}

_progress_store: dict[str, dict] = {}
_PROGRESS_TTL_S = 3600  # drop entries not updated in the last hour


def _prune_progress() -> None:
    """Drop progress entries older than _PROGRESS_TTL_S so the in-memory progress store cannot grow
    without bound.
    """
    now = time.time()
    stale = [k for k, v in _progress_store.items() if now - v.get("updated_at", now) > _PROGRESS_TTL_S]
    for k in stale:
        _progress_store.pop(k, None)


def _set_progress(job_id: Optional[str], activity: Optional[str] = None, **fields) -> None:
    """Merge fields into the job's progress entry. No-op if job_id is None
    (e.g. older frontend builds that don't send one).

    `activity`, if given, is appended to a small rolling log (newest first,
    capped at 15 entries) so the frontend can render a live activity feed
    driven by real pipeline events instead of a simulated one.
    """
    if not job_id:
        return
    _prune_progress()
    entry = _progress_store.setdefault(job_id, {"job_id": job_id, "activity": []})
    entry.update(fields)
    if activity:
        entry.setdefault("activity", [])
        entry["activity"] = ([{"text": activity, "ts": time.time()}] + entry["activity"])[:15]
    entry["updated_at"] = time.time()


# ── Helpers ────────────────────────────────────────────────────────────────────
#
# _split_into_chunks (row-safe chunking with header preservation) and
# estimate_chunk_token_budget (multi-signal output-token budgeting) now live
# in utils/chunking.py — see ISSUES 2, 4, 5. Imported above.


def _document_ocr_confidence(extraction: Optional[dict]) -> Optional[float]:
    """
    Mean Tesseract word-confidence (0-100) across this document's pages, or
    None for a native-text-layer document (no OCR confidence to report).
    Shared by _analyze_in_chunks' token budgeting (ISSUE 5) and
    research.engine.analyze_patents' confidence blending (Additional Issue).
    """
    if not extraction:
        return None
    page_confidences = extraction.get("page_confidences") or []
    confs = [p.get("confidence") for p in page_confidences if isinstance(p.get("confidence"), (int, float))]
    return sum(confs) / len(confs) if confs else None


# ── v0.4.7: deep-enrichment answer cache ───────────────────────────────────────────────────────
_enrich_mem: Optional[dict] = None
_ENRICH_SIG = None


def _enrich_sig() -> str:
    """Short hash of the deep-enrichment system prompt. Part of every enrichment cache key, so changing
    the prompt automatically invalidates cached answers.
    """
    global _ENRICH_SIG
    if _ENRICH_SIG is None:
        import hashlib
        _ENRICH_SIG = hashlib.sha1(DEEP_ENRICH_SYSTEM_PROMPT.encode("utf-8")).hexdigest()[:10]
    return _ENRICH_SIG


def _enrich_key(model_name: str, d: dict) -> str:
    """Cache key for one disclosure's deep enrichment: model + prompt signature + normalised title +
    sorted declared standards. Family members (US/EP/CN rows of one invention) share a key and
    therefore one LLM answer.
    """
    import hashlib
    ex = d.get("explicit_data") or {}
    title = " ".join(str(ex.get("title") or "").lower().split())
    stds = "|".join(sorted({" ".join(str(x).upper().split()) for x in (ex.get("standards") or [])}))
    return hashlib.sha1(f"{model_name}\x1f{_enrich_sig()}\x1f{title}\x1f{stds}".encode("utf-8")).hexdigest()


def _enrich_cache_path():
    """Location of the on-disk enrichment cache (CACHE_DIR/isld_enrichment_cache.json)."""
    from pathlib import Path
    d = Path(settings.CACHE_DIR)
    d.mkdir(parents=True, exist_ok=True)
    return d / "isld_enrichment_cache.json"


def _enrich_cache_load() -> dict:
    """The enrichment cache, loaded from disk once per process (an unreadable file starts a fresh cache
    instead of failing the analysis).
    """
    global _enrich_mem
    if _enrich_mem is None:
        _enrich_mem = {}
        try:
            data = json.loads(_enrich_cache_path().read_text(encoding="utf-8"))
            if isinstance(data, dict):
                _enrich_mem = data
        except FileNotFoundError:
            pass
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Enrichment cache unreadable ({e}); starting a new one.")
    return _enrich_mem


def _enrich_cache_get(key: str) -> Optional[dict]:
    """Cached enrichment for `key`, or None (also None when ISLD_ENRICH_CACHE is off)."""
    if not getattr(settings, "ISLD_ENRICH_CACHE", True):
        return None
    v = _enrich_cache_load().get(key)
    return dict(v) if isinstance(v, dict) else None


def _enrich_cache_put(key: str, item: dict) -> None:
    """Store one enrichment answer (bounded to 50,000 entries, oldest dropped) and write the cache
    atomically via a temp file; write errors are only logged.
    """
    if not getattr(settings, "ISLD_ENRICH_CACHE", True):
        return
    mem = _enrich_cache_load()
    mem[key] = {k: v for k, v in item.items() if k != "n"}
    if len(mem) > 50000:
        for k in list(mem)[: len(mem) - 50000]:
            mem.pop(k, None)
    try:
        p = _enrich_cache_path()
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(mem), encoding="utf-8")
        os.replace(tmp, p)
    except OSError as e:
        logger.warning(f"Could not write the enrichment cache: {e}")


async def _deep_enrich_disclosures(disclosures: list, llm, filename: str, job_id: Optional[str] = None,
                                    model: Optional[str] = None) -> None:
    """
    Real second pass for Deep Mode: fills standards_analysis/sep_analysis/
    geographic_analysis/inferred_fields per patent instead of leaving
    slim_to_full()'s blank placeholders (which is what previously made
    every patent show "50%" SEP strength and collapse onto the same
    standard/version/clause match in research/engine.py).

    Batched at ENRICH_BATCH_SIZE — asking a 3B model to write a distinct
    paragraph of analysis for 20-40 patents in one call reintroduces the
    same reliability problem that caused the extraction under-count, so
    this is deliberately NOT single-shot the way slim extraction is.
    Mutates `disclosures` in place; never raises — a failed batch just
    leaves those patents with their existing (blank) enrichment fields.
    """
    # v0.4.7 speed: the enrichment prompt sees ONLY (declared standards, title). Family members of one
    # invention (US / EP / CN ... rows) share both, so the model is asked once per distinct input and the
    # answer is copied to its siblings; answers are also kept on disk per (model, prompt, input), so a
    # re-run or another declaration listing the same patent costs no LLM time at all.
    model_name = model or settings.OLLAMA_MODEL
    groups: dict[str, list] = {}
    for d in disclosures:
        groups.setdefault(_enrich_key(model_name, d), []).append(d)
    n_cached = 0
    for key in list(groups):
        item = _enrich_cache_get(key)
        if item is not None:
            for d in groups.pop(key):
                apply_enrichment([d], [{**item, "n": d.get("disclosure_number")}])
                n_cached += 1
    reps = [members[0] for members in groups.values()]
    rep_key = {id(members[0]): key for key, members in groups.items()}
    batches = [reps[i:i + ENRICH_BATCH_SIZE] for i in range(0, len(reps), ENRICH_BATCH_SIZE)]
    logger.info(
        f"[{filename}] Deep enrichment: {len(disclosures)} patents -> {len(reps)} distinct inputs "
        f"({n_cached} answered from cache) in {len(batches)} batch(es) of up to {ENRICH_BATCH_SIZE}"
    )
    if n_cached:
        _set_progress(job_id, activity=f"{n_cached} patent(s) enriched instantly from earlier analyses")
    if not batches:
        _set_progress(job_id, stage="deep_enrichment", chunk_index=0, total_chunks=0,
                      total_patents=len(disclosures), patents_completed=len(disclosures))
        return
    done_patents = [n_cached]

    # Batches are independent (each writes only into its own patents' dicts),
    # so run them concurrently instead of one at a time — see the CONCURRENCY
    # note in _analyze_in_chunks for the Ollama-side caveat. Tunable via
    # settings.OLLAMA_CONCURRENCY (.env: OLLAMA_CONCURRENCY) since each
    # concurrent call needs its own num_ctx-sized KV cache in Ollama.
    CONCURRENCY = settings.OLLAMA_CONCURRENCY
    semaphore = asyncio.Semaphore(CONCURRENCY)
    completed = [0]

    async def _process_one_batch(bi: int, batch: list):
        """Enrich one batch of disclosures with the LLM inside the concurrency semaphore: skipped
        entirely when a stop was requested before it started, served from the enrichment cache where
        possible, otherwise one LLM call whose JSON answer is merged back onto each disclosure. A
        failed batch leaves its disclosures with blank enrichment rather than failing the whole
        analysis.
        """
        async with semaphore:
            if is_analysis_stop_requested(job_id):
                # Cooperative stop: a batch not yet started when the stop
                # was requested is skipped outright (its patents simply
                # keep slim_to_full's blank enrichment placeholders,
                # which this function's own docstring already treats as
                # an acceptable "failed batch" outcome) -- a batch already
                # past this point finishes normally, never abandoned
                # mid-generation.
                completed[0] += 1
                return
            batch_input = [
                {
                    "n": d.get("disclosure_number"),
                    "standards": (d.get("explicit_data") or {}).get("standards") or [],
                    "title": (d.get("explicit_data") or {}).get("title") or "",
                }
                for d in batch
            ]
            user_msg = build_enrich_user_prompt(batch_input)
            try:
                # Small batch (up to ENRICH_BATCH_SIZE=6 patents) — keep the
                # stronger anti-loop settings here; this is the call where
                # the original 140x-identical-row degenerate loop was
                # actually observed, and a batch this small has little
                # legitimate long-range repetition for a wide penalty
                # window to falsely suppress.
                output = await llm.generate(
                    user_msg, system=DEEP_ENRICH_SYSTEM_PROMPT, temperature=0.0, max_tokens=3500,
                    repeat_penalty=1.3, repeat_last_n=512, model=model,
                )
                parsed = extract_json_safe(output)
                if not parsed:
                    # Same "broke partway through generation" failure mode as
                    # the extraction chunks — recover whatever individually
                    # well-formed entries exist rather than discarding the
                    # whole batch. This was previously unconditional data loss:
                    # logs showed the same batch truncating at a near-identical
                    # byte offset run after run, silently dropping enrichment
                    # for every patent in that batch of up to ENRICH_BATCH_SIZE.
                    parsed = salvage_partial_array(output, array_key="patents")
                    if not parsed:
                        logger.warning(f"[{filename}] Enrichment batch {bi+1}/{len(batches)}: no JSON recoverable (even after salvage), skipping")
                        return
                    logger.warning(f"[{filename}] Enrichment batch {bi+1}/{len(batches)}: full JSON parse failed, salvaged partial results")
                items = parsed.get("patents") or []
                apply_enrichment(batch, items)
                by_n = {it.get("n"): it for it in items if isinstance(it, dict)}
                for rep in batch:
                    it = by_n.get(rep.get("disclosure_number"))
                    key = rep_key.get(id(rep))
                    if it is None or key is None:
                        continue
                    _enrich_cache_put(key, it)
                    for sib in groups.get(key, [])[1:]:
                        apply_enrichment([sib], [{**it, "n": sib.get("disclosure_number")}])
                logger.info(
                    f"[{filename}] Enrichment batch {bi+1}/{len(batches)}: "
                    f"{len(items)}/{len(batch)} patents enriched"
                )
            except Exception as e:
                logger.warning(f"[{filename}] Enrichment batch {bi+1}/{len(batches)} failed: {e}")
            finally:
                completed[0] += 1
                done_patents[0] += sum(len(groups.get(rep_key.get(id(rep)), [rep])) for rep in batch)
                last_patent = batch_input[-1]["n"] if batch_input else None
                _set_progress(
                    job_id,
                    stage="deep_enrichment",
                    chunk_index=completed[0],
                    total_chunks=len(batches),
                    total_patents=len(disclosures),
                    patents_completed=min(done_patents[0], len(disclosures)),
                    current_patent=last_patent,
                    message=f"Deep-enriching {filename} — {completed[0]}/{len(batches)} batches done…",
                    activity=(
                        f"Patent {min(done_patents[0], len(disclosures))}/"
                        f"{len(disclosures)} analyzed"
                        + (f" (latest: {last_patent})" if last_patent else "")
                    ),
                )

    # Same fix as the extraction stage: publish `deep_enrichment` before the
    # batches start, not only from the per-batch `finally`. Otherwise the UI
    # stayed on the previous stage — and the progress fraction stayed frozen —
    # for the whole duration of the first (often multi-minute) batch.
    _set_progress(
        job_id,
        stage="deep_enrichment",
        chunk_index=0,
        total_chunks=len(batches),
        total_patents=len(disclosures),
        patents_completed=n_cached,
        current_patent=None,
        message=f"Deep-enriching {filename} — 0/{len(batches)} batches done…",
        activity=f"Starting deep analysis of {len(disclosures)} patent(s)…",
    )

    await asyncio.gather(*[
        _process_one_batch(bi, batch) for bi, batch in enumerate(batches)
    ])


async def _analyze_in_chunks(text: str, system: str, user_msg: str, llm, filename: str, job_id: Optional[str] = None,
                              model: Optional[str] = None, extraction: Optional[dict] = None) -> str:
    """
    Analyze declaration text in one shot if it fits the model's context
    window, falling back to row-safe chunking only for larger documents.

    `extraction` (the dict from extract_text_from_pdf_with_confidence, if
    available) feeds ISSUE 5's multi-signal token budgeting — OCR method
    and per-page confidence — instead of budgeting on row-count estimation
    alone. Optional/None is fully supported (falls back to the
    row-count-only formula) so existing callers that don't have this
    metadata handy keep working.

    CHUNK_SIZE=20000 matches num_ctx=settings.OLLAMA_NUM_CTX (default 24576,
    see llm_engine.py) with headroom for the system prompt and JSON output.
    Both the Samsung sample (~11K chars OCR'd) and the Nokia sample (~6-16K
    chars depending on extraction tier) fit in a single call, which is what
    actually fixes the count bug: splitting into smaller chunks was forcing
    2+ independent LLM calls per document, and a 3B model reliably miscounts
    near chunk boundaries with no shared context between calls (verified:
    chunking at 4000 chars produced 50 patents from Samsung's 40-row table
    and 26 from Nokia's 27-row table; chunking at 6000 chars still split the
    Nokia sample unnecessarily and reproduced the same salvage/truncation
    failure seen in production logs — the actual bug was chunking
    unnecessarily, not the chunking logic itself). CHUNK_SIZE must stay
    comfortably below OLLAMA_NUM_CTX's character-equivalent budget or this
    regresses again.
    """
    CHUNK_SIZE = 20000
    # v0.4.2: when the context is lowered so the model fits on the GPU (see
    # core/ollama_runtime.py), shrink the chunk proportionally — a prompt
    # larger than num_ctx would be silently truncated by Ollama.
    if settings.LLM_BACKEND == "ollama":
        try:
            from core.ollama_runtime import chunk_chars_for_ctx, plan_num_ctx
            ctx_plan = await plan_num_ctx(model or settings.OLLAMA_MODEL)
            CHUNK_SIZE = chunk_chars_for_ctx(int(ctx_plan["num_ctx"]))
            if CHUNK_SIZE < 20000:
                logger.info(f"[{filename}] num_ctx={ctx_plan['num_ctx']} ({ctx_plan['reason']}) "
                            f"-> chunk size {CHUNK_SIZE} chars")
        except Exception as e:  # planning is best-effort
            logger.debug(f"Context planning unavailable, using default chunk size: {e}")

    extraction = extraction or {}
    is_ocr = extraction.get("method") in ("tesseract", "vision_ollama", "fallback_best_effort")
    page_confidences = extraction.get("page_confidences") or []
    doc_confidence = _document_ocr_confidence(extraction)
    page_count = len(page_confidences) or None

    chunks = _split_into_chunks(text, chunk_size=CHUNK_SIZE)
    if len(chunks) == 1:
        logger.info(f"[{filename}] Single-shot analysis ({len(text)} chars, no chunking needed)")
    else:
        logger.info(f"[{filename}] Splitting into {len(chunks)} chunks of ~{CHUNK_SIZE} chars")

    all_disclosures = []
    metadata = {}
    portfolio = {}
    incomplete_chunks: list[int] = []

    # Chunks are independent — no chunk's output depends on another's — so
    # there's no reason to await them one at a time and pay the sum of their
    # latencies. CONCURRENCY bounds how many run at once, tunable via
    # settings.OLLAMA_CONCURRENCY (.env: OLLAMA_CONCURRENCY) — each
    # concurrent call here needs its own OLLAMA_NUM_CTX-sized KV cache, so
    # this is more VRAM-sensitive than the enrichment batches above. Chunking
    # is now rare (see CHUNK_SIZE docstring below), so this mostly matters
    # for genuinely large multi-hundred-row filings. NOTE: this only speeds
    # things up if Ollama itself will actually serve concurrent requests to
    # the same model; set OLLAMA_NUM_PARALLEL as an environment variable
    # (matching OLLAMA_CONCURRENCY) before starting `ollama serve` if you
    # don't see a speedup.
    CONCURRENCY = settings.OLLAMA_CONCURRENCY
    semaphore = asyncio.Semaphore(CONCURRENCY)
    completed = [0]
    # (chunk index, reason) for every chunk that produced NOTHING usable.
    # Previously these were logged and dropped, so a model crash / out-of-
    # memory on every chunk made the file "succeed" with zero disclosures.
    failed_chunks: list[tuple[int, str]] = []

    async def _run_one_generation(chunk_msg: str, max_tokens: int):
        """One LLM call + parse attempt. Returns (discs, metadata, portfolio,
        was_salvaged) or None on hard failure."""
        chunk_output = await llm.generate(
            chunk_msg, system=system, temperature=0.0, max_tokens=max_tokens,
            model=model,
            # Gentler than the enrichment call's settings: a single-shot
            # extraction call legitimately reuses the same JSON key names
            # and similar values across every row (up to ~40 of them here),
            # and the enrichment call's wide repeat_last_n=512 penalizes
            # that normal columnar repetition as if it were a stuck loop —
            # verified: it broke a 30-row single-shot extraction down to 4
            # salvageable patents. A short lookback still catches a
            # token/row being emitted many times in immediate succession
            # (the actual observed loop failure mode) without punishing key
            # reuse across rows that are tokens apart.
            repeat_penalty=1.15, repeat_last_n=128,
        )
        parsed_chunk = extract_json_safe(chunk_output)
        was_salvaged = False
        if not parsed_chunk:
            # Full-object parse failed — try to salvage individually
            # well-formed patent entries from a response that broke
            # partway through a long generation, rather than discarding
            # everything. This recovers partial results (e.g. 38 of 40
            # patents) instead of returning zero.
            parsed_chunk = salvage_partial_array(chunk_output)
            if not parsed_chunk:
                return None
            was_salvaged = True

        if "patents" in parsed_chunk:
            parsed_chunk = slim_to_full(parsed_chunk)
        discs = (
            parsed_chunk.get("patent_disclosures")
            or parsed_chunk.get("patents")
            or []
        )
        return {
            "discs": discs,
            "metadata": parsed_chunk.get("declaration_metadata"),
            "portfolio": parsed_chunk.get("portfolio_level_analysis"),
            "was_salvaged": was_salvaged or bool(parsed_chunk.get("_partial")),
        }

    async def _process_one_chunk(i: int, chunk: str):
        """Run the extraction prompt on one document chunk (with retries/repair of malformed JSON)
        inside the concurrency semaphore. Returns the parsed result, or None for a chunk that was
        skipped by a stop request or could not be recovered (reported as an incomplete chunk).
        """
        async with semaphore:
            if is_analysis_stop_requested(job_id):
                # Same cooperative-stop policy as the enrichment batches
                # above: a chunk not yet started is skipped (treated like
                # any other unrecoverable chunk -- see `incomplete_chunks`
                # handling below), never one already mid-generation.
                completed[0] += 1
                return None
            if len(chunks) == 1:
                chunk_msg = f"{user_msg}\n\nDocument content:\n{chunk}"
            else:
                chunk_msg = f"{user_msg}\n\nDocument content (part {i+1} of {len(chunks)}):\n{chunk}"
            # Estimate real row count in *this* chunk to size the output
            # budget — see rationale above the function.
            est_rows = max(1, len(re.findall(_ROW_ANCHOR_RE, chunk)))
            # ISSUE 5: budget from multiple signals (chunk length, OCR
            # confidence, page density), not est_rows alone — see
            # utils/chunking.estimate_chunk_token_budget's docstring for the
            # full rationale (this replaces the previous
            # `min(18000, est_rows*300+1200)` formula, which silently
            # under-budgeted whenever OCR under-detected the true row count).
            chunk_max_tokens = estimate_chunk_token_budget(
                chunk, est_rows, ocr_confidence=doc_confidence, is_ocr=is_ocr,
                page_count=page_count,
            )
            try:
                outcome = await _run_one_generation(chunk_msg, chunk_max_tokens)
                if outcome is None:
                    logger.warning(
                        f"[{filename}] Chunk {i+1}/{len(chunks)}: no JSON "
                        "recoverable (even after salvage attempt) — skipping"
                    )
                    failed_chunks.append((i, "the model's answer contained no readable JSON"))
                    return None

                # ISSUE 5: never silently accept a partial extraction.
                # Retry once with a materially bigger budget if the result
                # looks incomplete (either it needed salvage_partial_array
                # at all, or far fewer rows came back than row-detection
                # expected for this chunk).
                if extraction_seems_incomplete(
                    len(outcome["discs"]), est_rows, was_salvaged=outcome["was_salvaged"]
                ):
                    retry_tokens = min(int(chunk_max_tokens * 1.6) + 1500, 22000)
                    if retry_tokens > chunk_max_tokens:
                        logger.warning(
                            f"[{filename}] Chunk {i+1}/{len(chunks)}: extraction looks "
                            f"incomplete ({len(outcome['discs'])} patent(s) parsed vs. "
                            f"~{est_rows} row(s) detected, salvaged={outcome['was_salvaged']}) "
                            f"— retrying with a larger budget ({chunk_max_tokens} -> {retry_tokens} tokens)."
                        )
                        retry_outcome = await _run_one_generation(chunk_msg, retry_tokens)
                        if retry_outcome is not None and len(retry_outcome["discs"]) >= len(outcome["discs"]):
                            outcome = retry_outcome
                    if extraction_seems_incomplete(
                        len(outcome["discs"]), est_rows, was_salvaged=outcome["was_salvaged"]
                    ):
                        # Still incomplete after the retry — surface it
                        # rather than accepting silently. Downstream code
                        # (below) collects these into `incomplete_chunks`
                        # and attaches a `_possibly_incomplete` flag to the
                        # final result so the frontend/report can show it.
                        incomplete_chunks.append(i)
                        logger.warning(
                            f"[{filename}] Chunk {i+1}/{len(chunks)}: still looks "
                            f"incomplete after retry ({len(outcome['discs'])} patent(s) "
                            f"vs. ~{est_rows} row(s) expected) — accepting the partial "
                            "result but flagging it; consider re-running with a smaller "
                            "MAX_ROWS_PER_CHUNK or investigating this chunk's source text."
                        )

                if outcome["was_salvaged"]:
                    logger.warning(
                        f"[{filename}] Chunk {i+1}/{len(chunks)}: full JSON parse "
                        "failed, but partial results were salvaged — see warning "
                        "above for how many entries were recovered"
                    )
                result = {"discs": outcome["discs"], "metadata": outcome["metadata"], "portfolio": outcome["portfolio"]}
            except Exception as e:
                import traceback
                logger.error(f"[{filename}] Chunk {i+1}/{len(chunks)} (model={model or settings.OLLAMA_MODEL}) "
                             f"failed: {type(e).__name__}: {e}\n{traceback.format_exc()}")
                failed_chunks.append((i, f"{type(e).__name__}: {e}"))
                return None
            finally:
                completed[0] += 1
                _set_progress(
                    job_id,
                    stage="analyzing",
                    chunk_index=completed[0],
                    total_chunks=len(chunks),
                    message=(
                        f"Analyzing {filename}…" if len(chunks) == 1
                        else f"Analyzing {filename} — {completed[0]}/{len(chunks)} chunks done…"
                    ),
                    activity=(
                        f"Running LLM analysis on {filename}…" if len(chunks) == 1
                        else f"LLM analysis: chunk {completed[0]}/{len(chunks)} done"
                    ),
                )
            return result

    # Announce the stage BEFORE the work starts. Previously `analyzing` was
    # only ever published from the per-chunk `finally` block, i.e. after the
    # first chunk had already completed — so for the entire duration of the
    # single-chunk case (the common one) the UI still showed
    # "OCR & Text Extraction" as the active stage while the LLM was actually
    # running, and the progress fraction stayed frozen at its extraction-time
    # value for minutes. A frozen fraction is exactly what made the
    # remaining-time estimate inflate without bound.
    _set_progress(
        job_id,
        stage="analyzing",
        chunk_index=0,
        total_chunks=len(chunks),
        message=(
            f"Analyzing {filename}…" if len(chunks) == 1
            else f"Analyzing {filename} — 0/{len(chunks)} chunks done…"
        ),
        activity=(
            f"Running LLM analysis on {filename}…" if len(chunks) == 1
            else f"Running LLM analysis on {filename} ({len(chunks)} chunks)…"
        ),
    )

    chunk_results = await asyncio.gather(*[
        _process_one_chunk(i, chunk) for i, chunk in enumerate(chunks)
    ])

    stopped_early = is_analysis_stop_requested(job_id)
    if chunks and len(failed_chunks) == len(chunks) and not stopped_early:
        # Nothing at all came back: report the real cause for THIS file
        # instead of a "successful" analysis with no disclosures.
        reason = failed_chunks[0][1]
        raise RuntimeError(
            f"LLM analysis failed for {filename}"
            + (f" (all {len(chunks)} parts)" if len(chunks) > 1 else "")
            + f": {reason}"
        )
    if failed_chunks:
        logger.warning(f"[{filename}] {len(failed_chunks)}/{len(chunks)} chunk(s) produced no result: "
                       + "; ".join(f"part {i + 1}: {r}" for i, r in failed_chunks[:5]))

    # Merge in original chunk order (not completion order) so disclosure
    # numbering and any positional assumptions downstream stay stable.
    for i, result in enumerate(chunk_results):
        if result is None:
            continue
        discs = result["discs"]
        for idx, d in enumerate(discs):
            d["disclosure_number"] = len(all_disclosures) + idx + 1
        all_disclosures.extend(discs)
        if not metadata and result["metadata"]:
            metadata = result["metadata"]
        if not portfolio and result["portfolio"]:
            portfolio = result["portfolio"]
        logger.info(
            f"[{filename}] Chunk {i+1}/{len(chunks)}: +{len(discs)} patents "
            f"(running total: {len(all_disclosures)})"
        )

    # SOURCE-ROW PRESERVATION: no deduplication. Every disclosure row
    # extracted from the declaration is kept, in source order, even when
    # it repeats an application/publication number, title or company that
    # appears elsewhere in the same declaration -- ETSI declaration tables
    # legitimately repeat identifiers (e.g. the same patent declared
    # against several standards or family members), and each source row
    # is authoritative. Chunks are split on row boundaries without overlap
    # (see utils/chunking.split_into_chunks), so no row is fed to the model twice.
    # Disclosures are simply renumbered 1..N in their merged source order.
    for idx, d in enumerate(all_disclosures):
        d["disclosure_number"] = idx + 1

    merged = {
        "declaration_metadata": metadata,
        "patent_disclosures": all_disclosures,
        "portfolio_level_analysis": portfolio,
        # ISSUE 5: surfaced even after a successful merge — a chunk that
        # stayed incomplete after the retry above still contributed
        # SOMETHING, so the overall parse isn't a hard failure, but the
        # result must not look indistinguishable from a document where
        # every chunk fully completed.
        "_possibly_incomplete": bool(incomplete_chunks or failed_chunks),
        "_incomplete_chunk_count": len(incomplete_chunks) + len(failed_chunks),
        "_failed_chunks": [{"part": i + 1, "reason": r} for i, r in failed_chunks],
    }
    return json.dumps(merged, ensure_ascii=False)


def _attach_csv_family_data(csv_slim: dict, parsed: dict) -> None:
    """
    Re-attach PATENT FAMILY columns that slim_to_full() doesn't carry.

    The ETSI annex's trailing "Other members of this PATENT FAMILY" columns
    have no home in the slim schema — the LLM path never had them, so
    slim_to_full() has no key for them and silently drops anything extra.
    Rather than widen the shared schema (and change behaviour for the PDF
    path, which cannot populate these), the CSV parser stashes them under
    `_family` and they are grafted back on here, into explicit_data where
    the rest of the declared-as-written data lives.

    Matched by disclosure number rather than list position, because
    continuation-row merging inside slim_to_full() collapses rows and so
    index i of the input does not correspond to index i of the output.
    """
    by_number = {
        p.get("n"): p["_family"]
        for p in (csv_slim.get("patents") or [])
        if p.get("_family") and p.get("n") is not None
    }
    if not by_number:
        return
    for disc in parsed.get("patent_disclosures") or []:
        family = by_number.get(disc.get("disclosure_number"))
        if not family:
            continue
        disc.setdefault("explicit_data", {})["patent_family"] = {
            "application_number": family.get("family_app", ""),
            "publication_number": family.get("family_pub", ""),
            "country": family.get("family_cc", ""),
        }


async def _run_analysis(file_bytes: bytes, filename: str, mode: str,
                        use_rag: bool, db: AsyncSession, job_id: Optional[str] = None,
                        model: Optional[str] = None, kind: str = "pdf") -> dict:
    """
    Core analysis pipeline — optimised for small/fast local models:
    1. Extract text from PDF (or map columns directly, for CSV input)
    2. Always use the FAST slim-schema prompt (tiny output = fast inference)
    3. Analyze in a single LLM call when the document fits ~20K chars
       (matches num_ctx=8192), falling back to row-safe chunking only
       for larger documents
    4. Convert slim output → full schema via slim_to_full()
    5. Run research augmentation (embeddings, figures, metrics)
    6. Persist to database

    `kind` selects the front end of the pipeline only. Steps 4b onward —
    deep enrichment, per-field verification, research augmentation,
    persistence — are shared verbatim between PDF, CSV, Excel and HTML
    inputs, so a CSV, XLSX or HTML declaration produces exactly the same
    result structure, database rows and frontend behaviour as the
    equivalent PDF. A recognised CSV/XLSX/HTML table skips steps 2-3
    entirely: its structure was never lost, so there is nothing for the
    extraction model to recover and every reason not to give it the
    opportunity to invent something.
    """
    llm = get_llm()

    # ── Tabular (CSV/XLSX/HTML) front end ───────────────────────────────
    # Populated only for CSV, Excel or HTML input, and only when the header
    # scan actually recognised the columns. When it's set, the LLM
    # extraction call below is skipped; when a spreadsheet or HTML table
    # fails to map, this stays None and the file falls through to the
    # identical text-extraction path a PDF takes, with the sheet/page
    # rendered as text. Either way, everything after step 4 is shared, so
    # there is exactly one copy of enrichment/verification/research/
    # persistence rather than a parallel implementation per tabular format
    # that can drift out of sync.
    csv_slim: Optional[dict] = None
    csv_meta: Optional[dict] = None

    # 1. PDF text extraction — use the confidence-aware variant so low-
    # quality OCR pages are visible before this text ever reaches the LLM.
    # Garbled OCR is the root cause behind bogus standard codes/numbers: the
    # LLM doesn't echo back garbage, it "corrects" it toward a plausible-
    # looking value from training, which is how a mangled "TS 38.331" turns
    # into a confidently wrong standard mapping. We can't un-corrupt the
    # scan, but we can flag which pages produced it so anything sourced from
    # them gets extra scrutiny in the per-field verification step below.
    _set_progress(job_id, stage="extracting_text", chunk_index=None, total_chunks=None,
                  message=f"Reading {filename}…")
    if kind in _TABULAR_PARSERS:
        # Structured input: map columns instead of extracting text. Run in a
        # thread — parsing a large spreadsheet is CPU-bound and would
        # otherwise stall the event loop (and with it the /progress polling
        # the frontend relies on) for the duration.
        loop = asyncio.get_running_loop()
        csv_result = await loop.run_in_executor(
            None, _TABULAR_PARSERS[kind], file_bytes, filename
        )
        csv_meta = csv_result.get("meta") or {}
        text_content = csv_result.get("text") or ""
        format_label = {"xlsx": "Excel", "html": "HTML"}.get(kind, "CSV")
        if csv_result.get("ok"):
            csv_slim = csv_result.get("slim")
            n_rows = csv_meta.get("rows_parsed", 0)
            _set_progress(
                job_id,
                message=f"Mapped {n_rows} row(s) from {filename}",
                activity=(
                    f"{format_label} columns mapped directly — {n_rows} row(s) read, "
                    "no text extraction needed"
                ),
            )
        else:
            logger.info(
                f"[{filename}] {format_label} columns not recognised "
                f"({'; '.join(csv_meta.get('warnings') or []) or 'no detail'}) — "
                "falling back to language-model extraction over the raw contents."
            )
            _set_progress(
                job_id,
                activity=f"{format_label} columns not recognised — using language-model extraction",
            )
        # Mirror the PDF extraction dict's shape so every downstream consumer
        # (_document_ocr_confidence, the OCR-quality block, chunk token
        # budgeting) works unchanged. page_confidences stays empty because
        # there is no OCR here to be uncertain about — which is exactly what
        # a None document confidence means to those callers, rather than a
        # fabricated 100 that would imply a measurement was taken.
        extraction = {
            "text": text_content,
            "method": f"{kind}_structured" if csv_slim else f"{kind}_text_fallback",
            "page_confidences": [],
            "low_confidence_pages": [],
            "csv_meta": csv_meta,
        }
    else:
        extraction = await extract_text_from_pdf_with_confidence(file_bytes, filename)
        text_content = extraction["text"]
    low_confidence_pages = extraction.get("low_confidence_pages") or []
    if kind not in _TABULAR_PARSERS:
        # A CSV/XLSX/HTML input has no pages, so reporting "0 page(s)
        # processed" would be both meaningless and alarming in the
        # activity log.
        _set_progress(
            job_id, total_pages=extraction.get("page_count") or len(extraction.get("page_confidences") or []),
            activity=(
                f"{extraction.get('page_count') or len(extraction.get('page_confidences') or [])} page(s) processed via "
                f"{extraction.get('method', 'text extraction')}"
            ),
        )
    if low_confidence_pages:
        logger.warning(
            f"[{filename}] {len(low_confidence_pages)} page(s) had low OCR "
            f"confidence (pages: {low_confidence_pages}, method: "
            f"{extraction.get('method')}) — application numbers and standard "
            "codes extracted from this document should be treated as lower "
            "confidence until verified against the source text."
        )

    if not text_content.strip():
        raise HTTPException(400, f"Could not extract text from {filename}")

    # 1b. Text repair — reconstruct wrapped patent identifiers and wrapped/
    # column-bled country names BEFORE anything else (chunking, the LLM
    # prompt, and field verification) looks at this text. This is the root-
    # cause fix for ISSUES 1, 3, 6, 7: a PDF's text layer wraps mid-cell at
    # the visual line boundary, not at a logical field boundary, and every
    # downstream consumer needs the logical (unwrapped) text, not the raw
    # per-line layout. Run unconditionally for both OCR and native
    # extractions — the wrap problem is a PDF-layout artifact, not an OCR
    # artifact (see utils/text_repair.py's module docstring).
    # Skipped for CSV/XLSX/HTML input: reconstruct_all exists to undo a PDF
    # text layer's mid-cell line wrapping, and none of these formats have
    # that wrapping — cell/text-node boundaries are explicit. Running it
    # here would be a net negative, since the repair heuristics would read
    # genuine row boundaries in the rendered text as wrap points and stitch
    # adjacent rows together.
    if kind not in _TABULAR_PARSERS:
        text_content = reconstruct_all(text_content)

    # Guard: if vision OCR wasn't available and text is still too short, bail
    # early rather than wasting 4 minutes having the LLM hallucinate patents.
    if len(text_content.strip()) < 50:  # low threshold — vision OCR prose is still parseable
        logger.warning(
            f"[{filename}] Only {len(text_content)} chars extracted — too little "
            "to analyze. For scanned PDFs: ensure VISION_MODEL=moondream is set "
            "in .env and run: ollama pull moondream"
        )
        return {
            "declaration_metadata": {"declarant": filename, "confidence": 0.0},
            "patent_disclosures": [],
            "portfolio_level_analysis": {},
            "filename": filename,
            "_inference_time_s": 0.0,
            "_backend": settings.LLM_BACKEND,
            "_model": model or settings.OLLAMA_MODEL,
            "_parse_error": True,
            "_error_reason": "insufficient_text",
            "research_outputs": None,
        }

    # 2. Always use the fast prompt — slim schema = 10x less output tokens
    # Deep mode adds a brief RAG context prefix but still uses fast schema
    system = FAST_SYSTEM_PROMPT
    user_msg = FAST_USER_PROMPT
    rag_context_used: list[dict] = []
    if use_rag and mode == "deep":
        try:
            # v0.3-continuation fix (cPARAS audit item 16): use the SAME
            # shared retrieval service C-SARAS uses (services/
            # retrieval_service.py), filtered to standards sources, rather
            # than an unfiltered raw vector-store call -- and track what
            # was actually retrieved (rag_context_used) so the injected
            # context isn't an invisible input to the LLM. Label reflects
            # the real source_type mix (3GPP Retrieval Index / Standards Corpus and/or the
            # legacy 3GPP corpus), never hardcoded to "3GPP" regardless of
            # what was actually retrieved.
            from services.retrieval_service import retrieve, STANDARDS_SOURCE_TYPES
            _set_progress(job_id, activity="Retrieving relevant standards context…")
            evidence = await retrieve(text_content[:500], top_k=4, source_types=STANDARDS_SOURCE_TYPES)
            if evidence:
                sources_seen = sorted({e.source_type for e in evidence if e.source_type})
                label = "ETSI/standards" if sources_seen else "standards"
                rag_context = "\n".join(e.content[:200] for e in evidence)
                system = f"{FAST_SYSTEM_PROMPT}\n\nRelevant {label} context:\n{rag_context}"
                rag_context_used = [
                    {"content": e.content[:300], "standard": e.standard, "source_type": e.source_type, "score": round(e.combined_score, 4)}
                    for e in evidence
                ]
        except Exception as e:
            logger.warning(f"RAG retrieval failed (non-fatal): {e}")

    # 3. Analyze — single-shot for documents that fit within CHUNK_SIZE
    # (~20K chars, sized against settings.OLLAMA_NUM_CTX — see
    # _analyze_in_chunks' docstring), chunked otherwise.
    effective_model = model or settings.OLLAMA_MODEL
    t0 = time.time()
    if csv_slim is not None:
        # 3'. Structured input — no extraction model involved. The CSV's own
        # columns ARE the slim schema, so we hand them to the same
        # slim_to_full() the LLM path uses. That shared conversion is what
        # guarantees a CSV disclosure is indistinguishable downstream from a
        # PDF one: identical field names, identical continuation-row
        # merging, identical cleaning of standards lists and number fields.
        logger.info(
            f"[{filename}] CSV columns mapped directly — skipping extraction "
            f"model ({csv_meta.get('rows_parsed', 0)} row(s))"
        )
        parsed = slim_to_full(csv_slim)
        _attach_csv_family_data(csv_slim, parsed)
        elapsed = time.time() - t0
    else:
        logger.info(
            f"[{filename}] Running {mode} analysis via {settings.LLM_BACKEND} "
            f"(text: {len(text_content)} chars, model: {effective_model})"
        )
        raw_output = await _analyze_in_chunks(text_content, system, user_msg, llm, filename, job_id=job_id, model=model, extraction=extraction)
        elapsed = time.time() - t0
        logger.info(f"[{filename}] Inference complete in {elapsed:.1f}s")

        # 4. Parse and normalise to full schema
        parsed = extract_json_safe(raw_output)
        if not parsed:
            logger.warning(f"[{filename}] JSON parse failed — returning empty result")
            parsed = {
                "declaration_metadata": {"declarant": filename, "confidence": 0.0},
                "patent_disclosures": [],
                "portfolio_level_analysis": {},
                "_parse_error": True,
            }
        elif "patents" in parsed:
            # slim schema → full schema
            parsed = slim_to_full(parsed)

    if parsed.get("_possibly_incomplete"):
        logger.warning(
            f"[{filename}] {parsed.get('_incomplete_chunk_count')} chunk(s) still "
            "looked incomplete even after a budget-increase retry — see the "
            "'_possibly_incomplete' flag on this result. Do not treat this "
            "extraction as guaranteed-complete without spot-checking against "
            "the source PDF."
        )

    disclosures = parsed.get("patent_disclosures") or []
    n_patents = len(disclosures)
    logger.info(f"[{filename}] Extracted {n_patents} patents")
    _set_progress(
        job_id, total_patents=n_patents, patents_completed=0,
        activity=f"{n_patents} patent{'s' if n_patents != 1 else ''} extracted from {filename}",
    )

    # 4b. Deep enrichment — actually run standards/SEP/geographic analysis
    # per patent when Deep Mode is selected. Previously Deep Mode only ever
    # ran the fast/slim extraction prompt (see comment at step 2 above), so
    # every patent's enrichment fields stayed at slim_to_full()'s blank
    # placeholders. That's the root cause both of "every patent shows 50%
    # SEP strength" and of "every patent maps to the same standard/version" —
    # research/engine.py's build_patent_text() had nothing but the bare
    # declared standard code to embed, so patents sharing a standard
    # collapsed onto the same nearest corpus match. This adds real
    # per-patent latency (a few seconds per batch of 6), unlike the
    # extraction pass, since a 3B model can't reliably write distinct
    # analysis for dozens of patents in one call.
    if mode == "deep" and disclosures:
        await _deep_enrich_disclosures(disclosures, llm, filename, job_id=job_id, model=model)

    # Per-field verification — the real fix, not just an aggregate count
    # compare. For every disclosure, check whether the application number,
    # publication number, and each declared standard code the LLM extracted
    # actually appears (exactly, or via OCR-noise-tolerant fuzzy matching)
    # in the source text. A count-only check can match by pure coincidence
    # (40 LLM patents vs. 40 detected numbers) while individual rows are
    # still wrong, and it never looked at standards at all — this closes
    # both gaps. Results are attached per-disclosure (under inferred_fields,
    # an existing JSON column — no schema change needed) so they flow
    # through to raw_json/the frontend without touching anything else.
    _set_progress(job_id, stage="verifying", chunk_index=None, total_chunks=None,
                  message=f"Verifying extracted fields for {filename}…",
                  activity=f"Verifying extracted fields for {n_patents} patent(s)…")
    unverified_examples = []
    fields_checked = 0
    fields_unverified = 0
    flagged_incomplete_rows = 0

    # CRASH FIX (v0.7): per-field fuzzy verification against the source
    # text exists to catch an LLM *hallucinating* a number/standard code —
    # it has nothing to check on the deterministic CSV/XLSX/HTML path
    # (csv_slim is not None), where every field was mapped 1:1 from a
    # column, not generated by a model (same reasoning the aggregate
    # `_count_diagnostic` below already uses to skip itself on this path).
    # Running it anyway doesn't just waste work, it doesn't scale: this
    # endpoint has no row cap, and `build_candidate_index` grows with the
    # size of the rendered sheet while `verify_patent_fields` linearly
    # scans that whole candidate list for every field of every disclosure
    # — O(disclosures × candidates). A real bulk-style CSV (the same
    # 35,000-50,000+ row ETSI-export shape GITAS/CSARAS ingest) pushed
    # through this single-declaration endpoint turned that into tens of
    # billions of difflib comparisons, which is what actually hung/OOM'd
    # the backend well after "Extracted N patents" had already logged —
    # not a timeout in extraction itself. Row completeness (cheap: no
    # source-text scan, just presence-weighted field checks) still runs
    # either way.
    if csv_slim is None:
        candidate_index = build_candidate_index(text_content)
        # ISSUE 1: fuzzy verification is no longer gated on OCR-vs-native
        # tier. `text_content` has already been through reconstruct_all()
        # above, which is what actually fixes the "correct wrapped
        # identifier rejected" failure mode — fuzzy matching was never the
        # right tool for that bug (it's a false rejection of an EXACT
        # match once reconstructed, not a near-miss that needs tolerance).
        # Fuzzy matching remains available on both tiers as a bounded
        # safety net for whatever reconstruction didn't catch, with a
        # stricter threshold on native text (no OCR noise to excuse a
        # near-miss there) — see default_fuzzy_threshold_for().
        is_ocr = extraction.get("method") in ("tesseract", "vision_ollama", "fallback_best_effort")
        allow_fuzzy = True
        fuzzy_threshold = default_fuzzy_threshold_for(is_ocr)
        for disc in disclosures:
            ed = disc.get("explicit_data") or {}
            verification = verify_patent_fields(
                ed, text_content, candidate_index=candidate_index,
                allow_fuzzy=allow_fuzzy, fuzzy_threshold=fuzzy_threshold,
            )
            disc.setdefault("inferred_fields", {})
            disc["inferred_fields"]["extraction_verification"] = verification

            # ISSUE 9: row completeness — never silently accept a partially
            # populated row indistinguishable from a fully-populated one.
            completeness = compute_row_completeness(disc)
            disc["inferred_fields"]["completeness"] = completeness
            if completeness["flagged"]:
                flagged_incomplete_rows += 1

            for field_name in ("application_number", "publication_number"):
                v = verification.get(field_name)
                if v is None:
                    continue
                fields_checked += 1
                if not v["verified"]:
                    fields_unverified += 1
                    unverified_examples.append({
                        "disclosure_number": disc.get("disclosure_number"),
                        "field": field_name,
                        "value": ed.get(field_name),
                        "best_match": v.get("matched_snippet"),
                        "similarity": v.get("similarity"),
                    })
            for v in verification.get("standards", []):
                fields_checked += 1
                if not v["verified"]:
                    fields_unverified += 1
                    unverified_examples.append({
                        "disclosure_number": disc.get("disclosure_number"),
                        "field": "standards",
                        "value": v.get("value"),
                        "best_match": v.get("matched_snippet"),
                        "similarity": v.get("similarity"),
                    })
    else:
        for disc in disclosures:
            completeness = compute_row_completeness(disc)
            disc.setdefault("inferred_fields", {})["completeness"] = completeness
            if completeness["flagged"]:
                flagged_incomplete_rows += 1

    if flagged_incomplete_rows:
        logger.warning(
            f"[{filename}] {flagged_incomplete_rows}/{len(disclosures)} disclosure(s) "
            f"flagged as incompletely populated (completeness score below "
            f"{COMPLETENESS_FLAG_THRESHOLD}) — see inferred_fields.completeness "
            "on each affected disclosure."
        )

    if fields_unverified:
        logger.warning(
            f"[{filename}] {fields_unverified}/{fields_checked} extracted "
            "application/publication numbers or standard codes could not be "
            "matched (even fuzzily) against the source text — these are "
            f"likely fabricated. First few: {unverified_examples[:5]}"
        )
    parsed["_verification_summary"] = {
        "fields_checked": fields_checked,
        "fields_unverified": fields_unverified,
        "unverified": unverified_examples,
        **({"skipped_reason": "Deterministic column-mapped input — nothing to verify against source text."}
           if csv_slim is not None else {}),
    }

    # Aggregate count diagnostic (kept as a secondary, best-effort signal
    # only — the per-field verification above is now the authoritative
    # check). Count distinct application/publication-like numbers actually
    # present in the source text and compare to what the LLM returned. This
    # is necessarily incomplete — patent number formats vary by office and
    # this only covers the most common ones seen across ETSI declarants
    # (US, India, China, UK, EPO/PCT, Japan, Korea, Germany, France). PCT
    # numbers that wrap across a line break in the source PDF can also lose
    # trailing digits. Treat this as a signal to investigate, never
    # authoritative on its own.
    #
    # Not run on the deterministic CSV path. This heuristic exists to catch
    # an extraction model silently dropping rows, which cannot happen when
    # rows are mapped one-to-one from columns — every data row either became
    # a disclosure or was counted in rows_skipped. Running it anyway would
    # only manufacture false alarms, since the regex also matches numbers
    # sitting in family/publication columns that legitimately don't
    # correspond to a separate disclosure.
    if csv_slim is not None:
        parsed["_count_diagnostic"] = {
            "extracted_patents": n_patents,
            "detected_source_numbers": n_patents,
            "significant_gap": False,
            "message": None,
            "note": (
                "Rows were mapped directly from CSV columns, so no extraction "
                "under-count is possible. See _source.rows_skipped for rows "
                "that were deliberately ignored (blank, repeated header, or "
                "annex boilerplate)."
            ),
        }
    else:
        try:
            import re as _re
            detected_numbers = set(_re.findall(
                r'(?<!\+)\b\d{9,12}\b'                    # plain digit app numbers, not preceded by + (phone numbers)
                r'|PCT/[A-Z]{2}\d{4}/\d+(?:\s\d+)?'        # PCT applications — tolerates a
                                                            # line-wrap splitting the serial
                                                            # digits so wrapped PCT numbers
                                                            # aren't under-counted
                r'|\d{6,8}\.\d\b'                          # UK-style e.g. 2411709.5
                r'|\b63/\d{6}\b'                           # US provisional e.g. 63/680651
                r'|\b(?:US|IN|CN|EP|WO|JP|KR|DE|FR|GB)\d{6,12}\b'  # country-prefixed numbers
                r'|\bEP\s?\d{5,10}\s?[AB]\d\b'              # EP granted publication w/
                                                              # kind code, e.g. "EP3134988 B1"
                r'|\b\d{2,4}[A-Z]{2,4}\d{4}\b',            # older Indian style e.g. 980DEL2001
                text_content,
            ))
            if detected_numbers:
                expected = len(detected_numbers)
                gap = abs(expected - n_patents)
                mismatch = gap > max(2, n_patents * 0.15)
                parsed["_count_diagnostic"] = {
                    "extracted_patents": n_patents,
                    "detected_source_numbers": expected,
                    "significant_gap": mismatch,
                    "message": (
                        f"The extraction pipeline returned {n_patents} patent disclosure(s), "
                        f"but {expected} distinct application/publication-like numbers were "
                        f"found in the source PDF text. This is a best-effort heuristic "
                        f"(coverage varies by patent-office number format and can under- or "
                        f"over-count), but a gap this large usually means some disclosures "
                        f"in the document were not extracted. Please verify the full "
                        f"disclosure count against the source PDF."
                    ) if mismatch else None,
                }
                if mismatch:
                    logger.warning(
                        f"[{filename}] Possible count mismatch: LLM returned {n_patents} "
                        f"patents, but {expected} distinct application/"
                        f"publication-like numbers were detected in the source text "
                        f"(best-effort estimate — coverage varies by patent office "
                        f"format and may under-count wrapped PCT numbers). "
                        f"Consider reviewing this declaration manually."
                    )
        except Exception:
            pass  # aggregate check is best-effort only, never block on it


    parsed["filename"] = filename
    parsed["_inference_time_s"] = round(elapsed, 2)
    parsed["_backend"] = settings.LLM_BACKEND
    # Bug fix: this previously used settings.LLAMACPP_MODEL_PATH for EVERY
    # non-ollama backend, including vLLM — so a vLLM-backed analysis had its
    # reported model name (surfaced in the UI's model badge and stored in
    # the DB/raw_json) silently show the llama.cpp GGUF path instead of the
    # actual vLLM model being used.
    if settings.LLM_BACKEND == "ollama":
        parsed["_model"] = effective_model
    elif settings.LLM_BACKEND == "vllm":
        parsed["_model"] = settings.VLLM_MODEL
    else:
        parsed["_model"] = settings.LLAMACPP_MODEL_PATH
    parsed["_ocr_quality"] = {
        "method": extraction.get("method"),
        "low_confidence_pages": low_confidence_pages,
        "page_confidences": extraction.get("page_confidences"),
    }

    # Source provenance. Whether a row came from a column or from a model
    # reading OCR'd pixels is the single most important thing to know when
    # judging how far to trust it, so it travels with the result rather than
    # only appearing in the server log. `extraction_method` is deliberately
    # distinct from `_model`: on the CSV/XLSX/HTML path no extraction model
    # ran, and reporting one would misattribute deterministic output to an
    # LLM. The meta key stays "csv" even for XLSX/HTML input (`kind` on
    # `_source` already disambiguates the three) so the frontend has one
    # field to read regardless of which tabular format produced it.
    parsed["_source"] = {
        "kind": kind,
        "extraction_method": extraction.get("method"),
        "used_extraction_model": csv_slim is None,
        **({"csv": csv_meta} if csv_meta is not None else {}),
    }
    if rag_context_used:
        # v0.3-continuation (cPARAS audit item 16): the exact standards
        # context supplied to the deep-enrichment LLM call, persisted so
        # it's never an invisible input -- surfaced in the frontend's
        # "Standards context supplied to the model" panel and in raw_json.
        parsed["_rag_context"] = rag_context_used
    if csv_slim is not None:
        # `_model` is intentionally left as-is (the model configured for this
        # run, which deep enrichment does still use on these rows) rather
        # than nulled — the UI model badge and the DB column both read it,
        # and a null there renders as a missing value rather than as the
        # meaningful statement it's trying to make. `used_extraction_model`
        # above is what carries that statement.
        #
        # Extraction confidence is 1.0 because the identifiers were read out
        # of columns, not inferred. This says nothing about whether the
        # declaration's contents are correct — only that we transcribed them
        # faithfully.
        parsed.setdefault("declaration_metadata", {})["confidence"] = 1.0
        if csv_meta and csv_meta.get("warnings"):
            parsed["_source_warnings"] = csv_meta["warnings"]

    # 5. Research augmentation
    _set_progress(job_id, stage="research", chunk_index=None, total_chunks=None,
                  message=f"Running research augmentation for {filename}…",
                  activity="Running research & similarity analysis…")
    n_disclosures_for_research = len(parsed.get("patent_disclosures") or [])
    if n_disclosures_for_research > settings.CPARAS_MAX_DISCLOSURES_FOR_RESEARCH:
        # CRASH FIX (v0.7): analyze_patents() embeds every disclosure plus
        # the whole standards corpus in one call and builds a single
        # (n_disclosures x n_corpus) similarity matrix in memory -- fine
        # for one declarant's filing, not for a bulk multi-company export.
        # See settings.CPARAS_MAX_DISCLOSURES_FOR_RESEARCH's docstring.
        logger.warning(
            f"[{filename}] {n_disclosures_for_research} disclosures exceeds "
            f"CPARAS ISLD's single-declaration research-augmentation limit "
            f"({settings.CPARAS_MAX_DISCLOSURES_FOR_RESEARCH}) — skipping "
            "essentiality/similarity research for this run to avoid an "
            "out-of-memory crash. This file looks like a bulk multi-company "
            "export; use CPARAS Bulk (POST /api/sep/upload) instead, which "
            "is built for this scale."
        )
        parsed["research_outputs"] = None
        parsed["_research_skipped_reason"] = (
            f"This declaration has {n_disclosures_for_research:,} patent(s), above CPARAS ISLD's "
            f"{settings.CPARAS_MAX_DISCLOSURES_FOR_RESEARCH:,}-row research-augmentation limit "
            "for a single declaration. This looks like a bulk multi-company dataset — upload it "
            "to CPARAS Bulk instead for essentiality scoring at this scale."
        )
    else:
        try:
            prior_patents = await _fetch_prior_patents(db)
            parsed["research_outputs"] = await analyze_patents(
                parsed.get("patent_disclosures", []) or [],
                prior_patents=prior_patents,
                ocr_confidence=_document_ocr_confidence(extraction),
            )
        except Exception as e:
            logger.warning(f"[{filename}] Research augmentation failed (non-fatal): {e}")
            parsed["research_outputs"] = None

    # 6. Persist
    _set_progress(job_id, stage="persisting", chunk_index=None, total_chunks=None,
                  message=f"Saving {filename}…", activity="Saving results to database…")
    persist_error = await _persist_declaration(parsed, filename, mode, db)
    if persist_error:
        # The analysis itself succeeded and is returned, but tell the user
        # it is NOT in the history (so no Excel export / re-open later).
        parsed["_persist_error"] = persist_error
    return parsed


def _str_field(val) -> str:
    """Ensure a field that should be a string isn't accidentally a list."""
    if val is None:
        return ""
    if isinstance(val, list):
        return ", ".join(str(v) for v in val if v) if val else ""
    return str(val)


def _float_field(val):
    """
    Ensure a field bound to a Float DB column is actually float-able before
    it reaches SQLAlchemy's INSERT. The LLM occasionally emits a confidence
    field as a list (e.g. per-point confidences instead of one blended
    score) or as a string like "90%" instead of a plain number. Passed
    through unguarded, SQLAlchemy raises
    "float() argument must be a string or a real number, not 'list'" deep
    inside the INSERT's bind-parameter processing — which aborts the whole
    declaration's persistence (all patents in that batch silently fail to
    save, even though extraction itself succeeded), not just the one bad
    field. Mirrors _str_field's list-guard above, but for the five Float
    columns (Declaration.confidence, Patent.sep_confidence,
    Patent.research_confidence, Patent.standard_similarity,
    Patent.similar_patent_score) that are populated straight from LLM JSON.
    """
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, (list, tuple)):
        # Take the first numeric element rather than discarding the signal
        # entirely - a list of per-point confidences still carries a
        # usable headline number.
        for item in val:
            coerced = _float_field(item)
            if coerced is not None:
                return coerced
        return None
    if isinstance(val, str):
        match = re.search(r"-?\d+(?:\.\d+)?", val)
        if match:
            try:
                return float(match.group())
            except ValueError:
                return None
        return None
    return None


async def _fetch_prior_patents(db: AsyncSession, exclude_declaration_id: Optional[int] = None,
                                limit: int = 300) -> list[dict]:
    """
    Build `{"label", "text"}` entries for patents already in the database, so
    `analyze_patents()` can find "similar patent" matches across declarations,
    not just within the current batch (Requirement 2 / Paper 1's nearest-
    neighbour recommendation step).
    """
    stmt = select(Patent).order_by(Patent.created_at.desc()).limit(limit)
    if exclude_declaration_id is not None:
        stmt = stmt.where(Patent.declaration_id != exclude_declaration_id)
    result = await db.execute(stmt)

    prior: list[dict] = []
    for p in result.scalars().all():
        label = p.publication_number or p.application_number
        if not label:
            continue
        try:
            standards = json.loads(p.standards) if p.standards else []
        except Exception:
            standards = []
        parts = [p.title or ""]
        if standards:
            parts.append("Declared standards: " + ", ".join(standards))
        if p.primary_domain:
            parts.append(f"Technical domain: {p.primary_domain}")
        text_blob = ". ".join(x for x in parts if x).strip()
        prior.append({"label": label, "text": text_blob or label})
    return prior


async def _persist_declaration(data: dict, filename: str, mode: str, db: AsyncSession):
    """Save analysis results to SQLite."""
    try:
        meta = data.get("declaration_metadata", {})
        decl = Declaration(
            filename=filename,
            declarant=meta.get("declarant", ""),
            filing_type=meta.get("filing_type", ""),
            project=meta.get("project", ""),
            likely_standard_generation=meta.get("likely_standard_generation", ""),
            portfolio_characterization=meta.get("portfolio_characterization", ""),
            overall_technology_domains=json.dumps(meta.get("overall_technology_domains", []), ensure_ascii=False),
            confidence=_float_field(meta.get("confidence")),
            analysis_mode=mode,
            raw_json=json.dumps(data, ensure_ascii=False),
        )
        db.add(decl)
        await db.flush()  # get decl.id

        research = data.get("research_outputs") or {}
        research_records = research.get("records", [])

        for i, disc in enumerate(data.get("patent_disclosures", [])):
            ed = disc.get("explicit_data", {})
            sa = disc.get("sep_analysis", {})
            domains = disc.get("standards_analysis", {}).get("technical_domains", [])
            primary_domain = domains[0]["domain"] if domains else None

            rr = research_records[i] if i < len(research_records) else {}
            verification = rr.get("verification") or {}
            potential = rr.get("potential_sep")

            patent = Patent(
                declaration_id=decl.id,
                disclosure_number=disc.get("disclosure_number"),
                project=ed.get("project"),
                standards=json.dumps(ed.get("standards", []), ensure_ascii=False),
                applicant=_str_field(ed.get("applicant")),
                application_number=_str_field(ed.get("application_number")),
                publication_number=_str_field(ed.get("publication_number")),
                country=_str_field(ed.get("country")),
                title=_str_field(ed.get("title")),
                standards_analysis=json.dumps(disc.get("standards_analysis", {}), ensure_ascii=False),
                sep_analysis=json.dumps(sa, ensure_ascii=False),
                geographic_analysis=json.dumps(disc.get("geographic_analysis", {}), ensure_ascii=False),
                inferred_fields=json.dumps(disc.get("inferred_fields", {}), ensure_ascii=False),
                sep_strength=_str_field(sa.get("likely_sep_strength")),
                sep_confidence=_float_field(sa.get("confidence")),
                primary_domain=primary_domain,
                jurisdiction=ed.get("country"),
                # Research / paper-style outputs (see research/engine.py)
                predicted_sep_status=rr.get("predicted_sep_status"),
                research_confidence=_float_field(rr.get("confidence_score")),
                recommended_standard=rr.get("recommended_standard"),
                recommended_version=rr.get("recommended_version"),
                standard_clause=rr.get("standard_clause"),
                standard_similarity=_float_field(rr.get("similarity_score")),
                # v0.5: mandatory three-signal fields (see research/
                # three_signal_scoring.py). standard_similarity above
                # already IS semantic_similarity (same calibrated value) --
                # not duplicated as a second column.
                claim_match=_float_field(rr.get("claim_match")),
                standard_coverage=_float_field(rr.get("standard_coverage")),
                similar_patent_ref=rr.get("similar_patent"),
                similar_patent_score=_float_field(rr.get("similar_patent_score")),
                match_explanation=json.dumps(rr.get("explanation", []), ensure_ascii=False),
                matched_phrases=json.dumps(rr.get("matched_phrases") or [], ensure_ascii=False),
                matched_terms=json.dumps(rr.get("matched_terms") or [], ensure_ascii=False),
                verification_status=verification.get("status"),
                version_evolution=json.dumps(rr.get("version_evolution", {}), ensure_ascii=False),
                is_potential_sep=bool(rr.get("is_potential_sep", False)),
                potential_sep_detail=json.dumps(potential, ensure_ascii=False) if potential else None,
                # v0.3-continuation (cPARAS audit items 8-14): essentiality
                # evidence provenance -- which source (real ETSI KB vs.
                # illustrative corpus) and exact evidence text produced
                # this result, plus the KB version, so a re-opened
                # historical analysis stays interpretable even after the
                # retrieval index itself later changes.
                evidence_source=rr.get("evidence_source"),
                evidence_text=rr.get("evidence_text"),
                knowledge_base_version=rr.get("knowledge_base_version"),
                evidence_retrieved_at=_utcnow() if rr.get("evidence_source") else None,
            )
            db.add(patent)

        port = data.get("portfolio_level_analysis", {})
        if port:
            portfolio = PortfolioAnalysis(
                declaration_id=decl.id,
                dominant_technology_domains=json.dumps(port.get("dominant_technology_domains", []), ensure_ascii=False),
                release_focus=json.dumps(port.get("release_focus", []), ensure_ascii=False),
                most_targeted_specs=json.dumps(port.get("most_targeted_specs", []), ensure_ascii=False),
                geographic_filing_strategy=port.get("geographic_filing_strategy"),
                portfolio_strength_assessment=port.get("portfolio_strength_assessment"),
                frand_implications=port.get("frand_implications"),
                implementation_risk_summary=port.get("implementation_risk_summary"),
            )
            db.add(portfolio)

        if research:
            _save_research_run(db, decl.id, research)

        await db.commit()
        logger.info(f"[{filename}] Persisted to database (declaration id={decl.id})")
        data["_declaration_id"] = decl.id
        return None
    except Exception as e:
        logger.error(f"[{filename}] DB persist failed (nothing was saved for this file): {type(e).__name__}: {e}")
        await db.rollback()
        return (f"The analysis could not be saved to the local database ({type(e).__name__}: {e}); "
                "it is shown here but will not appear in History and cannot be exported to Excel later.")


def _save_research_run(db: AsyncSession, declaration_id: int, research: dict) -> None:
    """Store the metrics/aggregates summary for a research run (figures stay in raw_json)."""
    meta = research.get("meta", {})
    db.add(ResearchRun(
        declaration_id=declaration_id,
        embedder_mode=meta.get("embedder_mode"),
        similarity_threshold=meta.get("similarity_threshold"),
        corpus_size=meta.get("corpus_size"),
        metrics_json=json.dumps(research.get("metrics", {}), ensure_ascii=False),
        aggregates_json=json.dumps(research.get("aggregates", {}), ensure_ascii=False),
    ))


async def _apply_research_to_existing_patents(db: AsyncSession, declaration_id: int, research: dict) -> None:
    """Write per-patent research fields onto already-persisted Patent rows, matched by insertion order."""
    records = (research or {}).get("records", [])
    if not records:
        return
    result = await db.execute(
        select(Patent).where(Patent.declaration_id == declaration_id).order_by(Patent.id)
    )
    for patent, rr in zip(result.scalars().all(), records):
        verification = rr.get("verification") or {}
        potential = rr.get("potential_sep")
        patent.predicted_sep_status = rr.get("predicted_sep_status")
        patent.research_confidence = _float_field(rr.get("confidence_score"))
        patent.recommended_standard = rr.get("recommended_standard")
        patent.recommended_version = rr.get("recommended_version")
        patent.standard_clause = rr.get("standard_clause")
        patent.standard_similarity = _float_field(rr.get("similarity_score"))
        patent.claim_match = _float_field(rr.get("claim_match"))
        patent.standard_coverage = _float_field(rr.get("standard_coverage"))
        patent.similar_patent_ref = rr.get("similar_patent")
        patent.similar_patent_score = _float_field(rr.get("similar_patent_score"))
        patent.match_explanation = json.dumps(rr.get("explanation", []), ensure_ascii=False)
        patent.matched_phrases = json.dumps(rr.get("matched_phrases") or [], ensure_ascii=False)
        patent.matched_terms = json.dumps(rr.get("matched_terms") or [], ensure_ascii=False)
        patent.verification_status = verification.get("status")
        patent.version_evolution = json.dumps(rr.get("version_evolution", {}), ensure_ascii=False)
        patent.is_potential_sep = bool(rr.get("is_potential_sep", False))
        patent.potential_sep_detail = json.dumps(potential, ensure_ascii=False) if potential else None
        patent.evidence_source = rr.get("evidence_source")
        patent.evidence_text = rr.get("evidence_text")
        patent.knowledge_base_version = rr.get("knowledge_base_version")
        patent.evidence_retrieved_at = _utcnow() if rr.get("evidence_source") else None


# ── Routes ─────────────────────────────────────────────────────────────────────

@router.post("/analyze")
async def analyze_declarations(
    files: list[UploadFile] = File(...),
    mode: str = Form("deep"),
    use_rag: bool = Form(True),
    job_id: Optional[str] = Form(None),
    model: Optional[str] = Form(None),
    db: AsyncSession = Depends(get_db),
):
    """
    Analyze one or more ETSI IPR declaration PDFs.
    Returns combined JSON compatible with the original frontend schema.

    job_id (optional): a client-generated UUID. When provided, live progress
    (current file, pipeline stage, chunk N/M) is written to an in-memory
    store the frontend can poll via GET /progress/{job_id} while this
    request — which can legitimately run long on large multi-chunk
    declarations — is still in flight. It also enables start/stop/pause/
    continue: while this request is running, a concurrent POST
    /jobs/{job_id}/pause or /stop cooperatively stops it between files
    (see this module's "start / stop / pause / continue" section above) —
    the response comes back early with "stopped": true and a
    "skipped_files" list rather than an error.

    model (optional): the Ollama model name the frontend's model-selection
    dropdown picked (e.g. "qwen2.5:7b"). When omitted, falls back to
    settings.OLLAMA_MODEL as before — every existing caller that doesn't
    send this field keeps working unchanged. When provided and the backend
    is Ollama, it's validated against the models Ollama currently reports
    as installed so a stale/deleted selection fails fast with a clear
    message instead of surfacing as an opaque Ollama error mid-extraction.
    """
    if not files:
        raise HTTPException(400, "No files uploaded")

    if len(files) > MAX_UPLOAD_FILES:
        raise HTTPException(
            413, f"Too many files: {len(files)} (limit {MAX_UPLOAD_FILES})."
        )

    if mode not in ("deep", "quick"):
        raise HTTPException(400, "mode must be 'deep' or 'quick'")

    job_id = _validate_job_id(job_id)

    # Validate the model that will ACTUALLY run — the user's selection, or
    # the configured default when none was sent — before reading any file.
    # A missing model fails fast with an actionable message instead of
    # surfacing as an opaque Ollama 404 halfway through extraction, and a
    # different model is never substituted silently.
    if settings.LLM_BACKEND == "ollama":
        probe = await get_llm().health_check()
        resolved_model, model_error = validate_model_choice(model or settings.OLLAMA_MODEL, probe, purpose="text")
        if model_error:
            raise HTTPException(503 if not probe.get("reachable") else 400, model_error)
        model = resolved_model

    results = []
    errors = []
    skipped_files: list[str] = []
    stopped = False

    effective_model_for_job = (model or settings.OLLAMA_MODEL) if settings.LLM_BACKEND == "ollama" else None

    _set_progress(
        job_id, total_files=len(files), current_file_index=0,
        current_filename=files[0].filename if files else None,
        stage="starting", chunk_index=None, total_chunks=None, done=False,
        model=effective_model_for_job, backend=settings.LLM_BACKEND,
        message=f"Starting analysis of {len(files)} file(s)…",
        activity=f"Using model {effective_model_for_job}" if effective_model_for_job else "Starting analysis…",
    )

    # start/stop/pause/continue: register this job_id so POST
    # .../jobs/{job_id}/pause and /stop (same cooperative mechanism, see
    # this module's "start / stop / pause / continue" section above) have
    # something to signal while this request is in flight.
    if job_id:
        _register_job(job_id)

    try:
        for fi, upload in enumerate(files):
            if is_analysis_stop_requested(job_id):
                # Cooperative stop, checked at the coarsest (always-safe)
                # granularity: between files. Every file already fully
                # analyzed above stays in `results` (and, per
                # _persist_declaration, already committed to the
                # database) -- only files not yet started are left for a
                # later "Continue" (re-submit `skipped_files` under the
                # same job_id).
                skipped_files = [f.filename for f in files[fi:]]
                stopped = True
                logger.info(f"Analysis job {job_id}: stopped before file {fi + 1}/{len(files)} ({upload.filename}).")
                break

            # Reset every per-file counter here. _set_progress only merges fields
            # in, so without this the previous file's total_pages/total_patents/
            # patents_completed/current_patent survived into the next file — which
            # made the next file's progress fraction start out already "complete"
            # (e.g. patents_completed=30 of total_patents=30 carried over) and the
            # stage sub-labels show stale counts.
            _set_progress(
                job_id, total_files=len(files), current_file_index=fi,
                current_filename=upload.filename, stage="starting",
                chunk_index=None, total_chunks=None,
                total_pages=None, total_patents=None, patents_completed=0,
                current_patent=None, failed_stage=None,
                message=f"File {fi + 1}/{len(files)}: {upload.filename}",
                activity=f"Uploaded {upload.filename}",
            )
            try:
                file_bytes, kind = await _read_upload_limited(upload, MAX_UPLOAD_BYTES)
                result = await _run_analysis(
                    file_bytes, upload.filename or f"unknown.{kind}", mode, use_rag, db,
                    job_id=job_id, model=model, kind=kind,
                )
                results.append(result)
            except Exception as e:
                import traceback
                logger.error(f"Failed to analyze {upload.filename}: {e}\n{traceback.format_exc()}")
                errors.append({"filename": upload.filename, "error": str(e)})
                # Preserve which stage was actually in-flight when it broke —
                # overwriting `stage` with "error" would otherwise erase which
                # step to highlight as failed, even though every other field on
                # the entry (chunk_index, total_patents, activity log, etc.)
                # survives untouched since _set_progress only merges fields in.
                failed_stage = (_progress_store.get(job_id) or {}).get("stage")
                _set_progress(
                    job_id, stage="error", failed_stage=failed_stage,
                    message=f"Failed on {upload.filename}: {e}",
                    activity=f"Failed: {e}",
                )
    finally:
        if job_id:
            _unregister_job(job_id)

    if stopped:
        _set_progress(
            job_id, stage="stopped", done=True, chunk_index=None, total_chunks=None,
            message=f"Stopped — {len(results)}/{len(files)} file(s) completed, "
                    f"{len(skipped_files)} not yet started.",
            activity=f"Analysis stopped — {len(skipped_files)} file(s) left for Continue",
        )
    else:
        _set_progress(
            job_id, stage="done", done=True, chunk_index=None, total_chunks=None,
            message=f"Done — {len(results)}/{len(files)} succeeded.",
            activity=f"Analysis complete — {len(results)}/{len(files)} file(s) succeeded",
        )

    return {
        "declarations": results,
        "errors": errors,
        "total_files": len(files),
        "succeeded": len(results),
        "backend": settings.LLM_BACKEND,
        "stopped": stopped,
        "skipped_files": skipped_files,
    }


@router.get("/progress/{job_id}")
async def get_progress(job_id: str):
    """
    Poll live progress for an in-flight /analyze call. Returns a small
    status dict — current file (N/total), pipeline stage, and chunk/batch
    (N/total) when the current stage is chunked (analysis or deep
    enrichment). Frontend polls this every ~1.5s while waiting on the
    /analyze response, which can take a long time on large multi-chunk
    declarations.
    """
    entry = _progress_store.get(job_id)
    if not entry:
        return {"job_id": job_id, "stage": "unknown", "message": "No progress found for this job yet."}
    return entry


@router.post("/jobs/{job_id}/pause")
async def pause_analysis(job_id: str):
    """Cooperative pause: an in-flight POST /analyze call for this job_id
    finishes whatever file/chunk/batch is currently in progress, then
    stops before starting the next one (see this module's "start / stop /
    pause / continue" section above). Resume with .../continue, or simply
    re-submit the response's `skipped_files` under the same job_id."""
    accepted = request_analysis_stop(job_id)
    if not accepted:
        raise HTTPException(409, "No analysis is currently running for this job_id in this backend process.")
    return {"job_id": job_id, "status": "pausing"}


@router.post("/jobs/{job_id}/stop")
async def stop_analysis(job_id: str):
    """Same cooperative-stop mechanism as /pause -- a separate, explicitly
    -named action for a "Stop" button. Files already fully analyzed
    remain persisted; nothing is discarded."""
    accepted = request_analysis_stop(job_id)
    if not accepted:
        raise HTTPException(409, "No analysis is currently running for this job_id in this backend process.")
    return {"job_id": job_id, "status": "stopping"}


@router.post("/jobs/{job_id}/continue")
async def continue_analysis_info(job_id: str):
    """CPARAS doesn't checkpoint mid-file/mid-chunk state to disk the way
    GITAS/CSARAS's background jobs do, so there's no server-side resume
    to trigger here -- "continue" is re-submitting POST /analyze with the
    same job_id and only the files listed in the stopped response's
    `skipped_files` (already-analyzed files stay in the platform's
    history untouched either way). This endpoint exists so the frontend
    has one consistent "Continue" action across GITAS/CSARAS/CPARAS: it
    simply clears this job_id's stop flag (if a new /analyze call is
    about to reuse it) and confirms the job isn't still running."""
    if is_analysis_job_running(job_id):
        raise HTTPException(409, "This analysis job is still running -- nothing to continue yet.")
    _control_events.pop(job_id, None)
    return {
        "job_id": job_id, "status": "ready",
        "message": "Re-submit POST /api/analysis/analyze with this job_id and the remaining files "
                    "(see the stopped response's skipped_files) to continue.",
    }


@router.post("/analyze/stream")
async def analyze_stream(
    file: UploadFile = File(...),
    mode: str = Form("deep"),
    use_rag: bool = Form(True),
):
    """
    Stream a single PDF analysis as SSE (Server-Sent Events) — sends tokens
    live as the model generates them, rather than waiting for the full
    /analyze response. NOTE: not currently called from the main UI flow
    (App.tsx uses /analyze + /progress/{job_id} polling instead); kept as a
    documented, working alternative for callers that specifically want raw
    token streaming. Unlike /analyze, this endpoint does NOT persist to the
    database or run research augmentation — it returns the parsed extraction
    result only.

    Uses the same FAST_SYSTEM_PROMPT (slim schema) + slim_to_full() +
    salvage-on-broken-JSON pipeline as the main /analyze endpoint. This
    previously used DEEP_SYSTEM_PROMPT's much larger full schema directly
    (10x+ the output tokens per patent) with a hard 24,000-char input
    truncation and no salvage fallback on parse failure — exactly the
    combination of failure modes _run_analysis's docstrings describe
    fixing, reintroduced here because this endpoint was never updated
    alongside it. `mode` no longer changes which prompt is used (both
    modes always use the fast/slim schema, matching _run_analysis); it is
    kept as a parameter for API compatibility.
    """
    file_bytes, kind = await _read_upload_limited(file, MAX_UPLOAD_BYTES)
    filename = file.filename or f"unknown.{kind}"

    llm = get_llm()
    if settings.LLM_BACKEND == "ollama":
        probe = await llm.health_check()
        _resolved, model_error = validate_model_choice(settings.OLLAMA_MODEL, probe, purpose="text")
        if model_error:
            raise HTTPException(503 if not probe.get("reachable") else 400, model_error)
    if kind in _TABULAR_PARSERS:
        # This endpoint streams raw model tokens, so it has no way to emit a
        # deterministically-mapped result — there is nothing to stream. A
        # CSV, Excel or HTML file is therefore rendered to text and
        # analysed like any other document here. Callers wanting the
        # deterministic column mapping (and the provenance that comes with
        # it) should use /analyze.
        csv_result = await asyncio.get_running_loop().run_in_executor(
            None, _TABULAR_PARSERS[kind], file_bytes, filename
        )
        text_content = csv_result.get("text") or ""
    else:
        text_content = await extract_text_from_pdf(file_bytes, filename)
    if not text_content.strip():
        raise HTTPException(400, f"Could not extract text from {filename}")

    system = FAST_SYSTEM_PROMPT
    user_msg = FAST_USER_PROMPT
    # This endpoint streams a single LLM call and has no chunking logic
    # (unlike /analyze's _analyze_in_chunks) — still cap input length so a
    # very large declaration doesn't blow the context window, but warn
    # loudly since anything past this point is silently invisible to the
    # model, not just to this log line.
    MAX_CHARS = 20000
    if len(text_content) > MAX_CHARS:
        logger.warning(
            f"[{filename}] /analyze/stream: {len(text_content)} chars exceeds "
            f"this endpoint's {MAX_CHARS}-char single-shot limit (it does not "
            "chunk). Truncating — patents described only in the truncated "
            "portion will be missing. Use /analyze instead for large documents."
        )
    full_msg = f"{user_msg}\n\nDocument content:\n{text_content[:MAX_CHARS]}"

    async def event_generator():
        """Server-sent events for /analyze/stream: 'start', one 'token' per streamed LLM token, then
        'complete' with the parsed result or 'error'.
        """
        yield f"data: {json.dumps({'event': 'start', 'filename': filename}, ensure_ascii=False)}\n\n"
        buffer = ""
        try:
            async for token in llm.stream(full_msg, system=system, temperature=0.0):
                buffer += token
                yield f"data: {json.dumps({'event': 'token', 'text': token}, ensure_ascii=False)}\n\n"
        except Exception as e:
            logger.error(f"[{filename}] /analyze/stream: LLM stream failed: {e}")
            yield f"data: {json.dumps({'event': 'error', 'message': f'LLM streaming failed: {e}'}, ensure_ascii=False)}\n\n"
            return

        parsed = extract_json_safe(buffer)
        if not parsed:
            # Same recovery path as the main pipeline: a response that broke
            # partway through generation still has individually well-formed
            # patent entries worth keeping, rather than losing everything.
            salvaged = salvage_partial_array(buffer)
            if salvaged:
                logger.warning(f"[{filename}] /analyze/stream: full JSON parse failed, salvaged partial results")
                parsed = salvaged
            else:
                yield f"data: {json.dumps({'event': 'error', 'message': 'JSON parse failed and no partial results could be salvaged'}, ensure_ascii=False)}\n\n"
                return

        if "patents" in parsed:
            parsed = slim_to_full(parsed)
        parsed["filename"] = filename
        parsed["_backend"] = settings.LLM_BACKEND
        yield f"data: {json.dumps({'event': 'complete', 'result': parsed}, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/history")
async def get_analysis_history(
    limit: int = 50,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
):
    """Return past analyses from the local database."""
    result = await db.execute(
        select(Declaration).order_by(Declaration.created_at.desc()).limit(limit).offset(offset)
    )
    declarations = result.scalars().all()
    return {
        "declarations": [
            {
                "id": d.id,
                "filename": d.filename,
                "declarant": d.declarant,
                "filing_type": d.filing_type,
                "likely_standard_generation": d.likely_standard_generation,
                "confidence": d.confidence,
                "analysis_mode": d.analysis_mode,
                "created_at": d.created_at.isoformat(),
            }
            for d in declarations
        ]
    }


@router.get("/history/{declaration_id}")
async def get_declaration_detail(
    declaration_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Return full JSON for a stored declaration."""
    result = await db.execute(select(Declaration).where(Declaration.id == declaration_id))
    decl = result.scalar_one_or_none()
    if not decl:
        raise HTTPException(404, "Declaration not found")
    if decl.raw_json:
        return json.loads(decl.raw_json)
    return {"id": decl.id, "filename": decl.filename, "error": "no raw JSON stored"}


@router.get("/history/{declaration_id}/export/excel")
async def export_declaration_excel(
    declaration_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Download the CPARAS-enriched Excel workbook for one stored
    declaration: one row per persisted patent disclosure, with the
    mandatory CPARAS_Claim_Match / CPARAS_Semantic_Similarity /
    CPARAS_Standard_Coverage columns plus supporting context — all read
    from already-persisted Patent rows (see
    api/routes/cparas/excel_export.py; no re-scoring happens here)."""
    result = await db.execute(select(Declaration).where(Declaration.id == declaration_id))
    decl = result.scalar_one_or_none()
    if not decl:
        raise HTTPException(404, "Declaration not found")

    result = await db.execute(select(Patent).where(Patent.declaration_id == declaration_id).order_by(Patent.id))
    patents = result.scalars().all()
    if not patents:
        raise HTTPException(422, "No patent disclosures were stored for this declaration, so there is nothing to export.")

    from api.routes.cparas.excel_export import build_cparas_workbook, export_filename

    try:
        xlsx_bytes = build_cparas_workbook(decl, patents)
    except Exception as e:
        logger.exception(f"CPARAS Excel export failed for declaration {declaration_id}")
        raise HTTPException(500, f"Excel export failed: {e}")

    filename = export_filename(decl)
    return StreamingResponse(
        BytesIO(xlsx_bytes),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": content_disposition(filename, "declaration_CPARAS_ISLD_enriched.xlsx")},
    )


@router.post("/history/{declaration_id}/research")
async def recompute_research_outputs(
    declaration_id: int,
    db: AsyncSession = Depends(get_db),
):
    """
    (Re)compute the paper-style research outputs (Requirements 1-13) for an
    already-analyzed declaration without re-running the LLM. Useful for
    declarations analyzed before this feature existed, or after the standards
    reference corpus (research/standards_corpus.py) has been extended.
    """
    result = await db.execute(select(Declaration).where(Declaration.id == declaration_id))
    decl = result.scalar_one_or_none()
    if not decl:
        raise HTTPException(404, "Declaration not found")
    if not decl.raw_json:
        raise HTTPException(400, "No stored analysis JSON for this declaration")

    data = json.loads(decl.raw_json)
    disclosures = data.get("patent_disclosures", []) or []

    prior_patents = await _fetch_prior_patents(db, exclude_declaration_id=declaration_id)
    # Re-derive OCR confidence from the previously-stored extraction quality
    # info (no PDF/extraction available here — this endpoint recomputes
    # research outputs from already-analyzed data) so the confidence blend
    # (Additional Issue) is consistent with a fresh /analyze run.
    stored_ocr_quality = data.get("_ocr_quality") or {}
    ocr_confidence = _document_ocr_confidence(
        {"page_confidences": stored_ocr_quality.get("page_confidences")}
    )
    research_outputs = await analyze_patents(disclosures, prior_patents=prior_patents, ocr_confidence=ocr_confidence)
    data["research_outputs"] = research_outputs

    try:
        decl.raw_json = json.dumps(data, ensure_ascii=False)
        await _apply_research_to_existing_patents(db, declaration_id, research_outputs)

        existing = await db.execute(select(ResearchRun).where(ResearchRun.declaration_id == declaration_id))
        for row in existing.scalars().all():
            await db.delete(row)
        _save_research_run(db, declaration_id, research_outputs)

        await db.commit()
    except Exception as e:
        logger.error(f"[declaration {declaration_id}] research recompute persist failed: {e}")
        await db.rollback()
        raise HTTPException(500, "Failed to persist recomputed research outputs")

    return data


@router.get("/research/corpus-info")
async def research_corpus_info():
    """Describe the offline standards reference corpus used for Requirements 1-13."""
    from research.standards_corpus import STANDARDS_CORPUS, CORPUS_DISCLAIMER, all_families, all_domains
    families = []
    for fam in all_families():
        entries = [e for e in STANDARDS_CORPUS if e.family == fam]
        families.append({
            "family": fam,
            "standard": entries[0].standard,
            "domain": entries[0].domain,
            "clauses": sorted({e.clause_label for e in entries}),
            "versions": sorted({e.version for e in entries}),
        })
    return {
        "corpus_size": len(STANDARDS_CORPUS),
        "families": families,
        "domains": all_domains(),
        "disclaimer": CORPUS_DISCLAIMER,
    }
