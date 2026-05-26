"""PeerReviewAgents command-line entry point.

Usage:
    peerreview <manuscript> [options]      # launches the Textual TUI
    peerreview <manuscript> --no-tui       # headless run with live Rich output
"""

from __future__ import annotations

import argparse
import os
import sys

from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel

from peerreviewagents.default_config import get_config
from peerreviewagents.graph.review_graph import PeerReviewGraph
from peerreviewagents.reports import write_reports
from peerreviewagents.agents.utils.memory import append_memory

console = Console()

_NODE_LABELS = {
    "_ingest": "Ingesting manuscript",
    "advocate": "Advocate argues",
    "skeptic": "Skeptic responds",
    "meta_reviewer": "Area Chair synthesizes",
    "editor": "Editor-in-Chief decides",
}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="peerreview", description="Multi-agent peer review")
    p.add_argument("manuscript", nargs="?", help="Path to manuscript (pdf/md/tex/docx)")
    p.add_argument("--provider", help="anthropic|openai|google|openrouter|ollama")
    p.add_argument("--deep-model", dest="deep_think_llm")
    p.add_argument("--quick-model", dest="quick_think_llm")
    p.add_argument("--base-url", dest="base_url")
    p.add_argument("--reviewers", help="comma-separated reviewer set")
    p.add_argument("--debate-rounds", type=int, dest="max_debate_rounds")
    p.add_argument("--no-research", action="store_true")
    p.add_argument("--pdf", action="store_true")
    p.add_argument("--output-dir", dest="output_dir")
    p.add_argument("--no-tui", action="store_true", help="run headless")
    return p


def config_from_args(args) -> dict:
    overrides = {}
    for key in ("provider", "deep_think_llm", "quick_think_llm", "base_url",
                "max_debate_rounds", "output_dir"):
        val = getattr(args, key, None)
        if val is not None:
            overrides[key] = val
    if args.reviewers:
        overrides["reviewer_set"] = [r.strip() for r in args.reviewers.split(",") if r.strip()]
    if args.no_research:
        overrides["research_enabled"] = False
    if args.pdf:
        overrides["emit_pdf"] = True
    return get_config(**overrides)


def run_headless(manuscript: str, config: dict) -> None:
    console.print(Panel.fit(f"[bold]PeerReviewAgents[/bold]\n{manuscript}", border_style="cyan"))
    graph = PeerReviewGraph(config)
    final = {}
    for node, partial in graph.stream(manuscript):
        final.update(partial)
        label = _NODE_LABELS.get(node, node.replace("reviewer_", "Reviewer: ").replace("integrity_", "Integrity: "))
        console.print(f"  [green]✓[/green] {label}")
    run_dir = write_reports(final)
    try:
        append_memory(final)
    except Exception:  # noqa: BLE001
        pass
    decision = final.get("decision", "n/a")
    console.print(Panel.fit(f"[bold]Decision:[/bold] {decision}\nReports: {run_dir}", border_style="green"))


def run() -> None:
    load_dotenv()
    args = build_parser().parse_args()
    if not args.manuscript:
        console.print("[red]Provide a manuscript path.[/red] See --help.")
        sys.exit(1)
    if not os.path.exists(args.manuscript):
        console.print(f"[red]File not found:[/red] {args.manuscript}")
        sys.exit(1)
    config = config_from_args(args)

    if args.no_tui:
        run_headless(args.manuscript, config)
        return
    try:
        from .tui import run_tui

        run_tui(args.manuscript, config)
    except Exception as exc:  # noqa: BLE001
        console.print(f"[yellow]TUI unavailable ({exc}); running headless.[/yellow]")
        run_headless(args.manuscript, config)


if __name__ == "__main__":
    run()
