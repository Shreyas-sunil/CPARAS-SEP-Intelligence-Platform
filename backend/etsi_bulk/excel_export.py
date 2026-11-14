"""
ETSI Bulk Declaration Export — CPARAS Bulk Enriched Excel Export
===================================================================
Turns an already-scored `SepBulkRun` (see etsi_bulk/scoring.py, persisted
via api/routes/gitas/dashboard.py's /upload handler) into a downloadable
"<source>_CPARAS_Bulk_enriched.xlsx" workbook.

SOURCE-ROW PRESERVATION: the output keeps every original source row and
column, in the original order, and ADDS the CPARAS_Bulk_* enrichment
columns to each row. One stored record exists per source row (see
etsi_bulk/parser.py `_source_row_index`), and `_align_rows` writes each
record's enrichment onto exactly the row it came from -- no row is ever
dropped, merged or reordered because it shares an identifier with
another row.

This module is STRICTLY DOWNSTREAM of scoring:

    source .xlsx --> parse (parser.py) --> score (scoring.py) --> DB
                                                                     |
                                                                     v
                                                          *this module*
                                                                     |
                                                                     v
                                                        GITAS-enriched .xlsx

It never re-parses the declared standard, never re-embeds anything, never
calls `score_records()` or any research/ primitive, and never recomputes
essentiality/confidence/semantic-similarity/claim-match/standard-coverage.
Every GITAS_* export column is derived either directly from an already-
persisted `SepBulkPatent` column, or from a simple, deterministic,
label-only transformation of one or more already-persisted columns (e.g.
turning the existing `declared_family_match` boolean into a human-readable
reason code). No new thresholds are introduced anywhere in this file that
influence essentiality/status classification — the classification itself
(`essentiality`) is read verbatim from the DB.

Three source-fidelity tiers, tried in order (see `build_enriched_workbook`):

  1. Preferred: `SepBulkRun.original_file_blob` holds the verbatim bytes of
     the uploaded workbook (populated by the /upload handler) and reopens
     cleanly as .xlsx. We open that workbook as-is and only ADD the
     GITAS_* columns to its data sheet and a new "GITAS Summary" sheet —
     so every original column, its order, its cell types/formulas, and
     any existing "Summary" sheet are preserved apart from the additive
     GITAS columns and some header/freeze/filter formatting.

  2. Fallback: the blob exists but isn't an .xlsx openpyxl can reopen
     (a .xls or .csv upload, or a container openpyxl rejects). Re-parses
     the raw bytes with the exact same multi-sheet reader + header
     detection `etsi_bulk/parser.py` uses at upload time, and rebuilds
     Sheet1 with full original column/value fidelity (just without the
     source workbook's own formatting/formulas, since none exists to
     carry over for .xls/.csv).

  3. Last resort: no blob at all (older runs uploaded before this feature
     existed), or the bytes can't be read in any supported format.
     Reconstructs Sheet1 from the subset of original columns that are
     actually persisted per-row (see `_RECONSTRUCT_COLUMNS` below) — a
     handful of raw-only source columns that were never persisted (e.g.
     Reflected_Date, the LICD_*/DECL_* flag columns) come back empty.

Every tier is wrapped so a failure downgrades to the next tier (or, for
the GITAS Summary sheet specifically, to a minimal placeholder) rather
than raising — see the docstring on `build_enriched_workbook` — and a
single malformed patent row cannot abort the whole export (its GITAS_*
columns come back as an explicit FAILED/INVALID marker instead).
"""

from __future__ import annotations

import io
import json
import logging
import re
import statistics
from collections import Counter
from datetime import datetime
from typing import Optional

from openpyxl import Workbook, load_workbook
from utils.export_safety import xlsx_set
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from core.database import SepBulkPatent, SepBulkRun
from etsi_bulk.schema import COLUMN_MAP, resolve_header
from etsi_bulk.parser import (
    _load_any, looks_like_bulk_header, _find_header_row, _HEADER_SCAN_ROWS,
)
# Read-only reference-data helpers (pure stdlib module, no embedding/ML
# involved — see research/standards_corpus.py's own imports) reused here
# for the SAME purpose scoring.py already uses them: extracting a spec
# family number from free text, and looking up which technical domain a
# family belongs to / whether the corpus has any entries for it at all.
# This module calls them read-only, exactly like it already does for
# `matched_standard` via `_FAMILY_RE`/`_VERSION_RE` below — it does not
# call the embedder, does not re-score, and does not touch scoring.py.
from research.standards_corpus import family_of
# v0.4.4: corpus lookups follow the corpus Bulk actually scored against (dataset or illustrative)
from research.bulk_corpus import domain_of_family, entries_by_family, all_families, get_bulk_corpus
# The platform's own similarity bands (Confirmed/Likely/Potential), used
# by scoring.py's essentiality determination — imported, not redefined,
# so "sufficient evidence" here means the same thing it means everywhere
# else in the app rather than a second, drifting definition of the bar.
from research.engine import SIM_LIKELY
from research.three_signal_scoring import HIGH_CONFIDENCE, ESSENTIAL_MIN_CONFIDENCE

# Key source fields whose presence makes up CPARAS_Bulk_Extraction_Confidence.
# A bulk export is already structured, so there is no extraction model whose
# own confidence could be reported -- what CAN go wrong is a row arriving
# with identifying/standard fields missing or unparseable.
_EXTRACTION_FIELDS = (
    ("patent number", ("application_number", "publication_number")),
    ("title", ("title",)),
    ("declared standard", ("standard_raw",)),
    ("spec number", ("spec_number",)),
    ("spec version", ("spec_version",)),
    ("company", ("company",)),
    ("illustrative part", ("illustrative_part",)),
)


_CLASSIFIED_RE = re.compile(r"\bClassified\s+(Essential|Potential|Non-Essential)\b", re.IGNORECASE)
_ONE_WORD = {"essential": "Essential", "potential": "Potential", "non-essential": "Non-Essential"}


def classification_word(validation_notes: Optional[str], essentiality: Optional[str]) -> Optional[str]:
    """One word -- Essential / Potential / Non-Essential -- taken from the
    validation note ("Classified <X> ..."), else from the stored essentiality.
    None for a row with no analysis result."""
    m = _CLASSIFIED_RE.search(validation_notes or "")
    if m:
        return _ONE_WORD[m.group(1).lower()]
    if essentiality:
        return _ONE_WORD.get(essentiality.strip().lower())
    return None


def extraction_confidence_for_patent(p: SepBulkPatent) -> float:
    """Share (0-1) of the key source fields that are present for this row."""
    present = sum(
        1 for _, attrs in _EXTRACTION_FIELDS
        if any(str(getattr(p, a, None) or "").strip() for a in attrs)
    )
    return round(present / len(_EXTRACTION_FIELDS), 4)

logger = logging.getLogger(__name__)

# Hard ceiling on how many original data rows this module will read/write in
# one export. Mirrors etsi_bulk/parser.py's own MAX_ROWS — a pathological or
# corrupted workbook shouldn't be able to turn an export request into an
# unbounded memory/time spike. In practice a run's persisted patent count is
# already bounded by whatever the parser accepted at upload time; this is a
# second, independent ceiling applied while re-reading the stored original
# bytes for column-fidelity purposes. Imported (not duplicated as a literal)
# so raising the ingestion-side ceiling for large/extra-large datasets can't
# silently leave this one stale and truncate an export the parser itself
# would have accepted in full.
from etsi_bulk.parser import MAX_ROWS as MAX_EXPORT_DATA_ROWS

# ── CPARAS Bulk export column set ────────────────────────────────────────────
# User-facing product name is "CPARAS Bulk" (formerly "GITAS"), so the
# appended enrichment columns, summary sheet and download filename carry
# that name. Internal Python identifiers (GITAS_COLUMNS, gitas_fields_for_
# patent, ...) keep their historical names to avoid churn.
SUMMARY_SHEET_NAME = "CPARAS Bulk Summary"

