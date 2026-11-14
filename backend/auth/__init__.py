"""
CPARAS authentication & role-based access control (v0.5.0)
============================================================

    roles.py         the three roles and the permission set of each (source of truth)
    passwords.py     scrypt password hashing + password policy (stdlib only)
    service.py       accounts, login + lockout, sessions, first-run provisioning
    policy.py        route-prefix -> required-permission table (deny by default)
    dependencies.py  the app-wide FastAPI guard + ``current_user`` dependency
    cli.py           ``python -m auth.cli ...`` — create the first super admin,
                     seed test accounts, reset passwords, list users

HTTP endpoints: ``api/routes/shared/auth.py`` (``/api/auth``: login, logout,
me, own password, first-run setup) and ``api/routes/shared/users.py``
(``/api/auth/users``, ``/api/auth/events``: super-admin user management).
Database tables ``users``, ``user_sessions``, ``auth_events`` are defined in
``core/database.py``. Operator guide: ``docs/AUTH.md``.
"""
