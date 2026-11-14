"""
Patent Title Lookup
====================
Best-effort, multi-source resolver for REAL patent titles, used to backfill
`explicit_data.title` when the LLM extraction found none in the source PDF
(the common case — ETSI IPR Annex tables usually give application/publication
number, applicant, and standard, but not the title).

This is a genuinely fallible lookup against external databases, not a
guaranteed answer. Read the limitations below before wiring it into the
pipeline.

LIMITATIONS (read this first)
------------------------------
1. Most application numbers in a 2024+ IPR declaration are recent filings.
   Patent offices publish an application (title becomes public) roughly
   18 months after its earliest priority date, under WIPO/PCT and most
   national regimes. A large share of very recent numbers will have NO
   public title anywhere yet. A "not_published_yet" result is expected,
   not a bug.
2. ETSI's own IPR database (ipr.etsi.org) does store titles internally and
   lets you search by number, but its robots.txt disallows automated
   fetching and there's no public JSON API for it. It is NOT used here —
   scraping it would violate the site's stated crawling policy.
3. EPO OPS is the primary source (see below) because, via INPADOC, it
   resolves applications from many national offices (EP, WO/PCT, GB, IN,
   FI, ...), not only EPO-filed ones — which matches the IN/US/GB/PCT/FI
   spread typical of an ETSI declaration. It requires a free, registered
   OAuth2 key (see SETUP below). Fair-use cap for non-paying accounts is
   4 GB/week — comfortably enough for a few thousand biblio lookups, but
   the client below throttles conservatively regardless.
4. USPTO PatentsView is used as a fallback specifically for "US..."
   numbers. It's official, free, and needs no key — but is US-only.
5. Google Patents has no official public API. The `/xhr/result?id=...`
   endpoint used here is UNDOCUMENTED and used by many scrapers, but Google
   can change or block it at any time without notice. It is OFF by default
   (`use_google_patents_fallback=False`) — enable it only if you accept
   that it's the least reliable, least "sanctioned" source in this cascade,
   and never rely on it at meaningful scale or in a production path.
6. This module has not been executed against live traffic in the environment
   it was written in (network egress there is restricted to package
   registries). Test it against 2-3 known application numbers before
   trusting it on your full ~20,000-PDF corpus.

SETUP
-----
1. Register a free account: https://developers.epo.org -> "Register" ->
   choose the free ("non-paying") consumer key tier. Confirm by email.
2. pip install python-epo-ops-client httpx --break-system-packages
3. Set environment variables:
     export EPO_OPS_KEY="your-consumer-key"
     export EPO_OPS_SECRET="your-consumer-secret"

USAGE
-----
    from core.config import settings
    from research.patent_lookup import PatentTitleLookup

    lookup = PatentTitleLookup(
        cache_path="patent_title_cache.sqlite3",
        epo_ops_key=settings.EPO_OPS_KEY,
        epo_ops_secret=settings.EPO_OPS_SECRET,
    )
    result = await lookup.resolve("2411709.5", country_hint="GB")
    # result.title, result.source, result.status

    # Batch-fill only the disclosures whose LLM-extracted title is empty:
    await lookup.fill_missing_titles(disclosures)
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sqlite3
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

_OPS_NS = {"ops": "http://ops.epo.org", "epo": "http://www.epo.org/exchange"}
_CACHE_MISS_TTL_SECONDS = 7 * 24 * 3600  # don't re-try a genuine miss for a week


@dataclass
class TitleResult:
    """Outcome of one title lookup: the number asked for, the title (if any), which service answered
    and a status.
    """
    number: str
    title: Optional[str]
    source: str  # "epo_ops" | "uspto_patentsview" | "google_patents" | "none"
    status: str  # "found" | "not_published_yet" | "not_found" | "error"
    detail: str = ""


# ── Number normalization helpers ────────────────────────────────────────────
# ETSI Annex tables mix formats: bare PCT (PCT/IB2024/012345), UK-style
# (2411709.5), US provisional (63/680651), or plain country-prefixed digits
# (IN202411070606). These map to EPO OPS "original"-format country/number
# pairs reasonably well; exact kind-code guessing is deliberately NOT
# attempted — OPS's "original" reference type is tolerant of the number as
# filed and does the heavy lifting itself.

_PCT_RE = re.compile(r"PCT/([A-Z]{2})(\d{4})/(\d+)")
_COUNTRY_PREFIX_RE = re.compile(r"^([A-Z]{2})\s*[- ]?\s*(\d.*)$")


def guess_country(raw_number: str, declared_country: str = "") -> Optional[str]:
    """Best-effort ISO country code for an application number, used to pick
    the right EPO OPS 'original' query. Falls back to the declared country
    field from the IPR table (e.g. "UNITED KINGDOM" -> "GB") when the number
    itself carries no prefix."""
    raw_number = (raw_number or "").strip()
    m = _COUNTRY_PREFIX_RE.match(raw_number)
    if m:
        return m.group(1).upper()
    if _PCT_RE.match(raw_number):
        return "WO"
    country_map = {
        "UNITED KINGDOM": "GB", "GREAT BRITAIN": "GB",
        "UNITED STATES": "US", "USA": "US",
        "INDIA": "IN", "FINLAND": "FI", "CHINA": "CN",
        "GERMANY": "DE", "FRANCE": "FR", "JAPAN": "JP",
        "SOUTH KOREA": "KR", "KOREA": "KR",
    }
    return country_map.get((declared_country or "").strip().upper())


def normalize_for_ops(raw_number: str, declared_country: str = "") -> tuple[str, str]:
    """Return (country_code, cleaned_number) suitable for
    epo_ops.models.Original(number=cleaned_number, country_code=country_code)."""
    raw_number = (raw_number or "").strip()
    m = _PCT_RE.match(raw_number)
    if m:
        cc, year, seq = m.groups()
        return "WO", f"{year}/{seq}" if cc == "IB" else f"{cc}{year}/{seq}"
    country = guess_country(raw_number, declared_country) or ""
    number = re.sub(rf"^{country}\s*[- ]?\s*", "", raw_number) if country else raw_number
    return country, number


# ── Local cache (SQLite; avoids re-querying the same number every run) ──────

class _Cache:
    """SQLite cache of title lookups so a number is only fetched from an online service once."""
    def __init__(self, path: str):
        """Open/create the cache database and table."""
        self._conn = sqlite3.connect(path)
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS title_cache (
                 number TEXT PRIMARY KEY,
                 title TEXT,
                 source TEXT,
                 status TEXT,
                 detail TEXT,
                 fetched_at REAL
               )"""
        )
        self._conn.commit()

    def get(self, number: str) -> Optional[TitleResult]:
        """Cached result for a number, or None."""
        row = self._conn.execute(
            "SELECT title, source, status, detail, fetched_at FROM title_cache WHERE number = ?",
            (number,),
        ).fetchone()
        if not row:
            return None
        title, source, status, detail, fetched_at = row
        if status in ("not_found", "error") and time.time() - fetched_at > _CACHE_MISS_TTL_SECONDS:
            return None  # stale miss — allow a retry
        return TitleResult(number=number, title=title, source=source, status=status, detail=detail)

    def put(self, result: TitleResult) -> None:
        """Store/replace a lookup result."""
        self._conn.execute(
            "INSERT OR REPLACE INTO title_cache VALUES (?, ?, ?, ?, ?, ?)",
            (result.number, result.title, result.source, result.status, result.detail, time.time()),
        )
        self._conn.commit()


