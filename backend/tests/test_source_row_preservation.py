"""
Source-row preservation (no deduplication) — CPARAS Bulk and CPARAS ISLD.

Requirement: every uploaded source row is authoritative. Rows that share an
application number, publication number, title, company -- or that are
completely identical -- must ALL survive parsing, storage, enrichment and
export, in their original order, with the original values intact and the
enrichment ADDED to each row.

Covers:
  * parser level (XLSX / XLS / CSV, repeated identifiers, identical rows,
    rows without identifiers, large files)
  * end-to-end through the real FastAPI routes: upload -> background scoring
    -> stored records -> enriched Excel export (row count, order, values,
    enrichment present on every row, alignment by source row)
  * re-uploading the same file creates a second complete run and never
    touches the first run's records
  * pause/continue never loses or double-stores a source row
  * CPARAS ISLD: LLM-extracted disclosures and tabular (CSV) declarations
    keep repeated identifiers

Run: python -m pytest backend/tests/test_source_row_preservation.py -q
"""
from __future__ import annotations

import asyncio
import csv
import importlib
import io
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import openpyxl  # noqa: E402
import pytest  # noqa: E402

from etsi_bulk.parser import parse_bulk_workbook  # noqa: E402

HEADER = [
    "IPRD_REFERENCE", "DIPG_ID", "DIPG_DISPLAY_NUMBER", "COMP_LEGAL_NAME", "Standard",
    "TGPP_NUMBER", "TGPV_VERSION", "PATT_APPLICATION_NUMBER", "PUBL_NUMBER",
    "PBPA_TITLEEN", "Illustrative_Part", "Country_Of_Registration", "My Custom Note",
]


def _row(i: int, *, app=None, pub=None, title=None, company=None, dipg=None):
    return [
        f"ISLD-{i}", dipg if dipg is not None else f"D{i}", f"DISC{i}",
        company or f"Company {i % 4}", "3GPP TS 38.331 version 16.5.0", "38.331", "16.5.0",
        app if app is not None else f"APP{i:04d}",
        pub if pub is not None else f"PUB{i:04d}",
        title or f"Method variant {i}", f"Embodiment {i}", "UNITED STATES", f"note-{i}",
    ]


def ten_rows_with_three_duplicate_identifiers() -> list[list]:
    """10 source rows; rows 2/5, 4/7 and 6/9 (1-based) share an application
    number AND publication number AND DIPG id -- 3 intentional duplicate
    identifiers. Anything that dedups on identifiers would return 7."""
    rows = [_row(i) for i in range(1, 11)]
    for a, b in ((2, 5), (4, 7), (6, 9)):
        src = rows[a - 1]
        rows[b - 1][1] = src[1]   # DIPG_ID
        rows[b - 1][7] = src[7]   # application number
        rows[b - 1][8] = src[8]   # publication number
    return rows


def _xlsx(header, rows) -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    ws.append(header)
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _csv(header, rows) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    for r in rows:
        w.writerow(["" if v is None else v for v in r])
    return buf.getvalue().encode("utf-8")


def _xls(header, rows) -> bytes:
    xlwt = pytest.importorskip("xlwt")
    wb = xlwt.Workbook()
    ws = wb.add_sheet("Sheet1")
    for j, h in enumerate(header):
        ws.write(0, j, h)
    for i, r in enumerate(rows, start=1):
        for j, v in enumerate(r):
            if v is not None:
                ws.write(i, j, v)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ═════════════════════════════════════════════════════════════════════════
# Parser level
# ═════════════════════════════════════════════════════════════════════════

def _assert_records_match_rows(records, rows):
    assert len(records) == len(rows)
    for i, (rec, row) in enumerate(zip(records, rows)):
        assert rec["_source_row_index"] == i
        assert str(rec["application_number"]) == str(row[7])
        assert str(rec["publication_number"]) == str(row[8])
        assert rec["title"] == row[9]
        assert rec["company"] == row[3]


@pytest.mark.parametrize("fmt", ["xlsx", "xls", "csv"])
def test_parser_ten_rows_three_duplicate_identifiers_gives_ten_records(fmt):
    rows = ten_rows_with_three_duplicate_identifiers()
    data = {"xlsx": _xlsx, "xls": _xls, "csv": _csv}[fmt](HEADER, rows)
    result = parse_bulk_workbook(f"upload.{fmt}", data)
    assert result["ok"], result["warnings"]
    assert len(result["records"]) == 10, "duplicate identifiers must NOT collapse rows (expected 10, not 7)"
    _assert_records_match_rows(result["records"], rows)
    assert not any("duplicate" in w.lower() for w in result["warnings"])


