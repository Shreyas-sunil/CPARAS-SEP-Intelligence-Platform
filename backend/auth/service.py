"""
Authentication service — accounts, login, sessions, first-run provisioning
===========================================================================

All account/session logic lives here so the HTTP routes
(``api/routes/shared/auth.py``, ``api/routes/shared/users.py``), the request
guard (``auth/dependencies.py``) and the command line (``auth/cli.py``) share
one implementation. Functions take an ``AsyncSession`` and never commit on
their own unless documented — callers own the transaction.

Sessions
    ``create_session`` returns a random 256-bit token (``secrets.token_urlsafe``)
    that is handed to the client once; only its SHA-256 is stored. A session is
    valid until ``expires_at`` (AUTH_SESSION_MAX_HOURS), or until it has been
    idle for AUTH_SESSION_IDLE_MINUTES, or until it is revoked (logout, or an
    admin changing the user's role / password / active flag).

Brute-force protection
    ``authenticate`` counts failures per (username, client IP) in memory; after
    AUTH_LOGIN_MAX_FAILURES failures inside the lockout window that pair is
    refused for AUTH_LOGIN_LOCKOUT_MINUTES. Unknown usernames are verified
    against a dummy hash so they take as long as real ones.

First super admin
    While the ``users`` table is empty, ``ensure_setup_token`` writes a random
    one-time token to AUTH_SETUP_TOKEN_FILE (readable only on the server
    machine) and ``complete_first_run_setup`` accepts it exactly once to
    create the first super admin. The CLI (``python -m auth.cli
    create-superadmin``) is the token-free alternative for whoever runs the
    server. No default or hard-coded credentials exist anywhere.

Invariant
    There is always at least one active super admin once one exists: demoting,
    deactivating or deleting the last one is refused (``AuthError`` 409).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Optional

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from auth import passwords
from auth.roles import ROLE_LABELS, Role, landing_path, parse_role, permissions_for
from core.timeutil import utcnow

logger = logging.getLogger(__name__)

USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,63}$")


class AuthError(Exception):
    """A refused authentication/administration request. ``status_code`` is the
    HTTP status the API layer returns; ``detail`` is safe to show to users."""

    def __init__(self, status_code: int, detail: str):
        """`status_code`: HTTP status for the API; `detail`: user-facing message."""
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _db():
    """``core.database``, imported lazily: the test-suite reloads that module
    between tests, so a module-level import here could point at a stale
    engine/metadata."""
    import core.database as database
    return database


def _settings():
    """Current settings object (lazy for the same reason as ``_db``)."""
    from core.config import settings
    return settings


def hash_token(token: str) -> str:
    """SHA-256 hex digest of a session token — the only form ever stored."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def normalize_username(username: str) -> str:
    """Lower-case + trim, then validate: 3-64 chars of a-z 0-9 . _ - starting
    with a letter/digit. Raises ``AuthError(422)`` otherwise."""
    u = (username or "").strip().lower()
    if not USERNAME_RE.match(u):
        raise AuthError(422, "Username must be 3-64 characters: letters, digits, '.', '_' or '-', "
                             "starting with a letter or digit.")
    return u


def check_password_policy(password: str, username: str = "") -> None:
    """Raise ``AuthError(422)`` describing every policy problem with ``password``."""
    problems = passwords.password_problems(password, username=username,
                                           min_length=_settings().AUTH_PASSWORD_MIN_LENGTH)
    if problems:
        raise AuthError(422, "Password " + "; ".join(problems) + ".")


def user_public(user) -> dict:
    """The JSON shape of a user returned by the API (never includes the hash).
    ``permissions`` and ``landing_path`` let the frontend build its navigation
    from the server's decision instead of re-implementing the role table."""
    role = user.role
    try:
        role_label = ROLE_LABELS[Role(role)]
    except ValueError:
        role_label = role
    return {
        "id": user.id,
        "username": user.username,
        "display_name": user.display_name or user.username,
        "role": role,
        "role_label": role_label,
        "permissions": sorted(permissions_for(role)),
        "landing_path": landing_path(role),
        "is_active": bool(user.is_active),
        "must_change_password": bool(user.must_change_password),
        "created_at": user.created_at.isoformat() if user.created_at else None,
        "last_login_at": user.last_login_at.isoformat() if user.last_login_at else None,
        "created_by": user.created_by,
    }


# ── Audit trail ──────────────────────────────────────────────────────────────

async def record_event(db: AsyncSession, event: str, *, username: Optional[str] = None,
                       actor: Optional[str] = None, client_ip: Optional[str] = None,
                       detail: Optional[str] = None) -> None:
    """Append one ``auth_events`` row (added to the session; caller commits)."""
    db.add(_db().AuthEvent(event=event, username=username, actor=actor, client_ip=client_ip,
                           detail=(detail or None) and detail[:2000]))
    logger.info(f"auth: {event} user={username} actor={actor} ip={client_ip}")


