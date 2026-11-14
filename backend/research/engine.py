"""
Research / Paper-Style Analytics Engine
========================================
Post-processing layer that takes the `patent_disclosures` already produced by
the existing LLM pipeline (see core/prompts.SCHEMA) and augments them with the
outputs described in the two reference papers, using simplified, fully-local
methods:

  Paper 1 ("Identification of Probable SEPs Based on Semantic Analysis of
  Patent Claims"): Doc2Vec + Random Forest SEP/no-SEP classification, then
  nearest-neighbour similarity search to recommend a standard code.
  --> here: sentence embeddings + cosine similarity against a small
      version/clause-aware standards corpus, with a balanced
      same-vs-different-standard evaluation that reports accuracy /
      precision / recall / F1 / Cohen's kappa / confusion matrix exactly as
      in that paper's Tables II-III.

  Paper 2 ("Identifying Potential SEPs Based on Text Mining and GTM"):
  TF-IDF/LDA topics + GTM "vacuum" detection + I-Sim mapping + verification
  against later standard versions.
  --> here: domain-keyword "topic overlap" + cosine similarity "vacuum"
      detection (a patent whose content aligns with a standard/clause it
      hasn't declared, or with a *later* version than the one it declared),
      plus a current-vs-future-version verification table.

Nothing here changes the existing analysis pipeline — `analyze_patents()` is
called *after* the LLM result is parsed, and its output is attached as
`research_outputs` alongside the existing fields.
"""

from __future__ import annotations

import logging
import random
import re
from collections import Counter
from typing import Optional

import numpy as np

from research.standards_corpus import (
    STANDARDS_CORPUS,
    CORPUS_DISCLAIMER,
    all_families,
    clause_groups_by_family,
    family_of,
)
from research.embeddings import cosine_sim_matrix, calibrate_similarity_matrix, calibrate_match_scores, get_research_embedder
from research.etsi_kb_bridge import lookup_etsi_kb_evidence, EtsiKbEvidence
from research.phrase_match import find_matched_phrases
from research.three_signal_scoring import (
    tokenize as _st_tokenize, claim_match_score, standard_coverage_score,
    overall_sep_confidence, essentiality_from_confidence, essentiality_from_status,
    ESSENTIAL, POTENTIAL,
)

logger = logging.getLogger(__name__)

# ── Thresholds (tunable; exposed in `meta` so the UI/report can show them) ──
SIM_CONFIRMED = 0.60     # similarity AND declared-family match -> "Confirmed SEP"
SIM_LIKELY = 0.50        # similarity alone -> "Likely SEP"
SIM_POTENTIAL = 0.35     # similarity alone -> "Potential SEP"
SIMILAR_PATENT_MIN = 0.35  # minimum similarity to report a "similar patent"

# Below SIM_POTENTIAL there's no real evidence of a standard/clause match —
# the nearest-neighbour lookup still returns *some* top entry (argsort always
# picks a best score, even a near-zero one), but it must not be surfaced in
# tables as if it were a confident, well-supported mapping. Defined once here
# and reused by both _predicted_status() and _build_tables() so the two never
# drift out of sync.
STATUS_INSUFFICIENT_EVIDENCE = "Insufficient Evidence"

_VERSION_RE = re.compile(r"v?(\d{1,2}\.\d{1,2}\.\d{1,2})", re.IGNORECASE)


# ── Patent text / metadata helpers ──────────────────────────────────────────

def _stringify_list(items) -> list[str]:
    """
    Coerce a field that's *supposed* to be a list of strings into one,
    defensively. The extraction/enrichment LLM occasionally nests a list or
    dict inside one of these fields instead of a plain string (e.g.
    ["TS 38.331", ["TS 38.213", "TS 38.214"]]) — str.join() has no tolerance
    for that and raises "sequence item N: expected str instance, list
    found", which previously killed the entire research-augmentation pass
    for the affected declaration (caught non-fatally further up the call
    stack, but silently dropping all of research_outputs is still a real
    loss of the deep-enrichment tables/figures). Flattens one level and
    stringifies anything that isn't already a plain string, rather than
    raising.
    """
    if not items:
        return []
    if isinstance(items, (str, dict)):
        items = [items]
    flat: list[str] = []
    for it in items:
        if isinstance(it, (list, tuple, set)):
            flat.extend(str(sub).strip() for sub in it if str(sub).strip())
        elif it is not None and str(it).strip():
            flat.append(str(it).strip())
    return flat


def build_patent_text(disc: dict) -> str:
    """Build the text blob used to embed a patent disclosure for similarity search."""
    ed = disc.get("explicit_data", {}) or {}
    sa = disc.get("standards_analysis", {}) or {}
    sep = disc.get("sep_analysis", {}) or {}
    inf = disc.get("inferred_fields", {}) or {}

    parts: list[str] = []
    title = ed.get("title") or inf.get("probable_title") or ""
    if title:
        parts.append(str(title))
    standards = _stringify_list(ed.get("standards"))
    if standards:
        parts.append("Declared standards: " + ", ".join(standards))
    domains = _stringify_list(d.get("domain", "") for d in (sa.get("technical_domains") or []) if d.get("domain"))
    if domains:
        parts.append("Technical domains: " + ", ".join(domains))
    comps = _stringify_list(sa.get("likely_protocol_components"))
    if comps:
        parts.append("Protocol components: " + ", ".join(comps))
    for sd in sa.get("standard_descriptions") or []:
        if sd.get("description"):
            parts.append(str(sd["description"]))
    if inf.get("probable_technology_focus"):
        parts.append(str(inf["probable_technology_focus"]))
    if inf.get("probable_claim_scope"):
        parts.append(str(inf["probable_claim_scope"]))
    for uc in inf.get("probable_use_cases") or []:
        parts.append(str(uc))
    if sep.get("probable_implementation_scope"):
        parts.append(str(sep["probable_implementation_scope"]))

    text = ". ".join(p.strip() for p in parts if p and str(p).strip())
    return text or "Telecommunications patent disclosure with unspecified technical content."


