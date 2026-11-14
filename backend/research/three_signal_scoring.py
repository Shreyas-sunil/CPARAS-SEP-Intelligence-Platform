"""
Shared Three-Signal Scoring Utilities
======================================
Both ingestion paths — GITAS bulk-export rows (etsi_bulk/scoring.py) and
cPARAS PDF/CSV/XLSX declarations (research/engine.py) — surface the same
"Score Breakdown" panel in the UI: Claim Match / Semantic Similarity /
Standard Coverage. `semantic_similarity` is just the calibrated cosine
similarity to the best-matching corpus entry, already computed identically
by both callers via `research.embeddings.calibrate_similarity_matrix`. This
module holds the ONE authoritative implementation of the other two derived
signals, so GITAS and CPARAS can't silently drift into two different
definitions of "claim match" or "standard coverage".

Both pipelines call the SAME functions below. They differ only in what
evidence they have available to pass in:

  - GITAS bulk rows: title + illustrative part + a Yes/No explicit-
    disclosure flag. No claim text.
  - CPARAS declarations: LLM-extracted standard descriptions, probable
    claim scope, and use cases (research/engine.py's `build_patent_text`
    already assembles a richer text blob) plus a genuine per-field
    confidence score from the extraction model.

Each pipeline builds its own "patent evidence text" and passes it in — the
field DEFINITIONS and the 0.0-1.0 normalized output contract are shared;
the INPUT EVIDENCE is not forced to be identical, because it genuinely
isn't (see module docstrings on both callers).

CLAIM MATCH: how strongly the patent's claim/disclosed subject matter
  corresponds to the matched standard clause. Heuristic blend of:
    (a) an explicit "declarant confirms this discloses the standard" signal
        (weight 0.20),
    (b) lexical (Jaccard) token overlap between the patent's own evidence
        text and the matched clause's title + domain keywords (weight 0.45),
    (c) the semantic similarity itself as a smoothing term so a very strong
        embedding match isn't zeroed out by lexical-overlap noise
        (weight 0.35).

STANDARD COVERAGE: how much of the relevant standard/family/clause scope is
  represented by the matched evidence. Heuristic blend of:
    (a) the semantic similarity itself (weight 0.50),
    (b) inverse breadth of the matched family's known clause groups — a
        single matched clause represents less of a family that has many
        tracked clause groups than one that has only one (weight 0.25),
    (c) a fixed bonus when the declared standard/version actually matches
        (or is adjacent to) the matched corpus entry's family (0.25).

Both are heuristic estimates over an illustrative offline corpus (see
`research.standards_corpus.CORPUS_DISCLAIMER`) and are surfaced with the
same "not a legal opinion" caveat used elsewhere in the app. Neither
function ever fabricates a value when there is no matched entry at all —
callers are expected to only call these once a best-match corpus entry has
been established for the row (i.e. after top-1 argmax over the calibrated
similarity), and to store NULL rather than call these for a row whose
analysis failed outright.
"""

from __future__ import annotations

import re
from typing import Optional

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def tokenize(text: Optional[str]) -> set[str]:
    """Lower-case alphanumeric token set."""
    return set(_TOKEN_RE.findall((text or "").lower()))


def jaccard(a: set[str], b: set[str]) -> float:
    """Jaccard overlap of two token sets (0 when either is empty)."""
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def claim_match_score(
    *,
    patent_tokens: set[str],
    clause_title: Optional[str],
    clause_keywords: list[str],
    semantic_similarity: float,
    explicit_disclosure: bool,
) -> float:
    """0.0-1.0. See module docstring for the weighting rationale."""
    clause_tokens = tokenize(clause_title) | set(w for kw in (clause_keywords or []) for w in kw.split())
    lexical = jaccard(patent_tokens, clause_tokens)
    score = (
        0.20 * (1.0 if explicit_disclosure else 0.0)
        + 0.45 * lexical
        + 0.35 * max(0.0, min(1.0, semantic_similarity))
    )
    return max(0.0, min(1.0, score))


def standard_coverage_score(
    *,
    semantic_similarity: float,
    clause_group_count: int,
    declared_family_match: bool,
) -> float:
    """0.0-1.0. See module docstring for the weighting rationale."""
    breadth = 1.0 / max(1, clause_group_count)  # a single matched clause covers less of a broad family
    version_bonus = 0.25 if declared_family_match else 0.0
    score = 0.50 * max(0.0, min(1.0, semantic_similarity)) + 0.25 * (1 - breadth) + version_bonus
    return max(0.0, min(1.0, score))


def matched_key_terms(patent_tokens: set[str], clause_keywords: list[str]) -> list[str]:
    """Individual matched keyword PHRASES (not just tokens) — what the UI
    highlights and what the 'N of M key technical terms aligned' caption
    counts. Shared so GITAS and CPARAS report this consistently too."""
    return [phrase for phrase in (clause_keywords or []) if all(w in patent_tokens for w in phrase.split())]


