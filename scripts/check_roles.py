#!/usr/bin/env python3
"""
CPARAS role check against a RUNNING installation (v0.5.0) — standard library only.

Signs in as one Guest, one Data Entry and one Super Admin account and checks that
each role gets exactly the access it should, on the real backend:

    * landing page after sign-in (Guest -> /isld, others -> /)
    * representative ISLD, CPARAS Bulk, reference-data and administration endpoints
      (only read-only requests and requests the backend refuses before doing anything,
      so running this never changes data)
    * signing out ends the session

Typical use, after creating the three test accounts once:

    seed_test_users.cmd                      (Windows)   |   ./seed_test_users.sh
    python scripts/check_roles.py --guest test_guest:<pw> --data-entry test_data_entry:<pw> \
                                  --super-admin test_super_admin:<pw>

Passwords can also come from the environment: CPARAS_GUEST, CPARAS_DATA_ENTRY,
CPARAS_SUPER_ADMIN (each "username:password"). --base defaults to the backend at
http://127.0.0.1:8000 (the Vite address http://127.0.0.1:5173 works too).
Exit code 0 when every check passes, 1 otherwise.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

# (method, path, JSON body or None) -> expected HTTP status for (guest, data_entry, super_admin).
# None = "anything except 401/403" (allowed; the handler's own answer depends on the data).
CHECKS = [
    ("GET", "/api/health", None, (200, 200, 200)),
    ("GET", "/api/analysis/history", None, (200, 200, 200)),
    ("GET", "/api/text-match/status", None, (200, 200, 200)),
    ("GET", "/api/models/status", None, (None, None, None)),
    ("GET", "/api/sep/runs", None, (403, 200, 200)),
    ("GET", "/api/sep/patents?limit=1", None, (403, 200, 200)),
    ("GET", "/api/sep/dashboard/overview", None, (403, 200, 200)),
    ("POST", "/api/sep/upload", None, (403, 422, 422)),            # 422 = allowed, no file sent
    ("GET", "/api/retrieval-index/status", None, (403, 200, 200)),
    ("GET", "/api/etsi-corpus/status", None, (403, None, None)),
    ("POST", "/api/etsi-corpus/dataset/import", None, (403, 403, 422)),
    ("POST", "/api/scrape/start", {"series": ["__invalid__"]}, (403, 403, 400)),
    ("GET", "/api/auth/users", None, (403, 403, 200)),
    ("GET", "/api/auth/events?limit=5", None, (403, 403, 200)),
]

ROLES = ("guest", "data_entry", "super_admin")
LANDING = {"guest": "/isld", "data_entry": "/", "super_admin": "/"}


def request(base: str, method: str, path: str, body=None, token: str | None = None) -> tuple[int, dict]:
    """Send one request; returns (status, parsed JSON or {})."""
    data = json.dumps(body).encode() if body is not None else (b"" if method != "GET" else None)
    req = urllib.request.Request(base.rstrip("/") + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read()
            status = r.status
    except urllib.error.HTTPError as e:
        raw, status = e.read(), e.code
    try:
        return status, json.loads(raw or b"{}")
    except ValueError:
        return status, {}


def main() -> int:
    """Parse arguments, run every check for every role, print a report."""
    ap = argparse.ArgumentParser(description="Check CPARAS role permissions on a running installation.")
    ap.add_argument("--base", default=os.environ.get("CPARAS_URL", "http://127.0.0.1:8000"))
    ap.add_argument("--guest", default=os.environ.get("CPARAS_GUEST"), help="username:password")
    ap.add_argument("--data-entry", default=os.environ.get("CPARAS_DATA_ENTRY"), help="username:password")
    ap.add_argument("--super-admin", default=os.environ.get("CPARAS_SUPER_ADMIN"), help="username:password")
    a = ap.parse_args()
    creds = {"guest": a.guest, "data_entry": a.data_entry, "super_admin": a.super_admin}
    missing = [r for r, c in creds.items() if not c or ":" not in c]
    if missing:
        print(f"Missing credentials for: {', '.join(missing)} (use --guest user:pw etc.)", file=sys.stderr)
        return 2

    failures = 0

    def report(ok: bool, text: str) -> None:
        nonlocal failures
        failures += 0 if ok else 1
        print(("  PASS  " if ok else "  FAIL  ") + text)

    st, _ = request(a.base, "GET", "/api/sep/runs")
    report(st == 401, f"anonymous GET /api/sep/runs -> {st} (expect 401)")

    tokens = {}
    for role in ROLES:
        user, pw = creds[role].split(":", 1)
        st, body = request(a.base, "POST", "/api/auth/login", {"username": user, "password": pw})
        if st != 200:
            report(False, f"{role}: sign-in as '{user}' -> HTTP {st} {body.get('detail', '')}")
            continue
        u = body["user"]
        tokens[role] = body["token"]
        report(u["role"] == role, f"{role}: '{user}' has role {u['role']}")
        report(u["landing_path"] == LANDING[role], f"{role}: lands on {u['landing_path']} (expect {LANDING[role]})")

    print()
    print(f"  {'Endpoint':<48} " + "  ".join(f"{r:<11}" for r in ROLES))
    for method, path, body, expected in CHECKS:
        cells = []
        for role, want in zip(ROLES, expected):
            if role not in tokens:
                cells.append("n/a")
                continue
            st, _ = request(a.base, method, path, body, tokens[role])
            ok = (st not in (401, 403)) if want is None else (st == want)
            failures += 0 if ok else 1
            cells.append(f"{'ok' if ok else 'FAIL'} {st}")
        print(f"  {method + ' ' + path:<48} " + "  ".join(f"{c:<11}" for c in cells))

    print()
    for role, tok in tokens.items():
        request(a.base, "POST", "/api/auth/logout", None, tok)
        st, _ = request(a.base, "GET", "/api/auth/me", None, tok)
        report(st == 401, f"{role}: after sign-out the session is rejected ({st})")

    print(f"\n{'All checks passed.' if not failures else f'{failures} check(s) FAILED.'}")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
