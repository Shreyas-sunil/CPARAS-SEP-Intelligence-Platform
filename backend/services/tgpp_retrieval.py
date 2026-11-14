# -*- coding: utf-8 -*-
"""tgpp_retrieval -- read-only client for the portable 3GPP SQLite/FTS5 index.

Standard library only. Copy this single file (or use the copy bundled inside the
generated ``retrieval_data`` folder) into any application. It never imports the
index builder, never needs the original dataset, never rebuilds anything.

Quick start::

    import tgpp_retrieval as tr
    idx = tr.open_index("retrieval_data")
    for hit in tr.search(idx, "registration procedure", {"spec_id": "23.501"}, limit=5):
        print(hit["spec_id"], hit["version"], hit["clause_id"], hit["score"], hit["excerpt"])
    idx.close()

Public API (all return plain JSON-serialisable dicts/lists):

    open_index(index_dir)                      -> Index
    search(index, query, filters=None, limit=10, *, mode="all", offset=0,
           include_text=False, highlight=("<<", ">>"), normative=False)
    find_specifications(index, spec_id=None, title=None, version=None, release=None, limit=50)
    get_specification(index, spec_id, version=None, include_clauses=True)
    find_clauses(index, spec_id=None, clause_id=None, title=None, version=None,
                 release=None, clause_prefix=False, limit=50)
    get_clause(index, spec_id, clause_id, version=None, include_text=True)
    get_statistics(index)
    get_manifest(index_dir) / get_build_report(index_dir)
    get_index_status(index_dir, verify_checksum=False)

``index`` may be an open :class:`Index` or a directory path (opened for the call).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple, Union

__version__ = "1.1.0"

#: Version of the index format this client was written for (semver).
SCHEMA_VERSION = "1.1.0"
#: Index major versions this client can read. Minor/patch bumps are additive.
SUPPORTED_SCHEMA_MAJOR = 1
#: Oldest minor version of the supported major that this client can read (1.1 added documents etc.).
MIN_SCHEMA_MINOR = 1
INDEX_FORMAT = "tgpp-sqlite-fts5"

DB_FILENAME = "search_index.sqlite"
MANIFEST_FILENAME = "manifest.json"
SCHEMA_VERSION_FILENAME = "schema_version.json"
REPORT_FILENAME = "build_report.json"
CLIENT_FILENAME = "tgpp_retrieval.py"

CONTENT_TYPES = (
    "full_text",             # chunks of original specification text (text corpus)
    "technical_summary_item",  # one item of technical_summary key_features / mechanisms / procedures / parameters
    "classification",        # joined classification lists (topics, technical_entities, protocols, ...)
    "patent_relevance",      # one patent-relevance concept / feature / mechanism
    "reference",             # one entry of spec_history.references (the document cites it)
    "change_history",        # one change-request / change-in-this-version entry
    "source_text",           # text field found in structured JSON (assumed verbatim, not verified)
    "clause_summary",
    "spec_summary",
    "normative_requirement",
    "terminology",
    "clause_heading",        # heading-only clause (no other content)
    "metadata",
    "other_text",            # text whose role could not be determined
)

# Heuristic ranking multipliers: prefer clause-level evidence over document-level rows.
TYPE_WEIGHTS = {
    "technical_summary_item": 0.9,
    "classification": 0.7,
    "patent_relevance": 0.9,
    "reference": 0.7,
    "change_history": 0.7,
    "normative_requirement": 1.15,
    "full_text": 1.0,
    "source_text": 1.0,
    "clause_summary": 1.0,
    "other_text": 0.95,
    "terminology": 0.95,
    "clause_heading": 0.9,
    "spec_summary": 0.75,
    "metadata": 0.6,
}
_MODAL_RE = re.compile(r"\b(shall|must)\b", re.I)
_ALLOWED_FILTERS = {
    "spec_id", "version", "release", "content_type",
    "clause_id", "clause_prefix", "clause_level_only",
    "document_id", "field", "technology_domain",
}
_MAX_LIMIT = 1000


# --------------------------------------------------------------------------- errors
class RetrievalError(Exception):
    """Base class for all errors raised by this module."""


class IndexNotFoundError(RetrievalError):
    """The index directory or its database file does not exist."""


class IncompatibleIndexError(RetrievalError):
    """The index format/major version is not supported, or SQLite lacks FTS5."""


class InvalidQueryError(RetrievalError):
    """The query, filters or paging arguments are invalid."""


class NotFoundError(RetrievalError):
    """The requested specification or clause is not in the index."""


# ------------------------------------------------------------------- normalisation
_SPEC_RE = re.compile(r"(?<!\d)(\d{2})\.?(\d{3})(?:-(\d{1,2}))?(?!\d)")
_DOCTYPE_RE = re.compile(r"(?<![A-Za-z])(TS|TR)(?![A-Za-z])", re.I)
_REL_RE = re.compile(r"(?:rel(?:ease)?[\s._-]*|r)?(\d{1,2})", re.I)


def normalize_spec_id(raw: Any) -> Tuple[Optional[str], Optional[str]]:
    """Return ``(spec_id, doc_type)``. ``"TS 23501"`` -> ``("23.501", "TS")``.

    Unrecognised strings are returned trimmed but otherwise unchanged; empty -> (None, None).
    ``doc_type`` is only returned if it literally appears in the input.
    """
    if raw is None:
        return None, None
    s = " ".join(str(raw).split())
    if not s:
        return None, None
    dt = _DOCTYPE_RE.search(s)
    doc_type = dt.group(1).upper() if dt else None
    m = _SPEC_RE.search(s)
    if m:
        sid = "%s.%s" % (m.group(1), m.group(2))
        if m.group(3):
            sid += "-" + m.group(3)
        return sid, doc_type
    return s, doc_type


def normalize_release(raw: Any) -> Optional[str]:
    """``"Rel-17"``, ``"Release 17"``, ``17`` -> ``"17"``; unknown formats returned trimmed."""
    if raw is None or isinstance(raw, bool):
        return None
    s = str(raw).strip()
    if not s:
        return None
    m = _REL_RE.fullmatch(s)
    return m.group(1).lstrip("0") or "0" if m else s


def version_sort_key(version: Optional[str]) -> Tuple[int, Tuple[int, ...]]:
    """Sort key: numeric dotted versions order numerically; unknown/letter-coded sort lowest."""
    if version is None:
        return (0, ())
    if re.fullmatch(r"\d+(\.\d+)*", version):
        return (2, tuple(int(p) for p in version.split(".")))
    return (1, ())


def clause_sort_key(clause_id: Optional[str]) -> Tuple:
    """Natural ordering of clause ids ('5.2.10' after '5.2.9', annexes 'A.1' after numbers)."""
    if clause_id is None:
        return ((3, "", 0),)
    parts = []
    for p in clause_id.split("."):
        if p.isdigit():
            parts.append((0, "", int(p)))
        else:
            parts.append((1, p, 0))
    return tuple(parts)


def clean_clause_id(raw: Any) -> Optional[str]:
    """Normalise a clause reference: strip 'clause'/'section'/'sec.'/'§' prefixes and trailing dots
    ('Clause 5.2.2.' -> '5.2.2'); None for empty/boolean input.
    """
    if raw is None or isinstance(raw, bool):
        return None
    s = str(raw).strip()
    s = re.sub(r"^(?:clause|section|subclause|sec\.?|§)\s*", "", s, flags=re.I).strip()
    s = s.rstrip(".").strip()
    return s or None


# ------------------------------------------------------------------------ opening
class Index:
    """Read-only handle to an index directory. Use :func:`open_index`."""

    def __init__(self, index_dir: Path, conn: sqlite3.Connection, schema_info: Dict[str, Any]):
        """An open, schema-checked index: its folder, the read-only SQLite connection and the parsed
        schema_version.json.
        """
        self.index_dir = index_dir
        self.conn = conn
        self.schema_info = schema_info

    def close(self) -> None:
        """Close the SQLite connection (errors ignored)."""
        try:
            self.conn.close()
        except sqlite3.Error:
            pass

    def __enter__(self) -> "Index":
        """Use as a context manager; the connection is closed on exit."""
        return self

    def __exit__(self, *exc: Any) -> None:
        """Close the index."""
        self.close()

    # Convenience methods mirroring the module-level functions.
    def search(self, query: str, filters: Optional[dict] = None, limit: int = 10, **kw: Any) -> List[dict]:
        """Shortcut for module-level `search(self, ...)`."""
        return search(self, query, filters, limit, **kw)

    def get_specification(self, spec_id: str, version: Optional[str] = None, **kw: Any) -> dict:
        """Shortcut for module-level `get_specification(self, ...)`."""
        return get_specification(self, spec_id, version, **kw)

    def get_clause(self, spec_id: str, clause_id: str, version: Optional[str] = None, **kw: Any) -> dict:
        """Shortcut for module-level `get_clause(self, ...)`."""
        return get_clause(self, spec_id, clause_id, version, **kw)


def _read_json(path: Path) -> dict:
    """Load a JSON metadata file of the index; IndexNotFoundError when missing, IncompatibleIndexError
    when unreadable.
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        raise IndexNotFoundError("File not found: %s" % path)
    except (OSError, ValueError) as exc:
        raise RetrievalError("Cannot read %s: %s" % (path, exc))
    if not isinstance(data, dict):
        raise RetrievalError("%s does not contain a JSON object" % path)
    return data


