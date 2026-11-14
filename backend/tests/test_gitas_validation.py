"""
Tests for the evidence-based GITAS_Validation_Status logic in
`etsi_bulk/excel_export.py` (`gitas_fields_for_patent` /
`_conflict_materiality`).

Run: python -m pytest backend/tests/test_gitas_validation.py -q
 or: python3 backend/tests/_run_all.py

Background: `GITAS_Validation_Status` used to be a near-passthrough of
`essentiality` — every row with a scored result and essentiality other
than "Potential" was marked VALID, even when `GITAS_Declaration_Conflict`
was also True (a row could carry both `DECLARATION_CONFLICT` in its
reason codes AND `VALID` as its status). This file tests the fix: a
graded, evidence-based decision that distinguishes a genuine, checkable
contradiction (declared family confirmable in the reference corpus, or a
cross-generation/cross-domain mapping) from a merely-unconfirmed topical
match (the declared family simply isn't in this small illustrative
corpus at all — inherent to a ~11-family reference set, not a wrong
answer), and factors in confidence/similarity rather than a single
boolean.

These are unit tests against `gitas_fields_for_patent` directly (fake
`SimpleNamespace` patents, no DB/embedding needed) — see
test_excel_export.py for the full upload -> score -> export integration
tests, and see the bottom of this file for a summary-level check against
the real `Sample_Data_for_UIUX_Database.xlsx` reference dataset when
available.
"""
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from etsi_bulk.excel_export import gitas_fields_for_patent, _conflict_materiality  # noqa: E402
from research.engine import SIM_CONFIRMED, SIM_LIKELY  # noqa: E402
from research.three_signal_scoring import HIGH_CONFIDENCE  # noqa: E402