def patent_phrase_text(disc: dict) -> str:
    """The patent's own descriptive wording, for exact phrase matching
    (research/phrase_match.py): the same sources as build_patent_text() but
    WITHOUT the field labels and the declared standard numbers, so a
    "match" is always shared technical wording, never "TS 38.331" or a
    label like "Technical domains"."""
    ed = disc.get("explicit_data", {}) or {}
    sa = disc.get("standards_analysis", {}) or {}
    sep = disc.get("sep_analysis", {}) or {}
    inf = disc.get("inferred_fields", {}) or {}
    parts: list[str] = []
    for v in (ed.get("title"), inf.get("probable_title")):
        if v:
            parts.append(str(v))
    parts += _stringify_list(sa.get("likely_protocol_components"))
    for sd in sa.get("standard_descriptions") or []:
        if isinstance(sd, dict):
            for k in ("description", "functional_role"):
                if sd.get(k):
                    parts.append(str(sd[k]))
    for k in ("probable_technology_focus", "probable_claim_scope"):
        if inf.get(k):
            parts.append(str(inf[k]))
    parts += _stringify_list(inf.get("probable_use_cases"))
    if sep.get("probable_implementation_scope"):
        parts.append(str(sep["probable_implementation_scope"]))
    # Sentence break between fields so a phrase never runs across two of them.
    return " | ".join(p.strip() for p in parts if p and str(p).strip())


def patent_label(disc: dict, idx: int) -> str:
    """Display label of a disclosure: publication number, else application number, else 'Disclosure
    #n'.
    """
    ed = disc.get("explicit_data", {}) or {}
    return (
        ed.get("publication_number")
        or ed.get("application_number")
        or f"Disclosure #{disc.get('disclosure_number', idx + 1)}"
    )


def declared_families(disc: dict) -> list[str]:
    """3GPP TS family numbers ('38.331') parsed out of explicit_data.standards."""
    ed = disc.get("explicit_data", {}) or {}
    fams: list[str] = []
    for s in ed.get("standards") or []:
        f = family_of(s)
        if f and f not in fams:
            fams.append(f)
    return fams


def declared_version_for_family(disc: dict, family: str) -> Optional[str]:
    """Look for a 'vXX.Y.Z' style version next to the matching standard string."""
    ed = disc.get("explicit_data", {}) or {}
    for s in ed.get("standards") or []:
        if family in s:
            m = _VERSION_RE.search(s)
            if m:
                return f"v{m.group(1)}"
    return None


# ── Per-patent computations ─────────────────────────────────────────────────

def _predicted_status(
    top_score: float, declared_match: bool, sep_conf,
    *,
    claim_match: Optional[float] = None,
    standard_coverage: Optional[float] = None,
    verification_ratio: Optional[float] = None,
    completeness_score: Optional[float] = None,
    ocr_confidence: Optional[float] = None,
    has_collision: bool = False,
) -> tuple[str, float]:
    """
    Returns (status, overall_confidence).

    v0.4.1: overall confidence now means "confidence that this patent is an
    SEP of the matched standard" and is computed by the ONE shared formula in
    `research.three_signal_scoring.overall_sep_confidence` (semantic
    similarity, claim match, standard coverage, declared-family match, and
    the LLM's own SEP-analysis confidence when deep mode produced one), with
    bounded penalties for an unverified or colliding application number.

    The status is then derived FROM that confidence (see
    `essentiality_from_confidence`), so a status and the confidence shown
    next to it can no longer contradict each other (the old code picked the
    status from raw similarity thresholds independently of the blended
    number, which is how "Potential SEP" could sit next to "100%"):

        Essential band  -> "Confirmed SEP" if the declared family matched,
                           else "Likely SEP"
        Potential band  -> "Potential SEP"
        below           -> "Insufficient Evidence"  (= Non-Essential)

    `completeness_score` / `ocr_confidence` are still accepted for call-site
    compatibility but are extraction-QUALITY signals, not SEP evidence; they
    are reported separately as Extraction Confidence and no longer move the
    SEP confidence.
    """
    confidence = overall_sep_confidence(
        semantic_similarity=top_score,
        claim_match=claim_match,
        standard_coverage=standard_coverage,
        declared_family_match=declared_match,
        llm_sep_confidence=sep_conf,
        verification_ratio=verification_ratio,
        has_collision=has_collision,
    )
    band = essentiality_from_confidence(confidence)
    if band == ESSENTIAL:
        return ("Confirmed SEP" if declared_match else "Likely SEP"), confidence
    if band == POTENTIAL:
        return "Potential SEP", confidence
    return STATUS_INSUFFICIENT_EVIDENCE, confidence


