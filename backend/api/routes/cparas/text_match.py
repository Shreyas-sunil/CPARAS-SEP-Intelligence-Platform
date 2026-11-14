"""
Text Matching API
==================
Real backend for the cPARAS frontend's "Text Matching" tab (previously a
"Coming Soon" placeholder -- see frontend/src/cparas/App.tsx and the
TEXT_MATCHING_STEPS roadmap defined there).

Given arbitrary input text (e.g. a patent claim or disclosure paragraph),
this segments it into clause-sized pieces, embeds each segment with the
same model used everywhere else in the platform (rag.vector_store's
LocalEmbedder), and retrieves the most similar chunks from the indexed
3GPP corpus -- see ingestion/spec_corpus.py for how that corpus gets built
from a 3gpp_archive_crawler.py mirror.

This does NOT replace research/engine.py's patent-level SEP scoring
pipeline -- that operates on whole `patent_disclosures` objects produced
by the LLM extraction step and blends similarity with declared-standard /
coverage heuristics. This endpoint is the standalone, no-LLM-required
"compare this text against the standards corpus" tool the roadmap in
App.tsx described (steps: ingest -> normalize/segment -> candidate
retrieval -> comparison -> ranking -> review).

Upload formats (v0.3)
----------------------
/match-file used to accept PDF only. It now also accepts CSV, XLSX, XLS,
DOCX and TXT -- the same ISLD / declaration / claim-disclosure formats
GITAS's bulk upload and cPARAS's /api/analysis flow already parse. Rather
than a second parsing engine, CSV/XLSX/XLS reuse the exact same
fuzzy-header row parser as those flows (utils.csv_parser /
utils.xlsx_parser) -- see extract_matchable_text() below -- so a real
ISLD annex (e.g. one row per disclosure, with a "Specific Part" /
"Illustrative Part" clause-reference column and a title column) is read
the same way here as everywhere else in the platform, not re-guessed.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field

from rag.vector_store import get_vector_store
from utils.pdf_parser import extract_text_from_pdf_with_confidence
from utils.text_chunker import semantic_chunk
from utils.csv_parser import looks_like_csv, parse_csv_declaration
from utils.xlsx_parser import looks_like_xlsx, parse_xlsx_declaration

logger = logging.getLogger(__name__)

SUPPORTED_FORMATS = ["pdf", "csv", "xlsx", "xls", "docx", "txt"]


def _record_text(rec: dict) -> str:
    """One patent-record's matchable text: title + declared standard(s) +
    the illustrative/specific-part clause reference. Never invents text --
    an all-blank record (e.g. a continuation row that only carries extra
    standards for the row above) contributes nothing."""
    parts = []
    if rec.get("title"):
        parts.append(rec["title"])
    std = rec.get("std")
    if std:
        parts.append(f"Standard: {', '.join(std) if isinstance(std, list) else std}")
    if rec.get("clause"):
        parts.append(f"Clause: {rec['clause']}")
    return ". ".join(parts)


def _merge_continuation_records(records: list[dict]) -> list[dict]:
    """Reuse Declaration Analysis's continuation-row merge so both CPARAS
    ISLD tools see the same disclosures for the same file."""
    try:
        from core.prompts import _merge_continuation_patents
        return _merge_continuation_patents(records)
    except Exception:  # pragma: no cover - defensive; never block matching
        logger.warning("Continuation-row merge unavailable; matching rows as-is.", exc_info=True)
        return list(records)


def extract_matchable_text(filename: Optional[str], data: bytes) -> dict:
    """Format-detect + extract claim/disclosure text from a non-PDF ISLD
    upload (PDF is handled separately in match_file() since its extractor
    is async/OCR-aware). Pure and synchronous so it's unit-testable without
    spinning up the FastAPI app or a vector store -- see
    tests/test_text_match_extraction.py.

    Returns:
      {
        "ok": bool,
        "format": str,               -- one of SUPPORTED_FORMATS, or "unsupported"
        "text": str,                 -- combined matchable text (possibly empty)
        "extraction_method": str,
        "records_extracted": int,    -- individual patent/claim rows identified
                                         (0 for docx/txt, which are single documents)
        "text_source": str,          -- human-readable description of where the
                                         text came from, for the extraction preview
        "warnings": list[str],
        "error": Optional[str],      -- set (with ok=False) when nothing usable
                                         was found; never a raw stack trace
      }
    """
    name = (filename or "").lower()
    head = data[:4096]
    warnings: list[str] = []

    xlsx_kind = looks_like_xlsx(filename, head)
    if xlsx_kind:
        parsed = parse_xlsx_declaration(data, filename or f"upload.{xlsx_kind}")
        return _tabular_result(parsed, xlsx_kind, warnings)

    if looks_like_csv(filename, head):
        # utils.csv_parser.looks_like_csv() treats .txt as CSV-eligible by
        # design (delimited plain-text exports are common -- see its
        # docstring and api/routes/analysis.py's identical convention).
        # Reuse the same parser/fallback-to-raw-text behaviour either way,
        # but label the result by the extension the user actually gave, so
        # a genuinely unstructured .txt reports itself as "txt" rather than
        # a slightly misleading "csv" in the extraction preview.
        fmt = "txt" if name.endswith((".txt", ".text")) else "csv"
        parsed = parse_csv_declaration(data, filename or f"upload.{fmt}")
        return _tabular_result(parsed, fmt, warnings)

    if head.startswith(b"PK\x03\x04") or name.endswith((".docx", ".docm")):
        try:
            from ingestion.spec_corpus import extract_docx_paragraphs
            paragraphs = extract_docx_paragraphs(data)
            text = "\n\n".join(p for _heading, p in paragraphs)
        except Exception as e:
            return {
                "ok": False, "format": "docx", "text": "", "extraction_method": "docx:failed",
                "records_extracted": 0, "text_source": "", "warnings": [],
                "error": f"Could not extract text from '{filename}' ({e}). The file may be "
                          "corrupted or not a valid .docx.",
            }
        if not text.strip():
            return {
                "ok": False, "format": "docx", "text": "", "extraction_method": "docx:empty",
                "records_extracted": 0, "text_source": "", "warnings": [],
                "error": f"No extractable text found in '{filename}' (empty document?).",
            }
        return {
            "ok": True, "format": "docx", "text": text, "extraction_method": "docx",
            "records_extracted": 0, "text_source": "Document body text", "warnings": [],
            "error": None,
        }

    # Note: .txt/.text is already handled above -- utils.csv_parser.looks_like_csv()
    # claims that extension unconditionally (see its docstring), so a plain-text
    # upload always reaches the CSV parser's ok=False raw-text fallback rather
    # than falling through to here. Kept only as a final backstop.
    return {
        "ok": False, "format": "unsupported", "text": "", "extraction_method": "unsupported",
        "records_extracted": 0, "text_source": "", "warnings": [],
        "error": f"'{filename}' is not a supported file type. Upload a PDF, CSV, XLSX, XLS, "
                  f"DOCX, or TXT ISLD/declaration/claim-disclosure file.",
    }


def _tabular_result(parsed: dict, fmt: str, warnings: list[str]) -> dict:
    """Shared tail for the CSV/XLSX/XLS branches of extract_matchable_text():
    turn a utils.csv_parser/xlsx_parser {"ok","slim","text","meta"} result
    into the extract_matchable_text() response shape."""
    meta = parsed.get("meta", {})
    warnings = list(warnings) + list(meta.get("warnings", []))

    if parsed["ok"]:
        # v0.4: fold ETSI continuation rows (a row that only carries extra
        # standards for the disclosure above it) into that disclosure, the
        # same way Declaration Analysis does (core.prompts), instead of
        # matching each one as its own standard-only "record".
        records = _merge_continuation_records(parsed["slim"]["patents"])
        records = [r for r in records if _record_text(r).strip()]
        record_texts = [_record_text(r) for r in records]
        combined = "\n".join(f"Record {i + 1}: {t}" for i, t in enumerate(record_texts))
        mapped = meta.get("mapped_columns", {})
        cols = sorted(set(mapped.values()) & {"title", "clause", "std"})
        col_label = {"title": "title", "clause": "illustrative/specific-part clause", "std": "declared standard"}
        source = " + ".join(col_label[c] for c in cols) or "Recognised declaration columns"
        if not combined.strip():
            return {
                "ok": False, "format": fmt, "text": "", "extraction_method": f"{fmt}:no_text",
                "records_extracted": 0, "text_source": "", "warnings": warnings,
                "error": f"Recognised {len(records)} row(s) but none contained a title, "
                          "clause, or standard reference to match against.",
            }
        title_or_clause = sum(1 for r in records if r.get("title") or r.get("clause"))
        if title_or_clause < len(records):
            warnings.append(
                f"{len(records) - title_or_clause} of {len(records)} record(s) have no title or "
                "illustrative/specific-part clause in the file -- those are matched on their declared "
                "standard's scope only, and results are restricted to that standard where possible."
            )
        return {
            "ok": True, "format": fmt, "text": combined, "extraction_method": f"{fmt}:structured",
            "records_extracted": len(record_texts), "text_source": f"{source} column(s), {len(record_texts)} record(s)",
            "warnings": warnings, "error": None,
            "records": records,
        }

    # ok=False: header/columns weren't recognised. Fall back to the
    # whole-file rendered text rather than failing outright -- a weird
    # CSV/XLSX still gets matched, just not per-record.
    fallback_text = (parsed.get("text") or "").strip()
    if not fallback_text:
        return {
            "ok": False, "format": fmt, "text": "", "extraction_method": f"{fmt}:unrecognised",
            "records_extracted": 0, "text_source": "", "warnings": warnings,
            "error": f"Could not identify a title, disclosure, or claim column in this "
                      f"{fmt.upper()} file, and it contained no readable text to fall back to. "
                      "No supported text column was detected.",
        }
    return {
        "ok": True, "format": fmt, "text": fallback_text, "extraction_method": f"{fmt}:raw_text",
        "records_extracted": 0, "text_source": "Whole file (columns not recognised — matched as raw text)",
        "warnings": warnings + [
            "Could not identify the expected declaration columns (title/application/publication); "
            "matching against the whole file's text instead of per-record."
        ],
        "error": None,
    }

router = APIRouter()

# v0.3: the default is "AUTO" -- hybrid retrieval (services.retrieval_service)
# across BOTH the ETSI Standards Corpus (source_type="etsi_standard") and the
# legacy 3GPP-crawler corpus (source_type="3gpp_spec_text"),
# whichever has indexed content. Pass an explicit source_type to restrict to
# exactly one corpus.
#
# v0.4 fix: /match-file used to default to "3gpp_spec_text" (unlike /match),
# so uploaded ISLD files silently skipped the ETSI Standards Corpus. Both
# endpoints (and the new job endpoints) now default to AUTO.
DEFAULT_SOURCE_TYPE = "3gpp_spec_text"  # value for explicit single-corpus requests
AUTO_SOURCE_TYPES = ("etsi_standard", "3gpp_spec_text")


class TextMatchRequest(BaseModel):
    """Body of POST /api/text-match/match and /jobs/text."""
    text: str = Field(..., min_length=1, description="Patent claim / disclosure text to match against the standards corpus.")
    top_k: int = Field(5, ge=1, le=50, description="Matches to return per segment.")
    min_score: float = Field(0.3, ge=0.0, le=1.0, description="Minimum similarity score to include a match.")
    source_type: Optional[str] = Field(
        None,
        description="Restrict matches to one corpus source_type. Omit/null (default) hybrid-searches "
                     "both the ETSI Standards Corpus and the legacy 3GPP corpus, whichever is indexed.",
    )


class SegmentMatch(BaseModel):
    """One retrieved standards passage for a segment: its text, similarity score and source metadata
    (spec, version, clause, source_type...).
    """
    content: str
    score: float
    metadata: dict


class TextMatchSegmentResult(BaseModel):
    """Matches for one query unit (a record of a structured upload, or a text segment) plus the record
    context shown in the UI.
    """
    segment: str
    matches: list[SegmentMatch]
    # v0.4: per-record context for structured (CSV/XLSX/XLS) uploads.
    label: Optional[str] = None                   # "Record 3 · US11000053B2"
    declared_standards: Optional[list[str]] = None
    query_basis: Optional[str] = None             # "title + clause" / "declared standard scope only" / "text"


class TextMatchResponse(BaseModel):
    """Full Text Matching result: per-segment matches, corpus size, warnings and how the input file was
    read.
    """
    segments: list[TextMatchSegmentResult]
    corpus_indexed_chunks: int
    warning: Optional[str] = None
    source_filename: Optional[str] = None
    extraction_method: Optional[str] = None
    low_confidence_pages: Optional[list[int]] = None
    file_format: Optional[str] = None
    records_extracted: Optional[int] = None
    text_source: Optional[str] = None
    extraction_preview: Optional[str] = None


# ── Matching units ───────────────────────────────────────────────────────────
# A "unit" is one query sent to retrieval: one declared record for a
# structured upload, or one ~128-token segment of free text (paste / PDF /
# DOCX / unrecognised CSV). v0.4 fix: structured uploads used to be
# concatenated into one string and re-chunked, so a single segment mixed
# several records and records carrying only a standard number ("Standard:
# TS 38.331") produced near-random matches. Each record is now its own unit.

_SPEC_NUM_RE = re.compile(r"(?<!\d)(\d{2,3})\s?[.\s]?\s?(\d{3})(?!\d)")


def _norm_spec(value: str) -> Optional[str]:
    """'TS 38.331' / '38331' / 'ETSI TS 138 331' -> '38.331'. None if no spec number."""
    if not value:
        return None
    m = _SPEC_NUM_RE.search(str(value))
    if not m:
        return None
    series, num = m.group(1), m.group(2)
    if len(series) == 3 and series.startswith("1"):  # ETSI 1xx yyy == 3GPP xx.yyy
        series = series[1:]
    return f"{series}.{num}"


_SPEC_CATALOG: Optional[dict[str, dict]] = None


def _spec_catalog() -> dict[str, dict]:
    """Normalised spec number -> {"spec", "title", "description"} from the
    platform's built-in 3GPP knowledge list (ingestion.pipeline)."""
    global _SPEC_CATALOG
    if _SPEC_CATALOG is None:
        catalog: dict[str, dict] = {}
        try:
            from ingestion.pipeline import BUILTIN_3GPP_KNOWLEDGE
            for entry in BUILTIN_3GPP_KNOWLEDGE:
                key = _norm_spec(entry.get("spec", ""))
                if key and key not in catalog:
                    catalog[key] = entry
        except Exception:
            logger.warning("Built-in 3GPP spec catalog unavailable for Text Matching.", exc_info=True)
        _SPEC_CATALOG = catalog
    return _SPEC_CATALOG


