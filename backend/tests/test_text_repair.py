"""
Regression tests for utils/text_repair.

Run: python -m pytest backend/tests/test_text_repair.py -q
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from utils.text_repair import (  # noqa: E402
    reconstruct_wrapped_identifiers,
    repair_wrapped_country_names,
    reconstruct_all,
)


# ── ISSUE 1 / 3: wrapped PCT numbers ────────────────────────────────────────

def test_wrapped_pct_number_is_merged():
    raw = "5 | 5G Release 15 | PCT/CN2024/1107\n84 Cooperation Patent Treaty filing"
    fixed = reconstruct_wrapped_identifiers(raw)
    assert "PCT/CN2024/110784" in fixed


def test_wrapped_pct_tail_does_not_survive_as_its_own_line():
    """The orphaned '84' line must be gone — that's what previously looked
    like a new row number to any line-based row detector."""
    raw = "5 | 5G Release 15 | PCT/CN2024/1107\n84 Cooperation Patent Treaty filing"
    fixed = reconstruct_wrapped_identifiers(raw)
    lines = [l for l in fixed.split("\n") if l.strip()]
    assert not any(l.strip().startswith("84 ") for l in lines)


def test_remainder_text_after_tail_digits_is_preserved():
    raw = "PCT/CN2024/1107\n84 Cooperation Patent Treaty filing"
    fixed = reconstruct_wrapped_identifiers(raw)
    assert "Cooperation Patent Treaty filing" in fixed


def test_wo_wo_and_ep_ids_reconstruct_too():
    for prefix, before, tail, after in [
        ("WO", "WO2019/1234", "56", "WO2019/123456"),
        ("EP", "EP34567", "89", "EP3456789"),
    ]:
        raw = f"{before}\n{tail}"
        fixed = reconstruct_wrapped_identifiers(raw)
        assert after in fixed, f"{prefix}: expected {after!r} in {fixed!r}"


def test_genuine_new_row_with_pipe_is_not_merged():
    """A real pipe-delimited table row must never be eaten as a wrap tail,
    even if it starts with a short number."""
    raw = "US2019/1234\n84 | 5G Release 16 | TS 38.331"
    fixed = reconstruct_wrapped_identifiers(raw)
    assert "84 | 5G Release 16 | TS 38.331" in fixed
    assert "US2019/123484" not in fixed


def test_ordinary_prose_is_left_untouched():
    raw = "This document describes the applicant's patent portfolio.\nIt covers several standards."
    assert reconstruct_wrapped_identifiers(raw) == raw


# ── ISSUE 6 / 7: wrapped and column-bled country names ─────────────────────

def test_simple_wrapped_country_name():
    raw = "UNITED\nSTATES"
    fixed = repair_wrapped_country_names(raw)
    assert "UNITED STATES" in fixed


def test_wrapped_united_kingdom():
    raw = "UNITED\nKINGDOM"
    fixed = repair_wrapped_country_names(raw)
    assert "UNITED KINGDOM" in fixed


def test_column_bleed_applicant_between_country_halves():
    raw = "UNITED\nTechnologies Oy KINGDOM"
    fixed = repair_wrapped_country_names(raw)
    assert "UNITED KINGDOM" in fixed
    assert "Technologies Oy" in fixed
    # The applicant fragment must be separated back out as its own cell,
    # not fused into the country string.
    assert "Technologies Oy | UNITED KINGDOM" in fixed


def test_reconstruct_all_handles_both_in_one_pass():
    raw = (
        "3 | 5G Release 16 | PCT/CN2024/1107\n"
        "84 Cooperation Patent Treaty filing | Nokia\nUNITED\nKINGDOM"
    )
    fixed = reconstruct_all(raw)
    assert "PCT/CN2024/110784" in fixed
    assert "UNITED KINGDOM" in fixed


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
