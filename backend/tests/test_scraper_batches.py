"""
v0.3 Part C: true multi-series corpus batch tests.

Covers: batch lifecycle, concurrency limiting, attach-to-existing-active-
series (no duplicate crawl), cancellation (including the queued-child
race guard), resume (only failed series re-run), and duplicate-batch-start
protection (no duplicate crawl even with two racing batch-start requests).

Run: python -m pytest backend/tests/test_scraper_batches.py -q
"""
import asyncio
import importlib
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    import httpx
    HAVE_HTTPX = True
except ImportError:
    HAVE_HTTPX = False


def _fresh_app_and_db(db_path: str):
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{db_path}"
    for mod_name in list(sys.modules):
        if mod_name in ("core.database", "core.config") or mod_name.startswith("api.routes.gitas.scrape") or mod_name.startswith("ingestion.gpp_crawl"):
            del sys.modules[mod_name]
    core_database = importlib.import_module("core.database")
    scrape_mod = importlib.import_module("api.routes.gitas.scrape")
    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(scrape_mod.router, prefix="/api/scrape")
    return app, core_database, scrape_mod


def _run(coro):
    return asyncio.run(coro)


def _fake_success(call_log, delay=0.0):
    async def fake_crawl_series(series, out_root, job_id, max_files=None, on_progress=None, on_file_event=None, throttle=0.3):
        call_log.append(series)
        if delay:
            await asyncio.sleep(delay)
        class S:
            files_downloaded = 5; files_skipped_existing = 0; errors = 0
            bytes_downloaded = 100; files_found = 5; cancelled = False
        return S()
    return fake_crawl_series


def _fake_index(chunks=10):
    async def fake_build_corpus_from_crawl(root, include=None):
        class S:
            chunks_indexed = chunks
        return S()
    return fake_build_corpus_from_crawl


def test_single_series_batch_completes(tmp_path):
    if not HAVE_HTTPX:
        return
    app, core_database, scrape_mod = _fresh_app_and_db(str(tmp_path / "t1.db"))
    call_log = []
    scrape_mod.crawl_series = _fake_success(call_log)
    scrape_mod.build_corpus_from_crawl = _fake_index()

    async def scenario():
        await core_database.init_db()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.post("/api/scrape/batches", json={"series": ["38_series"]})
            batch_id = r.json()["batch_id"]
            return (await client.get(f"/api/scrape/batches/{batch_id}")).json()

    batch = _run(scenario())
    assert batch["status"] == "completed"
    assert batch["series_completed"] == 1
    assert batch["series_total"] == 1


def test_multi_series_batch_with_concurrency_limit_completes(tmp_path):
    if not HAVE_HTTPX:
        return
    app, core_database, scrape_mod = _fresh_app_and_db(str(tmp_path / "t2.db"))
    call_log = []
    scrape_mod.crawl_series = _fake_success(call_log)
    scrape_mod.build_corpus_from_crawl = _fake_index()

    async def scenario():
        await core_database.init_db()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.post("/api/scrape/batches", json={
                "series": ["38_series", "36_series", "37_series", "24_series"], "max_concurrent": 2,
            })
            batch_id = r.json()["batch_id"]
            return (await client.get(f"/api/scrape/batches/{batch_id}")).json()

    batch = _run(scenario())
    assert batch["status"] == "completed"
    assert batch["series_completed"] == 4
    assert batch["series_total"] == 4
    assert batch["chunks_indexed"] == 40
    assert set(call_log) == {"38_series", "36_series", "37_series", "24_series"}
    assert len(call_log) == 4  # exactly one crawl per series, no duplicates


