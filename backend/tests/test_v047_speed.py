"""
v0.4.7 speed work: embedder fairness + cache, fast retrieval-index candidates, Bulk shortlist scoring with
an on-disk per-clause vector cache. These tests pin behaviour (same answers, fewer model calls), not timings.
"""
from __future__ import annotations

import asyncio
import hashlib
import shutil
import threading
import time
from pathlib import Path

import numpy as np
import pytest

from tests.embedding_fakes import FakeModel, words

FIXTURE = Path(__file__).parent / "fixtures" / "retrieval_data"


def run(coro):
    return asyncio.run(coro)


class BowModel:
    """Bag-of-words 'embedding model': a text is the normalised sum of fixed random word vectors, so
    cosine similarity tracks shared vocabulary - meaningful enough to compare shortlist vs exhaustive."""
    device = "cpu"
    max_seq_length = 8192

    def __init__(self, dim=48, delay=0.0):
        self.dim, self.delay = dim, delay
        self.tokenizer = None
        self.encoded = 0
        self.order: list[str] = []
        self._lock = threading.Lock()

    def get_sentence_embedding_dimension(self):
        return self.dim

    def _w(self, w):
        seed = int.from_bytes(hashlib.sha256(w.encode()).digest()[:8], "little")
        return np.random.default_rng(seed).standard_normal(self.dim)

    def encode(self, texts, batch_size=32, normalize_embeddings=True, show_progress_bar=False, convert_to_numpy=True):
        if self.delay:
            time.sleep(self.delay)
        with self._lock:
            self.encoded += len(texts)
            self.order.append(texts[0] if texts else "")
        out = []
        for t in texts:
            v = sum((self._w(w) for w in t.lower().split()), np.zeros(self.dim))
            n = np.linalg.norm(v) or 1.0
            out.append(v / n)
        return np.asarray(out, dtype="float32")


@pytest.fixture
def embedder():
    from rag import vector_store as vs
    saved = (vs._embedder._model, vs.LocalEmbedder._load_error)

    def install(model):
        vs.LocalEmbedder._load_error = None
        vs._embedder._model = model
        vs._embedder._load_lock = None
        vs.LocalEmbedder._vec_cache.clear()
        vs.LocalEmbedder._vec_cache_model_id = None
        return model
    yield install
    vs._embedder._model, vs.LocalEmbedder._load_error = saved
    vs.LocalEmbedder._vec_cache.clear()


# ── embedder ──────────────────────────────────────────────────────────────────────────────────
def test_repeated_texts_are_served_from_the_vector_cache(embedder):
    from rag import vector_store as vs
    m = embedder(FakeModel())
    texts = [words(20, start=i) for i in range(10)]
    a = run(vs._embedder.embed(texts))
    n1 = sum(c["batch"] for c in m.encode_calls)
    b = run(vs._embedder.embed(texts + texts[:3]))
    assert np.allclose(np.asarray(a), np.asarray(b)[:10])
    assert sum(c["batch"] for c in m.encode_calls) == n1          # nothing re-encoded


def test_indexer_calls_with_a_token_limit_bypass_the_cache(embedder):
    from rag import vector_store as vs
    m = embedder(FakeModel())
    run(vs._embedder.embed([words(30)], max_seq_length=16))
    run(vs._embedder.embed([words(30)], max_seq_length=16))
    assert len(m.encode_calls) == 2


def test_small_call_is_served_between_slices_of_a_large_one(embedder):
    from rag import vector_store as vs
    m = embedder(BowModel(delay=0.05))
    big = [f"corpus clause number {i}" for i in range(vs.LocalEmbedder._SLICE * 6)]
    done = {}

    async def go():
        async def large():
            await vs._embedder.embed_array(big)
            done["large"] = time.time()

        async def small():
            await asyncio.sleep(0.08)               # the large call is already running
            await vs._embedder.embed(["interactive retrieval query"])
            done["small"] = time.time()
        await asyncio.gather(large(), small())
    run(go())
    assert done["small"] < done["large"]
    # the query was encoded long before the last slice of the large call
    assert m.order.index("interactive retrieval query") < len(m.order) - 2


# ── retrieval index ───────────────────────────────────────────────────────────────────────────
@pytest.fixture
def idx(tmp_path, monkeypatch):
    from services import sqlite_retrieval as sr
    d = tmp_path / "retrieval_data"
    shutil.copytree(FIXTURE, d)
    monkeypatch.setenv("CPARAS_RETRIEVAL_DATA", str(d))
    from core.config import settings
    monkeypatch.setattr(settings, "CPARAS_RETRIEVAL_DATA", str(d), raising=False)
    sr._info_cache.update({"key": None, "info": None})
    sr._df_cache.clear()
    yield sr
    sr._info_cache.update({"key": None, "info": None})


