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
    "_ingest_start": "Parsing manuscript (PDFs can take a few minutes)",
    "_ingest": "Manuscript ingested",
    "advocate": "Advocate argues",
    "skeptic": "Skeptic responds",
    "meta_reviewer": "Area Chair synthesizes",
    "editor": "Editor-in-Chief decides",
}

_VALID_DECISIONS = {"accept", "minor", "major", "reject"}


def _run_failed(state: dict) -> str | None:
    """Return a reason string if the run did not produce a real review, else None."""
    if state.get("decision") not in _VALID_DECISIONS:
        return "Editor never produced a valid decision"
    if not state.get("reports"):
        return "No reviewer reports were produced"
    return None


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="peerreview",
        description="Multi-agent peer review",
        epilog=(
            "Configuration (lowest to highest precedence):\n"
            "  built-in defaults\n"
            "  ~/.config/peerreviewagents/config.toml   (user-global)\n"
            "  ./peerreview.toml                        (project-local)\n"
            "  --config <file>                          (explicit TOML)\n"
            "  PEERREVIEW_* env vars                    (one-off overrides)\n"
            "  CLI flags below                          (this run only)\n\n"
            "Secrets live in .env (or your shell) — see .env.example.\n"
            "Non-secret settings live in ./peerreview.toml.\n\n"
            "Examples:\n"
            "  peerreview paper.pdf --no-tui\n"
            "  peerreview paper.pdf --config profiles/thorough.toml\n"
            "  peerreview paper.pdf --provider openrouter \\\n"
            "      --deep-model anthropic/claude-opus-4.1\n\n"
            "Required env var per provider:\n"
            "  anthropic  -> ANTHROPIC_API_KEY\n"
            "  openai     -> OPENAI_API_KEY\n"
            "  openrouter -> OPENROUTER_API_KEY (or OPENAI_API_KEY)\n"
            "  google     -> GOOGLE_API_KEY\n"
            "  ollama     -> (none; runs locally)\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("manuscript", nargs="?", help="Path to manuscript (pdf/md/tex/docx)")
    p.add_argument(
        "--config",
        dest="config_path",
        help="Path to a TOML config file (overrides ./peerreview.toml and "
             "~/.config/peerreviewagents/config.toml).",
    )
    p.add_argument("--provider", help="anthropic|openai|google|openrouter|ollama (default: anthropic)")
    p.add_argument(
        "--deep-model",
        dest="deep_think_llm",
        help="Model slug for synthesis/judgement (e.g. claude-opus-4-7, anthropic/claude-opus-4.1)",
    )
    p.add_argument(
        "--quick-model",
        dest="quick_think_llm",
        help="Model slug for the parallel reviewer pass (e.g. claude-haiku-4-5-20251001)",
    )
    p.add_argument("--base-url", dest="base_url", help="Override API base URL (for custom gateways)")
    p.add_argument("--reviewers", help="comma-separated reviewer set")
    p.add_argument("--debate-rounds", type=int, dest="max_debate_rounds")
    p.add_argument("--no-research", action="store_true")
    p.add_argument(
        "--no-cache",
        action="store_true",
        help="Skip the manuscript cache (force re-parsing the file).",
    )
    p.add_argument("--pdf", action="store_true")
    p.add_argument("--output-dir", dest="output_dir")
    p.add_argument("--no-tui", action="store_true", help="run headless")
    p.add_argument("--temperature", type=float, dest="temperature",
                   help="Sampling temperature for all LLM calls (0.0-1.0).")
    p.add_argument("--cache-dir", dest="cache_dir",
                   help="Override the manuscript parsing cache directory.")
    return p


def config_from_args(args) -> dict:
    overrides = {}
    for key in ("provider", "deep_think_llm", "quick_think_llm", "base_url",
                "max_debate_rounds", "output_dir", "temperature", "cache_dir"):
        val = getattr(args, key, None)
        if val is not None:
            overrides[key] = val
    if args.reviewers:
        overrides["reviewer_set"] = [r.strip() for r in args.reviewers.split(",") if r.strip()]
    if args.no_research:
        overrides["research_enabled"] = False
    if args.no_cache:
        overrides["cache_enabled"] = False
    if args.pdf:
        overrides["emit_pdf"] = True
    return get_config(config_path=args.config_path, **overrides)


def run_headless(manuscript: str, config: dict) -> None:
    console.print(Panel.fit(f"[bold]PeerReviewAgents[/bold]\n{manuscript}", border_style="cyan"))
    graph = PeerReviewGraph(config)
    final = {}
    for node, partial in graph.stream(manuscript):
        final.update(partial)
        label = _NODE_LABELS.get(node, node.replace("reviewer_", "Reviewer: ").replace("integrity_", "Integrity: "))
        glyph = "[yellow]…[/yellow]" if node.endswith("_start") else "[green]✓[/green]"
        console.print(f"  {glyph} {label}")

    reason = _run_failed(final)
    if reason:
        errors = final.get("errors", []) or ["(no error details collected)"]
        body = f"{reason}.\n\nErrors:\n" + "\n".join(f"  • {e}" for e in errors)
        console.print(Panel.fit(
            f"[bold red]Review failed — no report written.[/bold red]\n\n{body}",
            border_style="red",
        ))
        sys.exit(2)

    run_dir = write_reports(final)
    try:
        append_memory(final)
    except Exception:  # noqa: BLE001
        pass
    console.print(Panel.fit(
        f"[bold]Decision:[/bold] {final['decision']}\nReports: {run_dir}",
        border_style="green",
    ))


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
