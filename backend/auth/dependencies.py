"""
FastAPI request guards — who is calling, and may they call this route?
=======================================================================

``enforce_access_policy`` is installed once for the whole app
(``FastAPI(dependencies=[Depends(enforce_access_policy)])`` in ``main.py``), so
every route — existing and future — is checked against ``auth.policy`` before
its handler runs. Route modules therefore need no per-endpoint decorators;
handlers that want the caller use ``Depends(current_user)``.

Credentials accepted
    * the ``AUTH_COOKIE_NAME`` cookie (HttpOnly, SameSite=Strict) set by
      ``POST /api/auth/login`` — what the browser UI uses, and why plain
      ``<a href="/api/...">`` downloads keep working;
    * ``Authorization: Bearer <token>`` — for scripts and for a larger platform
      calling CPARAS service-to-service (the same token ``/api/auth/login``
      returns in its JSON body).

Cross-site request forgery
    The cookie is SameSite=Strict, so browsers do not attach it to requests
    started by other sites. As a second layer, state-changing requests that
    authenticate with the cookie must come from an allowed Origin (this app's
    own frontends, CORS_EXTRA_ORIGINS, or the same host the request was sent
    to). Bearer-token requests are not subject to this check (browsers never
    add that header on their own).

Responses
    401 ``{"detail": "<message>"}`` + header ``X-CPARAS-Auth: auth_required``  not signed in / expired
    403 ``{"detail": "<message>"}`` + header ``X-CPARAS-Auth: forbidden``      role lacks permission
    403 ``{"detail": "<message>"}`` + header ``X-CPARAS-Auth: bad_origin``     cross-site write refused
    ``detail`` stays a plain string so every existing UI error display shows it
    as-is; the frontend's HTTP interceptor uses the header to send an expired
    session back to the sign-in page.
"""

from __future__ import annotations

import logging
from typing import Optional
from urllib.parse import urlparse

from fastapi import HTTPException, Request

from auth.policy import AUTHENTICATED, PUBLIC, required_permission
from auth.roles import permissions_for

logger = logging.getLogger(__name__)

_UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}

PERMISSION_LABELS = {
    "isld.use": "CPARAS ISLD",
    "bulk.use": "CPARAS Bulk",
    "reference.view": "the reference data (3GPP Retrieval Index / Standards Corpus)",
    "reference.manage": "reference-data administration",
    "models.pull": "downloading LLM models",
    "users.manage": "user administration",
}


def _settings():
    """Current settings (lazy import: tests reload core.config)."""
    from core.config import settings
    return settings


def client_ip(request: Request) -> Optional[str]:
    """The direct peer address. X-Forwarded-For is deliberately NOT trusted
    (an offline install has no trusted proxy); a deployment behind one can
    put uvicorn's --proxy-headers in front instead."""
    return request.client.host if request.client else None


def token_from_request(request: Request) -> tuple[Optional[str], str]:
    """``(token, source)`` where source is "bearer", "cookie" or "none"."""
    auth = request.headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip() or None, "bearer"
    tok = request.cookies.get(_settings().AUTH_COOKIE_NAME)
    return (tok, "cookie") if tok else (None, "none")


def allowed_origins() -> set[str]:
    """Browser origins allowed to send cookie-authenticated writes (the same
    list main.py gives CORS)."""
    s = _settings()
    base = {f"http://{h}:{p}" for h in ("localhost", "127.0.0.1") for p in (5173, 4173, 3000)}
    return base | {o.strip().rstrip("/") for o in s.CORS_EXTRA_ORIGINS.split(",") if o.strip()}


def _origin_ok(request: Request) -> bool:
    """Same-host or allow-listed Origin (falls back to Referer). Requests with
    neither header (non-browser clients) pass — SameSite=Strict already keeps
    other sites' requests from carrying the cookie."""
    origin = request.headers.get("origin")
    if not origin:
        ref = request.headers.get("referer")
        if not ref:
            return True
        u = urlparse(ref)
        origin = f"{u.scheme}://{u.netloc}"
    origin = origin.rstrip("/")
    if origin in allowed_origins():
        return True
    host = (request.headers.get("x-forwarded-host") or request.headers.get("host") or "").split(",")[0].strip()
    return bool(host) and urlparse(origin).netloc == host


async def resolve_user(request: Request):
    """Return ``(user, session)`` for the request's credentials, or ``None``.
    Uses its own short DB session so the guard never shares (or commits) the
    route handler's transaction. Cached on ``request.state``."""
    if hasattr(request.state, "auth_resolved"):
        return request.state.auth_resolved
    token, source = token_from_request(request)
    result = None
    if token:
        from core.database import AsyncSessionLocal
        from auth.service import resolve_session
        async with AsyncSessionLocal() as db:
            result = await resolve_session(db, token)
    request.state.auth_resolved = result
    request.state.auth_source = source
    if result:
        request.state.user = result[0]
        request.state.session_token_hash = result[1].token_hash
    return result


def auth_error(status: int, message: str, code: str, **extra_headers: str) -> HTTPException:
    """HTTPException with a string ``detail`` and the ``X-CPARAS-Auth`` code header."""
    headers = {"X-CPARAS-Auth": code, **extra_headers}
    if status == 401:
        headers["WWW-Authenticate"] = "Bearer"
    return HTTPException(status, detail=message, headers=headers)


async def enforce_access_policy(request: Request) -> None:
    """App-wide guard: allow, 401 or 403 according to ``auth.policy``."""
    route = request.scope.get("route")
    path = getattr(route, "path", None) or request.url.path
    permission, _rule = required_permission(request.method, path)
    if permission == PUBLIC:
        return
    resolved = await resolve_user(request)
    if not resolved:
        raise auth_error(401, "Please sign in to use CPARAS (your session may have expired).", "auth_required")
    user, _sess = resolved
    if request.method in _UNSAFE and request.state.auth_source == "cookie" and not _origin_ok(request):
        logger.warning(f"auth: blocked cross-origin {request.method} {path} from {request.headers.get('origin')}")
        raise auth_error(403, "Cross-origin request refused.", "bad_origin")
    if permission == AUTHENTICATED or permission in permissions_for(user.role):
        return
    what = PERMISSION_LABELS.get(permission, permission)
    raise auth_error(403, f"Your role ({user.role.replace('_', ' ')}) does not have access to {what}.", "forbidden",
                     **{"X-CPARAS-Permission": permission})


async def current_user(request: Request):
    """Route dependency: the signed-in user (the policy guard has already
    authenticated the request; this just returns the cached object)."""
    resolved = await resolve_user(request)
    if not resolved:
        raise auth_error(401, "Please sign in to use CPARAS.", "auth_required")
    return resolved[0]
