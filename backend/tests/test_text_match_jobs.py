"""
Tests for CPARAS ISLD Text Matching v0.4:
  - structured uploads are matched one record per query (not re-chunked),
  - continuation rows are merged into their disclosure,
  - standard-only records use the declared spec's scope and are restricted
    to that spec's matches,
  - jobs report progress and support pause / resume / stop.

The vector store is replaced by a stub so no embedding model is needed.
Run: python -m pytest backend/tests/test_text_match_jobs.py -q
"""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from api.routes.cparas import text_match as tm  # noqa: E402

NOKIA_LIKE_CSV = (
    "Disclosure Number,Project or Standard name,Work Item or Standard No.,Version,"
    "Applicant/holder,Application No.,Publication No.,Patent/Application Title,Country of registration\n"
    "1,5G,TS 38.331,,Nokia Technologies Oy,202441060506,,,INDIA\n"
    "2,5G,TS 38.331 TS 38.213,,,202441059993,,,INDIA\n"
    ",,TS 38.214,,,,,,\n"
    "3,5G,TS 38.300,16.0.0,Nokia,US20200001,US11000001B2,Beam failure recovery for a user equipment,UNITED STATES\n"
)

CORPUS = [
    {"content": "RRC connection reconfiguration and handover", "metadata": {"spec": "TS 38.331", "clause": "5.3.5", "source_type": "3gpp_spec_text"}},
    {"content": "Beam failure detection and recovery procedure", "metadata": {"spec": "TS 38.213", "clause": "6", "source_type": "3gpp_spec_text"}},
    {"content": "NR overall description beam management", "metadata": {"spec": "TS 38.300", "clause": "9.2", "source_type": "3gpp_spec_text"}},
    {"content": "MAC random access", "metadata": {"spec": "TS 38.321", "clause": "5.1", "source_type": "3gpp_spec_text"}},
]


class _StubStore:
    delay = 0.0
    calls = 0

    async def get_stats(self):
        return {"indexed_chunks": len(CORPUS), "backend": "stub"}

    async def similarity_search(self, query, top_k=8, filter_source=None, min_score=0.5):
        type(self).calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        q = set(query.lower().split())
        out = []
        for c in CORPUS:
            if filter_source and c["metadata"]["source_type"] != filter_source:
                continue
            overlap = len(q & set(c["content"].lower().split()))
            out.append({"content": c["content"], "score": 0.4 + 0.1 * overlap, "metadata": dict(c["metadata"])})
        out.sort(key=lambda h: h["score"], reverse=True)
        return out[:top_k]


def _client(monkeypatch, delay=0.0):
    store = _StubStore()
    _StubStore.delay = delay
    _StubStore.calls = 0
    monkeypatch.setattr(tm, "get_vector_store", lambda: store)
    import services.retrieval_service as rs
    monkeypatch.setattr(rs, "get_vector_store", lambda: store)
    app = FastAPI()
    app.include_router(tm.router, prefix="/api/text-match")
    # A persistent client keeps one event loop alive (like uvicorn), so the
    # background job task isn't cancelled between requests.
    client = TestClient(app)
    client.__enter__()
    return client


