"""
v0.4.1 regressions — Overall Confidence, Essentiality column, Semantic
Similarity saturation.

Reported symptoms (CPARAS Bulk / GITAS patent detail page):
  * "Overall Confidence 100%" shown next to "Potential SEP".
  * Semantic Similarity 100% for every row.
  * Overall Confidence was exported as CPARAS_Bulk_Extraction_Confidence,
    which by name should be an extraction-quality measure.

Runs the real upload -> score -> export pipeline on the bundled
sample_data/sample_bulk_export.xlsx (tfidf-fallback embedder when
sentence-transformers is not installed).
"""
import asyncio
import importlib
import io
import os
import sys
import tempfile

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

SAMPLE = os.path.join(os.path.dirname(__file__), "..", "..", "sample_data", "sample_bulk_export.xlsx")


# ── Semantic similarity calibration ─────────────────────────────────────────

def test_match_calibration_does_not_saturate_best_matches():
    """Top-1-of-54 scores sit above the full matrix's 95th percentile by
    construction; the old calibration clipped every one of them to 1.0."""
    from research.embeddings import calibrate_similarity_matrix, calibrate_match_scores
    rng = np.random.default_rng(0)
    raw = rng.normal(0.35, 0.06, size=(200, 54)).clip(0, 1)
    raw[np.arange(200), rng.integers(0, 54, 200)] += rng.uniform(0.05, 0.3, 200)

    old, _ = calibrate_similarity_matrix(raw)
    new, meta = calibrate_match_scores(raw)
    old_top, new_top = old.max(axis=1), new.max(axis=1)

    assert (old_top >= 0.999).mean() > 0.9  # documents the bug
    assert (new_top >= 0.999).mean() == 0.0
    assert new_top.std() > 0.02
    assert meta["high"] == 1.0
    # ordering preserved
    assert np.array_equal(np.argsort(raw.max(axis=1)), np.argsort(new_top))


def test_bulk_scoring_sample_similarity_is_not_all_100_percent():
    if not os.path.isfile(SAMPLE):
        pytest.skip("sample workbook not present")
    from etsi_bulk.parser import parse_bulk_workbook
    from etsi_bulk.scoring import score_records
    recs = parse_bulk_workbook("sample_bulk_export.xlsx", open(SAMPLE, "rb").read())["records"]
    out, meta = asyncio.run(score_records(recs))
    sims = [r["semantic_similarity"] for r in out]
    assert sum(1 for s in sims if s >= 0.999) == 0
    assert len(set(round(s, 3) for s in sims)) > 1
    for r in out:
        # Overall confidence is no longer a copy of semantic similarity …
        assert r["confidence"] != r["semantic_similarity"]
        # … and the label is derived from it.
        from research.three_signal_scoring import essentiality_from_confidence
        assert r["essentiality"] == essentiality_from_confidence(r["confidence"])


def test_declared_family_tiebreak_prefers_declared_clause_on_near_tie():
    """'Conditional handover' declared against TS 38.331 / 5.3.5 must map to
    TS 38.331 5.3.5 (RRC Reconfiguration and Handover), not lose a ~0.01
    near-tie to TS 23.501 on generic words like 'network'."""
    if not os.path.isfile(SAMPLE):
        pytest.skip("sample workbook not present")
    from etsi_bulk.parser import parse_bulk_workbook
    from etsi_bulk.scoring import score_records
    recs = parse_bulk_workbook("sample_bulk_export.xlsx", open(SAMPLE, "rb").read())["records"]
    out, _ = asyncio.run(score_records(recs))
    ho = [r for r in out if "conditional handover" in (r["title"] or "").lower()]
    assert ho
    for r in ho:
        assert r["matched_standard"].startswith("TS 38.331")
        assert r["declared_family_match"] is True


def test_expired_patent_label_matches_confidence():
    """The old code downgraded Essential -> Potential on an expired legal
    status without touching the confidence (the 'Potential SEP / 100%'
    contradiction). The label must now always match the confidence band."""
    from etsi_bulk.scoring import _finalize_row
    from research.standards_corpus import STANDARDS_CORPUS
    from research.three_signal_scoring import essentiality_from_confidence
    entry = next(e for e in STANDARDS_CORPUS if e.family == "38.321")
    row = {"title": "Random access procedure with two-step RACH", "standard_raw": "TS 38.321",
           "spec_number": "38.321", "spec_version": "17.4.0", "illustrative_part": "5.1",
           "legal_status": "Expired - Lifetime"}
    scored = _finalize_row(row, entry, 0.9, explicit=True)
    assert scored["essentiality"] == essentiality_from_confidence(scored["confidence"])


