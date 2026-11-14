"""
CPARAS Excel Export
=====================
Downloadable "<filename>_CPARAS_enriched.xlsx" workbook for one stored
declaration's research/paper-style analysis outputs (research/engine.py),
built ONLY from already-persisted `Patent` rows — never recomputes or
re-embeds anything. Same "export is strictly downstream of scoring"
principle as `etsi_bulk/excel_export.py`'s CPARAS Bulk export, and the same
mandatory-columns contract:

    CPARAS_Claim_Match
    CPARAS_Semantic_Similarity
    CPARAS_Standard_Coverage

DO NOT rename these three. `CPARAS_Semantic_Similarity` reads from
`Patent.standard_similarity` — the same calibrated cosine-to-matched-clause
value `research/engine.py` already computed as `similarity_score`/
`semantic_similarity`; see that module's and `core.database.Patent`'s
docstrings for why this is a mapping onto an existing field rather than a
new one. `CPARAS_Claim_Match` / `CPARAS_Standard_Coverage` read from the
genuinely-new `Patent.claim_match` / `Patent.standard_coverage` columns
(research/three_signal_scoring.py — the same shared formulas CPARAS Bulk uses).
"""

from __future__ import annotations

import io
import logging

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

import json

from core.database import Declaration, Patent
from research.three_signal_scoring import essentiality_from_status
from utils.export_safety import xlsx_set

logger = logging.getLogger(__name__)

# The declaration's own fields for each disclosure row, as extracted from
# the uploaded file, come FIRST — the export is the input rows plus the
# enrichment, not the enrichment alone.
SOURCE_COLUMNS: list[str] = [
    "Disclosure_No",
    "Application_Number",
    "Publication_Number",
    "Declared_Standards",
    "Project",
    "Country",
]

CPARAS_COLUMNS: list[str] = [
    "CPARAS_Patent_Label",
    "CPARAS_Applicant",
    "CPARAS_Title",
    "CPARAS_Predicted_SEP_Status",
    # v0.4.1: explicit SEP determination columns.
    #   CPARAS_Essentiality       -> Essential / Potential / Non-Essential
    #                                (Confirmed/Likely SEP -> Essential,
    #                                 Potential SEP -> Potential,
    #                                 Insufficient Evidence -> Non-Essential)
    #   CPARAS_Overall_Confidence -> 0-1 confidence that the patent IS an SEP
    #                                (research/engine.py confidence_score);
    #                                the status above is derived from it.
    "CPARAS_Essentiality",
    "CPARAS_Overall_Confidence",
    "CPARAS_Claim_Match",
    "CPARAS_Semantic_Similarity",
    "CPARAS_Standard_Coverage",
    "CPARAS_Extraction_Confidence",
    "CPARAS_Recommended_Standard",
    "CPARAS_Recommended_Version",
    "CPARAS_Standard_Clause",
    # v0.4.9: the exact wording the patent shares with the matched clause
    # (research/phrase_match.py), "; "-separated, longest first.
    "CPARAS_Matched_Phrases",
    "CPARAS_Matched_Terms",
    "CPARAS_Verification_Status",
    "CPARAS_Evidence_Source",
]

_HEADER_FILL = PatternFill(start_color="FF1E3A5F", end_color="FF1E3A5F", fill_type="solid")
_HEADER_FONT = Font(color="FFFFFFFF", bold=True)

_NUMERIC_COLUMNS = (
    "CPARAS_Overall_Confidence", "CPARAS_Claim_Match", "CPARAS_Semantic_Similarity",
    "CPARAS_Standard_Coverage", "CPARAS_Extraction_Confidence",
)


class CparasExportValidationError(RuntimeError):
    """Raised when the just-generated workbook fails post-generation
    structural validation — mirrors
    `etsi_bulk.excel_export.ExcelExportValidationError`."""


ALL_COLUMNS: list[str] = SOURCE_COLUMNS + CPARAS_COLUMNS


def _standards_text(raw) -> str | None:
    """Declared standards stored as a JSON list -> 'TS 38.331, TS 38.300'; any other stored value is
    returned as text.
    """
    if not raw:
        return None
    try:
        v = json.loads(raw)
        if isinstance(v, list):
            return ", ".join(str(x) for x in v if x)
    except Exception:
        pass
    return str(raw)


