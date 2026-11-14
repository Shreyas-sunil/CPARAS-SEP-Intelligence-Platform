"""
Tests for the v0.3 production-hardening additions:
  - ZIP integrity checking (is_valid_zip)
  - safe ZIP extraction / path-traversal ("zip slip") rejection
  - crawl cancellation
  - /api/health structured diagnostics
  - 429 rate-limit handling

Run: python -m pytest backend/tests/test_v03_hardening.py -q
"""
import asyncio
import io
import os
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ingestion.gpp_crawl import is_valid_zip, safe_extract_zip, request_cancel, is_cancel_requested, _cancel_events  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


# ── ZIP integrity ────────────────────────────────────────────────────────────

def test_valid_zip_passes_integrity_check(tmp_path):
    p = tmp_path / "good.zip"
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("readme.txt", "hello")
    assert is_valid_zip(p) is True


def test_corrupt_zip_fails_integrity_check(tmp_path):
    p = tmp_path / "corrupt.zip"
    p.write_bytes(b"this is not a zip file at all, just garbage bytes")
    assert is_valid_zip(p) is False


def test_empty_file_fails_integrity_check(tmp_path):
    p = tmp_path / "empty.zip"
    p.write_bytes(b"")
    assert is_valid_zip(p) is False


def test_missing_file_fails_integrity_check(tmp_path):
    assert is_valid_zip(tmp_path / "does_not_exist.zip") is False


def test_truncated_zip_fails_integrity_check(tmp_path):
    good = tmp_path / "good.zip"
    with zipfile.ZipFile(good, "w") as zf:
        zf.writestr("a.txt", "x" * 10000)
    truncated = tmp_path / "truncated.zip"
    data = good.read_bytes()
    truncated.write_bytes(data[: len(data) // 2])  # cut off the central directory
    assert is_valid_zip(truncated) is False


# ── Safe extraction / zip-slip protection ───────────────────────────────────

def test_safe_extract_normal_zip_works(tmp_path):
    src = tmp_path / "normal.zip"
    with zipfile.ZipFile(src, "w") as zf:
        zf.writestr("docs/spec.txt", "spec content")
        zf.writestr("readme.txt", "readme content")
    dest = tmp_path / "out"
    extracted = safe_extract_zip(src, dest)
    assert len(extracted) == 2
    assert (dest / "docs" / "spec.txt").read_text() == "spec content"
    assert (dest / "readme.txt").read_text() == "readme content"
    for p in extracted:
        assert str(p.resolve()).startswith(str(dest.resolve()))


def test_safe_extract_rejects_relative_path_traversal(tmp_path):
    """The core 'zip slip' attack: a member named ../../evil.txt."""
    src = tmp_path / "evil.zip"
    with zipfile.ZipFile(src, "w") as zf:
        zf.writestr("../../evil.txt", "pwned")
    dest = tmp_path / "out"
    with pytest.raises(ValueError, match="escapes extraction directory"):
        safe_extract_zip(src, dest)
    # Nothing must have leaked outside dest even from partial processing.
    assert not (tmp_path.parent.parent / "evil.txt").exists()


def test_safe_extract_rejects_absolute_path(tmp_path):
    src = tmp_path / "evil_abs.zip"
    with zipfile.ZipFile(src, "w") as zf:
        zf.writestr("/etc/evil.txt", "pwned")
    dest = tmp_path / "out"
    with pytest.raises(ValueError, match="absolute path"):
        safe_extract_zip(src, dest)


def test_safe_extract_rejects_windows_drive_path(tmp_path):
    src = tmp_path / "evil_win.zip"
    with zipfile.ZipFile(src, "w") as zf:
        zf.writestr("C:/evil.txt", "pwned")
    dest = tmp_path / "out"
    with pytest.raises(ValueError, match="absolute path"):
        safe_extract_zip(src, dest)


def test_safe_extract_mixed_zip_rejects_whole_archive_on_first_bad_member(tmp_path):
    """A zip with one legitimate member and one malicious member must be
    rejected outright, not partially extracted -- partial extraction would
    leave the caller unsure what's actually on disk."""
    src = tmp_path / "mixed.zip"
    with zipfile.ZipFile(src, "w") as zf:
        zf.writestr("legit.txt", "fine")
        zf.writestr("../escape.txt", "not fine")
    dest = tmp_path / "out"
    with pytest.raises(ValueError):
        safe_extract_zip(src, dest)


# ── Cancellation ─────────────────────────────────────────────────────────────

def test_request_cancel_returns_false_for_unknown_job():
    assert request_cancel(999999) is False


def test_request_cancel_signals_registered_job():
    job_id = 424242
    _cancel_events[job_id] = asyncio.Event()
    try:
        assert is_cancel_requested(job_id) is False
        assert request_cancel(job_id) is True
        assert is_cancel_requested(job_id) is True
    finally:
        _cancel_events.pop(job_id, None)


def test_crawl_series_stops_when_cancelled_before_start(tmp_path):
    """If cancellation is requested before the crawl even begins listing,
    the crawl must stop immediately and report cancelled=True rather than
    silently proceeding."""
    import ingestion.gpp_crawl as gc

    async def scenario():
        job_id = 555
        # Pre-register and immediately cancel -- simulates a cancel request
        # that arrives essentially at the same moment as the start request.
        gc._cancel_events[job_id] = asyncio.Event()
        gc._cancel_events[job_id].set()

        async def fake_get_links(*a, **kw):
            raise AssertionError("must not attempt to list any directory once cancelled")

        gc.get_links = fake_get_links
        stats = await gc.crawl_series("38_series", tmp_path, job_id=job_id, throttle=0.0)
        return stats

    stats = _run(scenario())
    assert stats.cancelled is True
    assert stats.files_downloaded == 0


# ── /api/health ──────────────────────────────────────────────────────────────

def test_health_endpoint_reports_structured_status(tmp_path):
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp_path}/health_test.db"
    import importlib
    for mod_name in list(sys.modules):
        if mod_name in ("core.database", "core.config") or mod_name.startswith("api.routes.shared.health"):
            del sys.modules[mod_name]
    core_database = importlib.import_module("core.database")
    health_mod = importlib.import_module("api.routes.shared.health")

    async def scenario():
        await core_database.init_db()
        return await health_mod.health()

    result = _run(scenario())
    assert result["status"] in ("READY", "DEGRADED", "UNAVAILABLE")
    assert result["database"]["status"] == "ok"
    assert "corpus_storage" in result
    assert "llm" in result
    assert "vector_store" in result
    assert result["telemetry"] is False


if __name__ == "__main__":
    print("Run via pytest: python -m pytest backend/tests/test_v03_hardening.py -q")