# ── EPO OPS (primary) ────────────────────────────────────────────────────────

class _EpoOpsClient:
    """Thin OAuth2 wrapper around EPO OPS's published-data biblio endpoint.
    Uses raw httpx rather than python-epo-ops-client so this file has no
    hard import dependency — swap in the official client if you prefer its
    quota/retry handling."""

    TOKEN_URL = "https://ops.epo.org/3.2/auth/accesstoken"
    BASE_URL = "https://ops.epo.org/3.2/rest-services"

    def __init__(self, key: str, secret: str):
        """EPO OPS client with OAuth2 client credentials and a ~3 requests/s throttle."""
        self._key = key
        self._secret = secret
        self._token: Optional[str] = None
        self._token_expiry = 0.0
        self._client = httpx.AsyncClient(timeout=20.0)
        self._min_interval = 0.3  # ~3 req/s, well under fair-use limits
        self._last_call = 0.0

    async def _throttle(self):
        """Sleep as needed to keep at least `_min_interval` between calls."""
        elapsed = time.time() - self._last_call
        if elapsed < self._min_interval:
            await asyncio.sleep(self._min_interval - elapsed)
        self._last_call = time.time()

    async def _get_token(self) -> str:
        """Cached OAuth2 access token, refreshed shortly before it expires."""
        if self._token and time.time() < self._token_expiry:
            return self._token
        resp = await self._client.post(
            self.TOKEN_URL,
            data={"grant_type": "client_credentials"},
            auth=(self._key, self._secret),
        )
        resp.raise_for_status()
        data = resp.json()
        self._token = data["access_token"]
        self._token_expiry = time.time() + int(data.get("expires_in", 1200)) - 60
        return self._token

    async def lookup_application_title(self, country: str, number: str) -> TitleResult:
        """English title of an application from EPO OPS published-data biblio; distinguishes 'not
        published yet', 'not found' and errors.
        """
        combined = f"{country}{number}" if country else number
        if not country or not number:
            return TitleResult(combined, None, "epo_ops", "error", "could not normalize number")

        await self._throttle()
        token = await self._get_token()
        # "original" format: OPS accepts the number roughly as filed, with
        # the country supplied separately.
        url = f"{self.BASE_URL}/published-data/application/original/{country}.({number})/biblio"
        try:
            resp = await self._client.get(
                url, headers={"Authorization": f"Bearer {token}", "Accept": "application/xml"}
            )
        except httpx.HTTPError as e:
            return TitleResult(combined, None, "epo_ops", "error", str(e))

        if resp.status_code == 404:
            return TitleResult(combined, None, "epo_ops", "not_found", "no record for this number")
        if resp.status_code != 200:
            return TitleResult(combined, None, "epo_ops", "error", f"HTTP {resp.status_code}")

        try:
            root = ET.fromstring(resp.text)
            title_el = root.find(".//epo:invention-title[@lang='en']", _OPS_NS)
            if title_el is None:
                title_el = root.find(".//epo:invention-title", _OPS_NS)
            title = title_el.text.strip() if title_el is not None and title_el.text else None
        except ET.ParseError as e:
            return TitleResult(combined, None, "epo_ops", "error", f"XML parse error: {e}")

        if title:
            return TitleResult(combined, title, "epo_ops", "found")
        return TitleResult(combined, None, "epo_ops", "not_published_yet",
                            "record exists but no title published yet")

    async def close(self):
        """Close the HTTP client."""
        await self._client.aclose()


