"""
Authentication API — ``/api/auth`` (v0.5.0)
============================================

    POST /api/auth/login          sign in; sets the HttpOnly session cookie and
                                  also returns the token (for API clients)
    POST /api/auth/logout         end the current session, clear the cookie
    GET  /api/auth/me             the signed-in user, role, permissions, landing page
    POST /api/auth/password       change your own password (signs out your other sessions)
    GET  /api/auth/roles          the role catalogue (labels, descriptions, permissions)
    GET  /api/auth/setup-status   PUBLIC: does CPARAS still need its first super admin?
    POST /api/auth/setup          PUBLIC, one time only: create the first super admin
                                  with the token from AUTH_SETUP_TOKEN_FILE

Which role may call which endpoint is decided by ``auth/policy.py``; the
handlers here only implement the account operations (``auth/service.py``).
User administration for super admins lives in ``api/routes/shared/users.py``.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from auth import service
from auth.dependencies import client_ip, current_user, token_from_request
from auth.roles import ROLE_DESCRIPTIONS, ROLE_LABELS, ROLE_PERMISSIONS, Role
from core.config import settings
from core.database import get_db

router = APIRouter()


class LoginRequest(BaseModel):
    """Body of POST /api/auth/login."""
    username: str = Field(..., max_length=128)
    password: str = Field(..., max_length=256)


class PasswordChangeRequest(BaseModel):
    """Body of POST /api/auth/password (your own password)."""
    current_password: str = Field(..., max_length=256)
    new_password: str = Field(..., max_length=256)


class SetupRequest(BaseModel):
    """Body of POST /api/auth/setup (first super admin, one time only)."""
    setup_token: str = Field(..., max_length=256)
    username: str = Field(..., max_length=128)
    password: str = Field(..., max_length=256)
    display_name: Optional[str] = Field(None, max_length=128)


def _raise(e: service.AuthError):
    """Translate a service-level ``AuthError`` into the HTTP response."""
    raise HTTPException(e.status_code, detail=e.detail)


def _set_session_cookie(response: Response, token: str) -> None:
    """HttpOnly (no JavaScript access), SameSite=Strict (not sent on cross-site
    requests), Path=/api (only sent to the API), Secure when configured."""
    response.set_cookie(
        settings.AUTH_COOKIE_NAME, token, max_age=settings.AUTH_SESSION_MAX_HOURS * 3600,
        httponly=True, samesite="strict", secure=settings.AUTH_COOKIE_SECURE, path="/api",
    )


@router.post("/login")
async def login(body: LoginRequest, request: Request, response: Response, db: AsyncSession = Depends(get_db)):
    """Verify username + password and start a session.

    Returns ``{"user": {...}, "token": "...", "expires_in": seconds}`` and sets
    the session cookie. Errors: 401 wrong credentials (same message whether the
    username or the password was wrong), 403 deactivated account, 429 too many
    failed attempts from this client."""
    ip = client_ip(request)
    try:
        user = await service.authenticate(db, body.username, body.password, ip)
    except service.AuthError as e:
        await db.commit()  # keep the audit record of the failure
        _raise(e)
    token = await service.create_session(db, user, client_ip=ip, user_agent=request.headers.get("user-agent"))
    await db.commit()
    _set_session_cookie(response, token)
    return {"user": service.user_public(user), "token": token, "expires_in": settings.AUTH_SESSION_MAX_HOURS * 3600}


@router.post("/logout")
async def logout(request: Request, response: Response, user=Depends(current_user), db: AsyncSession = Depends(get_db)):
    """End the session this request was made with and clear the cookie."""
    token, _src = token_from_request(request)
    await service.revoke_session(db, token)
    await service.record_event(db, "logout", username=user.username, actor=user.username, client_ip=client_ip(request))
    await db.commit()
    response.delete_cookie(settings.AUTH_COOKIE_NAME, path="/api")
    return {"ok": True}


@router.get("/me")
async def me(user=Depends(current_user)):
    """The signed-in user — role, permission list and post-login landing page."""
    return {"user": service.user_public(user)}


@router.post("/password")
async def change_password(body: PasswordChangeRequest, request: Request, user=Depends(current_user),
                          db: AsyncSession = Depends(get_db)):
    """Change your own password. Requires the current password; signs out all
    of your OTHER sessions (this one stays signed in) and clears the
    "must change password" flag set by an administrator reset."""
    from auth.passwords import verify_password
    fresh = await service.get_user(db, user.id)
    if not verify_password(body.current_password, fresh.password_hash):
        raise HTTPException(403, detail="Your current password is not correct.")
    if body.current_password == body.new_password:
        raise HTTPException(422, detail="Choose a password different from your current one.")
    try:
        await service.set_password(db, fresh, body.new_password, must_change=False,
                                   keep_session_hash=getattr(request.state, "session_token_hash", None))
    except service.AuthError as e:
        _raise(e)
    await service.record_event(db, "password_changed", username=fresh.username, actor=fresh.username,
                               client_ip=client_ip(request))
    await db.commit()
    return {"ok": True, "user": service.user_public(fresh)}


@router.get("/roles")
async def roles():
    """Role catalogue for the UI (user-management form, help text)."""
    return {"roles": [
        {"id": r.value, "label": ROLE_LABELS[r], "description": ROLE_DESCRIPTIONS[r],
         "permissions": sorted(ROLE_PERMISSIONS[r])}
        for r in (Role.GUEST, Role.DATA_ENTRY, Role.SUPER_ADMIN)
    ]}


@router.get("/setup-status")
async def setup_status(db: AsyncSession = Depends(get_db)):
    """Whether the first super admin still has to be created. While it does,
    also tells the operator WHERE the one-time setup token file is (on the
    server machine) — never the token itself."""
    needs = await service.count_users(db) == 0
    out = {"needs_setup": needs, "password_min_length": settings.AUTH_PASSWORD_MIN_LENGTH}
    if needs:
        path = await service.ensure_setup_token(db)
        out["setup_token_file"] = str(path) if path else None
        out["cli_command"] = "python -m auth.cli create-superadmin  (run in the backend folder)"
    return out


@router.post("/setup")
async def first_run_setup(body: SetupRequest, request: Request, response: Response, db: AsyncSession = Depends(get_db)):
    """Create the first super admin (only while no user exists, and only with
    the one-time setup token), then sign them in."""
    try:
        user = await service.complete_first_run_setup(
            db, setup_token=body.setup_token, username=body.username, password=body.password,
            display_name=body.display_name, client_ip=client_ip(request))
    except service.AuthError as e:
        _raise(e)
    token = await service.create_session(db, user, client_ip=client_ip(request),
                                         user_agent=request.headers.get("user-agent"))
    await db.commit()
    _set_session_cookie(response, token)
    return {"user": service.user_public(user), "token": token, "expires_in": settings.AUTH_SESSION_MAX_HOURS * 3600}
