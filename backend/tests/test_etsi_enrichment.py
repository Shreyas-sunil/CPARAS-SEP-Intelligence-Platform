"""
Tests for etsi_corpus/enrichment.py.

Run: python -m pytest backend/tests/test_etsi_enrichment.py -q
"""
import io
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import openpyxl  # noqa: E402
from etsi_corpus.enrichment import (  # noqa: E402
    build_enriched_workbook, build_jsonl, ENRICHED_COLUMNS, row_from_document_record,
)

SAMPLE_RECORD = {
    "etsi_dir": "100200_100299", "pdf_url": "https://www.etsi.org/deliver/x.pdf",
    "standard_name_raw": "TS 100 220-1 V1.1.1", "document_type": "TS", "series": "100 220-1",
    "specification_number": "100 220-1", "version": "1.1.1", "release": "Unknown",
    "title": "Electromagnetic compatibility", "publication_date": "Unknown",
    "technology_domain": "Short Range Devices", "subdomain": "Unknown",
    "topics_json": json.dumps(["EMC", "radio equipment"]),
    "keywords_json": json.dumps(["interference"]),
    "protocols_json": json.dumps([]), "interfaces_json": json.dumps([]),
    "network_components_json": json.dumps([]),
    "purpose": "Defines EMC requirements.", "scope": "Applies to SRD equipment.",
    "executive_summary": "Summary.", "technical_summary": "Technical summary.",
    "key_features_json": json.dumps(["feature A"]),
    "major_procedures_json": json.dumps([]), "important_mechanisms_json": json.dumps([]),
    "important_parameters_json": json.dumps([]), "normative_requirements_json": json.dumps(["shall not exceed X"]),
    "patent_relevant_concepts_json": json.dumps(["concept A"]),
    "potentially_essential_features_json": json.dumps([]),
    "technical_terms_json": json.dumps(["EMC"]),
    "clauses_json": json.dumps([{"clause_number": "5.1", "clause_title": "General", "summary": "..."}]),
    "extraction_method": "pdf_text", "extraction_confidence": 0.82,
    "source_pages_json": json.dumps([1, 2]), "status": "completed",
    "error_message": None, "updated_at": "2026-08-31T00:00:00",
}


def test_row_from_document_record_matches_column_count():
    row = row_from_document_record(SAMPLE_RECORD)
    assert len(row) == len(ENRICHED_COLUMNS)
    assert row[0] == "100200_100299"
    assert row[3] == "TS"


def test_row_never_embeds_raw_full_text():
    # SAMPLE_RECORD deliberately has no raw text field mapped into the row --
    # this test guards against a future column accidentally adding one.
    row = row_from_document_record(SAMPLE_RECORD)
    for cell in row:
        assert not (isinstance(cell, str) and len(cell) > 5000), "enriched row must not embed huge raw text"


def test_build_enriched_workbook_has_expected_header_and_rows():
    xlsx_bytes = build_enriched_workbook([SAMPLE_RECORD, SAMPLE_RECORD])
    wb = openpyxl.load_workbook(io.BytesIO(xlsx_bytes))
    ws = wb["ETSI_Enriched"]
    header = [c.value for c in next(ws.iter_rows(min_row=1, max_row=1))]
    assert header == ENRICHED_COLUMNS
    data_rows = list(ws.iter_rows(min_row=2, values_only=True))
    assert len(data_rows) == 2
    assert "Export Info" in wb.sheetnames


def test_build_jsonl_round_trips_structured_fields():
    jsonl_bytes = build_jsonl([SAMPLE_RECORD])
    lines = jsonl_bytes.decode("utf-8").strip().split("\n")
    assert len(lines) == 1
    obj = json.loads(lines[0])
    assert obj["topics"] == ["EMC", "radio equipment"]
    assert obj["clauses"][0]["clause_number"] == "5.1"
    assert "topics_json" not in obj


def test_build_jsonl_empty_input():
    assert build_jsonl([]) == b""


if __name__ == "__main__":
    test_row_from_document_record_matches_column_count()
    test_row_never_embeds_raw_full_text()
    test_build_enriched_workbook_has_expected_header_and_rows()
    test_build_jsonl_round_trips_structured_fields()
    test_build_jsonl_empty_input()
    print("OK")
