"""
v0.4.4 - CPARAS Bulk keeps its output contract while it can use the dataset corpus.

Synthetic fixture only (tests/fixtures/build_sample); this is not a test against the full dataset.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

FIX = Path(__file__).parent / "fixtures" / "build_sample"

# The 21 enrichment columns exactly as shipped in v0.4.3 (names AND order must not change).
EXPECTED_BULK_COLUMNS = [
    "CPARAS_Bulk_Status", "CPARAS_Bulk_Essentiality", "CPARAS_Bulk_Overall_Confidence",
    "CPARAS_Bulk_Evidence_Level", "CPARAS_Bulk_Evidence_Label", "CPARAS_Bulk_Mapping_Type",
    "CPARAS_Bulk_Claim_Match", "CPARAS_Bulk_Semantic_Similarity", "CPARAS_Bulk_Standard_Coverage",
    "CPARAS_Bulk_Extraction_Confidence", "CPARAS_Bulk_Standard_Family", "CPARAS_Bulk_Standard_Version",
    "CPARAS_Bulk_Standard_Release", "CPARAS_Bulk_Recommended_Clause", "CPARAS_Bulk_Claim_1",
    "CPARAS_Bulk_Claim_Extraction_Status", "CPARAS_Bulk_Corpus_Coverage", "CPARAS_Bulk_Declaration_Conflict",
    "CPARAS_Bulk_Version_Inconsistency", "CPARAS_Bulk_Reason_Codes", "CPARAS_Bulk_Validation_Status",
]


def _cfg():
    mod = sys.modules.get("core.config")
    if mod is None:
        import core.config as mod
    return mod.settings


@pytest.fixture()
def bulk(monkeypatch):
    from research import bulk_corpus
    st = _cfg()

    def use(root="", mode="auto"):
        monkeypatch.setattr(st, "CPARAS_DATASET_ROOT", str(root), raising=False)
        monkeypatch.setattr(st, "BULK_CORPUS_SOURCE", mode, raising=False)
        bulk_corpus._cache.update(key=None, corpus=None)
        return bulk_corpus
    yield use
    bulk_corpus_mod = sys.modules.get("research.bulk_corpus")
    if bulk_corpus_mod:
        bulk_corpus_mod._cache.update(key=None, corpus=None)


def test_bulk_column_contract_is_unchanged():
    from etsi_bulk import excel_export as xe
    assert len(xe.GITAS_COLUMNS) >= len(EXPECTED_BULK_COLUMNS)
    assert xe.GITAS_COLUMNS[:len(EXPECTED_BULK_COLUMNS)] == EXPECTED_BULK_COLUMNS or \
        [c for c in xe.GITAS_COLUMNS if c in EXPECTED_BULK_COLUMNS] == EXPECTED_BULK_COLUMNS
    # no CPARAS_Bulk_* column was added or removed beyond the shipped set
    shipped = set(EXPECTED_BULK_COLUMNS) | {"CPARAS_Bulk_Validation_Notes", "CPARAS_Bulk_Classification", "CPARAS_Bulk_Report_Link",
                                            # v0.4.9: added on request, next to CPARAS_Bulk_Essentiality
                                            "CPARAS_Bulk_Essentiality_Confidence"}
    assert {c for c in xe.GITAS_COLUMNS if c.startswith("CPARAS_Bulk_")} <= shipped


def test_unset_root_uses_illustrative_corpus_exactly_as_before(bulk):
    from research.standards_corpus import STANDARDS_CORPUS
    b = bulk("", "auto")
    c = b.get_bulk_corpus()
    assert c.source == "illustrative" and c.entries is STANDARDS_CORPUS
    assert "CPARAS_DATASET_ROOT not set" in c.info["fallback_reason"]


def test_dataset_corpus_loaded_with_real_versions_and_unknowns_kept(bulk):
    c = bulk(FIX, "auto").get_bulk_corpus()
    assert c.source == "dataset" and c.fingerprint
    fams = set(c.all_families())
    assert {"23.501", "38.331"} <= fams
    vers = {(e.family, e.version, e.release) for e in c.entries}
    assert all(isinstance(v[1], str) and v[1] for v in vers)
    # nothing is guessed: release/version strings are exactly what the corpus file holds
    data = json.loads((FIX / "standards_corpus_3gpp.json").read_text(encoding="utf-8"))
    want = {(f["family"], v["version"], v["release"]) for f in data["families"] for cl in f["clauses"] for v in cl["versions"] if v.get("text")}
    assert vers == want
    assert all(e.year == 0 or e.year > 1990 for e in c.entries)


def test_illustrative_source_setting_never_touches_the_dataset(bulk):
    c = bulk(FIX, "illustrative").get_bulk_corpus()
    assert c.source == "illustrative"


def test_dataset_mode_is_strict_but_auto_falls_back_with_a_reason(bulk, tmp_path):
    with pytest.raises(Exception):
        bulk(tmp_path / "nope", "dataset").get_bulk_corpus()
    c = bulk(tmp_path / "nope", "auto").get_bulk_corpus()
    assert c.source == "illustrative" and c.info.get("fallback_reason")


def test_wrong_schema_corpus_falls_back_not_crashes(bulk, tmp_path):
    (tmp_path / "standards_corpus_3gpp.json").write_text(json.dumps({"schema": "x/1", "families": []}), encoding="utf-8")
    c = bulk(tmp_path, "auto").get_bulk_corpus()
    assert c.source == "illustrative"


def test_scoring_against_dataset_corpus_keeps_row_contract(bulk):
    bulk(FIX, "dataset")
    from etsi_bulk.scoring import score_records
    rec = {"title": "RRC connection reconfiguration", "standard_raw": "TS 38.331", "spec_number": "38.331",
           "spec_version": "v15.3.0", "illustrative_part": "RRC reconfiguration procedure", "explicit_disclosure": True,
           "dipg_id": "D1", "publication_number": "P1", "application_number": "A1", "project_acronym": "3GPP",
           "legal_status": "Active", "gen_5g": True}
    out, meta = asyncio.run(score_records([rec]))
    assert len(out) == 1
    assert meta["corpus_source"] == "dataset"
    row = out[0]
    for k in ("matched_standard", "semantic_similarity", "claim_match", "standard_coverage", "release", "essentiality", "matched_clause"):
        assert k in row
    assert 0.0 <= float(row["semantic_similarity"]) <= 1.0
    assert row["essentiality"] in ("Essential", "Potential", "Non-Essential")
    # the matched standard is a real dataset entry, with the dataset's own version/release strings
    assert row["matched_standard"].startswith("TS ")
    assert row["matched_standard"].split()[1] in {"23.501", "33.501", "36.321", "38.211", "38.331"}
    assert row["release"] in ("Rel-15", "Rel-16", "Rel-17", "Unknown") or row["release"].startswith("Rel-")