GITAS_COLUMNS: list[str] = [
    "CPARAS_Bulk_Status",
    # v0.4.1: the SEP determination itself, as two explicit columns.
    #   CPARAS_Bulk_Essentiality       -> Essential / Potential / Non-Essential
    #   CPARAS_Bulk_Overall_Confidence -> 0-1 confidence that the row IS an
    #       SEP of the matched standard (the "Overall Confidence" shown in
    #       the UI). The label is derived from this number (see
    #       research.three_signal_scoring.essentiality_from_confidence), so
    #       the two columns can never disagree.
    # CPARAS_Bulk_Status remains the PROCESSING status (SUCCESS/FAILED).
    "CPARAS_Bulk_Essentiality",
    # v0.4.9: how essential this patent is, as a percentage (e.g. 63%) --
    # the overall SEP confidence shown as a % so it reads at a glance.
    # Same value for every label: 0.63 -> 63% whether the row is Essential,
    # Potential or Non-Essential.
    "CPARAS_Bulk_Essentiality_Confidence",
    "CPARAS_Bulk_Overall_Confidence",
    "CPARAS_Bulk_Evidence_Level",
    "CPARAS_Bulk_Evidence_Label",
    "CPARAS_Bulk_Mapping_Type",
    # v0.5 fix: Claim_Match and Standard_Coverage were missing from this
    # export contract even though etsi_bulk/scoring.py already computes
    # (and the DB already persists) p.claim_match / p.standard_coverage --
    # see the v0.5 engineering report, "GITAS FIX". These three are the
    # exact fields shown in the UI's "Score Breakdown" panel and are
    # mandatory per that report's acceptance criteria; DO NOT remove or
    # rename them.
    "CPARAS_Bulk_Claim_Match",
    "CPARAS_Bulk_Semantic_Similarity",
    "CPARAS_Bulk_Standard_Coverage",
    # v0.4.1: now genuinely an extraction/input-quality measure (share of
    # the row's key identifying fields present in the source) -- it used
    # to hold the SEP confidence, which now lives in
    # CPARAS_Bulk_Overall_Confidence above.
    "CPARAS_Bulk_Extraction_Confidence",
    "CPARAS_Bulk_Standard_Family",
    "CPARAS_Bulk_Standard_Version",
    "CPARAS_Bulk_Standard_Release",
    "CPARAS_Bulk_Recommended_Clause",
    "CPARAS_Bulk_Claim_1",
    "CPARAS_Bulk_Claim_Extraction_Status",
    "CPARAS_Bulk_Corpus_Coverage",
    "CPARAS_Bulk_Declaration_Conflict",
    "CPARAS_Bulk_Version_Inconsistency",
    "CPARAS_Bulk_Reason_Codes",
    "CPARAS_Bulk_Validation_Status",
    "CPARAS_Bulk_Validation_Notes",
    # v0.4.1: one-word classification extracted from the Validation Notes
    # column next to it ("Classified Potential — ..." -> Potential,
    # "Classified Essential but ..." -> Essential). Rows whose note is blank
    # or doesn't name a class fall back to the row's stored essentiality, so
    # every analysed row gets exactly one of: Essential / Potential /
    # Non-Essential.
    "CPARAS_Bulk_Classification",
    "CPARAS_Bulk_Report_Link",
]

_HEADER_FILL = PatternFill(start_color="FF1E3A5F", end_color="FF1E3A5F", fill_type="solid")
_GITAS_HEADER_FILL = PatternFill(start_color="FF0F2A44", end_color="FF0F2A44", fill_type="solid")
_HEADER_FONT = Font(color="FFFFFFFF", bold=True)
_FAMILY_RE = re.compile(r"(\d{1,3}\.\d{1,3})")
_VERSION_RE = re.compile(r"(v?\d{1,2}\.\d{1,2}\.\d{1,2})", re.IGNORECASE)


class ExcelExportValidationError(RuntimeError):
    """Raised when the just-generated workbook fails post-generation
    structural validation (see `_validate_generated_workbook`). Signals a
    bug in the exporter's own assembly logic — not the scoring engine, and
    not a problem with the source data — so a caller gets a clear, well-
    defined failure instead of a corrupted or silently incomplete download.
    """


def _validate_generated_workbook(xlsx_bytes: bytes, expected_data_rows: int) -> None:
    """
    Final safety net: reopen the exact bytes about to be returned and
    confirm they form a complete, well-formed GITAS-enriched workbook
    before handing them back to the caller.

    Checks, in order:
      1. openpyxl can reopen the bytes at all (structurally valid .xlsx).
      2. 'Sheet1' and 'GITAS Summary' are both present.
      3. Sheet1's header contains every one of the canonical GITAS_*
         columns (see GITAS_COLUMNS) -- including the three mandatory
         score-breakdown fields (GITAS_Claim_Match, GITAS_Semantic_
         Similarity, GITAS_Standard_Coverage).
      4. Sheet1's header has no duplicate (non-blank) column names.
      5. Sheet1 has exactly `expected_data_rows` non-blank data rows.

    Raises `ExcelExportValidationError` on the first failing check — never
    attempts to repair, truncate, or otherwise silently patch the output.
    This function only reads; it never mutates `xlsx_bytes` or re-derives
    any GITAS_* value, so it cannot itself alter export results.
    """
    try:
        wb = load_workbook(io.BytesIO(xlsx_bytes), data_only=True, read_only=True)
    except Exception as e:
        raise ExcelExportValidationError(f"generated workbook could not be reopened by openpyxl: {e}") from e

    try:
        missing_sheets = [s for s in ("Sheet1", SUMMARY_SHEET_NAME) if s not in wb.sheetnames]
        if missing_sheets:
            raise ExcelExportValidationError(
                f"generated workbook is missing required sheet(s): {', '.join(missing_sheets)} "
                f"(present: {', '.join(wb.sheetnames)})"
            )

        ws = wb["Sheet1"]
        # The header is not always row 1: an original workbook may carry a
        # title block above it, which the export preserves verbatim (see
        # _resolve_data_sheet_name). Find the row that actually carries the
        # CPARAS_Bulk_* columns rather than assuming the first one, so the
        # validator checks the real header and counts the real data rows.
        scan = [[c.value for c in row]
                for row in ws.iter_rows(min_row=1, max_row=_HEADER_SCAN_ROWS)]
        header_index = next(
            (i for i, row in enumerate(scan)
             if GITAS_COLUMNS[0] in [v for v in row]),
            None,
        )
        if header_index is None:
            if not scan:
                raise ExcelExportValidationError("generated Sheet1 has no header row")
            header_index = 0
        headers = scan[header_index]
        header_row_number = header_index + 1

        missing_gitas_cols = [c for c in GITAS_COLUMNS if c not in headers]
        if missing_gitas_cols:
            raise ExcelExportValidationError(
                f"generated Sheet1 is missing CPARAS_Bulk_* column(s): {', '.join(missing_gitas_cols)}"
            )

        non_blank_headers = [h for h in headers if h not in (None, "")]
        dupes = sorted({h for h in non_blank_headers if non_blank_headers.count(h) > 1})
        if dupes:
            raise ExcelExportValidationError(
                f"generated Sheet1 has duplicate column name(s): {', '.join(dupes)}"
            )

        data_row_count = sum(
            1 for row in ws.iter_rows(min_row=header_row_number + 1, values_only=True)
            if any(v not in (None, "") for v in row)
        )
        if data_row_count != expected_data_rows:
            raise ExcelExportValidationError(
                f"generated Sheet1 has {data_row_count} data row(s), expected {expected_data_rows}"
            )
    finally:
        wb.close()


# ── Per-row GITAS_* field derivation (export-time labeling only) ────────────

# Every entry in the illustrative offline standards corpus is a Rel-8-or-
# later LTE/5G-NR spec (see research/standards_corpus.py — there is no 2G
# or 3G content in it at all). A row DECLARED purely against a pre-4G
# standard therefore cannot be genuinely confirmed OR meaningfully
# contradicted by corpus content that doesn't cover its era — but a
# specific-looking topical match to some unrelated LTE/5G clause is also
# not something that should read as a confident, validated result. See
# `_conflict_materiality` below for how this is used.
_LEGACY_TECHNOLOGIES = {"2G / GSM", "3G / UMTS"}


