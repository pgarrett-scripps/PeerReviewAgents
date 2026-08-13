"""Score the runs against human ground truth, and against each other.

Two reports, matching the two pilot goals:

  * **agreement** — system vs human: Spearman/Pearson of the confidence-
    weighted system score against the human mean rating (rank-based, so the
    1-5 vs 1-10 scale mismatch is harmless), plus accept/reject accuracy,
    Cohen's kappa, and a confusion matrix.
  * **consistency** — run vs run: for papers with >=2 runs, how often the
    verdict is unanimous, the majority fraction, and the spread of the
    weighted score across repeats.

All statistics are implemented here in pure Python (no scipy) and unit-tested
against hand-computed values.
"""

from __future__ import annotations

import json
import os
import statistics
from collections import Counter
from typing import Any

from .corpus import load_corpus
from .schema import RunRecord, read_jsonl

# System verdict -> binary, to compare against accept/reject ground truth.
# "minor revision" is treated as accept-leaning; "major"/"reject" as reject.
_ACCEPTISH = {"accept", "minor"}


def system_binary(decision: str | None) -> str | None:
    if not decision:
        return None
    return "accept" if decision in _ACCEPTISH else "reject"


# ---------------------------------------------------------------------------
# Pure statistics (unit-tested)
# ---------------------------------------------------------------------------


