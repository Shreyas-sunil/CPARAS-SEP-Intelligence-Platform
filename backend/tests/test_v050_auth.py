"""
v0.5.0 authentication + role-based access control, exercised through the REAL
application (main.app, every router mounted, the app-wide policy guard on).

Covers: deny-by-default route classification, first-run setup with the
one-time token, login/logout (cookie and Bearer), the three roles against
representative ISLD / Bulk / reference-data / admin endpoints, session
revocation on role change, the last-super-admin guard, login lockout,
cross-origin write refusal, password policy, deactivation, and the CLI's
test-account seeding.

Run: python -m pytest backend/tests/test_v050_auth.py -q
"""
import asyncio
import importlib
import os
import re
import subprocess
import sys

import pytest

BACKEND_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, BACKEND_DIR)

httpx = pytest.importorskip("httpx")

GOOD_PW = "Correct-Horse-42"


def _fresh_main(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'auth.db'}")
    monkeypatch.setenv("AUTH_SETUP_TOKEN_FILE", str(tmp_path / "SETUP_TOKEN.txt"))
    for mod in list(sys.modules):
        if mod in ("core.config", "core.database", "main") or mod.startswith("api.routes") or mod.startswith("auth"):
            del sys.modules[mod]
    m = importlib.import_module("main")
    importlib.import_module("auth.service").reset_login_failures()
    return m


def _client(m):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=m.app), base_url="http://127.0.0.1:8000")


def _run(coro):
    return asyncio.run(coro)


async def _setup_admin(m, tmp_path, c):
    await importlib.import_module("core.database").init_db()
    st = (await c.get("/api/auth/setup-status")).json()
    assert st["needs_setup"] is True
    token = (tmp_path / "SETUP_TOKEN.txt").read_text().strip()
    r = await c.post("/api/auth/setup", json={"setup_token": token, "username": "Root.Admin", "password": GOOD_PW})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _bearer(tok):
    return {"Authorization": f"Bearer {tok}"}


# ── Static policy checks ─────────────────────────────────────────────────────

def test_every_route_is_explicitly_classified(tmp_path, monkeypatch):
    m = _fresh_main(tmp_path, monkeypatch)
    from fastapi.routing import APIRoute
    from auth.policy import required_permission
    unclassified = [(meth, r.path) for r in m.app.routes if isinstance(r, APIRoute)
                    for meth in r.methods if required_permission(meth, r.path)[1] is None]
    assert unclassified == [], f"add these routes to auth/policy.py RULES: {unclassified}"


def test_guest_permissions_cover_isld_only():
    from auth.policy import required_permission
    from auth.roles import permissions_for
    guest = permissions_for("guest")
    assert required_permission("POST", "/api/analysis/analyze")[0] in guest
    assert required_permission("POST", "/api/text-match/jobs/file")[0] in guest
    for meth, path in [("POST", "/api/sep/upload"), ("GET", "/api/sep/runs"), ("GET", "/api/sep/patents/{patent_id}"),
                       ("GET", "/api/retrieval-index/status"), ("POST", "/api/etsi-corpus/dataset/import"),
                       ("GET", "/api/auth/users"), ("POST", "/api/models/pull")]:
        assert required_permission(meth, path)[0] not in guest, (meth, path)
    assert required_permission("GET", "/api/brand-new-endpoint")[0] == "users.manage"  # deny by default


# ── End-to-end through main.app ──────────────────────────────────────────────

def test_unauthenticated_requests_are_refused(tmp_path, monkeypatch):
    m = _fresh_main(tmp_path, monkeypatch)

    async def go():
        await importlib.import_module("core.database").init_db()
        async with _client(m) as c:
            assert (await c.get("/api/health")).status_code == 200
            for path in ("/api/sep/runs", "/api/analysis/history", "/api/auth/me", "/api/retrieval-index/status",
                         "/api/auth/users"):
                r = await c.get(path)
                assert r.status_code == 401, path
                assert r.headers.get("x-cparas-auth") == "auth_required"
                assert isinstance(r.json()["detail"], str)
            r = await c.post("/api/sep/upload")
            assert r.status_code == 401  # guard runs before body validation
    _run(go())


