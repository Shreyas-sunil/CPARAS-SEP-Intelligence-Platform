"""
v0.3 scraper concurrency/idempotency tests.

Covers the WinError 32 / duplicate-job bug fixed in this release:
  - core/database.py: partial UNIQUE index on spec_crawl_jobs
  - api/routes/scrape.py: per-series asyncio.Lock + check-then-create
  - ingestion/gpp_crawl.py: job-scoped temp filenames, retried atomic finalize

Run: python -m pytest backend/tests/test_scraper_concurrency.py -q
"""
import asyncio
import importlib
import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    import httpx
    HAVE_HTTPX = True
except ImportError:
    HAVE_HTTPX = False


def _fresh_app_and_db(db_path: str):
    """Same reimport-from-clean-sys.modules pattern as test_csaras_api.py's
    _fresh_app_and_db -- gives each test its own engine/DB rather than
    sharing state (and, critically here, its own fresh _series_locks dict
    in api.routes.gitas.scrape, since a stale lock object bound to a closed
    event loop from a previous test would otherwise deadlock this one)."""
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


# ── DB / job-creation level tests (no network, no real crawl) ──────────────

def test_five_concurrent_start_requests_create_exactly_one_job(tmp_path):
    if not HAVE_HTTPX:
        return
    app, core_database, scrape_mod = _fresh_app_and_db(str(tmp_path / "t1.db"))
    call_log = []

    async def fake_crawl_series(series, out_root, job_id, max_files=None, on_progress=None, on_file_event=None, throttle=0.3):
        call_log.append((series, job_id))
        await asyncio.sleep(0.3)
        class S:
            files_downloaded = 1; files_skipped_existing = 0; errors = 0; bytes_downloaded = 10; files_found = 1; cancelled = False
        return S()

    async def fake_build_corpus_from_crawl(root, include=None):
        class S:
            chunks_indexed = 1
        return S()

    scrape_mod.crawl_series = fake_crawl_series
    scrape_mod.build_corpus_from_crawl = fake_build_corpus_from_crawl

    async def scenario():
        await core_database.init_db()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            results = await asyncio.gather(*[
                client.post("/api/scrape/start", json={"series": ["38_series"]}) for _ in range(5)
            ])
            all_jobs = [j for r in results for j in r.json()["jobs"]]
            all_already = [j for r in results for j in r.json()["already_running"]]
            await asyncio.sleep(0.6)
            active = [j for j in (await client.get("/api/scrape/jobs")).json()["jobs"]
                      if j["status"] in ("pending", "crawling", "indexing")]
            return all_jobs, all_already, active

        return None

    all_jobs, all_already, active = _run(scenario())
    assert len(all_jobs) == 1, f"expected exactly 1 job created, got {len(all_jobs)}: {all_jobs}"
    assert len(all_already) == 4
    assert len(call_log) == 1, f"expected exactly 1 actual crawl_series call, got {len(call_log)}"
    assert active == []


def test_rapid_double_click_retry_creates_exactly_one_new_job(tmp_path):
    if not HAVE_HTTPX:
        return
    app, core_database, scrape_mod = _fresh_app_and_db(str(tmp_path / "t2.db"))
    call_count = {"n": 0}

    async def fake_crawl_series(series, out_root, job_id, max_files=None, on_progress=None, on_file_event=None, throttle=0.3):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("simulated network failure")
        await asyncio.sleep(0.3)
        class S:
            files_downloaded = 1; files_skipped_existing = 0; errors = 0; bytes_downloaded = 10; files_found = 1; cancelled = False
        return S()

    async def fake_build_corpus_from_crawl(root, include=None):
        class S:
            chunks_indexed = 1
        return S()

    scrape_mod.crawl_series = fake_crawl_series
    scrape_mod.build_corpus_from_crawl = fake_build_corpus_from_crawl

    async def scenario():
        await core_database.init_db()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            r1 = await client.post("/api/scrape/start", json={"series": ["36_series"]})
            job_id = r1.json()["jobs"][0]["id"]
            await asyncio.sleep(0.15)
            failed_status = (await client.get(f"/api/scrape/jobs/{job_id}")).json()["status"]

            await asyncio.gather(
                client.post(f"/api/scrape/jobs/{job_id}/retry"),
                client.post(f"/api/scrape/jobs/{job_id}/retry"),
            )
            await asyncio.sleep(0.5)
            jobs = [j for j in (await client.get("/api/scrape/jobs")).json()["jobs"] if j["series"] == "36_series"]
            return failed_status, jobs

    failed_status, jobs = _run(scenario())
    assert failed_status == "failed"
    assert len(jobs) == 2, f"expected exactly 2 total jobs (1 failed original + 1 retry), got {len(jobs)}"
    assert call_count["n"] == 2