def _wait(client, job_id, until, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        snap = client.get(f"/api/text-match/jobs/{job_id}").json()
        if until(snap):
            return snap
        time.sleep(0.02)
    raise AssertionError(f"job did not reach expected state: {snap}")


def test_extraction_merges_continuation_rows_and_splits_standards():
    r = tm.extract_matchable_text("nokia.csv", NOKIA_LIKE_CSV.encode())
    assert r["ok"]
    assert r["records_extracted"] == 3          # continuation row folded into record 2
    rec2 = r["records"][1]
    assert rec2["std"] == ["TS 38.331", "TS 38.213", "TS 38.214"]
    assert "TS 38.331, TS 38.213, TS 38.214" in r["text"]
    assert any("no title or illustrative" in w for w in r["warnings"])


def test_record_units_one_query_per_record_with_scope_for_standard_only():
    r = tm.extract_matchable_text("nokia.csv", NOKIA_LIKE_CSV.encode())
    units = tm._record_units(r["records"])
    assert len(units) == 3
    assert units[0]["restrict_to_declared"] is True
    assert "Radio Resource Control" in units[0]["query"]      # spec scope from built-in catalog
    assert units[2]["restrict_to_declared"] is False
    assert units[2]["query"].startswith("Beam failure recovery")
    assert units[2]["label"] == "Record 3 · US11000001B2"


def test_norm_spec_handles_etsi_numbering():
    assert tm._norm_spec("TS 38.331") == "38.331"
    assert tm._norm_spec("ETSI TS 138 331") == "38.331"
    assert tm._norm_spec("TS 123 501") == "23.501"
    assert tm._norm_spec("5G") is None


def test_match_file_restricts_standard_only_records_to_declared_spec(monkeypatch):
    client = _client(monkeypatch)
    resp = client.post("/api/text-match/match-file", files={"file": ("nokia.csv", NOKIA_LIKE_CSV, "text/csv")})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["segments"]) == 3
    seg1 = body["segments"][0]
    assert seg1["label"].startswith("Record 1")
    assert seg1["matches"] and all(m["metadata"]["spec"] == "TS 38.331" for m in seg1["matches"])
    seg2 = body["segments"][1]
    assert {m["metadata"]["spec"] for m in seg2["matches"]} <= {"TS 38.331", "TS 38.213", "TS 38.214"}
    seg3 = body["segments"][2]
    assert seg3["matches"][0]["metadata"]["declared_standard_match"] is True


def test_job_runs_to_completion(monkeypatch):
    client = _client(monkeypatch)
    start = client.post("/api/text-match/jobs/file", files={"file": ("nokia.csv", NOKIA_LIKE_CSV, "text/csv")}).json()
    snap = _wait(client, start["job_id"], lambda s: s["status"] == "completed")
    assert snap["done"] == snap["total"] == 3
    assert len(snap["result"]["segments"]) == 3
    assert snap["result"]["records_extracted"] == 3


def test_job_pause_resume_and_stop(monkeypatch):
    client = _client(monkeypatch, delay=0.15)
    text = " ".join(f"Sentence number {i} about beam failure recovery procedures." * 3 for i in range(40))
    start = client.post("/api/text-match/jobs/text", json={"text": text}).json()
    jid = start["job_id"]
    _wait(client, jid, lambda s: s["done"] >= 1)

    assert client.post(f"/api/text-match/jobs/{jid}/pause").json()["status"] == "paused"
    time.sleep(0.4)  # let the in-flight unit finish
    a = client.get(f"/api/text-match/jobs/{jid}").json()["done"]
    time.sleep(0.5)
    b = client.get(f"/api/text-match/jobs/{jid}").json()
    assert b["status"] == "paused" and b["done"] == a      # no progress while paused

    client.post(f"/api/text-match/jobs/{jid}/resume")
    _wait(client, jid, lambda s: s["done"] > a)

    client.post(f"/api/text-match/jobs/{jid}/stop")
    snap = _wait(client, jid, lambda s: s["status"] in ("stopped", "completed"))
    assert snap["status"] == "stopped"
    assert 0 < snap["done"] < snap["total"]
    assert len(snap["result"]["segments"]) == snap["done"]  # partial results kept


def test_stop_while_paused(monkeypatch):
    client = _client(monkeypatch, delay=0.1)
    text = " ".join(f"Sentence {i} about random access and handover." * 3 for i in range(30))
    jid = client.post("/api/text-match/jobs/text", json={"text": text}).json()["job_id"]
    _wait(client, jid, lambda s: s["done"] >= 1)
    client.post(f"/api/text-match/jobs/{jid}/pause")
    client.post(f"/api/text-match/jobs/{jid}/stop")
    snap = _wait(client, jid, lambda s: s["status"] == "stopped")
    assert snap["done"] < snap["total"]


def test_job_failure_is_reported(monkeypatch):
    client = _client(monkeypatch)
    jid = client.post("/api/text-match/jobs/file", files={"file": ("x.docx", b"PK\x03\x04not-a-real-docx", "application/octet-stream")}).json()["job_id"]
    snap = _wait(client, jid, lambda s: s["status"] == "failed")
    assert snap["error"]


def test_unknown_job_404(monkeypatch):
    client = _client(monkeypatch)
    assert client.get("/api/text-match/jobs/nope").status_code == 404
