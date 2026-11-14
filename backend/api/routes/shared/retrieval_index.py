"""
Retrieval index (SQLite / FTS5) endpoints  (v0.4.5)
===================================================

Read-only access to the ``retrieval_data`` folder - the output of tgpp-sqlite-retrieval - found in
``<project>/dist/retrieval_data`` (default) or the folder named by ``CPARAS_RETRIEVAL_DATA``. No path is ever accepted from a client; nothing is imported, written or copied.

    GET /api/retrieval-index/status?deep=        configured? usable? where it looks, which files are there, what it contains
    GET /api/retrieval-index/search?q=&spec=&release=&content_type=&limit=    keyword (BM25) search, no embedder
    GET /api/retrieval-index/evidence?q=&limit=  the hybrid evidence the analyses use (FTS candidates + embedder re-score)
    GET /api/retrieval-index/specs?spec=&title=&release=
    GET /api/retrieval-index/specs/{spec}/clauses?version=&clause=
    GET /api/retrieval-index/documents/{document_id}   identity, where spec/version/release came from, history
    GET /api/retrieval-index/bulk-corpus/summary       what CPARAS Bulk would score against (counts only)

A hit is a *candidate* found by words and re-scored; it is never proof of essentiality. Rows other than
``content_type == "full_text"`` come from model-generated summaries in the dataset.
"""
from __future__ import annotations

import asyncio
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from services import sqlite_retrieval as sr
from services import tgpp_retrieval as tr

router = APIRouter()


def _unavailable(exc: "sr.RetrievalIndexError"):
    """HTTP error for a RetrievalIndexError: 404 when no index is configured/found, 503 when one exists
    but is unusable; the body carries the error's code and message.
    """
    code = 404 if exc.code in ("not_configured", "not_found") else 503
    return HTTPException(status_code=code, detail=exc.to_dict())


def _call(fn, *a, **kw):
    """Run a blocking retrieval-index helper and translate its exceptions into HTTP errors (404 not
    found, 400 invalid query, 503 index problem). Used with asyncio.to_thread so SQLite work never
    blocks the event loop.
    """
    try:
        return fn(*a, **kw)
    except sr.RetrievalIndexError as exc:
        raise _unavailable(exc)
    except tr.NotFoundError as exc:
        raise HTTPException(status_code=404, detail={"code": "not_found", "message": str(exc)})
    except tr.InvalidQueryError as exc:
        raise HTTPException(status_code=400, detail={"code": "invalid_query", "message": str(exc)})
    except tr.RetrievalError as exc:
        raise HTTPException(status_code=503, detail={"code": "retrieval_error", "message": str(exc)})


@router.get("/status")
async def status(deep: bool = False):
    """GET /api/retrieval-index/status?deep= - whether the 3GPP Retrieval Index is installed and
    usable, where the backend looks for it, which files are present and what it contains (documents,
    clauses, versions, size). `deep=true` also verifies schema compatibility and checksums (slow on
    a 20 GB index). Requires `reference.view`; also the source of the home page's 3GPP Retrieval
    Index card.
    """
    return await asyncio.to_thread(sr.status, deep)


def _search(q, spec, release, content_type, limit, offset, mode):
    """Keyword (FTS5/BM25) search with optional spec / release / content-type filters (runs in a worker
    thread).
    """
    flt = {}
    if spec:
        flt["spec_id"] = spec
    if release:
        flt["release"] = release
    if content_type:
        flt["content_type"] = content_type
    with sr.open_index() as idx:
        return tr.search(idx, q, flt, limit, mode=mode, offset=offset)


@router.get("/search")
async def search(q: str = Query(..., min_length=2), spec: Optional[str] = None, release: Optional[str] = None,
                 content_type: Optional[list[str]] = Query(None), mode: str = "all",
                 limit: int = Query(10, ge=1, le=100), offset: int = Query(0, ge=0)):
    """GET /api/retrieval-index/search - keyword search over the index without the embedding model.
    `mode`: all words, any word, or exact phrase.
    """
    if mode not in ("all", "any", "phrase"):
        raise HTTPException(status_code=400, detail={"code": "invalid_query", "message": "mode must be all, any or phrase"})
    hits = await asyncio.to_thread(_call, _search, q, spec, release, content_type, limit, offset, mode)
    return {"query": q, "mode": mode, "count": len(hits), "results": hits}


@router.get("/evidence")
async def evidence(q: str = Query(..., min_length=3), limit: int = Query(8, ge=1, le=30),
                   min_score: float = Query(0.35, ge=0.0, le=1.0)):
    """GET /api/retrieval-index/evidence - the hybrid evidence the analyses use: FTS candidates re-
    scored by the embedder. Results are candidates, never proof of essentiality.
    """
    try:
        hits = await sr.similarity_search(q, top_k=limit, min_score=min_score)
    except sr.RetrievalIndexError as exc:
        raise _unavailable(exc)
    return {"query": q, "count": len(hits), "results": hits}


def _specs(spec, title, release, limit):
    """Specification lookup by id / title / release (worker thread)."""
    with sr.open_index() as idx:
        return tr.find_specifications(idx, spec_id=spec, title=title, release=release, limit=limit)


@router.get("/specs")
async def specs(spec: Optional[str] = None, title: Optional[str] = None, release: Optional[str] = None,
                limit: int = Query(50, ge=1, le=500)):
    """GET /api/retrieval-index/specs - list specifications matching the filters."""
    return {"results": await asyncio.to_thread(_call, _specs, spec, title, release, limit)}


def _clauses(spec, version, clause):
    """One clause of a specification version, or the specification with its clause list (worker
    thread).
    """
    with sr.open_index() as idx:
        if clause:
            return tr.get_clause(idx, spec, clause, version)
        return tr.get_specification(idx, spec, version, include_clauses=True)


@router.get("/specs/{spec}/clauses")
async def clauses(spec: str, version: Optional[str] = None, clause: Optional[str] = None):
    """GET /api/retrieval-index/specs/{spec}/clauses?version=&clause= - clause text / clause list for a
    specification.
    """
    return await asyncio.to_thread(_call, _clauses, spec, version, clause)


def _document(document_id):
    """One indexed document with its version history (worker thread)."""
    with sr.open_index() as idx:
        return tr.get_document(idx, document_id, include_history=True)


@router.get("/documents/{document_id:path}")
async def document(document_id: str):
    """GET /api/retrieval-index/documents/{document_id} - identity of an indexed document, where its
    spec/version/release metadata came from, and its history.
    """
    return await asyncio.to_thread(_call, _document, document_id)


@router.get("/bulk-corpus/summary")
async def bulk_corpus_summary():
    """What CPARAS Bulk would score against right now (cached; the first call builds it)."""
    from research.bulk_corpus import get_bulk_corpus
    from core import gpp_dataset as gd
    try:
        corpus = await asyncio.to_thread(get_bulk_corpus)
    except gd.DatasetError as exc:
        raise HTTPException(status_code=503, detail=exc.to_dict())
    return {"source": corpus.source, "entries": len(corpus.entries), "families": len(corpus.all_families()),
            "disclaimer": corpus.disclaimer, "info": corpus.info}