def _check_schema_info(info: dict) -> int:
    """Validate schema_version.json (index format and a supported major schema version); returns the
    minor version. Raises IncompatibleIndexError otherwise.
    """
    if info.get("index_format") != INDEX_FORMAT:
        raise IncompatibleIndexError(
            "Unknown index_format %r (expected %r)." % (info.get("index_format"), INDEX_FORMAT))
    ver = str(info.get("schema_version", ""))
    m = re.match(r"^(\d+)\.(\d+)\.(\d+)$", ver)
    if not m:
        raise IncompatibleIndexError("Unparseable schema_version %r." % ver)
    major, minor = int(m.group(1)), int(m.group(2))
    if major == SUPPORTED_SCHEMA_MAJOR and minor < MIN_SCHEMA_MINOR:
        raise IncompatibleIndexError(
            "Index schema %s is older than this client supports (>= %d.%d.0). Rebuild the index with the current builder."
            % (ver, SUPPORTED_SCHEMA_MAJOR, MIN_SCHEMA_MINOR))
    if major != SUPPORTED_SCHEMA_MAJOR:
        raise IncompatibleIndexError(
            "Index schema %s is not supported by this client (supports major version %d). "
            "Rebuild the index with a matching builder or use a matching client."
            % (ver, SUPPORTED_SCHEMA_MAJOR))
    return major


def open_index(index_dir: Union[str, Path]) -> Index:
    """Open ``index_dir`` (the folder holding ``search_index.sqlite``) read-only.

    Raises IndexNotFoundError, IncompatibleIndexError or RetrievalError.
    No network access, no writes, no builder required.
    """
    d = Path(index_dir)
    if not d.is_dir():
        raise IndexNotFoundError("Index directory not found: %s" % d)
    db = d / DB_FILENAME
    if not db.is_file():
        raise IndexNotFoundError("%s not found in %s" % (DB_FILENAME, d))
    sv = d / SCHEMA_VERSION_FILENAME
    if not sv.is_file():
        raise IncompatibleIndexError("%s is missing in %s; cannot verify index format." % (SCHEMA_VERSION_FILENAME, d))
    info = _read_json(sv)
    _check_schema_info(info)
    try:
        conn = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute("SELECT rowid FROM records_fts LIMIT 1").fetchall()
        row = conn.execute("SELECT value FROM build_meta WHERE key='schema_version'").fetchone()
    except sqlite3.OperationalError as exc:
        msg = str(exc)
        if "fts5" in msg.lower():
            raise IncompatibleIndexError(
                "This Python's SQLite (%s) was built without FTS5, which the index requires. "
                "Use python.org's Windows installer (includes FTS5) or a newer SQLite." % sqlite3.sqlite_version)
        raise RetrievalError("Cannot open database %s: %s" % (db, msg))
    except sqlite3.DatabaseError as exc:
        raise RetrievalError("Database %s is unreadable or corrupt: %s" % (db, exc))
    if row is not None and row[0] != info.get("schema_version"):
        conn.close()
        raise IncompatibleIndexError(
            "schema_version.json (%s) disagrees with the database (%s); the folder is inconsistent."
            % (info.get("schema_version"), row[0]))
    return Index(d, conn, info)


def _ensure(index: Union[Index, str, Path]) -> Tuple[Index, bool]:
    """(Index, opened_here) for an Index or a folder path, so API functions accept either and close
    what they opened.
    """
    if isinstance(index, Index):
        return index, False
    return open_index(index), True


# ------------------------------------------------------------------- query building
def _as_list(v: Any) -> List[str]:
    """Filter value as a list of non-empty strings (scalar -> one-item list)."""
    if v is None:
        return []
    if isinstance(v, (str, int, float)):
        return [str(v)]
    return [str(x) for x in v if x is not None and str(x) != ""]


def _like_escape(s: str) -> str:
    """Escape %, _ and backslash for a LIKE pattern used with ESCAPE '\\'."""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _quote(tok: str) -> str:
    """Quote one token as an FTS5 string literal."""
    return '"' + tok.replace('"', '""') + '"'


def build_fts_query(query: str, mode: str = "all") -> str:
    """Turn user text into a safe FTS5 expression. mode: all | any | phrase | raw."""
    if not isinstance(query, str) or not query.strip():
        raise InvalidQueryError("Query is empty.")
    if mode == "raw":
        return query
    if mode == "phrase":
        words = [w for w in re.findall(r"\w+", query)]
        if not words:
            raise InvalidQueryError("Phrase contains no searchable words.")
        return _quote(" ".join(words))
    if mode not in ("all", "any"):
        raise InvalidQueryError("Unknown search mode %r (use all, any, phrase, raw)." % mode)
    groups: List[List[str]] = [[]]
    for m in re.finditer(r'"([^"]*)"|(\S+)', query):
        if m.group(1) is not None:
            words = re.findall(r"\w+", m.group(1))
            if words:
                groups[-1].append(_quote(" ".join(words)))
            continue
        tok = m.group(2)
        if tok == "AND":
            continue
        if tok == "OR":
            groups.append([])
            continue
        prefix = tok.endswith("*")
        words = re.findall(r"\w+", tok)
        if not words:
            continue
        piece = _quote(" ".join(words)) + ("*" if prefix and len(words) == 1 else "")
        groups[-1].append(piece)
    groups = [g for g in groups if g]
    if not groups:
        raise InvalidQueryError("Query contains no searchable terms: %r" % query)
    if mode == "any":
        flat = [t for g in groups for t in g]
        return " OR ".join(flat)
    return " OR ".join("(" + " AND ".join(g) + ")" for g in groups)


def _filter_sql(filters: Optional[dict]) -> Tuple[str, List[Any]]:
    """Translate a filters dict (spec_id, version, release, clause_id, clause_prefix, content_type,
    doc_type, domain...) into a parameterised SQL WHERE fragment. Unknown keys raise
    InvalidQueryError - values are never interpolated into SQL.
    """
    if filters is None:
        return "", []
    if not isinstance(filters, dict):
        raise InvalidQueryError("filters must be a dict or None.")
    unknown = set(filters) - _ALLOWED_FILTERS
    if unknown:
        raise InvalidQueryError("Unknown filter(s): %s. Supported: %s"
                                % (", ".join(sorted(unknown)), ", ".join(sorted(_ALLOWED_FILTERS))))
    clauses: List[str] = []
    params: List[Any] = []
    ids = [normalize_spec_id(x)[0] for x in _as_list(filters.get("spec_id"))]
    if ids:
        clauses.append("s.spec_id IN (%s)" % ",".join("?" * len(ids)))
        params += ids
    vers = _as_list(filters.get("version"))
    if vers:
        parts = []
        for v in vers:
            parts.append("(s.version = ? OR s.version LIKE ? ESCAPE '\\')")
            params += [v, _like_escape(v) + ".%"]
        clauses.append("(" + " OR ".join(parts) + ")")
    rels = [normalize_release(x) for x in _as_list(filters.get("release"))]
    if rels:
        clauses.append("s.release IN (%s)" % ",".join("?" * len(rels)))
        params += rels
    cts = _as_list(filters.get("content_type"))
    if cts:
        clauses.append("r.content_type IN (%s)" % ",".join("?" * len(cts)))
        params += cts
    cid = clean_clause_id(filters.get("clause_id")) if filters.get("clause_id") is not None else None
    if cid:
        clauses.append("c.clause_id = ?")
        params.append(cid)
    cpre = clean_clause_id(filters.get("clause_prefix")) if filters.get("clause_prefix") is not None else None
    if cpre:
        clauses.append("(c.clause_id = ? OR c.clause_id LIKE ? ESCAPE '\\')")
        params += [cpre, _like_escape(cpre) + ".%"]
    if filters.get("clause_level_only"):
        clauses.append("r.clause_pk IS NOT NULL")
    docs = _as_list(filters.get("document_id"))
    if docs:
        clauses.append("lower(d.document_id) IN (%s)" % ",".join("?" * len(docs)))
        params += [x.lower() for x in docs]
    flds = _as_list(filters.get("field"))
    if flds:
        clauses.append("r.field IN (%s)" % ",".join("?" * len(flds)))
        params += flds
    doms = _as_list(filters.get("technology_domain"))
    if doms:
        clauses.append("lower(d.technology_domain) IN (%s)" % ",".join("?" * len(doms)))
        params += [x.lower() for x in doms]
    return (" AND " + " AND ".join(clauses)) if clauses else "", params


