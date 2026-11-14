"""
v0.4.4 - canonical 3GPP dataset integration (shared contract + streaming importer + lookups + API).

Fixture: backend/tests/fixtures/build_sample/ is the REAL output of build_cparas_dataset.py 2.0 run on a
tiny synthetic extraction (9 records: 38.331 x3 + one unknown-version draft, 38.211 with non-ASCII text,
36.321 x2, 33.501, 23.501; one record has a large spec_history moved to the gzip sidecar). The full
32,440-record dataset is NOT needed to run these tests.

Run: python -m pytest backend/tests/test_gpp_dataset_integration.py -q
"""
import asyncio
import gzip
import importlib
import json
import os
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
FIXTURE = Path(__file__).parent / "fixtures" / "build_sample"
UNKNOWN_DOC = "38_series_38.331_unversioned.zip_draft.docx"


def _copy_fixture(tmp: Path, name: str = "build v4 (copy)") -> Path:
    """Copies the fixture into a folder whose name contains spaces and parentheses (Windows-style path)."""
    dst = tmp / name
    shutil.copytree(FIXTURE, dst)
    return dst


def _gd():
    return importlib.import_module("core.gpp_dataset")


def _fresh(db_path, out_dir, chroma, root=None, batch=3):
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{db_path}"
    os.environ["ETSI_DATASET_OUTPUT"] = out_dir
    os.environ["ETSI_IMPORT_BATCH_SIZE"] = str(batch)
    os.environ["ETSI_STREAM_BATCH"] = "4"
    os.environ["CHROMADB_PATH"] = chroma
    os.environ["VECTOR_BACKEND"] = "chromadb"
    os.environ["CPARAS_DATASET_ROOT"] = str(root) if root else ""
    for m in list(sys.modules):
        if m in ("core.database", "core.config") or m.startswith("etsi_corpus"):
            del sys.modules[m]
    db = importlib.import_module("core.database")
    pipeline = importlib.import_module("etsi_corpus.pipeline")
    local = importlib.import_module("etsi_corpus.dataset_local")
    lookup = importlib.import_module("etsi_corpus.gpp_lookup")
    return db, pipeline, local, lookup


def _stub_index(monkeypatch, pipeline, calls=None):
    async def fake_index(doc_id, text, metadata, clauses=None):
        if calls is not None:
            calls.append((doc_id, metadata, clauses, text))
        return f"etsi_{doc_id}", (len(clauses) if clauses else 1)

    async def fake_delete(doc_id):
        return None
    monkeypatch.setattr(pipeline, "index_document", fake_index)
    monkeypatch.setattr(pipeline, "delete_document_index", fake_delete)


def _all_docs(db):
    from sqlalchemy import select

    async def go():
        async with db.AsyncSessionLocal() as s:
            return (await s.execute(select(db.EtsiDocument))).scalars().all()
    return asyncio.run(go())


# ───────────────────────── shared contract module ─────────────────────────
def test_inspect_valid_dataset_with_spaces_in_path():
    gd = _gd()
    with tempfile.TemporaryDirectory() as t:
        root = _copy_fixture(Path(t))
        assert " " in str(root)
        info = gd.inspect_dataset(root, needs=("jsonl", "text", "reference", "corpus", "sidecar"))
        assert info["ok"], info["problems"]
        assert info["build"]["records_written"] == 9
        assert any(w["code"] == "unknown_versions" for w in info["warnings"])
        assert info["validation"]["ok"] is True


def test_missing_files_are_reported_not_silently_ignored():
    gd = _gd()
    with tempfile.TemporaryDirectory() as t:
        root = _copy_fixture(Path(t))
        (root / "etsi_dataset.jsonl").unlink()
        (root / "etsi_dataset.zip").unlink()
        info = gd.inspect_dataset(root, needs=("jsonl", "text"))
        codes = {p["code"] for p in info["problems"]}
        assert not info["ok"] and {"jsonl_missing", "text_missing"} <= codes


