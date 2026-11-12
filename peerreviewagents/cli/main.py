"""The ``peerreview`` command: one plugin review of a manuscript PDF.

``peerreview review manuscript.pdf <run folder>`` is the whole review. The PDF
is imported into ``<run folder>/manuscript``, a headless Claude Code session
runs the ``pra`` plugin over it — N core reviewers and six specialist tracks in
parallel, then an editor — and ``<run folder>/review/REVIEW.md`` is written with
every finding anchored to an exact quotation. ``peerreview manuscript.pdf
<run folder>`` means the same thing.

There are two commands and no third. ``review`` runs the review and is the only
way to run one. ``report`` re-renders the reports of a run that already
finished, which makes no model call at all. Everything the review itself owns —
the manuscript, the agent identities, the ledger and the budgets — belongs to
the plugin's MCP server, not to this command.

The review runs on a Claude Code subscription login installed on this machine,
so there is no API key and no PeerReviewAgents service.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

COMMANDS = ("review", "report")

DESCRIPTION = (
    "Review a manuscript PDF end to end with the pra plugin, with every finding "
    "anchored to a quotation in it. `peerreview review paper.pdf run/` is the whole review."
)


def build_parser() -> argparse.ArgumentParser:
    """The two commands, for ``peerreview --help`` and for an unusable invocation.

    Each command parses its own arguments, so the subparsers here carry no
    options: ``peerreview review -h`` and ``peerreview report -h`` are answered
    by the command itself.
    """
    parser = argparse.ArgumentParser(prog="peerreview", description=DESCRIPTION)
    sub = parser.add_subparsers(dest="command", required=True, metavar="{review,report}")
    sub.add_parser("review", add_help=False,
                   help="review one manuscript PDF (peerreview review -h for its options)")
    sub.add_parser("report", add_help=False,
                   help="re-render the reports of a finished run, offline")
    return parser


def report_parser(prog: str = "peerreview report") -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog,
        description="Re-render REVIEW.md and REVIEW.txt from a finished run folder. "
                    "No model call is made and nothing but the reports is written.")
    parser.add_argument("run", help="the run folder, or the review folder inside it")
    parser.add_argument("--workspace", default=None,
                        help="the manuscript workspace, if it is not beside the run")
    parser.add_argument("--output-dir", dest="output_dir", default=None,
                        help="where to write the reports (default: the run folder itself)")
    return parser


def report_main(argv: list[str] | None = None, prog: str = "peerreview report") -> int:
    """``peerreview report`` as a function: render a run folder's reports offline.

    This is the supported entry point for an application that wraps the
    command. It takes the arguments after ``report`` (a leading ``report`` is
    accepted too), prints JSON on stdout and returns the exit code rather than
    raising it. ``prog`` is the program name its ``--help`` and its errors use,
    so a wrapper named something else says its own name, exactly as
    :func:`peerreviewagents.review_main` does.

    A run that stopped is rendered like any other. The status and the cause are
    printed with the report paths, because a folder that was killed on a token
    ceiling or whose probe never launched a session is a result to read, not a
    folder to be told is "not a finished run".

    A folder that is not a run, and a run file that is a symlink, are a message
    on stderr and the code 1. Nothing is raised: the code is returned, which is
    what a wrapper around this function has to be able to rely on.
    """
    from peerreviewagents.engine.run_report import write_reports
    from peerreviewagents.runs import read_run

    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "report":
        argv = argv[1:]
    args = report_parser(prog=prog).parse_args(argv)
    try:
        # The run folder holds `manuscript/` and `review/`; the reports belong
        # to the review folder, so naming the run folder means that one.
        # NotARunFolder is a ValueError, and so is the symlink refusal.
        summary = read_run(args.run)
        paths = write_reports(Path(summary["review"]), args.workspace, args.output_dir)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps({'status': summary['status'], 'cause': summary['cause'],
                      'findings': summary['counts']['findings'],
                      'publishable': summary['publishable'],
                      'not_publishable_reasons': summary['not_publishable_reasons'],
                      'problems': summary['problems'],
                      **{key: str(value) for key, value in paths.items()}}, indent=2))
    return 0




def first_word(argv: list[str]) -> str | None:
    """The first argument that is a command name's place, or ``None``.

    An option and the value that follows it are skipped, so the command is
    looked for where a command can be: ``--reviewers 3 paper.pdf`` names no
    command, and ``3`` is the value of ``--reviewers`` rather than a bad one.
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

    ``peerreview paper.pdf run/`` is the one-command review, so an invocation
    that names no command is a review of the manuscript it names, whether the
    review's own options come before the path or after it. ``--help`` is the
    top-level parser's, so it is never rewritten into a review.
    """
    if not argv or any(flag in ("-h", "--help") for flag in argv):
        return argv
    if first_word(argv) in COMMANDS:
        return argv
    return ["review", *argv]


def run() -> None:
    argv = review_argv(sys.argv[1:])
    command = argv[0] if argv else None
    if command == "review":
        from peerreviewagents.plugin.starter import review_main

        raise SystemExit(review_main(argv))
    if command == "report":
        raise SystemExit(report_main(argv[1:]))
    build_parser().parse_args(argv)  # Prints the help, or names the bad command.


if __name__ == "__main__":
    run()
