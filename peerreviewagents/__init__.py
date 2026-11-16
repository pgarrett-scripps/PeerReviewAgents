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
    never launched a session. It returns the status, the cause, the issues the
    editor raised with their member findings, the findings themselves
    de-duplicated to the latest row per ``finding_uid``, the escalations and
    their rulings, the manifest identity, the report paths
    and the runtime of each agent. :func:`write_reports` re-renders
    ``REVIEW.md`` and ``REVIEW.txt`` from a run folder, with no model call and
    no workspace path needed. :func:`locate_run` is the resolution those two
    do first — ``(run folder, review folder)`` from either spelling — for an
    app that lists folders before reading them, and
    :class:`NotARunFolder` is what both raise for a folder that is not a run.
    :func:`read_citation_ledger` is the citation table itself rather than its
    counts — one row per reference and per in-text use, the last row written
    for each, with the fetched abstract and full text left out unless asked
    for — for a consumer that shows which reference resolved to which record.

Checking a file before publishing it
    :func:`validate_prior_issues` is every problem that would stop an issues
    document being read as the prior round of a later review, in words; an
    empty list means ``peerreview review --prior-issues`` will load it. It is
    the check that loader runs rather than a copy of it, so a consumer holds a
    file it is about to publish against the code that will read it next round.
    :mod:`peerreviewagents.issues` also holds the tolerant readers a consumer
    of a document it did not write needs — ``rows_of``, ``members_of``,
    ``quotes_of`` — and ``field_problems``, which is the list of malformed
    fields :func:`read_run` reports a damaged document through.

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
    model call. :func:`write_manuscript` writes the same workspace from pieces
    a caller composed itself — a Typst or LaTeX source it parsed, an editor's
    own document — and returns the manifest, as the import does; a piece's
    ``page`` and ``bbox`` are optional there, because a manuscript built from a
    source file has no pages. :func:`read_manuscript` reads a workspace back as
    a :class:`PaperBase`: the documents' text, their lines, the map rows and
    the manifest. :func:`verify_workspace` is the same read with the manifest's
    digests re-checked first, and returns that same :class:`PaperBase`, so a
    folder whose Markdown was edited after it was written is refused by name
    instead of producing line numbers that no longer mean anything.
    :func:`export_draft`
    applies the accepted anchored edits of a review to a copy of ``paper.md``.
    :func:`cited_text` is the one way to turn a citation — a finding from
    :func:`read_run`, an issue member, a citation ledger row — back into the
    manuscript's own characters: each carries ``document``, ``line``,
    ``end_line``, ``start`` and ``end``, and this reads that span off a
    :class:`PaperBase`, including the case of a quote that crosses a line
    break, so an app highlights the words without searching the line for them
    again. :func:`citation_uses` is the other half of that: it reads a
    :class:`PaperBase` and a reference list and answers one row per in-text use
    of a reference, anchored the same way, for an app building a citation
    ledger of its own. It is the engine's own anchoring rule — where a sentence
    ends, how one that crosses a line break is spanned, how a ``row_id`` is
    built — rather than a second implementation of it that would disagree with
    the engine by a line somewhere.
    :mod:`peerreviewagents.workspace` holds the smaller tools those are built
    from, for an app that writes files the engine reads back: the manifest
    digest, the atomic JSON read and write, the review normalisation and save,
    and the two text helpers the import uses.
    ``docs/INTEGRATIONS.md`` documents what a piece is on the way in and what a
    map row says on the way out.

Names
    :data:`RUN_DIRECTORY` and :data:`MANUSCRIPT_DIRECTORY` are the two folders
    inside a run, :data:`RUN_STATUSES` and :data:`COMPLETE_STATUSES` are the
    statuses a run folder can record, :data:`CLI_COMMANDS` is the two commands,
    and :data:`FINDING_SCHEMA_VERSION` is the schema every stored finding is
    stamped with. :data:`ISSUES_SCHEMA_VERSION` is the schema of ``issues.json``,
    the one versioned source of truth a run's review is rendered from, and
    :data:`ISSUE_SEVERITIES`, :data:`ISSUE_CATEGORIES` and
    :data:`ISSUE_STATUSES` are the vocabularies an issue is written in.
    :data:`BASE_SCHEMA_VERSION` is the schema of a manuscript workspace's
    ``map.json`` and ``manifest.json``, and :data:`MAIN_DOCUMENT` is the name of
    its main document, the one every workspace holds.
    :mod:`peerreviewagents.issues` holds the helpers that order and group them —
    required changes by rank, suggestions by category — so a consumer renders
    an issues document the same way ``REVIEW.md`` does.