def _std_list(rec: dict) -> list[str]:
    """Declared standards of a parsed record as a clean list (accepts a list or a comma-separated
    string).
    """
    std = rec.get("std") or []
    if isinstance(std, str):
        std = [s.strip() for s in std.split(",") if s.strip()]
    return [str(s) for s in std if str(s).strip()]


def _record_units(records: list[dict]) -> list[dict]:
    """One matching unit per declared record of a structured (CSV/XLSX) upload. Records with a title or
    clause are queried with that text; records that only name a standard are queried with that
    standard's scope from the built-in catalogue and restricted to the declared standard in ranking.
    """
    units: list[dict] = []
    catalog = _spec_catalog()
    for i, rec in enumerate(records):
        stds = _std_list(rec)
        ident = rec.get("pub") or rec.get("app") or ""
        label = f"Record {i + 1}" + (f" · {ident}" if ident else "")
        title = (rec.get("title") or "").strip()
        clause = (rec.get("clause") or "").strip()

        if title or clause:
            parts = [title] if title else []
            if stds:
                parts.append(f"Standard: {', '.join(stds)}")
            if clause:
                parts.append(f"Clause: {clause}")
            query = ". ".join(parts)
            basis = " + ".join(b for b, v in (("title", title), ("clause", clause)) if v)
        else:
            # No descriptive text in the file for this disclosure. A bare spec
            # number embeds to almost nothing, so query with each declared
            # spec's scope (title + description from the built-in catalog) and
            # restrict results to the declared spec(s) in _rank().
            scope = []
            for sd in stds:
                entry = catalog.get(_norm_spec(sd) or "")
                if entry:
                    scope.append(f"{entry['spec']} {entry.get('title', '')}. {entry.get('description', '')}")
                else:
                    scope.append(sd)
            query = " ".join(scope)
            basis = "declared standard scope only (no title/clause in file)"

        units.append({
            "segment": _record_text(rec),
            "query": query,
            "label": label,
            "declared_standards": stds,
            "declared_norm": sorted({n for n in (_norm_spec(s) for s in stds) if n}),
            "restrict_to_declared": not (title or clause),
            "query_basis": basis,
        })
    return [u for u in units if u["query"].strip()]