def _explanation(top_entry, top_score, patent_text, second, sim_label, sim_score) -> list[str]:
    """'Matched because' bullets for an illustrative-corpus match: similarity to the clause, topic
    overlap, up to two matched keywords, the best clause from another family and the most similar
    other patent.
    """
    bullets = [
        f"{round(top_score * 100)}% semantic similarity to "
        f"{top_entry.standard_version} {top_entry.clause_label}",
        f"{top_entry.domain} topic overlap",
    ]
    text_l = patent_text.lower()
    matched_kw = [kw for kw in top_entry.keywords if kw in text_l]
    for kw in matched_kw[:2]:
        bullets.append(f'"{kw}" keyword overlap')
    if second is not None:
        sec_entry, sec_score = second
        bullets.append(
            f"Secondary alignment with {sec_entry.standard_version} "
            f"{sec_entry.clause_label} ({round(sec_score * 100)}%)"
        )
    if sim_label and sim_score:
        bullets.append(f"Similar to Patent {sim_label} ({round(sim_score * 100)}% match)")
    return bullets


def _explanation_from_etsi_evidence(evidence: EtsiKbEvidence, sim_label, sim_score) -> list[str]:
    """Same explanation-bullet shape as _explanation() above, but grounded
    in a REAL retrieved standards chunk (3GPP Retrieval Index or Standards Corpus; cPARAS audit items
    8-11) instead of the illustrative static corpus -- no keyword-overlap
    bullet here (that mechanism is specific to standards_corpus.py's fixed
    keyword lexicon), but the clause/standard reference and evidence
    snippet are real, source-derived text."""
    clause_part = f" {evidence.clause_title}" if evidence.clause_title and evidence.clause_title != "Unknown" else ""
    bullets = [
        f"{round(evidence.similarity * 100)}% semantic similarity to "
        f"{evidence.standard} {evidence.specification_number} {evidence.clause}{clause_part}".strip(),
        f"{evidence.domain} domain ({evidence.source_label})" if evidence.domain != "Unknown" else f"{evidence.source_label} match",
    ]
    if evidence.evidence_text and evidence.evidence_text != "No evidence found":
        snippet = evidence.evidence_text[:160].strip()
        bullets.append(f"Evidence: \u201c{snippet}{'…' if len(evidence.evidence_text) > 160 else ''}\u201d")
    if sim_label and sim_score:
        bullets.append(f"Similar to Patent {sim_label} ({round(sim_score * 100)}% match)")
    return bullets


async def _current_kb_version() -> str:
    """dataset_version of the installed Standards Corpus package, stored with each result for
    provenance ('Unknown' when none is installed).
    """
    try:
        from etsi_corpus.package import load_package_manifest
        manifest = load_package_manifest()
        return manifest.dataset_version if manifest else "Unknown"
    except Exception:
        return "Unknown"


def _version_evolution(cg_with_sims: list[tuple]) -> dict:
    """Similarity of the patent to every version of its matched clause (chronological), with a trend
    label (increasing / decreasing / stable) - the 'version evolution' trail shown per patent.
    """
    points = [
        {
            "standard_version": e.standard_version,
            "version": e.version,
            "release": e.release,
            "year": e.year,
            "similarity": round(s, 3),
        }
        for e, s in cg_with_sims
    ]
    if len(points) >= 2:
        delta = points[-1]["similarity"] - points[0]["similarity"]
        if delta > 0.03:
            trend, trend_label = "increasing", "Predicted essentiality increased"
        elif delta < -0.03:
            trend, trend_label = "decreasing", "Predicted essentiality decreased"
        else:
            trend, trend_label = "stable", "Predicted essentiality stable"
    else:
        trend, trend_label = "stable", "Insufficient version history"
    first = cg_with_sims[0][0]
    return {
        "standard": first.standard,
        "clause": first.clause_label,
        "clause_title": first.clause_title,
        "points": points,
        "trend": trend,
        "trend_label": trend_label,
    }


def _verification(declared_version: Optional[str], cg_with_sims: list[tuple]) -> dict:
    """Compare the declared (or earliest) version of the matched clause with the next later version and
    label the direction of the difference symmetrically: 'Verified - forward alignment',
    'Predicted', 'Stable' or 'Declining relevance', with a human sentence and the delta.
    """
    entries = [e for e, _ in cg_with_sims]
    sims = {e.version: s for e, s in cg_with_sims}

    cur_idx, source = 0, "earliest-in-corpus (no declared version found)"
    if declared_version:
        for i, e in enumerate(entries):
            if e.version == declared_version:
                cur_idx, source = i, "declared"
                break

    current = entries[cur_idx]
    future = entries[cur_idx + 1] if cur_idx + 1 < len(entries) else None

    if future is None:
        return {
            "current_version": current.standard_version,
            "current_version_source": source,
            "future_version": None,
            "current_similarity": round(sims[current.version], 3),
            "future_similarity": None,
            "verification": "No later reference version available in the corpus.",
            "status": "Stable",
            "delta": 0.0,
        }

    cur_sim, fut_sim = sims[current.version], sims[future.version]
    delta = round(fut_sim - cur_sim, 3)

    # Symmetric magnitude bands, applied the same way regardless of sign, so
    # a +0.03 delta and a -0.03 delta get labels that reflect the same
    # underlying confidence — previously any positive delta (even +0.001)
    # was called "Predicted" while a same-magnitude negative delta up to
    # -0.05 was called "Stable", which produced inconsistent-looking labels
    # for comparably small gaps just because of their sign.
    STABLE_BAND = 0.02   # |delta| below this: no meaningful direction
    STRONG_BAND = 0.05   # |delta| at/above this: confident direction

    if abs(delta) < STABLE_BAND:
        verification = "Comparable alignment across both versions."
        status = "Stable"
    elif delta >= STRONG_BAND:
        verification = (
            f"Content aligns more strongly with {future.standard_version} "
            f"({round(fut_sim * 100)}%) than {current.standard_version} "
            f"({round(cur_sim * 100)}%)."
        )
        status = "Verified - forward alignment"
    elif delta > 0:
        verification = (
            f"Slightly stronger alignment with {future.standard_version} "
            f"({round(fut_sim * 100)}%) than {current.standard_version} "
            f"({round(cur_sim * 100)}%)."
        )
        status = "Predicted"
    elif delta <= -STRONG_BAND:
        verification = (
            f"Alignment concentrated in {current.standard_version} "
            f"({round(cur_sim * 100)}%) vs {future.standard_version} "
            f"({round(fut_sim * 100)}%)."
        )
        status = "Declining relevance"
    else:
        verification = (
            f"Slightly stronger alignment with {current.standard_version} "
            f"({round(cur_sim * 100)}%) than {future.standard_version} "
            f"({round(fut_sim * 100)}%)."
        )
        status = "Declining relevance"

    return {
        "current_version": current.standard_version,
        "current_version_source": source,
        "future_version": future.standard_version,
        "current_similarity": round(cur_sim, 3),
        "future_similarity": round(fut_sim, 3),
        "verification": verification,
        "status": status,
        "delta": delta,
    }


