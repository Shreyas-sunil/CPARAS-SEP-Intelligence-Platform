"""Regression tests for embed_cparas.py (unittest + numpy only; no model or GPU needed).

    cd <folder containing embed_cparas.py>
    python -m unittest discover -s tests -v
"""
import hashlib
import importlib.util
import io
import json
import shutil
import sys
import tempfile
import threading
import unittest
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("ec", HERE.parent / "embed_cparas.py")
ec = importlib.util.module_from_spec(spec)
sys.modules["ec"] = ec
spec.loader.exec_module(ec)

PARA = ("The UE shall initiate the RRC connection re-establishment procedure when radio link failure is detected "
        "and the security has been activated. The network responds with the re-establishment message. ")
HANDOVER = ("Upon receiving the handover command the UE shall synchronise to the target cell, apply the new "
            "configuration and send the reconfiguration complete message to the target gNB. ")
PAGING = "The paging occasion is computed from the UE identity and the DRX cycle configured by upper layers. "


def tree_hash(root):
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


def make_record(did, spec_, version, release="15", title="Radio Resource Control (RRC); Protocol specification",
                clauses=None, summary=True):
    rec = {"identity": {"document_id": did, "pdf_url": f"https://www.3gpp.org/x/{did}.zip", "document_number": spec_,
                        "version": version, "release": release, "document_type": "TS", "etsi_series": spec_[:2],
                        "title": title},
           "classification": {"technology_domain": "PHY / MAC / Radio", "topics": ["RRC", "mobility"],
                              "protocols": ["RRC"]} if summary else {},
           "technical_summary": {"scope": "This specification defines the RRC protocol for the radio interface "
                                          "between the UE and the network, including connection control."} if summary else {},
           "patent_relevance": {}, "extraction_provenance": {},
           "provenance": {"raw_text_path": did + ".txt"},
           "clauses": clauses if clauses is not None else [
               {"clause_number": "1", "clause_title": "Scope"},
               {"clause_number": "5", "clause_title": "Procedures"},
               {"clause_number": "5.3.7", "clause_title": "RRC connection re-establishment"},
               {"clause_number": "5.3.8", "clause_title": "Handover"},
               {"clause_number": "5.5", "clause_title": "Paging"}]}
    return rec


def spec_text(did, body_paging=PAGING):
    return (f"Foreword\nThis Technical Specification has been produced by 3GPP. {did}\n\n"
            "1\tScope\nThe present document specifies the Radio Resource Control protocol. " + "Scope detail. " * 20 + "\n\n"
            "5\tProcedures\n\n"
            "5.3.7\tRRC connection re-establishment\n" + PARA * 12 + "\n\n"
            "5.3.8\tHandover\n" + HANDOVER * 6 + "\n\n"
            "Table 5.5-1: timer values\n"
            "5.5\t128 ms\tdefault value (a table row, not a heading)\n"
            "5.5\tPaging\n" + body_paging * 8 + "\n")


