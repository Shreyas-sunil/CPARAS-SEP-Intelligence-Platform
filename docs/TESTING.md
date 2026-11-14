# Testing CPARAS (v0.5.0)

## Automated tests (no Ollama, no network)

```
cd backend
python -m pytest tests -q
```

`tests/conftest.py` points every data path and the database at a temporary folder, so the suite
never touches `backend/data`. Notable suites:

| File | Covers |
|---|---|
| `test_v050_auth.py` | Every API route classified in `auth/policy.py`; unauthenticated requests refused; first-run setup (token, one-time, password policy, cookie flags); Guest / Data Entry / Super Admin against ISLD, Bulk, reference-data and admin endpoints through the real `main.app`; cookie + Bearer sessions and logout; role change / deactivation take effect immediately; last-Super-Admin protection; password reset / change / policy; login lock-out; cross-origin write refusal; `seed-test-users` CLI. |
| `test_v049_matched_phrases.py`, `test_v041_overall_confidence.py` | ISLD exact matched phrases; Bulk essentiality / confidence columns. |
| `test_excel_export*.py`, `test_source_row_preservation.py`, `test_bulk_*.py` | Bulk parsing, scoring, row preservation, exports. |
| `test_text_match_*.py`, `test_cparas_*.py`, `test_retrieval_index.py` | ISLD, Text Matching, retrieval index. |

`test_v047_speed.py` needs the `tokenizers` and `transformers` packages (installed with the
backend requirements via sentence-transformers).

Frontend:

```
cd frontend
npm run build                     # TypeScript type-check + production build
node --experimental-strip-types --test src/cparas/lib/__tests__/etaEstimator.test.ts
```

## Checking the roles on a running installation

```
seed_test_users.cmd               # or ./seed_test_users.sh — prints three one-time passwords
python scripts/check_roles.py --guest test_guest:<pw> --data-entry test_data_entry:<pw> --super-admin test_super_admin:<pw>
python scripts/e2e_roles_ui.py --guest ... --data-entry ... --super-admin ...      # optional, needs Playwright
```

`check_roles.py` (standard library only) signs in as each role and prints a PASS/FAIL table for
representative ISLD, Bulk, reference-data and admin endpoints, plus landing page and sign-out.
`e2e_roles_ui.py` does the same in a real browser: landing page, sidebar contents, blocked
pages, sign out. Both are read-only.

## Manual checklist

1. First start with an empty database: the start script prints the first-run hint; the browser
   shows **First-time setup**; a wrong token is refused; the right one creates the Super Admin
   and the token file disappears.
2. As Super Admin: User Management lists accounts; create a Guest with a generated password.
3. Guest: signs in straight to ISLD Declaration Analysis, must choose a new password, sees no
   Bulk / reference / admin navigation; `/bulk` shows "You don't have access"; an ISLD analysis
   and Text Matching run; the Bulk record link in Text Matching is hidden.
4. Data Entry: home page with the **3GPP Retrieval Index** card (document count); Bulk upload,
   run, dashboard, registry, enriched Excel; Standards Corpus page is read-only; no User
   Management.
5. Super Admin changes the Data Entry user to Guest: that user's next action returns them to the
   sign-in page with "Your session ended".
6. Five wrong passwords → temporary lock-out message.

## Not covered by the automated tests

* Real LLM output (deep-mode enrichment, PDF extraction quality) — needs Ollama with a model.
* OCR of scanned PDFs — needs Tesseract.
* The Windows `.ps1` / `.cmd` wrappers (`create_admin`, `seed_test_users`, `cparas_users`,
  start/setup hints) — they call the same `python -m auth.cli` commands the Linux scripts and the
  tests exercise.