def test_parser_repeated_publication_numbers_titles_companies_and_identical_rows():
    rows = [
        _row(1, pub="SAMEPUB"), _row(2, pub="SAMEPUB"), _row(3, pub="SAMEPUB"),       # repeated publication numbers
        _row(4, title="Same title"), _row(5, title="Same title"),                     # repeated titles
        _row(6, company="Nokia"), _row(7, company="Nokia"), _row(8, company="Nokia"),  # repeated companies
    ]
    identical = _row(9)
    rows += [list(identical), list(identical), list(identical)]                       # fully identical rows
    result = parse_bulk_workbook("upload.xlsx", _xlsx(HEADER, rows))
    assert result["ok"]
    _assert_records_match_rows(result["records"], rows)
    assert [r["_source_row_index"] for r in result["records"]] == list(range(len(rows)))


def test_parser_keeps_rows_without_any_identifier():
    rows = [_row(1), _row(2, app="", pub=""), _row(3)]
    rows[1][2] = None  # no disclosure number either
    result = parse_bulk_workbook("upload.xlsx", _xlsx(HEADER, rows))
    assert result["ok"]
    assert len(result["records"]) == 3
    assert result["records"][1]["title"] == "Method variant 2"
    assert result["skipped_unkeyable_rows"] == 0


def test_parser_blank_rows_keep_source_positions_of_later_rows():
    rows = [_row(1), [None] * len(HEADER), _row(3)]
    result = parse_bulk_workbook("upload.xlsx", _xlsx(HEADER, rows))
    assert [r["_source_row_index"] for r in result["records"]] == [0, 2]


def test_parser_large_excel_file_with_heavy_repetition():
    # 20,000 rows but only 100 distinct identifiers (each repeated 200x).
    n = 20_000
    rows = []
    for i in range(n):
        r = _row(i, app=f"APP{i % 100}", pub=f"PUB{i % 100}", dipg=f"D{i % 100}", title=f"T{i % 100}")
        rows.append(r)
    result = parse_bulk_workbook("big.xlsx", _xlsx(HEADER, rows))
    assert result["ok"]
    assert len(result["records"]) == n
    assert result["records"][-1]["_source_row_index"] == n - 1
    assert result["records"][12345]["title"] == "T45"


# ═════════════════════════════════════════════════════════════════════════
# End-to-end: upload -> scoring -> DB -> enriched export
# ═════════════════════════════════════════════════════════════════════════

@pytest.fixture()
def bulk_app(tmp_path, monkeypatch):
    db_path = tmp_path / "rows.db"
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


async def _upload_and_wait(client, filename, data, *, expect="completed"):
    resp = await client.post("/api/sep/upload", files={"file": (filename, data, "application/octet-stream")})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    run_id = body["run_id"]
    s = None
    for _ in range(3000):
        s = (await client.get(f"/api/sep/runs/{run_id}/status")).json()
        if s["status"] in ("completed", "completed_with_errors", "failed"):
            break
        await asyncio.sleep(0.02)
    assert s["status"] == expect, s
    return run_id, body


async def _all_patents(client, run_id, order="source"):
    out, offset = [], 0
    while True:
        page = (await client.get("/api/sep/patents", params={"run_id": run_id, "limit": 500, "offset": offset, "order": order})).json()
        out += page["patents"]
        offset += len(page["patents"])
        if not page["patents"] or offset >= page["total"]:
            return out, page["total"]


def _read_export(xlsx_bytes):
    wb = openpyxl.load_workbook(io.BytesIO(xlsx_bytes), data_only=True)
    ws = wb["Sheet1"]
    all_rows = list(ws.iter_rows(values_only=True))
    return wb, all_rows[0], all_rows[1:]


def _run(app, coro_fn):
    import httpx

    async def go():
        await importlib.import_module("core.database").init_db()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=120) as client:
            return await coro_fn(client)
    return asyncio.run(go())


