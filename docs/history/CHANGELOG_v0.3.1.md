# v0.3.1 Changelog

Fixes and features delivered in this pass. All 297 backend tests pass
(`cd backend && python -m pytest tests/ -q`); frontend passes `tsc --noEmit`
and `vite build` cleanly.

## 1. Fixed: bulk import silently dropped rows (22/25 bug)

**Root cause**: `backend/api/routes/gitas/dashboard.py`'s `_natural_key()`
(used to dedupe rows on import) keyed only on `dipg_id + standard`. A
single ETSI family-member id can legitimately cover several distinct
national/regional publications declared against the *same* standard (e.g.
one invention filed in EP, KR, and US under one DIPG_ID) — the old key
collapsed those into one record, silently dropping the rest.

**Fix**: the key now also folds in publication number (falling back to
application number), so each distinct family member is kept, while
re-importing the same workbook stays idempotent. A second bug surfaced
during testing — `publication_number`/`application_number` can arrive as
`int` from openpyxl on purely numeric identifiers (e.g. Indian application
numbers), which crashed the new key with `AttributeError: 'int' object has
no attribute 'strip'` — fixed with a defensive stringify helper.

Verified end-to-end against the real parser: 25 rows in, 25 unique
records out.

## 2. Added: Illustrative Mode

A one-click toggle (Corpus page, cPARAS Text Matching tab, C-SARAS
dashboard) that indexes a small offline reference corpus (54 clause-
versions across 11 3GPP families / 9 domains, from the existing
`research/standards_corpus.py`) into the vector store, so Text Matching
and related search work immediately without first crawling and indexing
the real 38_series archive.

- `backend/ingestion/illustrative_corpus.py` — builds/enables/disables the
  indexed chunks, tagged `illustrative: true` and id-prefixed so they can
  be cleanly removed without touching any real crawled/uploaded data.
- `backend/api/routes/shared/illustrative.py` — `GET/POST /api/illustrative/{status,enable,disable}`.
- `delete_by_id_prefix()` added to all three vector-store backends (FAISS/Qdrant/Chroma).
- `frontend/src/shared/IllustrativeModeToggle.tsx` — one reusable component, themed for both GITAS and cPARAS/C-SARAS visual languages.
- 5 new unit tests in `backend/tests/test_illustrative_corpus.py`.

## 3. Fixed: C-SARAS ignored the light/dark theme toggle

Both C-SARAS pages hardcoded `data-theme="dark"` on their cPARAS-styled
wrapper regardless of GITAS's own theme toggle. Now reads the real theme
via `useGitasTheme()`.

## 4. Added: CSV/XLSX/XLS upload support in C-SARAS

Previously rejected outright with a 415. `csaras/pipeline.py` now parses
spreadsheets with the same row-parser Text Matching already uses
(`utils.csv_parser` / `utils.xlsx_parser`), classifying and summarising the
declared-row text like any other document. A soft informational hint is
still shown if the upload looks like a large multi-declarant bulk export,
pointing at GITAS's dashboard import for that use case — but it's no
longer a hard rejection.

## 5. Reorganized file structure

**Backend**: `api/routes/` split into `gitas/`, `cparas/`, `csaras/`, and
`shared/` subpackages (routes, not just naming) so each product's HTTP
layer can be maintained independently. `main.py` and all test imports
updated accordingly.

**Frontend**: C-SARAS's three files moved out of `gitas/pages/` into their
own `frontend/src/csaras/` folder. New `frontend/src/shared/` folder for
genuinely cross-product pieces (Illustrative Mode).

## 6. Populated About & Resources pages

**About**: now covers all three tools (previously omitted C-SARAS
entirely), explains Illustrative Mode, keeps the data/disclaimer section.

**Resources**: real external links (ETSI IPR Database, 3GPP Specification
Series, TSDSI IPR Portal, WIPO's FRAND/standards page) plus internal
quick-links into the platform's own tools (3GPP Corpus, C-SARAS, Text
Matching, FAQs) — replacing the previous four static, link-less cards.

## 7. Nav bar: boxed tool links for C-SARAS and 3GPP Corpus

Previously only cPARAS had a boxed nav entry. C-SARAS and 3GPP Corpus now
get the same treatment, grouped together as a distinct "tools" cluster
(desktop and mobile nav), instead of blending into the plain-text link
list.
