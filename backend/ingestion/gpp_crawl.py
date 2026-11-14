"""
3GPP Archive Crawler (backend-integrated)
===========================================
Adapts the path-safety-hardened, tested crawling logic from the standalone
`3gpp_archive_crawler.py` tool (URL->local-path mapping, Windows-safe
sanitization, directory-vs-file detection tuned to 3GPP's actual listing
markup) for use as a backend job the frontend's Scraper panel can trigger,
one 3GPP series at a time, with progress reported back through
`core.database.SpecCrawlJob`.

This module deliberately reuses the exact tested pure functions
(`sanitize_component`, `url_to_local_path`, `classify_link`) rather than
reimplementing them, to avoid re-introducing the path-traversal / Windows
path bugs that standalone tool's fix addressed. The only real difference
is the HTTP client: this uses `httpx.AsyncClient` (already a dependency
here, see ingestion/pipeline.py) instead of `requests`, so a long crawl
doesn't block the FastAPI event loop.
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Callable, Optional
from urllib.parse import urljoin, urlparse, unquote, parse_qs

import httpx
from bs4 import BeautifulSoup

from core.config import settings

logger = logging.getLogger(__name__)

BASE_URL = settings.THREEGPP_BASE_URL
USER_AGENT = "sep-platform-scraper/1.0 (research tool; polite crawler)"

KNOWN_FILE_EXTENSIONS = {
    ".zip", ".doc", ".docx", ".rtf", ".pdf", ".ppt", ".pptx",
    ".xls", ".xlsx", ".txt", ".htm", ".html", ".csv", ".xml", ".mdb",
}

WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

_ILLEGAL_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
MAX_COMPONENT_LEN = 150

# All top-level 3GPP archive series. "OpenAPI" is a real folder but not a
# numbered series, so it's excluded from the pick-list.
ALL_SERIES = [f"{i:02d}_series" for i in list(range(0, 13)) + list(range(21, 53)) + [55]]


def sanitize_component(name: str) -> str:
    """Make one URL path segment safe as a file/folder name on Windows and POSIX (illegal characters
    replaced, reserved names and trailing dots handled).
    """
    name = unquote(name).strip()
    name = _ILLEGAL_CHARS_RE.sub("_", name)
    name = name.rstrip(". ").strip()
    if name in ("", ".", ".."):
        name = "_"
    stem = name.split(".")[0].upper()
    if stem in WINDOWS_RESERVED_NAMES:
        name = f"_{name}"
    if len(name) > MAX_COMPONENT_LEN:
        p = PurePosixPath(name)
        suffix = p.suffix[:20]
        name = name[: MAX_COMPONENT_LEN - len(suffix)] + suffix
    return name


def url_path_segments(url: str) -> list[str]:
    """Sanitised path segments of a URL ('.' and '..' dropped, so a link can never escape the output
    folder).
    """
    path = urlparse(url).path
    raw = [seg for seg in path.split("/") if seg not in ("", ".", "..")]
    return [sanitize_component(seg) for seg in raw]


def url_to_local_path(url: str, out_root: Path, base_url: str = BASE_URL) -> Path:
    """Local mirror path for a 3GPP archive URL under `out_root`, preserving the archive's folder
    structure.
    """
    base_segments = url_path_segments(base_url)
    full_segments = url_path_segments(url)
    if full_segments[: len(base_segments)] == base_segments:
        rel_segments = full_segments[len(base_segments):]
    else:
        rel_segments = full_segments
    out_root = out_root.resolve()
    local_path = out_root.joinpath(*rel_segments) if rel_segments else out_root
    resolved = local_path.resolve()
    try:
        resolved.relative_to(out_root)
    except ValueError:
        raise ValueError(f"Path safety check failed: {url!r} -> {resolved} outside {out_root}")
    return local_path


def _is_same_directory_query_link(current_url: str, candidate_url: str) -> bool:
    """True for links that only re-sort the same directory listing (?C=M;O=A) - not followed."""
    a, b = urlparse(current_url), urlparse(candidate_url)
    return (a.scheme, a.netloc, a.path.rstrip("/")) == (b.scheme, b.netloc, b.path.rstrip("/"))


def classify_link(a_tag, page_url: str, base_url: str = BASE_URL) -> Optional[tuple[str, str, bool]]:
    """Classify an <a> tag of a directory listing as ('dir'|'file', absolute URL, is-zip) or None for
    navigation/off-site links.
    """
    href = a_tag.get("href")
    if not href:
        return None
    href = href.strip()
    if href in ("", "#", "../", "..", "/") or href.startswith("#"):
        return None
    abs_url = urljoin(page_url, href)
    parsed = urlparse(abs_url)
    if not parsed.path.lower().startswith(urlparse(base_url).path.lower()):
        return None
    if parsed.query and _is_same_directory_query_link(page_url, abs_url):
        return None
    norm_current = urlparse(page_url).path.rstrip("/")
    norm_target = parsed.path.rstrip("/")
    if norm_target == norm_current or norm_current.startswith(norm_target + "/"):
        return None
    segments = [s for s in parsed.path.split("/") if s]
    if not segments:
        return None
    name = unquote(segments[-1])

    is_dir = None
    if href.endswith("/") or parsed.path.endswith("/"):
        is_dir = True
    if is_dir is None:
        img = a_tag.find("img")
        if img is None:
            row = a_tag.find_parent("tr")
            img = row.find("img") if row else None
        if img and img.get("src"):
            icon_qs = parse_qs(urlparse(img["src"]).query)
            icon_ext = icon_qs.get("file", [""])[0]
            is_dir = icon_ext == ""
    if is_dir is None:
        ext = PurePosixPath(name).suffix.lower()
        is_dir = ext not in KNOWN_FILE_EXTENSIONS

    return name, abs_url, is_dir


async def get_links(client: httpx.AsyncClient, url: str, timeout: float, base_url: str = BASE_URL):
    """Fetch one directory listing and return its classified, de-duplicated links."""
    resp = await client.get(url, timeout=timeout)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    links, seen = [], set()
    for a in soup.find_all("a", href=True):
        result = classify_link(a, url, base_url)
        if result is None:
            continue
        name, abs_url, is_dir = result
        if abs_url in seen:
            continue
        seen.add(abs_url)
        links.append((name, abs_url, is_dir))
    return links


def is_valid_zip(path: Path) -> bool:
    """True if `path` exists, is non-empty, and is a structurally valid ZIP
    (readable central directory). Does NOT open/decode every member -- that
    happens lazily wherever the ZIP's contents are actually needed (see
    ingestion/spec_corpus.py) -- this is just the "did the download
    actually produce a usable archive" integrity gate."""
    try:
        if not path.exists() or path.stat().st_size == 0:
            return False
        return zipfile.is_zipfile(path)
    except OSError:
        return False