def _assert_export_preserves(header_out, data_out, header_in, rows_in, patents):
    n_in = len(header_in)
    # original columns first, unchanged and in order; enrichment appended
    assert list(header_out[:n_in]) == header_in
    enrich_cols = list(header_out[n_in:])
    assert enrich_cols and all(str(c).startswith("CPARAS_Bulk_") for c in enrich_cols)
    assert not any("GITAS" in str(c) for c in header_out)
    # same number of rows, same order, same values
    data_out = [r for r in data_out if any(v not in (None, "") for v in r)]
    assert len(data_out) == len(rows_in), f"expected {len(rows_in)} output rows, got {len(data_out)}"
    idx = {h: i for i, h in enumerate(header_out)}
    by_id = {p["id"]: p for p in patents}
    for i, (out_row, in_row) in enumerate(zip(data_out, rows_in)):
        for j in range(n_in):
            want, got = in_row[j], out_row[j]
            assert (str(want) if want not in (None, "") else None) == (str(got) if got not in (None, "") else None), \
                f"row {i} col {header_in[j]!r}: {want!r} != {got!r}"
        # enrichment present on every row and belongs to THIS source row
        assert out_row[idx["CPARAS_Bulk_Status"]] in ("SUCCESS", "FAILED")
        link = out_row[idx["CPARAS_Bulk_Report_Link"]]
        pid = int(str(link).rsplit("/", 1)[-1])
        assert by_id[pid]["source_row_index"] == i


def test_e2e_ten_rows_three_duplicate_ids_stored_and_exported_as_ten(bulk_app):
    app, _db, _routes = bulk_app
    rows = ten_rows_with_three_duplicate_identifiers()

    async def flow(client):
        run_id, body = await _upload_and_wait(client, "dups.xlsx", _xlsx(HEADER, rows))
        assert body["total_rows"] == 10
        patents, total = await _all_patents(client, run_id)
        exp = await client.get(f"/api/sep/runs/{run_id}/export/excel")
        assert exp.status_code == 200
        assert exp.headers["content-disposition"].split(';')[1].strip().endswith('_CPARAS_Bulk_enriched.xlsx"')
        overview = (await client.get("/api/sep/dashboard/overview", params={"run_id": run_id})).json()
        return patents, total, exp.content, overview

    patents, total, xlsx_bytes, overview = _run(app, flow)
    assert total == 10, "expected 10 stored records, NOT 7"
    assert overview["total_seps"] == 10
    assert [p["source_row_index"] for p in patents] == list(range(10))
    assert [p["application_number"] for p in patents] == [r[7] for r in rows]
    wb, header_out, data_out = _read_export(xlsx_bytes)
    assert "CPARAS Bulk Summary" in wb.sheetnames
    _assert_export_preserves(header_out, data_out, HEADER, rows, patents)


def test_e2e_identical_rows_and_repeated_fields_all_survive(bulk_app):
    app, _db, _routes = bulk_app
    identical = _row(50)
    rows = [
        _row(1, pub="P-SAME"), _row(2, pub="P-SAME"),
        _row(3, title="Same"), _row(4, title="Same"),
        _row(5, company="Ericsson"), _row(6, company="Ericsson"),
        list(identical), list(identical), list(identical), list(identical),
        _row(7, app="", pub=""),  # no identifiers at all
    ]

    async def flow(client):
        run_id, _ = await _upload_and_wait(client, "same.xlsx", _xlsx(HEADER, rows))
        patents, total = await _all_patents(client, run_id)
        exp = await client.get(f"/api/sep/runs/{run_id}/export/excel")
        return patents, total, exp.content

    patents, total, xlsx_bytes = _run(app, flow)
    assert total == len(rows) == 11
    _wb, header_out, data_out = _read_export(xlsx_bytes)
    _assert_export_preserves(header_out, data_out, HEADER, rows, patents)


@pytest.mark.parametrize("fmt", ["csv", "xls"])
def test_e2e_csv_and_xls_preserve_rows(bulk_app, fmt):
    app, _db, _routes = bulk_app
    rows = ten_rows_with_three_duplicate_identifiers()
    data = _csv(HEADER, rows) if fmt == "csv" else _xls(HEADER, rows)

    async def flow(client):
        run_id, _ = await _upload_and_wait(client, f"dups.{fmt}", data)
        patents, total = await _all_patents(client, run_id)
        exp = await client.get(f"/api/sep/runs/{run_id}/export/excel")
        assert exp.status_code == 200
        return patents, total, exp.content

    patents, total, xlsx_bytes = _run(app, flow)
    assert total == 10
    _wb, header_out, data_out = _read_export(xlsx_bytes)
    _assert_export_preserves(header_out, data_out, HEADER, rows, patents)


