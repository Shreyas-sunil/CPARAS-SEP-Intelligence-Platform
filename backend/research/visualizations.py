"""
Research Visualizations
========================
Produces the "publication-quality" figures requested in Requirements 7 & 10:
patent similarity scatter, standard clustering map, patent-topic heatmap,
similarity matrix, patent-standard network graph, standards timeline, version
evolution diagram, confidence/similarity distribution, and standard/version
frequency charts.

Each figure is rendered with matplotlib (Agg backend — no display needed,
fully offline) and returned as a `data:image/png;base64,...` string so the
frontend can drop it straight into an `<img src=...>` with zero new
dependencies, and so it can be embedded directly into generated reports.

`build_all_figures()` is defensive: any individual figure that can't be built
(e.g. too few patents for a 2D projection) is simply omitted rather than
raising, so a partial figure set never breaks the overall research response.
"""

from __future__ import annotations

import base64
import io
from collections import Counter
from typing import Optional

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import Patch  # noqa: E402
import numpy as np  # noqa: E402

from research.embeddings import cosine_sim_matrix  # noqa: E402
from research.standards_corpus import (  # noqa: E402
    clause_groups_by_family,
    family_of,
    representative_entry,
)


STATUS_COLORS = {
    "Confirmed SEP": "#1f3a5f",
    "Likely SEP": "#3d6b8a",
    "Potential SEP": "#c97c2c",
    "Insufficient Evidence": "#b0b0b0",
}

_STYLE_APPLIED = False


def _set_style() -> None:
    """Apply the publication-style matplotlib defaults once per process."""
    global _STYLE_APPLIED
    if _STYLE_APPLIED:
        return
    plt.rcParams.update({
        "figure.facecolor": "white",
        "axes.facecolor": "white",
        "savefig.facecolor": "white",
        "axes.edgecolor": "#444444",
        "axes.labelcolor": "#222222",
        "text.color": "#222222",
        "xtick.color": "#444444",
        "ytick.color": "#444444",
        "axes.grid": True,
        "grid.color": "#e5e5e5",
        "grid.linewidth": 0.6,
        "font.size": 9,
        "axes.titlesize": 11,
        "axes.titleweight": "bold",
        "axes.spines.top": False,
        "axes.spines.right": False,
    })
    _STYLE_APPLIED = True


def _fig_to_b64(fig) -> str:
    """Render a figure to a PNG data URI (and close it) for embedding in JSON/HTML reports."""
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return "data:image/png;base64," + base64.b64encode(buf.read()).decode("ascii")


def _short_label(label: str, maxlen: int = 14) -> str:
    """Truncate an axis label with an ellipsis."""
    label = label or "—"
    return label if len(label) <= maxlen else label[: maxlen - 1] + "…"


def _pca_2d(x: np.ndarray) -> np.ndarray:
    """2-D PCA projection of vectors for scatter plots (zeros when there are too few points)."""
    n = x.shape[0]
    if n < 2 or x.shape[1] < 2:
        return np.zeros((n, 2))
    from sklearn.decomposition import PCA
    nc = min(2, n - 1, x.shape[1])
    coords = PCA(n_components=nc, random_state=42).fit_transform(x)
    if coords.shape[1] < 2:
        coords = np.hstack([coords, np.zeros((coords.shape[0], 2 - coords.shape[1]))])
    return coords


def _status_handles() -> list[Patch]:
    """Legend handles for the predicted-SEP status colours."""
    return [Patch(facecolor=c, label=s) for s, c in STATUS_COLORS.items()]


# ── Individual figures ───────────────────────────────────────────────────────

