"""
CPARAS Bulk reference corpus selector (v0.4.4).

Bulk scores every declaration row against a standards corpus (clause entries grouped by specification
family). Until v0.4.3 that corpus was the 54-entry illustrative one in research/standards_corpus.py, so most
declared families were "outside corpus coverage". This module lets Bulk use the REAL corpus built from the
validated 3GPP dataset - ``standards_corpus_3gpp.json`` (schema cparas_standards_corpus/1.0) in
``CPARAS_DATASET_ROOT`` - without changing what any Bulk output column MEANS:

* The unit is unchanged: one StandardClauseEntry = (specification, version, clause, excerpt text).
* Scores stay what they were: heuristic similarity to the best-matching clause, a screening aid, never a
  statement of essentiality. A standards corpus is NOT an ETSI declaration spreadsheet and is never mixed
  with one.
* Version/release strings come straight from the dataset ("v15.3.0", "Rel-15"). Entries whose release the
  dataset could not determine keep release "Unknown"; ``year`` is 0 (unknown) rather than guessed.
* ISLD keeps using research.standards_corpus.STANDARDS_CORPUS (as its fallback) exactly as before - this
  module only changes what *Bulk scoring and the Bulk export* see.

BULK_CORPUS_SOURCE: ``auto`` (default) = (v0.4.5) the SQLite retrieval index (dist/retrieval_data, or CPARAS_RETRIEVAL_DATA) when it is
present and usable, else the dataset corpus when CPARAS_DATASET_ROOT holds a valid
standards_corpus_3gpp.json, otherwise the illustrative corpus (the reason is reported in the run metadata);
``retrieval`` = require the retrieval index; ``dataset`` = require the dataset corpus (error if unusable);
``illustrative`` = never use the dataset.
"""
from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np

from core import gpp_dataset as gd
from research import standards_corpus as _illustrative
from research.standards_corpus import DOMAIN_KEYWORDS, StandardClauseEntry

logger = logging.getLogger(__name__)

DATASET_DISCLAIMER = (
    "Standard/clause/version references come from the validated 3GPP specification dataset "
    "(clause excerpts of the published specifications). Similarity scores are heuristic screening "
    "signals, not a legal opinion on essentiality; verify clause numbers and versions against the "
    "specification text before relying on them for FRAND, licensing or litigation purposes."
)

# Dataset domains use the Initial Categoriser vocabulary; map them onto the keyword lexicon that the
# claim-match score uses. Domains without an entry simply have no keyword boost (never invented).
_DOMAIN_KEYWORD_SOURCES: dict[str, tuple[str, ...]] = {
    "PHY / MAC / Radio": ("PHY", "MAC", "RRM"),
    "Protocols": ("RRC", "NAS"),
    "Architecture": ("Architecture",),
    "Core Network": ("Core Network",),
    "Security": ("Security",),
    "RAN Interface": ("RAN Interface",),
    "Network Management": (),
    "Services": (),
    "UE / Device": (),
    "Testing": (),
}


@dataclass(frozen=True)
class DatasetClauseEntry(StandardClauseEntry):
    """A StandardClauseEntry whose domain keywords come from the dataset-domain alias table."""

    @property
    def keywords(self) -> list[str]:
        """Domain keyword phrases for this clause's technical domain (used by the claim-match lexical
        signal).
        """
        out: list[str] = []
        for key in _DOMAIN_KEYWORD_SOURCES.get(self.domain, (self.domain,)):
            out.extend(DOMAIN_KEYWORDS.get(key, []))
        return list(dict.fromkeys(out))


@dataclass
class BulkCorpus:
    """The clause corpus CPARAS Bulk scores against: entries, where they came from ('dataset',
    'retrieval' index or the built-in 'illustrative' set), the disclaimer shown in exports, a
    content fingerprint (cache key) and build info.
    """
    entries: list
    source: str                       # "dataset" | "illustrative"
    disclaimer: str
    fingerprint: Optional[str] = None
    info: dict = field(default_factory=dict)
    _by_family: dict = field(default_factory=dict, repr=False)

    def __post_init__(self):
        """Index entry positions by specification family."""
        for i, e in enumerate(self.entries):
            self._by_family.setdefault(e.family, []).append(i)

    def family_indices(self, family: str) -> list:
        """Positions of the entries of one specification family."""
        return self._by_family.get(family, [])

    def entries_by_family(self, family: str) -> list:
        """Entries of one specification family."""
        return [self.entries[i] for i in self._by_family.get(family, [])]

    def clause_groups_by_family(self, family: str) -> dict:
        """Entries of a family grouped by clause, each group in version order."""
        groups: dict = {}
        for e in self.entries_by_family(family):
            groups.setdefault(e.clause_group, []).append(e)
        for g in groups.values():
            g.sort(key=lambda e: e.order)
        return groups

    def domain_of_family(self, family: str) -> Optional[str]:
        """Technical domain of a family (from its first entry), or None."""
        idx = self._by_family.get(family)
        return self.entries[idx[0]].domain if idx else None

    def all_families(self) -> list:
        """Sorted list of every family in the corpus."""
        return sorted(self._by_family)