def _declared_family_of(p: SepBulkPatent) -> Optional[str]:
    """The standard family this row's declarant DECLARED (not the one the
    scorer matched). Same derivation etsi_bulk/scoring.py uses."""
    return family_of(p.standard_raw or "") or (
        str(getattr(p, "spec_number", None)) if getattr(p, "spec_number", None) else None
    )


def _corpus_families() -> frozenset:
    """Families present in the offline reference corpus. Static reference
    data, so computed once rather than per row of a 17,000-row export."""
    global _CORPUS_FAMILIES_CACHE
    corpus = get_bulk_corpus()
    key = (corpus.source, corpus.fingerprint, len(corpus.entries))
    if _CORPUS_FAMILIES_CACHE is None or _CORPUS_FAMILIES_CACHE[0] != key:
        _CORPUS_FAMILIES_CACHE = (key, frozenset(corpus.all_families()))
    return _CORPUS_FAMILIES_CACHE[1]


_CORPUS_FAMILIES_CACHE = None


def _matched_is_legacy(p) -> bool:
    """True when the family CPARAS matched belongs to a pre-4G series (3G 25.xxx or the 2G/GSM 4x-5x
    series). With the illustrative corpus (LTE/5G only) this is never true, so behaviour there is
    unchanged; with the 3GPP dataset corpus a 2G/3G declaration can legitimately match a 2G/3G clause."""
    fam = family_of(getattr(p, "matched_standard", None) or "")
    if not fam:
        return False
    ser = fam[:2]
    return ser == "25" or (ser.isdigit() and 41 <= int(ser) <= 55)


def _conflict_materiality(p: SepBulkPatent, declaration_conflict: bool) -> bool:
    """
    Whether a `declaration_conflict` (matched entry isn't the declared
    family) is MATERIAL — i.e. actually checkable and contradicted —
    versus merely reflecting that this tiny illustrative corpus (11
    families total) has no entry for the declared family at all, so a
    best-effort topical neighbor was used instead. The latter is an
    inherent limitation of a small reference corpus, not a wrong answer,
    and treating every such row as equally suspect as a genuine
    contradiction would flag the large majority of rows in any
    real-world bulk export (this corpus covers a handful of 3GPP
    families; a real declaration set spans the whole catalog) — exactly
    the "everything becomes NEEDS_REVIEW" failure mode to avoid.

    A conflict is material when any of these hold:
      - the declared family IS represented in the corpus (so there was
        real reference material to confirm against, and the scorer
        picked a different family anyway — this is what the reported
        bug's example actually demonstrates the *type* of problem to
        catch, even though that specific example's declared family
        happens not to be in-corpus; see the generation check below for
        that case specifically);
      - the declared family's corpus domain and the matched entry's
        domain differ (e.g. declared Security, matched RRC) — a
        same-family-unknown-but-clearly-different-topic situation;
      - the row was declared purely against a pre-4G standard (2G/3G) —
        the corpus is 100% LTE/5G-era, so ANY topical match for such a
        row is, by construction, a cross-generation mapping. A GSM-only
        disclosure cannot be essential to a 5G NR or LTE procedure; this
        directly targets the reported example (a TS 48.008 — GSM BSS
        interface — declaration mapped to TS 33.501 5G Security).
    """
    if not declaration_conflict:
        return False
    declared_family = _declared_family_of(p)
    declared_family_in_corpus = bool(declared_family) and declared_family in _corpus_families()
    declared_domain = domain_of_family(declared_family) if declared_family else None
    matched_domain = getattr(p, "matched_domain", None)
    domain_conflict = bool(declared_domain) and bool(matched_domain) and declared_domain != matched_domain
    generation_conflict = getattr(p, "technology", None) in _LEGACY_TECHNOLOGIES and not _matched_is_legacy(p)
    return declared_family_in_corpus or domain_conflict or generation_conflict