The callables resolve lazily (PEP 562) so that ``import peerreviewagents``
stays as cheap as it is on its own, and so a caller that only wants to read a
run never imports the launcher. The constants are plain values: they come from
:mod:`peerreviewagents.names`, which imports nothing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .issues import (
    ISSUE_CATEGORIES,
    ISSUE_SEVERITIES,
    ISSUE_STATUSES,
    ISSUES_SCHEMA_VERSION,
)
from .names import (
    BASE_SCHEMA_VERSION,
    CLI_COMMANDS,
    COMPLETE_STATUSES,
    FINDING_SCHEMA_VERSION,
    MAIN_DOCUMENT,
    MANUSCRIPT_DIRECTORY,
    RUN_DIRECTORY,
    RUN_STATUSES,
)

if TYPE_CHECKING:  # Static tooling sees the real objects, not the fallback.
    from .cli.main import report_parser
    from .engine.annotations import export_draft, verify_workspace
    from .engine.citation_ledger import citation_uses
    from .engine.ingest import import_paper, write_manuscript
    from .engine.paper_base import PaperBase
    from .engine.paper_base import load as read_manuscript
    from .engine.quotes import cited_text
    from .engine.run_report import write_reports
    from .plugin.starter import build_parser as review_parser
    from .plugin.starter import claude_version as claude_code_version
    from .plugin.starter import plugin_dir as plugin_directory
    from .plugin.starter import review, review_main
    from .runs import NotARunFolder, read_citation_ledger, read_run
    from .runs import locate as locate_run

__all__ = [
    "review",
    "review_main",
    "report_main",
    "read_run",
    "read_citation_ledger",
    "validate_prior_issues",
    "locate_run",
    "NotARunFolder",
    "import_paper",
    "write_manuscript",
    "read_manuscript",
    "PaperBase",
    "cited_text",
    "citation_uses",
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
    "BASE_SCHEMA_VERSION",
    "MAIN_DOCUMENT",
    "ISSUES_SCHEMA_VERSION",
    "ISSUE_SEVERITIES",
    "ISSUE_CATEGORIES",
    "ISSUE_STATUSES",
]

_LAZY = {
    "review": (".plugin.starter", "review"),
    "review_main": (".plugin.starter", "review_main"),
    "report_main": (".cli.main", "report_main"),
    "read_run": (".runs", "read_run"),
    # The ledger rows themselves, for the consumers that were re-implementing
    # "the last row for a row_id is the row" against the engine module.
    "read_citation_ledger": (".runs", "read_citation_ledger"),
    # The check `--prior-issues` runs, for a consumer holding a file it is about
    # to publish against the code that will read it next round.
    "validate_prior_issues": (".issues", "validate_prior_issues"),
    # The run-folder resolution and the error both of those raise, so an app
    # that lists folders never imports `runs` for them.
    "locate_run": (".runs", "locate"),
    "NotARunFolder": (".runs", "NotARunFolder"),
    # The installation checks and the two parsers. Each is the object the
    # command itself uses, under the name this surface calls it by, so a
    # consumer never needs `plugin.starter` or `cli.main`.
    "plugin_directory": (".plugin.starter", "plugin_dir"),
    "claude_code_version": (".plugin.starter", "claude_version"),
    "review_parser": (".plugin.starter", "build_parser"),
    "report_parser": (".cli.main", "report_parser"),
    "import_paper": (".engine.ingest", "import_paper"),
    # The other end of the import, and the read it leaves behind: an app that
    # composes its own pieces writes them with `write_manuscript` and reads the
    # workspace back with `read_manuscript`, which is `verify_workspace`
    # without the digest check — what a viewer wants, since it is showing the
    # folder rather than reviewing it. `PaperBase` is the type both return.
    "write_manuscript": (".engine.ingest", "write_manuscript"),
    "read_manuscript": (".engine.paper_base", "load"),
    "PaperBase": (".engine.paper_base", "PaperBase"),
    # The citation rule, executable: every consumer that highlights a quote was
    # about to write this loop itself, and the line-crossing case is the one it
    # would get wrong.
    "cited_text": (".engine.quotes", "cited_text"),
    # And the rule that produces what `cited_text` reads back. An app writing
    # its own ledger had every part but this one: the sentence spanning and the
    # row_id are the half a second implementation gets wrong by a line.
    "citation_uses": (".engine.citation_ledger", "citation_uses"),
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