def _extraction_confidence(p: Patent):
    """How reliably this row's fields were READ from the uploaded document
    (v0.4.1 — previously this column held the SEP confidence, now exported as
    CPARAS_Overall_Confidence). Mean of the per-row extraction-quality
    signals stored in inferred_fields: field completeness score and the
    share of application/publication/standard values verified against the
    source text. None when neither signal exists for the row."""
    try:
        inf = json.loads(p.inferred_fields) if p.inferred_fields else {}
    except Exception:
        return None
    if not isinstance(inf, dict):
        return None
    signals = []
    comp = (inf.get("completeness") or {}).get("score") if isinstance(inf.get("completeness"), dict) else None
    if isinstance(comp, (int, float)):
        signals.append(float(comp))
    ver = inf.get("extraction_verification")
    if isinstance(ver, dict) and ver.get("any_checked"):
        checked = [v for v in (ver.get("application_number"), ver.get("publication_number")) if isinstance(v, dict)]
        checked += [v for v in (ver.get("standards") or []) if isinstance(v, dict)]
        if checked:
            signals.append(sum(1 for v in checked if v.get("verified")) / len(checked))
    return round(sum(signals) / len(signals), 4) if signals else None


def _json_list_text(raw) -> str | None:
    """'["a", "b"]' -> 'a; b'. None for empty/missing/unreadable."""
    if not raw:
        return None
    try:
        v = json.loads(raw)
    except Exception:
        return str(raw)
    if isinstance(v, list):
        return "; ".join(str(x) for x in v if x) or None
    return str(v) or None


def _row_values(p: Patent) -> dict:
    """Every export column (SOURCE_COLUMNS + CPARAS_COLUMNS) for one persisted Patent row. Values are
    read from stored columns only; a row without an analysis result gets blank cells (None), never a
    fake 0.
    """
    has_result = p.predicted_sep_status is not None
    return {
        "Disclosure_No": p.disclosure_number,
        "Application_Number": p.application_number,
        "Publication_Number": p.publication_number,
        "Declared_Standards": _standards_text(p.standards),
        "Project": p.project,
        "Country": p.country,
        "CPARAS_Patent_Label": p.application_number or p.publication_number or f"Disclosure #{p.disclosure_number}",
        "CPARAS_Applicant": p.applicant,
        "CPARAS_Title": p.title,
        "CPARAS_Predicted_SEP_Status": p.predicted_sep_status or "FAILED",
        "CPARAS_Essentiality": essentiality_from_status(p.predicted_sep_status) if has_result else None,
        "CPARAS_Overall_Confidence": p.research_confidence if has_result else None,
        # Read verbatim from the already-persisted, already-scored columns.
        # Never recomputed here, and left blank (None -> a blank Excel
        # cell) rather than 0 when a row genuinely has no result, per the
        # same "no zero as a fake fallback" requirement as CPARAS Bulk's export.
        "CPARAS_Claim_Match": p.claim_match if has_result else None,
        "CPARAS_Semantic_Similarity": p.standard_similarity if has_result else None,
        "CPARAS_Standard_Coverage": p.standard_coverage if has_result else None,
        "CPARAS_Extraction_Confidence": _extraction_confidence(p),
        "CPARAS_Recommended_Standard": p.recommended_standard,
        "CPARAS_Recommended_Version": p.recommended_version,
        "CPARAS_Standard_Clause": p.standard_clause,
        "CPARAS_Matched_Phrases": _json_list_text(getattr(p, "matched_phrases", None)),
        "CPARAS_Matched_Terms": _json_list_text(getattr(p, "matched_terms", None)),
        "CPARAS_Verification_Status": p.verification_status,
        "CPARAS_Evidence_Source": p.evidence_source,
    }