def gitas_fields_for_patent(p: SepBulkPatent, run_id: int) -> dict:
    """
    Compute the CPARAS_Bulk_* export column values (see GITAS_COLUMNS) for one already-scored
    `SepBulkPatent` row.

    Reads only fields already written by `etsi_bulk/scoring.py` at analysis
    time (essentiality, confidence, claim_match, semantic_similarity,
    standard_coverage, matched_standard/clause/clause_title/domain,
    declared_family_match, legal_status, release, illustrative_part).
    Performs no embedding, no re-scoring — the corpus family/domain lookups
    in `_conflict_materiality` above are static reference-data reads, the
    same kind of read `matched_standard`'s own family/version regex parse
    below already does, not a re-run of any classification.

    VALIDATION STATUS — evidence-based tiers, not a single essentiality
    passthrough:
      VALID — has a result; for "Essential" rows, no material declaration
        conflict and no version inconsistency (a "Non-Essential" result
        needs neither, since it isn't making a specific standard-mapping
        claim in the first place).
      NEEDS_REVIEW — "Potential" classification (unchanged from before);
        or an "Essential" row with a material conflict backed by
        confident evidence (declared-family confirmable in corpus, or a
        checkable domain/generation contradiction, but similarity/
        confidence is still high enough that a human might reasonably
        confirm it — e.g. the declarant mis-typed their own standard);
        or a version inconsistency (same family, diverging release); or
        a non-material topical match whose similarity is still below the
        platform's own "Likely" bar.
      INVALID — no scored result at all; or an "Essential" row with a
        material conflict AND overall confidence below HIGH_CONFIDENCE
        (research.three_signal_scoring) — i.e. a specific standard-mapping claim that is
        both checkably contradicted and not well-evidenced enough to
        overrule that contradiction.
    """
    has_result = p.essentiality is not None
    status = "SUCCESS" if has_result else "FAILED"

    declared_match = bool(p.declared_family_match)
    has_match = bool(p.matched_standard)

    # Evidence tier: a plain, order-preserving label over the *existing*
    # essentiality/declared_family_match fields — it does not change which
    # tier a row falls into (that was decided by scoring.py), it only gives
    # the tier a numeric level + label for the spreadsheet.
    conf_value = p.confidence if isinstance(p.confidence, (int, float)) else 0.0
    if not has_result:
        evidence_level, evidence_label = 0, "Not analyzed"
    elif declared_match and p.essentiality == "Essential":
        evidence_level, evidence_label = 3, "Declared family confirmed"
    elif p.essentiality == "Potential" and conf_value >= ESSENTIAL_MIN_CONFIDENCE:
        # Scored as strongly as an "Essential" row, but against a family
        # other than the one declared -- so the evidence is topical, not a
        # confirmation of the declaration (see etsi_bulk/scoring.py's
        # declared-family evidence gate). Level 2 is unchanged in meaning;
        # it is simply no longer labelled "Essential" in the column next
        # to it, which is what made CPARAS_Bulk_Essentiality and
        # CPARAS_Bulk_Evidence_Level disagree.
        evidence_level, evidence_label = 2, "Strong topical match (declared family not confirmed)"
    elif p.essentiality == "Potential":
        evidence_level, evidence_label = 1, "Weak topical overlap"
    else:
        evidence_level, evidence_label = 0, "No meaningful mapping"

    if not has_result or not has_match:
        mapping_type = "no_match"
    elif declared_match:
        mapping_type = "declared_family_match"
    elif p.essentiality in ("Essential", "Potential"):
        mapping_type = "topical_similarity"
    else:
        mapping_type = "no_match"

    family = version = None
    if p.matched_standard:
        fm = _FAMILY_RE.search(p.matched_standard)
        vm = _VERSION_RE.search(p.matched_standard)
        family = fm.group(1) if fm else None
        version = vm.group(1) if vm else None

    if p.matched_clause and p.matched_clause_title:
        recommended_clause = f"{p.matched_clause}: {p.matched_clause_title}"
    else:
        recommended_clause = p.matched_clause or p.matched_clause_title or None

    # Same field the existing /patents/{id} detail endpoint already uses as
    # "patent_claim_simplified" (see api/routes/sep.py claim_to_standard_mapping).
    claim_text = p.illustrative_part or p.title or None
    claim_status = "AVAILABLE" if claim_text else "NOT_AVAILABLE"

    # CORPUS COVERAGE is "does the reference corpus contain the standard
    # family this row DECLARED?" -- an independent property of the corpus,
    # not of how the match turned out. It used to be set to
    # `declared_match`, which conflated two different situations that need
    # different handling:
    #   * the declared family IS in the corpus but the scorer matched a
    #     different one  -> a real disagreement, already reported as
    #     DECLARATION_CONFLICT; coverage is fine.
    #   * the declared family is NOT in the corpus at all -> there is no
    #     reference text to assess this row against, so NO confident
    #     verdict is possible for it, however the argmax came out.
    # Reporting the second case as merely "outside corpus coverage", with
    # the same code as the first, hid the one condition a reviewer most
    # needs to see.
    declared_family = _declared_family_of(p)
    declared_family_in_corpus = bool(declared_family) and declared_family in _corpus_families()
    corpus_coverage = declared_family_in_corpus
    declaration_conflict = has_result and bool(p.standard_raw) and not declared_match
    material_conflict = _conflict_materiality(p, declaration_conflict)

    version_inconsistency = False
    if declared_match and p.spec_version and version:
        declared_v = str(p.spec_version).lstrip("vV")
        matched_v = version.lstrip("vV")
        version_inconsistency = declared_v.split(".")[:1] != matched_v.split(".")[:1]

    reason_codes = []
    if not has_result:
        reason_codes.append("ANALYSIS_FAILED")
    if has_result and not declared_family_in_corpus:
        reason_codes.append("DECLARED_FAMILY_NOT_IN_CORPUS")
    if has_result and declared_family_in_corpus and not declared_match:
        reason_codes.append("OUTSIDE_CORPUS_COVERAGE")
    if declaration_conflict:
        reason_codes.append("DECLARATION_CONFLICT" if material_conflict else "DECLARATION_UNCONFIRMED")
    if version_inconsistency:
        reason_codes.append("VERSION_MISMATCH")
    legal = (p.legal_status or "").strip().lower()
    if legal in ("expired", "expired - fee related", "expired - lifetime", "ceased"):
        reason_codes.append("LEGAL_STATUS_LAPSED")
    if not reason_codes and has_result:
        reason_codes.append("OK")

    confidence = p.confidence if isinstance(p.confidence, (int, float)) else 0.0
    similarity = p.semantic_similarity if isinstance(p.semantic_similarity, (int, float)) else 0.0

    if not has_result:
        validation_status = "INVALID"
        validation_notes = "No scored analysis result is available for this row."
    elif p.essentiality == "Potential":
        validation_status = "NEEDS_REVIEW"
        validation_notes = "Classified Potential — recommended for manual essentiality review."
    elif p.essentiality != "Essential":
        # Non-Essential: the determination itself is "no confident SEP
        # mapping found" — it is not asserting the declared family, so a
        # mismatch between the declared family and whichever corpus entry
        # happened to score highest isn't a claim this row is making, and
        # doesn't bear on whether the "not essential" conclusion is valid.
        validation_status = "VALID"
        validation_notes = None
    elif material_conflict and confidence < HIGH_CONFIDENCE:
        validation_status = "INVALID"
        validation_notes = (
            "Classified Essential and mapped to a different, checkably-inconsistent "
            "standard family than declared (" + (p.standard_raw or "declared standard") +
            f" -> {p.matched_standard or 'matched entry'}), without high enough confidence "
            f"({confidence:.2f} < {HIGH_CONFIDENCE:.2f}) to support overriding that conflict."
        )
    elif material_conflict:
        validation_status = "NEEDS_REVIEW"
        validation_notes = (
            "Classified Essential but mapped to a different standard family than declared "
            f"(declared {p.standard_raw or '?'}, matched {p.matched_standard or '?'}); confidence "
            f"is high enough ({confidence:.2f}) to warrant human confirmation rather than rejection."
        )
    elif version_inconsistency:
        validation_status = "NEEDS_REVIEW"
        validation_notes = (
            "Declared family confirmed but declared version "
            f"({p.spec_version or '?'}) and matched version ({version or '?'}) diverge — "
            "recommended for manual review before relying on the matched clause."
        )
    elif declaration_conflict and similarity < SIM_LIKELY:
        validation_status = "NEEDS_REVIEW"
        validation_notes = (
            f"Topical match only (similarity {similarity:.2f}), with no declared-family "
            "confirmation available in the reference corpus for this declared standard."
        )
    else:
        validation_status = "VALID"
        validation_notes = None

    if has_result and "LEGAL_STATUS_LAPSED" in reason_codes:
        lapsed_note = (f"Legal status '{p.legal_status}' — patent is no longer in force; the "
                       "essentiality classification is technical only.")
        validation_notes = f"{validation_notes} {lapsed_note}" if validation_notes else lapsed_note

    overall_conf = p.confidence if has_result and isinstance(p.confidence, (int, float)) else None
    # Read verbatim from the DB, exactly as the UI shows it. Runs scored
    # with v0.4.1+ always have a label that agrees with the confidence
    # (scoring derives one from the other); runs scored earlier carry the
    # old saturated similarity/confidence and must be re-uploaded/re-scored.
    essentiality_label = p.essentiality if has_result else None

    return {
        "CPARAS_Bulk_Status": status,
        "CPARAS_Bulk_Essentiality": essentiality_label,
        "CPARAS_Bulk_Essentiality_Confidence": overall_conf,   # formatted as a percentage in the sheet
        "CPARAS_Bulk_Overall_Confidence": overall_conf,
        "CPARAS_Bulk_Evidence_Level": evidence_level,
        "CPARAS_Bulk_Evidence_Label": evidence_label,
        "CPARAS_Bulk_Mapping_Type": mapping_type,
        # Read verbatim from the already-persisted, already-scored columns
        # (etsi_bulk/scoring.py) -- never recomputed here, and left as
        # Python None (-> a blank Excel cell) rather than 0 when a row
        # genuinely has no result (has_result == False, i.e. row_status ==
        # "FAILED"), per the "no zero as a fake fallback" requirement.
        "CPARAS_Bulk_Claim_Match": getattr(p, "claim_match", None) if has_result else None,
        "CPARAS_Bulk_Semantic_Similarity": p.semantic_similarity if has_result else None,
        "CPARAS_Bulk_Standard_Coverage": getattr(p, "standard_coverage", None) if has_result else None,
        "CPARAS_Bulk_Extraction_Confidence": extraction_confidence_for_patent(p),
        "CPARAS_Bulk_Standard_Family": family,
        "CPARAS_Bulk_Standard_Version": version,
        "CPARAS_Bulk_Standard_Release": p.release,
        "CPARAS_Bulk_Recommended_Clause": recommended_clause,
        "CPARAS_Bulk_Claim_1": claim_text,
        "CPARAS_Bulk_Claim_Extraction_Status": claim_status,
        "CPARAS_Bulk_Corpus_Coverage": corpus_coverage,
        "CPARAS_Bulk_Declaration_Conflict": declaration_conflict,
        "CPARAS_Bulk_Version_Inconsistency": version_inconsistency,
        "CPARAS_Bulk_Reason_Codes": ", ".join(reason_codes) if reason_codes else None,
        "CPARAS_Bulk_Validation_Status": validation_status,
        "CPARAS_Bulk_Validation_Notes": validation_notes,
        "CPARAS_Bulk_Classification": classification_word(validation_notes, essentiality_label),
        "CPARAS_Bulk_Report_Link": f"/api/sep/patents/{p.id}",
    }