def patent_similarity_scatter(records: list[dict], patent_vecs: np.ndarray) -> Optional[dict]:
    """Requirement 7: 'Patent similarity scatter plot'."""
    if len(records) < 2:
        return None
    _set_style()
    coords = _pca_2d(patent_vecs)
    fig, ax = plt.subplots(figsize=(6, 5))
    for status, color in STATUS_COLORS.items():
        idxs = [i for i, r in enumerate(records) if r["predicted_sep_status"] == status]
        if idxs:
            ax.scatter(coords[idxs, 0], coords[idxs, 1], c=color, label=status,
                       s=42, alpha=0.85, edgecolors="white", linewidths=0.6)
    for i, r in enumerate(records):
        ax.annotate(_short_label(r["patent_label"], 10), coords[i], fontsize=6,
                    xytext=(4, 3), textcoords="offset points", color="#555555")
    ax.set_title("Patent Similarity Map (PCA of semantic embeddings)")
    ax.set_xlabel("Component 1")
    ax.set_ylabel("Component 2")
    ax.legend(fontsize=7, frameon=False, loc="best")
    return {
        "title": "Patent Similarity Scatter Plot",
        "image": _fig_to_b64(fig),
        "caption": (
            "Each point is one analyzed patent, projected to 2D via PCA over "
            "its semantic embedding. Color encodes the predicted SEP status "
            "(cf. Paper 1's SEP / no-SEP separation)."
        ),
    }


def standard_clustering_plot(records: list[dict], patent_vecs: np.ndarray,
                              corpus_vecs: np.ndarray, corpus: list) -> Optional[dict]:
    """Requirement 7: 'Standard clustering plot'."""
    if len(records) < 2:
        return None
    _set_style()
    families = sorted({e.family for e in corpus})
    corpus_index_by_id = {e.id: i for i, e in enumerate(corpus)}
    rep_entries, rep_idx = [], []
    for fam in families:
        rep = representative_entry(fam)
        if rep:
            rep_entries.append(rep)
            rep_idx.append(corpus_index_by_id[rep.id])
    if not rep_entries:
        return None

    combined = np.vstack([patent_vecs, corpus_vecs[rep_idx]])
    coords = _pca_2d(combined)
    p_coords, s_coords = coords[: len(records)], coords[len(records):]

    fig, ax = plt.subplots(figsize=(6.5, 5.5))
    for status, color in STATUS_COLORS.items():
        idxs = [i for i, r in enumerate(records) if r["predicted_sep_status"] == status]
        if idxs:
            ax.scatter(p_coords[idxs, 0], p_coords[idxs, 1], c=color, label=status,
                       s=36, alpha=0.8, edgecolors="white", linewidths=0.5)
    ax.scatter(s_coords[:, 0], s_coords[:, 1], marker="*", c="#1a1a1a", s=200,
               label="Reference standard", zorder=5)
    for (x, y), e in zip(s_coords, rep_entries):
        ax.annotate(f"TS {e.family}", (x, y), fontsize=7, fontweight="bold",
                    xytext=(5, 5), textcoords="offset points", color="#1a1a1a")
    ax.set_title("Standard Clustering Map")
    ax.set_xlabel("Component 1")
    ax.set_ylabel("Component 2")
    ax.legend(fontsize=7, frameon=False, loc="best")
    return {
        "title": "Standard Clustering Plot",
        "image": _fig_to_b64(fig),
        "caption": (
            "Patents and reference standards share one PCA projection, so "
            "proximity to a star indicates semantic closeness to that 3GPP "
            "specification (cf. Paper 2's standard/patent map overlay)."
        ),
    }


def patent_topic_heatmap(records: list[dict], sims_to_corpus: np.ndarray, corpus: list) -> Optional[dict]:
    """Requirement 7: 'Patent-topic heatmap'."""
    if not records:
        return None
    _set_style()
    domains = sorted({e.domain for e in corpus})
    domain_cols = {d: [i for i, e in enumerate(corpus) if e.domain == d] for d in domains}
    mat = np.zeros((len(records), len(domains)))
    for di, d in enumerate(domains):
        mat[:, di] = sims_to_corpus[:, domain_cols[d]].max(axis=1)

    fig_h = max(3, 0.32 * len(records) + 1.5)
    fig, ax = plt.subplots(figsize=(7, fig_h))
    im = ax.imshow(mat, cmap="YlGnBu", aspect="auto", vmin=0, vmax=1)
    ax.grid(False)
    ax.set_xticks(range(len(domains)))
    ax.set_xticklabels(domains, rotation=45, ha="right", fontsize=7)
    ax.set_yticks(range(len(records)))
    ax.set_yticklabels([_short_label(r["patent_label"]) for r in records], fontsize=7)
    ax.set_title("Patent-Topic Heatmap")
    fig.colorbar(im, ax=ax, shrink=0.8, label="Similarity to topic")
    return {
        "title": "Patent-Topic Heatmap",
        "image": _fig_to_b64(fig),
        "caption": (
            "Similarity of each patent to the reference corpus's technical "
            "topics/domains (RRC, MAC, PHY, Core Network, ...), analogous to "
            "Paper 2's technical-topic matrix (Table 2/3)."
        ),
    }


