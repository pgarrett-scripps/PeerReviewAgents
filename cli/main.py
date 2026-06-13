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
from peerreviewagents.storage.memory import MemoryLog

console = Console()

_NODE_LABELS = {
    "_ingest_start": "Parsing manuscript (PDFs can take a few minutes)",
    "_ingest": "Manuscript ingested",
    "audit_methods_completeness": "Methods-completeness audit",
    "audit_citation_integrity": "Citation-integrity audit",
    "advocate": "Advocate argues",
    "skeptic": "Skeptic responds",
    "meta_reviewer": "Area Chair synthesizes",
    "author_rebuttal": "Author rebuts",
    "desk_screen": "Editor desk-screens (triage)",
    "editor": "Editor-in-Chief decides",
    "journal_recommender": "Journal Scout suggests venues",
}

_VALID_DECISIONS = {"accept", "minor", "major", "reject"}


def _run_failed(state: dict) -> str | None:
    """Return a reason string if the run did not produce a real review, else None."""
    # A desk reject is a valid terminal outcome with no reviewer reports.
    if state.get("desk_rejected") and state.get("decision") in _VALID_DECISIONS:
        return None
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
            "  peerreview paper.pdf --reasoning-model anthropic/claude-opus-4.1\n"
            "  peerreview paper.pdf --provider anthropic --reasoning-model claude-opus-4-7\n\n"
            "Run `just cache-clear` to wipe the manuscript parsing cache.\n\n"
            "Required env vars (one of, per --provider):\n"
            "  OPENROUTER_API_KEY  — provider=openrouter  (default)\n"
            "  ANTHROPIC_API_KEY   — provider=anthropic\n"
            "  OPENAI_API_KEY      — provider=openai\n"
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
        "--provider",
        dest="provider",
        choices=("openrouter", "anthropic", "openai"),
        help="LLM provider (default: openrouter).",
    )
    p.add_argument(
        "--reasoning-model",
        dest="reasoning_model",
        help="Model id for the active provider (e.g. anthropic/claude-opus-4.1 "
             "on OpenRouter, claude-opus-4-7 on Anthropic direct, gpt-4.1 on OpenAI).",
    )
    p.add_argument("--debate-rounds", type=int, dest="max_debate_rounds")
    p.add_argument(
        "--strictness",
        type=int,
        dest="review_strictness",
        choices=range(1, 6),
        metavar="{1-5}",
        help="How harsh the panel is: 1=very lenient, 3=balanced (default), "
             "5=very strict. Calibrates the reviewer, meta-reviewer, and "
             "editor.",
    )
    p.add_argument(
        "--desk-screen",
        dest="desk_screen",
        action="store_const",
        const=True,
        default=None,
        help="Enable the editorial desk-screen gate: a triage pass that can "
             "desk-reject (out-of-scope / incomplete / fatal-flaw) before the "
             "full panel runs. Off by default.",
    )
    p.add_argument(
        "--no-memory",
        dest="use_memory",
        action="store_const",
        const=False,
        default=None,
        help="Disable the cross-run memory loop for this run: retrieve no "
             "past lessons and do not append this run to the log. On by default.",
    )
    p.add_argument(
        "--journal",
        dest="target_journal",
        help="Slug of a target journal to review against (see "
             "--list-journals). Reviews venue-agnostically if omitted.",
    )
    p.add_argument(
        "--list-journals",
        action="store_true",
        help="List available target-journal slugs and exit.",
    )
    p.add_argument(
        "--article-type",
        dest="article_type",
        help="Kind of submission being reviewed (see --list-article-types): "
             "article, letter, communication, perspective, review, "
             "technical-note, tutorial. No manuscript-type framing if omitted.",
    )
    p.add_argument(
        "--list-article-types",
        action="store_true",
        help="List available article-type keys and exit.",
    )
    p.add_argument("--output-dir", dest="output_dir")
    p.add_argument(
        "--si",
        dest="supplement_path",
        help="Optional supplementary-information file (pdf/md/tex/docx). Passed "
             "in full to the methods-completeness auditor only. Ignored if omitted.",
    )
    p.add_argument("--no-tui", action="store_true", help="run headless")
    p.add_argument("--cache-dir", dest="cache_dir",
                   help="Override the manuscript parsing cache directory.")
    return p