def test_resolve_root_handles_quotes_and_missing_folder(monkeypatch):
    gd = _gd()
    with tempfile.TemporaryDirectory() as t:
        d = Path(t) / "my data"
        d.mkdir()
        assert gd.resolve_root(f'"{d}"') == d
        with pytest.raises(gd.DatasetError) as ei:
            gd.resolve_root(str(d / "nope"))
        assert ei.value.code == "root_missing"
    monkeypatch.delenv("CPARAS_DATASET_ROOT", raising=False)
    assert gd.resolve_root(None) is None


def test_zip_integrity_and_zip_slip_rejected():
    gd = _gd()
    with tempfile.TemporaryDirectory() as t:
        bad = Path(t) / "bad.zip"
        bad.write_bytes(b"this is not a zip")
        with pytest.raises(gd.DatasetError) as e1:
            gd.check_zip(bad)
        assert e1.value.code == "zip_corrupt"
        evil = Path(t) / "evil.zip"
        with zipfile.ZipFile(evil, "w") as zf:
            zf.writestr("../../escape.txt", "x")
        with pytest.raises(gd.DatasetError) as e2:
            gd.check_zip(evil)
        assert e2.value.code == "zip_unsafe_path"
        ok = gd.check_zip(FIXTURE / "etsi_dataset.zip", crc=True)
        assert ok["has_jsonl"] and ok["text_files"] == 9


def test_jsonl_streaming_counts_malformed_lines():
    gd = _gd()
    with tempfile.TemporaryDirectory() as t:
        p = Path(t) / "x.jsonl"
        p.write_bytes(b'{"a":1}\n\nnot json\n[1,2]\n{"b":2}\n')
        st = gd.JsonlStats()
        recs = [r for _, r in gd.iter_jsonl(p, st)]
        assert recs == [{"a": 1}, {"b": 2}]
        assert (st.records, st.malformed, st.not_object, st.blank) == (2, 1, 1, 1)


def test_version_selection_never_drops_unknown_versions():
    gd = _gd()
    ids = list(gd.scan_identities(FIXTURE / "etsi_dataset.jsonl"))
    keep_all, _ = gd.select_document_ids(ids, mode="all")
    assert len(keep_all) == 9
    keep, c = gd.select_document_ids(ids, mode="latest")
    assert UNKNOWN_DOC in keep and c["unknown_version_kept"] == 1
    # 38.331 -> 17.4.0 only, 36.321 -> 15.8.0 only, 38.211/33.501/23.501 single
    assert c["selected"] == 6 and c["superseded_by_newer_version"] == 3
    keep_rel, c2 = gd.select_document_ids(ids, mode="latest-per-release")
    assert len(keep_rel) == 9                      # every record is the newest of its own release
    only38, c3 = gd.select_document_ids(ids, mode="all", series=["38"])
    assert c3["selected"] == 5 and c3["filtered_by_series"] == 4


def test_history_sidecar_verified_and_resolved():
    gd = _gd()
    refs = set()
    rec_hist = None
    for _, rec in gd.iter_jsonl(FIXTURE / "etsi_dataset.jsonl"):
        for r in gd.history_refs(rec.get("spec_history")):
            refs.add(r["sha256"])
            rec_hist = rec["spec_history"]
    assert len(refs) == 1
    v = gd.verify_sidecar(FIXTURE / "spec_history_blobs.jsonl.gz", refs)
    assert v["ok"] and v["blobs"] == 1
    full = gd.resolve_history(rec_hist, FIXTURE / "spec_history_blobs.jsonl.gz")
    assert len(full["versions"]) == 900 and full["small"] == {"a": 1}


