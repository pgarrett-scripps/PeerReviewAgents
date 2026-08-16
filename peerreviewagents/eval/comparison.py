"""Paired full-workflow versus single-LLM practical-baseline report."""

from __future__ import annotations

import json
import os
import random
from typing import Any

from . import metrics as M
from .corpus import load_corpus, verify_corpus_manifest
from .schema import RunRecord


def _condition(records: list[RunRecord], corpus_path: str) -> dict[str, Any]:
    report = {
        "health": M.run_health(records),
        "agreement": M.compute_agreement(corpus_path, records),
    }
    manifests = {M._manifest_config_key(r.manifest) for r in records if r.manifest}
    if len(manifests) != 1:
        raise ValueError(
            f"condition contains {len(manifests)} configurations; split the runs file "
            "before comparison"
        )
    report["manifest"] = records[0].manifest if records else {}
    return report


def _delta_interval(full: list[Any], baseline: list[Any], truth: list[Any], metric,
                    *, seed: int, samples: int = 2000) -> tuple[float | None, list[float] | None]:
    if not full or not (len(full) == len(baseline) == len(truth)):
        return None, None
    estimate_full = metric(full, truth)
    estimate_baseline = metric(baseline, truth)
    estimate = (
        round(float(estimate_full) - float(estimate_baseline), 4)
        if estimate_full is not None and estimate_baseline is not None else None
    )
    rng = random.Random(seed)
    deltas = []
    for _ in range(samples):
        idx = [rng.randrange(len(full)) for _ in full]
        f = metric([full[i] for i in idx], [truth[i] for i in idx])
        b = metric([baseline[i] for i in idx], [truth[i] for i in idx])
        if f is not None and b is not None:
            deltas.append(float(f) - float(b))
    return estimate, M._percentile_interval(deltas)


def build_comparison(corpus_path: str, system_path: str, baseline_path: str) -> dict[str, Any]:
    """Compare conditions on the same successful paper set.

    The report calls this a practical baseline comparison, not an ablation:
    the full workflow consumes more model calls and compute.
    """
    corpus_manifest = verify_corpus_manifest(corpus_path, warn_missing=True)
    corpus = {c.id: c for c in load_corpus(corpus_path)}
    system_records = M._records(system_path)
    baseline_records = M._records(baseline_path)
    system = M._first_ok_per_paper(system_records)
    baseline = M._first_ok_per_paper(baseline_records)
    common = sorted(set(system) & set(baseline) & set(corpus))
    if not common:
        raise ValueError("system and baseline files have no successful papers in common")

    sys_scores, base_scores, human_scores = [], [], []
    sys_decisions, base_decisions, human_decisions = [], [], []
    for paper_id in common:
        item, full, base = corpus[paper_id], system[paper_id], baseline[paper_id]
        if (full.system_weighted_score is not None and
                base.system_weighted_score is not None and item.human_mean is not None):
            sys_scores.append(full.system_weighted_score)
            base_scores.append(base.system_weighted_score)
            human_scores.append(item.human_mean)
        full_binary = M.system_binary(full.system_decision)
        base_binary = M.system_binary(base.system_decision)
        if full_binary and base_binary and item.human_decision:
            sys_decisions.append(full_binary)
            base_decisions.append(base_binary)
            human_decisions.append(item.human_decision)

    def accuracy(pred, truth):
        return sum(p == t for p, t in zip(pred, truth)) / len(pred)

    deltas = {}
    for name, full, base, truth, metric, seed in (
        ("score_spearman", sys_scores, base_scores, human_scores, M.spearman, 20260815),
        ("decision_accuracy", sys_decisions, base_decisions, human_decisions, accuracy, 20260816),
        ("decision_balanced_accuracy", sys_decisions, base_decisions, human_decisions,
         M.balanced_accuracy, 20260817),
        ("decision_cohen_kappa", sys_decisions, base_decisions, human_decisions,
         M.cohen_kappa, 20260818),
    ):
        value, interval = _delta_interval(full, base, truth, metric, seed=seed)
        deltas[name] = {"estimate": value, "confidence_interval": interval}

    system_report = _condition(system_records, corpus_path)
    baseline_report = _condition(baseline_records, corpus_path)
    modes = (system_report["manifest"].get("mode"), baseline_report["manifest"].get("mode"))
    if modes != ("system", "single-llm"):
        raise ValueError(
            f"expected modes system and single-llm, found {modes[0]!r} and {modes[1]!r}"
        )
    for key in ("config_digest", "provider", "model", "source_fingerprint"):
        full_value = system_report["manifest"].get(key)
        baseline_value = baseline_report["manifest"].get(key)
        if full_value != baseline_value:
            raise ValueError(
                f"conditions differ in {key}: {full_value!r} vs {baseline_value!r}; "
                "run both conditions with identical evaluation flags"
            )
    if corpus_manifest:
        expected_corpus = corpus_manifest["corpus_sha256"]
        for label, condition in (("system", system_report), ("single-llm", baseline_report)):
            found = condition["manifest"].get("corpus_sha256")
            if found != expected_corpus:
                raise ValueError(
                    f"{label} runs reference corpus {found or 'unknown'}, not the "
                    f"currently frozen corpus {expected_corpus}"
                )
    return {
        "comparison_type": "practical baseline (not compute-matched causal ablation)",
        "n_common_papers": len(common),
        "paper_ids": common,
        "system": system_report,
        "single_llm": baseline_report,
        "paired_deltas_system_minus_single_llm": deltas,
    }