def test_fast_candidates_match_the_standard_search(idx):
    sr = idx
    q = "UE shall register with the network registration procedure"
    fast = sr.find_candidates(q, 20)
    slow = sr.find_candidates(q, 20, filters={"content_type": list(sr.EVIDENCE_TYPES)})
    assert fast and {c["record_id"] for c in fast} == {c["record_id"] for c in slow}
    assert [c["record_id"] for c in fast[:3]] == [c["record_id"] for c in slow[:3]]
    for c in fast:
        assert c["content_type"] in sr.EVIDENCE_TYPES and c["text"]
        assert "technology_domain" in c and c["keywords"]


def test_very_common_words_are_not_searched(idx, monkeypatch):
    sr = idx
    dfs = {"network": 10**6, "procedure": 10**6, "registration": 40, "amf": 7, "zzz": 0}
    monkeypatch.setattr(sr, "_term_df", lambda conn, key, t, cap: min(dfs.get(t, 0), cap))
    terms = sr.select_terms(None, None, list(dfs), total_rows=10**7)
    assert terms == ["amf", "registration"]
    # only common words: keep a few rather than nothing
    assert sr.select_terms(None, None, ["network", "procedure"], total_rows=10**7) == ["network", "procedure"]


# ── Bulk shortlist scoring ────────────────────────────────────────────────────────────────────
VOCAB = ("handover measurement beam failure recovery paging registration authentication ciphering bearer "
         "session slice timing advance preamble harq codebook antenna scheduling grant carrier aggregation "
         "secondary cell group mobility idle inactive resume suspend policy charging qos flow header "
         "compression segmentation reestablishment reconfiguration capability").split()


def _corpus(n_fam=12, per_fam=40, seed=3):
    from research.bulk_corpus import BulkCorpus, DatasetClauseEntry
    rng = np.random.default_rng(seed)
    entries = []
    for f in range(n_fam):
        fam = f"38.{300 + f}"
        topic = list(rng.choice(VOCAB, 5, replace=False))
        for c in range(per_fam):
            body = " ".join(list(rng.choice(topic, 6)) + list(rng.choice(VOCAB, 6)))
            entries.append(DatasetClauseEntry(standard=f"TS {fam}", family=fam, version="v17.0.0", release="Rel-17",
                                              year=0, order=0, section_type="Clause", clause=f"5.{c}",
                                              clause_title=" ".join(rng.choice(topic, 2)), domain="Protocols",
                                              text=body))
    return BulkCorpus(entries=entries, source="retrieval", disclaimer="test", fingerprint="feedc0de00000001")


def _records(corpus, n=30, seed=5):
    rng = np.random.default_rng(seed)
    recs = []
    for i in range(n):
        e = corpus.entries[int(rng.integers(len(corpus.entries)))]
        recs.append({"_source_row_index": i, "title": e.text.split()[:5] and " ".join(e.text.split()[:5]),
                     "standard_raw": e.standard, "spec_number": e.family, "spec_version": "17.0.0",
                     "illustrative_part": e.clause_title, "explicit_disclosure": False})
    return recs


@pytest.fixture
def bulk_env(monkeypatch, tmp_path, embedder):
    import core.config
    from etsi_bulk import scoring
    from research import bulk_fast
    from rag import vector_store as vs
    corpus = _corpus()
    # other test modules re-import core.config, so several Settings objects can be alive: patch them all
    objs = {id(o): o for o in (core.config.settings, scoring.settings, vs.settings)}.values()

    class Multi:
        def __getattr__(self, k):
            return getattr(scoring.settings, k)

        def __setattr__(self, k, v):
            for o in objs:
                monkeypatch.setattr(o, k, v, raising=False)
    settings = Multi()
    settings.CACHE_DIR = tmp_path
    settings.BULK_EXHAUSTIVE_MAX_ENTRIES = 50
    settings.BULK_CALIBRATION_SAMPLE = 64
    settings.BULK_CORPUS_PRECOMPUTE = "false"
    settings.BULK_SCORING_STRATEGY = "auto"
    monkeypatch.setattr(scoring, "get_bulk_corpus", lambda: corpus)
    bulk_fast._vc_registry.clear()
    bulk_fast._lex_cache.clear()
    model = embedder(BowModel())
    yield corpus, model, settings
    bulk_fast._vc_registry.clear()
    bulk_fast._lex_cache.clear()


def _score(records):
    from etsi_bulk.scoring import score_records
    return run(score_records(records))


def test_large_corpus_is_scored_from_a_shortlist_without_embedding_everything(bulk_env):
    corpus, model, _ = bulk_env
    rows, meta = _score(_records(corpus))
    assert meta["scoring_strategy"] == "shortlist"
    assert meta["failed"] == 0 and len(rows) == 30
    assert all(r["matched_clause"] and r["semantic_similarity"] is not None for r in rows)
    assert 0 < meta["corpus_vectors_cached"] < len(corpus.entries)
    first = model.encoded
    rows2, _ = _score(_records(corpus))                       # same rows again: no clause re-embedded
    assert model.encoded - first <= 30
    assert [r["matched_clause"] for r in rows2] == [r["matched_clause"] for r in rows]


