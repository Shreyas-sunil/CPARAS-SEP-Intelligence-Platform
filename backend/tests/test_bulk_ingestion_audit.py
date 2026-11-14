"""
CPARAS Bulk — ingestion & evidence-gate regressions found in the
independent verification pass.

Each case here failed before the fixes:

  * a workbook whose header sits below a title/notes block was rejected
    outright ("Unrecognized file format", HTTP 422) even though every data
    row in it was readable -- only row 1 was ever tried as the header;
  * a semicolon- or tab-separated CSV (what Excel writes when the machine's
    list separator is not a comma) was rejected the same way, because the
    CSV reader hard-coded a comma and `utf-8-sig`;
  * a CSV was read with no row ceiling at all, unlike every other format;
  * "Essential" could be asserted for a row whose best match was a
    DIFFERENT standard family than the one declared -- a 500-row sample
    returned 496 Essential while the customer's own acceptance rule
    (Essential = evidence level 3 = declared family confirmed) gave 9, both
    derived from the same export;
  * CPARAS_Bulk_Corpus_Coverage was set to `declared_family_match`, so
    "the reference corpus has no entry for your declared standard" and
    "the scorer matched a different family" were reported identically.
"""
from __future__ import annotations

import csv
import importlib
import io
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import openpyxl  # noqa: E402
import pytest  # noqa: E402

from etsi_bulk.parser import parse_bulk_workbook, MAX_ROWS  # noqa: E402

HEADER = ["DIPG_DISPLAY_NUMBER", "COMP_LEGAL_NAME", "PATT_APPLICATION_NUMBER", "PUBL_NUMBER",
          "Standard", "PBPA_TITLEEN", "Illustrative_Part"]
ROWS = [
    ["D-1", "Acme", "APP0001", "PUB0001", "3GPP TS 38.331 version 16.5.0",
     "Conditional handover method", "Embodiment 1"],
    ["D-2", "Beta", "APP0002", "PUB0002", "3GPP TS 36.331 version 13.0.0",
     "Measurement reporting", "Embodiment 2"],
]


def _xlsx(rows):
    wb = openpyxl.Workbook()
    ws = wb.active
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _csv(rows, delimiter=",", encoding="utf-8"):
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=delimiter)
    for r in rows:
        w.writerow(r)
    return buf.getvalue().encode(encoding)


# ── header row discovery ────────────────────────────────────────────────

def test_header_on_row_one_still_works():
    r = parse_bulk_workbook("a.xlsx", _xlsx([HEADER] + ROWS))
    assert r["ok"] and len(r["records"]) == 2
    assert [rec["_source_row_index"] for rec in r["records"]] == [0, 1]


def test_header_below_a_title_block_is_found():
    data = _xlsx([["ETSI IPR declarations export"], ["Generated 2026-10-01"], [], HEADER] + ROWS)
    r = parse_bulk_workbook("b.xlsx", data)
    assert r["ok"], r["warnings"]
    assert len(r["records"]) == 2
    # source row indices count from BELOW the header, so the enriched export
    # can put each row's enrichment back on the right row
    assert [rec["_source_row_index"] for rec in r["records"]] == [0, 1]
    assert r["records"][0]["company"] == "Acme"
    assert any("title block" in w for w in r["warnings"])


def test_a_sheet_with_no_recognisable_header_anywhere_is_still_rejected():
    r = parse_bulk_workbook("c.xlsx", _xlsx([["a", "b"], ["1", "2"], ["3", "4"]]))
    assert not r["ok"]
    assert r["records"] == []


# ── CSV dialect / encoding ──────────────────────────────────────────────

@pytest.mark.parametrize("delimiter", [",", ";", "\t", "|"])
def test_csv_delimiters_are_sniffed(delimiter):
    r = parse_bulk_workbook("d.csv", _csv([HEADER] + ROWS, delimiter=delimiter))
    assert r["ok"], f"delimiter {delimiter!r} rejected: {r['warnings']}"
    assert len(r["records"]) == 2
    assert r["records"][0]["standard_raw"] == "3GPP TS 38.331 version 16.5.0"


def test_cp1252_csv_decodes_without_mojibake():
    rows = [HEADER, ["D-1", "Nokia Oy Ab — Espoo", "APP1", "PUB1",
                     "3GPP TS 38.331 version 16.5.0", "Méthode de transfert", "E1"]]
    r = parse_bulk_workbook("e.csv", _csv(rows, encoding="cp1252"))
    assert r["ok"]
    assert r["records"][0]["company"] == "Nokia Oy Ab — Espoo"
    assert "�" not in r["records"][0]["title"]