# ── Users ────────────────────────────────────────────────────────────────────

async def count_users(db: AsyncSession) -> int:
    """Number of accounts (active or not)."""
    return int((await db.execute(select(func.count()).select_from(_db().User))).scalar() or 0)


async def count_active_super_admins(db: AsyncSession, exclude_user_id: Optional[int] = None) -> int:
    """Active super admins, optionally not counting ``exclude_user_id``."""
    User = _db().User
    q = select(func.count()).select_from(User).where(User.role == Role.SUPER_ADMIN.value, User.is_active.is_(True))
    if exclude_user_id is not None:
        q = q.where(User.id != exclude_user_id)
    return int((await db.execute(q)).scalar() or 0)


async def get_user_by_username(db: AsyncSession, username: str):
    """The user with this (case-insensitive) username, or None."""
    User = _db().User
    return (await db.execute(select(User).where(User.username == (username or "").strip().lower()))).scalar_one_or_none()


async def get_user(db: AsyncSession, user_id: int):
    """The user with this id, or ``AuthError(404)``."""
    user = await db.get(_db().User, user_id)
    if user is None:
        raise AuthError(404, "User not found.")
    return user


async def list_users(db: AsyncSession) -> list:
    """All users, super admins first, then by username."""
    User = _db().User
    rows = (await db.execute(select(User).order_by(User.username))).scalars().all()
    order = {Role.SUPER_ADMIN.value: 0, Role.DATA_ENTRY.value: 1, Role.GUEST.value: 2}
    return sorted(rows, key=lambda u: (order.get(u.role, 9), u.username))


async def create_user(db: AsyncSession, *, username: str, password: str, role: str,
                      display_name: Optional[str] = None, created_by: Optional[str] = None,
                      must_change_password: bool = False):
    """Create an account after validating username, role and password policy.
    Raises ``AuthError`` 409 if the username is taken. Caller commits."""
    uname = normalize_username(username)
    try:
        r = parse_role(role)
    except ValueError as e:
        raise AuthError(422, str(e)) from None
    check_password_policy(password, uname)
    if await get_user_by_username(db, uname) is not None:
        raise AuthError(409, f"A user named '{uname}' already exists.")
    now = utcnow()
    user = _db().User(
        username=uname, display_name=(display_name or "").strip()[:128] or None, role=r.value,
        password_hash=passwords.hash_password(password), is_active=True,
        must_change_password=bool(must_change_password), created_at=now, updated_at=now, created_by=created_by,
    )
    db.add(user)
    await db.flush()
    return user


async def update_user(db: AsyncSession, user, *, role: Optional[str] = None, is_active: Optional[bool] = None,
                      display_name: Optional[str] = None) -> list[str]:
    """Change role / active flag / display name. Returns a list of what
    changed. Refuses to remove the last active super admin; any role or
    active-flag change signs the user out everywhere. Caller commits."""
    changes: list[str] = []
    new_role = user.role
    if role is not None:
        try:
            new_role = parse_role(role).value
        except ValueError as e:
            raise AuthError(422, str(e)) from None
    new_active = user.is_active if is_active is None else bool(is_active)
    losing_admin = user.role == Role.SUPER_ADMIN.value and user.is_active and (
        new_role != Role.SUPER_ADMIN.value or not new_active)
    if losing_admin and await count_active_super_admins(db, exclude_user_id=user.id) == 0:
        raise AuthError(409, "This is the last active Super Admin. Make another user Super Admin first.")
    if new_role != user.role:
        changes.append(f"role {user.role} -> {new_role}")
        user.role = new_role
    if new_active != user.is_active:
        changes.append("activated" if new_active else "deactivated")
        user.is_active = new_active
    if display_name is not None and (display_name.strip() or None) != user.display_name:
        user.display_name = display_name.strip()[:128] or None
        changes.append("display name")
    if changes:
        user.updated_at = utcnow()
        if any(c.startswith("role") or c in ("activated", "deactivated") for c in changes):
            await revoke_user_sessions(db, user.id)
    return changes


async def set_password(db: AsyncSession, user, new_password: str, *, must_change: bool = False,
                       keep_session_hash: Optional[str] = None) -> None:
    """Replace the user's password (policy-checked) and sign out every other
    session — all of them, or all except ``keep_session_hash`` when users
    change their own password. Caller commits."""
    check_password_policy(new_password, user.username)
    user.password_hash = passwords.hash_password(new_password)
    user.must_change_password = bool(must_change)
    user.updated_at = utcnow()
    await revoke_user_sessions(db, user.id, except_token_hash=keep_session_hash)


