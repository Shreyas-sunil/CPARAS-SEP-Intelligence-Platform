"""
Regression tests for research/embeddings.calibrate_similarity_matrix and
research/engine._predicted_status (Additional Issues: similarity + confidence
score pathology).

Run: python -m pytest backend/tests/test_similarity_confidence.py -q
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import numpy as np  # noqa: E402

from research.embeddings import calibrate_similarity_matrix  # noqa: E402
from research.engine import _predicted_status  # noqa: E402


# ── calibrate_similarity_matrix ─────────────────────────────────────────────

def test_calibration_spreads_a_compressed_anisotropic_band():
    """Simulates the reported pathology: raw cosine similarities compressed
    into a narrow band around a high baseline (anisotropy). After
    calibration the spread must be much wider — the whole point of the fix."""
    rng = np.random.default_rng(0)
    raw = rng.normal(loc=0.5, scale=0.03, size=(20, 54)).astype("float32")
    raw = np.clip(raw, 0.0, 1.0)
    calibrated, meta = calibrate_similarity_matrix(raw)
    assert meta["degenerate"] is False
    assert calibrated.std() > raw.std() * 2, "calibration should meaningfully widen a compressed distribution"
    assert calibrated.min() >= 0.0 and calibrated.max() <= 1.0


def test_calibration_preserves_ordering():
    """Calibration is a monotonic affine transform (with clipping at the
    percentile anchors) — any two raw values that are NOT both clipped to
    the same boundary must keep their relative order after calibration."""
    rng = np.random.default_rng(1)
    raw = rng.uniform(0.2, 0.8, size=(10, 54)).astype("float32")
    calibrated, meta = calibrate_similarity_matrix(raw)
    lo, hi = meta["low"], meta["high"]
    flat_raw = raw.reshape(-1)
    flat_cal = calibrated.reshape(-1)
    # Sample a bunch of pairs strictly inside the non-clipped region and
    # check order is preserved for each.
    inside = np.where((flat_raw > lo) & (flat_raw < hi))[0]
    rng2 = np.random.default_rng(2)
    for _ in range(200):
        i, j = rng2.choice(inside, size=2, replace=False)
        if flat_raw[i] == flat_raw[j]:
            continue
        assert (flat_raw[i] < flat_raw[j]) == (flat_cal[i] < flat_cal[j])


def test_degenerate_zero_spread_falls_back_gracefully():
    """All-identical raw similarities (e.g. every patent embedded to the
    same fallback text) must not blow up into noise from dividing by ~0."""
    raw = np.full((5, 54), 0.42, dtype="float32")
    calibrated, meta = calibrate_similarity_matrix(raw)
    assert meta["degenerate"] is True
    assert np.allclose(calibrated, 0.42)


def test_empty_matrix_handled():
    raw = np.zeros((0, 0), dtype="float32")
    calibrated, meta = calibrate_similarity_matrix(raw)
    assert calibrated.size == 0
    assert meta["degenerate"] is True


def test_calibration_is_not_a_fixed_arbitrary_constant():
    """Two different raw distributions must calibrate against their OWN
    observed spread, not a single hardcoded constant — this is the
    'statistically sensible normalization instead of arbitrary scaling'
    requirement."""
    rng = np.random.default_rng(2)
    tight = np.clip(rng.normal(0.5, 0.02, size=(10, 54)), 0, 1).astype("float32")
    wide = np.clip(rng.normal(0.5, 0.25, size=(10, 54)), 0, 1).astype("float32")
    _, meta_tight = calibrate_similarity_matrix(tight)
    _, meta_wide = calibrate_similarity_matrix(wide)
    assert (meta_tight["high"] - meta_tight["low"]) < (meta_wide["high"] - meta_wide["low"])


# ── _predicted_status / confidence blending ─────────────────────────────────

def test_confidence_is_continuous_not_clustered():
    """Different input signals must produce meaningfully DIFFERENT
    confidence values, not the same constant — the reported '0%/50% for
    almost everything' symptom."""
    _, c1 = _predicted_status(0.9, True, None, verification_ratio=1.0, completeness_score=1.0)
    _, c2 = _predicted_status(0.4, False, None, verification_ratio=0.2, completeness_score=0.3)
    _, c3 = _predicted_status(0.65, True, None, verification_ratio=0.6, completeness_score=0.7)
    values = {c1, c2, c3}
    assert len(values) == 3, f"expected 3 distinct confidence values, got {values}"


def test_confidence_falls_back_gracefully_with_no_extra_signals():
    """With only similarity + declared-family match available, confidence is
    the renormalised weighted mean of just those two signals — no crash, no
    forced constant, no hidden zero for the missing ones."""
    status, confidence = _predicted_status(0.7, True, None)
    assert 0.0 <= confidence <= 1.0
    assert abs(confidence - (0.40 * 0.7 + 0.20 * 1.0) / 0.60) < 1e-3


def test_status_always_agrees_with_overall_confidence():
    """v0.4.1 regression: the status is derived FROM the overall confidence,
    so a 'Potential SEP' can never sit next to a confidence in the Essential
    band (the reported 'Potential SEP / 100%' contradiction), and vice versa."""
    from research.three_signal_scoring import (
        essentiality_from_confidence, essentiality_from_status,
    )
    for sim in (0.1, 0.3, 0.45, 0.6, 0.8, 1.0):
        for declared in (True, False):
            for cm in (None, 0.2, 0.7):
                for cov in (None, 0.3, 0.9):
                    status, conf = _predicted_status(
                        sim, declared, None, claim_match=cm, standard_coverage=cov,
                    )
                    assert essentiality_from_status(status) == essentiality_from_confidence(conf), (
                        sim, declared, cm, cov, status, conf)


def test_overall_confidence_is_not_just_semantic_similarity():
    """v0.4.1 regression: on the bulk path Overall Confidence used to be a
    copy of Semantic Similarity. With claim match / coverage / declared
    match in play the two must differ."""
    _, conf = _predicted_status(1.0, False, None, claim_match=0.37, standard_coverage=0.625)
    assert conf < 0.8


def test_verification_failure_visibly_lowers_confidence():
    _, high = _predicted_status(0.8, True, None, verification_ratio=1.0)
    _, low = _predicted_status(0.8, True, None, verification_ratio=0.0)
    assert low < high


def test_collision_flag_applies_a_visible_penalty():
    _, clean = _predicted_status(0.8, True, None, verification_ratio=0.9)
    _, flagged = _predicted_status(0.8, True, None, verification_ratio=0.9, has_collision=True)
    assert flagged < clean


def test_sep_conf_alone_no_longer_pulls_everything_to_the_same_constant():
    """The old bug: a missing sep_conf silently defaulted to 0.5 and was
    blended in at fixed weight regardless, dragging every patent toward the
    same ~0.5-0.56 constant. Two very different similarity scores with no
    sep_conf must now diverge, not converge."""
    _, low_sim = _predicted_status(0.1, False, None)
    _, high_sim = _predicted_status(0.95, True, None)
    assert high_sim - low_sim > 0.5


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