def _error_gitas_row(p: SepBulkPatent, error: Exception) -> dict:
    """Fallback row used when `gitas_fields_for_patent` raises for a
    particular patent (unexpected/malformed stored data). Keeps the export
    running for every *other* row instead of failing the whole workbook —
    the affected row is clearly marked FAILED/INVALID rather than silently
    dropped or given misleading blank-but-VALID fields."""
    logger.warning(f"CPARAS Bulk export: failed to derive export fields for patent id={getattr(p, 'id', '?')}: {error}")
    return {
        "CPARAS_Bulk_Status": "FAILED",
        "CPARAS_Bulk_Essentiality": None,
        "CPARAS_Bulk_Essentiality_Confidence": None,
        "CPARAS_Bulk_Overall_Confidence": None,
        "CPARAS_Bulk_Evidence_Level": 0,
        "CPARAS_Bulk_Evidence_Label": "Not analyzed",
        "CPARAS_Bulk_Mapping_Type": "no_match",
        "CPARAS_Bulk_Claim_Match": None,
        "CPARAS_Bulk_Semantic_Similarity": None,
        "CPARAS_Bulk_Standard_Coverage": None,
        "CPARAS_Bulk_Extraction_Confidence": None,
        "CPARAS_Bulk_Standard_Family": None,
        "CPARAS_Bulk_Standard_Version": None,
        "CPARAS_Bulk_Standard_Release": None,
        "CPARAS_Bulk_Recommended_Clause": None,
        "CPARAS_Bulk_Claim_1": None,
        "CPARAS_Bulk_Claim_Extraction_Status": "NOT_AVAILABLE",
        "CPARAS_Bulk_Corpus_Coverage": False,
        "CPARAS_Bulk_Declaration_Conflict": False,
        "CPARAS_Bulk_Version_Inconsistency": False,
        "CPARAS_Bulk_Reason_Codes": "EXPORT_ERROR",
        "CPARAS_Bulk_Validation_Status": "INVALID",
        "CPARAS_Bulk_Validation_Notes": f"Export-time error deriving CPARAS Bulk fields for this row: {error}",
        "CPARAS_Bulk_Classification": None,
        "CPARAS_Bulk_Report_Link": f"/api/sep/patents/{getattr(p, 'id', '')}" if getattr(p, "id", None) else None,
    }


# ── Row alignment: original workbook data rows <-> persisted patents ────────

def _is_blank_row(row) -> bool:
    """True for a row whose cells are all empty (such rows never became records)."""
    return row is None or all(c in (None, "") for c in row)


def _align_rows(header: list, data_rows: list[list], patents: list[SepBulkPatent]) -> tuple[list[Optional[SepBulkPatent]], bool]:
    """
    Maps each original data row (in sheet order) to the stored record that
    was produced FROM THAT ROW, so every row's enrichment lands on exactly
    its own source row.

    Primary path: every stored record carries `source_row_index` (its
    0-based position among the sheet's data rows -- see
    etsi_bulk/parser.py), so row i gets the record whose source_row_index
    is i. This is exact, identifier-free, and unaffected by repeated
    application/publication numbers, identical rows, or rows with no
    identifier at all -- each source row has its own record. Fully blank
    rows (which never become records) and any row without a stored record
    get `None` and are left in place, unenriched.

    Legacy path (runs stored before source_row_index existed): replays the
    old parser's skip rules positionally so exports of those older runs
    keep working as they did.

    Returns (aligned, used_positional_fallback).
    """
    if patents and all(getattr(p, "source_row_index", None) is not None for p in patents):
        by_index: dict[int, SepBulkPatent] = {}
        for p in patents:
            # One record per source row by construction; if a legacy DB
            # somehow holds two for the same row, keep the first (lowest id)
            # rather than silently shifting every later row.
            by_index.setdefault(int(p.source_row_index), p)
        return [
            None if _is_blank_row(row) else by_index.get(i)
            for i, row in enumerate(data_rows)
        ], False

    # ── legacy positional alignment (pre-source_row_index runs) ──
    header_norm = [str(h).strip() if h is not None else "" for h in header]
    app_idx = pub_idx = None
    for i, h in enumerate(header_norm):
        field = resolve_header(h)
        if field == "application_number":
            app_idx = i
        elif field == "publication_number":
            pub_idx = i

    used_positional = app_idx is None and pub_idx is None

    aligned: list[Optional[SepBulkPatent]] = []
    p_iter = iter(patents)
    for row in data_rows:
        if _is_blank_row(row):
            aligned.append(None)
            continue
        if used_positional:
            aligned.append(next(p_iter, None))
            continue
        app_val = row[app_idx] if app_idx is not None and app_idx < len(row) else None
        pub_val = row[pub_idx] if pub_idx is not None and pub_idx < len(row) else None
        if not app_val and not pub_val:
            aligned.append(None)
            continue
        aligned.append(next(p_iter, None))
    return aligned, used_positional


# ── Sheet1: append GITAS columns to the original workbook ───────────────────

def _style_data_sheet(ws: Worksheet, header_row: int, n_cols: int, n_rows: int, gitas_start_col: int) -> None:
    """Formatting only — freeze header, autofilter, column widths, header
    styling. Never touches a data cell's value or type."""
    if n_cols < 1:
        return
    for col in range(1, n_cols + 1):
        cell = ws.cell(row=header_row, column=col)
        cell.font = _HEADER_FONT
        cell.fill = _GITAS_HEADER_FILL if col >= gitas_start_col else _HEADER_FILL
        cell.alignment = Alignment(vertical="center", wrap_text=True)
    ws.freeze_panes = ws.cell(row=header_row + 1, column=1).coordinate
    last_col_letter = get_column_letter(n_cols)
    ws.auto_filter.ref = f"A{header_row}:{last_col_letter}{header_row + n_rows}"
    ws.row_dimensions[header_row].height = 30
    for col in range(1, n_cols + 1):
        letter = get_column_letter(col)
        header_val = ws.cell(row=header_row, column=col).value
        width = 16
        if header_val:
            width = min(max(len(str(header_val)) + 2, 12), 42)
        ws.column_dimensions[letter].width = width


def _write_gitas_columns(ws: Worksheet, header_row: int, first_data_row: int, n_orig_cols: int,
                          aligned: list[Optional[SepBulkPatent]], run_id: int) -> int:
    """Writes the CPARAS_Bulk_* headers (GITAS_COLUMNS) starting at column n_orig_cols+1, and
    fills each data row from `gitas_fields_for_patent`. Returns the total
    column count after the GITAS block. A per-row failure while deriving
    fields (unexpected/malformed stored data) is caught and downgraded to a
    FAILED/INVALID marker row rather than aborting the whole export."""
    start_col = n_orig_cols + 1
    for j, name in enumerate(GITAS_COLUMNS):
        ws.cell(row=header_row, column=start_col + j, value=name)

    for i, patent in enumerate(aligned):
        row_idx = first_data_row + i
        if patent is None:
            continue
        try:
            values = gitas_fields_for_patent(patent, run_id)
        except Exception as e:
            values = _error_gitas_row(patent, e)
        for j, name in enumerate(GITAS_COLUMNS):
            try:
                cell = ws.cell(row=row_idx, column=start_col + j)
                xlsx_set(cell, values[name])
            except Exception as e:
                # Defensive only — e.g. an unexpected merged-cell region in
                # this column. New GITAS columns are always beyond the
                # original sheet's used range, so this should not occur in
                # practice, but a single unwritable cell must not sink the
                # rest of the export.
                logger.warning(f"CPARAS Bulk export: could not write cell row={row_idx} col={start_col + j} ({name}): {e}")
                continue
            if name == "CPARAS_Bulk_Report_Link" and values[name]:
                cell.hyperlink = values[name]
                cell.style = "Hyperlink"
            if name == "CPARAS_Bulk_Essentiality_Confidence" and isinstance(values[name], (int, float)):
                cell.number_format = "0%"
            elif name in ("CPARAS_Bulk_Overall_Confidence", "CPARAS_Bulk_Claim_Match", "CPARAS_Bulk_Semantic_Similarity",
                        "CPARAS_Bulk_Standard_Coverage", "CPARAS_Bulk_Extraction_Confidence") and isinstance(values[name], (int, float)):
                cell.number_format = "0.0000"
    return start_col + len(GITAS_COLUMNS) - 1


