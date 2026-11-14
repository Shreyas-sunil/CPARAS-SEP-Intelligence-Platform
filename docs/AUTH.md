# CPARAS — sign-in, roles and administration (v0.5.0)

Since v0.5.0 every CPARAS page and every API call (except the health check and the sign-in
endpoints) requires a signed-in user. What a user may do is decided by their **role**, and it is
enforced on the **backend** for every request — hiding a menu item in the browser is only a
convenience on top.

Everything is offline: accounts live in the local SQLite database (`backend/data/sep_intel.db`),
passwords are hashed with scrypt (Python standard library), and no external identity service is
contacted.

## Roles

| | Guest | Data Entry | Super Admin |
|---|:---:|:---:|:---:|
| CPARAS ISLD — Declaration Analysis, history, ISLD Excel export, Text Matching | ✔ | ✔ | ✔ |
| Local-LLM status and the model test button (inside ISLD) | ✔ | ✔ | ✔ |
| Download a new LLM model (`ollama pull`) | | ✔ | ✔ |
| CPARAS Bulk — uploads, runs, dashboard, registry, patent/company pages, enriched Excel, PDF report | | ✔ | ✔ |
| View the 3GPP Retrieval Index and Standards Corpus pages | | ✔ | ✔ |
| Change reference data — dataset imports, 3GPP crawls/indexing, corpus packages, bulk exports of corpus internals | | | ✔ |
| User Management — create/delete accounts, change roles, reset passwords, audit log | | | ✔ |

* **Guest** signs in straight to **CPARAS ISLD → Declaration Analysis**. Bulk, reference data and
  administration are hidden in the navigation; opening such a URL directly shows "You don't have
  access to this page", and the API answers `403`.
* **Data Entry** lands on the platform home and uses both products with all their workflows.
  The Standards Corpus page is read-only for this role.
* **Super Admin** additionally sees **Administration → User Management**.

The exact permission names are in `backend/auth/roles.py`; the route → permission table is
`backend/auth/policy.py`.

## First-time setup — creating the first Super Admin

There are **no default accounts or passwords**. Create the first Super Admin in one of two ways:

**A. On the server machine (recommended)** — no token needed, because only someone who can run
commands on that computer can do it:

```
create_admin.cmd                         (Windows: double-click, or run in the project folder)
./create_admin.sh                        (Linux / macOS)
cd backend && python -m auth.cli create-superadmin --username admin    (any OS, backend venv active)
```

You are asked for a username and the password (typed twice at a hidden prompt).

**B. In the browser** — open the app. While no account exists, the sign-in page shows
**First-time setup**. It needs a one-time token that the backend wrote to a file **on the server
machine** (by default `backend\data\FIRST_ADMIN_SETUP_TOKEN.txt`; the page and the start script
show the exact path). Paste the file's contents, choose username and password. The token file is
deleted as soon as the first Super Admin exists, and the setup page disappears for good.

The start scripts (`start.cmd` / `start.sh`) print a reminder while no account exists.

## Test accounts (for trying all three roles)

```
seed_test_users.cmd                      (Windows)
./seed_test_users.sh                     (Linux / macOS)
cd backend && python -m auth.cli seed-test-users
```

creates `test_guest`, `test_data_entry` and `test_super_admin` with **random passwords printed
once** in the console. Run it again with `--reset` to get new passwords (e.g. if you lost them).
They are ordinary accounts — deactivate or delete them before using CPARAS in production
(User Management page, or `python -m auth.cli set-active --username test_guest --inactive`).

To check all three roles automatically against a running installation:

```
python scripts/check_roles.py --guest test_guest:<pw> --data-entry test_data_entry:<pw> --super-admin test_super_admin:<pw>
python scripts/e2e_roles_ui.py ...same arguments...      (browser check; needs Playwright, optional)
```

`check_roles.py` uses only the Python standard library and only makes requests that never change
data; it prints a PASS/FAIL table per endpoint and role.

## Managing users

In the app: **Administration → User Management** (Super Admin).

* **Add a user**: username, role, optional display name. Leave the password empty to have a strong
  one generated — it is shown **once**; give it to the user privately. By default the user must
  choose their own password at first sign-in.
* **Change role / Deactivate / Reset password / Delete**: each takes effect immediately — the user
  is signed out of every open session.
* The **last active Super Admin** can never be demoted, deactivated or deleted, and nobody can
  delete their own account, so the installation can never lock itself out.
* **Recent sign-in & admin activity** lists sign-ins, failed sign-ins, lock-outs and every account
  change with who made it.

Users change their own password from the account menu (top right) → **Change password**; their
other sessions are signed out.

Command line (all of it works with the app running or stopped):

```
cparas_users.cmd list-users
cparas_users.cmd create-user --username alice --role data_entry          (prompts for the password)
cparas_users.cmd create-user --username bob --role guest --generate --must-change
cparas_users.cmd reset-password --username alice --generate
cparas_users.cmd set-role --username alice --role guest
cparas_users.cmd set-active --username alice --inactive
```

