"""The supported library surface of PeerReviewAgents.

Everything an application needs is importable from here, and nothing it needs
is private:

    from peerreviewagents import review, read_run, write_reports

Running a review
    :func:`review` is the whole review: it starts one headless Claude Code
    session on the ``pra`` plugin, enforces the token ceilings from the
    session's own stream, and writes the run folder. It is exactly what
    ``peerreview review`` runs, and it returns the run summary as a dict.
    :func:`review_main` and :func:`report_main` are the two commands as
    functions — argument list in, JSON on stdout, exit code returned — so a
    wrapper does not have to reach into the starter or the CLI module.

Reading a run
    :func:`read_run` reads any run folder: complete, complete without
    findings, blocked, killed by the starter on a token ceiling, a wall-clock
    limit, a rate limit or a session error, or stopped by a start-up probe that
    never launched a session. It returns the status, the cause, the findings
    de-duplicated to the latest row per ``finding_uid``, body against appendix,
    the escalations and their rulings, the manifest identity, the report paths
    and the runtime of each agent. :func:`write_reports` re-renders
    ``REVIEW.md`` and ``REVIEW.txt`` from a run folder, with no model call and
    no workspace path needed.

Checking an installation
    :func:`plugin_directory` is the ``pra`` plugin that ships inside this
    package — package data, not a checkout — which is what a wrapper passes to
    ``claude --plugin-dir`` or asserts the existence of before offering a
    review. :func:`claude_code_version` is what ``claude --version`` prints,
    or ``None`` when Claude Code is not installed on this PATH, which is the
    one precondition a review cannot do without.

Checking a wrapper's own commands
    :data:`CLI_COMMANDS` is every command ``peerreview`` has, and
    :func:`review_parser` and :func:`report_parser` are the real parsers behind
    the two of them. A wrapper that documents its own options can compare them
    against these instead of against a copy, so an option added here shows up
    as a difference rather than as a wrapper that silently drops it. Use
    :func:`review_main` and :func:`report_main` to *run* a command; these three
    are for checking one.

Manuscripts and workspaces
    :func:`import_paper` imports a PDF into a manuscript workspace without any
    model call. :func:`verify_workspace` re-checks an imported workspace
    against its own manifest and returns the paper. :func:`export_draft`
    applies the accepted anchored edits of a review to a copy of the blocks.

Names
    :data:`RUN_DIRECTORY` and :data:`MANUSCRIPT_DIRECTORY` are the two folders
    inside a run, :data:`RUN_STATUSES` and :data:`COMPLETE_STATUSES` are the
    statuses a run folder can record, :data:`CLI_COMMANDS` is the two commands,
    and :data:`FINDING_SCHEMA_VERSION` is the schema every stored finding is
    stamped with.

The callables resolve lazily (PEP 562) so that ``import peerreviewagents``
stays as cheap as it is on its own, and so a caller that only wants to read a
run never imports the launcher. The constants are plain values: they come from
:mod:`peerreviewagents.names`, which imports nothing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .names import (
    CLI_COMMANDS,
    COMPLETE_STATUSES,
    FINDING_SCHEMA_VERSION,
    MANUSCRIPT_DIRECTORY,
    RUN_DIRECTORY,
    RUN_STATUSES,
)

if TYPE_CHECKING:  # Static tooling sees the real objects, not the fallback.
    from .cli.main import report_parser
    from .engine.annotations import export_draft, verify_workspace
    from .engine.ingest import import_paper
    from .engine.run_report import write_reports
    from .plugin.starter import build_parser as review_parser
    from .plugin.starter import claude_version as claude_code_version
    from .plugin.starter import plugin_dir as plugin_directory
    from .plugin.starter import review, review_main
    from .runs import read_run

__all__ = [
    "review",
    "review_main",
    "report_main",
    "read_run",
    "import_paper",
    "write_reports",
    "verify_workspace",
    "export_draft",
    "plugin_directory",
    "claude_code_version",
    "review_parser",
    "report_parser",
    "RUN_DIRECTORY",
    "MANUSCRIPT_DIRECTORY",
    "RUN_STATUSES",
    "COMPLETE_STATUSES",
    "CLI_COMMANDS",
    "FINDING_SCHEMA_VERSION",
]

_LAZY = {
    "review": (".plugin.starter", "review"),
    "review_main": (".plugin.starter", "review_main"),
    "report_main": (".cli.main", "report_main"),
    "read_run": (".runs", "read_run"),
    # The installation checks and the two parsers. Each is the object the
    # command itself uses, under the name this surface calls it by, so a
    # consumer never needs `plugin.starter` or `cli.main`.
    "plugin_directory": (".plugin.starter", "plugin_dir"),
    "claude_code_version": (".plugin.starter", "claude_version"),
    "review_parser": (".plugin.starter", "build_parser"),
    "report_parser": (".cli.main", "report_parser"),
    "import_paper": (".engine.ingest", "import_paper"),
    "write_reports": (".engine.run_report", "write_reports"),
    "verify_workspace": (".engine.annotations", "verify_workspace"),
    "export_draft": (".engine.annotations", "export_draft"),
}


def __getattr__(name: str):
    try:
        module, attribute = _LAZY[name]
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None
    from importlib import import_module

    value = getattr(import_module(module, __name__), attribute)
    # Cache on the module so later lookups skip this function entirely.
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_LAZY))