def test_history_missing_and_tampered_blobs_are_errors_not_stand_ins():
    gd = _gd()
    with tempfile.TemporaryDirectory() as t:
        missing = gd.verify_sidecar(FIXTURE / "spec_history_blobs.jsonl.gz", {"f" * 64})
        assert not missing["ok"] and missing["missing"] == ["f" * 64]
        # tamper: change the value but keep the recorded sha256
        tam = Path(t) / "tampered.jsonl.gz"
        with gzip.open(FIXTURE / "spec_history_blobs.jsonl.gz", "rt", encoding="utf-8") as f:
            item = json.loads(f.readline())
        item["value"] = {"tampered": True}
        with gzip.open(tam, "wt", encoding="utf-8") as f:
            f.write(json.dumps(item) + "\n")
        res = gd.verify_sidecar(tam)
        assert res["bad"] and res["bad"][0]["problem"] == "sha256_mismatch"
        with pytest.raises(gd.HistoryBlobError):
            gd.resolve_history_blobs(tam, {item["sha256"]})
        with pytest.raises(gd.HistoryBlobError) as e:
            gd.resolve_history({"x": {"externalized": True, "sha256": "a" * 64}}, None)
        assert e.value.code == "sidecar_missing"
        # truncated gzip
        trunc = Path(t) / "trunc.jsonl.gz"
        trunc.write_bytes((FIXTURE / "spec_history_blobs.jsonl.gz").read_bytes()[:-20])
        with pytest.raises(gd.DatasetError):
            gd.verify_sidecar(trunc)


def test_corpus_and_reference_schema_checks():
    gd = _gd()
    with tempfile.TemporaryDirectory() as t:
        assert len(gd.load_corpus(FIXTURE / "standards_corpus_3gpp.json")["families"]) == 5
        assert "38.331" in gd.load_reference(FIXTURE / "spec_reference_3gpp.json")["specs"]
        wrong = Path(t) / "w.json"
        wrong.write_text(json.dumps({"schema": "something_else/9.0", "families": []}))
        with pytest.raises(gd.DatasetError) as e:
            gd.load_corpus(wrong)
        assert e.value.code == "corpus_schema"
        broken = Path(t) / "b.json"
        broken.write_text("{ nope")
        with pytest.raises(gd.DatasetError) as e2:
            gd.load_reference(broken)
        assert e2.value.code == "json_invalid"


def test_clause_normalisation_maps_text_variants_verbatim():
    gd = _gd()
    n = gd.normalise_clause({"number": "5.3.3", "title": "T", "excerpt": "Exact words ünï", "page": 4})
    assert n == {"clause_number": "5.3.3", "clause_title": "T", "summary": "Exact words ünï", "page": 4}
    assert gd.normalise_clause("junk") is None


# ───────────────────────── importer (streaming, zip text source) ─────────────────────────
def test_import_from_canonical_zip_streams_and_keeps_unknown_version(monkeypatch):
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        root = _copy_fixture(tmp)
        db, pipeline, local, lookup = _fresh(tmp / "t.db", str(tmp / "out"), str(tmp / "chroma"), root)
        asyncio.run(db.init_db())
        calls = []
        _stub_index(monkeypatch, pipeline, calls)

        jsonl, ts, info = local.prepare_import_inputs()
        assert ts.kind == "zip"                           # text read in place from etsi_dataset.zip
        job_id = asyncio.run(local.create_local_job(None))
        asyncio.run(local.run_local_import(job_id, limit=None, versions="all", series=[], specs=[], copy_text=False))

        st = asyncio.run(pipeline.job_status(job_id))
        assert st["status"] == "completed" and st["documents_indexed"] == 9 and st["documents_failed"] == 0
        rep = pipeline.read_import_report(job_id)
        c = rep["counts"]
        assert rep["state"] == "completed" and rep["text_source"] == "zip" and rep["dataset_fingerprint"]
        assert (c["records_seen"], c["selected"], c["new_documents"], c["completed"]) == (9, 9, 9, 9)
        assert c["unknown_version_records"] == 1 and c["history_pointers"] == 1 and c["malformed_lines"] == 0
        assert rep["duration_seconds"] >= 0

        docs = _all_docs(db)
        assert len(docs) == 9
        by_id = {d.structured_path: d for d in docs}
        unknown = [d for d in docs if d.version == "Unknown"]
        assert len(unknown) == 1 and unknown[0].specification_number == "38.331"          # kept, not invented
        assert all(d.raw_extracted_text_path.startswith("dataset-text:") for d in docs)      # no local text copy
        assert not (tmp / "out" / "text").exists() or not list((tmp / "out" / "text").glob("*.txt"))
        # clause-level data and source reference survive
        d331 = [d for d in docs if d.specification_number == "38.331" and d.version == "16.5.0"][0]
        cl = json.loads(d331.clauses_json)
        assert [c["clause_number"] for c in cl] == ["5.3.3", "5.3.5", "5.5.4"]
        assert d331.pdf_url.startswith("https://www.3gpp.org/ftp/Specs/archive/38_series/38.331/")
        # indexing received the clause list and the ZIP text (non-ASCII intact)
        texts = [c[3] for c in calls]
        assert any("Übergabe" in x and "切换" in x for x in texts)
        meta = [c[1] for c in calls if c[1]["specification_number"] == "38.331"][0]
        assert meta["release"] in ("15", "16", "17", "Unknown")
        # canonical files untouched (read-only guarantee)
        for f in FIXTURE.iterdir():
            assert (root / f.name).read_bytes() == f.read_bytes()