def similarity_matrix_plot(records: list[dict], patent_vecs: np.ndarray) -> Optional[dict]:
    """Requirement 7: 'Similarity matrix'."""
    n = len(records)
    if n < 2:
        return None
    _set_style()
    k = min(n, 15)
    sims = cosine_sim_matrix(patent_vecs[:k], patent_vecs[:k])
    labels = [_short_label(r["patent_label"]) for r in records[:k]]

    fig, ax = plt.subplots(figsize=(0.45 * k + 2.5, 0.45 * k + 2.5))
    im = ax.imshow(sims, cmap="magma", vmin=0, vmax=1)
    ax.grid(False)
    ax.set_xticks(range(k))
    ax.set_xticklabels(labels, rotation=90, fontsize=6)
    ax.set_yticks(range(k))
    ax.set_yticklabels(labels, fontsize=6)
    title = "Patent Similarity Matrix" + (" (first 15)" if n > 15 else "")
    ax.set_title(title)
    fig.colorbar(im, ax=ax, shrink=0.8, label="Cosine similarity")
    return {
        "title": "Patent Similarity Matrix",
        "image": _fig_to_b64(fig),
        "caption": (
            "Pairwise cosine similarity between analyzed patents' semantic "
            "embeddings — the basis for the 'Similar Patent' column."
        ),
    }


def patent_standard_network(records: list[dict]) -> Optional[dict]:
    """Requirement 7: 'Patent-standard network graph'."""
    if not records:
        return None
    try:
        import networkx as nx
    except Exception:
        return None
    _set_style()

    g = nx.Graph()
    k = min(len(records), 25)
    for r in records[:k]:
        p_node = _short_label(r["patent_label"], 12)
        fam = family_of(r["recommended_standard"]) or r["recommended_standard"]
        fam_node = f"TS {fam}"
        g.add_node(p_node, kind="patent")
        g.add_node(fam_node, kind="standard")
        g.add_edge(p_node, fam_node, weight=r["similarity_score"])

    if g.number_of_nodes() == 0:
        return None

    pos = nx.spring_layout(g, seed=42, k=0.7)
    patent_nodes = [n for n, d in g.nodes(data=True) if d["kind"] == "patent"]
    standard_nodes = [n for n, d in g.nodes(data=True) if d["kind"] == "standard"]

    fig, ax = plt.subplots(figsize=(7, 6))
    weights = [g[u][v]["weight"] * 3 for u, v in g.edges()]
    nx.draw_networkx_edges(g, pos, ax=ax, alpha=0.35, edge_color="#999999", width=weights)
    nx.draw_networkx_nodes(g, pos, nodelist=patent_nodes, node_color="#3d6b8a",
                            node_size=110, ax=ax)
    nx.draw_networkx_nodes(g, pos, nodelist=standard_nodes, node_color="#c97c2c",
                            node_shape="s", node_size=520, ax=ax)
    nx.draw_networkx_labels(g, pos, {n: n for n in standard_nodes}, font_size=8,
                            font_weight="bold", ax=ax)
    nx.draw_networkx_labels(g, pos, {n: n for n in patent_nodes}, font_size=6, ax=ax)
    ax.set_title("Patent-Standard Network" + (" (first 25 patents)" if len(records) > 25 else ""))
    ax.axis("off")
    handles = [Patch(facecolor="#3d6b8a", label="Patent"), Patch(facecolor="#c97c2c", label="Standard")]
    ax.legend(handles=handles, fontsize=8, frameon=False, loc="lower right")
    return {
        "title": "Patent-Standard Network Graph",
        "image": _fig_to_b64(fig),
        "caption": "Bipartite graph linking each patent to its recommended standard family; edge thickness reflects similarity.",
    }