# ── Fallback tier 1: re-parse the stored raw bytes with full column fidelity ─
# Used when the blob can't be reopened by openpyxl (a .xls/.csv upload, or a
# genuinely different container format) but the bytes themselves are still
# readable. Reuses the exact same multi-sheet reader + header-matching
# `etsi_bulk/parser.py` used at upload time, so this reproduces the *exact*
# original header/values — not just the subset of fields the DB persisted.

def _build_reparsed_sheet1(wb: Workbook, run: SepBulkRun) -> tuple[list, list[list], list[str]] | None:
    """Attempts to reconstruct Sheet1 with full original-column fidelity by
    re-reading `run.original_file_blob` with the same sheet/header detection
    `parse_bulk_workbook` uses. Returns (header, data_rows, warnings) on
    success, or None if the bytes can't be read/matched at all (caller then
    falls back to `_build_reconstructed_sheet1`)."""
    if not run.original_file_blob:
        return None
    try:
        sheets, _truncated = _load_any(run.filename or "upload.xlsx", run.original_file_blob)
    except Exception as e:
        logger.warning(f"CPARAS Bulk export: could not re-parse original bytes for run {run.id}: {e}")
        return None

    picked = None
    for name, rows in sheets:
        if not rows:
            continue
        idx = _find_header_row(rows)
        if idx is not None:
            picked = (name, rows, idx)
            break
    if picked is None:
        return None

    # The header is not necessarily row 1 -- a real export can carry a
    # title/notes block above it, which parse_bulk_workbook now scans past.
    # This export MUST use the same header row, because each record's
    # source_row_index is its position among the rows BELOW that header; a
    # mismatch here would write every row's enrichment onto the wrong row.
    sheet_name, rows, header_idx = picked
    header = rows[header_idx]
    data_rows = rows[header_idx + 1:]
    warnings = [
        "The original uploaded file could not be reopened as an .xlsx workbook "
        "(likely a .xls or .csv upload) — Sheet1 was rebuilt by re-reading the "
        f"stored original bytes (sheet '{sheet_name}') with the same column "
        "detection used at upload time, preserving every original column and "
        "value, but any original cell formatting/formulas could not be carried "
        "over since no workbook object exists for a non-.xlsx source."
    ]
    if len(data_rows) > MAX_EXPORT_DATA_ROWS:
        warnings.append(
            f"Original data has {len(data_rows)} rows, exceeding this export's "
            f"{MAX_EXPORT_DATA_ROWS}-row cap — only the first {MAX_EXPORT_DATA_ROWS} were exported."
        )
        data_rows = data_rows[:MAX_EXPORT_DATA_ROWS]

    ws = wb.active
    ws.title = "Sheet1"
    for j, h in enumerate(header, start=1):
        ws.cell(row=1, column=j, value=h)
    for i, row in enumerate(data_rows, start=2):
        for j, v in enumerate(row, start=1):
            # CSV text must stay text: "=..." in an uploaded CSV must not
            # become a live formula in the exported workbook.
            xlsx_set(ws.cell(row=i, column=j), v)
    return header, data_rows, warnings


# ── Fallback tier 2 (last resort): reconstruct from persisted DB fields only ─

# Original header -> persisted SepBulkPatent attribute, for the subset that
# survives into the DB. Columns with no entry here were never persisted
# (only used transiently during parsing/scoring) and come back blank in the
# fallback path — this is documented in the export's warnings list.
_RECONSTRUCT_COLUMNS: list[tuple[str, Optional[str]]] = [
    ("IPRD_ID", None),
    ("IPRD_REFERENCE", "iprd_reference"),
    ("IPRD_SIGNATURE_DATE", "signature_date"),
    ("Reflected_Date", None),
    ("COMP_LEGAL_NAME", "company"),
    ("DECL_IS_PROP_FLAG", None),
    ("LICD_DEC_PREP_TO_GRANT_FLAG", None),
    ("LICD_REC_CONDI_FLAG", None),
    ("DIPG_ID", None),
    ("DIPG_DISPLAY_NUMBER", "disclosure_number"),
    ("DIPG_EXTERNAL_ID", None),
    ("Standard", "standard_raw"),
    ("Ess_To_Standard", None),
    ("WI_Type", None),
    ("WOIT_ETSI_DELIVERABLE_NUMBER", None),
    ("WOIT_REFERENCE", None),
    ("WOIT_VERSION", None),
    ("3GPP_Type", None),
    ("TGPP_NUMBER", "spec_number"),
    ("TGPV_VERSION", "spec_version"),
    ("Patent_Type", "patent_type"),
    ("PATT_APPLICATION_NUMBER", "application_number"),
    ("PUBL_NUMBER", "publication_number"),
    ("PBPA_TITLEEN", "title"),
    ("PBPA_PRIORITY_NUMBERS", None),
    ("Country_Of_Registration", "country_raw"),
    ("PBPA_APP_DATE", "filing_date"),
    ("ETPR_ID", None),
    ("ETPR_ACRONYM", None),
    ("Ess_To_Project", None),
    ("Illustrative_Part", "illustrative_part"),
    ("DIPG_PATF_ID", "family_id"),
    ("Original_Application_Number", None),
    ("Original_Publication_Number", None),
    ("Explicitely_Disclosed", "explicit_disclosure"),
    ("Normalized_Patent", None),
    ("2G", "gen_2g"),
    ("3G", "gen_3g"),
    ("4G", "gen_4g"),
    ("5G", "gen_5g"),
    ("Legal Status", "legal_status"),
]


def _build_reconstructed_sheet1(wb: Workbook, patents: list[SepBulkPatent]) -> list[str]:
    """Builds Sheet1 from persisted fields only, used when no original
    workbook bytes are available. Returns a list of warning strings."""
    ws = wb.active
    ws.title = "Sheet1"
    headers = [h for h, _ in _RECONSTRUCT_COLUMNS]
    for j, h in enumerate(headers, start=1):
        ws.cell(row=1, column=j, value=h)
    for i, p in enumerate(patents, start=2):
        for j, (_, attr) in enumerate(_RECONSTRUCT_COLUMNS, start=1):
            xlsx_set(ws.cell(row=i, column=j), (getattr(p, attr) if attr else None))
    missing = [h for h, attr in _RECONSTRUCT_COLUMNS if attr is None]
    warnings = [
        "No original workbook bytes were stored for this run (older run, or a "
        ".csv upload) — Sheet1 was reconstructed from stored per-row results. "
        f"The following original columns have no persisted value and are blank: "
        f"{', '.join(missing)}."
    ]
    return warnings


# ── Summary sheet passthrough / GITAS Summary sheet ─────────────────────────

def _write_kv_section(ws: Worksheet, row: int, title: str, rows: list[tuple[str, object]]) -> int:
    """Write a titled two-column label/value section to the summary sheet; returns the next free row
    (one blank row after the section).
    """
    cell = ws.cell(row=row, column=1, value=title)
    cell.font = Font(bold=True, size=12, color="FF1E3A5F")
    row += 1
    for label, value in rows:
        ws.cell(row=row, column=1, value=label)
        ws.cell(row=row, column=2, value=value)
        row += 1
    return row + 1