# ── USPTO PatentsView (US-only fallback) ────────────────────────────────────

class _PatentsViewClient:
    """USPTO PatentsView search (granted US patents only)."""
    SEARCH_URL = "https://search.patentsview.org/api/v1/patent/"

    def __init__(self):
        """HTTP client with a 20 s timeout."""
        self._client = httpx.AsyncClient(timeout=20.0)

    async def lookup_by_application_number(self, app_number: str) -> TitleResult:
        """Title of a granted US patent by application number (pending applications are not in
        PatentsView).
        """
        # PatentsView indexes GRANTED patents by application number; a
        # pending, unpublished US application will not appear here either.
        query = {"_eq": {"application.application_number": app_number}}
        try:
            resp = await self._client.get(
                self.SEARCH_URL,
                params={"q": str(query).replace("'", '"'), "f": '["patent_title"]'},
            )
        except httpx.HTTPError as e:
            return TitleResult(app_number, None, "uspto_patentsview", "error", str(e))

        if resp.status_code != 200:
            return TitleResult(app_number, None, "uspto_patentsview", "error", f"HTTP {resp.status_code}")

        data = resp.json()
        patents = data.get("patents") or []
        if patents and patents[0].get("patent_title"):
            return TitleResult(app_number, patents[0]["patent_title"], "uspto_patentsview", "found")
        return TitleResult(app_number, None, "uspto_patentsview", "not_found",
                            "not in PatentsView (unpublished, pending, or non-US)")

    async def close(self):
        """Close the HTTP client."""
        await self._client.aclose()


# ── Google Patents (optional, fragile, OFF by default) ──────────────────────

class _GooglePatentsFallback:
    """UNOFFICIAL. Uses an undocumented endpoint scrapers commonly rely on.
    No SLA, no ToS coverage, can break at any time. Only enable this if you
    understand and accept that."""

    def __init__(self):
        """HTTP client for the optional Google Patents fallback (off by default)."""
        self._client = httpx.AsyncClient(timeout=20.0, headers={"User-Agent": "Mozilla/5.0"})

    async def lookup(self, publication_number: str) -> TitleResult:
        """Title from Google Patents' result endpoint by publication number."""
        url = f"https://patents.google.com/xhr/result?id=patent/{publication_number}/en"
        try:
            resp = await self._client.get(url)
        except httpx.HTTPError as e:
            return TitleResult(publication_number, None, "google_patents", "error", str(e))
        if resp.status_code != 200:
            return TitleResult(publication_number, None, "google_patents", "not_found", f"HTTP {resp.status_code}")
        m = re.search(r'"title"\s*:\s*"([^"]+)"', resp.text)
        if m:
            title = m.group(1).encode().decode("unicode_escape")
            return TitleResult(publication_number, title, "google_patents", "found")
        return TitleResult(publication_number, None, "google_patents", "not_found", "no title in response")

    async def close(self):
        """Close the HTTP client."""
        await self._client.aclose()