def test_reimport_is_idempotent_and_does_not_reindex(monkeypatch):
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        root = _copy_fixture(tmp)
        db, pipeline, local, lookup = _fresh(tmp / "t.db", str(tmp / "out"), str(tmp / "chroma"), root)
        asyncio.run(db.init_db())
        calls = []
        _stub_index(monkeypatch, pipeline, calls)
        for _ in range(2):
            jid = asyncio.run(local.create_local_job(None))
            asyncio.run(local.run_local_import(jid, limit=None, versions="all", series=[], specs=[], copy_text=False))
        assert len(calls) == 9, "second import must not re-index completed documents"
        assert len(_all_docs(db)) == 9, "no duplicate rows"
        rep = pipeline.read_import_report(jid)
        assert rep["counts"]["already_present"] == 9 and rep["counts"]["already_completed"] == 9 and rep["counts"]["new_documents"] == 0
        assert asyncio.run(pipeline.job_status(jid))["documents_indexed"] == 9


def test_versions_filter_and_limit_are_reported_not_silent(monkeypatch):
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        root = _copy_fixture(tmp)
        db, pipeline, local, lookup = _fresh(tmp / "t.db", str(tmp / "out"), str(tmp / "chroma"), root)
        asyncio.run(db.init_db())
        _stub_index(monkeypatch, pipeline)
        jid = asyncio.run(local.create_local_job(None))
        asyncio.run(local.run_local_import(jid, limit=None, versions="latest", series=[], specs=[], copy_text=False))
        rep = pipeline.read_import_report(jid)
        assert rep["counts"]["selected"] == 6 and rep["counts"]["filtered_out"] == 3
        assert rep["selection"]["superseded_by_newer_version"] == 3 and rep["selection"]["unknown_version_kept"] == 1
        jid2 = asyncio.run(local.create_local_job(2))
        asyncio.run(local.run_local_import(jid2, limit=2, versions="all", series=[], specs=[], copy_text=False))
        assert pipeline.read_import_report(jid2)["counts"]["selected"] == 2