def safe_extract_zip(zip_path: Path, dest_dir: Path) -> list[Path]:
    """Extract every member of zip_path into dest_dir, rejecting any member
    whose path would resolve outside dest_dir (a "zip slip" / path-
    traversal archive, e.g. a member named "../../evil.txt" or an absolute
    path). Returns the list of extracted file paths.

    Nothing in this codebase currently extracts a downloaded 3GPP archive
    to disk (ingestion/spec_corpus.py reads the .docx member directly out
    of the zip via zipfile.read(), never zipfile.extractall()), so this
    function isn't on the crawl's hot path today -- it exists as the
    defense-in-depth extraction primitive for whenever archive extraction
    IS needed, so that capability can never be added later without this
    protection already in place. Tested directly (see
    tests/test_zip_safety.py) with a deliberately malicious archive.
    """
    dest_dir = dest_dir.resolve()
    dest_dir.mkdir(parents=True, exist_ok=True)
    extracted: list[Path] = []

    with zipfile.ZipFile(zip_path) as zf:
        for member in zf.infolist():
            if member.is_dir():
                continue
            # Reject absolute paths and drive letters outright before even
            # attempting to join -- PurePosixPath treats a leading "/" as
            # absolute, and os.path.join silently ignores everything before
            # an absolute-looking second argument on POSIX, which would
            # otherwise let a member named "/etc/passwd" through unnoticed.
            member_name = member.filename.replace("\\", "/")
            if member_name.startswith("/") or (len(member_name) > 1 and member_name[1] == ":"):
                raise ValueError(f"Unsafe archive member (absolute path): {member.filename!r}")

            target = (dest_dir / member_name).resolve()
            try:
                target.relative_to(dest_dir)
            except ValueError:
                raise ValueError(f"Unsafe archive member (escapes extraction directory): {member.filename!r}")

            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(member) as src, open(target, "wb") as dst:
                dst.write(src.read())
            extracted.append(target)

    return extracted