def _text_units(text: str) -> list[dict]:
    """Matching units for free text (paste / PDF / DOCX): ~128-token semantic chunks with a small
    overlap.
    """
    segments = semantic_chunk(text, chunk_size=128, overlap=16)
    if not segments:
        cleaned = text.strip()
        segments = [cleaned] if cleaned else []
    return [{
        "segment": seg, "query": seg, "label": None, "declared_standards": None,
        "declared_norm": [], "restrict_to_declared": False, "query_basis": None,
    } for seg in segments]


def _hit_spec(meta: dict) -> Optional[str]:
    """Normalised spec number ('38.331') of a retrieved hit, from whichever metadata field carries it."""
    for key in ("spec", "standard", "specification_number", "standard_name"):
        n = _norm_spec(str(meta.get(key) or ""))
        if n:
            return n
    return None


async def _search(query: str, top_k: int, min_score: float, source_type: Optional[str]) -> list[SegmentMatch]:
    """Retrieve candidate passages for one query. With an explicit source_type, searches only that
    vector-store source; otherwise uses the shared hybrid retrieval (3GPP Retrieval Index and/or
    Standards Corpus, services/retrieval_service.py).
    """
    if source_type:
        vs = get_vector_store()
        hits = await vs.similarity_search(query, top_k=top_k, filter_source=source_type, min_score=min_score)
        return [
            SegmentMatch(content=h.get("content", ""), score=float(h.get("score", 0.0)),
                         metadata=dict(h.get("metadata", {}) or {}))
            for h in hits
        ]
    from services.retrieval_service import retrieve
    evidence = await retrieve(query, top_k=top_k, min_score=min_score, source_types=AUTO_SOURCE_TYPES)
    return [
        SegmentMatch(
            content=e.content, score=e.combined_score,
            metadata={
                "source_type": e.source_type, "standard": e.standard,
                "specification_number": e.specification_number, "release": e.release,
                "spec": e.standard, "clause": e.clause, "source_url": e.source_url,
            },
        )
        for e in evidence
    ]