# ── Overall (SEP) confidence + essentiality classification ──────────────────
#
# WHY THIS EXISTS (v0.4.1 fix): "Overall Confidence" used to be whatever
# `research.engine._predicted_status` blended, and on the CPARAS Bulk path
# the ONLY signal available to that blend was semantic similarity — so the
# "Overall Confidence" shown in the UI and exported as
# CPARAS_Bulk_Extraction_Confidence was literally a copy of Semantic
# Similarity. Separately, the Essential/Potential/Non-Essential label was
# decided by its own similarity thresholds (plus a legal-status downgrade)
# that never touched the confidence number, so the two could disagree —
# e.g. "Potential SEP" shown next to "Overall Confidence 100%".
#
# Overall confidence is now ONE number answering "how confident are we
# that this patent is essential to the matched standard?", built from the
# SEP evidence the row actually has, and the essentiality label is a pure
# function of that number — so a label and its confidence can never
# contradict each other again.
#
# Extraction quality (how accurately fields were read out of a PDF —
# completeness, OCR quality) is deliberately NOT part of this number; it is
# reported separately as Extraction Confidence. The two exceptions below are
# applied as bounded penalties because they cast doubt on whether the
# evidence even belongs to THIS patent (an unverified or colliding
# application number), not on how well fields were extracted.

ESSENTIAL_MIN_CONFIDENCE = 0.60   # overall confidence >= this -> "Essential"
POTENTIAL_MIN_CONFIDENCE = 0.40   # overall confidence >= this -> "Potential"; below -> "Non-Essential"
HIGH_CONFIDENCE = 0.75            # strong enough to stand against a conflicting declaration

ESSENTIAL = "Essential"
POTENTIAL = "Potential"
NON_ESSENTIAL = "Non-Essential"
ESSENTIALITY_LABELS = (ESSENTIAL, POTENTIAL, NON_ESSENTIAL)

# Weights over the SEP-evidence signals. Renormalised over whichever
# signals are actually present for a row, so a missing optional signal
# (e.g. no LLM SEP-analysis on the bulk path) never acts as a hidden zero.
_CONF_WEIGHTS = {
    "semantic_similarity": 0.40,
    "claim_match": 0.25,
    "standard_coverage": 0.15,
    "declared_family_match": 0.20,
    "llm_sep_confidence": 0.20,
}


def _clip01(v: float) -> float:
    """Clamp to [0, 1]."""
    return max(0.0, min(1.0, float(v)))


def overall_sep_confidence(
    *,
    semantic_similarity: float,
    claim_match: Optional[float] = None,
    standard_coverage: Optional[float] = None,
    declared_family_match: Optional[bool] = None,
    llm_sep_confidence: Optional[float] = None,
    verification_ratio: Optional[float] = None,
    has_collision: bool = False,
) -> float:
    """0.0-1.0 confidence that the patent is essential to the matched
    standard. Weighted mean of the available evidence signals (see
    `_CONF_WEIGHTS`), then bounded penalties for identity red flags."""
    signals = [("semantic_similarity", _clip01(semantic_similarity))]
    if claim_match is not None:
        signals.append(("claim_match", _clip01(claim_match)))
    if standard_coverage is not None:
        signals.append(("standard_coverage", _clip01(standard_coverage)))
    if declared_family_match is not None:
        signals.append(("declared_family_match", 1.0 if declared_family_match else 0.0))
    if isinstance(llm_sep_confidence, (int, float)):
        signals.append(("llm_sep_confidence", _clip01(llm_sep_confidence)))

    total_w = sum(_CONF_WEIGHTS[k] for k, _ in signals)
    conf = sum(_CONF_WEIGHTS[k] * v for k, v in signals) / total_w

    if has_collision:
        conf *= 0.85
    if verification_ratio is not None and verification_ratio < 0.5:
        conf *= 0.90
    return round(_clip01(conf), 4)


def essentiality_from_confidence(confidence: Optional[float]) -> Optional[str]:
    """Essential / Potential / Non-Essential, decided ONLY by the overall
    confidence so the label always agrees with the number shown next to it.
    None in -> None out (row not analysed)."""
    if confidence is None:
        return None
    if confidence >= ESSENTIAL_MIN_CONFIDENCE:
        return ESSENTIAL
    if confidence >= POTENTIAL_MIN_CONFIDENCE:
        return POTENTIAL
    return NON_ESSENTIAL


def essentiality_from_status(status: Optional[str]) -> Optional[str]:
    """Map the CPARAS ISLD status vocabulary onto the three-way label.
    Confirmed/Likely SEP are both the "Essential" band (they differ only by
    whether the declared family matched); see research.engine._predicted_status."""
    if not status:
        return None
    s = status.strip().lower()
    if s in ("confirmed sep", "likely sep", "essential"):
        return ESSENTIAL
    if s in ("potential sep", "potential"):
        return POTENTIAL
    if s in ("insufficient evidence", "non-essential", "not essential", "non essential"):
        return NON_ESSENTIAL
    return None