def _potential_sep(top_entry, top_score: float, declared_fams: list[str], verification: dict) -> Optional[dict]:
    """Potential-SEP ('vacuum') detection: either a confident match to a standard the patent did NOT
    declare, or a declared standard whose LATER version matches clearly better. None when neither
    applies.
    """
    # Case A: strong alignment with a standard the patent did NOT declare —
    # a "vacuum" the patent could plausibly fill (Paper 2 vacuum-mapping idea).
    if top_entry.family not in declared_fams and top_score >= SIM_POTENTIAL:
        return {
            "topic": top_entry.domain,
            "reason": (
                f"{round(top_score * 100)}% semantic alignment with "
                f"{top_entry.standard_version} {top_entry.clause_label}, a "
                f"standard not present in this patent's declared standards."
            ),
            "standard": top_entry.standard,
            "missing_standard_section": top_entry.clause_label,
            "predicted_future_version": top_entry.version,
        }
    # Case B: declared standard matches, but content aligns more with a
    # *future* version than the declared one (Requirement 9: version tracking).
    if verification.get("status") == "Verified - forward alignment" and verification.get("future_version"):
        future_ver = verification["future_version"].split(" ")[-1]
        return {
            "topic": top_entry.domain,
            "reason": (
                f"Declared under {verification['current_version']}, but content "
                f"aligns more strongly with {verification['future_version']} "
                f"({round(verification['future_similarity'] * 100)}% vs "
                f"{round(verification['current_similarity'] * 100)}%)."
            ),
            "standard": top_entry.standard,
            "missing_standard_section": top_entry.clause_label,
            "predicted_future_version": future_ver,
        }
    return None


def _similar_patents(
    patent_vecs: np.ndarray,
    labels: list[str],
    extra_vecs: Optional[np.ndarray] = None,
    extra_labels: Optional[list[str]] = None,
) -> list[tuple[Optional[str], Optional[float]]]:
    """For each patent, the most similar other patent in this batch or among earlier declarations
    (calibrated cosine >= SIMILAR_PATENT_MIN), else (None, None).
    """
    n = patent_vecs.shape[0]
    if extra_vecs is not None and getattr(extra_vecs, "size", 0):
        all_vecs = np.vstack([patent_vecs, extra_vecs])
        all_labels = list(labels) + list(extra_labels or [])
    else:
        all_vecs = patent_vecs
        all_labels = list(labels)

    sims = cosine_sim_matrix(patent_vecs, all_vecs)
    sims, _calib_meta = calibrate_similarity_matrix(sims)
    out: list[tuple[Optional[str], Optional[float]]] = []
    for i in range(n):
        row = sims[i].copy()
        if i < row.shape[0]:
            row[i] = -1.0
        j = int(np.argmax(row)) if row.size else -1
        score = float(row[j]) if j >= 0 else -1.0
        if score >= SIMILAR_PATENT_MIN:
            out.append((all_labels[j], round(score, 3)))
        else:
            out.append((None, None))
    return out


# ── Aggregate metrics & tables ──────────────────────────────────────────────