# ── Orchestrator ─────────────────────────────────────────────────────────────

class PatentTitleLookup:
    """Title lookup facade: cache first, then EPO OPS (when credentials are configured), PatentsView
    and optionally Google Patents. Only used when explicitly enabled - CPARAS runs offline by
    default and never calls these services otherwise.
    """
    def __init__(self, cache_path: str = "patent_title_cache.sqlite3",
                 use_google_patents_fallback: bool = False,
                 epo_ops_key: str = "", epo_ops_secret: str = ""):
        """Configure the cache path, credentials (explicit, else environment) and whether the Google
        fallback is allowed.
        """
        # Falls back to os.environ only if the caller didn't pass credentials
        # explicitly — but the recommended path is to pass settings.EPO_OPS_KEY
        # / settings.EPO_OPS_SECRET from core.config, since pydantic-settings
        # reads .env into the Settings object WITHOUT exporting it to
        # os.environ, so relying on os.environ alone will silently no-op here.
        key = epo_ops_key or os.environ.get("EPO_OPS_KEY", "")
        secret = epo_ops_secret or os.environ.get("EPO_OPS_SECRET", "")
        self._ops = _EpoOpsClient(key, secret) if key and secret else None
        if not self._ops:
            logger.warning(
                "EPO_OPS_KEY/EPO_OPS_SECRET not set — EPO OPS lookups (the "
                "primary, highest-coverage source for IN/GB/FI/PCT numbers) "
                "are disabled. Register free at https://developers.epo.org"
            )
        self._patentsview = _PatentsViewClient()
        self._google = _GooglePatentsFallback() if use_google_patents_fallback else None
        self._cache = _Cache(cache_path)

    async def resolve(self, raw_number: str, country_hint: str = "") -> TitleResult:
        """Try to resolve a real title for one application/publication number."""
        if not raw_number or not raw_number.strip():
            return TitleResult(raw_number, None, "none", "error", "empty number")

        cached = self._cache.get(raw_number)
        if cached:
            return cached

        result: Optional[TitleResult] = None

        if self._ops:
            country, number = normalize_for_ops(raw_number, country_hint)
            result = await self._ops.lookup_application_title(country, number)
            if result.status == "found":
                self._cache.put(result)
                return result

        if raw_number.upper().startswith("US") or country_hint.upper() == "US":
            pv_result = await self._patentsview.lookup_by_application_number(raw_number)
            if pv_result.status == "found":
                self._cache.put(pv_result)
                return pv_result
            result = result or pv_result

        if self._google:
            g_result = await self._google.lookup(raw_number)
            if g_result.status == "found":
                self._cache.put(g_result)
                return g_result
            result = result or g_result

        result = result or TitleResult(raw_number, None, "none", "error", "no source available")
        self._cache.put(result)
        return result

    async def fill_missing_titles(self, disclosures: list[dict]) -> dict:
        """Mutates `disclosures` in place: for each disclosure whose
        `explicit_data.title` is empty, tries to resolve a real title from
        its application/publication number and fills it in. Never overwrites
        a title the LLM already extracted from the source PDF. Returns a
        small summary dict for logging."""
        summary = {"found": 0, "not_published_yet": 0, "not_found": 0, "error": 0, "skipped": 0}
        for disc in disclosures:
            ed = disc.get("explicit_data", {}) or {}
            if ed.get("title"):
                summary["skipped"] += 1
                continue
            number = ed.get("application_number") or ed.get("publication_number") or ""
            if not number:
                summary["skipped"] += 1
                continue
            result = await self.resolve(number, country_hint=ed.get("country", ""))
            summary[result.status if result.status in summary else "error"] += 1
            if result.title:
                ed["title"] = result.title
                ed["_title_source"] = result.source  # provenance, for transparency in the UI/report
                disc["explicit_data"] = ed
        return summary

    async def close(self):
        """Close every HTTP client."""
        if self._ops:
            await self._ops.close()
        await self._patentsview.close()
        if self._google:
            await self._google.close()