def test_malformed_lines_duplicates_and_missing_text_are_counted(monkeypatch):
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        root = _copy_fixture(tmp)
        lines = (root / "etsi_dataset.jsonl").read_bytes().splitlines()
        # damage: a garbage line, a duplicate record, and a record with no matching text
        orphan = json.loads(lines[0])
        orphan["identity"]["document_id"] = "orphan_doc"
        orphan["identity"]["pdf_url"] = "https://example.org/orphan"
        orphan["provenance"]["raw_text_path"] = "does_not_exist.txt"
        (root / "etsi_dataset.jsonl").write_bytes(b"\n".join([b"{ broken", lines[0]] + lines + [json.dumps(orphan).encode()]) + b"\n")
        db, pipeline, local, lookup = _fresh(tmp / "t.db", str(tmp / "out"), str(tmp / "chroma"), root)
        asyncio.run(db.init_db())
        _stub_index(monkeypatch, pipeline)
        jid = asyncio.run(local.create_local_job(None))
        asyncio.run(local.run_local_import(jid, limit=None, versions="all", series=[], specs=[], copy_text=False))
        rep = pipeline.read_import_report(jid)
        c = rep["counts"]
        assert c["malformed_lines"] == 1 and c["duplicates_in_file"] == 1
        assert c["import_failed"] == 1 and c["completed"] == 9
        assert rep["state"] == "completed_with_errors" and rep["errors_sample"][0]["document_id"] == "orphan_doc"
        failed = asyncio.run(pipeline.failed_documents(jid))
        assert len(failed) == 1 and "text" in failed[0]["error_message"].lower()


def test_interrupted_import_recovers_and_resumes(monkeypatch):
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        root = _copy_fixture(tmp)
        db, pipeline, local, lookup = _fresh(tmp / "t.db", str(tmp / "out"), str(tmp / "chroma"), root)
        asyncio.run(db.init_db())
        calls = []
        _stub_index(monkeypatch, pipeline, calls)
        jid = asyncio.run(local.create_local_job(3))
        asyncio.run(local.run_local_import(jid, limit=3, versions="all", series=[], specs=[], copy_text=False))
        assert len(calls) == 3
        # simulate a crash that left two rows half-processed and never finished
        from sqlalchemy import update

        async def corrupt():
            async with db.AsyncSessionLocal() as s:
                ids = [d.id for d in (await s.execute(__import__("sqlalchemy").select(db.EtsiDocument))).scalars().all()][:2]
                await s.execute(update(db.EtsiDocument).where(db.EtsiDocument.id == ids[0]).values(status="indexing"))
                await s.execute(update(db.EtsiDocument).where(db.EtsiDocument.id == ids[1]).values(status="importing"))
                await s.commit()
        asyncio.run(corrupt())
        jid2 = asyncio.run(local.create_local_job(None))
        asyncio.run(local.run_local_import(jid2, limit=None, versions="all", series=[], specs=[], copy_text=False))
        docs = _all_docs(db)
        assert len(docs) == 9 and all(d.status == "completed" for d in docs)
        assert len(calls) == 3 + 2 + 6, "the 2 interrupted docs and the 6 new ones were processed, the 1 healthy one was not repeated"


def test_stop_request_pauses_job_and_resume_finishes(monkeypatch):
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        root = _copy_fixture(tmp)
        db, pipeline, local, lookup = _fresh(tmp / "t.db", str(tmp / "out"), str(tmp / "chroma"), root, batch=1)
        asyncio.run(db.init_db())
        calls = []
        _stub_index(monkeypatch, pipeline, calls)
        jid = asyncio.run(local.create_local_job(None))
        real = pipeline.index_document

        async def stopping(doc_id, text, metadata, clauses=None):
            r = await real(doc_id, text, metadata, clauses)
            if len(calls) == 5:
                pipeline.request_stop(jid)
            return r
        monkeypatch.setattr(pipeline, "index_document", stopping)
        asyncio.run(local.run_local_import(jid, limit=None, versions="all", series=[], specs=[], copy_text=False))
        assert asyncio.run(pipeline.job_status(jid))["status"] == "paused"
        assert 5 <= len(_all_docs(db)) < 9 or any(d.status != "completed" for d in _all_docs(db))
        monkeypatch.setattr(pipeline, "index_document", real)
        asyncio.run(local.run_local_import(jid, limit=None, versions="all", series=[], specs=[], copy_text=False))
        assert all(d.status == "completed" for d in _all_docs(db)) and len(_all_docs(db)) == 9


