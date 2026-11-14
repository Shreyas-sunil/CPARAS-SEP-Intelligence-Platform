"""
User & role administration API — ``/api/auth/users``, ``/api/auth/events`` (v0.5.0)
====================================================================================

Super admin only (``users.manage``, enforced by ``auth/policy.py``).

    GET    /api/auth/users                    list accounts
    POST   /api/auth/users                    create an account (role + initial password)
    PATCH  /api/auth/users/{id}               change role / active flag / display name
    POST   /api/auth/users/{id}/password      set a new password (optionally a generated one)
    DELETE /api/auth/users/{id}               delete an account
    GET    /api/auth/events                   recent authentication / administration events

Changing a user's role, deactivating them or resetting their password signs
them out everywhere, so the change takes effect immediately. The last active
super admin can never be demoted, deactivated or deleted (409). Every change
is written to the ``auth_events`` audit trail with the acting admin's name.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth import service
from auth.dependencies import client_ip, current_user
from auth.passwords import generate_password
from core.database import AuthEvent, get_db

router = APIRouter()


class UserCreate(BaseModel):
    """Body of POST /api/auth/users. `must_change_password` (default true) makes the user choose their
    own password at first sign-in.
    """
    username: str = Field(..., max_length=128)
    role: str = Field(..., max_length=32)
    password: Optional[str] = Field(None, max_length=256, description="Omit to generate a random one (returned once).")
    display_name: Optional[str] = Field(None, max_length=128)
    must_change_password: bool = True


class UserUpdate(BaseModel):
    """Body of PATCH /api/auth/users/{id}; omitted fields are left unchanged."""
    role: Optional[str] = Field(None, max_length=32)
    is_active: Optional[bool] = None
    display_name: Optional[str] = Field(None, max_length=128)


class PasswordReset(BaseModel):
    """Body of POST /api/auth/users/{id}/password."""
    password: Optional[str] = Field(None, max_length=256, description="Omit to generate a random one (returned once).")
    must_change_password: bool = True


def _raise(e: service.AuthError):
    """Translate a service-level AuthError into the HTTP response."""
    raise HTTPException(e.status_code, detail=e.detail)


@router.get("/users")
async def list_users(db: AsyncSession = Depends(get_db)):
    """All accounts (no password hashes)."""
    return {"users": [service.user_public(u) for u in await service.list_users(db)]}


@router.post("/users", status_code=201)
async def create_user(body: UserCreate, request: Request, admin=Depends(current_user), db: AsyncSession = Depends(get_db)):
    """Create an account. With no ``password`` a strong random one is
    generated and returned ONCE as ``generated_password``."""
    pw = body.password or generate_password()
    try:
        user = await service.create_user(db, username=body.username, password=pw, role=body.role,
                                         display_name=body.display_name, created_by=admin.username,
                                         must_change_password=body.must_change_password)
    except service.AuthError as e:
        _raise(e)
    await service.record_event(db, "user_created", username=user.username, actor=admin.username,
                               client_ip=client_ip(request), detail=f"role={user.role}")
    await db.commit()
    out = {"user": service.user_public(user)}
    if not body.password:
        out["generated_password"] = pw
    return out


@router.patch("/users/{user_id}")
async def update_user(user_id: int, body: UserUpdate, request: Request, admin=Depends(current_user),
                      db: AsyncSession = Depends(get_db)):
    """Change role, active flag and/or display name (signs the user out on a
    role or active-flag change)."""
    try:
        user = await service.get_user(db, user_id)
        changes = await service.update_user(db, user, role=body.role, is_active=body.is_active,
                                            display_name=body.display_name)
    except service.AuthError as e:
        _raise(e)
    if changes:
        await service.record_event(db, "user_updated", username=user.username, actor=admin.username,
                                   client_ip=client_ip(request), detail="; ".join(changes))
    await db.commit()
    return {"user": service.user_public(user), "changes": changes}


@router.post("/users/{user_id}/password")
async def reset_password(user_id: int, body: PasswordReset, request: Request, admin=Depends(current_user),
                         db: AsyncSession = Depends(get_db)):
    """Set a user's password (an administrator reset). The user is signed out
    everywhere and, by default, asked to choose a new password at next sign-in."""
    pw = body.password or generate_password()
    try:
        user = await service.get_user(db, user_id)
        await service.set_password(db, user, pw, must_change=body.must_change_password)
    except service.AuthError as e:
        _raise(e)
    await service.record_event(db, "password_reset", username=user.username, actor=admin.username,
                               client_ip=client_ip(request))
    await db.commit()
    out = {"user": service.user_public(user)}
    if not body.password:
        out["generated_password"] = pw
    return out


@router.delete("/users/{user_id}")
async def delete_user(user_id: int, request: Request, admin=Depends(current_user), db: AsyncSession = Depends(get_db)):
    """Delete an account (you cannot delete yourself; the last active super
    admin cannot be deleted)."""
    if user_id == admin.id:
        raise HTTPException(409, detail="You cannot delete your own account. Ask another Super Admin.")
    try:
        user = await service.get_user(db, user_id)
        name = user.username
        await service.delete_user(db, user)
    except service.AuthError as e:
        _raise(e)
    await service.record_event(db, "user_deleted", username=name, actor=admin.username, client_ip=client_ip(request))
    await db.commit()
    return {"ok": True}


@router.get("/events")
async def list_events(limit: int = Query(100, ge=1, le=1000), db: AsyncSession = Depends(get_db)):
    """The most recent authentication/administration events, newest first."""
    rows = (await db.execute(select(AuthEvent).order_by(AuthEvent.id.desc()).limit(limit))).scalars().all()
    return {"events": [
        {"id": e.id, "at": e.at.isoformat() if e.at else None, "event": e.event, "username": e.username,
         "actor": e.actor, "client_ip": e.client_ip, "detail": e.detail}
        for e in rows
    ]}