def _rank(unit: dict, matches: list[SegmentMatch], top_k: int) -> list[SegmentMatch]:
    """Flag matches from a declared standard and list them first. Scores
    are never changed. For records with no title/clause, keep only matches
    from the declared standard(s) whenever any exist."""
    declared = set(unit["declared_norm"])
    if not declared:
        return matches[:top_k]
    for m in matches:
        m.metadata["declared_standard_match"] = _hit_spec(m.metadata) in declared
    in_decl = [m for m in matches if m.metadata["declared_standard_match"]]
    others = [m for m in matches if not m.metadata["declared_standard_match"]]
    if unit["restrict_to_declared"] and in_decl:
        return in_decl[:top_k]
    return (in_decl + others)[:top_k]


async def _match_unit(unit: dict, top_k: int, min_score: float, source_type: Optional[str],
                      cache: dict) -> TextMatchSegmentResult:
    """Matches for one unit, cached per (query, declared specs, restriction) within a run so identical
    records are only searched once. Over-fetches when re-ranking by declared standard so declared-
    spec hits are not cut off. 500 if retrieval itself fails.
    """
    key = (unit["query"], tuple(unit["declared_norm"]), unit["restrict_to_declared"])
    if key not in cache:
        # Over-fetch when re-ranking by declared standard so declared-spec hits aren't cut off.
        fetch_k = min(50, top_k * 4) if unit["declared_norm"] else top_k
        try:
            raw = await _search(unit["query"], fetch_k, min_score, source_type)
        except Exception as e:
            logger.error(f"similarity_search failed for a segment: {e}", exc_info=True)
            raise HTTPException(500, f"Text matching search failed: {e}")
        cache[key] = _rank(unit, raw, top_k)
    return TextMatchSegmentResult(
        segment=unit["segment"],
        matches=[m.model_copy(deep=True) for m in cache[key]],
        label=unit["label"],
        declared_standards=unit["declared_standards"],
        query_basis=unit["query_basis"],
    )


