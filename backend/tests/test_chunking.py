"""
Regression tests for utils/chunking (ISSUES 2, 4, 5).

Run: python -m pytest backend/tests/test_chunking.py -q
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from utils.chunking import (  # noqa: E402
    ROW_ANCHOR_RE,
    row_positions,
    split_into_chunks,
    estimate_chunk_token_budget,
    extraction_seems_incomplete,
)


# ── ISSUE 2: comma in row anchor ────────────────────────────────────────────

def test_row_with_comma_in_project_code_matches():
    text = "1 | 5G Release 15, | TS 38.331 | some title"
    assert ROW_ANCHOR_RE.search(text) is not None


def test_row_without_comma_still_matches():
    text = "1 | 5G Release 15 | TS 38.331 | some title"
    assert ROW_ANCHOR_RE.search(text) is not None


def test_multiple_comma_rows_all_detected():
    text = (
        "1 | 5G Release 15, | TS 38.331 | A\n"
        "2 | 4G Release 14, LTE | EN 301 908 | B\n"
        "3 | 5G Release 16 | TS 38.213 | C\n"
    )
    assert len(row_positions(text)) == 3


# ── ISSUE 4: header preservation across chunks ──────────────────────────────

def _make_big_declaration(n_rows: int, header: str) -> str:
    rows = "\n".join(
        f"{i} | 5G Release 16 | TS 38.{300 + i} | Some invention title number {i} " + ("x" * 50)
        for i in range(1, n_rows + 1)
    )
    return header + "\n\n" + rows


def test_header_survives_into_every_chunk():
    header = "Qualcomm Incorporated\nETSI IPR Declaration\nDeclarant: Qualcomm\n" + ("cover page filler " * 100)
    text = _make_big_declaration(80, header)
    chunks = split_into_chunks(text, chunk_size=4000)
    assert len(chunks) > 1, "test setup should force multiple chunks"
    for i, c in enumerate(chunks):
        assert "Qualcomm" in c, f"chunk {i} lost the header"


def test_single_chunk_document_unaffected():
    text = "short document with one row\n1 | 5G | TS 38.331 | title"
    chunks = split_into_chunks(text, chunk_size=20000)
    assert chunks == [text]


def test_no_row_anchors_falls_back_to_paragraphs_and_keeps_lead_in():
    header = "Sony Corporation cover page. " * 50
    body = ("\n\nThis is an unstructured paragraph of prose. " * 200)
    text = header + body
    chunks = split_into_chunks(text, chunk_size=2000)
    assert len(chunks) > 1
    assert any("Sony" in c for c in chunks[1:]), "later chunks should still carry lead-in context"


# ── ISSUE 5: token budgeting ─────────────────────────────────────────────────

def test_budget_scales_with_row_count():
    small = estimate_chunk_token_budget("x" * 100, est_rows=2)
    large = estimate_chunk_token_budget("x" * 100, est_rows=30)
    assert large > small


def test_budget_has_a_floor_even_with_zero_rows_on_long_text():
    long_text = "some table cell content here, " * 2000  # ~62k chars
    budget = estimate_chunk_token_budget(long_text, est_rows=0)
    assert budget > 5000, "a long chunk must not get a near-zero budget just because row detection found nothing"


def test_low_ocr_confidence_increases_budget():
    text = "x" * 3000
    baseline = estimate_chunk_token_budget(text, est_rows=15, is_ocr=True, ocr_confidence=90)
    noisy = estimate_chunk_token_budget(text, est_rows=15, is_ocr=True, ocr_confidence=40)
    assert noisy > baseline


def test_reproduces_the_reported_ocr_under_detection_case():
    """
    ISSUE 5's reported scenario: OCR detects 15 rows instead of the real 40.
    The old formula: min(18000, 15*300+1200) = 5700 tokens — not enough for
    ~40 rows (~40*300+1200=13200). The new budget must clear a much higher
    floor once low OCR confidence is taken into account, even without ever
    being told the true row count.
    """
    # Build OCR-like text roughly the size of a 40-row chunk, but only 15
    # rows are anchor-detectable (simulating OCR mangling most TS/EN/TR
    # tokens into noise).
    detectable_rows = "\n".join(
        f"{i} | 5G Release 16 | TS 38.{300+i} | " + ("garbled ocr text " * 8) for i in range(1, 16)
    )
    undetectable_rows = "\n".join(
        f"{i} | 5G Rel3ase | T5 38.{300+i} | " + ("garbled ocr text " * 8) for i in range(16, 41)
    )
    chunk_text = detectable_rows + "\n" + undetectable_rows
    est_rows = len(row_positions(chunk_text))
    assert est_rows < 20  # confirm the under-detection premise

    old_style_budget = min(18000, est_rows * 300 + 1200)
    new_budget = estimate_chunk_token_budget(
        chunk_text, est_rows, is_ocr=True, ocr_confidence=45,
    )
    assert new_budget > old_style_budget
    assert new_budget >= 10000  # comfortably enough headroom for ~40 rows


# ── ISSUE 5: incomplete-extraction detection ────────────────────────────────

def test_salvaged_result_always_flagged_incomplete():
    assert extraction_seems_incomplete(parsed_row_count=38, est_rows=40, was_salvaged=True) is True


def test_shortfall_without_salvage_is_flagged():
    assert extraction_seems_incomplete(parsed_row_count=4, est_rows=30, was_salvaged=False) is True


def test_normal_variance_is_not_flagged():
    assert extraction_seems_incomplete(parsed_row_count=27, est_rows=30, was_salvaged=False) is False


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