@dataclass
class CrawlStats:
    """Counters of one crawl (directories visited, files found/downloaded/skipped, bytes, errors)."""
    dirs_visited: int = 0
    files_found: int = 0
    files_downloaded: int = 0
    files_skipped_existing: int = 0
    bytes_downloaded: int = 0
    errors: int = 0
    failed_urls: list = field(default_factory=list)
    cancelled: bool = False


ProgressCallback = Optional[Callable[[CrawlStats, str], "asyncio.Future"]]
FileEventCallback = Optional[Callable[[dict], "asyncio.Future"]]

# Cooperative cancellation: one asyncio.Event per active job_id. crawl_series
# checks this between files/directories and stops scheduling new work once
# set -- it does not forcibly kill an in-flight download, it lets the
# current operation finish/abort cleanly and then stops, so a cancel can
# never corrupt a file that was mid-write. Cleared when the job's crawl
# coroutine returns (success, failure, or cancellation) so the dict doesn't
# grow unboundedly across many jobs over the app's lifetime.
_cancel_events: dict[int, asyncio.Event] = {}


def request_cancel(job_id: int) -> bool:
    """Signal an active crawl to stop. Returns True if a matching active
    job was found to signal, False if job_id isn't currently crawling
    (already finished, or never started) -- the caller (api/routes/scrape.py)
    uses this to distinguish "cancel accepted" from "nothing to cancel"."""
    ev = _cancel_events.get(job_id)
    if ev is None:
        return False
    ev.set()
    return True


def is_cancel_requested(job_id: int) -> bool:
    """Whether a cancel was requested for a crawl job."""
    ev = _cancel_events.get(job_id)
    return ev.is_set() if ev else False

# Windows can briefly hold a file handle open (AV scan, indexer, a second
# process's own retry) even after our own write completed -- this shows up
# as PermissionError / OSError WinError 32 on os.replace()/Path.replace().
# These are almost always transient; retrying briefly is correct, treating
# the first occurrence as a permanent failure is not.
FINALIZE_MAX_RETRIES = 5
FINALIZE_BASE_DELAY = 0.25  # seconds, exponential backoff from here


async def _atomic_finalize(tmp_path: Path, dest_path: Path) -> None:
    """Rename tmp_path -> dest_path, retrying transient Windows file-lock
    errors (WinError 32 and friends) with short exponential backoff rather
    than failing the whole download on the first lock.

    Raises the last error if every retry is exhausted, so the caller can
    still record a clean failure -- this never silently drops the file.
    """
    last_err: Optional[OSError] = None
    for attempt in range(1, FINALIZE_MAX_RETRIES + 1):
        try:
            tmp_path.replace(dest_path)
            return
        except (PermissionError, OSError) as e:
            last_err = e
            if attempt < FINALIZE_MAX_RETRIES:
                delay = FINALIZE_BASE_DELAY * (2 ** (attempt - 1))
                logger.warning(
                    f"finalize attempt {attempt}/{FINALIZE_MAX_RETRIES} failed for "
                    f"{dest_path.name} ({e}); retrying in {delay:.2f}s"
                )
                await asyncio.sleep(delay)
    raise last_err  # noqa: RSE102 -- deliberately re-raising the last captured OSError