async def _retrieval_index_summary() -> dict:
    """v0.5.0: whether the 3GPP Retrieval Index (services/sqlite_retrieval.py)
    is installed and usable, and how many documents it holds. Counts only --
    no server paths -- because this is returned to every ISLD user, Guests
    included. Never raises."""
    try:
        from services import sqlite_retrieval as sr
        st = await asyncio.to_thread(sr.status, False)
        return {"ok": bool(st.get("ok")), "configured": bool(st.get("configured")),
                "documents": st.get("documents") if st.get("ok") else None}
    except Exception as e:  # noqa: BLE001 - status is informational
        logger.warning(f"Could not read the 3GPP Retrieval Index status: {e}")
        return {"ok": False, "configured": False, "documents": None}


async def _corpus_readiness() -> tuple[int, Optional[str]]:
    """``(vector-store chunk count, warning)``. Matching searches the 3GPP
    Retrieval Index when one is installed and the Standards Corpus vector
    store otherwise (services/retrieval_service.py, RETRIEVAL_SOURCE=auto),
    so the "nothing to match against" warning is only raised when BOTH are
    empty (before v0.5.0 it ignored an installed index)."""
    vs = get_vector_store()
    try:
        vs_stats = await vs.get_stats()
    except Exception as e:
        logger.warning(f"Could not fetch vector store stats: {e}")
        vs_stats = {"indexed_chunks": 0}
    indexed = vs_stats.get("indexed_chunks", 0)
    warning = None
    if not indexed and not (await _retrieval_index_summary())["ok"]:
        warning = (
            "No standards text is available to match against yet -- results will be empty. "
            "Install the 3GPP Retrieval Index (copy the retrieval_data folder into dist/retrieval_data, "
            "see the 3GPP Retrieval Index page), import a dataset into the Standards Corpus "
            "(`python -m etsi_corpus.cli ingest --limit 25` in the backend folder)."
        )
    return indexed, warning