class Fx:
    def __init__(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ec_"))
        self.ds = self.tmp / "build_v2"
        self.ds.mkdir()
        self.out = self.tmp / "vectors"
        self.recs = []

    def close(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def add(self, did, spec_="38.331", version="15.3.0", text="__default__", **kw):
        rec = make_record(did, spec_, version, **kw)
        self.recs.append((rec, spec_text(did) if text == "__default__" else text))
        return rec

    def write_zip(self, name="etsi_dataset.zip", recs=None):
        recs = recs if recs is not None else self.recs
        with zipfile.ZipFile(self.ds / name, "w") as zf:
            zf.writestr("etsi_dataset.jsonl", "".join(json.dumps(r) + "\n" for r, _ in recs))
            for r, t in recs:
                if t is not None:
                    zf.writestr("text/" + r["provenance"]["raw_text_path"], t)

    def write_jsonl(self):
        (self.ds / "text").mkdir(exist_ok=True)
        (self.ds / "etsi_dataset.jsonl").write_text("".join(json.dumps(r) + "\n" for r, _ in self.recs), encoding="utf-8")
        for r, t in self.recs:
            if t is not None:
                (self.ds / "text" / r["provenance"]["raw_text_path"]).write_text(t, encoding="utf-8")

    def run(self, *args, embedder=None, out=None, dataset=None):
        a = ec.make_parser().parse_args(["embed", "-d", str(dataset or self.ds), "-o", str(out or self.out),
                                         "--backend", "hash", "--quiet", *args])
        buf = io.StringIO()
        with redirect_stdout(buf), redirect_stderr(buf):
            try:
                rc = ec.cmd_embed(a, embedder=embedder)
            except SystemExit as e:
                return 1, str(e.code) + buf.getvalue()
        return rc, buf.getvalue()

    def chunks(self, out=None):
        return list(ec._iter_chunks(Path(out or self.out)))

    def report(self, out=None):
        return json.loads((Path(out or self.out) / "embed_report.json").read_text(encoding="utf-8"))

    def validate(self, out=None, **kw):
        return ec.validate_store(Path(out or self.out), **kw)


class CountingEmbedder(ec.HashEmbedder):
    def __init__(self, poison=None):
        super().__init__(64)
        self.inputs, self.poison = [], poison

    def embed(self, texts):
        if self.poison and any(self.poison in t for t in texts):
            raise ec.ItemError("rejected input")
        self.inputs += texts
        return super().embed(texts)


class TestChunking(unittest.TestCase):
    CFG = {"max_chars": 600, "overlap": 80, "min_chars": 150, "summary": True}

    def test_sections_found_and_offsets_exact(self):
        rec, text = make_record("d", "38.331", "15.3.0"), spec_text("d")
        chs = ec.make_chunks(rec, text, self.CFG)
        body = [c for c in chs if c["kind"] == "text"]
        self.assertEqual({c["clause"] for c in body}, {"1", "5.3.7", "5.3.8", "5.5"})   # short foreword glued to clause 1
        self.assertTrue(body[0]["text"].startswith("Foreword"))
        for c in body:
            self.assertEqual(text[c["char_start"]:c["char_end"]], c["text"])
            self.assertLessEqual(len(c["text"]), 600 * 1.25)
        reest = [c for c in body if c["clause"] == "5.3.7"]
        self.assertGreater(len(reest), 1)
        self.assertTrue(all("re-establishment" in c["text"] or "RRC" in c["text"] for c in reest))

    def test_table_line_with_clause_number_is_not_a_heading(self):
        rec, text = make_record("d", "38.331", "15.3.0"), spec_text("d")
        secs, n_known = ec.find_sections(text, rec["clauses"])
        self.assertEqual([s[2] for s in secs], ["1", "5", "5.3.7", "5.3.8", "5.5"])
        self.assertEqual(n_known, 5)
        self.assertEqual(text[secs[-1][0]:secs[-1][1]], "5.5\tPaging")        # not the '5.5  128 ms' table row

    def test_heading_only_section_merged_into_next(self):
        rec, text = make_record("d", "38.331", "15.3.0"), spec_text("d")
        chs = ec.make_chunks(rec, text, self.CFG)
        self.assertNotIn("5", {c["clause"] for c in chs})              # "5 Procedures" has no body
        first_reest = next(c for c in chs if c["clause"] == "5.3.7")
        self.assertEqual(first_reest["clauses_covered"], ["5", "5.3.7"])
        self.assertTrue(first_reest["text"].startswith("5\tProcedures"))

    def test_overlap_between_consecutive_chunks(self):
        s = " ".join(f"word{i}" for i in range(2000))
        spans = ec.split_text(s, 500, 100, 100)
        for (a1, b1), (a2, b2) in zip(spans, spans[1:]):
            self.assertLess(a2, b1)                                       # overlapping
            self.assertGreater(a2, a1)                                    # progressing
        self.assertEqual(spans[-1][1], len(s))

    def test_split_terminates_on_pathological_text(self):
        for s in ("x" * 10_000, "\n" * 5000, "a " * 3000, "", "   ", "é" * 777):
            spans = ec.split_text(s, 300, 50, 60)
            for a, b in spans:
                self.assertLessEqual(b - a, 300 * 1.25)
            if s.strip():
                self.assertEqual(spans[-1][1], len(s.rstrip()))

    def test_no_clause_tree_plain_windows(self):
        rec = make_record("d", "38.331", "15.3.0", clauses=[])
        chs = ec.make_chunks(rec, spec_text("d"), self.CFG)
        self.assertTrue(all(c["clause"] == "" for c in chs if c["kind"] == "text"))

    def test_summary_chunk_flagged_llm(self):
        chs = ec.make_chunks(make_record("d", "38.331", "15.3.0"), None, self.CFG)
        self.assertEqual([c["kind"] for c in chs], ["summary"])
        self.assertTrue(chs[0]["llm_generated"])
        self.assertEqual(ec.make_chunks(make_record("d", "38.331", "15.3.0", summary=False), None, self.CFG), [])

    def test_crlf_text(self):
        rec, text = make_record("d", "38.331", "15.3.0"), spec_text("d").replace("\n", "\r\n")
        body = [c for c in ec.make_chunks(rec, text, self.CFG) if c["kind"] == "text"]
        self.assertIn("5.3.7", {c["clause"] for c in body})

    def test_header_excludes_version(self):
        dm = ec.doc_meta(make_record("d", "38.331", "15.3.0"))
        h = ec.context_header(dm, {"kind": "text", "clause": "5.3.7", "clause_title": "RRC connection re-establishment"})
        self.assertIn("38.331", h)
        self.assertIn("Clause 5.3.7", h)
        self.assertNotIn("15.3.0", h)

    def test_prefixes(self):
        self.assertEqual(ec.auto_prefixes("nomic-embed-text"), ("search_document: ", "search_query: "))
        self.assertEqual(ec.auto_prefixes("intfloat/multilingual-e5-large"), ("passage: ", "query: "))
        self.assertEqual(ec.auto_prefixes("bge-m3"), ("", ""))
        self.assertTrue(ec.auto_prefixes("BAAI/bge-small-en-v1.5")[1].startswith("Represent"))
        self.assertEqual(ec.auto_prefixes("gte5-something"), ("", ""))


class TestEmbed(unittest.TestCase):
    def setUp(self):
        self.fx = Fx()
        self.addCleanup(self.fx.close)

    def _three(self):
        self.fx.add("38_series_38.331_38331-f30.zip_a.docx", version="15.3.0")
        self.fx.add("38_series_38.331_38331-g00.zip_a.docx", version="16.0.0", release="16")
        self.fx.add("36_series_36.331_36331-f00.zip_a.docx", spec_="36.331", version="15.0.0",
                    text=spec_text("lte").replace("RRC connection re-establishment", "RRC connection re-establishment"))

    def test_end_to_end_zip(self):
        self._three()
        self.fx.write_zip()
        rc, log = self.fx.run()
        self.assertEqual(rc, 0, log)
        X = np.load(self.fx.out / "embeddings.npy", mmap_mode="r")
        chunks = self.fx.chunks()
        self.assertEqual(X.shape[0], len(chunks))
        self.assertEqual(X.shape[1], 256)
        np.testing.assert_allclose(np.linalg.norm(np.asarray(X), axis=1), 1.0, rtol=1e-5)
        self.assertEqual([c["row"] for c in chunks], list(range(len(chunks))))
        self.assertEqual(len({c["chunk_id"] for c in chunks}), len(chunks))
        c = next(c for c in chunks if c["clause"] == "5.3.7")
        self.assertEqual((c["spec"], c["release"], c["series"], c["document_type"]), ("38.331", "15", "38", "TS"))
        self.assertTrue(self.fx.validate(dataset=str(self.fx.ds))["ok"])
        self.assertEqual(self.fx.report()["docs_embedded"], 3)

    def test_jsonl_plus_text_and_extractor_layout(self):
        self._three()
        self.fx.write_jsonl()
        rc, log = self.fx.run()
        self.assertEqual(rc, 0, log)
        self.assertEqual(self.fx.report()["docs_embedded"], 3)
        ext = self.fx.tmp / "3gpp_out"
        (ext / "cparas_dataset").mkdir(parents=True)
        shutil.copy(self.fx.ds / "etsi_dataset.jsonl", ext / "cparas_dataset")
        shutil.copytree(self.fx.ds / "text", ext / "cparas_dataset" / "text")
        rc, log = self.fx.run(dataset=ext, out=self.fx.tmp / "v2")
        self.assertEqual(rc, 0, log)
        self.assertEqual(len(self.fx.chunks(self.fx.tmp / "v2")), len(self.fx.chunks()))

    def test_split_zip_parts(self):
        self._three()
        self.fx.write_zip("etsi_dataset_part01.zip", self.fx.recs[:2])
        self.fx.write_zip("etsi_dataset_part02.zip", self.fx.recs[2:])
        rc, log = self.fx.run()
        self.assertEqual(rc, 0, log)
        self.assertEqual(self.fx.report()["docs_embedded"], 3)

    def test_resume_skips_done_and_appends_new(self):
        self._three()
        self.fx.write_zip()
        self.assertEqual(self.fx.run()[0], 0)
        rows = len(self.fx.chunks())
        emb = CountingEmbedder()
        self.assertEqual(self.fx.run(embedder=emb)[0], 1 if False else 0)
        self.assertEqual(emb.inputs, [])
        self.assertEqual(len(self.fx.chunks()), rows)
        self.assertEqual(self.fx.report()["already_embedded"], 3)
        self.fx.add("38_series_38.331_38331-h00.zip_a.docx", version="17.0.0", release="17",
                    text=spec_text("x", body_paging="Completely new paging text for release seventeen. "))
        self.fx.write_zip()
        self.assertEqual(self.fx.run()[0], 0)
        self.assertEqual(self.fx.report()["docs_embedded"], 1)
        self.assertGreater(len(self.fx.chunks()), rows)
        self.assertTrue(self.fx.validate()["ok"])

    def test_identical_clause_text_embedded_once(self):
        a = self.fx.add("38_series_38.331_38331-f30.zip_a.docx", version="15.3.0")
        self.fx.recs[0] = (a, spec_text("same"))
        b = self.fx.add("38_series_38.331_38331-g00.zip_a.docx", version="16.0.0", release="16")
        self.fx.recs[1] = (b, spec_text("same"))
        self.fx.write_zip()
        emb = CountingEmbedder()
        self.assertEqual(self.fx.run(embedder=emb)[0], 0)
        chunks = self.fx.chunks()
        n1 = sum(1 for c in chunks if c["version"] == "15.3.0")
        self.assertEqual(len(emb.inputs), n1)                              # second version cost nothing
        X = np.load(self.fx.out / "embeddings.npy")
        for c1, c2 in zip([c for c in chunks if c["version"] == "15.3.0"], [c for c in chunks if c["version"] == "16.0.0"]):
            np.testing.assert_array_equal(X[c1["row"]], X[c2["row"]])
        # small batches: vectors resolved but not yet committed must be reused too
        for bs in ("2", "3", "4", "5", "7"):
            emb_small = CountingEmbedder()
            self.assertEqual(self.fx.run("--batch-size", bs, embedder=emb_small, out=self.fx.tmp / f"small{bs}")[0], 0)
            self.assertEqual(len(emb_small.inputs), n1, f"batch size {bs}")
        # and across runs: a new version with the same text reuses vectors from disk
        c = self.fx.add("38_series_38.331_38331-h00.zip_a.docx", version="17.0.0", release="17")
        self.fx.recs[2] = (c, spec_text("same"))
        self.fx.write_zip()
        emb2 = CountingEmbedder()
        self.assertEqual(self.fx.run(embedder=emb2)[0], 0)
        self.assertEqual(emb2.inputs, [])
        self.assertTrue(self.fx.validate()["ok"])

    def test_bad_input_fails_only_its_document(self):
        self._three()
        poisoned = spec_text("bad").replace("Handover\n", "Handover\nPOISON ")
        self.fx.recs[1] = (self.fx.recs[1][0], poisoned)
        self.fx.write_zip()
        rc, log = self.fx.run(embedder=CountingEmbedder(poison="POISON"))
        self.assertEqual(rc, 4, log)
        rep = self.fx.report()
        self.assertEqual((rep["docs_embedded"], rep["docs_failed"]), (2, 1))
        failed = [json.loads(l) for l in (self.fx.out / "failed.jsonl").read_text().splitlines()]
        self.assertEqual([f["version"] for f in failed], ["16.0.0"])
        self.assertTrue(self.fx.validate()["ok"])
        rc, log = self.fx.run(embedder=CountingEmbedder())                # retried next run
        self.assertEqual(rc, 0, log)
        self.assertEqual(self.fx.report()["docs_embedded"], 1)
        self.assertTrue(self.fx.validate(dataset=str(self.fx.ds))["ok"])

    def test_backend_rejecting_everything_stops_the_run(self):
        for i in range(12):
            self.fx.add(f"d{i}", text=spec_text(f"d{i}") + f"unique {i}")
        self.fx.write_zip()
        rc, log = self.fx.run("--batch-size", "1", embedder=CountingEmbedder(poison=" "))
        self.assertEqual(rc, 3, log)
        self.assertIn("rejected 25 inputs in a row", log)
        self.assertEqual(self.fx.report()["status"], "stopped")
        self.assertTrue(self.fx.validate()["ok"])

    def test_validate_catches_misaligned_rows(self):
        self._three()
        self.fx.write_zip()
        self.assertEqual(self.fx.run()[0], 0)
        p = self.fx.out / "chunks.jsonl"
        p.write_bytes(b"".join(p.read_bytes().splitlines(keepends=True)[:-1]))
        self.assertEqual({e["code"] for e in self.fx.validate()["errors"]}, {"row_mismatch"})

    def test_crash_tail_is_repaired(self):
        self._three()
        self.fx.write_zip()
        self.assertEqual(self.fx.run()[0], 0)
        rows = len(self.fx.chunks())
        with open(self.fx.out / "embeddings.npy", "ab") as f:
            f.write(b"\x00" * 256 * 4 * 3)                                # vectors of an uncommitted doc
        with open(self.fx.out / "chunks.jsonl", "ab") as f:
            f.write(b'{"chunk_id": "half"}\n{"chunk_id": "hal')
        with open(self.fx.out / "docs.jsonl", "ab") as f:
            f.write(b'{"doc_id": "x", "fingerp')
        self.assertFalse(self.fx.validate()["ok"])
        self.fx.add("38_series_38.331_38331-h00.zip_a.docx", version="17.0.0", release="17")
        self.fx.write_zip()
        rc, log = self.fx.run()
        self.assertEqual(rc, 0, log)
        self.assertTrue(self.fx.validate()["ok"], self.fx.validate())
        self.assertGreater(len(self.fx.chunks()), rows)

    def test_settings_mismatch_refused(self):
        self._three()
        self.fx.write_zip()
        self.assertEqual(self.fx.run()[0], 0)
        rc, log = self.fx.run("--max-chars", "800", "--overlap", "100")
        self.assertNotEqual(rc, 0)
        self.assertIn("different settings", log)
        rc, log = self.fx.run("--max-chars", "800", "--overlap", "100", "--overwrite")
        self.assertEqual(rc, 0, log)
        self.assertTrue(self.fx.validate()["ok"])

    def test_dataset_never_modified_and_out_inside_refused(self):
        self._three()
        self.fx.write_zip()
        before = tree_hash(self.fx.ds)
        self.assertEqual(self.fx.run()[0], 0)
        self.assertEqual(self.fx.run("--dry-run", out=self.fx.tmp / "dry")[0], 0)
        self.assertEqual(tree_hash(self.fx.ds), before)
        rc, log = self.fx.run(out=self.fx.ds / "vectors")
        self.assertNotEqual(rc, 0)
        self.assertFalse((self.fx.ds / "vectors").exists())
        rc, log = self.fx.run(out=self.fx.tmp)
        self.assertNotEqual(rc, 0)

    def test_dry_run(self):
        self._three()
        self.fx.write_zip()
        rc, log = self.fx.run("--dry-run")
        self.assertEqual(rc, 0, log)
        rep = json.loads((self.fx.out / "dryrun_report.json").read_text())
        self.assertEqual(rep["docs"], 3)
        self.assertGreater(rep["chunks"], 3)
        self.assertFalse((self.fx.out / "embeddings.npy").exists())

    def test_filters_and_limit(self):
        self._three()
        self.fx.write_zip()
        self.assertEqual(self.fx.run("--series", "36")[0], 0)
        self.assertEqual({c["spec"] for c in self.fx.chunks()}, {"36.331"})
        self.assertEqual(self.fx.run("--limit", "1", out=self.fx.tmp / "l")[0], 0)
        self.assertEqual(self.fx.report(self.fx.tmp / "l")["docs_embedded"], 1)
        self.assertEqual(self.fx.run("--specs", "38.331", out=self.fx.tmp / "s")[0], 0)
        self.assertEqual({c["spec"] for c in self.fx.chunks(self.fx.tmp / "s")}, {"38.331"})

    def test_missing_text_keeps_summary_only(self):
        self.fx.add("x_doc", text=None)
        self.fx.write_zip()
        self.assertEqual(self.fx.run()[0], 0)
        self.assertEqual({c["kind"] for c in self.fx.chunks()}, {"summary"})
        self.assertEqual(self.fx.report()["docs_without_text"], 1)

    def test_changed_document_reported_not_mixed(self):
        self._three()
        self.fx.write_zip()
        self.assertEqual(self.fx.run()[0], 0)
        self.fx.recs[0] = (self.fx.recs[0][0], spec_text("edited"))
        self.fx.write_zip()
        self.assertEqual(self.fx.run()[0], 0)
        rep = self.fx.report()
        self.assertEqual(rep["changed_since_embedded"], 1)
        self.assertEqual(rep["docs_embedded"], 0)

    def test_surrogates_and_odd_records(self):
        self.fx.add("sur", text=spec_text("s") + "bad \ud800 char")
        r = self.fx.add("noident", text="plain text " * 50)
        r["identity"] = {"document_id": "noident"}
        self.fx.recs.append(({"identity": None}, "x"))
        with zipfile.ZipFile(self.fx.ds / "etsi_dataset.zip", "w") as zf:
            zf.writestr("etsi_dataset.jsonl", "".join(json.dumps(r) + "\n" for r, _ in self.fx.recs) + "{broken\n")
            zf.writestr("text/sur.txt", self.fx.recs[0][1].encode("utf-8", "surrogatepass"))
            zf.writestr("text/noident.txt", "plain text " * 50)
        rc, log = self.fx.run()
        self.assertEqual(rc, 0, log)
        rep = self.fx.report()
        self.assertEqual(rep["unreadable_lines"], 1)
        self.assertEqual(rep["docs_without_id"], 1)
        self.assertTrue(self.fx.validate()["ok"])


class TestQuery(unittest.TestCase):
    def test_query_finds_the_right_clause_and_filters(self):
        fx = Fx()
        self.addCleanup(fx.close)
        fx.add("38_series_38.331_38331-f30.zip_a.docx", version="15.3.0")
        fx.add("36_series_36.331_36331-f00.zip_a.docx", spec_="36.331", version="15.0.0")
        fx.write_zip()
        self.assertEqual(fx.run()[0], 0)
        hits = ec.search(fx.out, "handover command synchronise to the target cell", k=3)
        self.assertEqual(hits[0]["clause"], "5.3.8")
        hits = ec.search(fx.out, "handover command synchronise to the target cell", k=3, filters={"spec": ["36.331"]})
        self.assertTrue(hits and all(h["spec"] == "36.331" for h in hits))
        self.assertEqual(ec.search(fx.out, "paging", k=3, filters={"release": ["99"]}), [])
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(ec.main(["query", "-o", str(fx.out), "paging occasion DRX", "-k", "2"]), 0)
        self.assertIn("clause 5.5 Paging", buf.getvalue())


class TestNpy(unittest.TestCase):
    def test_header_is_standard_and_grows(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        s = ec.NpyStore(tmp / "e.npy")
        for n in (1, 9, 990, 123_456 % 1000):
            s.append(np.random.rand(n, 7).astype(np.float32))
            s.sync()
            self.assertEqual(np.load(tmp / "e.npy").shape, (s.rows, 7))
        s.close()
        self.assertEqual(ec.NpyStore.read_shape(tmp / "e.npy"), (1456, 7))


class FakeOllama(BaseHTTPRequestHandler):
    calls = []
    legacy_only = False

    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path == "/api/tags":
            return self._send(200, {"models": [{"name": "nomic-embed-text:latest"}]})
        self._send(404, {"error": "no"})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeOllama.calls.append((self.path, body))
        h = ec.HashEmbedder(32)
        if self.path == "/api/embed" and not FakeOllama.legacy_only:
            return self._send(200, {"embeddings": [v.tolist() for v in h.embed(body["input"])]})
        if self.path == "/api/embeddings":
            return self._send(200, {"embedding": h.embed([body["prompt"]])[0].tolist()})
        self._send(404, {"error": "404 page"})


class TestOllama(unittest.TestCase):
    def setUp(self):
        self.srv = HTTPServer(("127.0.0.1", 0), FakeOllama)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_port}"
        FakeOllama.calls, FakeOllama.legacy_only = [], False
        self.fx = Fx()
        self.addCleanup(self.fx.close)
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        self.fx.add("38_series_38.331_38331-f30.zip_a.docx")
        self.fx.write_zip()

    def _run(self, *extra):
        a = ec.make_parser().parse_args(["embed", "-d", str(self.fx.ds), "-o", str(self.fx.out), "--quiet",
                                         "--ollama-url", self.url, "--batch-size", "4", *extra])
        with redirect_stderr(io.StringIO()):
            try:
                return ec.cmd_embed(a)
            except SystemExit as e:
                return e.code

    def test_batches_prefix_and_query(self):
        self.assertEqual(self._run(), 0)
        embeds = [b for p, b in FakeOllama.calls if p == "/api/embed"]
        self.assertTrue(embeds and all(len(b["input"]) <= 4 for b in embeds))
        self.assertTrue(all(t.startswith("search_document: ") for b in embeds for t in b["input"]))
        self.assertEqual(embeds[0]["model"], "nomic-embed-text")
        ec.search(self.fx.out, "paging", k=1)
        self.assertTrue(FakeOllama.calls[-1][1]["input"][0].startswith("search_query: "))
        self.assertTrue(ec.validate_store(self.fx.out)["ok"])

    def test_legacy_endpoint_fallback(self):
        FakeOllama.legacy_only = True
        self.assertEqual(self._run(), 0)
        self.assertTrue(any(p == "/api/embeddings" for p, _ in FakeOllama.calls))

    def test_model_not_pulled(self):
        self.assertIn("ollama pull", str(self._run("--model", "bge-m3")))

    def test_server_down_stops_cleanly(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.assertIn("not reachable", str(self._run()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
