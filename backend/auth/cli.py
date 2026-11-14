"""
CPARAS account administration from the command line (v0.5.0)
=============================================================

Run in the ``backend`` folder with the backend's Python (on Windows the
``create_admin.cmd`` / ``seed_test_users.cmd`` wrappers in the project folder
do this for you)::

    python -m auth.cli create-superadmin [--username admin] [--display-name "..."]
    python -m auth.cli create-user --username alice --role data_entry
    python -m auth.cli seed-test-users [--prefix test_] [--reset]
    python -m auth.cli reset-password --username alice
    python -m auth.cli set-role --username alice --role guest
    python -m auth.cli set-active --username alice --inactive
    python -m auth.cli list-users

Passwords are typed at a hidden prompt (twice), read from stdin with
``--password-stdin`` (for automation: ``echo pw | python -m auth.cli ...``), or
generated (``--generate``) and printed once. Nothing here contains or creates
a fixed/default password.

This is the trusted provisioning path: whoever can run commands on the server
machine can create the first super admin without the one-time browser setup
token. It works whether or not the backend is running (same SQLite database;
WAL mode allows both). Sessions of a user whose role/password changes are
revoked immediately, so a running app picks the change up on the next request.

Exit codes: 0 success, 1 refused/invalid input, 2 usage error.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from auth.passwords import generate_password  # noqa: E402
from auth.roles import ROLE_LABELS, Role  # noqa: E402


def _read_password(args, username: str) -> tuple[str, bool]:
    """``(password, generated)`` from --generate, --password-stdin or a hidden
    prompt (asked twice)."""
    if getattr(args, "generate", False):
        return generate_password(), True
    if getattr(args, "password_stdin", False):
        pw = sys.stdin.readline().rstrip("\r\n")
        if not pw:
            raise SystemExit("No password on stdin.")
        return pw, False
    if not sys.stdin.isatty():
        raise SystemExit("No terminal for a password prompt: use --password-stdin or --generate.")
    from core.config import settings
    print(f"Choose a password for '{username}' (at least {settings.AUTH_PASSWORD_MIN_LENGTH} characters, "
          "mixing letters with digits or symbols).")
    while True:
        a = getpass.getpass("Password: ")
        b = getpass.getpass("Repeat password: ")
        if a != b:
            print("The two passwords differ, try again.")
            continue
        return a, False


async def _with_db(fn):
    """Open the database (creating any missing tables first) and run ``fn(db)``."""
    from core.database import AsyncSessionLocal, init_db
    await init_db()
    async with AsyncSessionLocal() as db:
        return await fn(db)


def _fail(msg: str) -> int:
    """Print an error to stderr and return exit code 1."""
    print(f"ERROR: {msg}", file=sys.stderr)
    return 1


async def cmd_create(args, role: str) -> int:
    """create-superadmin / create-user."""
    from auth import service
    username = (args.username or "").strip() or (input("Username: ").strip() if sys.stdin.isatty() else "")
    if not username:
        return _fail("A username is required (--username).")
    try:
        username = service.normalize_username(username)
    except service.AuthError as e:
        return _fail(e.detail)
    pw, generated = _read_password(args, username)

    async def run(db):
        """Create the account and remove the first-run token file once a user exists."""
        try:
            user = await service.create_user(db, username=username, password=pw, role=role,
                                             display_name=args.display_name, created_by="cli",
                                             must_change_password=bool(getattr(args, "must_change", False)))
        except service.AuthError as e:
            return _fail(e.detail)
        await service.record_event(db, "user_created", username=user.username, actor="cli", detail=f"role={role}")
        await db.commit()
        await service.ensure_setup_token(db)  # removes the first-run token file once a user exists
        print(f"Created {ROLE_LABELS[Role(role)]} '{user.username}'.")
        if generated:
            print(f"  Password (shown once, store it safely): {pw}")
        return 0
    return await _with_db(run)


async def cmd_seed(args) -> int:
    """seed-test-users: one account per role for testing, random passwords."""
    from auth import service
    plan = [(f"{args.prefix}guest", Role.GUEST), (f"{args.prefix}data_entry", Role.DATA_ENTRY),
            (f"{args.prefix}super_admin", Role.SUPER_ADMIN)]

    async def run(db):
        """Create (or with --reset re-password) the three test accounts and print their one-time
        passwords.
        """
        rows = []
        for uname, role in plan:
            pw = generate_password()
            existing = await service.get_user_by_username(db, uname)
            try:
                if existing is None:
                    await service.create_user(db, username=uname, password=pw, role=role.value,
                                              display_name=f"Test {ROLE_LABELS[role]}", created_by="cli:seed")
                    state = "created"
                elif args.reset:
                    await service.update_user(db, existing, role=role.value, is_active=True)
                    await service.set_password(db, existing, pw, must_change=False)
                    state = "password reset"
                else:
                    rows.append((uname, ROLE_LABELS[role], "(exists - unchanged; use --reset for a new password)"))
                    continue
            except service.AuthError as e:
                return _fail(f"{uname}: {e.detail}")
            await service.record_event(db, "test_user_seeded", username=uname, actor="cli", detail=state)
            rows.append((uname, ROLE_LABELS[role], pw))
        await db.commit()
        await service.ensure_setup_token(db)
        print("\nCPARAS test accounts (passwords are shown ONCE - note them now):\n")
        print(f"  {'Username':<24} {'Role':<12} Password")
        print(f"  {'-' * 24} {'-' * 12} {'-' * 20}")
        for u, r, p in rows:
            print(f"  {u:<24} {r:<12} {p}")
        print("\nThese are ordinary accounts: delete or deactivate them (User Management page, or\n"
              "`python -m auth.cli set-active --username <name> --inactive`) before going live.")
        return 0
    return await _with_db(run)


async def cmd_reset_password(args) -> int:
    """reset-password: new password for an existing account (signs it out)."""
    from auth import service

    async def run(db):
        """Set the new password and sign the user out everywhere."""
        user = await service.get_user_by_username(db, args.username)
        if user is None:
            return _fail(f"No user named '{args.username}'.")
        pw, generated = _read_password(args, user.username)
        try:
            await service.set_password(db, user, pw, must_change=bool(args.must_change))
        except service.AuthError as e:
            return _fail(e.detail)
        await service.record_event(db, "password_reset", username=user.username, actor="cli")
        await db.commit()
        print(f"Password for '{user.username}' changed; their open sessions were signed out.")
        if generated:
            print(f"  New password (shown once): {pw}")
        return 0
    return await _with_db(run)


async def cmd_update(args, *, role=None, is_active=None) -> int:
    """set-role / set-active."""
    from auth import service

    async def run(db):
        """Apply the role / active-flag change (last-super-admin rule enforced)."""
        user = await service.get_user_by_username(db, args.username)
        if user is None:
            return _fail(f"No user named '{args.username}'.")
        try:
            changes = await service.update_user(db, user, role=role, is_active=is_active)
        except service.AuthError as e:
            return _fail(e.detail)
        if changes:
            await service.record_event(db, "user_updated", username=user.username, actor="cli", detail="; ".join(changes))
        await db.commit()
        print(f"'{user.username}': " + ("; ".join(changes) if changes else "no change"))
        return 0
    return await _with_db(run)


async def cmd_list(_args) -> int:
    """list-users: a table of accounts."""
    from auth import service

    async def run(db):
        """Print the account table."""
        users = await service.list_users(db)
        if not users:
            print("No users yet. Create the first one with:  python -m auth.cli create-superadmin")
            return 0
        print(f"  {'Username':<24} {'Role':<12} {'Active':<7} Last sign-in")
        for u in users:
            print(f"  {u.username:<24} {ROLE_LABELS.get(Role(u.role), u.role):<12} {'yes' if u.is_active else 'no':<7} "
                  f"{u.last_login_at.isoformat(timespec='minutes') if u.last_login_at else '-'}")
        return 0
    return await _with_db(run)


def build_parser() -> argparse.ArgumentParser:
    """argparse definition for every sub-command (see module docstring)."""
    p = argparse.ArgumentParser(prog="python -m auth.cli", description="CPARAS user administration")
    sub = p.add_subparsers(dest="cmd", required=True)

    def pw_opts(sp):
        """Add the mutually exclusive --password-stdin / --generate options."""
        g = sp.add_mutually_exclusive_group()
        g.add_argument("--password-stdin", action="store_true", help="read the password from the first line of stdin")
        g.add_argument("--generate", action="store_true", help="generate a random password and print it once")

    sa = sub.add_parser("create-superadmin", help="create a Super Admin (first-time setup)")
    sa.add_argument("--username", default="")
    sa.add_argument("--display-name", default=None)
    pw_opts(sa)

    cu = sub.add_parser("create-user", help="create an account with any role")
    cu.add_argument("--username", required=True)
    cu.add_argument("--role", required=True, choices=[r.value for r in Role])
    cu.add_argument("--display-name", default=None)
    cu.add_argument("--must-change", action="store_true", help="ask the user to change the password at first sign-in")
    pw_opts(cu)

    st = sub.add_parser("seed-test-users", help="create one test account per role with random passwords")
    st.add_argument("--prefix", default="test_")
    st.add_argument("--reset", action="store_true", help="re-generate passwords for test accounts that already exist")

    rp = sub.add_parser("reset-password", help="set a new password for an account")
    rp.add_argument("--username", required=True)
    rp.add_argument("--must-change", action="store_true")
    pw_opts(rp)

    sr = sub.add_parser("set-role", help="change an account's role")
    sr.add_argument("--username", required=True)
    sr.add_argument("--role", required=True, choices=[r.value for r in Role])

    sx = sub.add_parser("set-active", help="activate or deactivate an account")
    sx.add_argument("--username", required=True)
    g = sx.add_mutually_exclusive_group(required=True)
    g.add_argument("--active", dest="active", action="store_true")
    g.add_argument("--inactive", dest="active", action="store_false")

    sub.add_parser("list-users", help="list accounts")
    return p


def main(argv=None) -> int:
    """Entry point; returns the process exit code."""
    args = build_parser().parse_args(argv)
    if args.cmd == "create-superadmin":
        coro = cmd_create(args, Role.SUPER_ADMIN.value)
    elif args.cmd == "create-user":
        coro = cmd_create(args, args.role)
    elif args.cmd == "seed-test-users":
        coro = cmd_seed(args)
    elif args.cmd == "reset-password":
        coro = cmd_reset_password(args)
    elif args.cmd == "set-role":
        coro = cmd_update(args, role=args.role)
    elif args.cmd == "set-active":
        coro = cmd_update(args, is_active=args.active)
    else:
        coro = cmd_list(args)
    return asyncio.run(coro)


if __name__ == "__main__":
    sys.exit(main())