# ───────────────────────── validate / status / diagnostics ─────────────────────────
def test_validate_dataset_ok_and_detects_damage():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        root = _copy_fixture(tmp)
        db, pipeline, local, lookup = _fresh(tmp / "t.db", str(tmp / "out"), str(tmp / "chroma"), root)
        res = local.validate_dataset_sync(crc=True)
        assert res["ok"], res["problems"]
        assert res["counts"]["records"] == 9 and res["counts"]["unknown_version"] == 1 and res["counts"]["missing_text"] == 0
        assert res["history"]["ok"] and res["zip"]["crc_checked"]
        # damage: drop the sidecar -> history pointer cannot resolve
        (root / "spec_history_blobs.jsonl.gz").unlink()
        bad = local.validate_dataset_sync()
        assert not bad["ok"] and any(p["code"] == "sidecar_missing" for p in bad["problems"])


def test_status_not_configured_and_partial_index_diagnosis(monkeypatch):
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        db, pipeline, local, lookup = _fresh(tmp / "t.db", str(tmp / "out"), str(tmp / "chroma"), None)
        asyncio.run(db.init_db())
        st = asyncio.run(local.dataset_status())
        assert st["configured"] is False and st["problems"][0]["code"] == "not_configured"
        root = _copy_fixture(tmp)
        db, pipeline, local, lookup = _fresh(tmp / "t2.db", str(tmp / "out2"), str(tmp / "chroma2"), root)
        asyncio.run(db.init_db())
        _stub_index(monkeypatch, pipeline)
        st = asyncio.run(local.dataset_status())
        assert st["ok"] and any(d["code"] == "not_imported" for d in st["diagnostics"])
        jid = asyncio.run(local.create_local_job(3))
        asyncio.run(local.run_local_import(jid, limit=3, versions="all", series=[], specs=[], copy_text=False))
        st = asyncio.run(local.dataset_status())
        assert any(d["code"] == "partially_indexed" for d in st["diagnostics"]) and st["imported"]["indexed"] == 3


# ───────────────────────── spec / version / clause lookup ─────────────────────────
def test_spec_version_and_clause_lookup(monkeypatch):
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        root = _copy_fixture(tmp)
        db, pipeline, local, lookup = _fresh(tmp / "t.db", str(tmp / "out"), str(tmp / "chroma"), root)
        asyncio.run(db.init_db())
        _stub_index(monkeypatch, pipeline)
        jid = asyncio.run(local.create_local_job(None))
        asyncio.run(local.run_local_import(jid, limit=None, versions="all", series=[], specs=[], copy_text=False))

        sv = asyncio.run(lookup.spec_versions("TS 38.331"))
        assert [v["version"] for v in sv["versions"]] == ["Unknown", "15.3.0", "16.5.0", "17.4.0"]
        assert sv["latest_known"]["version"] == "17.4.0" and sv["unknown_version_documents"] == 1
        exact = asyncio.run(lookup.get_spec("38.331", version="v16.5.0"))
        assert exact["found"] and exact["document"]["release"] == "16"
        rel = asyncio.run(lookup.get_spec("38.331", release="Rel-15"))
        assert rel["document"]["version"] == "15.3.0"
        none = asyncio.run(lookup.get_spec("38.331", version="99.0.0"))
        assert none["found"] is False                                # no nearest-match guessing
        latest = asyncio.run(lookup.get_spec("38.331"))
        assert latest["selection"] == "latest_known_version" and latest["document"]["version"] == "17.4.0"

        cl = asyncio.run(lookup.get_clauses("38.331", version="16.5.0", clause="5.3"))
        nums = [c["clause_number"] for c in cl["clauses"]]
        assert nums == ["5.3.3", "5.3.5"]                            # '5.3' prefix on a dotted boundary
        src = cl["clauses"][0]["source"]
        assert src["version"] == "16.5.0" and src["specification_number"] == "38.331" and src["pdf_url"].startswith("https://")
        assert "essentiality" in cl["note"].lower()
        assert asyncio.run(lookup.get_clauses("38.331", clause="5.30"))["clauses"] == []
        listing = asyncio.run(lookup.list_specs(series="38"))
        assert {s["specification_number"] for s in listing["specs"]} == {"38.331", "38.211"}