async def delete_user(db: AsyncSession, user) -> None:
    """Delete an account and its sessions (last active super admin refused). Caller commits."""
    if user.role == Role.SUPER_ADMIN.value and user.is_active and \
            await count_active_super_admins(db, exclude_user_id=user.id) == 0:
        raise AuthError(409, "This is the last active Super Admin and cannot be deleted.")
    await revoke_user_sessions(db, user.id)
    await db.delete(user)


# ── Login / brute-force protection ───────────────────────────────────────────

@dataclass
class _Failures:
    """Failed-login counter for one (username, client) pair within the lockout window."""
    count: int
    first_at: float
    locked_until: float = 0.0


_FAILURES: dict[tuple[str, str], _Failures] = {}
_FAILURES_LOCK = threading.Lock()


def _lock_key(username: str, client_ip: Optional[str]) -> tuple[str, str]:
    """Lockout key: normalised username + client IP."""
    return ((username or "").strip().lower(), client_ip or "?")


def _locked_seconds(key: tuple[str, str]) -> int:
    """Seconds left on a lockout for ``key`` (0 = not locked)."""
    with _FAILURES_LOCK:
        f = _FAILURES.get(key)
        return max(0, int(f.locked_until - time.time())) if f else 0


def _register_failure(key: tuple[str, str]) -> None:
    """Count a failed login; lock the key once the threshold is reached
    within one lockout window."""
    s = _settings()
    window = s.AUTH_LOGIN_LOCKOUT_MINUTES * 60
    now = time.time()
    with _FAILURES_LOCK:
        f = _FAILURES.get(key)
        if f is None or now - f.first_at > window:
            f = _FAILURES[key] = _Failures(count=0, first_at=now)
        f.count += 1
        if f.count >= s.AUTH_LOGIN_MAX_FAILURES:
            f.locked_until = now + window
        if len(_FAILURES) > 10_000:  # bound memory under a spray of random usernames
            for k in [k for k, v in _FAILURES.items() if now - v.first_at > window][:5000]:
                _FAILURES.pop(k, None)


def reset_login_failures() -> None:
    """Forget all failure counters (tests; also safe to call from an admin tool)."""
    with _FAILURES_LOCK:
        _FAILURES.clear()


async def authenticate(db: AsyncSession, username: str, password: str, client_ip: Optional[str] = None):
    """Return the active user for valid credentials, else raise ``AuthError``:
    401 (wrong username/password — deliberately the same message for both),
    403 (account deactivated), 429 (temporarily locked out). Records the
    outcome in the audit trail and upgrades an outdated hash. Caller commits."""
    key = _lock_key(username, client_ip)
    wait = _locked_seconds(key)
    if wait:
        await record_event(db, "login_locked", username=key[0], client_ip=client_ip)
        raise AuthError(429, f"Too many failed sign-in attempts. Try again in {max(1, wait // 60)} minute(s).")
    user = await get_user_by_username(db, username) if (username or "").strip() else None
    ok = passwords.verify_password(password or "", user.password_hash if user else passwords.DUMMY_HASH)
    if not user or not ok:
        _register_failure(key)
        await record_event(db, "login_failed", username=key[0], client_ip=client_ip)
        raise AuthError(401, "Incorrect username or password.")
    if not user.is_active:
        await record_event(db, "login_inactive", username=user.username, client_ip=client_ip)
        raise AuthError(403, "This account is deactivated. Contact a CPARAS Super Admin.")
    with _FAILURES_LOCK:
        _FAILURES.pop(key, None)
    if passwords.needs_rehash(user.password_hash):
        user.password_hash = passwords.hash_password(password)
    user.last_login_at = utcnow()
    await record_event(db, "login", username=user.username, actor=user.username, client_ip=client_ip)
    return user


# ── Sessions ─────────────────────────────────────────────────────────────────

async def create_session(db: AsyncSession, user, *, client_ip: Optional[str] = None,
                         user_agent: Optional[str] = None) -> str:
    """Start a session for ``user`` and return the raw token (shown to the
    client once; only its hash is stored). Caller commits."""
    token = secrets.token_urlsafe(32)
    now = utcnow()
    db.add(_db().UserSession(
        token_hash=hash_token(token), user_id=user.id, created_at=now, last_seen_at=now,
        expires_at=now + timedelta(hours=_settings().AUTH_SESSION_MAX_HOURS),
        client_ip=client_ip, user_agent=(user_agent or "")[:256] or None,
    ))
    return token


# Touching last_seen_at on every request would turn every read into a write
# (SQLite!); once a minute is plenty for an idle timeout measured in hours.
_TOUCH_INTERVAL = timedelta(seconds=60)