# ── Export columns (unit) ───────────────────────────────────────────────────

def test_bulk_export_has_essentiality_and_overall_confidence_columns():
    from types import SimpleNamespace
    from etsi_bulk.excel_export import GITAS_COLUMNS, gitas_fields_for_patent
    assert "CPARAS_Bulk_Essentiality" in GITAS_COLUMNS
    assert "CPARAS_Bulk_Overall_Confidence" in GITAS_COLUMNS
    p = SimpleNamespace(
        id=7, essentiality="Potential", confidence=0.52, semantic_similarity=0.61,
        claim_match=0.3, standard_coverage=0.6, declared_family_match=True,
        matched_standard="TS 38.331 v16.2.0", matched_clause="Clause 5.3.5",
        matched_clause_title="RRC Reconfiguration and Handover", matched_domain="RRC",
        illustrative_part="5.3.5", title="Conditional handover", standard_raw="TS 38.331",
        spec_number="38.331", spec_version="16.5.0", release="Rel-16", legal_status=None,
        technology="5G NR", application_number="US1", publication_number=None, company="X",
    )
    f = gitas_fields_for_patent(p, run_id=1)
    assert f["CPARAS_Bulk_Essentiality"] == "Potential"
    assert f["CPARAS_Bulk_Overall_Confidence"] == 0.52
    assert f["CPARAS_Bulk_Extraction_Confidence"] == 1.0  # all key source fields present
    assert f["CPARAS_Bulk_Extraction_Confidence"] != f["CPARAS_Bulk_Overall_Confidence"]


def test_isld_export_has_essentiality_and_overall_confidence_columns():
    import json
    from types import SimpleNamespace
    mod = importlib.import_module("api.routes.cparas.excel_export")
    assert "CPARAS_Essentiality" in mod.CPARAS_COLUMNS
    assert "CPARAS_Overall_Confidence" in mod.CPARAS_COLUMNS
    p = SimpleNamespace(
        disclosure_number=1, application_number="US1", publication_number=None,
        standards='["TS 38.331"]', project=None, country="US", applicant="A", title="T",
        predicted_sep_status="Likely SEP", claim_match=0.4, standard_similarity=0.6,
        standard_coverage=0.7, research_confidence=0.66, recommended_standard="TS 38.331",
        recommended_version="v16.2.0", standard_clause="Clause 5.3.5",
        verification_status="Verified", evidence_source="illustrative_reference_corpus",
        inferred_fields=json.dumps({"completeness": {"score": 0.8},
                                    "extraction_verification": {"any_checked": True,
                                                                "application_number": {"verified": True},
                                                                "publication_number": None,
                                                                "standards": [{"verified": False}]}}),
    )
    v = mod._row_values(p)
    assert v["CPARAS_Essentiality"] == "Essential"
    assert v["CPARAS_Overall_Confidence"] == 0.66
    assert v["CPARAS_Extraction_Confidence"] == round((0.8 + 0.5) / 2, 4)


# ── End to end: upload -> export ────────────────────────────────────────────