def test_first_run_setup_is_one_time_and_needs_the_token(tmp_path, monkeypatch):
    m = _fresh_main(tmp_path, monkeypatch)

    async def go():
        await importlib.import_module("core.database").init_db()
        async with _client(m) as c:
            st = (await c.get("/api/auth/setup-status")).json()
            assert st["needs_setup"] and st["setup_token_file"].endswith("SETUP_TOKEN.txt")
            assert "token" not in st
            bad = await c.post("/api/auth/setup", json={"setup_token": "nope", "username": "admin", "password": GOOD_PW})
            assert bad.status_code == 403
            token = (tmp_path / "SETUP_TOKEN.txt").read_text().strip()
            weak = await c.post("/api/auth/setup", json={"setup_token": token, "username": "admin", "password": "short"})
            assert weak.status_code == 422
            ok = await c.post("/api/auth/setup", json={"setup_token": token, "username": "admin", "password": GOOD_PW})
            assert ok.status_code == 200
            assert ok.json()["user"]["role"] == "super_admin"
            assert "cparas_session" in ok.headers.get("set-cookie", "")
            assert "httponly" in ok.headers["set-cookie"].lower() and "samesite=strict" in ok.headers["set-cookie"].lower()
            assert not (tmp_path / "SETUP_TOKEN.txt").exists()
            again = await c.post("/api/auth/setup", json={"setup_token": token, "username": "x2", "password": GOOD_PW})
            assert again.status_code == 409
            assert (await c.get("/api/auth/setup-status")).json()["needs_setup"] is False
    _run(go())


def test_three_roles_against_representative_endpoints(tmp_path, monkeypatch):
    m = _fresh_main(tmp_path, monkeypatch)

    async def go():
        async with _client(m) as c:
            admin = await _setup_admin(m, tmp_path, c)
            for uname, role in (("gina", "guest"), ("dave", "data_entry"), ("sam", "super_admin")):
                r = await c.post("/api/auth/users", headers=_bearer(admin),
                                 json={"username": uname, "role": role, "password": GOOD_PW, "must_change_password": False})
                assert r.status_code == 201, r.text
            toks = {}
            for uname in ("gina", "dave", "sam"):
                r = await c.post("/api/auth/login", json={"username": uname.upper(), "password": GOOD_PW})
                assert r.status_code == 200, r.text
                toks[uname] = r.json()["token"]

            me = (await c.get("/api/auth/me", headers=_bearer(toks["gina"]))).json()["user"]
            assert me["landing_path"] == "/isld" and me["permissions"] == ["isld.use"]
            assert (await c.get("/api/auth/me", headers=_bearer(toks["dave"]))).json()["user"]["landing_path"] == "/"

            # (method, path, json) -> expected status for guest / data_entry / super_admin
            cases = [
                (("GET", "/api/analysis/history", None), (200, 200, 200)),
                (("GET", "/api/text-match/jobs/doesnotexist", None), (404, 404, 404)),   # allowed -> handler's 404
                (("GET", "/api/sep/runs", None), (403, 200, 200)),
                (("POST", "/api/sep/upload", None), (403, 422, 422)),                    # allowed -> body validation
                (("GET", "/api/sep/patents", None), (403, 200, 200)),
                (("GET", "/api/retrieval-index/status", None), (403, 200, 200)),
                (("POST", "/api/models/pull", {"model_id": ""}), (403, None, None)),
                (("POST", "/api/etsi-corpus/dataset/import", None), (403, 403, 422)),
                (("GET", "/api/auth/users", None), (403, 403, 200)),
                (("GET", "/api/auth/events", None), (403, 403, 200)),
            ]
            for (meth, path, body), expected in cases:
                for uname, want in zip(("gina", "dave", "sam"), expected):
                    r = await c.request(meth, path, json=body, headers=_bearer(toks[uname]))
                    if want is None:
                        assert r.status_code != 403, (uname, meth, path, r.status_code)
                    else:
                        assert r.status_code == want, (uname, meth, path, r.status_code, r.text[:200])
                    if want == 403:
                        assert r.headers.get("x-cparas-auth") == "forbidden"
    _run(go())


def test_cookie_login_logout_and_bearer(tmp_path, monkeypatch):
    m = _fresh_main(tmp_path, monkeypatch)

    async def go():
        async with _client(m) as c:
            await _setup_admin(m, tmp_path, c)
            c.cookies.clear()
            r = await c.post("/api/auth/login", json={"username": "root.admin", "password": GOOD_PW})
            assert r.status_code == 200
            assert (await c.get("/api/auth/me")).status_code == 200        # cookie
            tok = r.json()["token"]
            assert (await c.post("/api/auth/logout")).status_code == 200
            c.cookies.clear()
            assert (await c.get("/api/auth/me", headers=_bearer(tok))).status_code == 401   # revoked
    _run(go())


