"""CLI for the evaluation harness:  ``python -m peerreviewagents.eval ...``

    fetch    pull a labeled OpenReview corpus (PDFs + human scores/decisions)
    run      review every corpus paper --repeats times into runs.jsonl
    metrics  score runs vs humans (agreement) and vs each other (consistency)

Typical pilot (10 papers for agreement, 3 of them re-run for consistency):

    python -m peerreviewagents.eval fetch  --venue ICLR.cc/2025/Conference --limit 10 --out data/eval
    python -m peerreviewagents.eval run     --dir data/eval --repeats 1
    python -m peerreviewagents.eval run     --dir data/eval --repeats 3 --only ID1,ID2,ID3
    python -m peerreviewagents.eval metrics --dir data/eval

For a publication comparison, see ``docs/EVALUATION.md`` and use ``compare``
after running the same frozen corpus in ``system`` and ``single-llm`` modes.
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any

from ..default_config import get_config


def _corpus_path(d: str) -> str:
    return os.path.join(d, "corpus.jsonl")


def _runs_path(d: str) -> str:
    return os.path.join(d, "runs.jsonl")


def _build_config(args) -> dict:
    # Venue recommendation occurs after the decision and cannot affect any
    # evaluation endpoint. Excluding it makes cost/latency comparisons honest.
    overrides: dict = {"enable_journal_recommender": False}
    if getattr(args, "provider", None):
        overrides["provider"] = args.provider
    if getattr(args, "model", None):
        overrides["reasoning_model"] = args.model
    if getattr(args, "strictness", None):
        overrides["review_strictness"] = args.strictness
    if getattr(args, "journal", None) is not None:
        overrides["target_journal"] = args.journal
    if getattr(args, "article_type", None) is not None:
        overrides["article_type"] = args.article_type
    if getattr(args, "desk_screen", False):
        overrides["desk_screen"] = True
    if getattr(args, "no_debate", False):
        overrides["enable_debate"] = False
    if getattr(args, "debate_rounds", None) is not None:
        overrides["max_debate_rounds"] = args.debate_rounds
    if getattr(args, "single_model", False):
        overrides["single_model"] = True
    if getattr(args, "offline", False):
        overrides["research_enabled"] = False
    return get_config(config_path=getattr(args, "config", None), **overrides)


def cmd_inspect(args) -> int:
    from .corpus import inspect_venue

    summary = inspect_venue(args.venue)
    return 0 if summary["n_reviews"] else 1


def cmd_fetch(args) -> int:
    from .corpus import _DECISION_FIELDS, _RATING_FIELDS, fetch_corpus

    note = args.leakage_note
    if not note:
        note = (
            f"Venue {args.venue} may predate the model's training cutoff; "
            "treat agreement on these papers as potentially leakage-inflated."
        )
    rating_fields = (args.rating_field,) if args.rating_field else _RATING_FIELDS
    decision_fields = (args.decision_field,) if args.decision_field else _DECISION_FIELDS
    scan_cap = args.scan_cap or args.limit * 12
    items = fetch_corpus(
        args.venue, limit=args.limit, out_dir=args.out, leakage_note=note,
        rating_fields=rating_fields, decision_fields=decision_fields,
        scan_cap=scan_cap,
    )
    return 0 if items else 1


def cmd_freeze(args) -> int:
    from .corpus import verify_corpus_manifest, write_corpus_manifest

    corpus_path = _corpus_path(args.dir)
    if not os.path.exists(corpus_path):
        print(f"No corpus at {corpus_path} — run `fetch` first.", file=sys.stderr)
        return 1
    manifest = write_corpus_manifest(
        corpus_path,
        frozen_manually=True,
        leakage_note=args.leakage_note,
    )
    verified = verify_corpus_manifest(corpus_path)
    print(f"Wrote and verified {manifest} ({verified['n_papers']} papers).")
    return 0


def cmd_plan(args) -> int:
    """Preselect the repeatability subset before any model results exist."""
    import json
    import random

    from .corpus import load_corpus, verify_corpus_manifest

    corpus_path = _corpus_path(args.dir)
    manifest = verify_corpus_manifest(corpus_path)
    if manifest is None:
        print("Corpus is not frozen — run `freeze` first.", file=sys.stderr)
        return 1
    corpus = load_corpus(corpus_path)
    by_class = {
        label: sorted(c.id for c in corpus if c.human_decision == label)
        for label in ("accept", "reject")
    }
    n_accept = (args.repeat_papers + 1) // 2
    n_reject = args.repeat_papers // 2
    if len(by_class["accept"]) < n_accept or len(by_class["reject"]) < n_reject:
        print("Not enough papers in both classes for the requested repeat subset.", file=sys.stderr)
        return 1
    rng = random.Random(args.seed)
    repeat_ids = sorted(
        rng.sample(by_class["accept"], n_accept) + rng.sample(by_class["reject"], n_reject)
    )
    protocol = {
        "schema_version": 1,
        "corpus_sha256": manifest["corpus_sha256"],
        "primary_endpoint": "Spearman correlation with mean human rating",
        "secondary_endpoints": [
            "balanced accept/reject accuracy",
            "Cohen's kappa",
            "completion rate",
            "cost and latency",
            "repeat-run verdict agreement and score dispersion",
        ],
        "comparison": "full PeerReviewAgents vs single-LLM practical baseline",
        "comparison_caveat": "not compute-matched and not a causal architecture ablation",
        "bootstrap_samples": 2000,
        "bootstrap_seed": 20260815,
        "repeatability": {
            "seed": args.seed,
            "papers": repeat_ids,
            "runs_per_paper": args.repeats,
        },
        "required_controls": [
            "same provider and reasoning model",
            "single_model=true",
            "research_enabled=false",
            "journal recommender disabled",
            "one venue and pinned OpenReview rating field",
        ],
    }
    out = args.out or os.path.join(args.dir, "protocol.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(protocol, fh, indent=2)
    print(f"Wrote {out}")
    print("Repeat subset for --only: " + ",".join(repeat_ids))
    return 0


def cmd_run(args) -> int:
    config = _build_config(args)
    only = [s.strip() for s in args.only.split(",") if s.strip()] if args.only else None
    corpus_path = _corpus_path(args.dir)
    if not os.path.exists(corpus_path):
        print(f"No corpus at {corpus_path} — run `fetch` first.", file=sys.stderr)
        return 1

    if args.mode == "single-llm":
        # Degenerate one-call ablation. Writes to a model-namespaced file so
        # several models can be benchmarked without colliding on resume keys.
        from .baseline import baseline_runs_path, run_baseline_batch

        runs_path = args.runs_out or baseline_runs_path(args.dir, config)
        run_baseline_batch(
            corpus_path, runs_path, config,
            repeats=args.repeats, only=only, leakage_note=args.leakage_note,
        )
        return 0

    from .runner import run_batch

    run_batch(
        corpus_path,
        args.runs_out or _runs_path(args.dir),
        config,
        repeats=args.repeats,
        only=only,
        leakage_note=args.leakage_note,
    )
    return 0


def cmd_figure(args) -> int:
    from .figure import make_figure

    corpus_path = _corpus_path(args.dir)
    runs_path = args.runs or _runs_path(args.dir)
    if not os.path.exists(runs_path):
        print(f"No runs at {runs_path} — run `run` first.", file=sys.stderr)
        return 1
    svg, png = make_figure(corpus_path, runs_path, args.out, title=args.title or None)
    print(f"Wrote {svg} and {png}")
    return 0


def cmd_ablation_figure(args) -> int:
    from .figure import make_ablation_figure

    files = [s.strip() for s in args.runs.split(",") if s.strip()]
    labels = [s.strip() for s in args.labels.split(",")]
    if len(files) != len(labels):
        print("--labels count must match --runs count", file=sys.stderr)
        return 1
    svg, png = make_ablation_figure(_corpus_path(args.dir), files, labels,
                                    args.out, title=args.title or None)
    print(f"Wrote {svg} and {png}")
    return 0


def cmd_strictness_figure(args) -> int:
    from .figure import make_strictness_figure

    files = [s.strip() for s in args.runs.split(",") if s.strip()]
    labels = [s.strip() for s in args.labels.split(",")]
    if len(files) != len(labels):
        print("--labels count must match --runs count", file=sys.stderr)
        return 1
    svg, png = make_strictness_figure(_corpus_path(args.dir), files, labels,
                                      args.out, title=args.title or None)
    print(f"Wrote {svg} and {png}")
    return 0


def cmd_metrics(args) -> int:
    from .metrics import build_report, render_markdown, write_report

    corpus_path = _corpus_path(args.dir)
    runs_path = args.runs or _runs_path(args.dir)
    if not os.path.exists(runs_path):
        print(f"No runs at {runs_path} — run `run` first.", file=sys.stderr)
        return 1
    report = build_report(corpus_path, runs_path)
    print(render_markdown(report))
    out_prefix = args.out or os.path.join(args.dir, "report")
    json_path, md_path = write_report(report, out_prefix)
    print(f"\nWrote {md_path} and {json_path}")
    return 0


def cmd_compare(args) -> int:
    from .comparison import build_comparison, render_markdown, write_comparison

    report = build_comparison(_corpus_path(args.dir), args.system_runs, args.baseline_runs)
    print(render_markdown(report))
    out = args.out or os.path.join(args.dir, "comparison")
    json_path, md_path = write_comparison(report, out)
    print(f"\nWrote {md_path} and {json_path}")
    return 0


def cmd_sweep(args) -> int:
    """Tabulate one metric across several runs files, one column per label.

    Generic over any sweep: point ``--runs`` at the per-condition files and
    ``--labels`` at their condition values (e.g. strictness 1..5). Reports the
    chosen metric (weighted score by default, or decision) per paper per
    condition, plus the per-condition mean — so you can see how the score
    moves as the condition changes.
    """
    import json as _json

    from .corpus import load_corpus
    from .schema import RunRecord, read_jsonl

    files = [s.strip() for s in args.runs.split(",") if s.strip()]
    labels = ([s.strip() for s in args.labels.split(",")]
              if args.labels else [str(i + 1) for i in range(len(files))])
    if len(labels) != len(files):
        print("--labels count must match --runs count", file=sys.stderr)
        return 1

    titles = {c.id: c.title for c in load_corpus(_corpus_path(args.dir))}
    table: dict[str, dict[str, Any]] = {}
    means: dict[str, float | None] = {}
    for path, lab in zip(files, labels):
        if not os.path.exists(path):
            print(f"missing runs file: {path}", file=sys.stderr)
            return 1
        seen: set[str] = set()
        vals: list[float] = []
        for d in read_jsonl(path):
            r = RunRecord.from_dict(d)
            if not r.ok or r.paper_id in seen:
                continue
            seen.add(r.paper_id)
            v = r.system_weighted_score if args.metric == "weighted_score" else r.system_decision
            table.setdefault(r.paper_id, {})[lab] = v
            if args.metric == "weighted_score" and isinstance(v, (int, float)):
                vals.append(float(v))
        means[lab] = round(sum(vals) / len(vals), 3) if vals else None

    # Markdown table: one row per paper, one column per label, mean at the foot.
    header = "| paper | " + " | ".join(labels) + " |"
    sep = "|" + "---|" * (len(labels) + 1)
    lines = [f"# Sweep ({args.metric})", "",
             f"Files: {', '.join(files)}", "", header, sep]
    for pid in sorted(table):
        cells = [str(table[pid].get(lab, "")) for lab in labels]
        short = (titles.get(pid, "") or "")[:40]
        lines.append(f"| `{pid}` {short} | " + " | ".join(cells) + " |")
    if args.metric == "weighted_score":
        mean_cells = ["" if means[lab] is None else f"{means[lab]:.3f}" for lab in labels]
        lines += [sep.replace("---", ":-:"), "| **mean** | " + " | ".join(mean_cells) + " |"]
    md = "\n".join(lines)
    print(md)

    out_prefix = args.out or os.path.join(args.dir, "sweep")
    with open(f"{out_prefix}.md", "w", encoding="utf-8") as fh:
        fh.write(md + "\n")
    with open(f"{out_prefix}.json", "w", encoding="utf-8") as fh:
        _json.dump({"metric": args.metric, "labels": labels, "files": files,
                    "table": table, "means": means}, fh, indent=2)
    print(f"\nWrote {out_prefix}.md and {out_prefix}.json")
    return 0


def _add_config_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("--config", help="explicit peerreview.toml path")
    p.add_argument("--provider", help="override provider (openrouter/openai/anthropic)")
    p.add_argument("--model", help="override reasoning model id")
    p.add_argument("--strictness", help="review strictness level")
    p.add_argument("--journal", help="target journal slug (e.g. ml-conference, general)")
    p.add_argument("--article-type", dest="article_type",
                   help="manuscript type (e.g. conference-paper, article)")
    p.add_argument("--desk-screen", action="store_true", dest="desk_screen",
                   help="enable the desk-screen triage gate")
    p.add_argument("--no-debate", action="store_true", dest="no_debate",
                   help="omit advocate/skeptic deliberation; the completed panel "
                        "feeds the Editor-in-Chief directly")
    p.add_argument("--debate-rounds", type=int, dest="debate_rounds",
                   help="override max debate rounds (default 2)")
    p.add_argument("--leakage-note", default="",
                   help="free-text note stamped on every run's manifest")
    p.add_argument("--single-model", action="store_true",
                   help="force every full-workflow role to use reasoning_model; "
                        "recommended for a controlled baseline comparison")
    p.add_argument("--offline", action="store_true",
                   help="disable literature-search tools; recommended to reduce "
                        "outcome leakage during evaluation")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m peerreviewagents.eval")
    sub = p.add_subparsers(dest="cmd", required=True)

    i = sub.add_parser("inspect", help="print a venue's review/decision fields (run before fetch)")
    i.add_argument("--venue", required=True, help="e.g. ICLR.cc/2025/Conference")
    i.set_defaults(func=cmd_inspect)

    f = sub.add_parser("fetch", help="fetch a labeled OpenReview corpus")
    f.add_argument("--venue", required=True, help="e.g. ICLR.cc/2025/Conference")
    f.add_argument("--limit", type=int, default=10)
    f.add_argument("--out", default="data/eval", help="output dir (holds corpus.jsonl + pdfs/)")
    f.add_argument("--scan-cap", type=int, dest="scan_cap", default=0,
                   help="max submissions to scan (default: limit*12, since many are withdrawn)")
    f.add_argument("--rating-field", help="pin the review rating field (see `inspect`)")
    f.add_argument("--decision-field", help="pin the decision field (see `inspect`)")
    f.add_argument("--leakage-note", default="")
    f.set_defaults(func=cmd_fetch)

    fr = sub.add_parser("freeze", help="fingerprint an existing corpus and every PDF")
    fr.add_argument("--dir", default="data/eval", help="dir holding corpus.jsonl + pdfs/")
    fr.add_argument("--leakage-note", default="",
                    help="study-specific leakage assessment recorded in the manifest")
    fr.set_defaults(func=cmd_freeze)

    pl = sub.add_parser("plan", help="freeze endpoints and select a repeatability subset")
    pl.add_argument("--dir", default="data/eval", help="dir holding a frozen corpus")
    pl.add_argument("--repeat-papers", type=int, default=6,
                    help="balanced number of papers to repeat (default 6)")
    pl.add_argument("--repeats", type=int, default=3,
                    help="total full-PRA runs per selected paper (default 3)")
    pl.add_argument("--seed", type=int, default=20260815)
    pl.add_argument("--out", help="protocol JSON path (default <dir>/protocol.json)")
    pl.set_defaults(func=cmd_plan)

    r = sub.add_parser("run", help="review corpus papers into runs.jsonl (resumable)")
    r.add_argument("--dir", default="data/eval", help="dir holding corpus.jsonl")
    r.add_argument("--repeats", type=int, default=1, help="runs per paper to ensure exist")
    r.add_argument("--only", help="comma-separated paper ids to restrict to")
    r.add_argument("--mode", choices=("system", "single-llm"), default="system",
                   help="'system' = full pipeline (default); 'single-llm' = the "
                        "one-call practical baseline (not compute-matched).")
    r.add_argument("--runs-out", dest="runs_out",
                   help="override the runs output path (single-llm defaults to a "
                        "model-namespaced runs_baseline_<model>.jsonl).")
    _add_config_flags(r)
    r.set_defaults(func=cmd_run)

    m = sub.add_parser("metrics", help="score runs vs humans and vs each other")
    m.add_argument("--dir", default="data/eval", help="dir holding corpus.jsonl + runs.jsonl")
    m.add_argument("--runs", help="runs file to score (default <dir>/runs.jsonl; point at "
                                  "a runs_baseline_*.jsonl to score the baseline).")
    m.add_argument("--out", help="report path prefix (default <dir>/report)")
    m.set_defaults(func=cmd_metrics)

    c = sub.add_parser("compare", help="paired full-PRA vs single-LLM report")
    c.add_argument("--dir", default="data/eval", help="dir holding corpus.jsonl")
    c.add_argument("--system-runs", required=True,
                   help="full-workflow runs JSONL")
    c.add_argument("--baseline-runs", required=True,
                   help="single-LLM runs JSONL")
    c.add_argument("--out", help="output prefix (default <dir>/comparison)")
    c.set_defaults(func=cmd_compare)

    g = sub.add_parser("figure", help="render the agreement + consistency figure (SVG+PNG)")
    g.add_argument("--dir", default="data/eval", help="dir holding corpus.jsonl + runs.jsonl")
    g.add_argument("--runs", help="runs file to plot (default <dir>/runs.jsonl).")
    g.add_argument("--out", default="paper/figures/eval_results",
                   help="output path prefix (default paper/figures/eval_results)")
    g.add_argument("--title", default="", help="optional figure suptitle")
    g.set_defaults(func=cmd_figure)

    af = sub.add_parser("ablation-figure",
                        help="plot agreement metrics across configs (one runs file each)")
    af.add_argument("--dir", default="data/eval", help="dir holding corpus.jsonl")
    af.add_argument("--runs", required=True, help="comma-separated runs files, ordered")
    af.add_argument("--labels", required=True, help="comma-separated config labels")
    af.add_argument("--out", default="paper/figures/eval_ablation", help="output path prefix")
    af.add_argument("--title", default="", help="optional figure title")
    af.set_defaults(func=cmd_ablation_figure)

    sf = sub.add_parser("strictness-figure",
                        help="plot per-paper + mean weighted score across conditions")
    sf.add_argument("--dir", default="data/eval", help="dir holding corpus.jsonl")
    sf.add_argument("--runs", required=True, help="comma-separated runs files, ordered")
    sf.add_argument("--labels", required=True, help="comma-separated level labels")
    sf.add_argument("--out", default="paper/figures/eval_strictness", help="output path prefix")
    sf.add_argument("--title", default="", help="optional figure title")
    sf.set_defaults(func=cmd_strictness_figure)

    s = sub.add_parser("sweep", help="tabulate a metric across several runs files by label")
    s.add_argument("--dir", default="data/eval", help="dir holding corpus.jsonl")
    s.add_argument("--runs", required=True,
                   help="comma-separated runs files, one per condition")
    s.add_argument("--labels",
                   help="comma-separated labels matching --runs (default 1,2,3,...)")
    s.add_argument("--metric", choices=("weighted_score", "decision"),
                   default="weighted_score", help="value to tabulate per paper")
    s.add_argument("--out", help="report path prefix (default <dir>/sweep)")
    s.set_defaults(func=cmd_sweep)

    return p


def main(argv: list[str] | None = None) -> int:
    # Load .env so OPENROUTER_API_KEY / DATALAB_API_KEY reach the pipeline,
    # exactly as the main `peerreview` CLI does.
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