def _sentences(text: str) -> List[str]:
    """Split text into sentences/lines for excerpting."""
    return [s.strip() for s in re.split(r"(?<=[.;:])\s+|\n+", text) if s.strip()]


def _modal_excerpt(text: str, max_chars: int = 420) -> str:
    """Up to three normative sentences (shall/must/should...) from `text`, bounded to `max_chars` -
    used for the 'normative' search mode.
    """
    sents = [s for s in _sentences(text) if _MODAL_RE.search(s)]
    out, size = [], 0
    for s in sents[:3]:
        if size + len(s) > max_chars and out:
            break
        out.append(s)
        size += len(s)
    return " ".join(out)[:max_chars + 80]


def _run_fts(idx: Index, fts_query: str, where_sql: str, params: List[Any], pool: int,
             markers: Tuple[str, str], need_text: bool, extra_where: str = "",
             extra_params: Sequence[Any] = ()) -> List[sqlite3.Row]:
    """Execute the FTS5 MATCH query joined to records/documents/specifications with the given filters,
    returning up to `pool` BM25-ranked rows (text column only when needed).
    """
    sql = (
        "SELECT r.rec_pk, r.record_id, r.content_type, r.origin, r.label, r.field, r.page,"
        " d.document_id, d.technology_domain,"
        " s.spec_id, s.doc_type, s.title AS spec_title, s.version, s.release,"
        " c.clause_id, c.title AS clause_title, src.rel_path AS source_path,"
        " bm25(records_fts, 1.0, 2.0, 2.0, 1.0) AS bm,"
        " snippet(records_fts, 0, ?, ?, ' ... ', 32) AS excerpt,"
        + (" records_fts.text AS full_text" if need_text else " NULL AS full_text") +
        " FROM records_fts"
        " JOIN records r ON r.rec_pk = records_fts.rowid"
        " JOIN specifications s ON s.spec_pk = r.spec_pk"
        " LEFT JOIN clauses c ON c.clause_pk = r.clause_pk"
        " LEFT JOIN documents d ON d.document_pk = r.document_pk"
        " JOIN sources src ON src.source_id = r.source_id"
        " WHERE records_fts MATCH ?" + where_sql + extra_where +
        " ORDER BY bm LIMIT ?"
    )
    args = [markers[0], markers[1], fts_query] + list(params) + list(extra_params) + [pool]
    try:
        return idx.conn.execute(sql, args).fetchall()
    except sqlite3.OperationalError as exc:
        raise InvalidQueryError("Invalid full-text query (%s). Query was: %s" % (exc, fts_query))


def _norm_ws(s: str) -> str:
    """Case-folded, whitespace-collapsed text for duplicate detection."""
    return " ".join(s.casefold().split())


def _row_to_result(row: sqlite3.Row, score: float, excerpt: str, text: Optional[str],
                   derived: bool) -> dict:
    """JSON result dict for one search hit (spec/version/release/clause, content type, provenance,
    score, excerpt and optional full text).
    """
    return {
        "record_id": row["record_id"],
        "spec_id": row["spec_id"],
        "doc_type": row["doc_type"],
        "spec_title": row["spec_title"],
        "version": row["version"],
        "release": row["release"],
        "clause_id": row["clause_id"],
        "clause_title": row["clause_title"],
        "content_type": row["content_type"],
        "origin": row["origin"],
        "label": row["label"],
        "document_id": row["document_id"],
        "field": row["field"],
        "page": row["page"],
        "excerpt": " ".join((excerpt or "").split()),
        "text": text,
        "score": round(score, 4),
        "source_path": row["source_path"],
        "derived_normative": derived,
    }


def search(index: Union[Index, str, Path], query: str, filters: Optional[dict] = None,
           limit: int = 10, *, mode: str = "all", offset: int = 0, include_text: bool = False,
           highlight: Optional[Tuple[str, str]] = ("<<", ">>"), normative: bool = False) -> List[dict]:
    """Full-text search. Returns a list of result dicts, best first.

    mode: "all" (every term; default), "any", "phrase" (exact phrase, verified
    case-insensitively against the stored text), "raw" (FTS5 syntax).
    filters: see module docs / docs/RETRIEVAL_API.md.
    normative=True: only requirement-bearing evidence (explicit normative_requirement rows
    plus sentences containing 'shall'/'must' from source text; the latter are flagged
    ``derived_normative=True``).
    Raises InvalidQueryError for bad input. No results -> [].
    """
    if not isinstance(limit, int) or isinstance(limit, bool) or not (1 <= limit <= _MAX_LIMIT):
        raise InvalidQueryError("limit must be an integer between 1 and %d." % _MAX_LIMIT)
    if not isinstance(offset, int) or offset < 0:
        raise InvalidQueryError("offset must be a non-negative integer.")
    where_sql, params = _filter_sql(filters)
    fts_query = build_fts_query(query, mode)
    markers = highlight if highlight else ("", "")
    idx, opened = _ensure(index)
    try:
        want = offset + limit
        pool = min(max(want * 6, 60), 2000)
        need_text = include_text or mode == "phrase" or normative
        scored: Dict[str, Tuple[float, sqlite3.Row, str, bool]] = {}

        def add(rows: Iterable[sqlite3.Row], derived_mode: bool) -> None:
            for row in rows:
                text = row["full_text"]
                excerpt = row["excerpt"]
                derived = False
                if mode == "phrase":
                    words = re.findall(r"\w+", query)
                    hay = " ".join(re.findall(r"\w+", (text or "").casefold()))
                    if " ".join(w.casefold() for w in words) not in hay:
                        continue
                if derived_mode:
                    ex = _modal_excerpt(text or "")
                    if not ex:
                        continue
                    excerpt, derived = ex, True
                score = -float(row["bm"]) * TYPE_WEIGHTS.get(row["content_type"], 1.0)
                prev = scored.get(row["record_id"])
                if prev is None or score > prev[0]:
                    scored[row["record_id"]] = (score, row, excerpt, derived)

        if normative:
            add(_run_fts(idx, fts_query, where_sql, params, pool, markers, need_text,
                         " AND r.content_type = 'normative_requirement'"), False)
            modal_q = "(%s) AND (\"shall\" OR \"must\")" % fts_query
            add(_run_fts(idx, modal_q, where_sql, params, pool, markers, need_text,
                         " AND r.content_type IN ('full_text','source_text')"), True)
        else:
            add(_run_fts(idx, fts_query, where_sql, params, pool, markers, need_text), False)

        ordered = sorted(scored.values(), key=lambda t: (-t[0], t[1]["record_id"]))
        page = ordered[offset:offset + limit]
        out = []
        for score, row, excerpt, derived in page:
            text = row["full_text"] if include_text else None
            if include_text and text is None:
                r2 = idx.conn.execute("SELECT text FROM records_fts WHERE rowid=?", (row["rec_pk"],)).fetchone()
                text = r2[0] if r2 else None
            out.append(_row_to_result(row, score, excerpt, text, derived))
        return out
    finally:
        if opened:
            idx.close()


# --------------------------------------------------------------- spec / clause access
def _spec_rows(idx: Index, spec_id: str, version: Optional[str]) -> List[sqlite3.Row]:
    """All rows of a specification (optionally one version / version prefix); InvalidQueryError for an
    empty id, NotFoundError when absent.
    """
    sid, _ = normalize_spec_id(spec_id)
    if not sid:
        raise InvalidQueryError("spec_id is empty.")
    rows = idx.conn.execute("SELECT * FROM specifications WHERE spec_id = ?", (sid,)).fetchall()
    if not rows:
        raise NotFoundError("Specification %r not found in the index. Try find_specifications()." % spec_id)
    if version is not None:
        v = str(version).strip()
        rows = [r for r in rows if r["version"] == v or (r["version"] or "").startswith(v + ".")]
        if not rows:
            raise NotFoundError("Specification %s has no version matching %r." % (sid, version))
    return sorted(rows, key=lambda r: version_sort_key(r["version"]), reverse=True)


def _spec_header(r: sqlite3.Row) -> dict:
    """Identity fields of a specification row."""
    return {"spec_id": r["spec_id"], "doc_type": r["doc_type"], "title": r["title"],
            "version": r["version"], "release": r["release"]}