# ───────────────────────── HTTP API ─────────────────────────
def test_api_local_import_flow(monkeypatch):
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        root = _copy_fixture(tmp)
        db, pipeline, local, lookup = _fresh(tmp / "t.db", str(tmp / "out"), str(tmp / "chroma"), root)
        for m in list(sys.modules):
            if m.startswith("api.routes") and m.endswith("etsi_corpus"):
                del sys.modules[m]
        try:
            route_mod = importlib.import_module("api.routes.gitas.etsi_corpus")
        except ModuleNotFoundError:
            route_mod = importlib.import_module("api.routes.corpus.etsi_corpus")
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        app = FastAPI()
        app.include_router(route_mod.router, prefix="/api/etsi-corpus")
        _stub_index(monkeypatch, sys.modules["etsi_corpus.pipeline"])
        asyncio.run(db.init_db())
        with TestClient(app) as c:
            st = c.get("/api/etsi-corpus/dataset/local/status").json()
            assert st["configured"] and st["ok"] and st["build"]["records_written"] == 9
            v = c.post("/api/etsi-corpus/dataset/local/validate").json()
            assert v["ok"] and v["counts"]["records"] == 9
            assert c.post("/api/etsi-corpus/dataset/local/import?versions=bogus").status_code == 400
            r = c.post("/api/etsi-corpus/dataset/local/import?all_rows=true&versions=all")
            assert r.status_code == 200
            jid = r.json()["job_id"]
            job = c.get(f"/api/etsi-corpus/jobs/{jid}").json()          # TestClient runs background tasks before returning
            assert job["status"] == "completed" and job["documents_indexed"] == 9
            rep = c.get(f"/api/etsi-corpus/dataset/jobs/{jid}/report").json()
            assert rep["counts"]["completed"] == 9
            assert c.get("/api/etsi-corpus/dataset/jobs/9999/report").status_code == 404
            vs = c.get("/api/etsi-corpus/standards/38.331/versions").json()
            assert vs["count"] == 4 and vs["latest_known"]["version"] == "17.4.0"
            cl = c.get("/api/etsi-corpus/standards/38.331/clauses?version=15.3.0&clause=5.5.4").json()
            assert len(cl["clauses"]) == 1 and cl["clauses"][0]["source"]["version"] == "15.3.0"
            assert c.get("/api/etsi-corpus/standards/38.331?version=1.2.3").json()["found"] is False
            assert c.post("/api/etsi-corpus/dataset/recover").json() == {"recovered": 0}


def test_api_import_refused_when_not_configured():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        db, pipeline, local, lookup = _fresh(tmp / "t.db", str(tmp / "out"), str(tmp / "chroma"), None)
        for m in list(sys.modules):
            if m.startswith("api.routes") and m.endswith("etsi_corpus"):
                del sys.modules[m]
        try:
            route_mod = importlib.import_module("api.routes.gitas.etsi_corpus")
        except ModuleNotFoundError:
            route_mod = importlib.import_module("api.routes.corpus.etsi_corpus")
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        app = FastAPI()
        app.include_router(route_mod.router, prefix="/api/etsi-corpus")
        asyncio.run(db.init_db())
        with TestClient(app) as c:
            r = c.post("/api/etsi-corpus/dataset/local/import")
            assert r.status_code == 400 and "CPARAS_DATASET_ROOT" in r.json()["detail"]


def test_vendored_contract_module_is_identical_across_projects():
    """The three applications vendor one file; this guards against silent drift between copies."""
    here = Path(__file__).resolve().parents[1] / "core" / "gpp_dataset.py"
    assert 'CONTRACT_VERSION = "1.0"' in here.read_text(encoding="utf-8")
