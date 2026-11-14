"""
SQLite / FTS5 retrieval index as a dataset source  (v0.4.5)
============================================================

The ``retrieval_data`` folder produced by *tgpp-sqlite-retrieval* (``search_index.sqlite`` +
``manifest.json`` + ``schema_version.json`` + ``build_report.json``) can be used as the 3GPP dataset
of this application, instead of (or next to) the ``etsi_dataset.jsonl`` build + vector import:

* **Evidence retrieval** (``services/retrieval_service.retrieve``): SQLite FTS5 finds *candidate* clause
  rows with BM25; the application's own embedder (the one the vector store already uses) then re-scores
  each candidate against the query. The resulting ``score`` is a cosine similarity, exactly like a vector
  store hit, so every existing threshold keeps its meaning. If the embedder cannot be loaded the hits are
  returned with ``score_kind="lexical"`` and a score capped below 0.35, so they can be shown as context
  but are never mistaken for semantic evidence (the essentiality bridge ignores them).
* **CPARAS Bulk corpus** (``research/bulk_corpus``): clause entries are read from the index.

Nothing is imported or copied: the folder is opened READ-ONLY, in place. No absolute path is ever
accepted from a client; the folder is ``CPARAS_RETRIEVAL_DATA`` (env or backend/.env) or, when that is empty,
the project's own ``dist/retrieval_data`` folder (v0.4.6) - copy the builder's output there.

Honesty notes
-------------
* Rows of type ``clause_summary``, ``technical_summary_item`` ... come from model-generated summaries in
  the dataset (see the index's ``documents.llm_model``); only ``full_text`` rows are original specification
  text. Every hit carries ``content_type`` / ``origin`` so callers can tell.
* A hit is a *candidate* found by words: a semantically related clause that shares no word with the query
  is not found. This is a lexical-first design; it does not replace a vector index for recall.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import sqlite3
import threading
from collections import Counter, defaultdict
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

from services import tgpp_retrieval as tr

logger = logging.getLogger(__name__)

ENV_ROOT = "CPARAS_RETRIEVAL_DATA"
DB_NAME = tr.DB_FILENAME
#: Row types that carry clause-level evidence (documents' metadata, references, change history and
#: classification rows are never used as standards evidence).
EVIDENCE_TYPES = ("full_text", "clause_summary", "normative_requirement", "source_text", "technical_summary_item")
#: Lexical-only hits are capped below the smallest semantic threshold used anywhere in the app (0.35).
LEXICAL_SCORE_CAP = 0.34
SOURCE_TYPE = "3gpp_spec"

_STOPWORDS = {
    "the", "a", "an", "of", "and", "or", "to", "for", "in", "on", "is", "are", "with", "by", "at", "as", "be",
    "this", "that", "such", "said", "from", "which", "wherein", "comprising", "comprises", "claim", "claims",
    "method", "system", "apparatus", "device", "first", "second", "one", "least", "based", "step", "steps",
}


class RetrievalIndexError(Exception):
    """Configuration / compatibility problem with the retrieval index (message is user-readable)."""

    def __init__(self, code: str, message: str):
        """`code` is stable for the UI/API ('not_configured', 'not_found', 'incompatible', 'empty',
        ...); `message` tells the user what to do.
        """
        super().__init__(message)
        self.code = code
        self.message = message

    def to_dict(self) -> dict:
        """API form {code, message}."""
        return {"code": self.code, "message": self.message}


# ── location ───────────────────────────────────────────────────────────────────────────────────
#: Files of a complete ``retrieval_data`` folder (tgpp-sqlite-retrieval 1.1). Only the first three are needed to
#: open the index; the others are reported as missing but never block it.
REQUIRED_FILES = (DB_NAME, tr.SCHEMA_VERSION_FILENAME, tr.MANIFEST_FILENAME)
OPTIONAL_FILES = (tr.REPORT_FILENAME, "README.txt", tr.CLIENT_FILENAME)


def _clean(text: Optional[str]) -> str:
    """Trim a configured path value and drop surrounding quotes (people paste quoted Windows paths into
    .env).
    """
    t = (text or "").strip()
    if len(t) >= 2 and t[0] == t[-1] and t[0] in "\"'":
        t = t[1:-1].strip()
    return t


def configured_value() -> str:
    """The explicit CPARAS_RETRIEVAL_DATA value ('' = use the default folder)."""
    try:
        from core.config import settings
        v = getattr(settings, "CPARAS_RETRIEVAL_DATA", "") or ""
    except Exception:  # noqa: BLE001
        v = ""
    return _clean(v or os.environ.get(ENV_ROOT, ""))


def default_dir() -> Path:
    """<project>/dist/retrieval_data - where the index is expected when CPARAS_RETRIEVAL_DATA is empty."""
    try:
        from core.config import DEFAULT_RETRIEVAL_DATA_DIR
        return Path(DEFAULT_RETRIEVAL_DATA_DIR)
    except Exception:  # noqa: BLE001
        return Path(__file__).resolve().parent.parent.parent / "dist" / "retrieval_data"


def _anchor(raw: str) -> Path:
    """Path from a setting, expanded (~) and anchored to the backend folder when relative."""
    p = Path(raw).expanduser()
    if not p.is_absolute():
        try:
            from core.config import BACKEND_DIR
            p = Path(BACKEND_DIR) / p
        except Exception:  # noqa: BLE001
            pass
    return p


def _find_db_dir(p: Path) -> Optional[Path]:
    """The folder containing search_index.sqlite for a given path - accepts the folder itself, the
    .sqlite file, or a parent holding a retrieval_data/ sub-folder.
    """
    if p.is_file() and p.name == DB_NAME:
        p = p.parent
    for cand in (p, p / "retrieval_data"):
        if (cand / DB_NAME).is_file():
            return cand
    return None


def expected_dir() -> Path:
    """The folder the backend looks in right now (explicit setting, else the default folder)."""
    raw = configured_value()
    return _anchor(raw) if raw else default_dir()


def resolve_dir(value: Optional[str] = None) -> Optional[Path]:
    """The index folder, or None when nothing is configured AND the default folder holds no index.
    Accepts the folder itself, its parent (a ``dist`` folder containing ``retrieval_data``) or the
    ``search_index.sqlite`` file. An explicit value that holds no index raises ``not_found``."""
    raw = _clean(value) if value is not None else configured_value()
    if not raw:
        if value is not None:
            return None
        d = default_dir()
        # the default folder, or its parent `dist` when the files were pasted one level too high
        return _find_db_dir(d) or ((d.parent if (d.parent / DB_NAME).is_file() else None))
    p = _anchor(raw)
    found = _find_db_dir(p)
    if found is not None:
        return found
    raise RetrievalIndexError("not_found", f"No {DB_NAME} found in {p} (CPARAS_RETRIEVAL_DATA must point to the "
                                           f"retrieval_data folder built by tgpp-sqlite-retrieval).")


def is_configured() -> bool:
    """True when CPARAS_RETRIEVAL_DATA is set, or the default folder contains search_index.sqlite."""
    if configured_value():
        return True
    try:
        return resolve_dir() is not None
    except RetrievalIndexError:
        return False


def file_checklist(d: Optional[Path] = None) -> list[dict]:
    """Which files of a retrieval_data folder are present (name, required, present, size_bytes)."""
    d = d or expected_dir()
    out = []
    for name in REQUIRED_FILES + OPTIONAL_FILES:
        f = d / name
        ok = f.is_file()
        out.append({"name": name, "required": name in REQUIRED_FILES, "present": ok,
                    "size_bytes": f.stat().st_size if ok else None})
    return out


_lock = threading.Lock()
_info_cache: dict = {"key": None, "info": None}


def _stat_key(d: Path) -> tuple:
    """Cache key that changes whenever the index database or its manifest is replaced (path, size,
    mtimes).
    """
    st = (d / DB_NAME).stat()
    mt = (d / tr.MANIFEST_FILENAME).stat().st_mtime_ns if (d / tr.MANIFEST_FILENAME).exists() else 0
    return (str(d), st.st_size, st.st_mtime_ns, mt)


def _check_copy_complete(d: Path, manifest: dict) -> None:
    """A 20+ GB database copied by hand may still be in transit (or truncated): compare with the manifest."""
    want = manifest.get("database_size_bytes")
    if not isinstance(want, int) or want <= 0:
        return
    have = (d / DB_NAME).stat().st_size
    if have != want:
        raise RetrievalIndexError(
            "incomplete_copy",
            f"{DB_NAME} is {have:,} bytes but manifest.json says {want:,} bytes. The copy is still running or was "
            f"interrupted - wait for it to finish (or copy the folder again), then refresh.")


def _check_and_summarise(d: Path) -> dict:
    """Open the index with the bundled client (format / schema-major / FTS5 checks) and collect facts.

    Counts come from manifest.json when it has them (instant), so a multi-GB index is never scanned just to
    show a status; the live database is only counted when the manifest lacks a number."""
    manifest: dict = {}
    try:
        manifest = tr.get_manifest(d)
    except tr.RetrievalError:
        pass
    _check_copy_complete(d, manifest)
    try:
        idx = tr.open_index(d)
    except tr.RetrievalError as exc:
        raise RetrievalIndexError("incompatible", str(exc))
    try:
        c = idx.conn

        def count(manifest_value, sql):
            if isinstance(manifest_value, int):
                return manifest_value
            return c.execute(sql).fetchone()[0]

        ctypes = manifest.get("content_types_included") or {}
        return {
            "schema_version": idx.schema_info.get("schema_version") if hasattr(idx, "schema_info") else None,
            "search_rows": count(manifest.get("search_rows_indexed"), "SELECT COUNT(*) FROM records"),
            "documents": count(manifest.get("documents_indexed"), "SELECT COUNT(*) FROM documents"),
            "specification_versions": count(manifest.get("specifications_indexed"), "SELECT COUNT(*) FROM specifications"),
            "distinct_spec_ids": manifest.get("distinct_spec_ids"),
            "clauses": count(manifest.get("clauses_indexed"), "SELECT COUNT(*) FROM clauses"),
            "full_text_rows": count(ctypes.get("full_text") if manifest else None,
                                    "SELECT COUNT(*) FROM records WHERE content_type='full_text'"),
            "content_types": ctypes or None,
            "counts_from": "manifest" if manifest else "database",
            "database_size_bytes": (d / DB_NAME).stat().st_size,
            "build_timestamp": manifest.get("build_timestamp"),
            "builder_version": manifest.get("builder_version"),
            "database_sha256": manifest.get("database_sha256"),
            "source_fingerprint": (manifest.get("source_fingerprint") or {}).get("value"),
            "full_specification_text_available": manifest.get("full_specification_text_available"),
            "text_files_linked_to_documents": manifest.get("text_files_linked_to_documents"),
            "text_files_unlinked": manifest.get("text_files_unlinked"),
        }
    finally:
        idx.close()


def info() -> dict:
    """Validated facts about the configured index (cached until the files change). Raises
    RetrievalIndexError when not configured / unusable."""
    d = resolve_dir()
    if d is None:
        raise RetrievalIndexError("not_configured", f"No retrieval index found. Copy the retrieval_data folder to "
                                                    f"{default_dir()} or set CPARAS_RETRIEVAL_DATA.")
    key = _stat_key(d)
    with _lock:
        if _info_cache["key"] == key and _info_cache["info"] is not None:
            return _info_cache["info"]
        facts = _check_and_summarise(d)
        facts["dir"] = str(d)
        if not facts["search_rows"]:
            raise RetrievalIndexError("empty", "The retrieval index contains no searchable rows.")
        _info_cache["key"], _info_cache["info"] = key, facts
        return facts


def available() -> bool:
    """True when an index is configured AND usable. Never raises."""
    if not is_configured():
        return False
    try:
        info()
        return True
    except RetrievalIndexError as exc:
        logger.warning("Retrieval index unusable: %s", exc.message)
        return False
    except Exception as exc:  # noqa: BLE001
        logger.warning("Retrieval index check failed: %s", exc)
        return False


def _location() -> dict:
    """Where the backend looks, so the UI can tell the user exactly where to paste the files."""
    explicit = configured_value()
    d = expected_dir()
    try:
        found = resolve_dir()
    except RetrievalIndexError:
        found = None
    return {"env_var": ENV_ROOT, "explicit_setting": explicit or None, "using_default_folder": not explicit,
            "default_dir": str(default_dir()), "expected_dir": str(d), "found_dir": str(found) if found else None,
            "files": file_checklist(found or d)}


def status(deep: bool = False) -> dict:
    """Status for the API / CLI. Never raises."""
    loc = _location()
    if not is_configured():
        missing = [f["name"] for f in loc["files"] if f["required"] and not f["present"]]
        return {"configured": False, "ok": False, **loc, "problems": [{"code": "not_configured",
                "message": f"No retrieval index found. Copy the whole retrieval_data folder (built by "
                           f"tgpp-sqlite-retrieval) into {loc['expected_dir']} - missing: {', '.join(missing)}. "
                           f"Or set CPARAS_RETRIEVAL_DATA in backend/.env to another folder."}]}
    try:
        facts = info()
    except RetrievalIndexError as exc:
        return {"configured": True, "ok": False, "root": configured_value() or loc["expected_dir"], **loc,
                "problems": [exc.to_dict()]}
    except Exception as exc:  # noqa: BLE001
        return {"configured": True, "ok": False, "root": configured_value() or loc["expected_dir"], **loc,
                "problems": [{"code": "error", "message": f"{type(exc).__name__}: {exc}"}]}
    out = {"configured": True, "ok": True, "problems": [], **loc, **facts}
    if deep:
        st = tr.get_index_status(facts["dir"], verify_checksum=True)
        out["deep_check"] = st
        out["ok"] = bool(st.get("compatible")) and not st.get("problems")
        out["problems"] = [{"code": "deep_check", "message": p} for p in st.get("problems", [])]
    return out


@contextmanager
def open_index() -> Iterator[tr.Index]:
    """A read-only index handle for the CURRENT thread (sqlite connections are thread-bound)."""
    facts = info()
    idx = tr.open_index(facts["dir"])
    try:
        yield idx
    finally:
        idx.close()


# ── query -> candidates ────────────────────────────────────────────────────────────────────────
def query_keywords(text: str, limit: int = 24) -> list[str]:
    """Distinctive words of a (possibly long) query: frequency first, then length; stop words dropped."""
    toks = re.findall(r"[A-Za-z][A-Za-z0-9\-]{2,}", (text or "").lower())
    cnt = Counter(t for t in toks if t not in _STOPWORDS)
    ranked = sorted(cnt, key=lambda t: (-cnt[t], -len(t), t))
    return ranked[:limit]


def _standard_label(doc_type: Optional[str], spec_id: Optional[str]) -> Optional[str]:
    """Display label for a spec ('TS 23.501'; '3GPP 23.501' when the document type is unknown)."""
    if not spec_id:
        return None
    return f"{doc_type} {spec_id}" if doc_type else f"3GPP {spec_id}"


def find_candidates(query: str, pool: int = 40, *, filters: Optional[dict] = None,
                    evidence_types: tuple = EVIDENCE_TYPES, max_keywords: int = 24) -> list[dict]:
    """BM25 candidates (blocking; call from a worker thread). Each dict has the text and full provenance.

    v0.4.7 speed: on a multi-GB index the old single query (24 OR-ed words, every match joined to five
    tables and snippet()-ed BEFORE sorting) took tens of seconds to minutes per call, and ISLD makes one
    call per patent. Now (see _fast_candidates):
      * words that occur in a large share of all rows ("network", "procedure" ...) are dropped - their BM25
        weight is negligible but they make FTS5 score millions of rows; the rarest words are kept;
      * FTS5 ranks rowids on its own (no joins, no snippet), and only the top rows are joined for text and
        provenance;
      * the database connection is reused per worker thread instead of being re-opened per query.
    Results keep the same shape and BM25 x content-type weighting. Extra ``filters`` use the original path."""
    kws = query_keywords(query, max_keywords)
    if not kws:
        return []
    if not filters:
        try:
            return _fast_candidates(kws, pool, evidence_types)
        except Exception as exc:  # noqa: BLE001 - never worse than the original path
            logger.warning("Fast candidate search failed (%s: %s); using the standard search.", type(exc).__name__, exc)
    flt = dict(filters or {})
    flt["content_type"] = list(evidence_types)
    with open_index() as idx:
        try:
            res = tr.search(idx, " ".join(kws), flt, limit=pool, mode="any", include_text=True, highlight=None)
        except tr.InvalidQueryError as exc:
            logger.warning("Retrieval index query rejected: %s", exc)
            return []
        domains: dict[str, Optional[str]] = {}
        dids = sorted({r["document_id"] for r in res if r.get("document_id")})
        for i in range(0, len(dids), 500):
            part = dids[i:i + 500]
            q = "SELECT document_id, technology_domain FROM documents WHERE document_id IN (%s)" % ",".join("?" * len(part))
            for did, dom in idx.conn.execute(q, part):
                domains[did] = dom
    out = []
    for r in res:
        out.append({**r, "technology_domain": domains.get(r.get("document_id")), "keywords": kws})
    return out


# ── v0.4.7 fast path ───────────────────────────────────────────────────────────────────────────
_tls = threading.local()
_df_cache: dict = {}
_DF_CACHE_MAX = 200_000


def _thread_conn() -> tuple:
    """(stat_key, sqlite3.Connection) for this worker thread, opened once and validated by the bundled
    client; re-opened only when the index files change."""
    facts = info()
    d = Path(facts["dir"])
    key = _stat_key(d)
    held = getattr(_tls, "held", None)
    if held is not None and held[0] == key:
        return held
    if held is not None:
        try:
            held[1].close()
        except Exception:  # noqa: BLE001
            pass
    idx = tr.open_index(d)
    conn = idx.conn
    for pragma in ("PRAGMA cache_size=-65536", "PRAGMA mmap_size=1073741824", "PRAGMA temp_store=MEMORY"):
        try:
            conn.execute(pragma)
        except sqlite3.Error:
            pass
    _tls.held = (key, conn)
    return _tls.held


def _term_df(conn, key, term: str, cap: int) -> int:
    """Rows containing ``term`` (any column), counted up to ``cap``. Cached per index."""
    ck = (key, term, cap)
    v = _df_cache.get(ck)
    if v is not None:
        return v
    try:
        v = conn.execute("SELECT count(*) FROM (SELECT rowid FROM records_fts WHERE records_fts MATCH ? LIMIT ?)",
                         (tr._quote(term), cap)).fetchone()[0]
    except sqlite3.OperationalError:
        v = 0
    if len(_df_cache) > _DF_CACHE_MAX:
        _df_cache.clear()
    _df_cache[ck] = v
    return v


def _settings_int(name: str, default: int) -> int:
    """Integer setting with a fallback (settings import is lazy so this module also works stand-alone)."""
    try:
        from core.config import settings
        return int(getattr(settings, name, default))
    except Exception:  # noqa: BLE001
        return default


def _settings_float(name: str, default: float) -> float:
    """Float setting with a fallback."""
    try:
        from core.config import settings
        return float(getattr(settings, name, default))
    except Exception:  # noqa: BLE001
        return default


def select_terms(conn, key, kws: list[str], total_rows: int) -> list[str]:
    """The query words worth searching for: present in the index, not in a large share of all rows,
    rarest first (they carry the BM25 weight), at most RETRIEVAL_FTS_MAX_TERMS."""
    max_terms = max(1, _settings_int("RETRIEVAL_FTS_MAX_TERMS", 10))
    frac = _settings_float("RETRIEVAL_FTS_COMMON_FRACTION", 0.03)
    cap = max(2000, int(total_rows * frac)) if total_rows else 2000
    dfs = [(t, _term_df(conn, key, t, cap)) for t in kws]
    present = [(t, n) for t, n in dfs if n > 0]
    rare = [(t, n) for t, n in present if n < cap]
    if rare:
        rare.sort(key=lambda tn: (tn[1], -len(tn[0])))
        return [t for t, _ in rare[:max_terms]]
    # every word is very common: keep a few (_fast_candidates then requires all of them first)
    return [t for t, _ in present[:3]]


def _fast_candidates(kws: list[str], pool: int, evidence_types: tuple) -> list[dict]:
    """Fast candidate lookup on a large index: OR-query of the most selective query terms (rare terms
    first, see select_terms) restricted to the evidence content types, returning up to `pool`
    BM25-ranked rows with text cut for re-ranking.
    """
    key, conn = _thread_conn()
    total = int((info() or {}).get("search_rows") or 0)
    terms = select_terms(conn, key, kws, total)
    if not terms:
        return []
    quoted = [tr._quote(t) for t in terms]
    fts_q = " OR ".join(quoted)
    types = list(evidence_types)
    found: dict = {}
    if len(terms) > 1 and all(_term_df(conn, key, t, max(2000, int(total * _settings_float("RETRIEVAL_FTS_COMMON_FRACTION", 0.03))))
                              >= max(2000, int(total * _settings_float("RETRIEVAL_FTS_COMMON_FRACTION", 0.03))) for t in terms):
        # only very common words: rows containing ALL of them are the relevant ones, and far fewer to rank
        n_all = conn.execute("SELECT count(*) FROM (SELECT rowid FROM records_fts WHERE records_fts MATCH ? LIMIT ?)",
                             (" AND ".join(quoted), pool)).fetchone()[0]
        if n_all >= pool:
            fts_q = " AND ".join(quoted)
    for over in (8, 40):
        lim = max(pool * over, 200)
        ranked = conn.execute("SELECT rowid, bm25(records_fts, 1.0, 2.0, 2.0, 1.0) FROM records_fts "
                              "WHERE records_fts MATCH ? ORDER BY 2 LIMIT ?", (fts_q, lim)).fetchall()
        if not ranked:
            return []
        bm = {rid: b for rid, b in ranked}
        rids = [rid for rid, _ in ranked if rid not in found]
        for i in range(0, len(rids), 500):
            part = rids[i:i + 500]
            sql = ("SELECT r.rec_pk, r.record_id, r.content_type, r.origin, r.label, r.field, r.page,"
                   " d.document_id, d.technology_domain, s.spec_id, s.doc_type, s.title AS spec_title, s.version,"
                   " s.release, c.clause_id, c.title AS clause_title, src.rel_path AS source_path, f.text AS full_text"
                   " FROM records r JOIN specifications s ON s.spec_pk = r.spec_pk"
                   " LEFT JOIN clauses c ON c.clause_pk = r.clause_pk"
                   " LEFT JOIN documents d ON d.document_pk = r.document_pk"
                   " JOIN sources src ON src.source_id = r.source_id"
                   " JOIN records_fts f ON f.rowid = r.rec_pk"
                   " WHERE r.rec_pk IN (%s) AND r.content_type IN (%s)"
                   % (",".join("?" * len(part)), ",".join("?" * len(types))))
            for row in conn.execute(sql, part + types):
                score = -float(bm[row["rec_pk"]]) * tr.TYPE_WEIGHTS.get(row["content_type"], 1.0)
                found[row["rec_pk"]] = (score, row)
        if len(found) >= pool or len(ranked) < lim:
            break
    ordered = sorted(found.values(), key=lambda t: (-t[0], t[1]["record_id"]))[:pool]
    out = []
    for score, row in ordered:
        text = row["full_text"]
        r = tr._row_to_result(row, score, (text or "")[:300], text, False)
        r["technology_domain"] = row["technology_domain"]
        r["keywords"] = kws
        out.append(r)
    return out


_embed_warned = False


def _rerank_chars() -> int:
    """How many characters of each candidate are embedded for semantic re-ranking
    (RETRIEVAL_RERANK_MAX_CHARS).
    """
    return _settings_int("RETRIEVAL_RERANK_MAX_CHARS", 1000)


async def _semantic_scores(query: str, texts: list[str]) -> Optional[list[float]]:
    """Cosine similarity of the query against each text with the app's embedder; None if unavailable."""
    global _embed_warned
    try:
        import numpy as np
        from rag.vector_store import _embedder
        arr = await _embedder.embed_array([query] + [t[:_rerank_chars()] for t in texts])
        q, m = arr[0], arr[1:]
        qn = float(np.linalg.norm(q)) or 1.0
        mn = np.linalg.norm(m, axis=1)
        mn[mn == 0] = 1.0
        sims = (m @ q) / (mn * qn)
        return [float(min(1.0, max(0.0, s))) for s in sims]
    except Exception as exc:  # noqa: BLE001
        if not _embed_warned:
            _embed_warned = True
            logger.warning("Retrieval index: embedder unavailable (%s); returning lexical-only hits "
                           "(score_kind='lexical', score capped at %.2f).", exc, LEXICAL_SCORE_CAP)
        return None