def pearson(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 2:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = sum((x - mx) ** 2 for x in xs) ** 0.5
    dy = sum((y - my) ** 2 for y in ys) ** 0.5
    if dx == 0 or dy == 0:
        return None
    return round(num / (dx * dy), 4)


def _avg_ranks(vals: list[float]) -> list[float]:
    """1-based ranks with ties averaged (for Spearman)."""
    order = sorted(range(len(vals)), key=lambda i: vals[i])
    ranks = [0.0] * len(vals)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and vals[order[j + 1]] == vals[order[i]]:
            j += 1
        avg = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def spearman(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 2:
        return None
    return pearson(_avg_ranks(xs), _avg_ranks(ys))


def cohen_kappa(a: list[str], b: list[str]) -> float | None:
    n = len(a)
    if n == 0:
        return None
    labels = sorted(set(a) | set(b))
    po = sum(1 for x, y in zip(a, b) if x == y) / n
    ca, cb = Counter(a), Counter(b)
    pe = sum((ca[label] / n) * (cb[label] / n) for label in labels)
    if pe == 1:
        # Both raters used a single class, so chance agreement is total and
        # kappa is 0/0 — undefined, not perfect. Reporting 1.0 here scored a
        # system that rubber-stamps "accept" on an all-accept corpus as being
        # in flawless calibrated agreement, when the data cannot distinguish
        # it from a coin glued to one side.
        return None
    return round((po - pe) / (1 - pe), 4)


def confusion(pred: list[str], truth: list[str]) -> dict[str, int]:
    """2x2 accept/reject confusion counts keyed ``pred__truth``."""
    out = {f"{p}__{t}": 0 for p in ("accept", "reject") for t in ("accept", "reject")}
    for p, t in zip(pred, truth):
        key = f"{p}__{t}"
        if key in out:
            out[key] += 1
    return out


# ---------------------------------------------------------------------------
# Report assembly
# ---------------------------------------------------------------------------


def _records(runs_path: str) -> list[RunRecord]:
    return [RunRecord.from_dict(d) for d in read_jsonl(runs_path)]


def _first_ok_per_paper(records: list[RunRecord]) -> dict[str, RunRecord]:
    out: dict[str, RunRecord] = {}
    for r in sorted(records, key=lambda r: r.repeat):
        if r.ok and r.paper_id not in out:
            out[r.paper_id] = r
    return out


def _ok_runs_per_paper(records: list[RunRecord]) -> dict[str, list[RunRecord]]:
    out: dict[str, list[RunRecord]] = {}
    for r in records:
        if r.ok:
            out.setdefault(r.paper_id, []).append(r)
    return out


def compute_agreement(corpus_path: str, records: list[RunRecord]) -> dict[str, Any]:
    corpus = {c.id: c for c in load_corpus(corpus_path)}
    first = _first_ok_per_paper(records)

    sys_scores: list[float] = []
    hum_means: list[float] = []
    pred: list[str] = []
    truth: list[str] = []
    for pid, run in first.items():
        item = corpus.get(pid)
        if item is None:
            continue
        if run.system_weighted_score is not None and item.human_mean is not None:
            sys_scores.append(run.system_weighted_score)
            hum_means.append(item.human_mean)
        sb, hb = system_binary(run.system_decision), item.human_decision
        if sb and hb:
            pred.append(sb)
            truth.append(hb)

    acc = round(sum(1 for p, t in zip(pred, truth) if p == t) / len(pred), 4) if pred else None
    return {
        "n_scored_papers": len(sys_scores),
        "score_spearman": spearman(sys_scores, hum_means),
        "score_pearson": pearson(sys_scores, hum_means),
        "n_decision_papers": len(pred),
        "decision_accuracy": acc,
        "decision_cohen_kappa": cohen_kappa(pred, truth),
        "confusion": confusion(pred, truth),
    }


def compute_consistency(records: list[RunRecord]) -> dict[str, Any]:
    multi = {pid: rs for pid, rs in _ok_runs_per_paper(records).items() if len(rs) >= 2}
    per: list[dict[str, Any]] = []
    for pid, rs in multi.items():
        decisions = [r.system_decision for r in rs]
        scores = [r.system_weighted_score for r in rs if r.system_weighted_score is not None]
        counts = Counter(decisions)
        per.append({
            "paper_id": pid,
            "n_runs": len(rs),
            "decisions": decisions,
            "unanimous": len(counts) == 1,
            "majority_frac": round(counts.most_common(1)[0][1] / len(decisions), 3),
            "score_std": round(statistics.pstdev(scores), 4) if len(scores) >= 2 else None,
            "score_range": round(max(scores) - min(scores), 4) if len(scores) >= 2 else None,
        })
    n = len(per)
    stds = [p["score_std"] for p in per if p["score_std"] is not None]
    return {
        "n_papers_multi_run": n,
        "frac_unanimous": round(sum(1 for p in per if p["unanimous"]) / n, 3) if n else None,
        "mean_majority_frac": round(sum(p["majority_frac"] for p in per) / n, 3) if n else None,
        "mean_score_std": round(statistics.mean(stds), 4) if stds else None,
        "per_paper": per,
    }


def run_health(records: list[RunRecord]) -> dict[str, Any]:
    ok = [r for r in records if r.ok]
    degraded = [r for r in ok if r.errors]      # produced a decision but logged errors
    return {
        "total_runs": len(records),
        "ok_runs": len(ok),
        "failed_runs": len(records) - len(ok),
        "degraded_runs": len(degraded),
        "total_cost_usd": round(sum(r.cost_usd for r in records), 4),
        "mean_latency_s": round(statistics.mean([r.latency_s for r in records]), 1) if records else 0,
    }


def build_report(corpus_path: str, runs_path: str) -> dict[str, Any]:
    records = _records(runs_path)
    manifests = {tuple(sorted(r.manifest.items())) for r in records if r.manifest}
    return {
        "health": run_health(records),
        "agreement": compute_agreement(corpus_path, records),
        "consistency": compute_consistency(records),
        "distinct_configs": len(manifests),
        "sample_manifest": records[0].manifest if records else {},
    }


def _fmt(value: Any) -> Any:
    """Undefined statistics render as ``n/a`` — a κ that is 0/0 must not be
    mistaken for a κ of 1.0 or for the string ``None``."""
    return "n/a" if value is None else value


def render_markdown(report: dict[str, Any]) -> str:
    h, a, c = report["health"], report["agreement"], report["consistency"]
    m = report.get("sample_manifest", {})
    cf = a["confusion"]
    lines = [
        "# Evaluation Report",
        "",
        f"Model: `{m.get('model','?')}` · provider: `{m.get('provider','?')}` · "
        f"git: `{m.get('git_sha','?')}` · config: `{m.get('config_digest','?')}` · "
        f"venue: `{m.get('venue','?')}`",
    ]
    n_configs = report.get("distinct_configs", 1) or 1
    if n_configs > 1:
        # The manifest line above describes records[0] only; the numbers below
        # blend every config in the file. Said loudly, because a pooled runs
        # file otherwise reads as a single-configuration result.
        lines.append(
            f"> ⚠️ MIXED CONFIGS: this runs file pools {n_configs} distinct "
            "configurations. Every number below blends them, and the manifest "
            "above describes only one. Split the runs files to compare configs."
        )
    if m.get("leakage_note"):
        lines.append(f"> ⚠️ Leakage: {m['leakage_note']}")
    lines += [
        "",
        "## Run health",
        f"- Total runs: {h['total_runs']}  (ok {h['ok_runs']}, failed {h['failed_runs']}, "
        f"degraded {h['degraded_runs']})",
        f"- Total cost: ${h['total_cost_usd']}  ·  mean latency: {h['mean_latency_s']}s",
        "",
        "## Agreement with human reviewers",
        f"- Scored papers: {a['n_scored_papers']}",
        f"- Score correlation: Spearman ρ = {a['score_spearman']}, Pearson r = {a['score_pearson']}",
        f"- Decision papers: {a['n_decision_papers']}",
        f"- Decision accuracy: {_fmt(a['decision_accuracy'])}  ·  "
        f"Cohen's κ = {_fmt(a['decision_cohen_kappa'])}",
        f"- Confusion (pred__truth): "
        f"accept→accept {cf['accept__accept']}, accept→reject {cf['accept__reject']}, "
        f"reject→accept {cf['reject__accept']}, reject→reject {cf['reject__reject']}",
        "",
        "## Consistency across repeated runs",
        f"- Multi-run papers: {c['n_papers_multi_run']}",
        f"- Fraction unanimous: {c['frac_unanimous']}  ·  mean majority fraction: {c['mean_majority_frac']}",
        f"- Mean score std across runs: {c['mean_score_std']}",
    ]
    for p in c["per_paper"]:
        lines.append(
            f"  - `{p['paper_id']}`: {p['n_runs']} runs, decisions={p['decisions']}, "
            f"score_std={p['score_std']}, range={p['score_range']}"
        )
    return "\n".join(lines)


def write_report(report: dict[str, Any], out_prefix: str) -> tuple[str, str]:
    os.makedirs(os.path.dirname(os.path.abspath(out_prefix)) or ".", exist_ok=True)
    json_path = f"{out_prefix}.json"
    md_path = f"{out_prefix}.md"
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)
    with open(md_path, "w", encoding="utf-8") as fh:
        fh.write(render_markdown(report))
    return json_path, md_path
