"""Render the evaluation results as a two-panel figure for the paper.

Panel A (agreement): system weighted score vs. human mean rating, one point
per paper, colored by whether the system's accept/reject verdict matched the
human one; Spearman ρ, decision accuracy, and Cohen's κ annotated.

Panel B (consistency): for papers run multiple times, the spread of the
system's weighted score across repeats (dots = runs, bar = mean), with the
unanimity fraction and mean score σ annotated.

Output is SVG (vector, with text rendered as paths so the Typst paper build
needs no fonts or Python) plus a PNG preview. matplotlib is imported lazily so
it's only required when actually drawing — install with the ``eval`` extra.
"""

from __future__ import annotations

import os

from . import metrics as M
from .corpus import load_corpus

_MATCH = "#2a9d8f"
_MISMATCH = "#e76f51"
_RUN_DOT = "#264653"
_MEAN_BAR = "#e9c46a"


def make_figure(corpus_path: str, runs_path: str, out_prefix: str,
                *, title: str | None = None) -> tuple[str, str]:
    """Build the figure from corpus + runs; return (svg_path, png_path)."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.lines import Line2D

    plt.rcParams.update({"svg.fonttype": "path", "font.size": 9})

    records = M._records(runs_path)
    if not records:
        raise SystemExit(f"No runs in {runs_path} — run the pipeline first.")
    corpus = {c.id: c for c in load_corpus(corpus_path)}
    agreement = M.compute_agreement(corpus_path, records)
    consistency = M.compute_consistency(records)

    fig, (ax_a, ax_b) = plt.subplots(1, 2, figsize=(7.2, 3.3))

    _agreement_panel(ax_a, corpus, records, agreement, Line2D)
    _consistency_panel(ax_b, records, consistency)

    if title:
        fig.suptitle(title, fontsize=10)
    fig.tight_layout()

    os.makedirs(os.path.dirname(os.path.abspath(out_prefix)) or ".", exist_ok=True)
    svg_path, png_path = f"{out_prefix}.svg", f"{out_prefix}.png"
    fig.savefig(svg_path)
    fig.savefig(png_path, dpi=200)
    plt.close(fig)
    return svg_path, png_path


def make_ablation_figure(corpus_path: str, runs_files: list[str], labels: list[str],
                         out_prefix: str, *, title: str | None = None) -> tuple[str, str]:
    """Agreement metrics (accuracy, Cohen's κ, Spearman ρ) across an ordered
    list of configurations, one runs file each — shows agreement climbing as
    structure is restored.
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams.update({"svg.fonttype": "path", "font.size": 9})
    accs, kappas, rhos = [], [], []
    for f in runs_files:
        agr = M.compute_agreement(corpus_path, M._records(f))
        accs.append(agr["decision_accuracy"])
        kappas.append(agr["decision_cohen_kappa"])
        rhos.append(agr["score_spearman"])

    x = list(range(len(labels)))
    fig, ax = plt.subplots(figsize=(4.4, 3.3))
    for ys, lab, color, mk in (
        (accs, "Accuracy", _RUN_DOT, "o"),
        (kappas, "Cohen's κ", _MATCH, "s"),
        (rhos, "Spearman ρ", _MISMATCH, "^"),
    ):
        ax.plot(x, ys, color=color, marker=mk, lw=2, ms=7, label=lab, zorder=3)
        for xi, yi in zip(x, ys):
            if yi is not None:
                ax.annotate(f"{yi:.2f}", (xi, yi), textcoords="offset points",
                            xytext=(0, 7), ha="center", fontsize=7, color=color)
    ax.set_xticks(x)
    ax.set_xticklabels(labels, fontsize=8)
    # Limits follow the data and always include zero. A fixed window (this
    # was 0.4–0.85) silently clipped low or negative κ points out of the
    # frame, so the figure could only ever show the flattering range.
    vals = [v for series in (accs, kappas, rhos) for v in series if v is not None]
    if vals:
        lo, hi = min(0.0, min(vals)), max(0.0, max(vals))
        pad = 0.08 * max(hi - lo, 0.1)
        ax.set_ylim(lo - pad, hi + pad)
    ax.set_ylabel("Agreement with humans")
    ax.set_title(title or "Structure ablation")
    ax.grid(True, axis="y", alpha=0.25, zorder=0)
    ax.legend(fontsize=7, loc="lower right", framealpha=0.9)
    fig.tight_layout()
    return _save(fig, out_prefix)


def make_strictness_figure(corpus_path: str, runs_files: list[str], labels: list[str],
                           out_prefix: str, *, title: str | None = None) -> tuple[str, str]:
    """Per-paper weighted score across an ordered set of conditions (strictness
    levels), with the mean overlaid — shows the monotonic score response."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.lines import Line2D

    plt.rcParams.update({"svg.fonttype": "path", "font.size": 9})
    n = len(runs_files)
    paper_scores: dict[str, list[float | None]] = {}
    for j, f in enumerate(runs_files):
        for pid, run in M._first_ok_per_paper(M._records(f)).items():
            paper_scores.setdefault(pid, [None] * n)[j] = run.system_weighted_score

    x = list(range(n))
    fig, ax = plt.subplots(figsize=(4.6, 3.3))
    for ys in paper_scores.values():
        ax.plot(x, ys, color="0.72", lw=1.0, marker="o", ms=3, zorder=2)
    means: list[float | None] = []
    for j in range(n):
        vals = [s[j] for s in paper_scores.values() if s[j] is not None]
        means.append(round(sum(vals) / len(vals), 3) if vals else None)
    ax.plot(x, means, color=_MISMATCH, lw=2.6, marker="s", ms=7, zorder=4)
    ax.set_xticks(x)
    ax.set_xticklabels(labels)
    ax.set_xlabel("Review strictness (1 = lenient, 5 = harsh)")
    ax.set_ylabel("System weighted score")
    ax.set_title(title or f"Score vs. strictness (n={len(paper_scores)} papers)")
    ax.grid(True, alpha=0.25, zorder=0)
    ax.legend(handles=[
        Line2D([0], [0], color="0.72", marker="o", ms=4, label="per paper"),
        Line2D([0], [0], color=_MISMATCH, lw=2.6, marker="s", ms=6, label="mean"),
    ], fontsize=7, loc="lower left", framealpha=0.9)
    fig.tight_layout()
    return _save(fig, out_prefix)


def _save(fig, out_prefix: str) -> tuple[str, str]:
    import matplotlib.pyplot as plt

    os.makedirs(os.path.dirname(os.path.abspath(out_prefix)) or ".", exist_ok=True)
    svg_path, png_path = f"{out_prefix}.svg", f"{out_prefix}.png"
    fig.savefig(svg_path)
    fig.savefig(png_path, dpi=200)
    plt.close(fig)
    return svg_path, png_path


def _agreement_panel(ax, corpus, records, agreement, Line2D) -> None:
    first = M._first_ok_per_paper(records)
    xs: list[float] = []
    ys: list[float] = []
    colors: list[str] = []
    for pid, run in first.items():
        item = corpus.get(pid)
        if not item or run.system_weighted_score is None or item.human_mean is None:
            continue
        xs.append(item.human_mean)
        ys.append(run.system_weighted_score)
        sb, hb = M.system_binary(run.system_decision), item.human_decision
        colors.append(_MATCH if (sb is not None and sb == hb) else _MISMATCH)

    ax.scatter(xs, ys, c=colors, s=42, edgecolors="black", linewidths=0.4, zorder=3)
    ax.set_xlabel("Human mean rating")
    ax.set_ylabel("System weighted score")
    ax.set_title(f"Agreement with humans (n={agreement['n_scored_papers']})")
    ax.grid(True, alpha=0.25, zorder=0)
    ax.text(
        0.04, 0.96,
        f"Spearman ρ = {M._fmt(agreement['score_spearman'])}\n"
        f"Decision acc = {M._fmt(agreement['decision_accuracy'])}\n"
        f"Cohen's κ = {M._fmt(agreement['decision_cohen_kappa'])}",
        transform=ax.transAxes, va="top", ha="left", fontsize=8,
        bbox=dict(boxstyle="round", fc="white", ec="0.7"),
    )
    ax.legend(
        handles=[
            Line2D([0], [0], marker="o", linestyle="", markerfacecolor=_MATCH,
                   markeredgecolor="black", label="verdict match"),
            Line2D([0], [0], marker="o", linestyle="", markerfacecolor=_MISMATCH,
                   markeredgecolor="black", label="verdict mismatch"),
        ],
        fontsize=7, loc="lower right", framealpha=0.9,
    )


def _consistency_panel(ax, records, consistency) -> None:
    per = consistency["per_paper"]
    ax.set_title("Consistency across repeats")
    if not per:
        ax.text(0.5, 0.5, "no repeated runs yet\n(run with --repeats > 1)",
                ha="center", va="center", transform=ax.transAxes, color="0.5")
        ax.set_xticks([])
        return

    ok_runs = M._ok_runs_per_paper(records)
    for i, p in enumerate(per):
        scores = [r.system_weighted_score for r in ok_runs.get(p["paper_id"], [])
                  if r.system_weighted_score is not None]
        if not scores:
            continue
        ax.scatter([i] * len(scores), scores, s=38, color=_RUN_DOT, zorder=3, alpha=0.85)
        mean = sum(scores) / len(scores)
        ax.plot([i - 0.2, i + 0.2], [mean, mean], color=_MEAN_BAR, lw=2.5, zorder=2)

    ax.set_xticks(range(len(per)))
    ax.set_xticklabels([p["paper_id"][:6] for p in per], fontsize=7)
    ax.set_ylabel("System weighted score")
    ax.margins(x=0.12, y=0.12)   # keep edge points off the axes frame
    ax.grid(True, axis="y", alpha=0.25, zorder=0)
    # Lower-left: the high-scoring papers' dots live in the top-left, so keep
    # the stats box out of their way.
    ax.text(
        0.04, 0.04,
        f"Unanimous = {consistency['frac_unanimous']}\n"
        f"Mean score σ = {consistency['mean_score_std']}",
        transform=ax.transAxes, va="bottom", ha="left", fontsize=8,
        bbox=dict(boxstyle="round", fc="white", ec="0.7"),
    )