def test_batch_attaches_to_existing_active_series_no_duplicate_crawl(tmp_path):
    if not HAVE_HTTPX:
        return
    app, core_database, scrape_mod = _fresh_app_and_db(str(tmp_path / "t3.db"))
    call_log = []
    scrape_mod.crawl_series = _fake_success(call_log, delay=0.3)
    scrape_mod.build_corpus_from_crawl = _fake_index()

    async def scenario():
        await core_database.init_db()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            r1_task = asyncio.create_task(client.post("/api/scrape/start", json={"series": ["38_series"]}))
            await asyncio.sleep(0.02)
            r2_task = asyncio.create_task(client.post("/api/scrape/batches", json={"series": ["38_series", "36_series"]}))
            r1, r2 = await asyncio.gather(r1_task, r2_task)
            existing_job_id = r1.json()["jobs"][0]["id"]
            batch = (await client.get(f"/api/scrape/batches/{r2.json()['batch_id']}")).json()
            return existing_job_id, r2.json(), batch

    existing_job_id, batch_start, batch = _run(scenario())
    assert existing_job_id in batch_start["child_job_ids"]
    assert batch["status"] == "completed"
    assert call_log.count("38_series") == 1
    assert call_log.count("36_series") == 1


def test_batch_cancellation_stops_queued_children_and_prevents_late_start(tmp_path):
    """The queued-child race guard: a child that's cancelled while still
    waiting for its concurrency-limit slot must never run once its turn
    comes up."""
    if not HAVE_HTTPX:
        return
    app, core_database, scrape_mod = _fresh_app_and_db(str(tmp_path / "t4.db"))
    call_log = []

    async def scenario():
        import ingestion.gpp_crawl as gc

        async def fake_crawl_series(series, out_root, job_id, max_files=None, on_progress=None, on_file_event=None, throttle=0.3):
            call_log.append(series)
            cancel_event = gc._cancel_events.setdefault(job_id, asyncio.Event())
            try:
                for _ in range(20):
                    if cancel_event.is_set():
                        class S:
                            files_downloaded = 1; files_skipped_existing = 0; errors = 0
                            bytes_downloaded = 10; files_found = 1; cancelled = True
                        return S()
                    await asyncio.sleep(0.08)
                class S:
                    files_downloaded = 5; files_skipped_existing = 0; errors = 0
                    bytes_downloaded = 100; files_found = 5; cancelled = False
                return S()
            finally:
                gc._cancel_events.pop(job_id, None)

        scrape_mod.crawl_series = fake_crawl_series
        scrape_mod.build_corpus_from_crawl = _fake_index()

        await core_database.init_db()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            r_task = asyncio.create_task(client.post("/api/scrape/batches", json={
                "series": ["38_series", "36_series", "37_series"], "max_concurrent": 1,
            }))
            # Poll until the batch exists AND its first child has actually
            # entered crawl_series, instead of assuming a fixed 0.15s is
            # enough. Under load (the full suite on a busy machine) that
            # sleep could expire before the first child started, and the
            # test then failed on `call_log == ["38_series"]` -- a false
            # failure with nothing wrong in the code under test.
            batch_id = None
            for _ in range(400):
                batches = (await client.get("/api/scrape/batches")).json()["batches"]
                if batches:
                    batch_id = batches[0]["id"]
                    if call_log:
                        break
                await asyncio.sleep(0.02)
            assert batch_id is not None, "batch was never created"
            assert call_log, "the first child never started crawling"
            await client.post(f"/api/scrape/batches/{batch_id}/cancel")
            await r_task
            return (await client.get(f"/api/scrape/batches/{batch_id}")).json()

    batch = _run(scenario())
    assert batch["status"] == "cancelled"
    statuses = {c["series"]: c["status"] for c in batch["children"]}
    assert statuses["38_series"] == "cancelled"
    assert statuses["36_series"] == "cancelled"
    assert statuses["37_series"] == "cancelled"
    # Only the series that was actually mid-crawl should have invoked
    # crawl_series at all -- the queued ones must never start.
    assert call_log == ["38_series"]