def get_specification(index: Union[Index, str, Path], spec_id: str, version: Optional[str] = None,
                      include_clauses: bool = True) -> dict:
    """Specification details; latest numeric version if ``version`` is None (a prefix such
    as "17.5" matches "17.5.0"). Raises NotFoundError."""
    idx, opened = _ensure(index)
    try:
        rows = _spec_rows(idx, spec_id, version)
        r = rows[0]
        pk = r["spec_pk"]
        types = {t: n for t, n in idx.conn.execute(
            "SELECT content_type, COUNT(*) FROM records WHERE spec_pk=? GROUP BY content_type", (pk,))}
        srcs = [x[0] for x in idx.conn.execute(
            "SELECT DISTINCT src.rel_path FROM records r JOIN sources src ON src.source_id=r.source_id "
            "WHERE r.spec_pk=? ORDER BY 1", (pk,))]
        out = _spec_header(r)
        out.update({
            "available_versions": [{"version": x["version"], "release": x["release"]} for x in rows],
            "spec_id_origin": r["spec_id_origin"],
            "record_count": sum(types.values()),
            "content_types": types,
            "has_full_text": types.get("full_text", 0) > 0,
            "sources": srcs,
        })
        if include_clauses:
            cl = idx.conn.execute(
                "SELECT c.clause_id, c.title, c.depth, "
                "(SELECT COUNT(*) FROM records r WHERE r.clause_pk=c.clause_pk) AS n "
                "FROM clauses c WHERE c.spec_pk=?", (pk,)).fetchall()
            cl = sorted(cl, key=lambda x: clause_sort_key(x["clause_id"]))
            out["clause_count"] = len(cl)
            out["clauses"] = [{"clause_id": x["clause_id"], "title": x["title"], "depth": x["depth"],
                               "record_count": x["n"]} for x in cl]
        else:
            out["clause_count"] = idx.conn.execute(
                "SELECT COUNT(*) FROM clauses WHERE spec_pk=?", (pk,)).fetchone()[0]
        return out
    finally:
        if opened:
            idx.close()


def find_specifications(index: Union[Index, str, Path], spec_id: Optional[str] = None,
                        title: Optional[str] = None, version: Optional[str] = None,
                        release: Optional[Any] = None, limit: int = 50) -> List[dict]:
    """List specification versions. ``spec_id`` matches exactly, or as a prefix when it ends
    with '*' or is partial (e.g. "23.5" -> 23.501, 23.502 ...). ``title`` is a substring match."""
    idx, opened = _ensure(index)
    try:
        where, params = [], []  # type: List[str], List[Any]
        if spec_id:
            raw = str(spec_id).strip()
            sid, _ = normalize_spec_id(raw)
            if _SPEC_RE.fullmatch(raw.replace("TS", "").replace("TR", "").strip()) or sid != raw:
                where.append("spec_id = ?")
                params.append(sid)
            else:
                where.append("spec_id LIKE ? ESCAPE '\\'")
                params.append(_like_escape(raw.rstrip("*")) + "%")
        if title:
            where.append("title LIKE ? ESCAPE '\\'")
            params.append("%" + _like_escape(title) + "%")
        if version:
            where.append("(version = ? OR version LIKE ? ESCAPE '\\')")
            params += [str(version), _like_escape(str(version)) + ".%"]
        if release is not None:
            where.append("release = ?")
            params.append(normalize_release(release))
        sql = ("SELECT s.*, (SELECT COUNT(*) FROM clauses c WHERE c.spec_pk=s.spec_pk) AS nclauses, "
               "(SELECT COUNT(*) FROM records r WHERE r.spec_pk=s.spec_pk) AS nrecords "
               "FROM specifications s" + (" WHERE " + " AND ".join(where) if where else ""))
        rows = idx.conn.execute(sql, params).fetchall()
        rows = sorted(rows, key=lambda r: (r["spec_id"] or "~", [-x for x in version_sort_key(r["version"])[1]]))
        out = []
        for r in rows[: max(1, int(limit))]:
            d = _spec_header(r)
            d.update({"clause_count": r["nclauses"], "record_count": r["nrecords"]})
            out.append(d)
        return out
    finally:
        if opened:
            idx.close()


def find_clauses(index: Union[Index, str, Path], spec_id: Optional[str] = None,
                 clause_id: Optional[str] = None, title: Optional[str] = None,
                 version: Optional[str] = None, release: Optional[Any] = None,
                 clause_prefix: bool = False, limit: int = 50) -> List[dict]:
    """Search clauses by number and/or title substring (case-insensitive for ASCII)."""
    if not (spec_id or clause_id or title):
        raise InvalidQueryError("Provide at least one of spec_id, clause_id or title.")
    idx, opened = _ensure(index)
    try:
        where, params = [], []  # type: List[str], List[Any]
        if spec_id:
            where.append("s.spec_id = ?")
            params.append(normalize_spec_id(spec_id)[0])
        if clause_id:
            cid = clean_clause_id(clause_id)
            if clause_prefix:
                where.append("(c.clause_id = ? OR c.clause_id LIKE ? ESCAPE '\\')")
                params += [cid, _like_escape(cid or "") + ".%"]
            else:
                where.append("c.clause_id = ?")
                params.append(cid)
        if title:
            where.append("c.title LIKE ? ESCAPE '\\'")
            params.append("%" + _like_escape(title) + "%")
        if version:
            where.append("(s.version = ? OR s.version LIKE ? ESCAPE '\\')")
            params += [str(version), _like_escape(str(version)) + ".%"]
        if release is not None:
            where.append("s.release = ?")
            params.append(normalize_release(release))
        sql = ("SELECT s.spec_id, s.doc_type, s.title AS spec_title, s.version, s.release,"
               " c.clause_id, c.title AS clause_title, c.depth,"
               " (SELECT COUNT(*) FROM records r WHERE r.clause_pk=c.clause_pk) AS n"
               " FROM clauses c JOIN specifications s ON s.spec_pk=c.spec_pk WHERE " + " AND ".join(where))
        rows = idx.conn.execute(sql, params).fetchall()
        rows = sorted(rows, key=lambda r: (r["spec_id"] or "~", clause_sort_key(r["clause_id"])))
        return [{"spec_id": r["spec_id"], "doc_type": r["doc_type"], "spec_title": r["spec_title"],
                 "version": r["version"], "release": r["release"], "clause_id": r["clause_id"],
                 "clause_title": r["clause_title"], "depth": r["depth"], "record_count": r["n"]}
                for r in rows[: max(1, int(limit))]]
    finally:
        if opened:
            idx.close()


_TYPE_ORDER = {t: i for i, t in enumerate(
    ("clause_heading", "normative_requirement", "clause_summary", "full_text", "source_text",
     "terminology", "other_text", "spec_summary", "technical_summary_item", "patent_relevance",
     "classification", "reference", "change_history", "metadata"))}


def get_clause(index: Union[Index, str, Path], spec_id: str, clause_id: str,
               version: Optional[str] = None, include_text: bool = True) -> dict:
    """All stored evidence for one clause. With ``version=None`` the newest version that
    contains the clause is used. Raises NotFoundError."""
    cid = clean_clause_id(clause_id)
    if not cid:
        raise InvalidQueryError("clause_id is empty.")
    idx, opened = _ensure(index)
    try:
        rows = _spec_rows(idx, spec_id, version)
        for r in rows:
            c = idx.conn.execute("SELECT * FROM clauses WHERE spec_pk=? AND clause_id=?",
                                 (r["spec_pk"], cid)).fetchone()
            if c is None:
                continue
            recs = idx.conn.execute(
                "SELECT r.rec_pk, r.record_id, r.content_type, r.origin, r.label, r.ordinal, r.field, r.page,"
                " d.document_id, src.rel_path "
                "FROM records r JOIN sources src ON src.source_id=r.source_id "
                "LEFT JOIN documents d ON d.document_pk=r.document_pk WHERE r.clause_pk=?",
                (c["clause_pk"],)).fetchall()
            recs = sorted(recs, key=lambda x: (_TYPE_ORDER.get(x["content_type"], 99), x["rec_pk"]))
            records = []
            for x in recs:
                text = None
                if include_text:
                    t = idx.conn.execute("SELECT text FROM records_fts WHERE rowid=?", (x["rec_pk"],)).fetchone()
                    text = t[0] if t else None
                records.append({"record_id": x["record_id"], "content_type": x["content_type"],
                                "origin": x["origin"], "label": x["label"], "ordinal": x["ordinal"],
                                "field": x["field"], "page": x["page"], "document_id": x["document_id"],
                                "source_path": x["rel_path"], "text": text})
            kids = idx.conn.execute(
                "SELECT clause_id, title FROM clauses WHERE spec_pk=? AND clause_id LIKE ? ESCAPE '\\'",
                (r["spec_pk"], _like_escape(cid) + ".%")).fetchall()
            depth = cid.count(".") + 1
            kids = sorted((k for k in kids if k["clause_id"].count(".") == depth),
                          key=lambda k: clause_sort_key(k["clause_id"]))
            out = _spec_header(r)
            out.update({"clause_id": c["clause_id"], "clause_title": c["title"], "depth": c["depth"],
                        "records": records,
                        "subclauses": [{"clause_id": k["clause_id"], "title": k["title"]} for k in kids]})
            return out
        raise NotFoundError("Clause %s not found in specification %s%s."
                            % (cid, spec_id, (" version " + str(version)) if version else ""))
    finally:
        if opened:
            idx.close()


