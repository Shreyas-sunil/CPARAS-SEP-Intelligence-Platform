"""
Roles and permissions — the single source of truth for CPARAS RBAC
====================================================================

CPARAS has three roles. Each role is a fixed set of *permissions*; every API
route is mapped to exactly one permission by ``auth.policy``, and the frontend
receives the caller's permission list from ``GET /api/auth/me`` and hides what
the user may not use. Authorization is therefore decided in ONE place on the
server, never by the browser.

    Role          Permissions
    -----------   -------------------------------------------------------------
    guest         isld.use
    data_entry    isld.use, bulk.use, reference.view, models.pull
    super_admin   everything above + reference.manage, users.manage

Permission meanings:

    isld.use             CPARAS ISLD — declaration analysis, history, ISLD Excel
                         export, Text Matching; read the local-LLM status.
    bulk.use             CPARAS Bulk — upload exports, runs, dashboards, registry,
                         patent/company detail, enriched Excel export.
    reference.view       Read the shared reference data: 3GPP Retrieval Index,
                         Standards Corpus status/listings, semantic search.
    reference.manage     Change the reference data: dataset imports, corpus
                         packages, 3GPP crawls/indexing jobs (administrative).
    models.pull          Download (``ollama pull``) a local LLM model.
    users.manage         User and role management, authentication audit log.

Integration point: a larger platform that federates CPARAS can map its own
roles onto these permission strings (they are stable API), or extend
``ROLE_PERMISSIONS``; nothing else in CPARAS hard-codes a role name except the
"keep at least one active super admin" rule in ``auth.service``.
"""

from __future__ import annotations

from enum import Enum


class Role(str, Enum):
    """The three CPARAS roles. Stored in ``users.role`` as the enum value."""

    GUEST = "guest"
    DATA_ENTRY = "data_entry"
    SUPER_ADMIN = "super_admin"


# Permission identifiers (stable strings: used by the backend policy, returned
# by /api/auth/me, and checked by the frontend).
ISLD_USE = "isld.use"
BULK_USE = "bulk.use"
REFERENCE_VIEW = "reference.view"
REFERENCE_MANAGE = "reference.manage"
MODELS_PULL = "models.pull"
USERS_MANAGE = "users.manage"

ALL_PERMISSIONS: frozenset[str] = frozenset({
    ISLD_USE, BULK_USE, REFERENCE_VIEW, REFERENCE_MANAGE, MODELS_PULL, USERS_MANAGE,
})

ROLE_PERMISSIONS: dict[Role, frozenset[str]] = {
    Role.GUEST: frozenset({ISLD_USE}),
    Role.DATA_ENTRY: frozenset({ISLD_USE, BULK_USE, REFERENCE_VIEW, MODELS_PULL}),
    Role.SUPER_ADMIN: ALL_PERMISSIONS,
}

ROLE_LABELS: dict[Role, str] = {
    Role.GUEST: "Guest",
    Role.DATA_ENTRY: "Data Entry",
    Role.SUPER_ADMIN: "Super Admin",
}

ROLE_DESCRIPTIONS: dict[Role, str] = {
    Role.GUEST: "CPARAS ISLD only (declaration analysis and text matching).",
    Role.DATA_ENTRY: "CPARAS ISLD and CPARAS Bulk, including uploads, runs and exports.",
    Role.SUPER_ADMIN: "Everything, plus user/role management and reference-data administration.",
}


def parse_role(value: str) -> Role:
    """Return the ``Role`` for a stored/requested value; raises ``ValueError``
    for anything that is not exactly one of the three role identifiers."""
    try:
        return Role(str(value).strip().lower())
    except ValueError:
        raise ValueError(f"Unknown role '{value}'. Valid roles: {', '.join(r.value for r in Role)}.") from None


def permissions_for(role: str | Role) -> frozenset[str]:
    """Permissions granted to ``role`` (empty for an unknown role — fail closed)."""
    try:
        return ROLE_PERMISSIONS[role if isinstance(role, Role) else Role(role)]
    except (ValueError, KeyError):
        return frozenset()


def landing_path(role: str | Role) -> str:
    """Where the frontend sends a user right after login. Guests go straight
    to ISLD Declaration Analysis; everyone else lands on the platform home."""
    return "/isld" if BULK_USE not in permissions_for(role) else "/"