def _build_gitas_summary_sheet(wb: Workbook, run: SepBulkRun, patents: list[SepBulkPatent],
                                total_input_rows: int, skipped_rows: int) -> None:
    """Add the 'CPARAS Bulk Summary' sheet: run overview, essentiality / evidence-level / mapping-type
    distributions, coverage and quality signals (including the average essentiality %), and how the
    retrieval corpus was configured. Everything is aggregated from stored per-row results; nothing
    is re-scored.
    """
    ws = wb.create_sheet(SUMMARY_SHEET_NAME)
    ws.column_dimensions["A"].width = 40
    ws.column_dimensions["B"].width = 46

    try:
        run_meta = json.loads(run.meta_json) if run.meta_json else {}
    except (TypeError, ValueError):
        run_meta = {}

    n = len(patents)
    valid = [p for p in patents if p.essentiality is not None]
    invalid_rows = n - len(valid)

    fields = [gitas_fields_for_patent(p, run.id) for p in valid]

    def _mean(vals):
        """Mean of the non-None values rounded to 4 places, or None."""
        vals = [v for v in vals if v is not None]
        return round(statistics.mean(vals), 4) if vals else None

    evidence_counts = Counter(f["CPARAS_Bulk_Evidence_Level"] for f in fields)
    mapping_counts = Counter(f["CPARAS_Bulk_Mapping_Type"] for f in fields)

    row = 1
    row = _write_kv_section(ws, row, "Run Overview", [
        ("Batch ID", run.id),
        ("Source file", run.filename),
        ("Run timestamp", run.created_at.isoformat() if isinstance(run.created_at, datetime) else run.created_at),
        ("Total input rows", total_input_rows),
        ("Enriched output rows", total_input_rows - skipped_rows),
        ("Valid rows", len(valid)),
        ("Warning rows", sum(1 for f in fields if f["CPARAS_Bulk_Declaration_Conflict"])),
        ("Invalid rows", invalid_rows),
        ("Source rows without enrichment", skipped_rows),
        ("Successfully analyzed", len(valid)),
        ("Failed analysis", invalid_rows),
        ("Reports generated", len(valid)),
    ])

    ess_counts = Counter(f["CPARAS_Bulk_Essentiality"] for f in fields)
    row = _write_kv_section(ws, row, "Essentiality Distribution", [
        ("Essential", ess_counts.get("Essential", 0)),
        ("Potential", ess_counts.get("Potential", 0)),
        ("Non-Essential", ess_counts.get("Non-Essential", 0)),
    ])

    row = _write_kv_section(ws, row, "Evidence Level Distribution", [
        (f"Evidence level {lvl}", evidence_counts.get(lvl, 0)) for lvl in sorted(set(evidence_counts) | {0, 1, 2, 3})
    ])

    row = _write_kv_section(ws, row, "Mapping Type Distribution", [
        (mtype, cnt) for mtype, cnt in sorted(mapping_counts.items(), key=lambda kv: -kv[1])
    ] or [("(no rows)", 0)])

    row = _write_kv_section(ws, row, "Coverage & Quality Signals", [
        ("Corpus coverage rate", _mean([1.0 if f["CPARAS_Bulk_Corpus_Coverage"] else 0.0 for f in fields])),
        ("Declared-family coverage rate", _mean([1.0 if f["CPARAS_Bulk_Corpus_Coverage"] else 0.0 for f in fields])),
        ("Average semantic similarity", _mean(p.semantic_similarity for p in valid)),
        ("Average overall (SEP) confidence", _mean(p.confidence for p in valid)),
        ("Average essentiality (%)", (lambda m: f"{round(m * 100)}%" if m is not None else None)(
            _mean(f["CPARAS_Bulk_Essentiality_Confidence"] for f in fields))),
        ("Average extraction confidence", _mean(f["CPARAS_Bulk_Extraction_Confidence"] for f in fields)),
        ("Rows with version inconsistency", sum(1 for f in fields if f["CPARAS_Bulk_Version_Inconsistency"])),
        ("Rows with declaration conflict", sum(1 for f in fields if f["CPARAS_Bulk_Declaration_Conflict"])),
        ("Rows with claim extraction uncertainty", sum(1 for f in fields if f["CPARAS_Bulk_Claim_Extraction_Status"] != "AVAILABLE")),
        ("Rows requiring human review", sum(1 for f in fields if f["CPARAS_Bulk_Validation_Status"] == "NEEDS_REVIEW")),
        ("Rows failing QC", sum(1 for f in fields if f["CPARAS_Bulk_Validation_Status"] == "INVALID")),
    ])

    row = _write_kv_section(ws, row, "family_classification", [
        ("availability", "derived from stored per-row CPARAS_Bulk_Standard_Family / CPARAS_Bulk_Mapping_Type results"),
        ("reason", "Family classification is computed once at analysis time (etsi_bulk/scoring.py) "
                   "and exported as-is; this export does not re-run classification."),
    ])
    row = _write_kv_section(ws, row, "claim_mapping", [
        ("availability", "derived from stored per-row CPARAS_Bulk_Recommended_Clause / CPARAS_Bulk_Claim_1 results"),
        ("reason", "Claim-to-standard mapping mirrors the existing /api/sep/patents/{id} detail "
                   "endpoint's claim_to_standard_mapping block."),
    ])
    row = _write_kv_section(ws, row, "essentiality_screening", [
        ("availability", "derived from stored essentiality / confidence / claim_match / "
                          "semantic_similarity / standard_coverage results"),
        ("reason", "Essentiality screening itself runs in etsi_bulk/scoring.py at upload time; "
                   "this export only aggregates the already-computed per-row results."),
    ])
    row = _write_kv_section(ws, row, "retrieval", [
        ("embedder_mode", run_meta.get("embedder_mode")),
        ("corpus_size", run_meta.get("corpus_size")),
        ("corpus_source", run_meta.get("corpus_source", "illustrative (pre-v0.4.4 run)")),
        ("corpus_fingerprint", run_meta.get("corpus_fingerprint")),
        ("calibration", json.dumps(run_meta.get("calibration")) if run_meta.get("calibration") else None),
        ("corpus_disclaimer", run_meta.get("corpus_disclaimer")),
    ])


# ── Sheet selection (primary openpyxl path) ──────────────────────────────────

def _count_non_blank_rows(rows: list[list]) -> int:
    """Counts rows that aren't entirely None/empty — the same notion of a
    'data row' `_align_rows` uses to decide which rows to skip, reused here
    so post-generation validation checks against a consistent definition."""
    return sum(1 for row in rows if not (row is None or all(c in (None, "") for c in row)))


def _resolve_data_sheet_name(sheetnames: list[str], get_rows) -> tuple[Optional[str], bool, int]:
    """Picks the sheet whose header row actually looks like the bulk-export
    schema. `get_rows` is a callable(sheet_name) -> the sheet's first
    `_HEADER_SCAN_ROWS` rows. Returns (name, matched, header_row_1based) —
    matched is False if no sheet's header could be verified (caller should
    still proceed with the best-guess name and row 1, just without the same
    confidence `_align_rows` relies on).

    The header row is SEARCHED for rather than assumed to be row 1, to stay
    consistent with parse_bulk_workbook: a workbook with a title block
    above its header is now ingested, and each record's source_row_index
    counts from below that header, so this export has to start from the
    same row or every row's enrichment lands one row off."""
    for name in sheetnames:
        try:
            rows = get_rows(name)
        except Exception:
            continue
        idx = _find_header_row(rows)
        if idx is not None:
            return name, True, idx + 1
    return (sheetnames[0] if sheetnames else None), False, 1


# ── Public entry point ───────────────────────────────────────────────────────

