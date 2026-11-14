"""
Specification-, version- and clause-aware lookup over the imported 3GPP documents (v0.4.4).

These are plain database reads of what the dataset import stored - no similarity score is involved and
nothing here says anything about patent essentiality. Specification number, version, release, document id,
clause numbers and the source reference (pdf_url / structured_path) are returned exactly as imported.
A version that the dataset could not determine is reported as "Unknown" with ``version_known: false``;
it is never replaced by a guess, and "latest" is only ever chosen among documents with a KNOWN version.
"""
from __future__ import annotations

import json
from typing import Optional

from sqlalchemy import func, select

from core import gpp_dataset as gd
from core.database import AsyncSessionLocal, EtsiDocument

_LIST_COLS = (EtsiDocument.id, EtsiDocument.specification_number, EtsiDocument.version, EtsiDocument.release,
              EtsiDocument.title, EtsiDocument.document_type, EtsiDocument.series, EtsiDocument.technology_domain,
              EtsiDocument.status, EtsiDocument.pdf_url, EtsiDocument.standard_name_raw, EtsiDocument.chunks_indexed,
              EtsiDocument.structured_path)


def _doc_dict(r) -> dict:
    """API shape of one imported specification document."""
    return {"etsi_document_id": r.id, "specification_number": r.specification_number, "version": r.version,
            "release": r.release, "version_known": gd.version_key(r.version) is not None, "title": r.title,
            "document_type": r.document_type, "series": r.series, "technology_domain": r.technology_domain,
            "status": r.status, "chunks_indexed": r.chunks_indexed, "pdf_url": r.pdf_url,
            "standard_name": r.standard_name_raw, "structured_path": r.structured_path}


def _sort_key(d: dict):
    """Version sort key that puts unknown versions first and orders known ones numerically."""
    k = gd.version_key(d["version"])
    return (1, k) if k is not None else (0, (0, 0, 0))


async def list_specs(*, series: Optional[str] = None, q: Optional[str] = None, limit: int = 50, offset: int = 0) -> dict:
    """Distinct imported specifications with document counts, title and domain (filter by series /
    text, paged).
    """
    async with AsyncSessionLocal() as session:
        stmt = select(EtsiDocument.specification_number, func.count().label("n"),
                      func.max(EtsiDocument.title).label("title"), func.max(EtsiDocument.technology_domain).label("domain")
                      ).group_by(EtsiDocument.specification_number)
        if series:
            stmt = stmt.where(EtsiDocument.series == series)
        if q:
            like = f"%{q.strip()}%"
            stmt = stmt.where(EtsiDocument.specification_number.like(like) | EtsiDocument.title.like(like))
        total = (await session.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
        rows = (await session.execute(stmt.order_by(EtsiDocument.specification_number).offset(offset).limit(limit))).all()
    return {"total": int(total), "specs": [{"specification_number": r[0], "documents": int(r[1]), "title": r[2], "technology_domain": r[3]} for r in rows]}


async def spec_versions(spec: str, *, only_searchable: bool = False) -> dict:
    """Every imported version of one specification in version order (optionally only fully indexed
    ones).
    """
    spec_n = gd.norm_spec(spec)
    async with AsyncSessionLocal() as session:
        stmt = select(*_LIST_COLS).where(EtsiDocument.specification_number == spec_n)
        if only_searchable:
            stmt = stmt.where(EtsiDocument.status == "completed")
        rows = (await session.execute(stmt)).all()
    docs = sorted((_doc_dict(r) for r in rows), key=_sort_key)
    known = [d for d in docs if d["version_known"]]
    return {"specification_number": spec_n, "count": len(docs), "versions": docs,
            "latest_known": known[-1] if known else None,
            "unknown_version_documents": sum(1 for d in docs if not d["version_known"])}


async def get_spec(spec: str, *, version: Optional[str] = None, release: Optional[str] = None) -> dict:
    """Pick ONE document of a specification. version given -> exact match (v-prefix tolerated);
    release given -> newest known version in that release; neither -> newest known version. If the
    requested version/release does not exist the result says so (no nearest-match guessing)."""
    sv = await spec_versions(spec)
    docs = sv["versions"]
    sel, how = None, None
    if version and not gd.is_unknown(version):
        want = gd.norm_version(version)
        hits = [d for d in docs if gd.norm_version(d["version"]) == want]
        sel, how = (hits[0] if hits else None), "exact_version"
    elif release and not gd.is_unknown(release):
        want = gd.norm_release(release)
        hits = [d for d in docs if gd.norm_release(d["release"]) == want and d["version_known"]]
        sel, how = (hits[-1] if hits else None), "newest_in_release"
    else:
        sel, how = sv["latest_known"], "latest_known_version"
    return {"specification_number": sv["specification_number"], "selection": how, "found": sel is not None,
            "document": sel, "available_versions": [{"version": d["version"], "release": d["release"], "version_known": d["version_known"],
                                                      "etsi_document_id": d["etsi_document_id"]} for d in docs]}


async def get_clauses(spec: str, *, version: Optional[str] = None, release: Optional[str] = None,
                      clause: Optional[str] = None, limit: int = 200) -> dict:
    """Clause-level retrieval. ``clause`` matches the clause number exactly or as a prefix on a
    dotted boundary ('5.3' matches '5.3', '5.3.3' but not '5.30')."""
    picked = await get_spec(spec, version=version, release=release)
    if not picked["found"]:
        return {**picked, "clauses": [], "message": "No imported document matches that specification/version/release."}
    doc_id = picked["document"]["etsi_document_id"]
    async with AsyncSessionLocal() as session:
        raw = (await session.execute(select(EtsiDocument.clauses_json).where(EtsiDocument.id == doc_id))).scalar_one_or_none()
    try:
        clauses = json.loads(raw) if raw else []
    except (json.JSONDecodeError, TypeError):
        clauses = []
    out = []
    for c in clauses:
        n = gd.normalise_clause(c)
        if n is None:
            continue
        num = n["clause_number"]
        if clause and not (num == clause or num.startswith(clause + ".")):
            continue
        out.append({**n, "source": {"etsi_document_id": doc_id, "specification_number": picked["document"]["specification_number"],
                                    "version": picked["document"]["version"], "release": picked["document"]["release"],
                                    "pdf_url": picked["document"]["pdf_url"], "structured_path": picked["document"]["structured_path"]}})
        if len(out) >= limit:
            break
    return {**picked, "clauses": out, "clause_filter": clause,
            "note": "Clause text is dataset evidence (verbatim/extracted), not an essentiality finding."}