def test_csv_has_the_same_row_ceiling_as_a_workbook():
    rows = [HEADER] + [["D-%d" % i, "Acme", "APP%d" % i, "PUB%d" % i,
                        "3GPP TS 38.331 version 16.5.0", "t", "e"]
                       for i in range(MAX_ROWS + 50)]
    r = parse_bulk_workbook("f.csv", _csv(rows))
    assert r["ok"]
    assert len(r["records"]) == MAX_ROWS
    assert any("more than" in w for w in r["warnings"]), r["warnings"]


# ── declared-family evidence gate ───────────────────────────────────────

def _patent_row(**over):
    row = {"company": "Acme", "standard_raw": "3GPP TS 38.331 version 16.5.0",
           "title": "Conditional handover with RRC reconfiguration",
           "illustrative_part": "RRC reconfiguration and handover command",
           "explicit_disclosure": "Yes", "gen_5g": "Y", "_source_row_index": 0}
    row.update(over)
    return row


def test_essential_requires_the_declared_family_to_be_confirmed():
    """A near-perfect similarity to a family OTHER than the declared one
    must not be reported as Essential -- that label asserts essentiality to
    the DECLARED standard, and nothing here evidences that."""
    from etsi_bulk.scoring import _finalize_row
    from research.standards_corpus import STANDARDS_CORPUS

    entry = next(e for e in STANDARDS_CORPUS if e.family == "38.331")
    other = next(e for e in STANDARDS_CORPUS if e.family != "38.331")

    confirmed = _finalize_row(_patent_row(), entry, 0.99, True)
    assert confirmed["declared_family_match"] is True
    assert confirmed["essentiality"] == "Essential"

    unconfirmed = _finalize_row(_patent_row(), other, 0.99, True)
    assert unconfirmed["declared_family_match"] is False
    assert unconfirmed["essentiality"] == "Potential", (
        "a top match in a different family than declared was reported as Essential")
    # the confidence itself is NOT rewritten -- only the label is gated,
    # so the number and the reason stay inspectable
    assert unconfirmed["confidence"] > 0.6


def test_non_essential_is_not_promoted_by_the_gate():
    from etsi_bulk.scoring import _finalize_row
    from research.standards_corpus import STANDARDS_CORPUS

    other = next(e for e in STANDARDS_CORPUS if e.family != "38.331")
    row = _finalize_row(_patent_row(explicit_disclosure="No"), other, 0.01, False)
    assert row["essentiality"] == "Non-Essential"


def test_every_essential_row_in_a_batch_is_declared_family_confirmed():
    import asyncio
    from etsi_bulk.scoring import score_records

    out_of_corpus = ["3GPP TS 45.008 version 8.6.0", "3GPP TS 48.008 version 7.5.0",
                     "3GPP TS 25.331 version 7.0.0", "3GPP TS 29.244 version 16.0.0"]
    in_corpus = ["3GPP TS 38.331 version 16.5.0", "3GPP TS 23.501 version 16.4.0"]
    rows = []
    for i in range(60):
        std = out_of_corpus[i % len(out_of_corpus)] if i % 5 else in_corpus[i % len(in_corpus)]
        rows.append(_patent_row(standard_raw=std, _source_row_index=i))

    scored, _meta = asyncio.run(score_records(rows))
    assert len(scored) == 60
    essential = [r for r in scored if r["essentiality"] == "Essential"]
    assert all(r["declared_family_match"] for r in essential), (
        "an Essential row had no declared-family confirmation")


# ── corpus coverage is a real signal ────────────────────────────────────

class _FakePatent:
    """Minimal stand-in for a persisted SepBulkPatent row."""
    id = 1

    def __init__(self, **kw):
        defaults = dict(
            standard_raw="3GPP TS 38.331 version 16.5.0", spec_number="38.331", spec_version="16.5.0",
            title="t", illustrative_part="e", essentiality="Potential", confidence=0.5,
            claim_match=0.5, semantic_similarity=0.5, standard_coverage=0.5,
            matched_standard="TS 38.331 v16.5.0", matched_clause="5.3.5",
            matched_clause_title="RRC Reconfiguration", matched_domain="RRC",
            declared_family_match=True, legal_status="Active", release="Rel-16",
            technology="5G NR", company="Acme", application_number="APP1",
            publication_number="PUB1", country_code="US",
        )
        defaults.update(kw)
        for k, v in defaults.items():
            setattr(self, k, v)