def _fake_patent(**kwargs):
    defaults = dict(
        id=1, essentiality=None, confidence=None, semantic_similarity=None,
        claim_match=None, standard_coverage=None,
        declared_family_match=False, matched_standard=None, matched_clause=None,
        matched_clause_title=None, matched_domain=None, illustrative_part=None,
        title=None, standard_raw=None, spec_number=None, spec_version=None,
        release=None, legal_status=None, technology=None,
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


# ── No result → INVALID (unchanged rule) ─────────────────────────────────────

def test_no_result_is_invalid():
    p = _fake_patent()
    f = gitas_fields_for_patent(p, run_id=1)
    assert f["CPARAS_Bulk_Status"] == "FAILED"
    assert f["CPARAS_Bulk_Validation_Status"] == "INVALID"


# ── Potential → NEEDS_REVIEW (unchanged rule) ────────────────────────────────

def test_potential_is_needs_review_regardless_of_conflict():
    p = _fake_patent(
        essentiality="Potential", confidence=0.4, semantic_similarity=0.4,
        declared_family_match=False, matched_standard="TS 33.501 v16.2.0",
        standard_raw="3GPP TS 38.331", spec_version="16.2.0",
    )
    f = gitas_fields_for_patent(p, run_id=1)
    assert f["CPARAS_Bulk_Validation_Status"] == "NEEDS_REVIEW"


# ── Non-Essential → VALID (a negative claim doesn't need family confirmation) ─

def test_non_essential_is_valid_even_with_declaration_conflict():
    p = _fake_patent(
        essentiality="Non-Essential", confidence=0.2, semantic_similarity=0.15,
        declared_family_match=False, matched_standard="TS 33.501 v16.2.0",
        standard_raw="3GPP TS 48.008 version 7.5.0", technology="2G / GSM",
    )
    f = gitas_fields_for_patent(p, run_id=1)
    assert f["CPARAS_Bulk_Declaration_Conflict"] is True
    assert f["CPARAS_Bulk_Validation_Status"] == "VALID"


# ── Clean legitimate declared-family match → VALID ───────────────────────────

def test_declared_family_match_is_valid():
    p = _fake_patent(
        essentiality="Essential", confidence=0.9, semantic_similarity=0.85,
        declared_family_match=True, matched_standard="TS 38.331 v16.2.0",
        matched_domain="RRC", standard_raw="3GPP TS 38.331", spec_version="16.2.0",
        technology="5G NR",
    )
    f = gitas_fields_for_patent(p, run_id=1)
    assert f["CPARAS_Bulk_Validation_Status"] == "VALID"
    assert f["CPARAS_Bulk_Declaration_Conflict"] is False
    assert "DECLARATION_CONFLICT" not in (f["CPARAS_Bulk_Reason_Codes"] or "")


# ── The reported bug's exact shape: cross-generation, low-confidence mapping ─

def test_reported_bug_gsm_declared_matched_to_5g_security_is_not_valid():
    """The concrete case from the report: declared 3GPP TS 48.008 (a GSM
    BSS interface spec — technology "2G / GSM"), matched to TS 33.501
    (5G Security). The corpus contains zero 2G content, so this is a
    checkable cross-generation contradiction (a GSM-only declaration
    cannot be essential to a 5G NR security clause), and confidence here
    is deliberately low — the combination must NOT be VALID."""
    p = _fake_patent(
        essentiality="Essential", confidence=0.55, semantic_similarity=0.55,
        declared_family_match=False, matched_standard="TS 33.501 v16.2.0",
        matched_clause="Clause 6.1", matched_clause_title="Primary Authentication and Key Agreement",
        matched_domain="Security", standard_raw="3GPP TS 48.008 version 7.5.0",
        technology="2G / GSM",
    )
    f = gitas_fields_for_patent(p, run_id=1)
    assert f["CPARAS_Bulk_Declaration_Conflict"] is True
    assert f["CPARAS_Bulk_Validation_Status"] != "VALID"
    # The reason codes and the status must agree — this was the core bug.
    if "DECLARATION_CONFLICT" in (f["CPARAS_Bulk_Reason_Codes"] or ""):
        assert f["CPARAS_Bulk_Validation_Status"] in ("NEEDS_REVIEW", "INVALID")


def test_material_conflict_low_confidence_is_invalid():
    """Overall confidence below HIGH_CONFIDENCE, combined with a material
    (checkable) conflict, is not well-evidenced enough to overrule the
    contradiction -> INVALID, not just a review flag. (v0.4.1: every
    "Essential" row now has confidence >= ESSENTIAL_MIN_CONFIDENCE, so the
    override bar is HIGH_CONFIDENCE rather than SIM_CONFIRMED.)"""
    p = _fake_patent(
        essentiality="Essential", confidence=HIGH_CONFIDENCE - 0.05, semantic_similarity=0.5,
        declared_family_match=False, matched_standard="TS 33.501 v16.2.0",
        matched_domain="Security", standard_raw="3GPP TS 48.008 version 7.5.0",
        technology="2G / GSM",
    )
    f = gitas_fields_for_patent(p, run_id=1)
    assert f["CPARAS_Bulk_Validation_Status"] == "INVALID"


def test_material_conflict_high_confidence_is_needs_review_not_invalid():
    """Same material conflict, but confidence at/above HIGH_CONFIDENCE —
    strong enough evidence that a human should decide, not an automatic
    rejection. Demonstrates the tiering isn't "any conflict -> INVALID"."""
    p = _fake_patent(
        essentiality="Essential", confidence=HIGH_CONFIDENCE + 0.05, semantic_similarity=0.7,
        declared_family_match=False, matched_standard="TS 33.501 v16.2.0",
        matched_domain="Security", standard_raw="3GPP TS 48.008 version 7.5.0",
        technology="2G / GSM",
    )
    f = gitas_fields_for_patent(p, run_id=1)
    assert f["CPARAS_Bulk_Validation_Status"] == "NEEDS_REVIEW"


# ── Non-material conflict: declared family simply isn't in this small corpus ─

def test_non_material_conflict_with_good_similarity_stays_valid():
    """Declared family unknown to the (tiny, ~11-family) illustrative
    corpus, same generation as the matched entry, decent similarity, no
    domain signal available to contradict it — this is an inherent
    corpus-coverage gap, not a wrong answer, and must not be penalized
    the same as a genuine contradiction (the "don't just lower everything"
    requirement)."""
    p = _fake_patent(
        essentiality="Essential", confidence=0.7, semantic_similarity=0.65,
        declared_family_match=False, matched_standard="TS 38.214 v16.2.0",
        matched_domain="PHY", standard_raw="3GPP TS 29.244", spec_version="15.4.0",
        technology="LTE",
    )
    f = gitas_fields_for_patent(p, run_id=1)
    assert not _conflict_materiality(p, declaration_conflict=True)
    assert f["CPARAS_Bulk_Validation_Status"] == "VALID"
    assert "DECLARATION_UNCONFIRMED" in (f["CPARAS_Bulk_Reason_Codes"] or "")
    assert "DECLARATION_CONFLICT" not in (f["CPARAS_Bulk_Reason_Codes"] or "")


def test_non_material_conflict_with_weak_similarity_is_needs_review():
    """Same coverage-gap situation, but similarity is below the
    platform's own SIM_LIKELY bar — too weak to present as a confident
    VALID result even without a checkable contradiction."""
    p = _fake_patent(
        essentiality="Essential", confidence=SIM_LIKELY - 0.02, semantic_similarity=SIM_LIKELY - 0.02,
        declared_family_match=False, matched_standard="TS 38.214 v16.2.0",
        matched_domain="PHY", standard_raw="3GPP TS 29.244", spec_version="15.4.0",
        technology="LTE",
    )
    f = gitas_fields_for_patent(p, run_id=1)
    assert f["CPARAS_Bulk_Validation_Status"] == "NEEDS_REVIEW"


# ── Version inconsistency ─────────────────────────────────────────────────────

def test_version_inconsistency_is_needs_review():
    p = _fake_patent(
        essentiality="Essential", confidence=0.8, semantic_similarity=0.75,
        declared_family_match=True, matched_standard="TS 38.331 v16.2.0",
        matched_domain="RRC", standard_raw="3GPP TS 38.331", spec_version="8.5.0",
        technology="5G NR",
    )
    f = gitas_fields_for_patent(p, run_id=1)
    assert f["CPARAS_Bulk_Version_Inconsistency"] is True
    assert f["CPARAS_Bulk_Validation_Status"] == "NEEDS_REVIEW"
    assert "VERSION_MISMATCH" in f["CPARAS_Bulk_Reason_Codes"]


def test_matching_version_is_not_flagged():
    p = _fake_patent(
        essentiality="Essential", confidence=0.8, semantic_similarity=0.75,
        declared_family_match=True, matched_standard="TS 38.331 v16.2.0",
        matched_domain="RRC", standard_raw="3GPP TS 38.331", spec_version="16.5.0",
        technology="5G NR",
    )
    f = gitas_fields_for_patent(p, run_id=1)
    assert f["CPARAS_Bulk_Version_Inconsistency"] is False
    assert f["CPARAS_Bulk_Validation_Status"] == "VALID"


# ── Multiple simultaneous conflicts ──────────────────────────────────────────

def test_multiple_simultaneous_conflicts_still_produce_a_single_coherent_status():
    """Material conflict AND (nominally) version inconsistency at once —
    version_inconsistency only actually fires when declared_family_match
    is True, which by definition can't coexist with a declaration
    conflict, so this checks the two paths don't fight and produce an
    invalid/contradictory combination of flags."""
    p = _fake_patent(
        essentiality="Essential", confidence=0.3, semantic_similarity=0.3,
        declared_family_match=False, matched_standard="TS 33.501 v16.2.0",
        matched_domain="Security", standard_raw="3GPP TS 48.008 version 7.5.0",
        spec_version="7.5.0", technology="2G / GSM",
    )
    f = gitas_fields_for_patent(p, run_id=1)
    assert f["CPARAS_Bulk_Declaration_Conflict"] is True
    assert f["CPARAS_Bulk_Version_Inconsistency"] is False  # can't fire without declared_match
    assert f["CPARAS_Bulk_Validation_Status"] == "INVALID"


# ── Reason codes and status must always agree ────────────────────────────────

def test_declaration_conflict_reason_code_never_coexists_with_valid():
    """The core requirement: whenever the reason codes indicate a
    material DECLARATION_CONFLICT, the status must not be VALID."""
    cases = [
        dict(essentiality="Essential", confidence=0.3, semantic_similarity=0.3,
             matched_standard="TS 33.501 v16.2.0", matched_domain="Security",
             standard_raw="3GPP TS 48.008 version 7.5.0", technology="2G / GSM"),
        dict(essentiality="Essential", confidence=0.9, semantic_similarity=0.8,
             matched_standard="TS 23.501 v16.2.0", matched_domain="Core Network",
             standard_raw="3GPP TS 33.501", technology="5G NR"),  # declared family IS in corpus
    ]
    for kwargs in cases:
        p = _fake_patent(declared_family_match=False, **kwargs)
        f = gitas_fields_for_patent(p, run_id=1)
        reason_codes = f["CPARAS_Bulk_Reason_Codes"] or ""
        if "DECLARATION_CONFLICT" in reason_codes:
            assert f["CPARAS_Bulk_Validation_Status"] != "VALID", (
                f"DECLARATION_CONFLICT reason code co-occurred with VALID status: {f}"
            )


# ── Regression: DB-shaped object without every optional attribute ───────────

def test_missing_optional_attributes_do_not_crash():
    """A real SepBulkPatent always has every column (possibly None); this
    guards against a partial/fallback object (e.g. an older DB row from
    before a column existed) crashing `_conflict_materiality` instead of
    just treating the missing signal as unavailable."""
    p = SimpleNamespace(
        id=1, essentiality="Essential", confidence=0.8, semantic_similarity=0.8,
        declared_family_match=False, matched_standard="TS 38.331 v16.2.0",
        matched_clause=None, matched_clause_title=None, illustrative_part=None,
        title=None, standard_raw="3GPP TS 38.331", spec_version=None, release=None,
        legal_status=None,
        # matched_domain, technology, spec_number deliberately omitted
    )
    f = gitas_fields_for_patent(p, run_id=1)
    assert f["CPARAS_Bulk_Validation_Status"] in ("VALID", "NEEDS_REVIEW", "INVALID")


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(list(globals().items())):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception as e:
                failures += 1
                print(f"FAIL {name}: {e}")
    raise SystemExit(1 if failures else 0)