def build_cparas_workbook(declaration: Declaration, patents: list[Patent]) -> bytes:
    """Builds the CPARAS-enriched export workbook for one declaration's
    already-persisted, already-scored Patent rows.

    Memory-conscious at CPARAS's actual scale: a single declaration's
    disclosure count is tens to low hundreds of patents (one filing), not
    the 17,000-row CPARAS Bulk bulk-import scale, so writing row-by-row via
    openpyxl's normal API (as here) is appropriate — the large-dataset
    batching/streaming concerns that drove etsi_bulk/scoring.py's redesign
    don't apply to this per-declaration export.

    A single malformed patent row cannot abort the whole export — see the
    try/except per row below, mirroring CPARAS Bulk's `_error_gitas_row`.
    """
    wb = Workbook()
    ws = wb.active
    ws.title = "CPARAS"

    for j, name in enumerate(ALL_COLUMNS, start=1):
        cell = ws.cell(row=1, column=j, value=name)
        cell.fill = _HEADER_FILL
        cell.font = _HEADER_FONT
    ws.freeze_panes = "A2"

    for i, p in enumerate(patents, start=2):
        try:
            values = _row_values(p)
        except Exception as e:
            logger.warning(f"CPARAS export: failed to derive export fields for patent id={getattr(p, 'id', '?')}: {e}")
            values = {c: None for c in ALL_COLUMNS}
            values["Disclosure_No"] = getattr(p, "disclosure_number", None)
            values["CPARAS_Predicted_SEP_Status"] = "EXPORT_ERROR"
        for j, name in enumerate(ALL_COLUMNS, start=1):
            cell = ws.cell(row=i, column=j)
            xlsx_set(cell, values.get(name))
            if name in _NUMERIC_COLUMNS and isinstance(values.get(name), (int, float)):
                cell.number_format = "0.0000"

    for j, name in enumerate(ALL_COLUMNS, start=1):
        ws.column_dimensions[get_column_letter(j)].width = (
            60 if name in ("CPARAS_Matched_Phrases", "CPARAS_Matched_Terms") else max(14, min(40, len(name) + 2)))

    buf = io.BytesIO()
    wb.save(buf)
    xlsx_bytes = buf.getvalue()

    _validate_cparas_workbook(xlsx_bytes, len(patents))
    return xlsx_bytes


def _validate_cparas_workbook(xlsx_bytes: bytes, expected_data_rows: int) -> None:
    """Reopen the exact bytes about to be returned and confirm the
    mandatory CPARAS_* columns are present, mirroring
    `etsi_bulk.excel_export._validate_generated_workbook`. Raises
    `CparasExportValidationError` rather than returning a workbook that's
    silently missing a mandatory column."""
    try:
        wb = load_workbook(io.BytesIO(xlsx_bytes), data_only=True, read_only=True)
    except Exception as e:
        raise CparasExportValidationError(f"generated workbook could not be reopened by openpyxl: {e}") from e
    try:
        if "CPARAS" not in wb.sheetnames:
            raise CparasExportValidationError(f"generated workbook is missing the 'CPARAS' sheet (present: {', '.join(wb.sheetnames)})")
        ws = wb["CPARAS"]
        try:
            headers = [c.value for c in next(ws.iter_rows(min_row=1, max_row=1))]
        except StopIteration:
            raise CparasExportValidationError("generated CPARAS sheet has no header row")
        mandatory = ["CPARAS_Claim_Match", "CPARAS_Semantic_Similarity", "CPARAS_Standard_Coverage"]
        missing = [c for c in mandatory if c not in headers]
        if missing:
            raise CparasExportValidationError(f"generated CPARAS sheet is missing mandatory column(s): {', '.join(missing)}")
        data_row_count = sum(
            1 for row in ws.iter_rows(min_row=2, values_only=True)
            if any(v not in (None, "") for v in row)
        )
        if data_row_count != expected_data_rows:
            raise CparasExportValidationError(
                f"generated CPARAS sheet has {data_row_count} data row(s), expected {expected_data_rows}"
            )
    finally:
        wb.close()


def export_filename(declaration: Declaration) -> str:
    """Download name for a declaration's export: '<original name>_CPARAS_ISLD_enriched.xlsx'."""
    base = (declaration.filename or "declaration").rsplit(".", 1)[0]
    return f"{base}_CPARAS_ISLD_enriched.xlsx"