async def _prepare_file(filename: Optional[str], data: bytes) -> tuple[list[dict], dict]:
    """Extract an uploaded file into matching units + response metadata.
    Raises HTTPException with an actionable message on failure."""
    if not data:
        raise HTTPException(400, "Uploaded file is empty.")
    name = (filename or "").lower()
    is_pdf = data[:4].lstrip().startswith(b"%PDF") or name.endswith(".pdf")

    if is_pdf:
        try:
            extraction = await extract_text_from_pdf_with_confidence(data, filename or "upload.pdf")
        except Exception as e:
            logger.error(f"PDF extraction failed for {filename}: {e}", exc_info=True)
            raise HTTPException(422, f"Could not extract text from '{filename}': {e}")
        text = (extraction.get("text") or "").strip()
        if not text:
            raise HTTPException(422, f"No extractable text found in '{filename}' (empty/blank document?).")
        low_conf = extraction.get("low_confidence_pages") or []
        warnings = []
        if low_conf:
            warnings.append(f"{len(low_conf)} page(s) had low OCR confidence (pages {low_conf}); "
                            "matches from those segments may be less reliable.")
        return _text_units(text), {
            "source_filename": filename, "extraction_method": extraction.get("method"),
            "low_confidence_pages": low_conf or None, "file_format": "pdf", "warnings": warnings,
        }

    extracted = extract_matchable_text(filename, data)
    if not extracted["ok"]:
        raise HTTPException(422, {"message": extracted["error"], "supported_formats": SUPPORTED_FORMATS})
    records = extracted.get("records")
    units = _record_units(records) if records else _text_units(extracted["text"])
    return units, {
        "source_filename": filename, "extraction_method": extracted["extraction_method"],
        "file_format": extracted["format"], "records_extracted": extracted["records_extracted"],
        "text_source": extracted["text_source"], "extraction_preview": extracted["text"][:1000],
        "warnings": list(extracted["warnings"]),
    }


def _join_warning(corpus_warning: Optional[str], extra: list[str]) -> Optional[str]:
    """Combine the corpus-readiness warning with extraction warnings into one message (None when there
    are none).
    """
    parts = ([corpus_warning] if corpus_warning else []) + [w for w in extra if w]
    return " ".join(parts) or None


async def _run_all(units: list[dict], top_k: int, min_score: float, source_type: Optional[str]):
    """Match every unit sequentially for the one-shot endpoints; 400 when there is nothing to match."""
    if not units:
        raise HTTPException(400, "No non-empty text to match.")
    cache: dict = {}
    return [await _match_unit(u, top_k, min_score, source_type, cache) for u in units]


# ── One-shot endpoints (kept for API compatibility) ──────────────────────────

@router.post("/match", response_model=TextMatchResponse)
async def match_text(body: TextMatchRequest):
    """Segment pasted-in text and retrieve the closest standards chunks for
    each segment, ranked by similarity score."""
    indexed, warning = await _corpus_readiness()
    results = await _run_all(_text_units(body.text), body.top_k, body.min_score, body.source_type)
    return TextMatchResponse(segments=results, corpus_indexed_chunks=indexed, warning=warning)


