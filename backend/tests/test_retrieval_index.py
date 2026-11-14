"""
v0.4.5 - the SQLite / FTS5 retrieval index (tgpp-sqlite-retrieval output) as the 3GPP dataset.

Synthetic fixture only: tests/fixtures/retrieval_data was built by tgpp-sqlite-retrieval from INVENTED text shaped like the
real dataset. These tests prove the wiring and the honesty rules; they say nothing about the real 32,440-document index.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

FIX = Path(__file__).parent / "fixtures" / "retrieval_data"
DOC_2350117 = "23_series_23.501_23501-h50.zip_23501-h50.docx"


def _settings():
    import core.config as c
    return c.settings


@pytest.fixture()
def idx_dir(monkeypatch, tmp_path):
    """A private copy of the fixture index, configured as CPARAS_RETRIEVAL_DATA."""
    d = tmp_path / "retrieval_data"
    shutil.copytree(FIX, d)
    st = _settings()
    monkeypatch.setattr(st, "CPARAS_RETRIEVAL_DATA", str(d), raising=False)
    monkeypatch.setattr(st, "RETRIEVAL_SOURCE", "auto", raising=False)
    monkeypatch.delenv("CPARAS_RETRIEVAL_DATA", raising=False)
    from services import sqlite_retrieval as sr
    sr._info_cache.update({"key": None, "info": None})
    return d


@pytest.fixture()
def unconfigured(monkeypatch):
    st = _settings()
    monkeypatch.setattr(st, "CPARAS_RETRIEVAL_DATA", "", raising=False)
    monkeypatch.delenv("CPARAS_RETRIEVAL_DATA", raising=False)
    from services import sqlite_retrieval as sr
    sr._info_cache.update({"key": None, "info": None})


def run(coro):
    return asyncio.run(coro)


# ── status / location ─────────────────────────────────────────────────────────────────────────
def test_not_configured_is_a_normal_state(unconfigured):
    from services import sqlite_retrieval as sr
    assert not sr.is_configured() and not sr.available()
    st = sr.status()
    assert st["configured"] is False and st["ok"] is False
    assert st["problems"][0]["code"] == "not_configured"


def test_status_ok_and_facts(idx_dir):
    from services import sqlite_retrieval as sr
    st = sr.status()
    assert st["ok"] and st["configured"]
    assert st["documents"] == 8 and st["search_rows"] > 50 and st["full_text_rows"] > 0
    assert st["full_specification_text_available"] is True
    assert sr.available()


def test_folder_variants(idx_dir, monkeypatch):
    from services import sqlite_retrieval as sr
    parent = idx_dir.parent
    assert sr.resolve_dir(str(parent)) == idx_dir                    # a dist folder holding retrieval_data
    assert sr.resolve_dir(str(idx_dir / "search_index.sqlite")) == idx_dir   # the database file
    assert sr.resolve_dir(f'"{idx_dir}"') == idx_dir                 # quotes tolerated
    with pytest.raises(sr.RetrievalIndexError):
        sr.resolve_dir(str(parent / "nowhere"))


def test_unusable_index_is_reported_not_raised(idx_dir):
    from services import sqlite_retrieval as sr
    (idx_dir / "schema_version.json").unlink()
    sr._info_cache.update({"key": None, "info": None})
    st = sr.status()
    assert st["ok"] is False and st["problems"][0]["code"] == "incompatible"
    assert not sr.available()


def test_incompatible_major_version_refused(idx_dir):
    import json
    from services import sqlite_retrieval as sr
    p = idx_dir / "schema_version.json"
    d = json.loads(p.read_text())
    d["schema_version"] = "2.0.0"
    p.write_text(json.dumps(d))
    sr._info_cache.update({"key": None, "info": None})
    assert sr.status()["ok"] is False


# ── candidates and evidence ───────────────────────────────────────────────────────────────────
def test_candidates_carry_provenance_and_no_absolute_paths(idx_dir):
    from services import sqlite_retrieval as sr
    cands = sr.find_candidates("UE shall register with the network registration procedure", 20)
    assert cands
    c0 = cands[0]
    for k in ("spec_id", "version", "clause_id", "content_type", "origin", "document_id", "source_path", "text"):
        assert k in c0
    for c in cands:
        assert c["content_type"] in sr.EVIDENCE_TYPES
        assert not os.path.isabs(c["source_path"]) and ":" not in c["source_path"]
        assert c["source_path"].startswith(("structured/", "text/"))


def test_query_keywords_prefers_distinctive_words():
    from services import sqlite_retrieval as sr
    kws = sr.query_keywords("A method comprising the registration of the UE, the registration timer and the AMF", 5)
    assert "registration" in kws and "the" not in kws and "method" not in kws


def test_similarity_search_semantic_rerank(idx_dir, monkeypatch):
    from services import sqlite_retrieval as sr

    async def fake(query, texts):          # deterministic "embedder": share of query words present in the text
        qw = set(query.lower().split())
        return [min(1.0, len(qw & set(t.lower().split())) / max(len(qw), 1) + 0.4) * 0.9 for t in texts]
    monkeypatch.setattr(sr, "_semantic_scores", fake)
    hits = run(sr.similarity_search("registration management", top_k=5, min_score=0.4))
    assert hits and all(h["metadata"]["score_kind"] == "semantic_rerank" for h in hits)
    assert [h["score"] for h in hits] == sorted((h["score"] for h in hits), reverse=True)
    m = hits[0]["metadata"]
    assert m["source_type"] == "3gpp_spec" and m["specification_number"] and m["version"]
    assert m["etsi_document_id"] is None and m["source_url"] is None
    assert run(sr.similarity_search("registration management", top_k=5, min_score=0.99)) == []   # threshold applies


def test_similarity_search_lexical_fallback_is_capped(idx_dir, monkeypatch):
    from services import sqlite_retrieval as sr

    async def none(query, texts):
        return None
    monkeypatch.setattr(sr, "_semantic_scores", none)
    hits = run(sr.similarity_search("registration management UE", top_k=5, min_score=0.35))
    assert hits
    for h in hits:
        assert h["metadata"]["score_kind"] == "lexical"
        assert h["score"] <= sr.LEXICAL_SCORE_CAP < 0.35


def test_one_hit_per_clause(idx_dir, monkeypatch):
    from services import sqlite_retrieval as sr

    async def flat(query, texts):
        return [0.9] * len(texts)
    monkeypatch.setattr(sr, "_semantic_scores", flat)
    hits = run(sr.similarity_search("registration", top_k=20, min_score=0.1))
    keys = [(h["metadata"]["specification_number"], h["metadata"]["version"], h["metadata"]["clause"] or h["metadata"]["record_id"])
            for h in hits]
    assert len(keys) == len(set(keys))


# ── retrieval_service switch ──────────────────────────────────────────────────────────────────
class _BoomStore:
    async def similarity_search(self, *a, **k):
        raise AssertionError("the vector store must not be used")


class _FakeStore:
    async def similarity_search(self, query, top_k=8, filter_source=None, min_score=0.5):
        return [{"content": "vector hit", "score": 0.9,
                 "metadata": {"source_type": filter_source, "etsi_document_id": 7, "standard_name": "TS 99.999"}}]


@pytest.fixture()
def fake_embed(monkeypatch):
    from services import sqlite_retrieval as sr

    async def fake(query, texts):
        return [0.8] * len(texts)
    monkeypatch.setattr(sr, "_semantic_scores", fake)


def test_retrieve_auto_uses_index_and_not_the_vector_store(idx_dir, fake_embed, monkeypatch):
    from services import retrieval_service as rs
    monkeypatch.setattr(rs, "get_vector_store", lambda: _BoomStore())
    ev = run(rs.retrieve("UE shall register with the network", top_k=4))
    assert ev
    e = ev[0]
    assert e.source_type == "3gpp_spec" and e.score_kind == "semantic_rerank" and e.is_semantic
    assert e.standard.endswith(e.specification_number)
    assert e.content_type and e.dataset_document_id is not None or e.origin == "text_corpus"
    assert e.semantic_score == pytest.approx(0.8) and e.combined_score >= e.semantic_score


def test_retrieve_vector_mode_ignores_the_index(idx_dir, monkeypatch):
    from services import retrieval_service as rs
    monkeypatch.setattr(_settings(), "RETRIEVAL_SOURCE", "vector", raising=False)
    monkeypatch.setattr(rs, "get_vector_store", lambda: _FakeStore())
    ev = run(rs.retrieve("registration", top_k=3, source_types=("etsi_standard",)))
    assert [e.content for e in ev] == ["vector hit"]


def test_retrieve_both_merges(idx_dir, fake_embed, monkeypatch):
    from services import retrieval_service as rs
    monkeypatch.setattr(_settings(), "RETRIEVAL_SOURCE", "both", raising=False)
    monkeypatch.setattr(rs, "get_vector_store", lambda: _FakeStore())
    ev = run(rs.retrieve("registration", top_k=10, source_types=("etsi_standard", "3gpp_spec")))
    kinds = {e.score_kind for e in ev}
    assert "semantic" in kinds and "semantic_rerank" in kinds


def test_retrieve_auto_without_index_is_the_old_behaviour(unconfigured, monkeypatch):
    from services import retrieval_service as rs
    monkeypatch.setattr(rs, "get_vector_store", lambda: _FakeStore())
    ev = run(rs.retrieve("registration", top_k=3, source_types=("etsi_standard",)))
    assert [e.content for e in ev] == ["vector hit"] and ev[0].score_kind == "semantic"


def test_retrieve_user_document_source_types_do_not_touch_the_index(idx_dir, fake_embed, monkeypatch):
    from services import retrieval_service as rs
    monkeypatch.setattr(rs, "get_vector_store", lambda: _FakeStore())
    monkeypatch.setattr(_settings(), "RETRIEVAL_SOURCE", "both", raising=False)
    ev = run(rs.retrieve("registration", top_k=5, source_types=("user_document",)))
    assert {e.score_kind for e in ev} == {"semantic"}


def test_sqlite_mode_with_broken_index_returns_nothing_not_vector(idx_dir, monkeypatch):
    from services import retrieval_service as rs
    monkeypatch.setattr(_settings(), "RETRIEVAL_SOURCE", "sqlite", raising=False)
    monkeypatch.setattr(rs, "get_vector_store", lambda: _BoomStore())
    (idx_dir / "search_index.sqlite").unlink()
    from services import sqlite_retrieval as sr
    sr._info_cache.update({"key": None, "info": None})
    assert run(rs.retrieve("registration", top_k=3)) == []


# ── essentiality bridge ───────────────────────────────────────────────────────────────────────
def test_bridge_ignores_lexical_hits_and_keeps_provenance(idx_dir, monkeypatch):
    from research import etsi_kb_bridge as br
    from services import retrieval_service as rs
    from services import sqlite_retrieval as sr

    async def none(query, texts):
        return None
    monkeypatch.setattr(sr, "_semantic_scores", none)
    assert run(br.lookup_etsi_kb_evidence(["registration management UE network"])) == [None]

    async def fake(query, texts):
        return [0.8] * len(texts)
    monkeypatch.setattr(sr, "_semantic_scores", fake)
    out = run(br.lookup_etsi_kb_evidence(["registration management UE network"]))
    ev = out[0]
    assert ev is not None and ev.specification_number != "Unknown" and ev.similarity == pytest.approx(0.8)
    assert ev.content_type in sr.EVIDENCE_TYPES and ev.etsi_document_id is None


# ── API ───────────────────────────────────────────────────────────────────────────────────────
@pytest.fixture()
def client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from api.routes.shared import retrieval_index
    app = FastAPI()
    app.include_router(retrieval_index.router, prefix="/api/retrieval-index")
    return TestClient(app)


def test_api_not_configured(unconfigured, client):
    assert client.get("/api/retrieval-index/status").json()["configured"] is False
    r = client.get("/api/retrieval-index/search", params={"q": "registration"})
    assert r.status_code == 404 and r.json()["detail"]["code"] == "not_configured"


def test_api_endpoints(idx_dir, fake_embed, client):
    base = "/api/retrieval-index"
    assert client.get(f"{base}/status").json()["ok"] is True
    r = client.get(f"{base}/search", params={"q": "registration procedure", "spec": "23.501", "limit": 3})
    assert r.status_code == 200 and r.json()["count"] >= 1
    assert all(x["spec_id"] == "23.501" for x in r.json()["results"])
    assert client.get(f"{base}/search", params={"q": "x y", "mode": "bogus"}).status_code == 400
    r = client.get(f"{base}/evidence", params={"q": "UE registration with the network"})
    assert r.status_code == 200 and r.json()["results"][0]["metadata"]["score_kind"] == "semantic_rerank"
    assert client.get(f"{base}/specs", params={"spec": "23.5"}).json()["results"]
    r = client.get(f"{base}/specs/23.501/clauses", params={"version": "17.5.0", "clause": "5.2.2.1"})
    assert r.status_code == 200 and r.json()["clause_id"] == "5.2.2.1"
    assert client.get(f"{base}/specs/99.999/clauses").status_code == 404
    d = client.get(f"{base}/documents/{DOC_2350117}").json()
    assert d["spec_id"] == "23.501" and d["has_full_text"] and d["version_origin"]
    assert client.get(f"{base}/documents/nope").status_code == 404


def test_api_accepts_no_client_supplied_path(idx_dir, client):
    # the only inputs are query text and filters; a path-like parameter has no effect
    r = client.get("/api/retrieval-index/status", params={"root": "C:/Windows", "path": "C:/Windows"})
    assert r.json()["dir"] == str(idx_dir)


# ── CPARAS Bulk corpus (Platform only) ────────────────────────────────────────────────────────
try:
    import research.bulk_corpus as bulk
except ImportError:          # ISLD has no CPARAS Bulk module
    bulk = None
needs_bulk = pytest.mark.skipif(bulk is None, reason="research.bulk_corpus not present (ISLD)")


@pytest.fixture()
def bulk_mode(idx_dir, monkeypatch):
    st = _settings()
    monkeypatch.setattr(st, "BULK_CORPUS_SOURCE", "auto", raising=False)
    monkeypatch.setattr(st, "CPARAS_DATASET_ROOT", "", raising=False)
    monkeypatch.setattr(st, "BULK_RETRIEVAL_MAX_DEPTH", 4, raising=False)     # fixture texts are short and 5.2.2.1 has depth 4
    monkeypatch.setattr(st, "BULK_RETRIEVAL_MIN_CHARS", 10, raising=False)
    bulk._cache.update({"key": None, "corpus": None})
    return st


@needs_bulk
def test_bulk_corpus_from_index(bulk_mode):
    c = bulk.get_bulk_corpus(refresh=True)
    assert c.source == "retrieval" and c.entries and c.fingerprint
    fams = set(c.all_families())
    assert {"23.501", "24.501", "38.331", "29.500", "36.300"} <= fams
    # the document whose specification id is unknown is never attached to a guessed family
    assert not any("unknown" in f.lower() or f.startswith("00.") for f in fams)
    vs = {(e.family, e.version, e.release) for e in c.entries}
    assert ("23.501", "v17.5.0", "Rel-17") in vs and ("23.501", "v18.1.0", "Rel-18") in vs
    assert ("29.500", "v15.7.0", "Rel-15") in vs and ("29.500", "v15.6.0", "Rel-15") not in vs   # latest per release
    assert all(e.year == 0 for e in c.entries)                                                      # never guessed
    assert c.info["entries_from_full_text"] >= 1 and c.info["entries_from_clause_summaries"] >= 1
    assert "machine-generated" in c.disclaimer


@needs_bulk
def test_bulk_corpus_prefers_original_text_and_orders_versions(bulk_mode):
    c = bulk.get_bulk_corpus(refresh=True)
    groups = c.clause_groups_by_family("23.501")
    g = groups["23.501-5.2.2.1"]
    assert [e.version for e in g] == ["v17.5.0", "v18.1.0"] and [e.order for e in g] == [0, 1]
    assert "The UE shall register with the network" in g[0].text              # full_text of the linked text file
    assert "Release 18 general registration" in g[1].text                     # no text file -> the dataset's summary
    assert all(e.standard.startswith("TS ") for e in c.entries if e.family == "23.501")


@needs_bulk
def test_bulk_corpus_options(bulk_mode, monkeypatch):
    st = bulk_mode
    monkeypatch.setattr(st, "BULK_RETRIEVAL_VERSIONS", "all", raising=False)
    c = bulk.get_bulk_corpus(refresh=True)
    assert ("29.500", "v15.6.0", "Rel-15") in {(e.family, e.version, e.release) for e in c.entries}
    monkeypatch.setattr(st, "BULK_RETRIEVAL_VERSIONS", "latest", raising=False)
    c = bulk.get_bulk_corpus(refresh=True)
    assert not any(e.version == "v17.5.0" for e in c.entries if e.family == "23.501")
    monkeypatch.setattr(st, "BULK_RETRIEVAL_VERSIONS", "latest-per-release", raising=False)
    monkeypatch.setattr(st, "BULK_RETRIEVAL_MAX_ENTRIES", 3, raising=False)
    c = bulk.get_bulk_corpus(refresh=True)
    assert len(c.entries) == 3 and c.info["entries_omitted_by_max_entries"] > 0       # reported, not silent
    monkeypatch.setattr(st, "BULK_RETRIEVAL_MAX_ENTRIES", 60000, raising=False)
    monkeypatch.setattr(st, "BULK_RETRIEVAL_MAX_CHARS", 50, raising=False)
    c = bulk.get_bulk_corpus(refresh=True)
    assert all(len(e.text) <= 50 for e in c.entries)


@needs_bulk
def test_bulk_corpus_modes(bulk_mode, monkeypatch, unconfigured):
    st = bulk_mode
    c = bulk.get_bulk_corpus(refresh=True)                      # nothing configured, auto -> illustrative (as v0.4.4.1)
    assert c.source == "illustrative"
    monkeypatch.setattr(st, "BULK_CORPUS_SOURCE", "retrieval", raising=False)
    from core import gpp_dataset as gd
    with pytest.raises(gd.DatasetError):
        bulk.get_bulk_corpus(refresh=True)


@needs_bulk
def test_bulk_illustrative_source_setting_never_uses_the_index(bulk_mode, monkeypatch):
    monkeypatch.setattr(bulk_mode, "BULK_CORPUS_SOURCE", "illustrative", raising=False)
    assert bulk.get_bulk_corpus(refresh=True).source == "illustrative"


@needs_bulk
def test_bulk_corpus_cache_follows_the_index(bulk_mode):
    a = bulk.get_bulk_corpus()
    b = bulk.get_bulk_corpus()
    assert a is b
    bulk._cache.update({"key": None, "corpus": None})
    assert bulk.get_bulk_corpus().fingerprint == a.fingerprint


@needs_bulk
def test_bulk_embedding_cache_is_used_for_the_index_corpus(bulk_mode, tmp_path, monkeypatch):
    import numpy as np
    c = bulk.get_bulk_corpus(refresh=True)
    monkeypatch.setattr(_settings(), "CACHE_DIR", str(tmp_path), raising=False)

    class Emb:
        mode = "sentence-transformers"
        calls = 0
        _fallback_vectorizer = None
        _fallback_svd = None

        async def embed_corpus_for_batched_scoring(self, texts):
            Emb.calls += 1
            return np.ones((len(texts), 4), dtype="float32")

        async def embed_texts(self, texts):
            return np.ones((len(texts), 4), dtype="float32")
    e = Emb()
    run(bulk.embed_corpus_cached(e, c))
    run(bulk.embed_corpus_cached(e, c))
    assert Emb.calls == 1                                        # second run reused the on-disk vectors


# ── v0.4.6: default folder <project>/dist/retrieval_data ─────────────────────────────────────────
@pytest.fixture()
def default_folder(monkeypatch, tmp_path):
    """Pretend the project's dist/retrieval_data is tmp_path/dist/retrieval_data; nothing set in .env."""
    import core.config as c
    d = tmp_path / "dist" / "retrieval_data"
    d.mkdir(parents=True)
    monkeypatch.setattr(c, "DEFAULT_RETRIEVAL_DATA_DIR", d)
    monkeypatch.setattr(c.settings, "CPARAS_RETRIEVAL_DATA", "", raising=False)
    monkeypatch.delenv("CPARAS_RETRIEVAL_DATA", raising=False)
    from services import sqlite_retrieval as sr
    sr._info_cache.update({"key": None, "info": None})
    return d