# ------------------------------------------------------------------ documents / history
_DOC_COLS = ("document_id", "title", "document_number", "etsi_number", "standard_name", "archive_file",
             "source_folder", "document_type", "series", "series_name", "etsi_series", "working_group",
             "publication_date", "version_code", "technology_domain", "subdomain", "text_file_name",
             "structured_file_stem", "page_count", "character_count", "declared_clause_count",
             "extraction_quality", "extraction_confidence", "validation_status", "llm_model", "archive_file_date")


def _document_row(idx: Index, document_id: str) -> sqlite3.Row:
    """One document row joined with its specification and metadata provenance; NotFoundError when
    absent.
    """
    did = str(document_id or "").strip()
    if not did:
        raise InvalidQueryError("document_id is empty.")
    row = idx.conn.execute(
        "SELECT d.*, s.spec_id, s.doc_type, s.title AS spec_title, s.version, s.release, s.spec_id_origin,"
        " s.version_origin, s.release_origin FROM documents d JOIN specifications s ON s.spec_pk=d.spec_pk "
        "WHERE lower(d.document_id) = lower(?)", (did,)).fetchone()
    if row is None:
        raise NotFoundError("Document %r not found. Try find_documents()." % document_id)
    return row


def get_document(index: Union[Index, str, Path], document_id: str, include_history: bool = True,
                 include_clauses: bool = False) -> dict:
    """One structured document by its ``identity.document_id``: identity, how its specification id /
    version / release were obtained (``*_origin``), linked text files, row counts and (optionally)
    references, change history and version lists exactly as the data states them. Raises NotFoundError."""
    idx, opened = _ensure(index)
    try:
        r = _document_row(idx, document_id)
        pk = r["document_pk"]
        out: Dict[str, Any] = {k: r[k] for k in _DOC_COLS}
        out.update({"spec_id": r["spec_id"], "doc_type": r["doc_type"], "version": r["version"], "release": r["release"],
                    "spec_id_origin": r["spec_id_origin"], "version_origin": r["version_origin"],
                    "release_origin": r["release_origin"]})
        out["rows_by_content_type"] = {t: n for t, n in idx.conn.execute(
            "SELECT content_type, COUNT(*) FROM records WHERE document_pk=? GROUP BY 1", (pk,))}
        out["structured_source"] = idx.conn.execute(
            "SELECT rel_path FROM sources WHERE source_id=?", (r["source_id"],)).fetchone()[0]
        out["text_sources"] = [{"path": p, "link_method": m} for p, m in idx.conn.execute(
            "SELECT DISTINCT src.rel_path, src.link_method FROM records rc JOIN sources src ON src.source_id=rc.source_id "
            "WHERE rc.document_pk=? AND src.kind='text' ORDER BY 1", (pk,))]
        out["has_full_text"] = bool(out["text_sources"])
        if include_history:
            q = idx.conn.execute
            out["references"] = [dict(x) for x in q(
                "SELECT ordinal, ref_label, org, ref_type, ref_spec, ref_spec_id, title, raw FROM spec_references "
                "WHERE document_pk=? ORDER BY ordinal", (pk,))]
            for kind in ("change_history", "changes_in_this_version"):
                out[kind] = [dict(x) for x in q(
                    "SELECT ordinal, cr, rev, cat, subject, meeting, tdoc, date_text, old_version, new_version "
                    "FROM change_entries WHERE document_pk=? AND kind=? ORDER BY ordinal", (pk, kind))]
            for kind in ("versions_in_archive", "versions_in_dataset"):
                out[kind] = [dict(x) for x in q(
                    "SELECT ordinal, listed_document_id, file, version, release, publication_date FROM version_entries "
                    "WHERE document_pk=? AND kind=? ORDER BY ordinal", (pk, kind))]
            d = q("SELECT compared_to_version, added_clauses, modified_clauses, removed_clauses, unchanged_clause_count "
                  "FROM version_deltas WHERE document_pk=?", (pk,)).fetchone()
            out["version_delta"] = None if d is None else {
                "compared_to_version": d[0], "added_clauses": json.loads(d[1]) if d[1] else None,
                "modified_clauses": json.loads(d[2]) if d[2] else None,
                "removed_clauses": json.loads(d[3]) if d[3] else None, "unchanged_clause_count": d[4]}
        if include_clauses:
            out["clauses"] = [{"clause_id": c, "title": t, "page": pg, "content_type": ct, "field": fl}
                              for c, t, pg, ct, fl in idx.conn.execute(
                "SELECT c.clause_id, c.title, rc.page, rc.content_type, rc.field FROM records rc "
                "JOIN clauses c ON c.clause_pk=rc.clause_pk WHERE rc.document_pk=? AND rc.origin='source_field' "
                "ORDER BY rc.rec_pk", (pk,))]
        return out
    finally:
        if opened:
            idx.close()