@router.post("/match-file", response_model=TextMatchResponse)
async def match_file(
    file: UploadFile = File(..., description="ISLD / declaration / claim-disclosure file — PDF, CSV, XLSX, XLS, DOCX, or TXT."),
    top_k: int = 5,
    min_score: float = 0.3,
    source_type: Optional[str] = None,
):
    """Upload an ISLD/declaration/claim-disclosure file and match it against
    the indexed standards corpus in one request. The UI uses the job
    endpoints below instead, which add progress and pause/resume/stop."""
    units, meta = await _prepare_file(file.filename, await file.read())
    indexed, corpus_warning = await _corpus_readiness()
    results = await _run_all(units, top_k, min_score, source_type or None)
    warnings = meta.pop("warnings", [])
    return TextMatchResponse(segments=results, corpus_indexed_chunks=indexed,
                             warning=_join_warning(corpus_warning, warnings), **meta)


# ── Jobs: progress + pause / resume / stop ───────────────────────────────────
# The UI starts a job, polls GET /jobs/{id} for progress and partial results,
# and can pause, resume or stop it. Control is cooperative and checked
# between units: the unit in flight always finishes, so no result is ever
# half-written. Stop keeps every result produced so far. Jobs live in memory
# only (the most recent MAX_JOBS are kept); a backend restart drops them.

MAX_JOBS = 20
_ACTIVE = {"queued", "extracting", "running", "paused"}


@dataclass
class _TMJob:
    """In-memory state of one Text Matching job (status, progress, partial results, pause/stop
    control). Lost on backend restart by design.
    """
    id: str
    status: str = "queued"      # queued|extracting|running|paused|stopped|completed|failed
    total: int = 0
    done: int = 0
    stop_requested: bool = False
    resume_event: asyncio.Event = field(default_factory=asyncio.Event)
    segments: list = field(default_factory=list)
    meta: dict = field(default_factory=dict)
    corpus_indexed_chunks: int = 0
    warning: Optional[str] = None
    error: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    task: Optional[asyncio.Task] = None

    def snapshot(self) -> dict:
        """JSON progress + every result produced so far, as returned by GET /jobs/{job_id}."""
        return {
            "job_id": self.id,
            "status": self.status,
            "total": self.total,
            "done": self.done,
            "error": self.error,
            "elapsed_s": round((self.finished_at or time.time()) - self.created_at, 1),
            "result": TextMatchResponse(
                segments=list(self.segments),
                corpus_indexed_chunks=self.corpus_indexed_chunks,
                warning=self.warning,
                **{k: v for k, v in self.meta.items() if k != "warnings"},
            ).model_dump(),
        }


_JOBS: dict[str, _TMJob] = {}


def _register(job: _TMJob) -> None:
    """Store a new job and evict the oldest finished jobs beyond MAX_JOBS."""
    _JOBS[job.id] = job
    finished = sorted((j for j in _JOBS.values() if j.status not in _ACTIVE), key=lambda j: j.created_at)
    while len(_JOBS) > MAX_JOBS and finished:
        _JOBS.pop(finished.pop(0).id, None)


def _get_job(job_id: str) -> _TMJob:
    """The job with this id or 404 (expired or lost on restart)."""
    job = _JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "Text matching job not found (it may have expired or the backend restarted).")
    return job


async def _run_job(job: _TMJob, prepare, top_k: int, min_score: float, source_type: Optional[str]) -> None:
    """Job body: extract the input into units, then match unit by unit, honouring pause (waits between
    units) and stop (keeps results so far). Any failure is recorded on the job instead of leaving it
    hanging.
    """
    try:
        job.status = "extracting"
        units, meta = await prepare()
        job.meta = meta
        job.total = len(units)
        indexed, corpus_warning = await _corpus_readiness()
        job.corpus_indexed_chunks = indexed
        job.warning = _join_warning(corpus_warning, meta.get("warnings", []))
        if not units:
            raise HTTPException(400, "No non-empty text to match.")

        if job.status == "extracting":
            job.status = "running"
        cache: dict = {}
        for unit in units:
            # Pause point: wait here (between units) until resumed or stopped.
            while job.status == "paused" and not job.stop_requested:
                job.resume_event.clear()
                await job.resume_event.wait()
            if job.stop_requested:
                break
            job.segments.append(await _match_unit(unit, top_k, min_score, source_type, cache))
            job.done += 1
            await asyncio.sleep(0)  # let pause/stop/poll requests in between units

        job.status = "stopped" if job.stop_requested and job.done < job.total else "completed"
    except HTTPException as e:
        job.status = "failed"
        d = e.detail
        job.error = d.get("message") if isinstance(d, dict) else str(d)
    except asyncio.CancelledError:
        job.status = "stopped"
        raise
    except Exception as e:  # never a silent hang: surface the cause on the job
        logger.error(f"Text matching job {job.id} failed: {e}", exc_info=True)
        job.status = "failed"
        job.error = f"Text matching failed: {e}"
    finally:
        job.finished_at = time.time()