def config_from_args(args) -> dict:
    overrides = {}
    for key in ("provider", "reasoning_model", "max_debate_rounds",
                "output_dir", "cache_dir", "target_journal", "article_type",
                "review_strictness", "desk_screen", "use_memory",
                "supplement_path"):
        val = getattr(args, key, None)
        if val is not None:
            overrides[key] = val
    return get_config(config_path=args.config_path, **overrides)


def _print_journals(config: dict) -> None:
    """Print the available target-journal slugs and names."""
    from peerreviewagents.journals import list_journals

    profiles = list_journals(config)
    if not profiles:
        console.print("[yellow]No journal profiles found.[/yellow]")
        return
    default = config.get("target_journal") or ""
    console.print("[bold]Available target journals[/bold] (use with --journal <slug>):\n")
    for p in profiles:
        marker = "  [dim](default)[/dim]" if p.slug == default else ""
        console.print(f"  [cyan]{p.slug}[/cyan] — {p.name}{marker}")
    console.print(
        "\nUse [cyan]--journal \"\"[/cyan] for a fully venue-agnostic review "
        "(no journal framing)."
    )


def _validate_target_journal(config: dict) -> None:
    """Fail fast (with the available slugs) if --journal names a slug that
    doesn't resolve, rather than silently reviewing against no venue."""
    slug = config.get("target_journal")
    if not slug:
        return
    from peerreviewagents.journals import load_journal

    try:
        load_journal(slug, config)
    except FileNotFoundError as exc:
        console.print(f"[red]{exc}[/red]")
        console.print("Run [bold]peerreview --list-journals[/bold] to see valid slugs.")
        sys.exit(1)


def _validate_strictness(config: dict) -> None:
    """Fail fast if review_strictness (from any source — flag, env, TOML)
    isn't an integer 1-5, rather than silently clamping mid-run."""
    from peerreviewagents.strictness import normalize_strictness

    try:
        normalize_strictness(config.get("review_strictness"))
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        sys.exit(1)


def _print_article_types(config: dict) -> None:
    """Print the selectable article-type keys and what each is for."""
    from peerreviewagents.article_types import ARTICLE_TYPES

    default = config.get("article_type") or ""
    console.print(
        "[bold]Available article types[/bold] (use with --article-type <key>):\n"
    )
    for at in ARTICLE_TYPES.values():
        marker = "  [dim](selected)[/dim]" if at.key == default else ""
        console.print(f"  [cyan]{at.key}[/cyan] — {at.name}: {at.description}{marker}")
    console.print(
        "\nPer-type word limits come from the target journal's profile. "
        "Omit [cyan]--article-type[/cyan] for no manuscript-type framing."
    )


def _validate_article_type(config: dict) -> None:
    """Fail fast if article_type (from any source) isn't a known type key."""
    from peerreviewagents.article_types import normalize_article_type

    try:
        normalize_article_type(config.get("article_type"))
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        console.print(
            "Run [bold]peerreview --list-article-types[/bold] to see valid keys."
        )
        sys.exit(1)


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
    job_id = os.path.basename(run_dir.rstrip(os.sep))
    try:
        _append_pending_memory(final, job_id, config)
    except Exception:  # noqa: BLE001
        pass
    cost = final.get("total_cost") or 0.0
    cost_line = f"\nCost: ${cost:.4f}" if cost > 0 else ""
    outcome_hint = (
        f"\n\nWhen you know the venue's outcome, record it with:\n"
        f"  peerreview outcome {job_id} {{accepted|rejected|minor|major|withdrawn}}"
        if config.get("use_memory", True) else ""
    )
    console.print(Panel.fit(
        f"[bold]Decision:[/bold] {final['decision']}\n"
        f"Reports: {run_dir}\n"
        f"Job ID: {job_id}{cost_line}{outcome_hint}",
        border_style="green",
    ))