def find_documents(index: Union[Index, str, Path], spec_id: Optional[str] = None, title: Optional[str] = None,
                   version: Optional[str] = None, release: Optional[Any] = None,
                   technology_domain: Optional[str] = None, document_id: Optional[str] = None,
                   limit: int = 50) -> List[dict]:
    """List structured documents. ``document_id`` and ``title`` are substring matches."""
    idx, opened = _ensure(index)
    try:
        where, params = [], []  # type: List[str], List[Any]
        if spec_id:
            where.append("s.spec_id = ?")
            params.append(normalize_spec_id(spec_id)[0])
        if title:
            where.append("d.title LIKE ? ESCAPE '\\'")
            params.append("%" + _like_escape(title) + "%")
        if document_id:
            where.append("d.document_id LIKE ? ESCAPE '\\'")
            params.append("%" + _like_escape(document_id) + "%")
        if version:
            where.append("(s.version = ? OR s.version LIKE ? ESCAPE '\\')")
            params += [str(version), _like_escape(str(version)) + ".%"]
        if release is not None:
            where.append("s.release = ?")
            params.append(normalize_release(release))
        if technology_domain:
            where.append("lower(d.technology_domain) = lower(?)")
            params.append(technology_domain)
        sql = ("SELECT d.document_id, d.title, d.technology_domain, d.document_type, s.spec_id, s.version, s.release,"
               " (SELECT COUNT(*) FROM records r WHERE r.document_pk=d.document_pk) AS nrows,"
               " EXISTS (SELECT 1 FROM records r WHERE r.document_pk=d.document_pk AND r.content_type='full_text') AS ft"
               " FROM documents d JOIN specifications s ON s.spec_pk=d.spec_pk"
               + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY s.spec_id IS NULL, s.spec_id, d.document_id LIMIT ?")
        rows = idx.conn.execute(sql, params + [max(1, int(limit))]).fetchall()
        return [{"document_id": r["document_id"], "title": r["title"], "technology_domain": r["technology_domain"],
                 "document_type": r["document_type"], "spec_id": r["spec_id"], "version": r["version"],
                 "release": r["release"], "record_count": r["nrows"], "has_full_text": bool(r["ft"])} for r in rows]
    finally:
        if opened:
            idx.close()


def get_references(index: Union[Index, str, Path], document_id: Optional[str] = None,
                   spec_id: Optional[str] = None, version: Optional[str] = None,
                   cited_spec: Optional[str] = None, limit: int = 200) -> List[dict]:
    """Specification references (``spec_history.references``), verbatim.
    * ``document_id`` or ``spec_id`` (+``version``): what those documents cite.
    * ``cited_spec``: which documents cite that specification (matches only references whose cited
      string was exactly a spec number; ``ref_spec_id`` is NULL otherwise, never guessed)."""
    if not (document_id or spec_id or cited_spec):
        raise InvalidQueryError("Provide document_id, spec_id or cited_spec.")
    idx, opened = _ensure(index)
    try:
        where, params = [], []  # type: List[str], List[Any]
        if document_id:
            where.append("lower(d.document_id) = lower(?)")
            params.append(document_id)
        if spec_id:
            where.append("s.spec_id = ?")
            params.append(normalize_spec_id(spec_id)[0])
        if version:
            where.append("(s.version = ? OR s.version LIKE ? ESCAPE '\\')")
            params += [str(version), _like_escape(str(version)) + ".%"]
        if cited_spec:
            where.append("x.ref_spec_id = ?")
            params.append(normalize_spec_id(cited_spec)[0])
        sql = ("SELECT d.document_id, s.spec_id, s.version, s.release, x.ordinal, x.ref_label, x.org, x.ref_type,"
               " x.ref_spec, x.ref_spec_id, x.title, x.raw FROM spec_references x "
               "JOIN documents d ON d.document_pk=x.document_pk JOIN specifications s ON s.spec_pk=d.spec_pk "
               "WHERE " + " AND ".join(where) + " ORDER BY s.spec_id IS NULL, s.spec_id, d.document_id, x.ordinal LIMIT ?")
        return [dict(r) for r in idx.conn.execute(sql, params + [max(1, int(limit))]).fetchall()]
    finally:
        if opened:
            idx.close()


# ------------------------------------------------------------ manifest/report/status
def get_manifest(index_dir: Union[str, Path]) -> dict:
    """manifest.json of an index folder (counts, build info, checksums)."""
    return _read_json(Path(index_dir) / MANIFEST_FILENAME)


def get_build_report(index_dir: Union[str, Path]) -> dict:
    """build_report.json of an index folder (what the builder ingested/skipped)."""
    return _read_json(Path(index_dir) / REPORT_FILENAME)


def get_statistics(index: Union[Index, str, Path]) -> dict:
    """Live counts read from the database (specifications, documents, clauses, records per content
    type, distinct releases...).
    """
    idx, opened = _ensure(index)
    try:
        q = idx.conn.execute

        def one(sql: str) -> Any:
            return q(sql).fetchone()[0]

        def grp(sql: str) -> Dict[str, int]:
            return {str(k): n for k, n in q(sql).fetchall()}

        db = idx.index_dir / DB_FILENAME
        return {
            "specification_versions": one("SELECT COUNT(*) FROM specifications"),
            "distinct_spec_ids": one("SELECT COUNT(DISTINCT spec_id) FROM specifications"),
            "specifications_without_spec_id": one("SELECT COUNT(*) FROM specifications WHERE spec_id IS NULL"),
            "specifications_without_version": one("SELECT COUNT(*) FROM specifications WHERE version IS NULL"),
            "specifications_without_release": one("SELECT COUNT(*) FROM specifications WHERE release IS NULL"),
            "specifications_without_title": one("SELECT COUNT(*) FROM specifications WHERE title IS NULL"),
            "documents": one("SELECT COUNT(*) FROM documents"),
            "documents_with_full_text": one(
                "SELECT COUNT(DISTINCT document_pk) FROM records WHERE content_type='full_text' AND document_pk IS NOT NULL"),
            "text_files_by_link_state": grp(
                "SELECT COALESCE(link_state,'n/a'), COUNT(*) FROM sources WHERE kind='text' GROUP BY 1"),
            "references": one("SELECT COUNT(*) FROM spec_references"),
            "change_entries": one("SELECT COUNT(*) FROM change_entries"),
            "documents_by_technology_domain": grp(
                "SELECT COALESCE(technology_domain,'unknown'), COUNT(*) FROM documents GROUP BY 1 ORDER BY 2 DESC LIMIT 25"),
            "clauses": one("SELECT COUNT(*) FROM clauses"),
            "clauses_without_title": one("SELECT COUNT(*) FROM clauses WHERE title IS NULL"),
            "search_rows": one("SELECT COUNT(*) FROM records"),
            "rows_by_content_type": grp("SELECT content_type, COUNT(*) FROM records GROUP BY 1 ORDER BY 2 DESC"),
            "rows_by_origin": grp("SELECT origin, COUNT(*) FROM records GROUP BY 1"),
            "spec_versions_by_release": grp(
                "SELECT COALESCE(release,'unknown'), COUNT(*) FROM specifications GROUP BY 1 ORDER BY 2 DESC"),
            "source_files_by_kind": grp("SELECT kind, COUNT(*) FROM sources GROUP BY 1"),
            "source_files_by_status": grp("SELECT status, COUNT(*) FROM sources GROUP BY 1"),
            "database_size_bytes": db.stat().st_size if db.exists() else None,
        }
    finally:
        if opened:
            idx.close()


def _sha256(path: Path) -> str:
    """SHA-256 of a file in 1 MiB chunks (checksum verification)."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def get_index_status(index_dir: Union[str, Path], verify_checksum: bool = False) -> dict:
    """Compatibility/health check that needs *only* the index folder. Never raises for an
    unusable index; inspect ``compatible`` and ``problems``. Staleness versus the source
    dataset needs the dataset and is provided by ``python -m tgpp_index status`` (here
    ``stale`` is always None = unknown)."""
    d = Path(index_dir)
    status: Dict[str, Any] = {
        "index_dir": str(d), "compatible": False, "problems": [], "stale": None,
        "schema_version": None, "builder_version": None, "build_timestamp": None,
        "counts": None, "checksum_ok": None, "client_version": __version__,
        "supported_schema_major": SUPPORTED_SCHEMA_MAJOR,
    }
    try:
        idx = open_index(d)
    except RetrievalError as exc:
        status["problems"].append("%s: %s" % (type(exc).__name__, exc))
        return status
    try:
        status["compatible"] = True
        status["schema_version"] = idx.schema_info.get("schema_version")
        try:
            m = get_manifest(d)
            status["builder_version"] = m.get("builder_version")
            status["build_timestamp"] = m.get("build_timestamp")
            status["counts"] = {
                "specifications": m.get("specifications_indexed"),
                "clauses": m.get("clauses_indexed"),
                "search_rows": m.get("search_rows_indexed"),
            }
            live = idx.conn.execute("SELECT COUNT(*) FROM records").fetchone()[0]
            if m.get("search_rows_indexed") not in (None, live):
                status["problems"].append("manifest says %s search rows but database holds %s"
                                          % (m.get("search_rows_indexed"), live))
            if verify_checksum and m.get("database_sha256"):
                ok = _sha256(d / DB_FILENAME) == m["database_sha256"]
                status["checksum_ok"] = ok
                if not ok:
                    status["problems"].append("database checksum differs from manifest")
        except RetrievalError as exc:
            status["problems"].append(str(exc))
        res = idx.conn.execute("PRAGMA quick_check").fetchone()[0]
        if res != "ok":
            status["problems"].append("PRAGMA quick_check: %s" % res)
    finally:
        idx.close()
    if status["problems"]:
        status["compatible"] = status["compatible"] and not any(
            p.startswith("quick_check") or "quick_check" in p for p in status["problems"])
    return status


# ------------------------------------------------------------------------ CLI layer
def _fmt_spec_line(r: dict) -> str:
    """One-line human description of a spec hit for the CLI ('TS 23.501 v17.5.0 Rel-17 - title')."""
    bits = []
    if r.get("spec_id"):
        bits.append("%s %s" % (r.get("doc_type") or "spec", r["spec_id"]))
    else:
        bits.append("spec ?")
    bits.append("v%s" % r["version"] if r.get("version") else "v?")
    bits.append("(Rel-%s)" % r["release"] if r.get("release") else "(Rel ?)")
    line = " ".join(bits)
    if r.get("spec_title") or r.get("title"):
        line += " - " + (r.get("spec_title") or r.get("title"))
    return line


def format_result(res: dict, rank: int, detail: bool = False) -> str:
    """Human-readable multi-line rendering of a search result for the CLI."""
    cl = ""
    if res.get("clause_id") or res.get("clause_title"):
        cl = "clause %s %s" % (res.get("clause_id") or "?", res.get("clause_title") or "")
        cl = cl.strip() + "  |  "
    lines = ["%2d. [score %.3f] %s" % (rank, res["score"], _fmt_spec_line(res)),
             "    %s%s%s" % (cl, res["content_type"], " (derived 'shall/must' sentences)" if res.get("derived_normative") else "")]
    if res.get("label"):
        lines.append("    label: %s" % res["label"])
    where = [x for x in (("field " + res["field"]) if res.get("field") else None,
                         ("page %s" % res["page"]) if res.get("page") is not None else None,
                         ("document " + res["document_id"]) if res.get("document_id") else None) if x]
    if where:
        lines.append("    " + "  |  ".join(where))
    lines.append("    %s" % res["excerpt"])
    if detail and res.get("text"):
        lines.append("    ---- full text ----")
        lines.extend("    " + ln for ln in res["text"].strip().splitlines()[:60])
        lines.append("    -------------------")
    lines.append("    id=%s  source=%s" % (res["record_id"], res["source_path"]))
    return "\n".join(lines)


def _print_json(obj: Any) -> None:
    """Pretty-print JSON (UTF-8, not ASCII-escaped) for --json CLI output."""
    print(json.dumps(obj, indent=2, ensure_ascii=False))


def add_read_commands(sub: argparse._SubParsersAction, parent: argparse.ArgumentParser,
                      include_status: bool = True) -> None:
    """Register the read-only commands on an argparse subparsers object (shared by the
    builder CLI and this module's own CLI)."""

    def filt(p: argparse.ArgumentParser) -> None:
        """Add the shared --spec/--version/--release/--clause/--type... filter options to a CLI sub-
        command.
        """
        p.add_argument("--spec", action="append", help="specification number, e.g. 23.501 (repeatable)")
        p.add_argument("--version", action="append", help="version or prefix, e.g. 17.5.0 or 17.5")
        p.add_argument("--release", action="append", help="release number, e.g. 17 or Rel-17")
        p.add_argument("--clause", help="exact clause id, e.g. 5.2.2")
        p.add_argument("--clause-prefix", help="clause id and all sub-clauses, e.g. 5.2")
        p.add_argument("--type", action="append", dest="ctype", metavar="CONTENT_TYPE",
                       help="restrict content type (repeatable): " + ", ".join(CONTENT_TYPES))
        p.add_argument("--clause-level", action="store_true", help="only rows attached to a clause")
        p.add_argument("--document", action="append", help="restrict to a document id (identity.document_id)")
        p.add_argument("--field", action="append", help="restrict to a source field, e.g. technical_summary.key_features")
        p.add_argument("--domain", action="append", help="restrict to classification.technology_domain")
        p.add_argument("--limit", type=int, default=10, help="max results (default 10)")
        p.add_argument("--offset", type=int, default=0)
        p.add_argument("--detail", action="store_true", help="show full stored text per result")
        p.add_argument("--json", action="store_true", help="machine-readable JSON output")

    s = sub.add_parser("search", parents=[parent], help="keyword search (all terms; use --any / --phrase / --raw)")
    s.add_argument("query", nargs="+")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--any", action="store_true", help="match any term (OR)")
    g.add_argument("--phrase", action="store_true", help="exact phrase")
    g.add_argument("--raw", action="store_true", help="raw FTS5 query syntax")
    filt(s)
    s.set_defaults(func=_cmd_search, normative=False, fixed_type=None)

    s = sub.add_parser("requirements", parents=[parent], help="search normative requirements (shall/must)")
    s.add_argument("query", nargs="+")
    filt(s)
    s.set_defaults(func=_cmd_search, normative=True, any=False, phrase=False, raw=False, fixed_type=None)

    s = sub.add_parser("terms", parents=[parent], help="search technical terminology entries")
    s.add_argument("query", nargs="+")
    filt(s)
    s.set_defaults(func=_cmd_search, normative=False, any=False, phrase=False, raw=False,
                   fixed_type="terminology")

    s = sub.add_parser("specs", parents=[parent], help="find specifications by number/title/version/release")
    s.add_argument("spec", nargs="?", help="number or prefix, e.g. 23.501 or 23.5")
    s.add_argument("--title")
    s.add_argument("--version")
    s.add_argument("--release")
    s.add_argument("--limit", type=int, default=50)
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=_cmd_specs)

    s = sub.add_parser("spec", parents=[parent], help="show one specification (latest version unless --version)")
    s.add_argument("spec")
    s.add_argument("--version")
    s.add_argument("--clauses", action="store_true", help="list all clauses")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=_cmd_spec)

    s = sub.add_parser("documents", parents=[parent], help="list structured documents")
    s.add_argument("spec", nargs="?")
    s.add_argument("--title")
    s.add_argument("--version")
    s.add_argument("--release")
    s.add_argument("--domain")
    s.add_argument("--id", dest="doc_id", help="substring of the document id")
    s.add_argument("--limit", type=int, default=50)
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=_cmd_documents)

    s = sub.add_parser("document", parents=[parent], help="show one document by id (identity, text link, history)")
    s.add_argument("document_id")
    s.add_argument("--no-history", action="store_true")
    s.add_argument("--clauses", action="store_true")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=_cmd_document)

    s = sub.add_parser("references", parents=[parent], help="what a document/spec cites, or who cites a spec")
    s.add_argument("--document")
    s.add_argument("--spec")
    s.add_argument("--version")
    s.add_argument("--cited", help="list documents that cite this specification number")
    s.add_argument("--limit", type=int, default=200)
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=_cmd_references)

    s = sub.add_parser("clauses", parents=[parent], help="find clauses by number and/or title")
    s.add_argument("--spec")
    s.add_argument("--clause")
    s.add_argument("--prefix", action="store_true", help="treat --clause as a prefix (include sub-clauses)")
    s.add_argument("--title")
    s.add_argument("--version")
    s.add_argument("--release")
    s.add_argument("--limit", type=int, default=50)
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=_cmd_clauses)

    s = sub.add_parser("clause", parents=[parent], help="show all stored evidence for one clause")
    s.add_argument("spec")
    s.add_argument("clause")
    s.add_argument("--version")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=_cmd_clause)

    s = sub.add_parser("stats", parents=[parent], help="index statistics")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=_cmd_stats)

    s = sub.add_parser("manifest", parents=[parent], help="print manifest.json")
    s.set_defaults(func=_cmd_manifest)

    s = sub.add_parser("report", parents=[parent], help="summarise build_report.json")
    s.add_argument("--full", action="store_true", help="print the whole report as JSON")
    s.add_argument("--section", choices=["issues", "files", "examples"], help="show one section as JSON")
    s.set_defaults(func=_cmd_report)

    if include_status:
        s = sub.add_parser("status", parents=[parent], help="compatibility / health of the index folder")
        s.add_argument("--verify-checksum", action="store_true")
        s.add_argument("--json", action="store_true")
        s.set_defaults(func=_cmd_status_basic)