def _copy_fixture(dest: Path):
    for f in FIX.iterdir():
        shutil.copy2(f, dest / f.name)


def test_default_folder_empty_tells_where_to_copy(default_folder):
    from services import sqlite_retrieval as sr
    assert not sr.is_configured() and not sr.available()
    st = sr.status()
    assert st["configured"] is False and st["using_default_folder"] is True
    assert st["expected_dir"] == str(default_folder)
    assert str(default_folder) in st["problems"][0]["message"]
    names = {f["name"]: f for f in st["files"]}
    assert names["search_index.sqlite"]["required"] and not names["search_index.sqlite"]["present"]


def test_default_folder_is_used_without_any_setting(default_folder):
    from services import sqlite_retrieval as sr
    _copy_fixture(default_folder)
    assert sr.is_configured() and sr.available()
    st = sr.status()
    assert st["ok"] and st["dir"] == str(default_folder) and st["found_dir"] == str(default_folder)
    assert st["counts_from"] == "manifest" and st["documents"] == 8
    assert all(f["present"] for f in st["files"] if f["required"])


def test_files_pasted_into_dist_itself_are_found(default_folder):
    from services import sqlite_retrieval as sr
    _copy_fixture(default_folder.parent)
    assert sr.resolve_dir() == default_folder.parent and sr.available()


def test_explicit_setting_wins_over_default(default_folder, tmp_path, monkeypatch):
    from services import sqlite_retrieval as sr
    import core.config as c
    other = tmp_path / "elsewhere"
    shutil.copytree(FIX, other)
    monkeypatch.setattr(c.settings, "CPARAS_RETRIEVAL_DATA", str(other), raising=False)
    assert sr.resolve_dir() == other
    assert sr.status()["using_default_folder"] is False


def test_incomplete_copy_is_reported(default_folder):
    from services import sqlite_retrieval as sr
    _copy_fixture(default_folder)
    db = default_folder / "search_index.sqlite"
    with open(db, "r+b") as fh:
        fh.truncate(db.stat().st_size // 2)
    st = sr.status()
    assert st["configured"] and not st["ok"]
    assert st["problems"][0]["code"] == "incomplete_copy"