def _hit(c: dict, score: float, kind: str) -> dict:
    """Convert an index candidate into the vector-store hit shape used by services/retrieval_service.py
    (content, score, metadata with provenance).
    """
    return {
        "content": c.get("text") or c.get("excerpt") or "",
        "score": score,
        "metadata": {
            "source_type": SOURCE_TYPE,
            "score_kind": kind,
            "etsi_document_id": None,
            "dataset_document_id": c.get("document_id"),
            "record_id": c.get("record_id"),
            "standard_name": _standard_label(c.get("doc_type"), c.get("spec_id")),
            "specification_number": c.get("spec_id"),
            "spec_title": c.get("spec_title"),
            "version": c.get("version"),
            "release": c.get("release"),
            "technology_domain": c.get("technology_domain"),
            "clause": c.get("clause_id"),
            "clause_title": c.get("clause_title"),
            "page": c.get("page"),
            "content_type": c.get("content_type"),
            "origin": c.get("origin"),
            "field": c.get("field"),
            "source_path": c.get("source_path"),     # relative provenance label, never an absolute path
            "source_url": None,
        },
    }


def _dedupe(cands: list[dict]) -> list[dict]:
    """Keep the best-ranked chunk per (spec, version, clause), so one clause cannot fill the whole
    result list.
    """
    # one hit per (spec, version, clause): the best-ranked chunk of a clause represents it
    seen: set = set()
    uniq = []
    for c in cands:
        key = (c.get("spec_id"), c.get("version"), c.get("clause_id") or c.get("record_id"))
        if key in seen:
            continue
        seen.add(key)
        uniq.append(c)
    return uniq