def test_corpus_coverage_reports_the_corpus_not_the_match():
    from etsi_bulk.excel_export import gitas_fields_for_patent

    # declared family IS in the corpus, but the scorer matched another one
    mismatch = gitas_fields_for_patent(
        _FakePatent(declared_family_match=False, matched_standard="TS 23.501 v17.4.0"), run_id=1)
    assert mismatch["CPARAS_Bulk_Corpus_Coverage"] is True
    codes = mismatch["CPARAS_Bulk_Reason_Codes"]
    assert "OUTSIDE_CORPUS_COVERAGE" in codes
    assert "DECLARED_FAMILY_NOT_IN_CORPUS" not in codes

    # declared family is NOT in the corpus at all -- a different condition
    absent = gitas_fields_for_patent(
        _FakePatent(standard_raw="3GPP TS 45.008 version 8.6.0", spec_number="45.008",
                    declared_family_match=False, matched_standard="TS 33.501 v17.4.0"), run_id=1)
    assert absent["CPARAS_Bulk_Corpus_Coverage"] is False
    assert "DECLARED_FAMILY_NOT_IN_CORPUS" in absent["CPARAS_Bulk_Reason_Codes"]


def test_evidence_level_and_essentiality_agree():
    from etsi_bulk.excel_export import gitas_fields_for_patent

    confirmed = gitas_fields_for_patent(
        _FakePatent(essentiality="Essential", declared_family_match=True, confidence=0.8), run_id=1)
    assert confirmed["CPARAS_Bulk_Evidence_Level"] == 3
    assert confirmed["CPARAS_Bulk_Classification"] == "Essential"

    strong_topical = gitas_fields_for_patent(
        _FakePatent(essentiality="Potential", declared_family_match=False, confidence=0.8), run_id=1)
    assert strong_topical["CPARAS_Bulk_Evidence_Level"] == 2
    assert strong_topical["CPARAS_Bulk_Classification"] == "Potential"

    weak = gitas_fields_for_patent(
        _FakePatent(essentiality="Potential", declared_family_match=False, confidence=0.45), run_id=1)
    assert weak["CPARAS_Bulk_Evidence_Level"] == 1


# ── end to end: a title-block workbook must enrich the RIGHT rows ───────

@pytest.fixture()
def bulk_app(tmp_path, monkeypatch):
    db_path = tmp_path / "audit.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    core_config = importlib.import_module("core.config")
    core_config.settings.DATABASE_URL = f"sqlite+aiosqlite:///{db_path}"
    for mod in list(sys.modules):
        if mod == "core.database" or mod.startswith("api.routes") or mod.startswith("etsi_bulk.excel_export"):
            del sys.modules[mod]
    core_database = importlib.import_module("core.database")
    sep_routes = importlib.import_module("api.routes.gitas.dashboard")
    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(sep_routes.router, prefix="/api/sep")
    return app, core_database, sep_routes


def _run(app, coro_fn):
    import asyncio
    import httpx

    async def go():
        await importlib.import_module("core.database").init_db()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=120) as client:
            return await coro_fn(client)
    return asyncio.run(go())


def test_title_block_workbook_end_to_end_enriches_the_correct_rows(bulk_app):
    app, _core_database, _routes = bulk_app
    title_block = [["ETSI IPR declarations export"], ["Generated 2026-10-01"], []]
    rows = [
        ["D-%d" % i, "Company %d" % i, "APP%04d" % i, "PUB%04d" % i,
         "3GPP TS 38.331 version 16.5.0", "Method variant %d" % i, "Embodiment %d" % i]
        for i in range(1, 9)
    ]
    data = _xlsx(title_block + [HEADER] + rows)

    async def scenario(client):
        import asyncio
        r = await client.post("/api/sep/upload",
                              files={"file": ("titleblock.xlsx", data, "application/octet-stream")})
        assert r.status_code == 200, r.text
        run_id = r.json()["run_id"]
        assert r.json()["total_rows"] == len(rows)
        status = None
        for _ in range(3000):
            status = (await client.get(f"/api/sep/runs/{run_id}/status")).json()
            if status["status"] in ("completed", "completed_with_errors", "failed"):
                break
            await asyncio.sleep(0.02)
        assert status["status"] in ("completed", "completed_with_errors"), status
        export = await client.get(f"/api/sep/runs/{run_id}/export/excel")
        assert export.status_code == 200, export.text
        return export.content

    xlsx_bytes = _run(app, scenario)
    wb = openpyxl.load_workbook(io.BytesIO(xlsx_bytes), data_only=True)
    ws = wb["Sheet1"]
    all_rows = list(ws.iter_rows(values_only=True))

    # the original title block is preserved verbatim, above the header
    assert all_rows[0][0] == "ETSI IPR declarations export"
    header_out = list(all_rows[3])
    assert header_out[:len(HEADER)] == HEADER
    assert "CPARAS_Bulk_Essentiality" in header_out

    data_out = [r for r in all_rows[4:] if any(v not in (None, "") for v in r)]
    assert len(data_out) == len(rows), f"expected {len(rows)} data rows, got {len(data_out)}"

    ess_col = header_out.index("CPARAS_Bulk_Essentiality")
    title_col = HEADER.index("PBPA_TITLEEN")
    for i, out_row in enumerate(data_out):
        # original values untouched, on their own row
        assert out_row[title_col] == rows[i][5], f"row {i} original title moved"
        assert out_row[0] == rows[i][0]
        # and the enrichment is present on every row
        assert out_row[ess_col] not in (None, ""), f"row {i} got no enrichment"