def test_role_change_and_deactivation_take_effect_immediately(tmp_path, monkeypatch):
    m = _fresh_main(tmp_path, monkeypatch)

    async def go():
        async with _client(m) as c:
            admin = await _setup_admin(m, tmp_path, c)
            uid = (await c.post("/api/auth/users", headers=_bearer(admin),
                                json={"username": "dave", "role": "data_entry", "password": GOOD_PW})).json()["user"]["id"]
            tok = (await c.post("/api/auth/login", json={"username": "dave", "password": GOOD_PW})).json()["token"]
            assert (await c.get("/api/sep/runs", headers=_bearer(tok))).status_code == 200
            r = await c.patch(f"/api/auth/users/{uid}", headers=_bearer(admin), json={"role": "guest"})
            assert r.status_code == 200 and r.json()["changes"]
            assert (await c.get("/api/sep/runs", headers=_bearer(tok))).status_code == 401      # signed out
            tok = (await c.post("/api/auth/login", json={"username": "dave", "password": GOOD_PW})).json()["token"]
            assert (await c.get("/api/sep/runs", headers=_bearer(tok))).status_code == 403      # now a guest
            await c.patch(f"/api/auth/users/{uid}", headers=_bearer(admin), json={"is_active": False})
            assert (await c.post("/api/auth/login", json={"username": "dave", "password": GOOD_PW})).status_code == 403
    _run(go())


def test_last_super_admin_is_protected(tmp_path, monkeypatch):
    m = _fresh_main(tmp_path, monkeypatch)

    async def go():
        async with _client(m) as c:
            admin = await _setup_admin(m, tmp_path, c)
            my_id = (await c.get("/api/auth/me", headers=_bearer(admin))).json()["user"]["id"]
            assert (await c.patch(f"/api/auth/users/{my_id}", headers=_bearer(admin),
                                  json={"role": "guest"})).status_code == 409
            assert (await c.patch(f"/api/auth/users/{my_id}", headers=_bearer(admin),
                                  json={"is_active": False})).status_code == 409
            assert (await c.delete(f"/api/auth/users/{my_id}", headers=_bearer(admin))).status_code == 409
    _run(go())


def test_password_reset_change_and_policy(tmp_path, monkeypatch):
    m = _fresh_main(tmp_path, monkeypatch)

    async def go():
        async with _client(m) as c:
            admin = await _setup_admin(m, tmp_path, c)
            r = await c.post("/api/auth/users", headers=_bearer(admin), json={"username": "gina", "role": "guest"})
            gen = r.json()["generated_password"]
            uid = r.json()["user"]["id"]
            assert r.json()["user"]["must_change_password"] is True
            tok = (await c.post("/api/auth/login", json={"username": "gina", "password": gen})).json()["token"]
            assert (await c.post("/api/auth/password", headers=_bearer(tok),
                                 json={"current_password": "wrong", "new_password": GOOD_PW})).status_code == 403
            assert (await c.post("/api/auth/password", headers=_bearer(tok),
                                 json={"current_password": gen, "new_password": "aaaaaaaaaaaa"})).status_code == 422
            r = await c.post("/api/auth/password", headers=_bearer(tok), json={"current_password": gen, "new_password": GOOD_PW})
            assert r.status_code == 200 and r.json()["user"]["must_change_password"] is False
            assert (await c.get("/api/auth/me", headers=_bearer(tok))).status_code == 200   # own session kept
            r = await c.post(f"/api/auth/users/{uid}/password", headers=_bearer(admin), json={})
            assert r.status_code == 200 and r.json()["generated_password"]
            assert (await c.get("/api/auth/me", headers=_bearer(tok))).status_code == 401   # admin reset signs out
    _run(go())


def test_login_lockout_after_repeated_failures(tmp_path, monkeypatch):
    m = _fresh_main(tmp_path, monkeypatch)

    async def go():
        async with _client(m) as c:
            await _setup_admin(m, tmp_path, c)
            for _ in range(5):
                assert (await c.post("/api/auth/login", json={"username": "root.admin", "password": "bad"})).status_code == 401
            assert (await c.post("/api/auth/login", json={"username": "root.admin", "password": GOOD_PW})).status_code == 429
            # unknown user: same 401 as a wrong password
            r = await c.post("/api/auth/login", json={"username": "ghost", "password": "bad"})
            assert r.status_code == 401 and r.json()["detail"] == "Incorrect username or password."
    _run(go())


def test_cross_origin_cookie_write_is_refused(tmp_path, monkeypatch):
    m = _fresh_main(tmp_path, monkeypatch)

    async def go():
        async with _client(m) as c:
            await _setup_admin(m, tmp_path, c)   # leaves the session cookie on the client
            r = await c.post("/api/auth/users", json={"username": "xavier", "role": "guest"},
                             headers={"Origin": "http://evil.example"})
            assert r.status_code == 403 and r.headers.get("x-cparas-auth") == "bad_origin"
            r = await c.post("/api/auth/users", json={"username": "xavier", "role": "guest"},
                             headers={"Origin": "http://127.0.0.1:5173"})
            assert r.status_code == 201
    _run(go())