async def _download_file(client: httpx.AsyncClient, url: str, dest_path: Path,
                          stats: CrawlStats, timeout: float, max_retries: int, job_id: int,
                          relative_path: str = "", on_file_event: FileEventCallback = None):
    """Download one file with retries and resume-safe temp files (written as *.part.*, renamed when
    complete), skipping files already present with the right size; reports each state change through
    `on_file_event`.
    """
    async def _emit(status: str, **extra):
        """Report a per-file status event (errors in the callback are logged, never fatal)."""
        if on_file_event:
            try:
                await on_file_event({
                    "relative_path": relative_path or dest_path.name,
                    "filename": dest_path.name, "url": url, "status": status,
                    "local_path": str(dest_path), **extra,
                })
            except Exception as e:
                # File-event tracking is pure observability -- it must
                # never take down the crawl itself if a write fails.
                logger.warning(f"file-event callback failed for {dest_path.name}: {e}")

    if dest_path.exists() and dest_path.stat().st_size > 0:
        stats.files_skipped_existing += 1
        await _emit("skipped", file_size=dest_path.stat().st_size)
        return
    dest_path.parent.mkdir(parents=True, exist_ok=True)

    # Job-scoped, run-unique temp filename -- see module docstring. Two
    # concurrent operations (a duplicate job that slipped past
    # deduplication, or a leftover process from an old crawl) can never
    # collide on this name, because it embeds both the job id and a fresh
    # random token generated for this specific download attempt.
    tmp_path = dest_path.with_name(f"{dest_path.name}.part.{job_id}.{uuid.uuid4().hex[:8]}")

    try:
        for attempt in range(1, max_retries + 1):
            await _emit("downloading", retry_count=attempt - 1)
            try:
                async with client.stream("GET", url, timeout=timeout) as r:
                    if r.status_code in (401, 403, 404, 410):
                        # Permanent conditions -- retrying won't help, so
                        # don't burn the retry budget on them.
                        stats.errors += 1
                        stats.failed_urls.append(url)
                        await _emit("failed", error_code=f"HTTP_{r.status_code}",
                                    error_message=f"HTTP {r.status_code} (permanent)", retry_count=attempt)
                        return
                    if r.status_code == 429:
                        # Rate-limited: honor Retry-After if the server
                        # sends one, otherwise back off further than a
                        # normal transient failure would.
                        retry_after = r.headers.get("Retry-After")
                        delay = float(retry_after) if retry_after and retry_after.isdigit() else min(5 * attempt, 30)
                        logger.warning(f"429 rate-limited on {url}, waiting {delay:.0f}s (attempt {attempt}/{max_retries})")
                        if attempt < max_retries:
                            await asyncio.sleep(delay)
                        continue
                    r.raise_for_status()
                    size = 0
                    with open(tmp_path, "wb") as f:
                        async for chunk in r.aiter_bytes(1 << 16):
                            f.write(chunk)
                            size += len(chunk)

                # Finalization is a separate failure mode from the download
                # itself (network vs. local filesystem), so it gets its own
                # retry loop rather than re-downloading the whole file just
                # because the OS briefly held a lock on the destination.
                try:
                    await _atomic_finalize(tmp_path, dest_path)
                except OSError as e:
                    stats.errors += 1
                    stats.failed_urls.append(url)
                    logger.error(f"could not finalize {dest_path.name} after retries: {e}")
                    await _emit("failed", error_code="FINALIZE_FAILED", error_message=str(e), retry_count=attempt)
                    return

                # Integrity check: a ZIP that downloaded without an HTTP
                # error can still be truncated (connection dropped mid-body
                # with no error surfaced) or corrupt. Never count a file
                # that fails this check as a successful download -- delete
                # it and let the retry loop attempt a fresh download rather
                # than leaving a corrupt file in place that a future
                # "already exists, skip" check would then trust forever.
                if dest_path.suffix.lower() == ".zip" and not is_valid_zip(dest_path):
                    logger.warning(f"downloaded file failed ZIP integrity check, retrying: {dest_path.name}")
                    try:
                        dest_path.unlink()
                    except OSError:
                        pass
                    if attempt < max_retries:
                        await asyncio.sleep(min(2 ** attempt, 15))
                    continue

                stats.files_downloaded += 1
                stats.bytes_downloaded += size
                await _emit("downloaded", bytes_downloaded=size, file_size=size, retry_count=attempt - 1)
                return
            except httpx.HTTPError as e:
                logger.warning(f"attempt {attempt}/{max_retries} failed for {url}: {e}")
                if attempt < max_retries:
                    await asyncio.sleep(min(2 ** attempt, 15))
        stats.errors += 1
        stats.failed_urls.append(url)
        await _emit("failed", error_code="DOWNLOAD_FAILED",
                    error_message=f"Failed after {max_retries} attempts", retry_count=max_retries)
    finally:
        # Always clean up our OWN temp file if it's still sitting there
        # (download failed, retries exhausted, or an exception propagated).
        # This never touches another job's temp file -- the job-scoped name
        # above guarantees tmp_path can only ever refer to this attempt's
        # own file.
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass  # best-effort; a stray temp file is cleaned up on next run (see cleanup_stale_temp_files)