def render_markdown(report: dict[str, Any]) -> str:
    def fmt(value):
        return "n/a" if value is None else value

    def ci(interval):
        return "n/a" if not interval else f"{interval[0]}–{interval[1]}"

    full = report["system"]
    base = report["single_llm"]
    lines = [
        "# PeerReviewAgents Evaluation Comparison",
        "",
        f"Comparison: **{report['comparison_type']}**.",
        f"Paired papers with successful runs in both conditions: {report['n_common_papers']}.",
        "",
        "> The single-LLM condition uses fewer calls and less compute. Differences must not be "
        "interpreted as the isolated causal effect of multi-agent structure.",
        "",
        "| Metric | Full PRA | Single LLM | Difference (full − baseline), 95% CI |",
        "|---|---:|---:|---:|",
    ]
    names = (
        ("score_spearman", "Spearman ρ"),
        ("decision_accuracy", "Decision accuracy"),
        ("decision_balanced_accuracy", "Balanced accuracy"),
        ("decision_cohen_kappa", "Cohen's κ"),
    )
    for key, label in names:
        fa, ba = full["agreement"], base["agreement"]
        delta = report["paired_deltas_system_minus_single_llm"][key]
        lines.append(
            f"| {label} | {fmt(fa.get(key))} | {fmt(ba.get(key))} | "
            f"{fmt(delta['estimate'])} ({ci(delta['confidence_interval'])}) |"
        )
    fh, bh = full["health"], base["health"]
    lines += [
        f"| Successful-run fraction | {fmt(fh['success_fraction'])} | "
        f"{fmt(bh['success_fraction'])} | — |",
        f"| Mean cost / successful run (USD) | {fmt(fh['mean_cost_per_ok_run_usd'])} | "
        f"{fmt(bh['mean_cost_per_ok_run_usd'])} | — |",
        f"| Mean latency / successful run (s) | {fmt(fh['mean_latency_per_ok_run_s'])} | "
        f"{fmt(bh['mean_latency_per_ok_run_s'])} | — |",
        "",
        "Paired intervals use deterministic paper-level bootstrap resampling (2,000 samples).",
    ]
    return "\n".join(lines)


def write_comparison(report: dict[str, Any], out_prefix: str) -> tuple[str, str]:
    os.makedirs(os.path.dirname(os.path.abspath(out_prefix)) or ".", exist_ok=True)
    json_path, md_path = f"{out_prefix}.json", f"{out_prefix}.md"
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)
    with open(md_path, "w", encoding="utf-8") as fh:
        fh.write(render_markdown(report) + "\n")
    return json_path, md_path
