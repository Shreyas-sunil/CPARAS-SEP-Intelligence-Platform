# SEP Dashboard Backend + Frontend — Integration Guide

**This version adds the actual GITAS frontend** (the dashboard UI shown in
your screenshots) on top of the previously-delivered `/api/sep/*` backend.
cPARAS — the original single-declaration deep-analysis tool — still exists,
now as one page inside the same app rather than the whole app. Both are
built, typechecked, and were smoke-tested end to end against your real
`Data_For_Testing.xlsx` before this was packaged (details at the bottom).

This adds a second, additive capability to the existing SEP Intelligence
Platform backend so it can serve **both**:

1. **Existing function** — the single-declaration PDF/CSV/XLSX pipeline under
   `/api/analysis/*`, `/api/search/*`, `/api/models/*`, `/api/datasets/*`.
   Nothing here was changed; the `cPARAS` UI (now at `/analyze` inside the
   frontend, see below) still talks to it exactly as before.
2. **New: bulk SEP dashboard** — `/api/sep/*`, and now a full frontend for
   it: **GITAS — Gateway to IPR in Telecom Arena including SEPs**, matching
   the screenshots and `SEPRepositoryPlatform_UI-UX.pdf` you shared.

## What's new in this version — the frontend

`frontend/src/` is now two apps sharing one Vite build, wired together with
`react-router-dom`:

- **`frontend/src/gitas/`** — the primary shell. Top nav (About / Dashboard
  / Resources / FAQs / Facilitation Center / Reports / Chatbot / cPARAS +
  Login), footer, and:
  - `pages/DashboardPage.tsx` — the four tabs from your screenshots
    (Overview, Technical View, Portfolio View, Legal View), each backed by
    the matching `/api/sep/dashboard/*` endpoint. Includes a **run/dataset
    selector** — if you upload more than one workbook, the dashboard
    defaults to the most recently uploaded one instead of silently merging
    every upload together; "All runs" is available explicitly.
  - `pages/PatentDetailPage.tsx` — essentiality score breakdown, claim-to-
    standard mapping with amber term highlighting, 3GPP spec context panel,
    related patents, export buttons — matches `GET /patents/{id}` exactly.
  - `pages/CompanyProfilePage.tsx` — per-company KPI tiles plus release and
    domain breakdown and patent table, from `GET /entity/company/{name}`.
  - `pages/StaticPages.tsx` — About / Resources / FAQs / Facilitation
    Center / Reports / Chatbot — real content, not lorem ipsum, written
    around what this platform actually does.
  - `components/UploadPanel.tsx` — drag-and-drop bulk workbook upload,
    posts to `/api/sep/upload`, shown as an empty-state prompt when no run
    exists yet and as a compact button in the dashboard header otherwise.
  - `api.ts` — typed client for every `/api/sep/*` endpoint.
  - `styles/gitas.css` — scoped entirely under `.gitas-app` (light theme,
    navy/blue, matches your screenshots).
- **`frontend/src/cparas/`** — the original single-page app, moved here
  unchanged (same imports, same logic), now mounted at **`/analyze`**. Its
  dark theme is scoped under `.cparas-scope` (previously applied to
  `:root`/`body` globally) so navigating between GITAS and cPARAS never
  bleeds one theme into the other.
- **`frontend/src/App.tsx`** — the router. `/` redirects to `/dashboard`;
  `/analyze` is cPARAS; everything else is GITAS.

Run it exactly as before — `npm install && npm run dev` in `frontend/`,
`vite.config.ts`'s `/api` proxy is unchanged.

## Bugs found and fixed while integrating (not just code review — hit these
running the real app against your real file)