def _cmd_search(a: argparse.Namespace) -> int:
    """CLI `search`: full-text search with filters and modes (all/any/phrase/raw)."""
    query = " ".join(a.query)
    mode = "raw" if a.raw else "phrase" if a.phrase else "any" if a.any else "all"
    filters: Dict[str, Any] = {}
    if a.spec:
        filters["spec_id"] = a.spec
    if a.version:
        filters["version"] = a.version
    if a.release:
        filters["release"] = a.release
    types = list(a.ctype or [])
    if a.fixed_type:
        types.append(a.fixed_type)
    if types:
        filters["content_type"] = types
    if a.clause:
        filters["clause_id"] = a.clause
    if a.clause_prefix:
        filters["clause_prefix"] = a.clause_prefix
    if a.clause_level:
        filters["clause_level_only"] = True
    if a.document:
        filters["document_id"] = a.document
    if a.field:
        filters["field"] = a.field
    if a.domain:
        filters["technology_domain"] = a.domain
    with open_index(a.index) as idx:
        res = search(idx, query, filters, a.limit, mode=mode, offset=a.offset,
                     include_text=a.detail, normative=a.normative)
    if a.json:
        _print_json(res)
        return 0
    if not res:
        print("No results for %r%s." % (query, " with the given filters" if filters else ""))
        return 0
    for i, r in enumerate(res, a.offset + 1):
        print(format_result(r, i, a.detail))
        print()
    return 0


def _cmd_documents(a: argparse.Namespace) -> int:
    """CLI `documents`: list documents matching the filters."""
    with open_index(a.index) as idx:
        res = find_documents(idx, a.spec, a.title, a.version, a.release, a.domain, a.doc_id, a.limit)
    if a.json:
        _print_json(res)
    elif not res:
        print("No matching documents.")
    else:
        for r in res:
            print("%-9s v%-9s Rel-%-4s %-4s text=%-3s rows=%-5s %s" % (
                r["spec_id"] or "?", r["version"] or "?", r["release"] or "?", r["document_type"] or "",
                "yes" if r["has_full_text"] else "no", r["record_count"], r["document_id"]))
    return 0