def _compute_metrics(records: list[dict], sims_to_corpus: np.ndarray, corpus, threshold: float, seed: int = 42) -> dict:
    """Balanced evaluation in the style of the reference paper: for each declared family, does
    similarity clear the threshold (positive) versus a random other family (negative)? Returns
    accuracy, precision, recall, F1, Cohen's kappa and the confusion matrix, or available=False when
    no declared family is in the corpus.
    """
    try:
        from sklearn.metrics import (
            accuracy_score, cohen_kappa_score, confusion_matrix, f1_score,
            precision_score, recall_score,
        )
    except Exception as e:  # pragma: no cover - scikit-learn always required by requirements.txt
        return {"available": False, "reason": f"scikit-learn unavailable: {e}"}

    families = sorted({e.family for e in corpus})
    fam_idx = {f: i for i, f in enumerate(families)}
    fam_sims = np.zeros((sims_to_corpus.shape[0], len(families)), dtype="float32")
    for fi, fam in enumerate(families):
        cols = [i for i, e in enumerate(corpus) if e.family == fam]
        fam_sims[:, fi] = sims_to_corpus[:, cols].max(axis=1)

    rng = random.Random(seed)
    y_true: list[int] = []
    y_pred: list[int] = []
    pairs: list[dict] = []

    for pi, rec in enumerate(records):
        declared = [f for f in rec["_declared_families"] if f in fam_idx]
        for fam in declared:
            pos_sim = float(fam_sims[pi, fam_idx[fam]])
            y_true.append(1)
            y_pred.append(1 if pos_sim >= threshold else 0)
            pairs.append({"patent": rec["patent_label"], "family": fam, "role": "declared", "similarity": round(pos_sim, 3)})

            others = [f for f in families if f != fam]
            if others:
                neg_fam = others[rng.randrange(len(others))]
                neg_sim = float(fam_sims[pi, fam_idx[neg_fam]])
                y_true.append(0)
                y_pred.append(1 if neg_sim >= threshold else 0)
                pairs.append({"patent": rec["patent_label"], "family": neg_fam, "role": "control", "similarity": round(neg_sim, 3)})

    if not y_true:
        return {"available": False, "reason": "No patents had declared standards present in the reference corpus families."}

    cm = confusion_matrix(y_true, y_pred, labels=[0, 1]).tolist()
    try:
        kappa = float(cohen_kappa_score(y_true, y_pred))
    except Exception:
        kappa = 0.0

    return {
        "available": True,
        "n_pairs": len(y_true),
        "threshold": threshold,
        "accuracy": round(accuracy_score(y_true, y_pred), 3),
        "precision": round(precision_score(y_true, y_pred, zero_division=0), 3),
        "recall": round(recall_score(y_true, y_pred, zero_division=0), 3),
        "f1": round(f1_score(y_true, y_pred, zero_division=0), 3),
        "cohens_kappa": round(kappa, 3),
        "confusion_matrix": {
            "labels": ["Other standard (control)", "Declared standard"],
            "matrix": cm,
            "tn": cm[0][0], "fp": cm[0][1], "fn": cm[1][0], "tp": cm[1][1],
        },
        "sample_pairs": pairs[:20],
        "methodology": (
            "Balanced same-vs-different-standard classification, mirroring the "
            "50% SEP / 50% no-SEP evaluation set-up in Paper 1 (Fig. 1) and the "
            "accuracy / Cohen's-kappa reporting in its Tables II-III. For each "
            "patent and each of its declared standard families, the engine checks "
            "whether semantic similarity to that family's reference clauses clears "
            "the similarity threshold (expected positive), and compares this "
            "against a randomly chosen *different* standard family for the same "
            "patent (expected negative)."
        ),
    }


def _aggregate_stats(records: list[dict]) -> dict:
    """Batch-level summary: averages, status distribution, top standards / versions / applicants /
    domains.
    """
    if not records:
        return {}
    sims = [r["similarity_score"] for r in records]
    confs = [r["confidence_score"] for r in records]
    return {
        "n_patents": len(records),
        "average_similarity": round(sum(sims) / len(sims), 3),
        "average_confidence": round(sum(confs) / len(confs), 3),
        "n_potential_seps": sum(1 for r in records if r["is_potential_sep"]),
        "status_distribution": dict(Counter(r["predicted_sep_status"] for r in records)),
        "top_standards": Counter(r["recommended_standard"] for r in records).most_common(5),
        "top_versions": Counter(r["recommended_version"] for r in records).most_common(5),
        "top_applicants": Counter(r["applicant"] for r in records if r["applicant"] and r["applicant"] != "—").most_common(5),
        "top_domains": Counter(r["domain"] for r in records).most_common(5),
    }


def _build_tables(records: list[dict]) -> dict:
    """The four research tables shown in the UI and reports (classification, standard mapping incl.
    matched phrases, potential SEPs, version verification). Rows with 'Insufficient Evidence' are
    left out of the mapping/verification tables so a noise-level match never looks like a real one.
    """
    classification = [
        {
            "patent": r["patent_label"],
            "probability": r["similarity_score"],
            "predicted_sep": r["predicted_sep_status"],
            # A nearest-neighbour lookup always returns *a* top entry, even
            # when its similarity is noise-level. Don't print that entry's
            # standard/version as a specific fact next to "Insufficient
            # Evidence" — that made every low-confidence guess look identical
            # to a well-supported match.
            "recommended_standard": (
                r["recommended_standard"]
                if r["predicted_sep_status"] != STATUS_INSUFFICIENT_EVIDENCE
                else None
            ),
            "version": (
                r["recommended_version"]
                if r["predicted_sep_status"] != STATUS_INSUFFICIENT_EVIDENCE
                else None
            ),
            "confidence": r["confidence_score"],
        }
        for r in records
    ]

    # Same confidence gate as classification, applied by exclusion instead of
    # nulling fields: a patent whose best match never cleared SIM_POTENTIAL
    # has no real "matched standard" to report, so it has no row here at all
    # — mirroring how potential_seps below is already filtered on
    # is_potential_sep, rather than showing a specific standard/clause that
    # renders identically to a genuine match.
    standard_mapping = [
        {
            "patent": r["patent_label"],
            "matched_standard": f"{r['recommended_standard']} {r['recommended_version']}",
            "matched_clause": r["standard_clause"],
            "matched_clause_title": r.get("standard_clause_title"),
            "similarity": r["similarity_score"],
            "matched_phrases": r.get("matched_phrases") or [],
            "matched_terms": r.get("matched_terms") or [],
            "reason": r["explanation"][0] if r["explanation"] else "",
        }
        for r in records
        if r["predicted_sep_status"] != STATUS_INSUFFICIENT_EVIDENCE
    ]

    potential_seps = [
        {
            "patent": r["patent_label"],
            "topic": r["potential_sep"]["topic"],
            "reason": r["potential_sep"]["reason"],
            "missing_standard_section": f"{r['potential_sep']['standard']} {r['potential_sep']['missing_standard_section']}",
            "predicted_future_version": r["potential_sep"]["predicted_future_version"],
        }
        for r in records
        if r["is_potential_sep"]
    ]

    # Version-drift verification is computed relative to the same top_entry
    # match as standard_mapping above, so it inherits the same gate: no point
    # reporting a version trend against a standard the patent isn't
    # meaningfully aligned with in the first place.
    verification = [
        {
            "patent": r["patent_label"],
            "current_version": r["verification"]["current_version"],
            "future_version": r["verification"]["future_version"],
            "verification": r["verification"]["verification"],
            "status": r["verification"]["status"],
        }
        for r in records
        if r["predicted_sep_status"] != STATUS_INSUFFICIENT_EVIDENCE
    ]

    return {
        "patent_classification": classification,
        "standard_mapping": standard_mapping,
        "potential_seps": potential_seps,
        "verification": verification,
    }