def build_enriched_workbook(run: SepBulkRun, patents: list[SepBulkPatent]) -> tuple[bytes, list[str]]:
    """
    Builds the GITAS-enriched workbook for one run. Returns (xlsx_bytes,
    warnings). Consumes only already-persisted `run`/`patents` data — never
    touches the scoring engine.

    Tries, in order, to preserve as much original fidelity as possible:
      1. Reopen `original_file_blob` as an .xlsx workbook (formulas and
         formatting preserved; GITAS_* columns appended in place).
      2. Re-parse `original_file_blob` with the same reader/header-detection
         `parser.py` uses at upload time (covers .xls/.csv uploads, or an
         .xlsx blob that openpyxl itself can't reopen) — full column
         fidelity, no workbook formatting to preserve.
      3. Reconstruct Sheet1 from the subset of original columns actually
         persisted to the DB (lossy for columns never persisted) — only
         used if both of the above fail or no blob was stored at all.
    Every tier still appends the same GITAS_* columns and GITAS Summary
    sheet from the same already-scored `patents`.
    """
    warnings: list[str] = []
    total_input_rows = run.total_rows or len(patents)
    skipped_rows = 0
    expected_data_row_count = 0
    wb: Workbook

    opened_as_workbook = False
    if run.original_file_blob:
        try:
            # Two independent loads of the same bytes: `wb` is the one we
            # mutate and save, keeping any original formulas/styles intact
            # (loading without data_only=True preserves formula text rather
            # than baking in a stale cached value on save). `wb_values` is
            # read-only, opened with data_only=True purely so row alignment
            # below sees computed values rather than formula source text
            # for any application/publication-number cell that happens to
            # be a formula.
            wb = load_workbook(io.BytesIO(run.original_file_blob))
            wb_values = load_workbook(io.BytesIO(run.original_file_blob), data_only=True, read_only=True)

            def _scan_rows(name: str) -> list[list]:
                ws_v = wb_values[name]
                return [[c.value for c in row]
                        for row in ws_v.iter_rows(min_row=1, max_row=_HEADER_SCAN_ROWS)]

            sheet_name, matched, header_row = _resolve_data_sheet_name(wb_values.sheetnames, _scan_rows)
            if sheet_name is None:
                raise ValueError("workbook has no sheets")
            if run.sheet_name and run.sheet_name != sheet_name:
                warnings.append(
                    f"Stored sheet name '{run.sheet_name}' was not usable on reopen; "
                    f"used '{sheet_name}' instead (selected by matching the same header "
                    "columns the upload parser requires)."
                )
            if not matched:
                warnings.append(
                    f"Could not positively identify the data sheet by its header columns; "
                    f"proceeding with '{sheet_name}' as a best guess."
                )

            ws = wb[sheet_name]
            ws_v = wb_values[sheet_name]

            header = next(
                ([c.value for c in row]
                 for row in ws_v.iter_rows(min_row=header_row, max_row=header_row)),
                [],
            )
            n_orig_cols = len(header)
            if n_orig_cols == 0:
                raise ValueError(f"sheet '{sheet_name}' has an empty header row")
            if header_row > 1:
                warnings.append(
                    f"Header row of sheet '{sheet_name}' is row {header_row}; the "
                    f"{header_row - 1} row(s) above it were treated as a title block, "
                    "matching how the upload was parsed."
                )

            data_rows = [[c.value for c in row] for row in ws_v.iter_rows(min_row=header_row + 1)]
            wb_values.close()
            # Tier 1 never deletes original rows even when the cap below
            # trims how many get GITAS-enriched, so the row count Sheet1
            # will actually end up with is the *pre-cap* non-blank count.
            expected_data_row_count = _count_non_blank_rows(data_rows)
            if len(data_rows) > MAX_EXPORT_DATA_ROWS:
                warnings.append(
                    f"Original data has {len(data_rows)} rows, exceeding this export's "
                    f"{MAX_EXPORT_DATA_ROWS}-row cap — only the first {MAX_EXPORT_DATA_ROWS} were exported."
                )
                data_rows = data_rows[:MAX_EXPORT_DATA_ROWS]

            aligned, used_positional = _align_rows(header, data_rows, patents)
            if used_positional and data_rows:
                warnings.append(
                    "Could not locate an application-number or publication-number column "
                    "in the header — CPARAS_Bulk_* columns were matched to source rows "
                    "positionally (in order) instead of by identifier."
                )
            skipped_rows = sum(1 for a, r in zip(aligned, data_rows) if a is None and not _is_blank_row(r))
            total_input_rows = _count_non_blank_rows(data_rows)

            _write_gitas_columns(ws, header_row=header_row, first_data_row=header_row + 1,
                                  n_orig_cols=n_orig_cols, aligned=aligned, run_id=run.id)
            n_total_cols = n_orig_cols + len(GITAS_COLUMNS)
            _style_data_sheet(ws, header_row=header_row, n_cols=n_total_cols, n_rows=len(data_rows),
                               gitas_start_col=n_orig_cols + 1)

            if ws.title != "Sheet1":
                if "Sheet1" in wb.sheetnames and wb["Sheet1"] is not ws:
                    wb["Sheet1"].title = "Sheet1_orig"
                ws.title = "Sheet1"

            has_summary = any(s.lower() == "summary" for s in wb.sheetnames)
            if not has_summary:
                warnings.append("Original workbook had no 'Summary' sheet — none was added (nothing to preserve).")

            opened_as_workbook = True

        except Exception as e:
            logger.warning(f"CPARAS Bulk export: could not reopen original blob as .xlsx for run {run.id}: {e}")
            warnings.append(f"Could not reopen the original uploaded file as an Excel workbook ({e}).")

    if not opened_as_workbook:
        wb = Workbook()
        reparsed = None
        if run.original_file_blob:
            try:
                reparsed = _build_reparsed_sheet1(wb, run)
            except Exception as e:
                logger.warning(f"CPARAS Bulk export: full-fidelity re-parse failed for run {run.id}: {e}")

        if reparsed is not None:
            header, data_rows, reparse_warnings = reparsed
            warnings += reparse_warnings
            # Tier 2 builds Sheet1 fresh, row by row, from the (already
            # capped inside _build_reparsed_sheet1) `data_rows` — so those
            # are exactly the rows physically present in the sheet.
            expected_data_row_count = _count_non_blank_rows(data_rows)
            aligned, used_positional = _align_rows(header, data_rows, patents)
            if used_positional and data_rows:
                warnings.append(
                    "Could not locate an application-number or publication-number column "
                    "in the re-parsed header — CPARAS_Bulk_* columns were matched to source "
                    "rows positionally (in order) instead of by identifier."
                )
            skipped_rows = sum(1 for a, r in zip(aligned, data_rows) if a is None and not _is_blank_row(r))
            total_input_rows = _count_non_blank_rows(data_rows)
            n_orig_cols = len(header)
        else:
            if run.original_file_blob:
                warnings.append(
                    "The stored original bytes could not be read in any supported format "
                    "(.xlsx/.xls/.csv) — falling back to a Sheet1 reconstructed from stored "
                    "per-row results only."
                )
            warnings += _build_reconstructed_sheet1(wb, patents)
            aligned = list(patents)
            n_orig_cols = len(_RECONSTRUCT_COLUMNS)
            # Tier 3 writes exactly one row per patent, unconditionally.
            expected_data_row_count = len(patents)

        _write_gitas_columns(wb["Sheet1"], header_row=1, first_data_row=2, n_orig_cols=n_orig_cols,
                              aligned=aligned, run_id=run.id)
        _style_data_sheet(wb["Sheet1"], header_row=1, n_cols=n_orig_cols + len(GITAS_COLUMNS),
                           n_rows=len(aligned), gitas_start_col=n_orig_cols + 1)

    try:
        _build_gitas_summary_sheet(wb, run, patents, total_input_rows, skipped_rows)
    except Exception as e:
        # The GITAS Summary sheet is an aggregate convenience view — a bug
        # in a specific aggregate must not prevent the (far more important)
        # per-row Sheet1 data from being downloadable.
        logger.exception(f"CPARAS Bulk export: failed to build the summary sheet for run {run.id}")
        warnings.append(f"Could not build the '{SUMMARY_SHEET_NAME}' sheet ({e}); Sheet1 is still complete.")
        if SUMMARY_SHEET_NAME not in wb.sheetnames:
            wb.create_sheet(SUMMARY_SHEET_NAME).cell(row=1, column=1, value=f"Summary generation failed: {e}")

    buf = io.BytesIO()
    wb.save(buf)
    xlsx_bytes = buf.getvalue()

    # Final safety net: never hand back bytes we haven't independently
    # verified form a complete, well-structured workbook. Raises
    # ExcelExportValidationError (a controlled, well-typed failure) rather
    # than returning a corrupted/incomplete download if anything above
    # produced an inconsistent result.
    _validate_generated_workbook(xlsx_bytes, expected_data_row_count)

    return xlsx_bytes, warnings


def export_filename(run: SepBulkRun) -> str:
    """Builds the download filename. Sanitized against characters that would
    break a Content-Disposition header (quotes, CR/LF, path separators) —
    `run.filename` is user-supplied (the original upload's filename) so it
    must not be trusted verbatim in an HTTP header."""
    base = (run.filename or "sep_run").rsplit(".", 1)[0]
    base = re.sub(r'[\r\n"\\/\x00-\x1f]', "_", base).strip() or "sep_run"
    return f"{base}_CPARAS_Bulk_enriched.xlsx"