def standards_timeline(records: list[dict], corpus: list) -> Optional[dict]:
    """Requirement 7: 'Timeline of standards'."""
    if not records:
        return None
    _set_style()
    year_lookup = {(e.family, e.version): e.year for e in corpus}
    families = sorted({family_of(r["recommended_standard"]) for r in records if family_of(r["recommended_standard"])})
    if not families:
        return None
    fam_y = {f: i for i, f in enumerate(families)}

    fig, ax = plt.subplots(figsize=(7, max(3, 0.45 * len(families) + 1)))
    rng = np.random.RandomState(7)
    for r in records:
        fam = family_of(r["recommended_standard"])
        year = year_lookup.get((fam, r["recommended_version"]))
        if fam is None or year is None:
            continue
        y = fam_y[fam] + rng.uniform(-0.18, 0.18)
        ax.scatter(year, y, color=STATUS_COLORS.get(r["predicted_sep_status"], "#888888"),
                   s=44, alpha=0.85, edgecolors="white", linewidths=0.5)

    ax.set_yticks(range(len(families)))
    ax.set_yticklabels([f"TS {f}" for f in families], fontsize=8)
    ax.set_xlabel("Approximate release year")
    ax.set_title("Timeline of Recommended Standards")
    ax.legend(handles=_status_handles(), fontsize=7, frameon=False,
              loc="upper left", bbox_to_anchor=(1.01, 1.0))
    return {
        "title": "Timeline of Standards",
        "image": _fig_to_b64(fig),
        "caption": (
            "Each point places a patent's recommended standard version on the "
            "approximate 3GPP release timeline, colored by predicted SEP status."
        ),
    }


def version_evolution_diagram(records: list[dict], sims_to_corpus: np.ndarray, corpus: list) -> Optional[dict]:
    """Requirement 9: version-tracking visualization."""
    if not records:
        return None
    _set_style()
    corpus_index_by_id = {e.id: i for i, e in enumerate(corpus)}
    fam_counts = Counter(family_of(r["recommended_standard"]) for r in records if family_of(r["recommended_standard"]))
    top_fams = [f for f, _ in fam_counts.most_common(4)]
    if not top_fams:
        return None

    fig, axes = plt.subplots(len(top_fams), 1, figsize=(7, 1.7 * len(top_fams) + 0.6), squeeze=False)
    for ax_idx, fam in enumerate(top_fams):
        ax = axes[ax_idx, 0]
        groups = clause_groups_by_family(fam)
        cg_counts = Counter(r["standard_clause"] for r in records
                            if family_of(r["recommended_standard"]) == fam)
        target_label = cg_counts.most_common(1)[0][0] if cg_counts else None
        entries = None
        for ents in groups.values():
            if ents and ents[0].clause_label == target_label:
                entries = ents
                break
        if entries is None:
            entries = next(iter(groups.values()))

        patent_idxs = [i for i, r in enumerate(records) if family_of(r["recommended_standard"]) == fam]
        avg_sims = []
        for e in entries:
            col = corpus_index_by_id[e.id]
            vals = [sims_to_corpus[i, col] for i in patent_idxs]
            avg_sims.append(float(np.mean(vals)) if vals else 0.0)

        xs = list(range(len(entries)))
        ax.plot(xs, avg_sims, "-o", color="#3d6b8a", linewidth=2, markersize=6)
        for x, e, s in zip(xs, entries, avg_sims):
            ax.annotate(f"{e.version}\n{s:.2f}", (x, s), xytext=(0, 9),
                        textcoords="offset points", ha="center", fontsize=7)

        if avg_sims[-1] > avg_sims[0] + 0.02:
            trend = "↑ predicted essentiality increased"
        elif avg_sims[-1] < avg_sims[0] - 0.02:
            trend = "↓ predicted essentiality decreased"
        else:
            trend = "→ predicted essentiality stable"

        ax.set_title(f"TS {fam} — {entries[0].clause_label} {entries[0].clause_title}  ({trend})",
                     fontsize=8, loc="left", fontweight="normal")
        ax.set_xlim(-0.5, len(entries) - 0.5)
        ax.set_ylim(0, 1)
        ax.set_xticks([])
        ax.set_ylabel("Avg.\nsimilarity", fontsize=7)

    fig.suptitle("Version Evolution — Predicted Essentiality by Release", fontsize=10, fontweight="bold")
    fig.tight_layout(rect=(0, 0, 1, 0.94))
    return {
        "title": "Version Evolution Graph",
        "image": _fig_to_b64(fig),
        "caption": (
            "For the most-matched standard families, average semantic similarity "
            "of the analyzed patents to each successive version's reference clause "
            "— a rising line suggests increasing predicted essentiality "
            "(Requirement 9)."
        ),
    }