def test_cli_seed_test_users_creates_three_working_accounts(tmp_path, monkeypatch):
    env = dict(os.environ, DATABASE_URL=f"sqlite+aiosqlite:///{tmp_path / 'cli.db'}",
               AUTH_SETUP_TOKEN_FILE=str(tmp_path / "SETUP_TOKEN.txt"))
    out = subprocess.run([sys.executable, "-m", "auth.cli", "seed-test-users"], cwd=BACKEND_DIR, env=env,
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    creds = dict(re.findall(r"^\s+(test_\w+)\s+(?:Guest|Data Entry|Super Admin)\s+(\S+)\s*$", out.stdout, re.M))
    assert set(creds) == {"test_guest", "test_data_entry", "test_super_admin"}
    assert len(set(creds.values())) == 3  # random, distinct
    listed = subprocess.run([sys.executable, "-m", "auth.cli", "list-users"], cwd=BACKEND_DIR, env=env,
                            capture_output=True, text=True, timeout=120)
    assert "test_super_admin" in listed.stdout and "Super Admin" in listed.stdout

    # monkeypatch restores the session-wide isolated DATABASE_URL (tests/conftest.py)
    # afterwards, so later tests never fall back to the real backend/data database.
    monkeypatch.setenv("DATABASE_URL", env["DATABASE_URL"])
    monkeypatch.setenv("AUTH_SETUP_TOKEN_FILE", env["AUTH_SETUP_TOKEN_FILE"])

    async def verify():
        for mod in list(sys.modules):
            if mod in ("core.config", "core.database") or mod.startswith("auth"):
                del sys.modules[mod]
        from core.database import AsyncSessionLocal
        from auth import service
        async with AsyncSessionLocal() as db:
            for u, pw in creds.items():
                assert (await service.authenticate(db, u, pw)).username == u
    _run(verify())


def test_same_origin_behind_reverse_proxy_is_accepted(tmp_path, monkeypatch):
    """Docker/nginx on a LAN address: the Origin's host:port matches X-Forwarded-Host
    (nginx sends $http_host), so signed-in writes work; a different origin is refused."""
    m = _fresh_main(tmp_path, monkeypatch)

    async def go():
        async with _client(m) as c:
            await _setup_admin(m, tmp_path, c)
            ok = await c.post("/api/auth/users", json={"username": "lan.user", "role": "guest"},
                              headers={"Origin": "http://192.168.1.20:3000", "X-Forwarded-Host": "192.168.1.20:3000"})
            assert ok.status_code == 201, ok.text
            bad = await c.post("/api/auth/users", json={"username": "lan.user2", "role": "guest"},
                               headers={"Origin": "http://192.168.1.99:3000", "X-Forwarded-Host": "192.168.1.20:3000"})
            assert bad.status_code == 403
    _run(go())


def test_illustrative_mode_is_gone(tmp_path, monkeypatch):
    """v0.5.0: the demo-corpus switch and its permission no longer exist."""
    m = _fresh_main(tmp_path, monkeypatch)
    paths = {getattr(r, "path", "") for r in m.app.routes}
    assert not any(p.startswith("/api/illustrative") for p in paths)
    from auth.roles import ALL_PERMISSIONS
    assert not any("illustrative" in p for p in ALL_PERMISSIONS)


def test_legacy_demo_chunks_are_removed_at_startup(tmp_path, monkeypatch):
    """Chunks left by the retired switch are deleted (bookkeeping rows + vector store);
    real chunks stay."""
    _fresh_main(tmp_path, monkeypatch)

    async def go():
        import core.database as db
        from ingestion import legacy_cleanup
        await db.init_db()
        async with db.AsyncSessionLocal() as s:
            s.add_all([db.KnowledgeChunk(source_type="3gpp_spec_text", source_id="illustrative_38.331-5.3.3-v15.3.0", content="demo"),
                       db.KnowledgeChunk(source_type="3gpp_spec_text", source_id="3gpp_spec_text_real_1", content="real")])
            await s.commit()
        deleted = []

        class FakeStore:
            async def delete_by_id_prefix(self, prefix):
                deleted.append(prefix)
                return 1
        monkeypatch.setattr("rag.vector_store.get_vector_store", lambda: FakeStore())
        assert await legacy_cleanup.remove_legacy_demo_chunks() == 1
        assert deleted == ["illustrative_"]
        assert await legacy_cleanup.remove_legacy_demo_chunks() == 0      # nothing left -> no-op
        async with db.AsyncSessionLocal() as s:
            from sqlalchemy import select
            ids = [r.source_id for r in (await s.execute(select(db.KnowledgeChunk))).scalars()]
        assert ids == ["3gpp_spec_text_real_1"]
    _run(go())
