"""
Tests for the v0.3-continuation cPARAS fix (audit items 8-14): essentiality
analysis prefers REAL ETSI Knowledge Base evidence (via
services/retrieval_service.py) over the illustrative static corpus, on a
per-patent basis, with graceful fallback -- and never lets an LLM invent
the confidence number.

Uses mocked retrieval results (clearly-labeled test doubles), never
fabricated as real ETSI content. No network, no real LLM, no real
embedding model.

Run: python -m pytest backend/tests/test_cparas_etsi_kb_integration.py -q
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from research import engine as research_engine  # noqa: E402
from research.etsi_kb_bridge import EtsiKbEvidence  # noqa: E402


SAMPLE_DISCLOSURE = {
    "explicit_data": {
        "title": "Method for beam management",
        "applicant": "Example Corp",
        "standards": ["TS 138 331"],
    },
    "sep_analysis": {},
    "inferred_fields": {},
}

SAMPLE_DISCLOSURE_NO_KB_MATCH = {
    "explicit_data": {
        "title": "Method for widget assembly",
        "applicant": "Example Corp",
        "standards": [],
    },
    "sep_analysis": {},
    "inferred_fields": {},
}


def test_kb_evidence_used_when_available(monkeypatch):
    """When the ETSI KB has a real match for a patent, that match --
    not the illustrative corpus -- drives recommended_standard/clause/
    similarity, and evidence_source records which one was actually used."""
    real_hit = EtsiKbEvidence(
        standard="TS 138 331", specification_number="138 331", version="17.0.0", release="Rel-17",
        domain="5G NR", clause="6.3.2", clause_title="Beam management",
        page=42, similarity=0.87, evidence_text="The UE shall report beam quality using reference signal measurements.",
        etsi_document_id=7, source_url="https://www.etsi.org/deliver/x.pdf",
    )

    async def fake_lookup(patent_texts, min_score=0.35):
        return [real_hit]

    monkeypatch.setattr(research_engine, "lookup_etsi_kb_evidence", fake_lookup)

    async def fake_kb_version():
        return "v0.3"
    monkeypatch.setattr(research_engine, "_current_kb_version", fake_kb_version)

    outputs = asyncio.run(research_engine.analyze_patents([SAMPLE_DISCLOSURE], generate_figures=False))
    rec = outputs["records"][0]

    assert rec["evidence_source"] == "etsi_knowledge_base"
    assert rec["recommended_standard"] == "TS 138 331"
    assert rec["standard_clause"] == "6.3.2"
    assert rec["similarity_score"] == 0.87
    assert rec["evidence_text"] == real_hit.evidence_text
    assert rec["knowledge_base_version"] == "v0.3"
    assert rec["etsi_document_id"] == 7
    assert outputs["meta"]["n_patents_using_etsi_kb"] == 1
    assert outputs["meta"]["n_patents_using_illustrative_corpus"] == 0
    assert outputs["meta"]["knowledge_base_version"] == "v0.3"

    # confidence is still deterministically computed, not LLM-invented --
    # this patent has no sep_analysis.confidence, so weighting falls back
    # gracefully but the number is still a real weighted blend, not a
    # constant.
    assert 0.0 <= rec["confidence_score"] <= 1.0
    assert rec["predicted_sep_status"] in ("Confirmed SEP", "Likely SEP", "Potential SEP", "Insufficient Evidence")


def test_falls_back_to_illustrative_corpus_when_kb_has_no_match(monkeypatch):
    """A patent with no real ETSI KB hit falls back to the illustrative
    corpus for THAT patent -- the fallback is honestly labeled, not
    silently presented as authoritative."""
    async def fake_lookup(patent_texts, min_score=0.35):
        return [None]  # KB found nothing relevant

    monkeypatch.setattr(research_engine, "lookup_etsi_kb_evidence", fake_lookup)

    async def fake_kb_version():
        return "Unknown"
    monkeypatch.setattr(research_engine, "_current_kb_version", fake_kb_version)

    outputs = asyncio.run(research_engine.analyze_patents([SAMPLE_DISCLOSURE_NO_KB_MATCH], generate_figures=False))
    rec = outputs["records"][0]

    assert rec["evidence_source"] == "illustrative_reference_corpus"
    # Illustrative-corpus fields are still populated from STANDARDS_CORPUS
    assert rec["recommended_standard"]
    assert rec["standard_clause"]
    assert outputs["meta"]["n_patents_using_etsi_kb"] == 0
    assert outputs["meta"]["n_patents_using_illustrative_corpus"] == 1


def test_mixed_batch_each_patent_uses_its_own_best_source(monkeypatch):
    """A batch where patent 1 has real KB evidence and patent 2 doesn't --
    each patent independently picks its source; a miss for one patent
    never forces the whole batch onto the fallback."""
    real_hit = EtsiKbEvidence(
        standard="TS 138 331", specification_number="138 331", version="17.0.0", release="Rel-17",
        domain="5G NR", clause="6.3.2", clause_title="Beam management",
        page=1, similarity=0.80, evidence_text="Real ETSI evidence text.",
        etsi_document_id=1, source_url="https://www.etsi.org/deliver/x.pdf",
    )

    async def fake_lookup(patent_texts, min_score=0.35):
        return [real_hit, None]

    monkeypatch.setattr(research_engine, "lookup_etsi_kb_evidence", fake_lookup)

    async def fake_kb_version():
        return "v0.3"
    monkeypatch.setattr(research_engine, "_current_kb_version", fake_kb_version)

    outputs = asyncio.run(research_engine.analyze_patents(
        [SAMPLE_DISCLOSURE, SAMPLE_DISCLOSURE_NO_KB_MATCH], generate_figures=False,
    ))
    sources = [r["evidence_source"] for r in outputs["records"]]
    assert sources == ["etsi_knowledge_base", "illustrative_reference_corpus"]


def test_declared_match_checked_against_real_specification_number(monkeypatch):
    """declared_match (feeding the Confirmed-SEP threshold) is computed
    against the REAL retrieved specification number when KB evidence is
    used, not the illustrative corpus's family string."""
    matching_hit = EtsiKbEvidence(
        standard="TS 138 331", specification_number="138 331", version="17.0.0", release="Rel-17",
        domain="5G NR", clause="6.3.2", clause_title="Beam management",
        page=1, similarity=0.95, evidence_text="Strong evidence text here.",
        etsi_document_id=1, source_url="https://www.etsi.org/deliver/x.pdf",
    )

    async def fake_lookup(patent_texts, min_score=0.35):
        return [matching_hit]
    monkeypatch.setattr(research_engine, "lookup_etsi_kb_evidence", fake_lookup)

    async def fake_kb_version():
        return "v0.3"
    monkeypatch.setattr(research_engine, "_current_kb_version", fake_kb_version)

    # SAMPLE_DISCLOSURE declares "TS 138 331" -- should match "138 331" via
    # substring check -> declared_match True -> similarity 0.95 >= 0.60
    # threshold -> "Confirmed SEP".
    outputs = asyncio.run(research_engine.analyze_patents([SAMPLE_DISCLOSURE], generate_figures=False))
    rec = outputs["records"][0]
    assert rec["predicted_sep_status"] == "Confirmed SEP"


if __name__ == "__main__":
    print("Run via: python -m pytest backend/tests/test_cparas_etsi_kb_integration.py -q")