def build_entries_from_corpus_json(data: dict) -> list:
    """cparas_standards_corpus/1.0 -> StandardClauseEntry list. Strict about structure, never invents values."""
    entries = []
    for fam in data.get("families", []):
        if not isinstance(fam, dict):
            continue
        family = str(fam.get("family") or "").strip()
        standard = str(fam.get("standard") or "").strip() or (f"TS {family}" if family else "")
        if not family:
            continue
        domain = str(fam.get("domain") or "Unknown")
        stype = str(fam.get("section_type") or "Clause")
        for cl in fam.get("clauses", []) or []:
            clause = str(cl.get("clause") or "").strip()
            if not clause:
                continue
            title = str(cl.get("title") or f"Clause {clause}")
            for order, v in enumerate(cl.get("versions", []) or []):
                text = str(v.get("text") or "").strip()
                if not text:
                    continue
                version = str(v.get("version") or "Unknown")
                release = str(v.get("release") or "Unknown")
                try:
                    year = int(v.get("year") or 0)
                except (TypeError, ValueError):
                    year = 0
                entries.append(DatasetClauseEntry(
                    standard=standard, family=family, version=version, release=release, year=year, order=order,
                    section_type=stype, clause=clause, clause_title=title, domain=domain, text=text))
    return entries


_lock = threading.Lock()
_cache: dict = {"key": None, "corpus": None}

RETRIEVAL_DISCLAIMER = (
    "Standard/clause/version references come from the SQLite retrieval index built from the 3GPP dataset. "
    "Where the index holds the original specification text, clause excerpts are that text (cut to a fixed length); "
    "otherwise they are the dataset's clause summaries, which are machine-generated and not verified "
    "specification text (the counts are in the run metadata). Similarity scores are heuristic screening signals, "
    "not a legal opinion on essentiality; verify clause numbers and versions against the specification text "
    "before relying on them for FRAND, licensing or litigation purposes."
)


def _corpus_json_cache_path(fp: str) -> Path:
    """On-disk cache file of a corpus built from the retrieval index (keyed by the index fingerprint)."""
    from core.config import settings
    d = Path(settings.CACHE_DIR)
    d.mkdir(parents=True, exist_ok=True)
    return d / f"bulk_corpus_{fp}.corpus.pickle"


def _load_corpus_json_cache(fp: str) -> Optional[dict]:
    """v0.4.7 speed: the clause corpus read from a multi-GB retrieval index takes minutes to build; it is
    stored next to the vector cache (keyed by the index fingerprint + options) and reloaded in a second
    after a backend restart."""
    import pickle
    p = _corpus_json_cache_path(fp)
    if not p.is_file():
        return None
    try:
        with open(p, "rb") as fh:
            data = pickle.load(fh)
        if isinstance(data, dict) and data.get("schema") == "cparas_standards_corpus/1.0":
            return data
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Bulk corpus cache {p.name} unreadable ({e}); rebuilding.")
    return None


def _save_corpus_json_cache(fp: str, data: dict) -> None:
    """Write the built corpus cache atomically (temp file + replace); failures are only logged."""
    import pickle
    p = _corpus_json_cache_path(fp)
    try:
        tmp = p.with_suffix(".tmp")
        with open(tmp, "wb") as fh:
            pickle.dump(data, fh, protocol=pickle.HIGHEST_PROTOCOL)
        os.replace(tmp, p)
    except OSError as e:
        logger.warning(f"Could not write the Bulk corpus cache: {e}")