# ── country code derivation ─────────────────────────────────────────────
# Before the fix `country_code` took the first whitespace token and cut it
# to three characters, assuming every cell read "US UNITED STATES". The
# shipped sample export carries "UNITED STATES" and "EUROPEAN PATENT
# OFFICE", which became "UNI" and "EUR" -- the dashboard's country donut
# and country filter were keyed on word fragments, and a file mixing "US"
# with "UNITED STATES" produced two buckets for one jurisdiction.

@pytest.mark.parametrize("raw,expected", [
    ("UNITED STATES", "US"),
    ("United States of America", "US"),
    ("US UNITED STATES", "US"),
    ("us", "US"),
    ("EUROPEAN PATENT OFFICE", "EP"),
    ("EP", "EP"),
    ("Germany", "DE"),
    ("DE GERMANY", "DE"),
    ("KOREA, REPUBLIC OF", "KR"),
    ("GB", "GB"),
    ("", ""),
    (None, ""),
])
def test_country_code_resolves_names_and_codes(raw, expected):
    from etsi_bulk.schema import country_code
    assert country_code(raw) == expected


def test_country_code_does_not_truncate_an_unknown_value():
    from etsi_bulk.schema import country_code
    # better a verbatim value the UI can show than a 3-letter fragment
    # that looks like a code and isn't
    assert country_code("Freedonia") == "FREEDONIA"


def test_us_spelled_two_ways_group_into_one_bucket():
    from etsi_bulk.schema import country_code
    assert country_code("US") == country_code("UNITED STATES") == country_code("US UNITED STATES")


def test_shipped_sample_export_yields_real_country_codes():
    from etsi_bulk.schema import country_code, resolve_header
    path = os.path.join(os.path.dirname(__file__), "..", "..", "sample_data", "sample_bulk_export.xlsx")
    if not os.path.exists(path):
        pytest.skip("sample_bulk_export.xlsx not present")
    with open(path, "rb") as fh:
        parsed = parse_bulk_workbook("sample_bulk_export.xlsx", fh.read())
    assert parsed["ok"]
    codes = {country_code(r.get("country_raw")) for r in parsed["records"] if r.get("country_raw")}
    assert codes, "sample export has no country values"
    assert codes <= {"US", "EP"}, f"sample export produced non-code buckets: {sorted(codes)}"


# ── upload size limit ───────────────────────────────────────────────────

def test_oversized_upload_is_rejected_with_413(bulk_app, monkeypatch):
    """MAX_UPLOAD_MB existed in config but was enforced nowhere, so the
    whole body was buffered, parsed with the workbook fully resident, and
    stored again as original_file_blob."""
    app, _core_database, _routes = bulk_app
    import core.config
    monkeypatch.setattr(core.config.settings, "MAX_UPLOAD_MB", 1)

    big = b"x" * (1024 * 1024 + 2048)

    async def scenario(client):
        return await client.post("/api/sep/upload",
                                 files={"file": ("big.xlsx", big, "application/octet-stream")})

    resp = _run(app, scenario)
    assert resp.status_code == 413, resp.text
    assert "MAX_UPLOAD_MB" in resp.text


def test_empty_upload_is_rejected_with_400(bulk_app):
    app, _core_database, _routes = bulk_app

    async def scenario(client):
        return await client.post("/api/sep/upload",
                                 files={"file": ("empty.xlsx", b"", "application/octet-stream")})

    resp = _run(app, scenario)
    assert resp.status_code == 400, resp.text


def test_a_normal_upload_is_still_accepted(bulk_app):
    app, _core_database, _routes = bulk_app
    data = _xlsx([HEADER] + ROWS)

    async def scenario(client):
        return await client.post("/api/sep/upload",
                                 files={"file": ("ok.xlsx", data, "application/octet-stream")})

    resp = _run(app, scenario)
    assert resp.status_code == 200, resp.text
    assert resp.json()["total_rows"] == 2