def test_existing_zip_is_skipped_not_redownloaded():
    """Idempotent resume: a non-empty destination file is never re-fetched."""
    from ingestion import gpp_crawl as gc

    async def scenario():
        tmpdir = Path(tempfile.mkdtemp())
        dest = tmpdir / "existing.zip"
        dest.write_bytes(b"already downloaded content")
        stats = gc.CrawlStats()

        class FakeClient:
            async def stream(self, *a, **kw):
                raise AssertionError("must not attempt a network request for an existing non-empty file")

        await gc._download_file(FakeClient(), "http://example/existing.zip", dest, stats, 5.0, 3, job_id=1)
        assert stats.files_skipped_existing == 1
        assert stats.files_downloaded == 0
        assert dest.read_bytes() == b"already downloaded content"

    _run(scenario())


def test_temp_filenames_are_unique_per_job():
    from ingestion import gpp_crawl as gc
    dest = Path("/tmp/some/38211-j40.zip")
    tmp1 = dest.with_name(f"{dest.name}.part.1.aaaaaaaa")
    tmp2 = dest.with_name(f"{dest.name}.part.2.bbbbbbbb")
    assert tmp1 != tmp2


def test_atomic_finalize_retries_transient_lock_then_succeeds(tmp_path):
    from ingestion import gpp_crawl as gc
    import unittest.mock as mock

    async def scenario():
        tmp = tmp_path / "file.zip.part.1.aaaaaaaa"
        dest = tmp_path / "file.zip"
        tmp.write_bytes(b"data")
        attempts = {"n": 0}
        real_replace = Path.replace

        def flaky(self, target):
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise PermissionError("[WinError 32] simulated lock")
            return real_replace(self, target)

        gc.FINALIZE_BASE_DELAY = 0.01
        with mock.patch.object(Path, "replace", flaky):
            await gc._atomic_finalize(tmp, dest)
        assert dest.exists()
        assert attempts["n"] == 3

    _run(scenario())


def test_atomic_finalize_raises_cleanly_after_exhausting_retries(tmp_path):
    from ingestion import gpp_crawl as gc
    import unittest.mock as mock

    async def scenario():
        tmp = tmp_path / "file2.zip.part.1.bbbbbbbb"
        dest = tmp_path / "file2.zip"
        tmp.write_bytes(b"data")
        gc.FINALIZE_BASE_DELAY = 0.01

        def always_fails(self, target):
            raise PermissionError("[WinError 32] persistent lock")

        with mock.patch.object(Path, "replace", always_fails):
            with pytest.raises(PermissionError):
                await gc._atomic_finalize(tmp, dest)

    _run(scenario())


def test_stale_temp_file_cleanup_leaves_active_files_alone(tmp_path):
    import os as os_mod
    import time
    from ingestion import gpp_crawl as gc

    fresh = tmp_path / "active.zip.part.99.ffffffff"
    fresh.write_bytes(b"x")
    stale = tmp_path / "crashed.zip.part.1.deadbeef"
    stale.write_bytes(b"x")
    old = time.time() - 7 * 3600
    os_mod.utime(stale, (old, old))

    removed = gc.cleanup_stale_temp_files(tmp_path, max_age_seconds=6 * 3600)
    assert removed == 1
    assert fresh.exists()
    assert not stale.exists()


if __name__ == "__main__":
    print("Run via pytest: python -m pytest backend/tests/test_scraper_concurrency.py -q")
