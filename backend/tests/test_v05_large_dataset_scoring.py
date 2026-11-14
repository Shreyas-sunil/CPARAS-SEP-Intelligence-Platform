"""
Regression tests for the v0.5 large-dataset GITAS scoring architecture
(etsi_bulk/scoring.py) and the three-signal (claim_match/semantic_
similarity/standard_coverage) fields shared with cPARAS
(research/three_signal_scoring.py, research/engine.py).

Run: python -m pytest backend/tests/test_v05_large_dataset_scoring.py -q

No network, no ML dependency stack required — the embedder falls back to
TF-IDF automatically when sentence-transformers isn't installed (see
research/embeddings.py).
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import numpy as np  # noqa: E402
import pytest  # noqa: E402

from etsi_bulk.scoring import score_records_batched, score_records  # noqa: E402
from research import embeddings as research_embeddings  # noqa: E402
from research.three_signal_scoring import claim_match_score, standard_coverage_score, tokenize  # noqa: E402


def _make_records(n: int, *, standard_raw="TS 38.331", spec_number="38.331", spec_version="v16.5.0"):
    return [
        {
            "title": f"Method for RRC reconfiguration variant {i}",
            "standard_raw": standard_raw,
            "spec_number": spec_number,
            "spec_version": spec_version,
            "illustrative_part": f"Embodiment {i} of RRC connection procedure",
            "explicit_disclosure": i % 2 == 0,
            "dipg_id": f"D{i}",
            "publication_number": f"PUB{i}",
            "application_number": f"APP{i}",
            "project_acronym": "3GPP-Release-16",
            "legal_status": "Active",
            "gen_5g": True,
        }
        for i in range(n)
    ]


# ── 1. Score values stay in [0, 1] ───────────────────────────────────────────

def test_score_values_remain_0_to_1():
    records = _make_records(50)
    out, meta = asyncio.run(score_records(records))
    assert len(out) == 50
    for row in out:
        assert row["claim_match"] is None or 0.0 <= row["claim_match"] <= 1.0
        assert row["semantic_similarity"] is None or 0.0 <= row["semantic_similarity"] <= 1.0
        assert row["standard_coverage"] is None or 0.0 <= row["standard_coverage"] <= 1.0
        assert row["confidence"] is None or 0.0 <= row["confidence"] <= 1.0


# ── 2. 200-patent processing still works (small-dataset regression) ─────────

def test_small_dataset_200_still_works():
    records = _make_records(200)
    out, meta = asyncio.run(score_records(records))
    assert len(out) == 200
    assert meta["scored"] == 200
    assert meta["failed"] == 0
    assert all(r["essentiality"] in ("Essential", "Potential", "Non-Essential") for r in out)


# ── 3. A failed row does not abort the batch (fault isolation) ──────────────

def test_one_malformed_row_does_not_abort_the_batch():
    records = _make_records(300)
    poison_index = 130  # inside batch 1 of a batch_size=100 run, not the first row
    poison_marker = object()
    records[poison_index]["title"] = poison_marker

    orig_patent_text = None
    import etsi_bulk.scoring as scoring_mod
    orig_patent_text = scoring_mod._patent_text

    def patched(row):
        if row.get("title") is poison_marker:
            raise ValueError("simulated malformed row")
        return orig_patent_text(row)

    scoring_mod._patent_text = patched
    try:
        out, meta = asyncio.run(score_records(records))
    finally:
        scoring_mod._patent_text = orig_patent_text

    assert len(out) == 300, "every input row must produce an output row, success or failure"
    failed = [r for r in out if r.get("_row_failed")]
    ok = [r for r in out if not r.get("_row_failed")]
    assert len(failed) == 1
    assert len(ok) == 299
    # Neighbors in the same batch scored normally -- the actual acceptance
    # criterion ("if patent #N fails, the rest continue").
    assert out[poison_index - 1]["semantic_similarity"] is not None
    assert out[poison_index + 1]["semantic_similarity"] is not None
    # A failed row never gets a fabricated zero/fake score.
    assert out[poison_index]["claim_match"] is None
    assert out[poison_index]["semantic_similarity"] is None
    assert out[poison_index]["standard_coverage"] is None


# ── 4. No O(n_patents x n_corpus) matrix is ever built, regardless of n ─────

def test_no_full_matrix_is_ever_allocated_for_a_large_dataset():
    """The whole point of the batched architecture: whatever cosine_sim_
    matrix() is asked to compute during a run, its first dimension is
    always bounded by the batch size, never by the total dataset size."""
    records = _make_records(3000)
    max_first_dim_seen = 0

    import etsi_bulk.scoring as scoring_mod
    orig = scoring_mod.cosine_sim_matrix

    def spy(a, b):
        nonlocal max_first_dim_seen
        max_first_dim_seen = max(max_first_dim_seen, a.shape[0])
        return orig(a, b)

    scoring_mod.cosine_sim_matrix = spy
    try:
        out, meta = asyncio.run(score_records(records))
    finally:
        scoring_mod.cosine_sim_matrix = orig

    assert len(out) == 3000
    # batch_size defaults to settings.SCORING_BATCH_SIZE (256); the retry
    # path calls with a single row (shape[0] == 1), so the max across the
    # whole run should be the batch size, never anywhere near 3000.
    from core.config import settings
    assert max_first_dim_seen <= settings.SCORING_BATCH_SIZE, (
        f"a similarity computation used {max_first_dim_seen} rows at once "
        f"(batch size is {settings.SCORING_BATCH_SIZE}) -- the full-matrix "
        f"regression this architecture exists to prevent."
    )


# ── 5. Batched generator yields multiple chunks for a multi-batch dataset ──

def test_batched_generator_yields_multiple_chunks():
    records = _make_records(600)

    async def run():
        batches = []
        async for batch, meta in score_records_batched(records, batch_size=100):
            batches.append(batch)
        return batches

    batches = asyncio.run(run())
    assert len(batches) == 6, "600 records at batch_size=100 should yield 6 chunks"
    assert sum(len(b) for b in batches) == 600


# ── 6. Shared three-signal formulas: pure function sanity ───────────────────

def test_claim_match_and_standard_coverage_are_bounded_and_monotonic_in_similarity():
    toks = tokenize("rrc connection reconfiguration procedure")
    low = claim_match_score(
        patent_tokens=toks, clause_title="Unrelated topic", clause_keywords=["foo bar"],
        semantic_similarity=0.1, explicit_disclosure=False,
    )
    high = claim_match_score(
        patent_tokens=toks, clause_title="Unrelated topic", clause_keywords=["foo bar"],
        semantic_similarity=0.9, explicit_disclosure=False,
    )
    assert 0.0 <= low <= 1.0 and 0.0 <= high <= 1.0
    assert high > low, "claim_match should increase with semantic similarity, all else equal"

    cov_no_match = standard_coverage_score(semantic_similarity=0.5, clause_group_count=4, declared_family_match=False)
    cov_match = standard_coverage_score(semantic_similarity=0.5, clause_group_count=4, declared_family_match=True)
    assert 0.0 <= cov_no_match <= 1.0 and 0.0 <= cov_match <= 1.0
    assert cov_match > cov_no_match, "a declared-family match should raise standard_coverage"


# ── 7. GITAS Excel export contains the 3 mandatory columns (integration) ───

def test_gitas_excel_export_has_mandatory_three_signal_columns(tmp_path, monkeypatch):
    pytest.importorskip("openpyxl")
    pytest.importorskip("httpx")
    import io
    import importlib
    import openpyxl

    db_path = tmp_path / "v05_test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    for mod in list(sys.modules):
        if mod in ("core.database", "core.config") or mod.startswith("api.routes"):
            del sys.modules[mod]

    core_database = importlib.import_module("core.database")
    sep_routes = importlib.import_module("api.routes.gitas.dashboard")

    from fastapi import FastAPI
    import httpx

    app = FastAPI()
    app.include_router(sep_routes.router, prefix="/api/sep")

    async def go():
        await core_database.init_db()
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append([
            "DIPG_ID", "DIPG_DISPLAY_NUMBER", "COMP_LEGAL_NAME", "Standard", "TGPP_NUMBER", "TGPV_VERSION",
            "PATT_APPLICATION_NUMBER", "PUBL_NUMBER", "PBPA_TITLEEN", "Illustrative_Part",
        ])
        for i in range(10):
            ws.append([f"D{i}", f"DISC{i}", "Acme Corp", "TS 38.331", "38.331", "v16.5.0",
                       f"APP{i}", f"PUB{i}", f"Method variant {i}", f"Embodiment {i}"])
        buf = io.BytesIO()
        wb.save(buf)

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/api/sep/upload", files={"file": ("t.xlsx", buf.getvalue(),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
            assert resp.status_code == 200
            run_id = resp.json()["run_id"]

            for _ in range(200):
                s = (await client.get(f"/api/sep/runs/{run_id}/status")).json()
                if s["status"] in ("completed", "completed_with_errors", "failed"):
                    break
                await asyncio.sleep(0.05)
            assert s["status"] == "completed", s

            exp = await client.get(f"/api/sep/runs/{run_id}/export/excel")
            assert exp.status_code == 200
            return exp.content

    xlsx_bytes = asyncio.run(go())
    wbr = openpyxl.load_workbook(io.BytesIO(xlsx_bytes))
    ws = wbr["Sheet1"]
    headers = [c.value for c in next(ws.iter_rows(min_row=1, max_row=1))]
    for col in ("CPARAS_Bulk_Claim_Match", "CPARAS_Bulk_Semantic_Similarity", "CPARAS_Bulk_Standard_Coverage"):
        assert col in headers, f"MISSING mandatory GITAS column: {col}"
    idx = {h: i for i, h in enumerate(headers)}
    rows = list(ws.iter_rows(min_row=2, values_only=True))
    assert len(rows) == 10
    for r in rows:
        assert r[idx["CPARAS_Bulk_Claim_Match"]] is not None
        assert r[idx["CPARAS_Bulk_Semantic_Similarity"]] is not None
        assert r[idx["CPARAS_Bulk_Standard_Coverage"]] is not None


def test_gitas_bulk_run_pause_and_continue(tmp_path, monkeypatch):
    """v0.6 start/stop/pause/continue: pausing a bulk SEP run mid-scoring
    leaves it resumable exactly where it stopped, and /continue finishes
    the remaining rows (re-parsing the original file it kept for exactly
    this purpose) without losing or duplicating already-scored rows."""
    pytest.importorskip("openpyxl")
    pytest.importorskip("httpx")
    import io
    import importlib
    import openpyxl

    db_path = tmp_path / "v06_pause_test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")

    # Deliberately do NOT reload core.config here (unlike the export test
    # above): etsi_bulk/scoring.py did `from core.config import settings`
    # once, whenever it first got imported in this test session, and
    # keeps THAT object reference regardless of any later core.config
    # reload elsewhere in this file -- so mutate the settings object via
    # etsi_bulk.scoring itself (the module that actually reads
    # SCORING_BATCH_SIZE at call time), not via a freshly (re)imported
    # core.config that scoring.py may not be looking at.
    import etsi_bulk.scoring as scoring_mod
    # Force many small batches so a pause request has a real chance to
    # land mid-run instead of the whole (small, test-sized) file scoring
    # in a single batch.
    scoring_mod.settings.SCORING_BATCH_SIZE = 2

    # core.database (about to be reimported fresh below) does its own
    # `from core.config import settings` and builds its engine from
    # settings.DATABASE_URL at import time -- make sure whichever
    # core.config module is currently cached (possibly reloaded by an
    # earlier test in this file) also has this test's DB path.
    core_config = importlib.import_module("core.config")
    core_config.settings.DATABASE_URL = f"sqlite+aiosqlite:///{db_path}"

    for mod in list(sys.modules):
        if mod == "core.database" or mod.startswith("api.routes"):
            del sys.modules[mod]

    core_database = importlib.import_module("core.database")
    sep_routes = importlib.import_module("api.routes.gitas.dashboard")

    from fastapi import FastAPI
    import httpx

    app = FastAPI()
    app.include_router(sep_routes.router, prefix="/api/sep")

    async def go():
        await core_database.init_db()
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append([
            "DIPG_ID", "DIPG_DISPLAY_NUMBER", "COMP_LEGAL_NAME", "Standard", "TGPP_NUMBER", "TGPV_VERSION",
            "PATT_APPLICATION_NUMBER", "PUBL_NUMBER", "PBPA_TITLEEN", "Illustrative_Part",
        ])
        for i in range(20):
            ws.append([f"D{i}", f"DISC{i}", "Acme Corp", "TS 38.331", "38.331", "v16.5.0",
                       f"APP{i}", f"PUB{i}", f"Method variant {i}", f"Embodiment {i}"])
        buf = io.BytesIO()
        wb.save(buf)

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/api/sep/upload", files={"file": ("t.xlsx", buf.getvalue(),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
            assert resp.status_code == 200
            run_id = resp.json()["run_id"]

            # Pause as early as possible.
            pause_status = None
            for _ in range(200):
                r = await client.post(f"/api/sep/runs/{run_id}/pause")
                if r.status_code == 200:
                    pause_status = r
                    break
                await asyncio.sleep(0.005)
            assert pause_status is not None, "pause was never accepted -- the run finished before we could pause it"

            paused = None
            for _ in range(200):
                paused = (await client.get(f"/api/sep/runs/{run_id}/status")).json()
                if paused["status"] == "paused":
                    break
                await asyncio.sleep(0.02)
            assert paused["status"] == "paused", paused
            processed_at_pause = paused["processed"]
            assert 0 <= processed_at_pause < 20

            r2 = await client.post(f"/api/sep/runs/{run_id}/continue")
            assert r2.status_code == 200

            final = None
            for _ in range(400):
                final = (await client.get(f"/api/sep/runs/{run_id}/status")).json()
                if final["status"] in ("completed", "completed_with_errors", "failed"):
                    break
                await asyncio.sleep(0.02)
            assert final["status"] == "completed", final
            assert final["processed"] == 20

            patents = (await client.get(f"/api/sep/patents", params={"run_id": run_id, "limit": 100})).json()
            return patents

    patents = asyncio.run(go())
    assert patents["total"] == 20  # no duplicates or drops across the pause/continue boundary


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(list(globals().items())):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS: {name}")
            except Exception as e:
                failures += 1
                print(f"FAIL: {name}: {e}")
    raise SystemExit(1 if failures else 0)