def _finish(uniq: list[dict], sims: Optional[list[float]], top_k: int, min_score: float) -> list[dict]:
    """Final ranked hits: semantic scores (>= min_score) when the embedder re-scored the candidates,
    otherwise lexical-only hits marked score_kind='lexical' (context, never semantic evidence).
    """
    hits: list[dict] = []
    if sims is not None:
        for c, s in zip(uniq, sims):
            if s >= min_score:
                hits.append(_hit(c, round(s, 6), "semantic_rerank"))
        hits.sort(key=lambda h: h["score"], reverse=True)
    else:
        kws = set(c0 for c0 in (uniq[0].get("keywords") or []))
        for c in uniq:
            words = set(re.findall(r"[a-z][a-z0-9\-]{2,}", (c.get("text") or "").lower()))
            overlap = len(kws & words) / max(len(kws), 1)
            hits.append(_hit(c, round(min(LEXICAL_SCORE_CAP, overlap), 6), "lexical"))
        hits.sort(key=lambda h: h["score"], reverse=True)
    return hits[:top_k]


def _pool(pool: Optional[int], top_k: int) -> int:
    """Candidate pool size: RETRIEVAL_SQLITE_POOL, but at least three times top_k."""
    if pool is None:
        pool = _settings_int("RETRIEVAL_SQLITE_POOL", 24)
    return max(pool, top_k * 3)