def _cmd_document(a: argparse.Namespace) -> int:
    """CLI `document`: one document with its history (and clauses on request)."""
    with open_index(a.index) as idx:
        r = get_document(idx, a.document_id, include_history=not a.no_history, include_clauses=a.clauses)
    if a.json:
        _print_json(r)
        return 0
    print("document        : %s" % r["document_id"])
    print("specification   : %s v%s Rel-%s" % (r["spec_id"] or "?", r["version"] or "?", r["release"] or "?"))
    print("  spec id from  : %s" % r["spec_id_origin"])
    print("  version from  : %s" % (r["version_origin"] or "-"))
    print("  release from  : %s" % (r["release_origin"] or "-"))
    for k in ("title", "document_type", "technology_domain", "subdomain", "working_group", "archive_file",
              "source_folder", "text_file_name", "page_count", "extraction_quality", "llm_model"):
        if r.get(k) is not None:
            print("%-16s: %s" % (k, r[k]))
    print("structured file : %s" % r["structured_source"])
    print("text files      : %s" % (", ".join("%s (%s)" % (t["path"], t["link_method"]) for t in r["text_sources"]) or "none linked"))
    print("rows by type    : %s" % json.dumps(r["rows_by_content_type"]))
    if "references" in r:
        print("references      : %d, change entries: %d, in-this-version: %d, versions listed: %d/%d" % (
            len(r["references"]), len(r["change_history"]), len(r["changes_in_this_version"]),
            len(r["versions_in_archive"]), len(r["versions_in_dataset"])))
    if a.clauses:
        for c in r["clauses"]:
            print("    %-10s p.%-5s %-16s %s" % (c["clause_id"], c["page"] if c["page"] is not None else "?",
                                                 c["content_type"], c["title"] or ""))
    return 0


def _cmd_references(a: argparse.Namespace) -> int:
    """CLI `references`: specification references made by / to a document."""
    with open_index(a.index) as idx:
        res = get_references(idx, a.document, a.spec, a.version, a.cited, a.limit)
    if a.json:
        _print_json(res)
    elif not res:
        print("No references found.")
    else:
        for r in res:
            print("%s v%s  %s %s %s -> %s %s" % (r["spec_id"] or "?", r["version"] or "?", r["ref_label"] or "",
                                                 r["org"] or "", r["ref_type"] or "", r["ref_spec"] or "(no spec number)",
                                                 r["title"] or r["raw"] or ""))
    return 0


def _cmd_specs(a: argparse.Namespace) -> int:
    """CLI `specs`: list matching specifications."""
    with open_index(a.index) as idx:
        res = find_specifications(idx, a.spec, a.title, a.version, a.release, a.limit)
    if a.json:
        _print_json(res)
    elif not res:
        print("No matching specifications.")
    else:
        for r in res:
            print("%-12s v%-10s Rel-%-4s clauses=%-5s rows=%-6s %s" % (
                r["spec_id"] or "?", r["version"] or "?", r["release"] or "?",
                r["clause_count"], r["record_count"], r["title"] or ""))
    return 0


def _cmd_spec(a: argparse.Namespace) -> int:
    """CLI `spec`: one specification (with its clause list on request)."""
    with open_index(a.index) as idx:
        r = get_specification(idx, a.spec, a.version, include_clauses=a.clauses or a.json)
    if a.json:
        _print_json(r)
        return 0
    print(_fmt_spec_line(r))
    print("  versions in index : %s" % ", ".join(str(v["version"]) for v in r["available_versions"]))
    print("  clauses           : %d" % r["clause_count"])
    print("  rows by type      : %s" % json.dumps(r["content_types"]))
    print("  full text indexed : %s" % ("yes" if r["has_full_text"] else "no"))
    print("  sources           : %s" % ", ".join(r["sources"]))
    if a.clauses:
        for c in r["clauses"]:
            print("    %-12s %s  (%d rows)" % (c["clause_id"], c["title"] or "", c["record_count"]))
    return 0


def _cmd_clauses(a: argparse.Namespace) -> int:
    """CLI `clauses`: list matching clauses."""
    with open_index(a.index) as idx:
        res = find_clauses(idx, a.spec, a.clause, a.title, a.version, a.release, a.prefix, a.limit)
    if a.json:
        _print_json(res)
    elif not res:
        print("No matching clauses.")
    else:
        for r in res:
            print("%-10s v%-9s clause %-10s %s  (%d rows)" % (r["spec_id"] or "?", r["version"] or "?",
                                                              r["clause_id"], r["clause_title"] or "", r["record_count"]))
    return 0


def _cmd_clause(a: argparse.Namespace) -> int:
    """CLI `clause`: one clause with its text."""
    with open_index(a.index) as idx:
        r = get_clause(idx, a.spec, a.clause, a.version)
    if a.json:
        _print_json(r)
        return 0
    print("%s\n  clause %s %s" % (_fmt_spec_line(r), r["clause_id"], r["clause_title"] or ""))
    for rec in r["records"]:
        print("\n  [%s] id=%s source=%s%s" % (rec["content_type"], rec["record_id"], rec["source_path"],
                                             (" term=" + rec["label"]) if rec["label"] else ""))
        for ln in (rec["text"] or "").strip().splitlines()[:40]:
            print("    " + ln)
    if r["subclauses"]:
        print("\n  sub-clauses: " + ", ".join("%s %s" % (k["clause_id"], k["title"] or "") for k in r["subclauses"]))
    return 0


def _cmd_stats(a: argparse.Namespace) -> int:
    """CLI `stats`: database statistics."""
    with open_index(a.index) as idx:
        st = get_statistics(idx)
    if a.json:
        _print_json(st)
        return 0
    for k, v in st.items():
        if isinstance(v, dict):
            print("%s:" % k)
            for kk, vv in v.items():
                print("    %-24s %s" % (kk, vv))
        else:
            print("%-34s %s" % (k, v))
    return 0


def _cmd_manifest(a: argparse.Namespace) -> int:
    """CLI `manifest`: print manifest.json."""
    _print_json(get_manifest(a.index))
    return 0


def _cmd_report(a: argparse.Namespace) -> int:
    """CLI `report`: print the build report (whole, one section, or a summary)."""
    rep = get_build_report(a.index)
    if a.full:
        _print_json(rep)
        return 0
    if a.section:
        key = {"issues": "issue_totals", "files": "files", "examples": "examples"}[a.section]
        _print_json(rep.get(key))
        return 0
    print("Build report (%s, %s)" % (rep.get("build_mode"), rep.get("build_timestamp")))
    for k, v in (rep.get("summary") or {}).items():
        print("  %-28s %s" % (k, v))
    print("Changes this run: %s" % json.dumps(rep.get("changes_this_run")))
    print("Issue totals:")
    for sev, codes in (rep.get("issue_totals") or {}).items():
        for code, n in sorted(codes.items(), key=lambda kv: -kv[1]):
            print("  %-10s %-34s %d" % (sev, code, n))
    files = rep.get("files") or {}
    for k in ("error", "partial", "skipped"):
        lst = files.get(k) or {}
        print("Files %-8s: %s" % (k, lst.get("total", 0)))
    print("Use --section issues|files|examples or --full for details.")
    return 0


def _cmd_status_basic(a: argparse.Namespace) -> int:
    """CLI `status`: compatibility/health of an index folder (optionally verifying checksums)."""
    st = get_index_status(a.index, verify_checksum=a.verify_checksum)
    if a.json:
        _print_json(st)
    else:
        print("compatible : %s" % st["compatible"])
        print("schema     : %s (client supports major %d)" % (st["schema_version"], st["supported_schema_major"]))
        print("built      : %s by builder %s" % (st["build_timestamp"], st["builder_version"]))
        print("counts     : %s" % st["counts"])
        print("staleness  : unknown here (needs the source dataset; use `python -m tgpp_index status`)")
        for p in st["problems"]:
            print("problem    : %s" % p)
    return 0 if st["compatible"] and not st["problems"] else 2


def run_command(args: argparse.Namespace) -> int:
    """Run a parsed read command, converting RetrievalError into a friendly message."""
    try:
        return int(args.func(args))
    except RetrievalError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2


def configure_stdio() -> None:
    """Make stdout/stderr UTF-8 so spec titles print on Windows consoles."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Command-line entry point (`python tgpp_retrieval.py --index retrieval_data <command> ...`);
    returns the exit code.
    """
    configure_stdio()
    parent = argparse.ArgumentParser(add_help=False)
    parent.add_argument("--index", default=None, help="index folder (default: ./retrieval_data)")
    ap = argparse.ArgumentParser(prog="tgpp_retrieval", description="Query a portable 3GPP index folder.")
    ap.add_argument("--version", action="version", version="tgpp_retrieval " + __version__)
    ap.add_argument("--index", dest="global_index", default=None,
                    help="index folder; may be given before or after the command (default: ./retrieval_data)")
    sub = ap.add_subparsers(dest="command", required=True)
    add_read_commands(sub, parent)
    args = ap.parse_args(argv)
    args.index = args.index or args.global_index or "retrieval_data"
    return run_command(args)


if __name__ == "__main__":
    sys.exit(main())