async def resolve_session(db: AsyncSession, token: Optional[str]):
    """``(user, session)`` for a valid, unexpired, non-idle session whose user
    is still active; otherwise ``None``. Commits only the occasional
    ``last_seen_at`` refresh."""
    if not token or len(token) > 200:
        return None
    database = _db()
    sess = (await db.execute(select(database.UserSession).where(
        database.UserSession.token_hash == hash_token(token)))).scalar_one_or_none()
    if sess is None or sess.revoked_at is not None:
        return None
    now = utcnow()
    idle = timedelta(minutes=_settings().AUTH_SESSION_IDLE_MINUTES)
    if now >= sess.expires_at or now - sess.last_seen_at > idle:
        return None
    user = await db.get(database.User, sess.user_id)
    if user is None or not user.is_active:
        return None
    if now - sess.last_seen_at > _TOUCH_INTERVAL:
        sess.last_seen_at = now
        await db.commit()
    return user, sess


async def revoke_session(db: AsyncSession, token: Optional[str]) -> None:
    """End the session identified by ``token`` (logout). Caller commits."""
    if not token:
        return
    S = _db().UserSession
    await db.execute(update(S).where(S.token_hash == hash_token(token), S.revoked_at.is_(None))
                     .values(revoked_at=utcnow()))


async def revoke_user_sessions(db: AsyncSession, user_id: int, except_token_hash: Optional[str] = None) -> None:
    """End every open session of ``user_id`` (optionally keeping one). Caller commits."""
    S = _db().UserSession
    q = update(S).where(S.user_id == user_id, S.revoked_at.is_(None))
    if except_token_hash:
        q = q.where(S.token_hash != except_token_hash)
    await db.execute(q.values(revoked_at=utcnow()))


async def purge_expired_sessions(db: AsyncSession) -> int:
    """Delete sessions that can never be valid again (expired or revoked more
    than a day ago). Called at start-up; returns the number removed."""
    from sqlalchemy import delete, or_
    S = _db().UserSession
    cutoff = utcnow() - timedelta(days=1)
    res = await db.execute(delete(S).where(or_(S.expires_at < utcnow(), S.revoked_at < cutoff)))
    return int(res.rowcount or 0)


# ── First-run provisioning ───────────────────────────────────────────────────

def _token_file() -> Path:
    """Path of the one-time first-run setup token file (AUTH_SETUP_TOKEN_FILE)."""
    return Path(_settings().AUTH_SETUP_TOKEN_FILE)


async def ensure_setup_token(db: AsyncSession) -> Optional[Path]:
    """At start-up: if there are no users, make sure a one-time setup token
    file exists and return its path (logged so the operator knows where to
    look); once users exist, remove any leftover token file and return None."""
    path = _token_file()
    if await count_users(db) > 0:
        if path.exists():
            try:
                path.unlink()
            except OSError:
                logger.warning(f"Could not remove the stale first-run setup token file {path}.")
        return None
    if not path.exists() or not path.read_text(encoding="utf-8").strip():
        path.parent.mkdir(parents=True, exist_ok=True)
        token = secrets.token_urlsafe(24)
        # Create with owner-only permissions where the OS supports it (POSIX);
        # on Windows the file inherits the folder's ACL (the user's profile).
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(token + "\n")
    return path


async def complete_first_run_setup(db: AsyncSession, *, setup_token: str, username: str, password: str,
                                   display_name: Optional[str] = None, client_ip: Optional[str] = None):
    """Create the first super admin with the one-time token. Refused (409)
    once any user exists, (403) for a wrong/missing token. Deletes the token
    file on success. Commits."""
    if await count_users(db) > 0:
        raise AuthError(409, "CPARAS is already set up. Sign in, or ask a Super Admin for an account.")
    path = _token_file()
    expected = path.read_text(encoding="utf-8").strip() if path.exists() else ""
    if not expected or not hmac.compare_digest((setup_token or "").strip().encode(), expected.encode()):
        await record_event(db, "setup_failed", username=(username or "")[:64], client_ip=client_ip)
        await db.commit()
        raise AuthError(403, "The setup token is not correct. Copy it from the file named on the setup page "
                             "(on the computer that runs the CPARAS backend).")
    user = await create_user(db, username=username, password=password, role=Role.SUPER_ADMIN.value,
                             display_name=display_name, created_by="first-run-setup")
    await record_event(db, "first_admin_created", username=user.username, actor=user.username, client_ip=client_ip)
    await db.commit()
    try:
        path.unlink()
    except OSError:
        logger.warning(f"First super admin created, but the setup token file {path} could not be deleted.")
    return user