def _start_job(prepare, top_k: int, min_score: float, source_type: Optional[str]) -> dict:
    """Create, register and launch a Text Matching job; returns its first snapshot."""
    job = _TMJob(id=uuid.uuid4().hex[:12])
    _register(job)
    job.task = asyncio.create_task(_run_job(job, prepare, top_k, min_score, source_type))
    return job.snapshot()


@router.post("/jobs/text")
async def start_text_job(body: TextMatchRequest):
    """Start matching pasted text as a pausable/stoppable job."""
    text = body.text

    async def prepare():
        """Units for pasted text (no extraction step)."""
        return _text_units(text), {}

    return _start_job(prepare, body.top_k, body.min_score, body.source_type)


@router.post("/jobs/file")
async def start_file_job(
    file: UploadFile = File(..., description="ISLD / declaration / claim-disclosure file — PDF, CSV, XLSX, XLS, DOCX, or TXT."),
    top_k: int = Form(5),
    min_score: float = Form(0.3),
    source_type: Optional[str] = Form(None),
):
    """Start matching an uploaded file as a pausable/stoppable job.
    Extraction (including OCR for scanned PDFs) runs inside the job."""
    data = await file.read()
    if not data:
        raise HTTPException(400, "Uploaded file is empty.")
    filename = file.filename

    async def prepare():
        """Extract the uploaded file (OCR for scanned PDFs) inside the job."""
        return await _prepare_file(filename, data)

    return _start_job(prepare, top_k, min_score, source_type or None)


@router.get("/jobs/{job_id}")
async def get_job(job_id: str):
    """Progress + every result produced so far."""
    return _get_job(job_id).snapshot()


@router.post("/jobs/{job_id}/pause")
async def pause_job(job_id: str):
    """Pause after the record/segment currently being matched."""
    job = _get_job(job_id)
    if job.status in ("queued", "extracting", "running"):
        job.status = "paused"
    return {"job_id": job.id, "status": job.status, "done": job.done, "total": job.total}


@router.post("/jobs/{job_id}/resume")
async def resume_job(job_id: str):
    """POST /api/text-match/jobs/{job_id}/resume - continue a paused job."""
    job = _get_job(job_id)
    if job.status == "paused":
        job.status = "running" if job.total else "extracting"
        job.resume_event.set()
    return {"job_id": job.id, "status": job.status, "done": job.done, "total": job.total}


@router.post("/jobs/{job_id}/stop")
async def stop_job(job_id: str):
    """Stop after the unit in flight; results so far are kept."""
    job = _get_job(job_id)
    if job.status in _ACTIVE:
        job.stop_requested = True
        job.resume_event.set()  # wake a paused job so it can finish as "stopped"
    return {"job_id": job.id, "status": job.status, "done": job.done, "total": job.total}


@router.get("/status")
async def text_match_status():
    """Readiness check the frontend polls before/while showing Text Matching
    and the ISLD setup panel.

    ``indexed_chunks`` counts the Standards Corpus vector store;
    ``retrieval_index`` reports the 3GPP Retrieval Index; ``ready`` is true
    when EITHER has content, because retrieval uses whichever is available."""
    ri = await _retrieval_index_summary()
    vs = get_vector_store()
    try:
        stats = await vs.get_stats()
    except Exception as e:
        return {"error": str(e), "indexed_chunks": 0, "retrieval_index": ri, "ready": ri["ok"]}
    return {**stats, "retrieval_index": ri, "ready": stats.get("indexed_chunks", 0) > 0 or ri["ok"]}
