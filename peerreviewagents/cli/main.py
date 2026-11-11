"""The ``peerreview`` command: one evidence-linked review of a manuscript PDF.

``peerreview manuscript.pdf`` is the whole review. The PDF is imported into
``manuscript-review/manuscript``, two reviewers investigate it independently,
an audited debate synthesises them, and ``REVIEW.md`` is written into
``manuscript-review`` with every finding anchored to an exact quotation. The
same review named in full is ``peerreview review manuscript.pdf <folder>``.

Every engine command (``import``, ``verify``, ``review``, ``investigate``,
``report``, ``checks``, ``add-review``, ``result-map``) is here, plus ``serve``
for the browser UI over the same reviews. The review runs on a coding-agent
subscription installed on this machine — Claude Code by default, Codex with
``--provider codex`` — so there is no API key and no PeerReviewAgents service.
"""

from __future__ import annotations

import argparse
import sys

from peerreviewagents.engine.__main__ import COMMANDS, add_subcommands
from peerreviewagents.engine.__main__ import dispatch as engine_dispatch

DESCRIPTION = (
    "Review a manuscript PDF end to end, with every finding anchored to a "
    "quotation in it. `peerreview manuscript.pdf` is the whole review."
)


def add_serve_arguments(sub: argparse._SubParsersAction) -> None:
    """Attach the ``serve`` command: the same reviews, started from a browser."""
    p = sub.add_parser(
        "serve",
        help="Serve the web UI: upload a manuscript PDF, read its REVIEW.md",
    )
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--output-dir", dest="output_dir", default=None,
                   help="Where reviews are written (default: ./reviews)")
    p.add_argument("--upload-dir", dest="upload_dir", default=None,
                   help="Where uploaded manuscripts are stored "
                        "(default: ./.peerreview-uploads)")
    p.add_argument("--mode", dest="mode", default=None,
                   choices=("ensemble", "independent", "panel"),
                   help="Default review mode for the form (default: ensemble)")
    p.add_argument("--reviewers", dest="reviewers", type=int, default=None,
                   help="Default reviewer count for ensemble mode (default: 2)")
    p.add_argument("--provider", dest="provider", choices=("claude-code", "codex"),
                   default=None, help="Default transport (default: claude-code)")
    p.add_argument("--model", dest="model", default=None,
                   help="Default model for the chosen transport")
    p.add_argument("--policy", dest="policy", default=None,
                   help="Default review policy, such as standard-v1")
    p.add_argument("--literature-cutoff", dest="literature_cutoff", default=None,
                   metavar="YYYY-MM-DD",
                   help="Default literature cutoff date for the form")


def build_parser() -> argparse.ArgumentParser:
    """The engine's own commands, plus ``serve``, under the ``peerreview`` name."""
    parser = argparse.ArgumentParser(prog="peerreview", description=DESCRIPTION)
    sub = parser.add_subparsers(dest="command", required=True)
    add_subcommands(sub)
    add_serve_arguments(sub)
    return parser


def run_server(args) -> None:
    """Launch the FastAPI web UI over the review engine."""
    try:
        import uvicorn

        from peerreviewagents.web import create_app
    except ImportError as exc:
        print(f"Web dependencies missing: {exc}", file=sys.stderr)
        print("Run: uv pip install -e . to pick them up.", file=sys.stderr)
        raise SystemExit(1) from exc

    defaults = {
        key: getattr(args, key)
        for key in ("mode", "reviewers", "provider", "model", "policy", "literature_cutoff")
        if getattr(args, key, None) is not None
    }
    app = create_app(
        output_dir=args.output_dir,
        upload_dir=args.upload_dir,
        host=args.host,
        defaults=defaults,
    )
    print(f"PeerReviewAgents web UI on http://{args.host}:{args.port}")
    print(f"Reviews are written to {app.state.output_root}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


def first_word(argv: list[str]) -> str | None:
    """The first argument that is a command name's place, or ``None``.

    An option and the value that follows it are skipped, so the command is
    looked for where a command can be: ``--mode panel paper.pdf`` names no
    command, and ``panel`` is the value of ``--mode`` rather than a bad one.
    A ``--flag=value`` carries its own value, so the token after it is not
    skipped.
    """
    skip = False
    for argument in argv:
        if argument.startswith("-"):
            skip = "=" not in argument
            continue
        if skip:
            skip = False
            continue
        return argument
    return None


def review_argv(argv: list[str]) -> list[str]:
    """Read a bare manuscript path as the ``review`` command it stands for.

    ``peerreview manuscript.pdf`` is the one-command review, so an invocation
    that names no command is a review of the manuscript it names, whether the
    review's own options come before the path or after it. ``--help`` is the
    top-level parser's, so it is never rewritten into a review.
    """
    if not argv or any(flag in ("-h", "--help") for flag in argv):
        return argv
    if first_word(argv) in (*COMMANDS, "serve"):
        return argv
    return ["review", *argv]


def run() -> None:
    parser = build_parser()
    args = parser.parse_args(review_argv(sys.argv[1:]))
    if args.command == "serve":
        run_server(args)
        return
    engine_dispatch(parser, args)


if __name__ == "__main__":
    run()