def test_e2e_blank_row_stays_in_place_and_alignment_holds(bulk_app):
    app, _db, _routes = bulk_app
    rows = [_row(1), _row(2, app="APP0001"), [None] * len(HEADER), _row(4, app="APP0001"), _row(5)]

    async def flow(client):
        run_id, _ = await _upload_and_wait(client, "blank.xlsx", _xlsx(HEADER, rows))
        patents, total = await _all_patents(client, run_id)
        exp = await client.get(f"/api/sep/runs/{run_id}/export/excel")
        return patents, total, exp.content

    patents, total, xlsx_bytes = _run(app, flow)
    assert total == 4
    assert [p["source_row_index"] for p in patents] == [0, 1, 3, 4]
    _wb, header_out, data_out = _read_export(xlsx_bytes)
    assert len(data_out) == 5
    assert all(v in (None, "") for v in data_out[2])  # blank source row kept, not enriched
    idx = {h: i for i, h in enumerate(header_out)}
    by_id = {p["id"]: p for p in patents}
    for pos in (0, 1, 3, 4):
        pid = int(str(data_out[pos][idx["CPARAS_Bulk_Report_Link"]]).rsplit("/", 1)[-1])
        assert by_id[pid]["source_row_index"] == pos
        assert data_out[pos][0] == rows[pos][0]


def test_e2e_reupload_creates_second_full_run_and_keeps_first(bulk_app):
    app, _db, _routes = bulk_app
    rows = ten_rows_with_three_duplicate_identifiers()
    data = _xlsx(HEADER, rows)

    async def flow(client):
        run1, _ = await _upload_and_wait(client, "same.xlsx", data)
        run2, _ = await _upload_and_wait(client, "same.xlsx", data)
        _p1, t1 = await _all_patents(client, run1)
        p2, t2 = await _all_patents(client, run2)
        exp1 = await client.get(f"/api/sep/runs/{run1}/export/excel")
        all_total = (await client.get("/api/sep/patents", params={"limit": 1})).json()["total"]
        return t1, t2, p2, exp1.content, all_total

    t1, t2, p2, exp1, all_total = _run(app, flow)
    assert (t1, t2) == (10, 10)
    assert all_total == 20
    assert [p["source_row_index"] for p in p2] == list(range(10))
    _wb, _h, data_out = _read_export(exp1)
    assert len([r for r in data_out if any(v not in (None, "") for v in r)]) == 10


def test_e2e_large_excel_with_repeated_identifiers(bulk_app):
    app, _db, _routes = bulk_app
    n = 3000
    rows = [_row(i, app=f"APP{i % 50}", pub=f"PUB{i % 50}", dipg=f"D{i % 50}", title=f"Title {i % 50}")
            for i in range(n)]

    async def flow(client):
        run_id, _ = await _upload_and_wait(client, "large.xlsx", _xlsx(HEADER, rows))
        patents, total = await _all_patents(client, run_id)
        exp = await client.get(f"/api/sep/runs/{run_id}/export/excel")
        return patents, total, exp.content

    patents, total, xlsx_bytes = _run(app, flow)
    assert total == n
    assert [p["source_row_index"] for p in patents] == list(range(n))
    _wb, header_out, data_out = _read_export(xlsx_bytes)
    _assert_export_preserves(header_out, data_out, HEADER, rows, patents)


def test_e2e_pause_continue_with_duplicates_never_loses_or_doubles_rows(bulk_app):
    app, _db, _routes = bulk_app
    import etsi_bulk.scoring as scoring_mod
    old = scoring_mod.settings.SCORING_BATCH_SIZE
    scoring_mod.settings.SCORING_BATCH_SIZE = 2
    rows = [_row(i, app="APP-X" if i % 2 else f"APP{i}") for i in range(24)]
    try:
        async def flow(client):
            resp = await client.post("/api/sep/upload", files={"file": ("p.xlsx", _xlsx(HEADER, rows), "application/octet-stream")})
            run_id = resp.json()["run_id"]
            for _ in range(500):
                if (await client.post(f"/api/sep/runs/{run_id}/pause")).status_code == 200:
                    break
                await asyncio.sleep(0.002)
            for _ in range(500):
                s = (await client.get(f"/api/sep/runs/{run_id}/status")).json()
                if s["status"] != "processing":
                    break
                await asyncio.sleep(0.01)
            if s["status"] == "paused":
                r = await client.post(f"/api/sep/runs/{run_id}/continue")
                assert r.status_code == 200
                for _ in range(1000):
                    s = (await client.get(f"/api/sep/runs/{run_id}/status")).json()
                    if s["status"] in ("completed", "completed_with_errors", "failed"):
                        break
                    await asyncio.sleep(0.01)
            assert s["status"] == "completed", s
            return await _all_patents(client, run_id)

        patents, total = _run(app, flow)
    finally:
        scoring_mod.settings.SCORING_BATCH_SIZE = old
    assert total == 24
    assert sorted(p["source_row_index"] for p in patents) == list(range(24))