def _retrieval_corpus(mode: str, refresh: bool) -> Optional["BulkCorpus"]:
    """Corpus from the SQLite retrieval index, or None (``auto`` only) when it is not configured / unusable."""
    from services import sqlite_retrieval as sr
    st = _settings()
    if not sr.is_configured():
        if mode == "retrieval":
            raise gd.DatasetError("not_configured", f"BULK_CORPUS_SOURCE=retrieval but no retrieval index was found in {sr.expected_dir()} (copy the retrieval_data folder there or set CPARAS_RETRIEVAL_DATA)")
        return None
    opts = {"versions": getattr(st, "BULK_RETRIEVAL_VERSIONS", "latest-per-release"),
            "max_depth": int(getattr(st, "BULK_RETRIEVAL_MAX_DEPTH", 3)),
            "max_chars": int(getattr(st, "BULK_RETRIEVAL_MAX_CHARS", 1500)),
            "min_chars": int(getattr(st, "BULK_RETRIEVAL_MIN_CHARS", 40)),
            "max_entries": int(getattr(st, "BULK_RETRIEVAL_MAX_ENTRIES", 60000))}
    try:
        fp = sr.corpus_key(opts)
    except sr.RetrievalIndexError as e:
        if mode == "retrieval":
            raise gd.DatasetError(e.code, e.message)
        logger.warning(f"Bulk corpus: retrieval index unusable ({e.message}); trying the dataset corpus.")
        return None
    with _lock:
        if not refresh and _cache["key"] == ("retrieval", fp) and _cache["corpus"] is not None:
            return _cache["corpus"]
        try:
            data = None if refresh else _load_corpus_json_cache(fp)
            if data is None:
                data = sr.build_corpus_json(**opts)
                _save_corpus_json_cache(fp, data)
            entries = build_entries_from_corpus_json(data)
            if not entries:
                raise gd.DatasetError("corpus_empty", "the retrieval index yielded no usable clause entries "
                                                      "(no specification with a known id and version has clause text)")
        except (sr.RetrievalIndexError, gd.DatasetError) as e:
            if mode == "retrieval":
                raise gd.DatasetError(getattr(e, "code", "retrieval_error"), getattr(e, "message", str(e)))
            logger.warning(f"Bulk corpus: {getattr(e, 'message', e)}; trying the dataset corpus.")
            return None
        sel = data.get("_selection", {})
        facts = sr.info()
        corpus = BulkCorpus(
            entries=entries, source="retrieval", disclaimer=RETRIEVAL_DISCLAIMER, fingerprint=fp,
            info={"source": "retrieval", "root": facts.get("dir"), "entries": len(entries),
                  "families": len({e.family for e in entries}), "schema": data.get("schema"),
                  "index_build_timestamp": facts.get("build_timestamp"), "index_schema_version": facts.get("schema_version"),
                  "full_specification_text_available": facts.get("full_specification_text_available"),
                  "fingerprint": fp, **sel})
        _cache["key"], _cache["corpus"] = ("retrieval", fp), corpus
        logger.info(f"Bulk corpus: {len(entries)} clause entries / {corpus.info['families']} specifications from the "
                    f"retrieval index ({sel.get('entries_from_full_text')} original text, "
                    f"{sel.get('entries_from_clause_summaries')} summaries; "
                    f"{sel.get('entries_omitted_by_max_entries')} omitted by BULK_RETRIEVAL_MAX_ENTRIES)")
        return corpus


def _illustrative_corpus(reason: Optional[str] = None) -> BulkCorpus:
    """The built-in illustrative corpus as a BulkCorpus, with the reason the real one was not used (if
    any).
    """
    info = {"source": "illustrative", "entries": len(_illustrative.STANDARDS_CORPUS)}
    if reason:
        info["fallback_reason"] = reason
    return BulkCorpus(entries=_illustrative.STANDARDS_CORPUS, source="illustrative",
                      disclaimer=_illustrative.CORPUS_DISCLAIMER, info=info)


def _settings():
    """Current settings (lazy import)."""
    from core.config import settings
    return settings


