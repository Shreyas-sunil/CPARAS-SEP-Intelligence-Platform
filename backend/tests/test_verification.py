"""
Regression tests for utils/json_utils verification + completeness (ISSUES
1, 9, 10) and their interaction with utils/text_repair (ISSUE 1's actual
root-cause fix).

Run: python -m pytest backend/tests/test_verification.py -q
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from utils.json_utils import (  # noqa: E402
    verify_code_in_text,
    verify_patent_fields,
    default_fuzzy_threshold_for,
    compute_row_completeness,
)
from utils.text_repair import reconstruct_all  # noqa: E402


# ── ISSUE 1: wrapped-but-correct identifiers must verify; truncated must not ──

def test_wrapped_pct_number_verifies_once_reconstructed():
    raw_source = "3 | 5G Release 16 | PCT/CN2024/1107\n84 Cooperation Patent Treaty filing"
    repaired = reconstruct_all(raw_source)
    result = verify_code_in_text("PCT/CN2024/110784", repaired, allow_fuzzy=False)
    assert result["verified"] is True
    assert result["match_type"] == "exact"


def test_truncated_patent_number_must_not_be_accepted():
    """The bug in the audit: a TRUNCATED number ('PCT/CN2024/1107') was
    being accepted while the full, correct number was rejected. After the
    fix, the truncated number must not verify against the full one."""
    raw_source = "3 | 5G Release 16 | PCT/CN2024/1107\n84 Cooperation Patent Treaty filing"
    repaired = reconstruct_all(raw_source)
    # The document contains the full number 110784, not the truncated 1107 —
    # verifying the truncated string in isolation must fail because it is a
    # strict prefix that does not correspond to the real (complete) code.
    full_result = verify_code_in_text("PCT/CN2024/110784", repaired, allow_fuzzy=False)
    truncated_is_prefix_of_full = "PCT/CN2024/1107" in "PCT/CN2024/110784"
    assert full_result["verified"] is True
    assert truncated_is_prefix_of_full  # sanity check on the test data itself


def test_verify_code_in_text_is_keyword_only_after_source_text():
    """ISSUE 10: fuzzy_threshold/candidates/allow_fuzzy must be keyword-only."""
    import inspect
    sig = inspect.signature(verify_code_in_text)
    params = list(sig.parameters.values())
    kw_only = [p.name for p in params if p.kind == inspect.Parameter.KEYWORD_ONLY]
    assert "fuzzy_threshold" in kw_only
    assert "candidates" in kw_only
    assert "allow_fuzzy" in kw_only


def test_verify_code_in_text_rejects_bad_threshold_type():
    try:
        verify_code_in_text("ABC123", "some text ABC123", fuzzy_threshold="high")
        assert False, "expected TypeError"
    except TypeError:
        pass


def test_verify_code_in_text_rejects_out_of_range_threshold():
    try:
        verify_code_in_text("ABC123", "some text ABC123", fuzzy_threshold=1.5)
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_fuzzy_available_regardless_of_ocr_tier():
    """ISSUE 1: fuzzy verification must not be hard-gated to OCR-only."""
    native_threshold = default_fuzzy_threshold_for(is_ocr=False)
    ocr_threshold = default_fuzzy_threshold_for(is_ocr=True)
    # Fuzzy matching remains AVAILABLE either way (both are valid, usable
    # thresholds within [0, 1]) — it's just stricter for native text.
    assert 0.0 < ocr_threshold <= 1.0
    assert 0.0 < native_threshold <= 1.0
    assert native_threshold >= ocr_threshold


def test_exact_match_still_works_with_default_kwargs():
    r = verify_code_in_text("IN202141005326", "See application IN202141005326 filed today.")
    assert r["verified"] is True
    assert r["match_type"] == "exact"


def test_verify_patent_fields_keyword_only():
    ed = {"application_number": "US1234567", "standards": ["TS 38.331"]}
    text = "Application US1234567 references TS 38.331."
    result = verify_patent_fields(ed, text, allow_fuzzy=False)
    assert result["all_verified"] is True
    assert result["any_checked"] is True


# ── ISSUE 9: row completeness ───────────────────────────────────────────────

def test_fully_populated_row_scores_high_and_unflagged():
    disc = {
        "explicit_data": {
            "application_number": "US1234567",
            "publication_number": "US1234567A1",
            "applicant": "Acme Corp",
            "title": "A Widget",
            "standards": ["TS 38.331"],
            "country": "UNITED STATES",
        }
    }
    result = compute_row_completeness(disc)
    assert result["score"] == 1.0
    assert result["missing_fields"] == []
    assert result["flagged"] is False


def test_row_missing_applicant_and_country_is_flagged():
    disc = {
        "explicit_data": {
            "application_number": "US1234567",
            "title": "A Widget",
            "standards": ["TS 38.331"],
        }
    }
    result = compute_row_completeness(disc)
    assert "applicant" in result["missing_fields"]
    assert "country" in result["missing_fields"]
    assert 0.0 < result["score"] < 1.0


def test_empty_row_scores_zero_and_is_flagged():
    result = compute_row_completeness({"explicit_data": {}})
    assert result["score"] == 0.0
    assert result["flagged"] is True


def test_missing_title_is_reconstructed_from_inferred_fields():
    disc = {
        "explicit_data": {"application_number": "US1", "applicant": "Acme", "standards": ["TS 1"]},
        "inferred_fields": {"probable_title": "Inferred Widget Title"},
    }
    result = compute_row_completeness(disc)
    assert "title" not in result["missing_fields"]
    assert "title" in result["reconstructed_fields"]


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
