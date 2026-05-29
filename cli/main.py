"""PeerReviewAgents command-line entry point.

Usage:
    peerreview <manuscript> [options]      # launches the Textual TUI
    peerreview <manuscript> --no-tui       # headless run with live Rich output
    peerreview serve [options]             # launch the FastAPI web UI
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
    "author_rebuttal": "Author rebuts",
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
        description="Multi-agent peer review (OpenRouter-backed)",
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
            "  peerreview paper.pdf --reasoning-model anthropic/claude-opus-4.1\n\n"
            "Run `just cache-clear` to wipe the manuscript parsing cache.\n\n"
            "Required env vars:\n"
            "  OPENROUTER_API_KEY (or OPENAI_API_KEY) — text + vision + web search\n"
            "  DATALAB_API_KEY                        — PDF ingest\n"
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
    p.add_argument(
        "--reasoning-model",
        dest="reasoning_model",
        help="OpenRouter slug for the text model (e.g. anthropic/claude-opus-4.1).",
    )
    p.add_argument(
        "--vision-model",
        dest="vision_model",
        help="OpenRouter slug for the figure-description model.",
    )
    p.add_argument("--debate-rounds", type=int, dest="max_debate_rounds")
    p.add_argument("--output-dir", dest="output_dir")
    p.add_argument("--no-tui", action="store_true", help="run headless")
    p.add_argument("--cache-dir", dest="cache_dir",
                   help="Override the manuscript parsing cache directory.")
    return p


def config_from_args(args) -> dict:
    overrides = {}
    for key in ("reasoning_model", "vision_model", "max_debate_rounds",
                "output_dir", "cache_dir"):
        val = getattr(args, key, None)
        if val is not None:
            overrides[key] = val
    return get_config(config_path=args.config_path, **overrides)


def run_headless(manuscript: str, config: dict) -> None:
    console.print(Panel.fit(f"[bold]PeerReviewAgents[/bold]\n{manuscript}", border_style="cyan"))
    graph = PeerReviewGraph(config)
    final = {}
    for node, partial in graph.stream(manuscript):
        final.update(partial)
        label = _NODE_LABELS.get(node, node.replace("reviewer_", "Reviewer: "))
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
    cost = final.get("total_cost") or 0.0
    cost_line = f"\nCost: ${cost:.4f}" if cost > 0 else ""
    console.print(Panel.fit(
        f"[bold]Decision:[/bold] {final['decision']}\nReports: {run_dir}{cost_line}",
        border_style="green",
    ))


def run_server(args) -> None:
    """Launch the FastAPI web UI."""
    try:
        import uvicorn

        from peerreviewagents.web import create_app
    except ImportError as exc:
        console.print(f"[red]Web dependencies missing:[/red] {exc}")
        console.print("Run: [bold]uv pip install -e .[/bold] to pick them up.")
        sys.exit(1)

    overrides: dict = {}
    for key in ("reasoning_model", "vision_model", "max_debate_rounds",
                "output_dir", "cache_dir"):
        val = getattr(args, key, None)
        if val is not None:
            overrides[key] = val
    if args.config_path:
        overrides["__config_path__"] = args.config_path

    app = create_app(
        config_overrides={k: v for k, v in overrides.items() if not k.startswith("__")},
        upload_dir=args.upload_dir,
    )
    console.print(Panel.fit(
        f"[bold]PeerReviewAgents web UI[/bold]\nhttp://{args.host}:{args.port}",
        border_style="cyan",
    ))
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


def run() -> None:
    load_dotenv()
    argv = sys.argv[1:]
    # Hand off the `serve` subcommand to a dedicated parser so the
    # primary review CLI keeps its current positional-argument shape.
    if argv and argv[0] == "serve":
        sp = argparse.ArgumentParser(prog="peerreview serve",
                                     description="Launch the web UI")
        sp.add_argument("--host", default="127.0.0.1")
        sp.add_argument("--port", type=int, default=8765)
        sp.add_argument("--upload-dir", dest="upload_dir", default=None,
                        help="Where to store uploaded manuscripts "
                             "(default: ./.peerreview-uploads)")
        sp.add_argument("--config", dest="config_path", default=None)
        sp.add_argument("--reasoning-model", dest="reasoning_model", default=None)
        sp.add_argument("--vision-model", dest="vision_model", default=None)
        sp.add_argument("--debate-rounds", type=int, dest="max_debate_rounds", default=None)
        sp.add_argument("--output-dir", dest="output_dir", default=None)
        sp.add_argument("--cache-dir", dest="cache_dir", default=None)
        run_server(sp.parse_args(argv[1:]))
        return

    args = build_parser().parse_args()
    if not args.manuscript:
        console.print("[red]Provide a manuscript path or use the `serve` subcommand.[/red] See --help.")
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