async def similarity_search(query: str, top_k: int = 8, min_score: float = 0.35, *,
                            pool: Optional[int] = None, rerank: bool = True,
                            filters: Optional[dict] = None) -> list[dict]:
    """Vector-store-shaped hits ({content, score, metadata}) from the retrieval index."""
    if not (query or "").strip():
        return []
    cands = await asyncio.to_thread(find_candidates, query, _pool(pool, top_k), filters=filters)
    if not cands:
        return []
    uniq = _dedupe(cands)
    sims = await _semantic_scores(query, [c.get("text") or "" for c in uniq]) if rerank else None
    return _finish(uniq, sims, top_k, min_score)


async def similarity_search_many(queries: list[str], top_k: int = 8, min_score: float = 0.35, *,
                                 pool: Optional[int] = None, rerank: bool = True) -> list[list[dict]]:
    """similarity_search() for many queries at once (v0.4.7 speed - ISLD looks up every patent).

    The FTS5 look-ups run concurrently in worker threads, and ALL candidates of ALL queries are re-scored
    with ONE embedding call (shared candidates embedded once, the GPU/CPU kept busy with real batches)
    instead of one small embedding call per patent queued one after another."""
    pool = _pool(pool, top_k)
    sem = asyncio.Semaphore(4)

    async def cands_for(q: str):
        """Deduplicated candidates for one query, with the FTS lookup bounded by the shared semaphore
        and run in a worker thread.
        """
        if not (q or "").strip():
            return []
        async with sem:
            return _dedupe(await asyncio.to_thread(find_candidates, q, pool))
    per = await asyncio.gather(*[cands_for(q) for q in queries])
    out: list[list[dict]] = [[] for _ in queries]
    live = [i for i, u in enumerate(per) if u]
    if not live:
        return out
    sims_per: dict = {}
    if rerank:
        if _semantic_scores is _ORIG_SEMANTIC_SCORES:
            try:
                import numpy as np
                from rag.vector_store import _embedder
                lim = _rerank_chars()
                texts: dict[str, int] = {}
                for i in live:
                    for t in [queries[i]] + [(c.get("text") or "")[:lim] for c in per[i]]:
                        texts.setdefault(t, len(texts))
                arr = await _embedder.embed_array(list(texts))
                arr = arr / np.clip(np.linalg.norm(arr, axis=1, keepdims=True), 1e-9, None)
                for i in live:
                    q = arr[texts[queries[i]]]
                    m = arr[[texts[(c.get("text") or "")[:lim]] for c in per[i]]]
                    sims_per[i] = [float(min(1.0, max(0.0, v))) for v in (m @ q)]
            except Exception as exc:  # noqa: BLE001
                global _embed_warned
                if not _embed_warned:
                    _embed_warned = True
                    logger.warning("Retrieval index: embedder unavailable (%s); returning lexical-only hits.", exc)
                sims_per = {i: None for i in live}
        else:                                   # a replaced scorer (tests) is honoured per query
            for i in live:
                sims_per[i] = await _semantic_scores(queries[i], [c.get("text") or "" for c in per[i]])
    for i in live:
        out[i] = _finish(per[i], sims_per.get(i), top_k, min_score)
    return out


