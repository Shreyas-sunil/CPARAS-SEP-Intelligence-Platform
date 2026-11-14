"""
Enriched dataset export: ETSI_Series_Enriched_v0.3.xlsx + a matching JSONL.

Preserves the original manifest columns (ETSI_Dir, PDF_URL, Standard Name)
and adds every extracted technical field (item 8). Full raw extracted text
is NEVER written into a spreadsheet cell -- it lives on disk under
ETSI_DATASET_OUTPUT/text/{id}.txt (see pipeline.py), and only its path is
referenced in `raw_extracted_text_path` / provided separately in the JSONL.
"""

from __future__ import annotations

import io
import json
from datetime import datetime, timezone
from typing import Any, Iterable

import openpyxl
from openpyxl.utils import get_column_letter
from openpyxl.styles import Font, PatternFill

ENRICHED_COLUMNS: list[str] = [
    "ETSI_Dir", "PDF_URL", "Standard Name", "Document Type", "Series",
    "Specification Number", "Version", "Release", "Title", "Publication Date",
    "Technology Domain", "Subdomain", "Technology Topics", "Technical Keywords",
    "Protocols", "Interfaces", "Network Components", "Purpose", "Scope",
    "Executive Summary", "Technical Summary", "Key Features", "Major Procedures",
    "Important Mechanisms", "Important Parameters", "Normative Requirements",
    "Patent Relevant Concepts", "Potentially Essential Features", "Technical Terms",
    "Clause Information", "Extraction Method", "Extraction Confidence",
    "Source Pages", "Processing Status", "Processing Error", "Processed At",
]


def _join(values: Any, limit: int = 25) -> str:
    """Join a list (or a JSON-encoded list) into a '; '-separated string, capped at `limit` items."""
    if not values:
        return ""
    if isinstance(values, str):
        try:
            values = json.loads(values)
        except (json.JSONDecodeError, TypeError):
            return values
    if isinstance(values, list):
        return " | ".join(str(v) for v in values[:limit])
    return str(values)


def row_from_document_record(rec: dict) -> list[Any]:
    """Maps one document record (a dict of EtsiDocument fields, as produced
    by pipeline.py's _document_to_record()) into one ENRICHED_COLUMNS row."""
    return [
        rec.get("etsi_dir") or "",
        rec.get("pdf_url") or "",
        rec.get("standard_name_raw") or "",
        rec.get("document_type") or "Unknown",
        rec.get("series") or "Unknown",
        rec.get("specification_number") or "Unknown",
        rec.get("version") or "Unknown",
        rec.get("release") or "Unknown",
        rec.get("title") or "Unknown",
        rec.get("publication_date") or "Unknown",
        rec.get("technology_domain") or "Unknown",
        rec.get("subdomain") or "Unknown",
        _join(rec.get("topics_json")),
        _join(rec.get("keywords_json")),
        _join(rec.get("protocols_json")),
        _join(rec.get("interfaces_json")),
        _join(rec.get("network_components_json")),
        rec.get("purpose") or "Unknown",
        rec.get("scope") or "Unknown",
        rec.get("executive_summary") or "Unknown",
        rec.get("technical_summary") or "Unknown",
        _join(rec.get("key_features_json")),
        _join(rec.get("major_procedures_json")),
        _join(rec.get("important_mechanisms_json")),
        _join(rec.get("important_parameters_json")),
        _join(rec.get("normative_requirements_json")),
        _join(rec.get("patent_relevant_concepts_json")),
        _join(rec.get("potentially_essential_features_json")),
        _join(rec.get("technical_terms_json")),
        _join([f"{c.get('clause_number', '')}: {c.get('clause_title', '')}" for c in _loads(rec.get("clauses_json"))], limit=15),
        rec.get("extraction_method") or "Unknown",
        rec.get("extraction_confidence") if rec.get("extraction_confidence") is not None else "",
        _join(rec.get("source_pages_json")),
        rec.get("status") or "pending",
        rec.get("error_message") or "",
        rec.get("updated_at") or "",
    ]


def _loads(v: Any) -> list:
    """Value as a list: lists unchanged, JSON strings decoded, anything else empty."""
    if not v:
        return []
    if isinstance(v, list):
        return v
    try:
        return json.loads(v)
    except (json.JSONDecodeError, TypeError):
        return []


def build_enriched_workbook(records: Iterable[dict]) -> bytes:
    """Builds ETSI_Series_Enriched_v0.3.xlsx as bytes. Streams rows in
    write-only mode so this scales to the full manifest size without
    holding the whole workbook's cell objects in memory (item 28)."""
    wb = openpyxl.Workbook(write_only=True)
    ws = wb.create_sheet("ETSI_Enriched")
    ws.append(ENRICHED_COLUMNS)

    n = 0
    for rec in records:
        ws.append(row_from_document_record(rec))
        n += 1

    meta_ws = wb.create_sheet("Export Info")
    meta_ws.append(["Generated", datetime.now(timezone.utc).isoformat()])
    meta_ws.append(["Rows", n])
    meta_ws.append(["Source", "GITAS / SEP Intelligence Platform v0.3 -- ETSI Standards Knowledge Pipeline"])
    meta_ws.append(["Note", "Full raw extracted text is stored separately (see data/etsi_corpus/output/text/), not in this workbook."])

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def build_jsonl(records: Iterable[dict]) -> bytes:
    """Machine-readable companion export (item 8: 'Also produce a
    machine-readable format such as JSONL'). One JSON object per line,
    with the full structured record (unlike the Excel export's flattened/
    joined string columns)."""
    lines = []
    for rec in records:
        obj = {k: v for k, v in rec.items() if not k.startswith("_")}
        for json_field in [k for k in obj if k.endswith("_json")]:
            obj[json_field.removesuffix("_json")] = _loads(obj.pop(json_field))
        lines.append(json.dumps(obj, default=str, ensure_ascii=False))
    return ("\n".join(lines) + ("\n" if lines else "")).encode("utf-8")