def test_shortlist_agrees_with_exhaustive_and_cache_completion_switches_strategy(bulk_env, monkeypatch):
    corpus, model, settings = bulk_env
    recs = _records(corpus, n=40)
    short, m1 = _score(recs)
    settings.BULK_SCORING_STRATEGY = "exhaustive"
    full, m2 = _score(recs)
    assert m1["scoring_strategy"] == "shortlist" and m2["scoring_strategy"] == "exhaustive"
    assert m2["corpus_vectors_cached"] == len(corpus.entries)
    # the shortlist is an approximation until every clause has a vector: same specification for (almost)
    # every row, and its best clause close to the true best
    same_spec = sum(a["matched_standard"] == b["matched_standard"] for a, b in zip(short, full))
    assert same_spec >= 0.9 * len(recs)
    gap = np.mean([b["semantic_similarity"] - a["semantic_similarity"] for a, b in zip(short, full)])
    assert 0 <= gap < 0.05
    # every vector is now on disk: auto mode scores exhaustively from the cache
    settings.BULK_SCORING_STRATEGY = "auto"
    again, m3 = _score(recs)
    assert m3["scoring_strategy"] == "exhaustive"
    assert [r["matched_clause"] for r in again] == [r["matched_clause"] for r in full]


def test_vector_cache_survives_a_restart(bulk_env):
    corpus, model, settings = bulk_env
    from research import bulk_fast
    from rag.vector_store import LocalEmbedder
    _score(_records(corpus))
    n = bulk_fast.vector_cache_for(corpus, settings.EMBEDDING_MODEL, LocalEmbedder.precision,
                                   settings.BULK_CORPUS_EMBED_MAX_TOKENS).n_done
    bulk_fast._vc_registry.clear()                             # "restart"
    vc = bulk_fast.vector_cache_for(corpus, settings.EMBEDDING_MODEL, LocalEmbedder.precision,
                                    settings.BULK_CORPUS_EMBED_MAX_TOKENS)
    assert vc.n_done == n and vc.dim == model.dim


# ── ISLD deep enrichment: one LLM answer per distinct input, remembered on disk ─────────────────────
def test_enrichment_asks_once_per_distinct_patent_and_caches(monkeypatch, tmp_path):
    import json as _json
    import re as _re
    from api.routes.cparas import analysis as an
    monkeypatch.setattr(an.settings, "CACHE_DIR", tmp_path, raising=False)
    monkeypatch.setattr(an.settings, "ISLD_ENRICH_CACHE", True, raising=False)
    monkeypatch.setattr(an, "_enrich_mem", None)
    calls = []

    class FakeLLM:
        async def generate(self, prompt, **kw):
            ns = [int(n) for n in _re.findall(r"n=(\d+)", prompt)]
            calls.append(ns)
            return _json.dumps({"patents": [{"n": n, "domains": [f"D{n}"], "strength": "High",
                                             "strength_confidence": 0.7} for n in ns]})

    def disc(n, title, std):
        return {"disclosure_number": n, "explicit_data": {"title": title, "standards": [std]}}
    family = [disc(1, "Conditional handover", "TS 38.331"), disc(2, "Conditional  HANDOVER", "TS 38.331"),
              disc(3, "Conditional handover", "TS 38.331"), disc(4, "Beam failure recovery", "TS 38.213")]
    run(an._deep_enrich_disclosures(family, FakeLLM(), "x.csv", job_id=None, model="m"))
    assert sorted(n for c in calls for n in c) == [1, 4]                 # siblings 2 and 3 not re-asked
    for d in family:
        assert d["sep_analysis"]["likely_sep_strength"] == "High"
        assert d["standards_analysis"]["technical_domains"]
    assert family[1]["standards_analysis"]["technical_domains"] == family[0]["standards_analysis"]["technical_domains"]

    calls.clear()
    monkeypatch.setattr(an, "_enrich_mem", None)                         # a backend restart
    again = [disc(7, "Conditional handover", "TS 38.331"), disc(8, "Beam failure recovery", "TS 38.213")]
    run(an._deep_enrich_disclosures(again, FakeLLM(), "y.csv", job_id=None, model="m"))
    assert calls == []                                                   # answered from the cache
    assert again[0]["sep_analysis"]["likely_sep_strength"] == "High"
    run(an._deep_enrich_disclosures([disc(9, "Conditional handover", "TS 38.331")], FakeLLM(), "z.csv",
                                    job_id=None, model="other-model"))
    assert calls == [[9]]                                                # cache is per model