def get_bulk_corpus(*, refresh: bool = False) -> BulkCorpus:
    """The corpus Bulk should score against right now (cached per dataset fingerprint)."""
    st = _settings()
    mode = (getattr(st, "BULK_CORPUS_SOURCE", "auto") or "auto").lower()
    if mode in ("auto", "retrieval"):
        rc = _retrieval_corpus(mode, refresh)
        if rc is not None:
            return rc
    if mode == "retrieval":     # unreachable in practice (raises above) - kept so a bad value never falls through
        raise gd.DatasetError("not_configured", "BULK_CORPUS_SOURCE=retrieval but the retrieval index is not usable")
    root_cfg = getattr(st, "CPARAS_DATASET_ROOT", "") or os.environ.get(gd.ENV_ROOT, "")
    key = (mode, str(root_cfg))
    if mode == "illustrative" or not gd.clean_path_text(root_cfg):
        if mode == "dataset":
            raise gd.DatasetError("not_configured", "BULK_CORPUS_SOURCE=dataset but CPARAS_DATASET_ROOT is not set")
        return _illustrative_corpus(None if mode == "illustrative" else "CPARAS_DATASET_ROOT not set")

    with _lock:
        try:
            root = gd.resolve_root(root_cfg)
            lay = gd.layout(root)
            fp = gd.fingerprint(lay)
        except gd.DatasetError as e:
            if mode == "dataset":
                raise
            logger.warning(f"Bulk corpus: dataset unusable ({e.message}); using the illustrative corpus.")
            return _illustrative_corpus(e.message)
        if not refresh and _cache["key"] == (key, fp) and _cache["corpus"] is not None:
            return _cache["corpus"]
        try:
            data = gd.load_corpus(lay.corpus)
            entries = build_entries_from_corpus_json(data)
            if not entries:
                raise gd.DatasetError("corpus_empty", f"{gd.CORPUS_NAME} contains no usable clause entries")
        except gd.DatasetError as e:
            if mode == "dataset":
                raise
            logger.warning(f"Bulk corpus: {e.message}; using the illustrative corpus.")
            return _illustrative_corpus(e.message)
        corpus = BulkCorpus(
            entries=entries, source="dataset", disclaimer=DATASET_DISCLAIMER, fingerprint=fp,
            info={"source": "dataset", "root": str(root), "entries": len(entries),
                  "families": len({e.family for e in entries}), "schema": data.get("schema"),
                  "generated_at": data.get("generated_at"), "fingerprint": fp})
        _cache["key"], _cache["corpus"] = (key, fp), corpus
        logger.info(f"Bulk corpus: {len(entries)} clause entries / {corpus.info['families']} specifications from {root}")
        return corpus


# module-level accessors with the same names research.standards_corpus exposes (Bulk imports these)
def entries_by_family(family: str) -> list:
    """Module-level shortcut on the active Bulk corpus."""
    return get_bulk_corpus().entries_by_family(family)


def clause_groups_by_family(family: str) -> dict:
    """Module-level shortcut on the active Bulk corpus."""
    return get_bulk_corpus().clause_groups_by_family(family)


def domain_of_family(family: str) -> Optional[str]:
    """Module-level shortcut on the active Bulk corpus."""
    return get_bulk_corpus().domain_of_family(family)


def all_families() -> list:
    """Module-level shortcut on the active Bulk corpus."""
    return get_bulk_corpus().all_families()


# ── embedding cache for the (large) dataset corpus ───────────────────────────────────────────
def _cache_file(corpus: BulkCorpus, model: str) -> Path:
    """On-disk embedding cache file for a corpus + model."""
    from core.config import settings
    safe = "".join(ch if ch.isalnum() else "_" for ch in model)[:60]
    d = Path(settings.CACHE_DIR)
    d.mkdir(parents=True, exist_ok=True)
    return d / f"bulk_corpus_{corpus.fingerprint}_{safe}.npz"


async def embed_corpus_cached(embedder, corpus: BulkCorpus) -> np.ndarray:
    """Embed the corpus for a Bulk run. The dataset corpus has thousands of entries, so its vectors are
    cached on disk per (dataset fingerprint, embedding model) and reused by later runs. The cache is only
    trusted if a live probe embedding is in the same vector space (same mode and dimension); otherwise
    the corpus is re-embedded. Illustrative-corpus runs behave exactly as before (no cache)."""
    texts = [e.embedding_text() for e in corpus.entries]
    if corpus.source not in ("dataset", "retrieval"):
        return await embedder.embed_corpus_for_batched_scoring(texts)
    st = _settings()
    path = _cache_file(corpus, getattr(st, "EMBEDDING_MODEL", "model"))
    if path.exists():
        try:
            z = np.load(path, allow_pickle=False)
            vecs, mode, n = z["vectors"], str(z["mode"]), len(texts)
            if vecs.shape[0] == n and mode == "sentence-transformers":
                probe = await embedder.embed_texts([texts[0]])
                if embedder.mode == "sentence-transformers" and probe.shape[1] == vecs.shape[1]:
                    embedder._fallback_vectorizer = None
                    embedder._fallback_svd = None
                    logger.info(f"Bulk corpus: reused cached embeddings ({n} x {vecs.shape[1]}) from {path.name}")
                    return vecs
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Bulk corpus embedding cache unusable ({e}); re-embedding.")
    vecs = await embedder.embed_corpus_for_batched_scoring(texts)
    if embedder.mode == "sentence-transformers":
        try:
            tmp = path.with_suffix(".tmp.npz")
            np.savez(tmp, vectors=vecs.astype("float32"), mode=np.array("sentence-transformers"))
            os.replace(tmp, path)
        except OSError as e:
            logger.warning(f"Could not write the Bulk corpus embedding cache: {e}")
    return vecs