def test_persist_batch_is_idempotent_per_source_row_but_keeps_lookalikes(bulk_app):
    """The only persistence guard is (run_id, source_row_index): the SAME
    source row processed twice is stored once, while two DIFFERENT source
    rows with identical content are both stored."""
    app, core_database, routes = bulk_app

    async def go():
        await core_database.init_db()
        async with core_database.AsyncSessionLocal() as db:
            run = core_database.SepBulkRun(filename="x.xlsx", total_rows=3, parsed_rows=3, status="processing")
            db.add(run)
            await db.commit()
            await db.refresh(run)
            same = {"application_number": "A1", "publication_number": "P1", "title": "T", "company": "C"}
            batch = [dict(same, _source_row_index=0), dict(same, _source_row_index=1), dict(same, _source_row_index=2)]
            n1 = await routes._persist_batch(db, run.id, batch)
            n2 = await routes._persist_batch(db, run.id, batch[1:])  # resumed too early: rows 1-2 again
            count = await routes._stored_row_count(db, run.id)
            return n1, n2, count

    n1, n2, count = asyncio.run(go())
    assert (n1, n2, count) == (3, 0, 3)


def test_no_unique_constraint_on_identifier_columns(bulk_app):
    _app, core_database, _routes = bulk_app
    from sqlalchemy import text

    async def go():
        await core_database.init_db()
        async with core_database.engine.connect() as conn:
            idx = (await conn.execute(text("PRAGMA index_list(sep_bulk_patents)"))).fetchall()
            unique_cols = []
            for row in idx:
                name, unique = row[1], row[2]
                if unique:
                    cols = (await conn.execute(text(f"PRAGMA index_info({name})"))).fetchall()
                    unique_cols.append(tuple(c[2] for c in cols))
            return unique_cols

    unique_cols = asyncio.run(go())
    banned = {"application_number", "publication_number", "title", "company", "iprd_reference",
              "dipg_id", "natural_key", "disclosure_number"}
    for cols in unique_cols:
        assert not (set(cols) & banned), f"identifier-based uniqueness constraint found: {cols}"
    assert ("run_id", "source_row_index") in unique_cols


# ═════════════════════════════════════════════════════════════════════════
# CPARAS ISLD
# ═════════════════════════════════════════════════════════════════════════

def test_isld_llm_extraction_keeps_repeated_identifiers():
    """_analyze_in_chunks used to drop any disclosure whose application
    number (and similar title) had already been seen ("Dropping duplicate
    disclosure ... Deduplicated X -> Y"). Every extracted row must now
    survive, in order."""
    analysis = importlib.import_module("api.routes.cparas.analysis")

    discs = []
    for i in range(10):
        app_no = {4: "US111", 6: "US222", 8: "US333"}.get(i, f"US{i:03d}")
        if i in (1, 3, 5):
            app_no = {1: "US111", 3: "US222", 5: "US333"}[i]
        discs.append({"explicit_data": {"application_number": app_no, "title": f"Title for {app_no}",
                                         "standards": ["TS 38.331"]}})
    payload = json.dumps({"declaration_metadata": {"declarant": "Acme"}, "patent_disclosures": discs})

    class FakeLLM:
        async def generate(self, *a, **k):
            return payload

    out = asyncio.run(analysis._analyze_in_chunks("short declaration text", "sys", "{text}", FakeLLM(), "decl.pdf"))
    result = json.loads(out)
    got = result["patent_disclosures"]
    assert len(got) == 10, "expected 10 disclosures, NOT 7"
    assert [d["explicit_data"]["application_number"] for d in got] == [d["explicit_data"]["application_number"] for d in discs]
    assert [d["disclosure_number"] for d in got] == list(range(1, 11))


def test_isld_tabular_declaration_keeps_repeated_identifiers():
    from utils.csv_parser import parse_csv_declaration
    from core.prompts import slim_to_full

    header = ["Application No.", "Publication No.", "Title", "Country of registration", "Project or Standard name"]
    body = [
        ["US111", "US-P-1", "Same title", "UNITED STATES", "TS 38.331"],
        ["US111", "US-P-1", "Same title", "UNITED STATES", "TS 38.300"],
        ["US222", "US-P-2", "Other", "UNITED STATES", "TS 38.331"],
        ["US111", "US-P-1", "Same title", "UNITED STATES", "TS 38.331"],
    ]
    data = _csv(header, body)
    slim = parse_csv_declaration(data, "decl.csv")
    full = slim_to_full(slim.get("slim") or slim)
    assert slim["meta"]["rows_parsed"] == 4
    eds = [d["explicit_data"] for d in full["patent_disclosures"]]
    apps = [ed.get("application_number") or ed.get("_raw_application_number") for ed in eds]
    assert apps == ["US111", "US111", "US222", "US111"]