def test_end_to_end_export_columns_are_consistent():
    if not os.path.isfile(SAMPLE):
        pytest.skip("sample workbook not present")
    httpx = pytest.importorskip("httpx")
    openpyxl = pytest.importorskip("openpyxl")
    from research.three_signal_scoring import essentiality_from_confidence

    with tempfile.TemporaryDirectory() as tmp:
        os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{os.path.join(tmp, 't.db')}"
        for m in list(sys.modules):
            if m in ("core.database", "core.config") or m.startswith("api.routes"):
                del sys.modules[m]
        core_database = importlib.import_module("core.database")
        routes = importlib.import_module("api.routes.gitas.dashboard")
        from fastapi import FastAPI
        app = FastAPI()
        app.include_router(routes.router, prefix="/api/sep")

        async def main():
            await core_database.init_db()
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
                r = await c.post("/api/sep/upload", files={"file": ("sample_bulk_export.xlsx", open(SAMPLE, "rb").read(),
                                 "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
                assert r.status_code == 200, r.text
                run_id = r.json()["run_id"]
                for _ in range(600):
                    rs = await c.get(f"/api/sep/runs/{run_id}/status")
                    if rs.status_code == 200 and rs.json().get("status") in ("completed", "completed_with_errors", "failed"):
                        break
                    await asyncio.sleep(0.1)
                assert rs.json().get("status") == "completed", rs.text
                pats = (await c.get("/api/sep/patents", params={"limit": 5})).json()["patents"]
                detail = (await c.get(f"/api/sep/patents/{pats[0]['id']}")).json()
                x = await c.get(f"/api/sep/runs/{run_id}/export/excel")
                return detail, x
        detail, x = asyncio.run(main())

    det = detail["essentiality_determination"]
    assert det["score_breakdown"]["semantic_similarity"] < 100
    assert det["overall_confidence"] != det["score_breakdown"]["semantic_similarity"]

    assert x.status_code == 200, x.text[:300]
    ws = openpyxl.load_workbook(io.BytesIO(x.content), data_only=True)["Sheet1"]
    hdr = [c.value for c in ws[1]]
    i_notes, i_cls = hdr.index("CPARAS_Bulk_Validation_Notes"), hdr.index("CPARAS_Bulk_Classification")
    assert i_cls == i_notes + 1  # sits right next to the notes column
    i_ess, i_conf, i_sim = (hdr.index("CPARAS_Bulk_Essentiality"),
                            hdr.index("CPARAS_Bulk_Overall_Confidence"),
                            hdr.index("CPARAS_Bulk_Semantic_Similarity"))
    rows = [r for r in ws.iter_rows(min_row=2, values_only=True) if r[i_ess] is not None]
    assert rows
    for r in rows:
        assert r[i_ess] in ("Essential", "Potential", "Non-Essential")
        assert r[i_ess] == essentiality_from_confidence(r[i_conf])
        assert r[i_sim] < 0.999
        assert r[i_cls] == r[i_ess]  # one word, consistent with the note and the label
        if r[i_notes] and "Classified" in r[i_notes]:
            assert r[i_cls] in r[i_notes]


def test_classification_word_extraction():
    from etsi_bulk.excel_export import classification_word
    assert classification_word("Classified Potential — recommended for manual essentiality review.", "Potential") == "Potential"
    assert classification_word("Classified Essential but mapped to a different standard family than declared (declared TS 38.331 ...", "Essential") == "Essential"
    assert classification_word(None, "Non-Essential") == "Non-Essential"
    assert classification_word("Legal status 'Expired' — patent is no longer in force", "Essential") == "Essential"
    assert classification_word(None, None) is None


# ── v0.4.9: Essentiality Confidence = how essential the patent is, in % ────

def test_bulk_export_essentiality_confidence_is_the_essentiality_percentage():
    from types import SimpleNamespace
    from etsi_bulk.excel_export import GITAS_COLUMNS, gitas_fields_for_patent
    i = GITAS_COLUMNS.index("CPARAS_Bulk_Essentiality")
    assert GITAS_COLUMNS[i + 1] == "CPARAS_Bulk_Essentiality_Confidence"
    base = dict(
        id=7, semantic_similarity=0.2, claim_match=0.1, standard_coverage=0.2, declared_family_match=False,
        matched_standard="TS 38.331 v16.2.0", matched_clause="Clause 5.3.5", matched_clause_title="RRC",
        matched_domain="RRC", illustrative_part=None, title="t", standard_raw="TS 38.331", spec_number="38.331",
        spec_version="16.5.0", release="Rel-16", legal_status=None, technology="5G NR",
        application_number="US1", publication_number=None, company="X",
    )
    # same number for every label: how essential the patent is
    for label, conf in (("Non-Essential", 0.15), ("Potential", 0.52), ("Essential", 0.77)):
        f = gitas_fields_for_patent(SimpleNamespace(essentiality=label, confidence=conf, **base), run_id=1)
        assert f["CPARAS_Bulk_Essentiality_Confidence"] == conf
    none = gitas_fields_for_patent(SimpleNamespace(essentiality=None, confidence=None, **base), run_id=1)
    assert none["CPARAS_Bulk_Essentiality_Confidence"] is None