def cleanup_stale_temp_files(out_root: Path, max_age_seconds: float = 6 * 3600) -> int:
    """Remove leftover .part.<job>.<token> files older than max_age_seconds.

    A temp file this old almost certainly belongs to a crashed/killed
    process rather than a currently-running job (an active job either
    finishes fast per file or is still writing to it, which keeps its
    mtime fresh) -- so this is safe to call opportunistically before
    starting a new crawl without risking another *active* job's file.
    Returns the number of files removed.
    """
    import time
    if not out_root.exists():
        return 0
    now = time.time()
    removed = 0
    for p in out_root.rglob("*.part.*"):
        try:
            if now - p.stat().st_mtime > max_age_seconds:
                p.unlink()
                removed += 1
        except OSError:
            continue
    return removed


async def crawl_series(
    series: str,
    out_root: Path,
    *,
    job_id: int,
    throttle: float = 0.3,
    timeout: float = 60.0,
    max_retries: int = 3,
    max_files: Optional[int] = None,
    on_progress: ProgressCallback = None,
    on_file_event: FileEventCallback = None,
) -> CrawlStats:
    """Crawl exactly one 3GPP archive series (e.g. "38_series") into
    out_root, mirroring the remote hierarchy. Safe to re-run: existing
    non-empty files are skipped. `job_id` scopes this run's temporary
    download filenames so concurrent/overlapping crawls (a duplicate job
    that slipped past deduplication, or a leftover process) can never
    collide on the same .part file -- see _download_file.

    Cooperatively cancellable via request_cancel(job_id): checked between
    files and between directories, so a cancel finishes the file currently
    being written (never corrupts a partial file into looking complete)
    and then stops scheduling new work. stats.cancelled is True if the
    crawl stopped this way rather than completing/erroring normally.

    `on_file_event`, if given, is called at each per-file state transition
    (queued/downloading/downloaded/skipped/failed) -- see api/routes/
    scrape.py, which uses this to populate the SpecCrawlFile table for the
    frontend's file-level progress list.
    """
    stats = CrawlStats()
    visited: set[str] = set()
    start_url = urljoin(BASE_URL, series)
    cancel_event = _cancel_events.setdefault(job_id, asyncio.Event())

    try:
        removed = cleanup_stale_temp_files(out_root)
        if removed:
            logger.info(f"[job={job_id} series={series}] cleaned up {removed} stale temp file(s) under {out_root}")
    except OSError as e:
        logger.warning(f"[job={job_id} series={series}] stale temp file cleanup skipped: {e}")

    try:
        async with httpx.AsyncClient(headers={"User-Agent": USER_AGENT}, follow_redirects=True) as client:
            async def _crawl_dir(url: str):
                if cancel_event.is_set():
                    stats.cancelled = True
                    return
                norm = url.rstrip("/")
                if norm in visited:
                    return
                visited.add(norm)
                stats.dirs_visited += 1
                if on_progress:
                    await on_progress(stats, url)
                try:
                    links = await get_links(client, url, timeout)
                except httpx.HTTPError as e:
                    logger.warning(f"[job={job_id} series={series}] failed to list {url}: {e}")
                    stats.errors += 1
                    return
                for name, abs_url, is_dir in links:
                    if cancel_event.is_set():
                        stats.cancelled = True
                        return
                    if max_files is not None and stats.files_downloaded >= max_files:
                        return
                    if is_dir:
                        await _crawl_dir(abs_url)
                    else:
                        stats.files_found += 1
                        dest_path = url_to_local_path(abs_url, out_root)
                        try:
                            relative_path = str(dest_path.relative_to(out_root))
                        except ValueError:
                            relative_path = dest_path.name
                        if on_file_event:
                            try:
                                await on_file_event({
                                    "relative_path": relative_path, "filename": dest_path.name,
                                    "url": abs_url, "status": "queued", "local_path": str(dest_path),
                                })
                            except Exception as e:
                                logger.warning(f"file-event (queued) callback failed for {dest_path.name}: {e}")
                        await _download_file(client, abs_url, dest_path, stats, timeout, max_retries, job_id,
                                              relative_path=relative_path, on_file_event=on_file_event)
                        if on_progress:
                            await on_progress(stats, str(dest_path))
                        if throttle:
                            await asyncio.sleep(throttle)

            await _crawl_dir(start_url)
    finally:
        _cancel_events.pop(job_id, None)

    return stats


def default_out_root() -> Path:
    """Where crawled 3GPP files are stored (DATA_DIR/3gpp_crawl)."""
    return settings.DATA_DIR / "3gpp_crawl"