def test_batch_resume_only_reruns_failed_series(tmp_path):
    if not HAVE_HTTPX:
        return
    app, core_database, scrape_mod = _fresh_app_and_db(str(tmp_path / "t5.db"))
    call_log = []
    state = {"38_failed_once": False}

    async def fake_crawl_series(series, out_root, job_id, max_files=None, on_progress=None, on_file_event=None, throttle=0.3):
        call_log.append(series)
        if series == "38_series" and not state["38_failed_once"]:
            state["38_failed_once"] = True
            raise RuntimeError("simulated transient failure")
        class S:
            files_downloaded = 5; files_skipped_existing = 0; errors = 0
            bytes_downloaded = 100; files_found = 5; cancelled = False
        return S()

    scrape_mod.crawl_series = fake_crawl_series
    scrape_mod.build_corpus_from_crawl = _fake_index()

    async def scenario():
        await core_database.init_db()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.post("/api/scrape/batches", json={"series": ["38_series", "36_series"]})
            batch_id = r.json()["batch_id"]
            first = (await client.get(f"/api/scrape/batches/{batch_id}")).json()

            await client.post(f"/api/scrape/batches/{batch_id}/retry")
            second = (await client.get(f"/api/scrape/batches/{batch_id}")).json()
            return first, second

    first, second = _run(scenario())
    assert first["status"] == "completed_with_errors"
    assert second["status"] == "completed"
    assert call_log.count("36_series") == 1  # never re-run
    assert call_log.count("38_series") == 2  # 1 failure + 1 successful resume


def test_rapid_double_click_batch_start_never_duplicates_crawls(tmp_path):
    if not HAVE_HTTPX:
        return
    app, core_database, scrape_mod = _fresh_app_and_db(str(tmp_path / "t6.db"))
    call_log = []
    scrape_mod.crawl_series = _fake_success(call_log, delay=0.15)
    scrape_mod.build_corpus_from_crawl = _fake_index()

    async def scenario():
        await core_database.init_db()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            await asyncio.gather(
                client.post("/api/scrape/batches", json={"series": ["38_series", "36_series"]}),
                client.post("/api/scrape/batches", json={"series": ["38_series", "36_series"]}),
            )

    _run(scenario())
    assert call_log.count("38_series") == 1
    assert call_log.count("36_series") == 1


def test_batch_history_lists_recent_batches(tmp_path):
    if not HAVE_HTTPX:
        return
    app, core_database, scrape_mod = _fresh_app_and_db(str(tmp_path / "t7.db"))
    call_log = []
    scrape_mod.crawl_series = _fake_success(call_log)
    scrape_mod.build_corpus_from_crawl = _fake_index()

    async def scenario():
        await core_database.init_db()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            await client.post("/api/scrape/batches", json={"series": ["38_series"]})
            return (await client.get("/api/scrape/batches")).json()

    result = _run(scenario())
    assert len(result["batches"]) == 1
    assert result["batches"][0]["series_total"] == 1


def test_restart_recovery_marks_orphaned_active_jobs_and_batches_failed(tmp_path):
    """If the process is killed mid-crawl, the next init_db() must not
    leave a job/batch stuck 'active' forever (which would otherwise
    permanently block that series via the duplicate-job protection)."""
    app, core_database, scrape_mod = _fresh_app_and_db(str(tmp_path / "t8.db"))

    async def scenario():
        await core_database.init_db()
        from core.database import AsyncSessionLocal, SpecCrawlJob, SpecCrawlBatch
        from sqlalchemy import select
        from datetime import datetime

        async with AsyncSessionLocal() as session:
            job = SpecCrawlJob(series="38_series", status="crawling", started_at=datetime.utcnow())
            session.add(job)
            batch = SpecCrawlBatch(series_requested="[]", child_job_ids="[]", status="running", started_at=datetime.utcnow())
            session.add(batch)
            await session.commit()

        # Simulate the app restarting: re-run the recovery step directly.
        recovered = await core_database.recover_interrupted_crawl_jobs()

        async with AsyncSessionLocal() as session:
            result = await session.execute(select(SpecCrawlJob))
            job_after = result.scalars().first()
            result = await session.execute(select(SpecCrawlBatch))
            batch_after = result.scalars().first()
            return recovered, job_after.status, job_after.retryable, batch_after.status

    recovered, job_status, retryable, batch_status = _run(scenario())
    assert recovered == 1
    assert job_status == "failed"
    assert retryable is True
    assert batch_status == "failed"


if __name__ == "__main__":
    print("Run via pytest: python -m pytest backend/tests/test_scraper_batches.py -q")