(`./cparas_users.sh ...` on Linux/macOS; `python -m auth.cli ...` from the backend folder anywhere.)

**Locked out?** If every Super Admin password is lost, run on the server machine:
`cparas_users.cmd reset-password --username <admin> --generate`, or create a new Super Admin with
`create_admin.cmd`.

## Sessions and security

| | |
|---|---|
| Passwords | scrypt (N=2^14, r=8, p=1, 16-byte salt) via `hashlib`; at least 10 characters mixing at least two of lowercase / uppercase / digits / symbols, not equal to the username. Never logged or returned. |
| Sessions | random 256-bit token; only its SHA-256 is stored (`user_sessions`). Ends after `AUTH_SESSION_IDLE_MINUTES` (default 480) without activity, after `AUTH_SESSION_MAX_HOURS` (72) at the latest, on sign-out, and when an admin changes the account. |
| Browser | the token is an **HttpOnly, SameSite=Strict** cookie scoped to `/api` (JavaScript cannot read it; other sites cannot send it). Set `AUTH_COOKIE_SECURE=true` when serving over HTTPS. |
| Cross-site writes | state-changing requests authenticated by the cookie must come from this app's own origin (or `CORS_EXTRA_ORIGINS`); others get `403`. |
| Brute force | after `AUTH_LOGIN_MAX_FAILURES` (5) wrong passwords for one username from one computer, sign-in is refused for `AUTH_LOGIN_LOCKOUT_MINUTES` (10). Unknown usernames and wrong passwords get the same message and take the same time. |
| API clients | `POST /api/auth/login` also returns the token; send it as `Authorization: Bearer <token>`. |
| Deny by default | an endpoint that is not listed in `auth/policy.py` is Super-Admin-only; a test fails if any route is unclassified. |
| Audit | `auth_events` table — sign-ins (also failed / locked), sign-outs, account changes, first-run setup. |

`HOST=127.0.0.1` (default) still keeps the API on the local machine. If you expose CPARAS on a
network, put it behind HTTPS (reverse proxy) and set `AUTH_COOKIE_SECURE=true`.

## Settings (`backend/.env`)

| Setting | Default | |
|---|---|---|
| `AUTH_SESSION_IDLE_MINUTES` | 480 | sign out after this long without activity |
| `AUTH_SESSION_MAX_HOURS` | 72 | absolute session lifetime |
| `AUTH_COOKIE_SECURE` | false | true when served over HTTPS |
| `AUTH_COOKIE_NAME` | cparas_session | |
| `AUTH_PASSWORD_MIN_LENGTH` | 10 | |
| `AUTH_LOGIN_MAX_FAILURES` / `AUTH_LOGIN_LOCKOUT_MINUTES` | 5 / 10 | brute-force lock-out |
| `AUTH_SETUP_TOKEN_FILE` | ./data/FIRST_ADMIN_SETUP_TOKEN.txt | one-time first-run token (relative to `backend/`) |

There is deliberately no setting that turns authentication off.

## API

| Endpoint | Who | |
|---|---|---|
| `POST /api/auth/login` `{username, password}` | anyone | sets the cookie, returns `{user, token, expires_in}` |
| `POST /api/auth/logout` | signed in | ends this session |
| `GET /api/auth/me` | signed in | user, role, `permissions`, `landing_path` |
| `POST /api/auth/password` `{current_password, new_password}` | signed in | own password |
| `GET /api/auth/roles` | signed in | role catalogue |
| `GET /api/auth/setup-status` · `POST /api/auth/setup` | anyone, only while no account exists | first-run setup |
| `GET/POST /api/auth/users`, `PATCH/DELETE /api/auth/users/{id}`, `POST /api/auth/users/{id}/password` | Super Admin | user management |
| `GET /api/auth/events` | Super Admin | audit trail |

Errors: `401` (not signed in / session ended) with header `X-CPARAS-Auth: auth_required`; `403`
(role not allowed) with `X-CPARAS-Auth: forbidden` and `X-CPARAS-Permission: <permission>`.
`detail` is always a human-readable string.

## Integrating CPARAS into a larger platform

* **Single source of truth for authorization** is `auth/policy.py` (route → permission) plus
  `auth/roles.py` (role → permissions). A host platform can map its own roles/claims onto the
  permission strings (`isld.use`, `bulk.use`, `reference.view`, `reference.manage`,
  `models.pull`, `users.manage`).
* **Replacing the login** (SSO / OIDC / a gateway): implement a resolver that turns the incoming
  request (e.g. a verified gateway header or JWT) into a user with a role, and use it in
  `auth/dependencies.resolve_user`; the policy check, the route handlers and the frontend (which
  only reads `GET /api/auth/me`) stay unchanged.
* **Service-to-service calls** use the Bearer token from `/api/auth/login` (create a dedicated
  account with the minimal role).
* The frontend broadcasts `cparas:auth-expired` on `window` whenever a call reports an ended
  session (`frontend/src/platform/auth/http.ts`) — a host shell can listen for it to start its own
  sign-in flow.