def _empty_outputs(threshold: float) -> dict:
    """research_outputs shape for a declaration with no disclosures."""
    return {
        "meta": {
            "embedder_mode": "n/a",
            "corpus_size": len(STANDARDS_CORPUS),
            "corpus_families": all_families(),
            "similarity_threshold": threshold,
            "disclaimer": CORPUS_DISCLAIMER,
            "n_patents": 0,
        },
        "records": [],
        "tables": {"patent_classification": [], "standard_mapping": [], "potential_seps": [], "verification": []},
        "metrics": {"available": False, "reason": "No patent disclosures to analyze."},
        "aggregates": {},
        "figures": None,
    }


# ── Public entry point ──────────────────────────────────────────────────────

async def analyze_patents(
    disclosures: list[dict],
    prior_patents: Optional[list[dict]] = None,
    similarity_threshold: float = SIM_LIKELY,
    generate_figures: bool = True,
    ocr_confidence: Optional[float] = None,
) -> dict:
    """
    Run the full research/paper-style analysis on a batch of patent
    disclosures (the `patent_disclosures` list from the LLM pipeline schema).

    `prior_patents`: optional list of `{"label": str, "text": str}` dicts for
    patents from *other* declarations already in the database, so "similar
    patent" search can look across the whole corpus, not just this batch.

    `ocr_confidence`: document-level mean OCR word-confidence (0-100), if
    this declaration went through an OCR tier — one of the signals blended
    into each patent's confidence_score (see _predicted_status). None for
    native-text-layer documents (no OCR confidence to report).

    Returns the `research_outputs` dict attached to the declaration JSON.
    """
    if not disclosures:
        return _empty_outputs(similarity_threshold)

    embedder = get_research_embedder()

    corpus_texts = [e.embedding_text() for e in STANDARDS_CORPUS]
    patent_texts = [build_patent_text(d) for d in disclosures]
    prior_patents = prior_patents or []
    prior_texts = [p.get("text", "") for p in prior_patents]

    all_vecs = await embedder.embed_texts(corpus_texts + patent_texts + prior_texts)

    n_corpus, n_patents = len(corpus_texts), len(patent_texts)
    corpus_vecs = all_vecs[:n_corpus]
    patent_vecs = all_vecs[n_corpus:n_corpus + n_patents]
    prior_vecs = all_vecs[n_corpus + n_patents:]

    # ── v0.3-continuation fix (cPARAS audit items 8-11): look up REAL
    #    retrieved evidence (3GPP Retrieval Index, else Standards Corpus) for
    #    every patent BEFORE falling back to the illustrative static corpus
    #    below. Each patent independently uses whichever source actually has
    #    relevant material -- a retrieval lookup miss for one patent doesn't force the whole batch onto the
    #    fallback, and the illustrative corpus is never silently treated
    #    as authoritative once real evidence exists for a given patent. ──
    etsi_evidence = await lookup_etsi_kb_evidence(patent_texts)
    knowledge_base_version = await _current_kb_version()

    sims_to_corpus = cosine_sim_matrix(patent_vecs, corpus_vecs)  # (N_patents, N_corpus)
    # ADDITIONAL ISSUE (similarity score): calibrate raw cosine similarities
    # into a statistically meaningful 0-1 range before anything downstream
    # (top_score, version-evolution points, verification, explanation
    # percentages, metrics) uses them — see
    # research.embeddings.calibrate_similarity_matrix's docstring for why
    # the raw values alone produced the reported "0% or 50% for almost
    # everything" pathology and why percentile rescaling (not an arbitrary
    # fixed constant) is the statistically sound fix. `calib_meta` is
    # exposed in the run's `meta` below so the calibration anchors are
    # auditable rather than a hidden transform.
    # v0.4.1: floor-to-unity calibration -- the old 5th/95th-percentile
    # rescale clipped every patent's BEST match to 100% (see
    # research.embeddings.calibrate_match_scores).
    sims_to_corpus, calib_meta = calibrate_match_scores(sims_to_corpus)
    corpus_index_by_id = {e.id: i for i, e in enumerate(STANDARDS_CORPUS)}

    labels = [patent_label(d, i) for i, d in enumerate(disclosures)]
    prior_labels = [p.get("label", "") for p in prior_patents]
    similar = _similar_patents(
        patent_vecs, labels,
        prior_vecs if prior_vecs.size else None,
        prior_labels if prior_labels else None,
    )

    records: list[dict] = []
    for i, disc in enumerate(disclosures):
        ed = disc.get("explicit_data", {}) or {}
        sep = disc.get("sep_analysis", {}) or {}
        inf = disc.get("inferred_fields", {}) or {}
        text = patent_texts[i]
        sims_row = sims_to_corpus[i]
        order = np.argsort(-sims_row)

        top_entry = STANDARDS_CORPUS[int(order[0])]
        top_score = float(sims_row[int(order[0])])

        second = None
        for j in order[1:]:
            cand = STANDARDS_CORPUS[int(j)]
            if cand.family != top_entry.family:
                second = (cand, float(sims_row[int(j)]))
                break

        d_fams = declared_families(disc)
        declared_match = top_entry.family in d_fams

        # ── v0.3-continuation: prefer REAL retrieved evidence for this
        #    patent, if the lookup above found something above the
        #    minimum score. Falls back to the illustrative corpus's
        #    top_entry/top_score computed above only when retrieval had
        #    nothing relevant (or isn't installed) for THIS patent. ──
        kb_hit: Optional[EtsiKbEvidence] = etsi_evidence[i] if i < len(etsi_evidence) else None
        evidence_source = "illustrative_reference_corpus"
        evidence_text = "No evidence found"
        if kb_hit is not None:
            evidence_source = "etsi_knowledge_base"
            evidence_text = kb_hit.evidence_text
            top_score = kb_hit.similarity
            # Declared-match check against real evidence: does the
            # retrieved specification number correspond to any of this
            # patent's own declared standard strings? Compared as
            # digits-only (e.g. "TS 138 331" -> "138331", "138 331" ->
            # "138331") because real declared-standard strings and ETSI
            # specification numbers are formatted inconsistently (spacing,
            # "TS " prefixes, dots vs spaces) -- family_of() above only
            # recognises 3GPP's dotted style ("38.331") and returns
            # nothing for ETSI's spaced style, so it can't be reused here.
            # This is a SIGNAL for the Confirmed-vs-Likely threshold, not
            # a legal determination.
            def _digits_only(s: str) -> str:
                return "".join(ch for ch in s if ch.isdigit())
            kb_spec_digits = _digits_only(kb_hit.specification_number) if kb_hit.specification_number != "Unknown" else ""
            declared_raw = ed.get("standards") or []
            declared_match = bool(kb_spec_digits) and any(
                kb_spec_digits in _digits_only(str(s)) or _digits_only(str(s)) in kb_spec_digits
                for s in declared_raw if _digits_only(str(s))
            )

        # Additional signals for confidence (see _predicted_status): pulled
        # from the per-field verification (ISSUE 1/10) and row-completeness
        # (ISSUE 9) results already attached to this disclosure by
        # api/routes/analysis.py, when present. Both are optional so this
        # function still works standalone (e.g. the bundled demo script)
        # against disclosures that never went through that pipeline.
        verification = disc.get("inferred_fields", {}).get("extraction_verification")
        verification_ratio = None
        if verification and verification.get("any_checked"):
            checked = [v for v in [verification.get("application_number"), verification.get("publication_number")] if v]
            checked += verification.get("standards") or []
            if checked:
                verification_ratio = sum(1 for v in checked if v.get("verified")) / len(checked)
        completeness = disc.get("inferred_fields", {}).get("completeness")
        completeness_score = completeness.get("score") if completeness else None
        has_collision = bool(disc.get("_possible_number_collision"))

        patent_toks = _st_tokenize(text)
        claim_match = claim_match_score(
            patent_tokens=patent_toks,
            clause_title=top_entry.clause_title,
            clause_keywords=top_entry.keywords,
            semantic_similarity=top_score,
            explicit_disclosure=declared_match,
        )
        standard_coverage = standard_coverage_score(
            semantic_similarity=top_score,
            clause_group_count=len(clause_groups_by_family(top_entry.family)),
            declared_family_match=declared_match,
        )
        status, confidence = _predicted_status(
            top_score, declared_match, sep.get("confidence"),
            claim_match=claim_match,
            standard_coverage=standard_coverage,
            verification_ratio=verification_ratio,
            completeness_score=completeness_score,
            ocr_confidence=ocr_confidence,
            has_collision=has_collision,
        )

        sim_label, sim_score = similar[i]
        potential = None

        # ── v0.5: mandatory three-signal fields -- claim_match and
        #    standard_coverage, computed via the SAME shared formulas GITAS
        #    uses (research/three_signal_scoring.py), fed with the richer
        #    evidence this pipeline actually has: the full assembled patent
        #    text (title + declared standards + technical domains + LLM-
        #    extracted standard descriptions + probable claim scope + use
        #    cases -- see build_patent_text() above), not just a title.
        #    `semantic_similarity` is intentionally NOT a new field here --
        #    it IS `similarity_score`/`top_score` below (the same calibrated
        #    cosine value), just under a second, clearer export name; see
        #    core.database.Patent's `claim_match`/`standard_coverage`
        #    column docstring for that mapping. Always grounded in
        #    `top_entry` (the illustrative corpus's own matched clause) for
        #    the lexical-overlap/clause-breadth terms even on the kb_hit
        #    (real-evidence) path, since the retrieval layer doesn't
        #    expose a clause-group breadth concept the way the illustrative
        #    corpus's `clause_groups_by_family` does -- top_entry stays
        #    available as that fallback structural reference regardless of
        #    which evidence source produced the *label* fields above.

        if kb_hit is not None:
            # ── real-evidence path ──
            recommended_standard = kb_hit.standard
            recommended_version = kb_hit.version
            standard_clause = kb_hit.clause
            standard_clause_title = kb_hit.clause_title
            domain = kb_hit.domain
            explanation = _explanation_from_etsi_evidence(kb_hit, sim_label, sim_score)
            # Version-evolution / future-version verification tables are
            # specific to standards_corpus.py's hand-curated chronological
            # clause-version chains -- the retrieval layer doesn't yet
            # index multiple versions of the same clause in a queryable,
            # chronology-aware way, so honestly report "not available"
            # rather than fabricating an evolution table from real data
            # that doesn't actually support it.
            evolution = {}
            verification = {
                "current_version": kb_hit.version, "current_version_source": "etsi_knowledge_base",
                "future_version": None, "current_similarity": kb_hit.similarity, "future_similarity": None,
                "verification": f"Version-evolution tracking is not yet available for evidence retrieved from the {kb_hit.source_label} (requires the illustrative reference corpus's curated clause history).",
                "status": "Not Available", "delta": 0.0,
            }
        else:
            # ── illustrative-corpus fallback path (unchanged) ──
            cg_entries = clause_groups_by_family(top_entry.family).get(top_entry.clause_group, [top_entry])
            cg_with_sims = [(e, float(sims_row[corpus_index_by_id[e.id]])) for e in cg_entries]
            evolution = _version_evolution(cg_with_sims)
            declared_version = declared_version_for_family(disc, top_entry.family)
            verification = _verification(declared_version, cg_with_sims)
            explanation = _explanation(top_entry, top_score, text, second, sim_label, sim_score)
            potential = _potential_sep(top_entry, top_score, d_fams, verification)
            recommended_standard = top_entry.standard
            recommended_version = top_entry.version
            standard_clause = top_entry.clause_label
            standard_clause_title = top_entry.clause_title
            domain = top_entry.domain

        # v0.4.9: exact wording the patent shares with the matched clause.
        if kb_hit is not None:
            clause_text = ". ".join(t for t in (
                kb_hit.clause_title if kb_hit.clause_title != "Unknown" else "",
                kb_hit.full_text or kb_hit.evidence_text,
            ) if t and t != "No evidence found")
        else:
            clause_text = f"{top_entry.clause_title}. {top_entry.text}"
        phrase_match = find_matched_phrases(patent_phrase_text(disc), clause_text)

        records.append({
            "index": i,
            "patent_label": labels[i],
            "title": ed.get("title") or inf.get("probable_title") or "—",
            "applicant": ed.get("applicant") or "—",
            "filing_date": None,
            "publication_date": None,
            "etsi_declaration": True,
            "declared_standards": ed.get("standards") or [],
            "_declared_families": d_fams,
            "declared_sep_status": "Declared" if ed.get("standards") else "Undeclared (Blanket)",
            "predicted_sep_status": status,
            "confidence_score": confidence,
            "overall_confidence": confidence,  # v0.4.1: confidence that this is an SEP (same value as confidence_score)
            "essentiality": essentiality_from_status(status),  # Essential / Potential / Non-Essential
            "recommended_standard": recommended_standard,
            "recommended_version": recommended_version,
            "standard_clause": standard_clause,
            "standard_clause_title": standard_clause_title,
            "domain": domain,
            "similarity_score": round(top_score, 3),
            "semantic_similarity": round(top_score, 3),  # same value, clearer export name -- see comment above
            "claim_match": round(claim_match, 4),
            "standard_coverage": round(standard_coverage, 4),
            "similar_patent": sim_label,
            "similar_patent_score": sim_score,
            "explanation": explanation,
            "matched_phrases": phrase_match["phrases"],   # v0.4.9: exact shared word sequences
            "matched_terms": phrase_match["terms"],       # v0.4.9: shared single technical words
            "version_evolution": evolution,
            "verification": verification,
            "is_potential_sep": potential is not None,
            "potential_sep": potential,
            # v0.3-continuation provenance (cPARAS audit item 13/14):
            "evidence_source": evidence_source,
            "evidence_text": evidence_text,
            "knowledge_base_version": knowledge_base_version,
            "etsi_document_id": kb_hit.etsi_document_id if kb_hit else None,
        })

    metrics = _compute_metrics(records, sims_to_corpus, STANDARDS_CORPUS, threshold=similarity_threshold)
    aggregates = _aggregate_stats(records)
    tables = _build_tables(records)

    figures = None
    if generate_figures:
        try:
            from research import visualizations as viz
            figures = viz.build_all_figures(records, patent_vecs, corpus_vecs, STANDARDS_CORPUS, sims_to_corpus)
        except Exception as e:
            logger.warning(f"Research figure generation failed (non-fatal): {e}")
            figures = {"error": str(e)}

    return {
        "meta": {
            "embedder_mode": embedder.mode,
            "corpus_size": len(STANDARDS_CORPUS),
            "corpus_families": all_families(),
            "similarity_threshold": similarity_threshold,
            "disclaimer": CORPUS_DISCLAIMER,
            "n_patents": len(records),
            "similarity_calibration": calib_meta,
            # v0.3-continuation (cPARAS audit items 8-11): how many of
            # this run's patents actually used real retrieved evidence
            # evidence vs. the illustrative fallback -- surfaced so the UI
            # (and this function's own honesty) never implies retrieval was
            # used for every patent when the index was empty/partial.
            "knowledge_base_version": knowledge_base_version,
            "n_patents_using_etsi_kb": sum(1 for r in records if r["evidence_source"] == "etsi_knowledge_base"),
            "n_patents_using_illustrative_corpus": sum(1 for r in records if r["evidence_source"] == "illustrative_reference_corpus"),
        },
        "records": records,
        "tables": tables,
        "metrics": metrics,
        "aggregates": aggregates,
        "figures": figures,
    }