def _append_pending_memory(state: dict, job_id: str, config: dict) -> None:
    """Write a pending entry to the review memory log. Called from both
    the headless CLI and the TUI. No-op when memory is disabled for the run."""
    if not config.get("use_memory", True):
        return
    sections = state.get("sections") or {}
    abstract = sections.get("abstract") or state.get("manuscript_md", "")[:500]
    MemoryLog(config["memory_path"]).append_pending(
        job_id=job_id,
        title=state.get("manuscript_title", ""),
        abstract=abstract,
        decision=state.get("decision", ""),
        draft_summary=state.get("decision_letter", ""),
        reports=state.get("reports", []),
    )


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
    for key in ("provider", "reasoning_model", "max_debate_rounds",
                "output_dir", "cache_dir", "target_journal", "article_type",
                "review_strictness", "desk_screen"):
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


def run_outcome(args) -> None:
    """Mark a past review's real-world outcome and (optionally) reflect on it."""
    config = get_config(config_path=args.config_path)
    log = MemoryLog(config["memory_path"])

    llm = None
    if not args.no_reflect:
        try:
            from peerreviewagents.agents.utils.llm import make_llm

            llm = make_llm(config)
        except Exception as exc:  # noqa: BLE001
            console.print(
                f"[yellow]Could not initialize LLM for reflection ({exc}); "
                "recording outcome without a lesson.[/yellow]"
            )
            llm = None

    try:
        entry = log.mark_resolved(args.job_id, args.outcome, llm=llm, config=config)
    except KeyError:
        console.print(f"[red]No memory entry found for job_id '{args.job_id}'.[/red]")
        console.print(
            f"Check the log at: {config['memory_path']} "
            "(use the Job ID printed at the end of a review run)."
        )
        sys.exit(1)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        sys.exit(1)

    lesson = entry.lesson or "(reflection skipped)"
    console.print(Panel.fit(
        f"[bold]Outcome recorded:[/bold] {entry.outcome}\n"
        f"[bold]Original decision:[/bold] {entry.decision}\n"
        f"[bold]Lesson:[/bold] {lesson}",
        border_style="green",
    ))


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
        sp.add_argument("--provider", dest="provider",
                        choices=("openrouter", "anthropic", "openai"), default=None)
        sp.add_argument("--reasoning-model", dest="reasoning_model", default=None)
        sp.add_argument("--debate-rounds", type=int, dest="max_debate_rounds", default=None)
        sp.add_argument("--output-dir", dest="output_dir", default=None)
        sp.add_argument("--cache-dir", dest="cache_dir", default=None)
        sp.add_argument("--journal", dest="target_journal", default=None,
                        help="Default target-journal slug for jobs (the web "
                             "form can override per-upload).")
        sp.add_argument("--article-type", dest="article_type", default=None,
                        help="Default article-type key for jobs (the web "
                             "form can override per-upload).")
        sp.add_argument("--strictness", type=int, dest="review_strictness",
                        choices=range(1, 6), metavar="{1-5}", default=None,
                        help="Default review strictness for jobs 1-5 (the web "
                             "form can override per-upload).")
        sp.add_argument("--desk-screen", dest="desk_screen",
                        action="store_const", const=True, default=None,
                        help="Enable the desk-screen gate by default for jobs "
                             "(the web form can override per-upload).")
        run_server(sp.parse_args(argv[1:]))
        return

    if argv and argv[0] == "outcome":
        sp = argparse.ArgumentParser(
            prog="peerreview outcome",
            description="Mark a past review's real-world outcome and reflect.",
        )
        sp.add_argument("job_id", help="Job ID printed at the end of the review run.")
        sp.add_argument(
            "outcome",
            choices=("accepted", "rejected", "minor", "major", "withdrawn"),
            help="What actually happened to the manuscript at the venue.",
        )
        sp.add_argument("--config", dest="config_path", default=None)
        sp.add_argument(
            "--no-reflect", action="store_true",
            help="Record the outcome but skip the LLM reflection step.",
        )
        run_outcome(sp.parse_args(argv[1:]))
        return

    args = build_parser().parse_args()
    if args.list_journals:
        _print_journals(config_from_args(args))
        return
    if args.list_article_types:
        _print_article_types(config_from_args(args))
        return
    if not args.manuscript:
        console.print("[red]Provide a manuscript path or use the `serve` subcommand.[/red] See --help.")
        sys.exit(1)
    if not os.path.exists(args.manuscript):
        console.print(f"[red]File not found:[/red] {args.manuscript}")
        sys.exit(1)
    config = config_from_args(args)
    _validate_target_journal(config)
    _validate_article_type(config)
    _validate_strictness(config)

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
