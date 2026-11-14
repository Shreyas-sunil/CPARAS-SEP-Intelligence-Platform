"""
API access policy — which permission every route needs
=======================================================

One table, ``RULES``, maps (HTTP method, route-path prefix) to the permission
required to call it. ``auth.dependencies.enforce_access_policy`` is installed
as an app-wide FastAPI dependency (``main.py``), looks up the *matched route's
path template* (e.g. ``/api/sep/runs/{run_id}/status``) here and refuses the
request unless the signed-in user's role grants that permission.

Rules are checked in order; the first whose method set contains the request
method and whose prefix matches wins. A route that matches NO rule requires
``users.manage`` (super admin) — deny-by-default, so a newly added endpoint is
never accidentally exposed to guests. ``tests/test_v050_auth.py`` fails if any
registered route is unclassified, so new endpoints get an explicit decision.

Special requirements:
    PUBLIC         no sign-in needed (health check, login, first-run setup)
    AUTHENTICATED  any active signed-in user (own profile, logout, own password)

Integration point: when CPARAS is mounted inside a larger platform, this table
is the place to translate the platform's own authorization model — e.g. map an
upstream "patent-analyst" claim to ``bulk.use``. Keep prefixes in sync with the
``include_router(prefix=...)`` calls in ``main.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from auth.roles import (
    BULK_USE, ISLD_USE, MODELS_PULL, REFERENCE_MANAGE, REFERENCE_VIEW, USERS_MANAGE,
)

PUBLIC = "public"
AUTHENTICATED = "authenticated"

READ = frozenset({"GET", "HEAD", "OPTIONS"})
ANY = frozenset({"GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE"})
WRITE = ANY - READ


@dataclass(frozen=True)
class Rule:
    """``methods`` × path-template ``prefix`` → ``permission``. ``exact``
    rules match only the full template (used for single public endpoints)."""
    methods: frozenset
    prefix: str
    permission: str
    exact: bool = False

    def matches(self, method: str, path: str) -> bool:
        """Whether this rule applies to `method` on the route template `path` (prefix match on whole
        path segments, or exact).
        """
        if method not in self.methods:
            return False
        if self.exact:
            return path == self.prefix
        return path == self.prefix or path.startswith(self.prefix.rstrip("/") + "/")


RULES: tuple[Rule, ...] = (
    # ── Public ────────────────────────────────────────────────────────────
    Rule(READ, "/api/health", PUBLIC, exact=True),
    Rule(frozenset({"POST"}), "/api/auth/login", PUBLIC, exact=True),
    Rule(READ, "/api/auth/setup-status", PUBLIC, exact=True),
    Rule(frozenset({"POST"}), "/api/auth/setup", PUBLIC, exact=True),

    # ── Own account ───────────────────────────────────────────────────────
    Rule(ANY, "/api/auth/me", AUTHENTICATED, exact=True),
    Rule(frozenset({"POST"}), "/api/auth/logout", AUTHENTICATED, exact=True),
    Rule(frozenset({"POST"}), "/api/auth/password", AUTHENTICATED, exact=True),
    Rule(READ, "/api/auth/roles", AUTHENTICATED, exact=True),

    # ── Administration ────────────────────────────────────────────────────
    Rule(ANY, "/api/auth/users", USERS_MANAGE),
    Rule(ANY, "/api/auth/events", USERS_MANAGE),

    # ── CPARAS ISLD ───────────────────────────────────────────────────────
    Rule(ANY, "/api/analysis", ISLD_USE),
    Rule(ANY, "/api/text-match", ISLD_USE),
    # Local-LLM status/recommendations/smoke test are shown inside ISLD.
    Rule(frozenset({"POST"}), "/api/models/pull", MODELS_PULL),
    Rule(ANY, "/api/models", ISLD_USE),

    # ── CPARAS Bulk ───────────────────────────────────────────────────────
    Rule(ANY, "/api/sep", BULK_USE),

    # ── Shared reference data (3GPP Retrieval Index, Standards Corpus) ────
    Rule(READ, "/api/retrieval-index", REFERENCE_VIEW),
    Rule(ANY, "/api/search", REFERENCE_VIEW),                 # semantic search is read-only (POST body)
    Rule(READ, "/api/etsi-corpus/package/download", REFERENCE_MANAGE, exact=True),
    Rule(READ, "/api/etsi-corpus/export", REFERENCE_MANAGE, exact=True),
    Rule(READ, "/api/etsi-corpus", REFERENCE_VIEW),
    Rule(WRITE, "/api/etsi-corpus", REFERENCE_MANAGE),
    Rule(READ, "/api/scrape/export/files", REFERENCE_MANAGE, exact=True),
    Rule(READ, "/api/scrape", REFERENCE_VIEW),
    Rule(WRITE, "/api/scrape", REFERENCE_MANAGE),
    Rule(READ, "/api/datasets", REFERENCE_VIEW),
    Rule(WRITE, "/api/datasets", REFERENCE_MANAGE),
)

DEFAULT_PERMISSION = USERS_MANAGE  # anything unclassified: super admin only


def required_permission(method: str, path_template: str) -> tuple[str, Optional[Rule]]:
    """Permission needed for ``method`` on ``path_template`` (the matched
    route's template, not the concrete URL) and the rule that decided it
    (``None`` = fell through to ``DEFAULT_PERMISSION``)."""
    m = (method or "GET").upper()
    for rule in RULES:
        if rule.matches(m, path_template):
            return rule.permission, rule
    return DEFAULT_PERMISSION, None