def confidence_distribution(records: list[dict]) -> Optional[dict]:
    """Requirement 10: confidence distribution."""
    if not records:
        return None
    _set_style()
    confs = [r["confidence_score"] for r in records]
    sims = [r["similarity_score"] for r in records]
    fig, ax = plt.subplots(figsize=(6, 3.5))
    bins = np.linspace(0, 1, 11)
    ax.hist(confs, bins=bins, alpha=0.7, label="Confidence score", color="#3d6b8a")
    ax.hist(sims, bins=bins, alpha=0.5, label="Standard similarity", color="#c97c2c")
    ax.set_xlabel("Score")
    ax.set_ylabel("Number of patents")
    ax.set_title("Confidence & Similarity Distribution")
    ax.legend(fontsize=8, frameon=False)
    return {
        "title": "Confidence Distribution",
        "image": _fig_to_b64(fig),
        "caption": "Distribution of blended confidence scores and top-match standard similarity across the batch.",
    }


def standard_frequency_chart(records: list[dict]) -> Optional[dict]:
    """Requirement 10: standard frequency chart."""
    if not records:
        return None
    _set_style()
    fam_counts = Counter(family_of(r["recommended_standard"]) for r in records if family_of(r["recommended_standard"]))
    items = fam_counts.most_common(10)
    if not items:
        return None
    labels = [f"TS {f}" for f, _ in items][::-1]
    values = [c for _, c in items][::-1]
    fig, ax = plt.subplots(figsize=(6, max(2.5, 0.4 * len(items) + 1)))
    ax.barh(labels, values, color="#3d6b8a")
    ax.set_xlabel("Number of patents")
    ax.set_title("Standard Frequency (Recommended)")
    return {
        "title": "Standard Frequency Chart",
        "image": _fig_to_b64(fig),
        "caption": "How many analyzed patents were mapped to each 3GPP specification family.",
    }


def version_distribution_chart(records: list[dict]) -> Optional[dict]:
    """Requirement 10: version distribution chart."""
    if not records:
        return None
    _set_style()
    ver_counts = Counter(r["recommended_version"] for r in records)
    items = sorted(ver_counts.items())
    fig, ax = plt.subplots(figsize=(6, 3))
    ax.bar([v for v, _ in items], [c for _, c in items], color="#c97c2c")
    ax.set_xlabel("Recommended version")
    ax.set_ylabel("Number of patents")
    ax.set_title("Version Distribution")
    plt.setp(ax.get_xticklabels(), rotation=45, ha="right", fontsize=8)
    return {
        "title": "Version Distribution Chart",
        "image": _fig_to_b64(fig),
        "caption": "Distribution of recommended standard versions across the analyzed batch.",
    }


# ── Orchestrator ─────────────────────────────────────────────────────────────

def build_all_figures(records: list[dict], patent_vecs: np.ndarray, corpus_vecs: np.ndarray,
                       corpus: list, sims_to_corpus: np.ndarray) -> dict:
    """Build every figure, skipping (not failing) any that can't be produced."""
    builders = {
        "patent_similarity_scatter": lambda: patent_similarity_scatter(records, patent_vecs),
        "standard_clustering": lambda: standard_clustering_plot(records, patent_vecs, corpus_vecs, corpus),
        "patent_topic_heatmap": lambda: patent_topic_heatmap(records, sims_to_corpus, corpus),
        "similarity_matrix": lambda: similarity_matrix_plot(records, patent_vecs),
        "patent_standard_network": lambda: patent_standard_network(records),
        "standards_timeline": lambda: standards_timeline(records, corpus),
        "version_evolution": lambda: version_evolution_diagram(records, sims_to_corpus, corpus),
        "confidence_distribution": lambda: confidence_distribution(records),
        "standard_frequency": lambda: standard_frequency_chart(records),
        "version_distribution": lambda: version_distribution_chart(records),
    }
    out: dict = {}
    for key, build in builders.items():
        try:
            result = build()
            if result is not None:
                out[key] = result
        except Exception as e:  # pragma: no cover - defensive, keeps one bad figure from breaking all
            out[key] = {"error": str(e)}
    return out