1. **`api/routes/sep.py` wouldn't boot.** `from __future__ import
   annotations` at the top of the file, combined with the class-based
   `Filters` FastAPI `Depends()` further down, breaks pydantic 2.8.2's
   forward-reference resolution (`NameError: name 'Optional' is not
   defined` at import time — the whole app failed to start). Removed the
   future-annotations import; nothing else in the file needed it.
2. **Every patent showed `release: "Unknown"`.** `etsi_bulk/scoring.py`'s
   `_release_label()` matched spec versions like `"16.5.0"` but not
   `"v16.5.0"` (a `v`-prefixed form the offline standards corpus itself
   uses internally). Your actual `Data_For_Testing.xlsx` doesn't use the
   prefix, so this wouldn't have hit you today, but a bulk export that does
   include it would have silently lost all release faceting. Fixed to
   accept an optional `v`/`V` prefix.

## How the Excel upload is handled

`Data_For_Testing.xlsx` is the **ETSI bulk IPR-declaration export** format:
one row per patent-family-member per declared standard, with columns like
`IPRD_ID`, `COMP_LEGAL_NAME`, `Standard`, `TGPP_NUMBER`/`TGPV_VERSION`,
`PATT_APPLICATION_NUMBER`, `PUBL_NUMBER`, `Illustrative_Part`,
`Legal Status`, `2G`/`3G`/`4G`/`5G`. Handled by `etsi_bulk/parser.py` rather
than the fuzzy single-declaration header matcher.

`POST /api/sep/upload` (`multipart/form-data`, field `file`):
1. Detects the sheet whose header matches the bulk-export schema (a
   `Summary` sheet alongside the data sheet, as in your file, is scanned
   and correctly skipped).
2. Runs essentiality determination for every row via `etsi_bulk/scoring.py`
   — same embedder, same offline 3GPP standards corpus, same
   Confirmed/Likely/Potential thresholds as the single-declaration
   pipeline. Output per patent: `essentiality`, `confidence`,
   `claim_match` / `semantic_similarity` / `standard_coverage`,
   `matched_standard` / `matched_clause` / `matched_clause_title`.
3. Persists to `sep_bulk_runs` / `sep_bulk_patents` and returns the scored
   rows immediately.

## API reference (`/api/sep/*`)

| Endpoint | Purpose | Screenshot it feeds |
|---|---|---|
| `POST /upload` | Upload & score a bulk workbook | Dashboard empty-state / header upload |
| `GET /runs` | List uploaded runs | Dashboard run selector |
| `GET /filters` | Distinct Technology/Release/Company/Country values | Sidebar filter panel |
| `GET /dashboard/overview` | Total SEPs, essential count, avg score, top release, specs covered | KPI tiles |
| `GET /dashboard/companies` | Top companies stacked by essentiality | "Top Companies by SEP Count" |
| `GET /dashboard/timeline` | SEPs declared per year, cumulative | "SEP Declarations Over Time" |
| `GET /dashboard/countries` | SEPs by country | "SEPs by Country" donut |
| `GET /dashboard/by-release` | SEPs per release, stacked by essentiality | "SEPs by Release" bars |
| `GET /dashboard/portfolio` | Per-company totals table | "Company Portfolio Overview" |
| `GET /dashboard/heatmap` | Release × Domain matrix of SEP counts | "SEP Coverage Heatmap" |
| `GET /patents` | Filterable, paginated patent registry | "Legal View" table |
| `GET /patents/{id}` | Full patent detail | Patent detail page |
| `GET /entity/company/{name}` | Company profile | "View Profile →" pages |

All list/dashboard endpoints accept the same filter query params (repeat a
key for multi-select, matching the sidebar's checkboxes):
`run_id`, `technology`, `release`, `company`, `country`, `essentiality`.

### `GET /patents/{id}` response shape (maps directly to the detail page)

```json
{
  "id": 42,
  "application_number": "US10987654",
  "title": "...",
  "essentiality_determination": {
    "status": "Essential",
    "overall_confidence": 78.0,
    "score_breakdown": {
      "claim_match": 75.0,
      "semantic_similarity": 81.0,
      "standard_coverage": 68.0
    },
    "disclaimer": "Determined by NLP & ML analysis ... not a legal opinion."
  },
  "claim_to_standard_mapping": {
    "patent_claim_simplified": "...",
    "matched_standard_section": "TS 38.211 v16.5.0 — Clause 5.2: ...",
    "match_confidence": 75.0,
    "matched_key_terms": ["multi-trp", "qcl type d configuration"],
    "matched_key_terms_count": 3,
    "total_key_terms": 4,
    "summary": "3 of 4 key technical terms aligned"
  },
  "spec_context": {
    "spec_number": "38.214",
    "spec_version": "16.5.0",
    "total_seps": 234,
    "essential": 156,
    "potential": 54,
    "non_essential": 24,
    "families": 89
  },
  "declaration": {
    "isld_reference": "ISLD-202203-219",
    "note": "Build the ETSI IPR database search link from this reference on the frontend — no fixed URL pattern is assumed here."
  },
  "related_patents": [ ... up to 6, same technical domain ... ],
  "family_count": 9
}
```

- `matched_key_terms` is the list the UI highlights (amber background) in
  both the "Patent Claim (Simplified)" and "Matched Standard Section"
  panels on `PatentDetailPage.tsx` — matched client-side via string search,
  no character offsets needed from the backend.
- `declaration.isld_reference` is the raw ETSI declaration reference. The
  "Open ISLD Document" button on the detail page is intentionally disabled
  (shown, not clickable) until a real ETSI IPR search URL pattern is
  verified — no fabricated link is wired up.

## Running it

```bash
# backend
cd backend
pip install -r requirements.txt
uvicorn main:app --reload --port 8000

# frontend (separate terminal)
cd frontend
npm install
npm run dev   # http://localhost:5173 — GITAS at /dashboard, cPARAS at /analyze
```

If no local embedding model has been pulled yet, `etsi_bulk/scoring.py`
automatically falls back to a fully offline TF-IDF similarity mode — scoring
still works, just with a lower-fidelity similarity signal until a real
embedding model is available. This was the mode used for the verification
below.

## What was actually verified (not just built)

Before packaging, this was run for real, not just typechecked:

- `npx tsc --noEmit` and `npm run build` — clean, no errors.
- Backend booted with `uvicorn`, hit every `/api/sep/*` endpoint with
  `curl` and confirmed response shapes against what the frontend expects.
- Uploaded your actual `Data_For_Testing.xlsx` (25 rows) — confirmed it
  parses, scores, and now correctly derives `Rel-4` / `Rel-8` / `Rel-16` /
  `Rel-18` / legacy releases from its version strings.
- Served the production build via `vite preview` proxied to the live
  backend, and used a headless browser to screenshot: all four dashboard
  tabs, a patent detail page, a company profile page, `/analyze` (cPARAS,
  confirmed its dark theme doesn't leak into GITAS pages or vice versa),
  and a static page — all rendering real, live-scored data correctly.
- Found and fixed one hooks-order bug (`useMemo` called after an early
  `return` in `CompanyProfilePage.tsx`) that only surfaced once real data
  with a `release` field reached that page — caught via a browser
  `pageerror` listener, not by inspection.

