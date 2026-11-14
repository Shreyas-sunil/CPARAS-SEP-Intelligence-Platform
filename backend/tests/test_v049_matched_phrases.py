"""
v0.4.9: CPARAS ISLD reports the EXACT phrases a patent shares with its
matched standard clause (research/phrase_match.py), next to the clause and
version, in the analysis records, the Standard Mapping table, the database
and the ISLD Excel export.

Run: python -m pytest backend/tests/test_v049_matched_phrases.py -q
"""
import asyncio
import importlib
import json
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from research.phrase_match import find_matched_phrases  # noqa: E402

CLAUSE = ("5.3.5.13 Conditional reconfiguration. The UE shall evaluate the Conditional Handover execution "
          "condition for each candidate target cell; upon fulfilment the UE initiates Random Access to the "
          "target cell and applies the stored RRCReconfiguration.")


def test_finds_exact_shared_phrases_in_clause_wording():
    patent = ("Conditional handover execution in a wireless network | the UE evaluates a conditional "
              "handover execution condition and performs random access to the target cell")
    out = find_matched_phrases(patent, CLAUSE)
    # longest first, in the STANDARD's casing, stopwords trimmed at the ends
    assert out["phrases"] == ["Random Access to the target cell", "Conditional Handover execution condition"]
    for ph in out["phrases"]:
        assert ph in CLAUSE                                   # verbatim substring of the clause
        assert ph.lower() in patent.lower()                   # and of the patent


def test_sub_phrases_are_not_repeated_and_terms_exclude_phrase_words():
    out = find_matched_phrases("conditional handover execution condition. Also mentions RRCReconfiguration.", CLAUSE)
    assert "Conditional Handover execution condition" in out["phrases"]
    assert "Conditional Handover" not in out["phrases"]
    assert "RRCReconfiguration" in out["terms"]
    assert "Conditional" not in out["terms"]


def test_no_fake_matches():
    assert find_matched_phrases("", CLAUSE) == {"phrases": [], "terms": []}
    assert find_matched_phrases("widget assembly", None) == {"phrases": [], "terms": []}
    # only stopwords / numbers in common -> nothing
    out = find_matched_phrases("of the and for each 5.3.5.13", CLAUSE)
    assert out == {"phrases": [], "terms": []}


def test_engine_reports_matched_phrases_for_kb_evidence(monkeypatch):
    from research import engine as research_engine
    from research.etsi_kb_bridge import EtsiKbEvidence

    disc = {
        "explicit_data": {"title": "Conditional handover with random access to the target cell",
                          "applicant": "Example Corp", "standards": ["TS 38.331"]},
        "sep_analysis": {},
        "inferred_fields": {"probable_claim_scope": "UE evaluates a conditional handover execution condition"},
    }
    hit = EtsiKbEvidence(
        standard="TS 38.331", specification_number="38.331", version="17.0.0", release="Rel-17",
        domain="RRC", clause="5.3.5.13", clause_title="Conditional reconfiguration", page=None,
        similarity=0.8, evidence_text=CLAUSE[:60], etsi_document_id=None, source_url=None,
        full_text=CLAUSE,   # phrases are found in the WHOLE excerpt, not only the 600-char display cut
    )

    async def fake_lookup(texts, min_score=0.35):
        return [hit]

    async def fake_ver():
        return "test"
    monkeypatch.setattr(research_engine, "lookup_etsi_kb_evidence", fake_lookup)
    monkeypatch.setattr(research_engine, "_current_kb_version", fake_ver)

    out = asyncio.run(research_engine.analyze_patents([disc], generate_figures=False))
    rec = out["records"][0]
    assert "Random Access to the target cell" in rec["matched_phrases"]
    assert "Conditional Handover execution condition" in rec["matched_phrases"]
    assert rec["explanation"][0].startswith("80% semantic similarity")   # mapping "reason" unchanged
    row = out["tables"]["standard_mapping"][0]
    assert row["matched_phrases"] == rec["matched_phrases"]
    assert row["matched_clause"] == "5.3.5.13"


def test_engine_illustrative_corpus_path_has_phrase_fields(monkeypatch):
    from research import engine as research_engine

    async def no_kb(texts, min_score=0.35):
        return [None] * len(texts)

    async def fake_ver():
        return "test"
    monkeypatch.setattr(research_engine, "lookup_etsi_kb_evidence", no_kb)
    monkeypatch.setattr(research_engine, "_current_kb_version", fake_ver)
    disc = {"explicit_data": {"title": "RRC connection establishment procedure", "standards": ["TS 38.331"]},
            "sep_analysis": {}, "inferred_fields": {}}
    rec = asyncio.run(research_engine.analyze_patents([disc], generate_figures=False))["records"][0]
    assert isinstance(rec["matched_phrases"], list) and isinstance(rec["matched_terms"], list)
    from research.standards_corpus import STANDARDS_CORPUS
    entry = next(e for e in STANDARDS_CORPUS if e.standard == rec["recommended_standard"]
                 and e.version == rec["recommended_version"] and e.clause_label == rec["standard_clause"])
    clause_text = f"{entry.clause_title}. {entry.text}"
    assert rec["matched_phrases"] or rec["matched_terms"]
    for ph in rec["matched_phrases"] + rec["matched_terms"]:
        assert ph in clause_text
        assert ph.lower() in "rrc connection establishment procedure"


def test_isld_excel_export_has_matched_phrase_columns():
    mod = importlib.import_module("api.routes.cparas.excel_export")
    i = mod.CPARAS_COLUMNS.index("CPARAS_Standard_Clause")
    assert mod.CPARAS_COLUMNS[i + 1:i + 3] == ["CPARAS_Matched_Phrases", "CPARAS_Matched_Terms"]
    p = SimpleNamespace(
        disclosure_number=1, application_number="US1", publication_number=None, standards='["TS 38.331"]',
        project=None, country="US", applicant="A", title="T", predicted_sep_status="Likely SEP",
        claim_match=0.4, standard_similarity=0.6, standard_coverage=0.7, research_confidence=0.66,
        recommended_standard="TS 38.331", recommended_version="v16.2.0", standard_clause="Clause 5.3.5",
        verification_status="Verified", evidence_source="etsi_knowledge_base", inferred_fields=None,
        matched_phrases=json.dumps(["Random Access to the target cell", "Conditional Handover"]),
        matched_terms=json.dumps(["RRCReconfiguration"]),
    )
    v = mod._row_values(p)
    assert v["CPARAS_Matched_Phrases"] == "Random Access to the target cell; Conditional Handover"
    assert v["CPARAS_Matched_Terms"] == "RRCReconfiguration"
    # rows analysed before v0.4.9 have no value -> blank cell, not an error
    old = SimpleNamespace(**{**vars(p), "matched_phrases": None, "matched_terms": None})
    assert mod._row_values(old)["CPARAS_Matched_Phrases"] is None
    wb_bytes = mod.build_cparas_workbook(SimpleNamespace(filename="x.pdf"), [p, old])
    assert wb_bytes[:2] == b"PK"