_ORIG_SEMANTIC_SCORES = _semantic_scores


# ── CPARAS Bulk clause corpus ──────────────────────────────────────────────────────────────────
def _fmt_version(v: str) -> str:
    """'17.5.0' -> 'v17.5.0' (unchanged when already prefixed)."""
    return v if v.lower().startswith("v") else f"v{v}"


def _fmt_release(r: Optional[str]) -> str:
    """'17' -> 'Rel-17'; 'Unknown' when missing."""
    return f"Rel-{r}" if r else "Unknown"


def _cut(text: str, limit: int) -> str:
    """Whitespace-normalised text cut at a word boundary to at most `limit` characters."""
    t = " ".join(text.split())
    if len(t) <= limit:
        return t
    cut = t[:limit].rsplit(" ", 1)[0]
    return cut or t[:limit]


def build_corpus_json(*, versions: str = "latest-per-release", max_depth: int = 3, max_chars: int = 1500,
                      min_chars: int = 40, max_entries: int = 60000) -> dict:
    """Clause corpus in the ``cparas_standards_corpus/1.0`` shape (see research/bulk_corpus.py), read from
    the index. Blocking. Unknown values stay unknown; nothing is invented:

    * only specifications with a known spec id AND a known version are used;
    * ``versions``: ``latest-per-release`` (default; the newest known version of each release, one
      "Unknown" bucket for versions without a release), ``latest`` or ``all``;
    * a clause's text is the ORIGINAL text rows (``full_text``) when the index has them, otherwise the
      dataset's clause summaries; ``text_basis`` per entry says which;
    * ``max_entries`` bounds memory/embedding time; specifications beyond it are counted in ``info``.
    """
    if versions not in ("latest-per-release", "latest", "all"):
        raise RetrievalIndexError("bad_option", f"versions must be latest-per-release | latest | all, not {versions!r}")
    with open_index() as idx:
        c = idx.conn
        rows = c.execute("SELECT spec_pk, spec_id, doc_type, version, release FROM specifications "
                         "WHERE spec_id IS NOT NULL AND version IS NOT NULL").fetchall()
        total_specs = len(rows)
        groups: dict = defaultdict(list)
        for r in rows:
            groups[(r["spec_id"], r["release"] if versions == "latest-per-release" else
                    (None if versions == "latest" else r["version"]))].append(r)
        chosen = []
        for key in sorted(groups, key=lambda k: (k[0], str(k[1]))):
            best = sorted(groups[key], key=lambda r: tr.version_sort_key(r["version"]))
            chosen.append(best[-1])
        doms: dict = {}
        for spk, dom, n in c.execute("SELECT spec_pk, technology_domain, COUNT(*) FROM documents "
                                     "WHERE technology_domain IS NOT NULL GROUP BY 1, 2 ORDER BY 3"):
            doms[spk] = dom                       # ORDER BY count: the most frequent domain is written last
        fams: dict = {}
        n_entries = n_full = n_summary = 0
        omitted = 0
        # v0.4.7 speed: read row METADATA first (no text), pick the rows each clause actually uses (full text
        # preferred, lowest source, in ordinal order) and fetch text only for those - and only until the
        # excerpt is long enough to be cut at max_chars. v0.4.6 read the text of every chunk of every clause
        # (both content types, every source), i.e. most of a 25 GB file, to keep 1,500 characters per clause.
        def fetch(rec_pks: list) -> dict:
            got: dict = {}
            for k in range(0, len(rec_pks), 900):
                part = rec_pks[k:k + 900]
                for rid, txt in c.execute("SELECT rowid, text FROM records_fts WHERE rowid IN (%s)"
                                          % ",".join("?" * len(part)), part):
                    got[rid] = txt or ""
            return got

        want_raw = max(2 * max_chars, max_chars + 400)
        for i in range(0, len(chosen), 200):
            batch = chosen[i:i + 200]
            pks = [r["spec_pk"] for r in batch]
            meta = {r["spec_pk"]: r for r in batch}
            q = ("SELECT r.rec_pk, r.spec_pk, c.clause_id, c.title, r.source_id, r.content_type, r.ordinal, r.char_count "
                 "FROM records r JOIN clauses c ON c.clause_pk = r.clause_pk "
                 "WHERE r.spec_pk IN (%s) AND c.depth <= ? AND r.content_type IN ('full_text','clause_summary') "
                 "ORDER BY r.spec_pk, c.clause_pk, r.source_id, r.ordinal" % ",".join("?" * len(pks)))
            per: dict = defaultdict(lambda: {"full_text": defaultdict(list), "clause_summary": defaultdict(list), "title": None})
            for r in c.execute(q, pks + [max_depth]):
                slot = per[(r["spec_pk"], r["clause_id"])]
                slot["title"] = slot["title"] or r["title"]
                slot[r["content_type"]][r["source_id"]].append((r["rec_pk"], int(r["char_count"] or 0)))
            plan = []                                   # (spk, cid, title, basis, [rec_pk ...] in order)
            for (spk, cid), slot in sorted(per.items(), key=lambda kv: (kv[0][0], tr.clause_sort_key(kv[0][1]))):
                if slot["full_text"]:
                    rows, basis = slot["full_text"][min(slot["full_text"])], "full_text"
                elif slot["clause_summary"]:
                    rows, basis = slot["clause_summary"][min(slot["clause_summary"])], "clause_summary"
                else:
                    continue
                plan.append((spk, cid, slot["title"], basis, rows))
            if n_entries >= max_entries:                 # cap reached: count the rest, read no text
                omitted += sum(1 for *_x, rows in plan if sum(n for _r, n in rows) >= min_chars)
                continue
            first: list = []
            for _spk, _cid, _t, _b, rows in plan:
                acc = 0
                for rid, n in rows:
                    first.append(rid)
                    acc += n
                    if n > 0 and acc >= want_raw:
                        break
            texts = fetch(first)
            for spk, cid, title, basis, rows in plan:
                parts, acc, used = [], 0, 0
                for rid, n in rows:
                    if rid not in texts:
                        break
                    parts.append(texts[rid])
                    used += 1
                text = _cut(" ".join(parts), max_chars)
                if len(text) < max_chars and used < len(rows):       # whitespace shrank it: read the rest
                    more = fetch([rid for rid, _ in rows[used:]])
                    parts += [more.get(rid, "") for rid, _ in rows[used:]]
                    text = _cut(" ".join(parts), max_chars)
                if len(text) < min_chars:
                    continue
                if n_entries >= max_entries:
                    omitted += 1
                    continue
                sp = meta[spk]
                fam = fams.setdefault(sp["spec_id"], {
                    "family": sp["spec_id"], "standard": _standard_label(sp["doc_type"], sp["spec_id"]),
                    "domain": doms.get(spk) or "Unknown", "section_type": "Clause", "clauses": {}})
                cl = fam["clauses"].setdefault(cid, {"clause": cid, "title": title or f"Clause {cid}", "versions": []})
                cl["versions"].append({"version": _fmt_version(sp["version"]), "release": _fmt_release(sp["release"]),
                                       "year": 0, "text": text, "text_basis": basis, "_k": tr.version_sort_key(sp["version"])})
                n_entries += 1
                if basis == "full_text":
                    n_full += 1
                else:
                    n_summary += 1
    families = []
    for fid in sorted(fams):
        fam = fams[fid]
        clauses = []
        for cid in sorted(fam["clauses"], key=tr.clause_sort_key):
            cl = fam["clauses"][cid]
            cl["versions"].sort(key=lambda v: v["_k"])
            for v in cl["versions"]:
                v.pop("_k", None)
            clauses.append(cl)
        families.append({**{k: v for k, v in fam.items() if k != "clauses"}, "clauses": clauses})
    return {"schema": "cparas_standards_corpus/1.0", "generated_at": None, "families": families,
            "_selection": {"source": "retrieval_index", "versions": versions, "max_depth": max_depth,
                           "max_chars": max_chars, "max_entries": max_entries, "entries": n_entries,
                           "entries_from_full_text": n_full, "entries_from_clause_summaries": n_summary,
                           "entries_omitted_by_max_entries": omitted, "specification_versions_in_index": total_specs,
                           "specification_versions_selected": len(chosen)}}


def corpus_key(options: dict) -> str:
    """Fingerprint of the index content plus the corpus-building options; the on-disk Bulk corpus cache
    is reused only while this key is unchanged.
    """
    facts = info()
    basis = "|".join([facts.get("database_sha256") or "", facts.get("source_fingerprint") or "",
                      str(facts.get("search_rows")), repr(sorted(options.items()))])
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16]
