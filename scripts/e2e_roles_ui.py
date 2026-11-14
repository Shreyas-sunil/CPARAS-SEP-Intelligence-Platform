#!/usr/bin/env python3
"""
Browser check of the three CPARAS roles against a RUNNING installation (v0.5.0).

Optional developer tool - needs Playwright (`pip install playwright` and
`python -m playwright install chromium`, both online once). Read-only: it signs in,
looks, navigates and signs out; it never changes data.

For each role it checks the post-login landing page, which products / pages the
sidebar offers, that pages the role may not use show "You don't have access",
that the role's own pages open, and that "Sign out" returns to the sign-in page.

    python scripts/e2e_roles_ui.py --guest test_guest:<pw> --data-entry test_data_entry:<pw> \
                                   --super-admin test_super_admin:<pw> [--base http://127.0.0.1:5173] \
                                   [--shots screenshots/]

Credentials can also come from CPARAS_GUEST / CPARAS_DATA_ENTRY / CPARAS_SUPER_ADMIN.
Exit code 0 when every check passes.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

ALL_PAGES = ["/bulk", "/bulk/runs", "/bulk/registry", "/corpus", "/retrieval-index", "/admin/users", "/isld",
             "/isld/text-matching"]
EXPECT = {
    "guest": {"landing": "/isld", "sidebar_has": ["CPARAS ISLD", "Declaration Analysis", "Text Matching"],
              "sidebar_lacks": ["CPARAS Bulk", "Uploads & Runs", "Standards Corpus", "3GPP Retrieval Index", "User Management"],
              "allowed": ["/isld", "/isld/text-matching"]},
    "data_entry": {"landing": "/", "sidebar_has": ["CPARAS Bulk", "CPARAS ISLD", "Standards Corpus", "3GPP Retrieval Index"],
                   "sidebar_lacks": ["User Management"],
                   "allowed": ["/bulk", "/bulk/runs", "/bulk/registry", "/corpus", "/retrieval-index", "/isld", "/isld/text-matching"]},
    "super_admin": {"landing": "/", "sidebar_has": ["CPARAS Bulk", "CPARAS ISLD", "3GPP Retrieval Index", "User Management"],
                    "sidebar_lacks": [], "allowed": ALL_PAGES},
}
NO_ACCESS = "You don’t have access to this page"


def main() -> int:
    """Run the checks; print PASS/FAIL lines; return the exit code."""
    ap = argparse.ArgumentParser(description="Browser check of CPARAS roles.")
    ap.add_argument("--base", default=os.environ.get("CPARAS_UI_URL", "http://127.0.0.1:5173"))
    ap.add_argument("--guest", default=os.environ.get("CPARAS_GUEST"))
    ap.add_argument("--data-entry", default=os.environ.get("CPARAS_DATA_ENTRY"))
    ap.add_argument("--super-admin", default=os.environ.get("CPARAS_SUPER_ADMIN"))
    ap.add_argument("--shots", default=None, help="folder for screenshots (optional)")
    a = ap.parse_args()
    try:
        from playwright.sync_api import expect, sync_playwright
    except ImportError:
        print("Playwright is not installed: pip install playwright && python -m playwright install chromium")
        return 2
    creds = {"guest": a.guest, "data_entry": a.data_entry, "super_admin": a.super_admin}
    if any(not c or ":" not in c for c in creds.values()):
        print("Give all three accounts as username:password (--guest, --data-entry, --super-admin).")
        return 2
    shots = Path(a.shots) if a.shots else None
    if shots:
        shots.mkdir(parents=True, exist_ok=True)
    base = a.base.rstrip("/")
    fails = 0

    def check(ok: bool, text: str) -> None:
        nonlocal fails
        fails += 0 if ok else 1
        print(("  PASS  " if ok else "  FAIL  ") + text)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        for role, spec in EXPECT.items():
            user, pw = creds[role].split(":", 1)
            ctx = browser.new_context(viewport={"width": 1440, "height": 900})
            page = ctx.new_page()
            page.goto(base + "/login")
            page.get_by_label("Username").fill(user)
            page.get_by_label("Password", exact=True).fill(pw)
            page.get_by_role("button", name="Sign in").click()
            try:
                page.wait_for_url(re.compile(re.escape(base) + re.escape(spec["landing"]) + r"$"), timeout=15000)
                check(True, f"{role}: lands on {spec['landing']}")
            except Exception:  # noqa: BLE001
                check(False, f"{role}: expected landing {spec['landing']}, got {page.url}")
            if page.get_by_text("Choose your password").is_visible():
                print(f"  NOTE  {role}: account must change its password first - sign in once in the browser.")
                ctx.close()
                continue
            sb = page.locator("aside.sidebar").inner_text()
            for item in spec["sidebar_has"]:
                check(item in sb, f"{role}: sidebar shows '{item}'")
            for item in spec["sidebar_lacks"]:
                check(item not in sb, f"{role}: sidebar hides '{item}'")
            if shots:
                page.screenshot(path=str(shots / f"{role}_landing.png"))
            for path in ALL_PAGES:
                page.goto(base + path)
                blocked = page.get_by_text(NO_ACCESS)
                if path in spec["allowed"]:
                    page.wait_for_timeout(800)
                    check(not blocked.is_visible(), f"{role}: {path} opens")
                else:
                    try:
                        expect(blocked).to_be_visible(timeout=8000)
                        check(True, f"{role}: {path} blocked")
                    except AssertionError:
                        check(False, f"{role}: {path} should be blocked")
            page.locator(".user-chip").click()
            page.get_by_role("menuitem", name="Sign out").click()
            try:
                page.wait_for_url(re.compile(r".*/login$"), timeout=10000)
                check(True, f"{role}: sign out returns to /login")
            except Exception:  # noqa: BLE001
                check(False, f"{role}: sign out did not return to /login")
            ctx.close()
        browser.close()
    print(f"\n{'All checks passed.' if not fails else f'{fails} check(s) FAILED.'}")
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
