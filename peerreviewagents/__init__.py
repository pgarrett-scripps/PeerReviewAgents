"""The supported library surface of PeerReviewAgents.

Everything an application needs is importable from here, and nothing it needs
is private:

    from peerreviewagents import review, read_run, write_reports

Running a review
    :func:`review` is the whole review: it starts one headless Claude Code
    session on the ``pra`` plugin, enforces the token ceilings from the
    session's own stream, and writes the run folder. It is exactly what
    ``peerreview review`` runs, and it returns the run summary as a dict.
    Pass ``code=`` — a local folder or a repository URL — to add the lane that
    reads the paper against the code behind it: the text of that code is
    snapshotted into the run folder, read-only, and every finding the lane
    files carries a passage of it the server checked against the snapshot.
    :func:`review_main` and :func:`report_main` are the two commands as
    functions — argument list in, JSON on stdout, exit code returned — so a
    wrapper does not have to reach into the starter or the CLI module.
    :func:`review_arms` is what each arm runs, one row per arm a review runs
    — nothing it would refuse — and each row says: how many core
    reviewers it fixes, whether it runs the specialist tracks, an editor, a
    revision pass, whether it can keep a citation ledger, which arm is the
    default, and whether it accepts ``code=`` at all. The code review is a
    specialist lane, so an arm that runs none refuses code rather than
    reviewing it silently — ask this instead of holding a list of arm names of
    your own.

Checking a new draft against a review
    :func:`check_fixes` is the second run this package knows how to start, and
    the only one that raises nothing: it reads a *new* draft against problems a
    review already raised and answers each one ``fixed``, ``not-fixed`` or
    ``cannot-tell``, with a quotation of the new draft behind every verdict
    that has one. :func:`problems_from_run` writes the input it takes from a
    finished run's own issues, so a revision is checked against the review it
    came from without a consumer composing that file by hand;
    :mod:`peerreviewagents.problems` is the same schema for a caller composing
    one from somewhere else — an editor's decision letter, a reviewer's list —
    and :data:`FIX_VERDICTS` is the vocabulary a verdict is written in.
    :func:`read_fix_check` is the reading of what it leaves behind, the twin of
    :func:`read_run`: the status and the cause, one row per problem with its
    verdict and its anchored quotation, the counts per verdict, the problems
    nobody answered, and the tokens. It takes no ``code``: every verdict rests
    on the new draft, so a code snapshot would support none of them, and a
    problem whose fix is in the code is what ``cannot-tell`` is for.
    :func:`check_fixes_main` is the command as a function and
    :func:`check_fixes_parser` its real parser.

Reading a run
    :func:`read_run` reads any run folder: complete, complete without
    findings, blocked, killed by the starter on a token ceiling, a wall-clock
    limit, a rate limit or a session error, or stopped by a start-up probe that
    never launched a session. It returns the status, the cause, the issues the
    editor raised with their member findings, the findings themselves
    de-duplicated to the latest row per ``finding_uid``, the escalations and
    their rulings, the manifest identity, whether code was reviewed beside the
    paper and which files of it were opened, the report paths
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

Reading a document this tool did not write
    :mod:`peerreviewagents.issues` holds the tolerant readers a consumer of an
    issues document needs — ``rows_of``, ``members_of`` — and
    ``field_problems``, which is the list of malformed fields
    :func:`read_run` reports a damaged document through.

Checking an installation
    :func:`plugin_directory` is the ``pra`` plugin that ships inside this
    package — package data, not a checkout — which is what a wrapper passes to
    ``claude --plugin-dir`` or asserts the existence of before offering a
    review. :func:`claude_code_version` is what ``claude --version`` prints,
    or ``None`` when Claude Code is not installed on this PATH, which is the
    one precondition a review cannot do without.

Checking a wrapper's own commands
    :data:`CLI_COMMANDS` is every command ``peerreview`` has, and
    :func:`review_parser`, :func:`check_fixes_parser` and
    :func:`report_parser` are the real parsers behind the three of them. A
    wrapper that documents its own options can compare them against these
    instead of against a copy, so an option added here shows up as a
    difference rather than as a wrapper that silently drops it. Use
    :func:`review_main`, :func:`check_fixes_main` and :func:`report_main` to
    *run* a command; these four are for checking one.

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

Building a fixture to test against
    :func:`scripted_read` is the Read stage driven in code, with no model and
    nothing launched: it opens a run on a PDF, claims the reader's slot and
    hands back a :class:`ScriptedRead` whose methods are the reader's own
    tools, so a consumer's test fixture for what that stage writes — a re-read
    part's ``part`` and ``of``, an absorbed piece's reading, the reader's note
    in the manifest — is written by the engine instead of by hand.
    :class:`ReaderRefused` is what a refused call raises. It is for tests; a
    review's Read stage is a model stage and :func:`review` is what runs one.
    :func:`scripted_fix_check` is the same for the other stage an application
    renders: it opens a fix check on a PDF with a problems file, claims every
    checker slot and hands back a :class:`ScriptedFixCheck` whose methods are
    ``fix_queue``, ``submit_fix_check`` and the tools that read the paper, so a
    fixture holding ``fix-check.json`` and ``FIXES.md`` is written by the lane.
    :func:`scripted_review` is the third and the widest: it opens a review on a
    PDF, claims every slot the arm plans and the recorder, and hands back a
    :class:`ScriptedReview` whose methods are each slot's own tools —
    ``submit_finding``, ``submit_report``, ``submit_issues``, ``submit_editor``
    — so a fixture holding ``issues.json``, ``REVIEW.md`` and the findings
    :func:`read_run` reads is written by the engine with no model in it.
    :class:`CheckRefused` and :class:`ReviewRefused` are their refusals and
    :class:`ToolRefused` is the base of all three.

Names
    :data:`RUN_DIRECTORY` and :data:`MANUSCRIPT_DIRECTORY` are the two folders
    every run holds and :data:`CODE_DIRECTORY` the third one a run given code
    holds, :data:`RUN_STATUSES` and :data:`COMPLETE_STATUSES` are the
    statuses a run folder can record, :data:`CLI_COMMANDS` is the three commands,
    and :data:`FINDING_SCHEMA_VERSION` is the schema every stored finding is
    stamped with. :data:`ISSUES_SCHEMA_VERSION` is the schema of ``issues.json``,
    the one versioned source of truth a run's review is rendered from, and
    :data:`ISSUE_SEVERITIES` and :data:`ISSUE_CATEGORIES` are the vocabularies
    an issue is written in.
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
    ISSUES_SCHEMA_VERSION,
)
from .names import (
    BASE_SCHEMA_VERSION,
    CLI_COMMANDS,
    CODE_DIRECTORY,
    COMPLETE_STATUSES,
    FINDING_SCHEMA_VERSION,
    FIX_VERDICTS,
    MAIN_DOCUMENT,
    MANUSCRIPT_DIRECTORY,
    MANUSCRIPT_STATES,
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
    from .plugin.server import review_arms
    from .plugin.starter import build_parser as review_parser
    from .plugin.starter import (
        check_fixes,
        check_fixes_main,
        check_fixes_parser,
        review,
        review_main,
    )
    from .plugin.starter import claude_version as claude_code_version
    from .plugin.starter import plugin_dir as plugin_directory
    from .problems import problems_from_run
    from .runs import NotARunFolder, read_citation_ledger, read_fix_check, read_run
    from .runs import locate as locate_run
    from .scripted import (
        CheckRefused,
        ReaderRefused,
        ReviewRefused,
        ScriptedFixCheck,
        ScriptedRead,
        ScriptedReview,
        ToolRefused,
        scripted_fix_check,
        scripted_read,
        scripted_review,
    )

__all__ = [
    "review",
    "review_main",
    "report_main",
    "review_arms",
    "check_fixes",
    "check_fixes_main",
    "problems_from_run",
    "read_fix_check",
    "read_run",
    "read_citation_ledger",
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
    "scripted_read",
    "ScriptedRead",
    "ReaderRefused",
    "scripted_fix_check",
    "ScriptedFixCheck",
    "CheckRefused",
    "scripted_review",
    "ScriptedReview",
    "ReviewRefused",
    "ToolRefused",
    "plugin_directory",
    "claude_code_version",
    "review_parser",
    "check_fixes_parser",
    "report_parser",
    "RUN_DIRECTORY",
    "MANUSCRIPT_DIRECTORY",
    "CODE_DIRECTORY",
    "RUN_STATUSES",
    "COMPLETE_STATUSES",
    "MANUSCRIPT_STATES",
    "CLI_COMMANDS",
    "FINDING_SCHEMA_VERSION",
    "BASE_SCHEMA_VERSION",
    "MAIN_DOCUMENT",
    "ISSUES_SCHEMA_VERSION",
    "ISSUE_SEVERITIES",
    "ISSUE_CATEGORIES",
    "FIX_VERDICTS",
]

_LAZY = {
    "review": (".plugin.starter", "review"),
    "review_main": (".plugin.starter", "review_main"),
    "report_main": (".cli.main", "report_main"),
    # What each arm runs, for a caller deciding before a run exists: which
    # arms accept `code=`, which keep a citation ledger, which is the default.
    # Lazy like the rest, so an app that only reads runs never imports it.
    "review_arms": (".plugin.server", "review_arms"),
    # The fix check: the run that answers problems a review raised against a
    # new draft, the file of problems it takes written from a finished run, and
    # the reading of the folder it leaves behind.
    "check_fixes": (".plugin.starter", "check_fixes"),
    "check_fixes_main": (".plugin.starter", "check_fixes_main"),
    "check_fixes_parser": (".plugin.starter", "check_fixes_parser"),
    "problems_from_run": (".problems", "problems_from_run"),
    "read_fix_check": (".runs", "read_fix_check"),
    "read_run": (".runs", "read_run"),
    # The ledger rows themselves, for the consumers that were re-implementing
    # "the last row for a row_id is the row" against the engine module.
    "read_citation_ledger": (".runs", "read_citation_ledger"),
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
    # The Read stage in code, for a consumer's tests: a fixture holding what
    # that stage writes, written by the stage. Lazy like the rest, so a
    # consumer that only reads runs never imports the server for it.
    "scripted_read": (".scripted", "scripted_read"),
    "ScriptedRead": (".scripted", "ScriptedRead"),
    "ReaderRefused": (".scripted", "ReaderRefused"),
    # And the fix check in code, for the same reason: a folder holding
    # `fix-check.json` and `FIXES.md`, written by the lane that writes them.
    "scripted_fix_check": (".scripted", "scripted_fix_check"),
    "ScriptedFixCheck": (".scripted", "ScriptedFixCheck"),
    "CheckRefused": (".scripted", "CheckRefused"),
    # And a whole review in code, which is the folder an application renders
    # most of: `issues.json`, `REVIEW.md` and the findings behind them.
    "scripted_review": (".scripted", "scripted_review"),
    "ScriptedReview": (".scripted", "ScriptedReview"),
    "ReviewRefused": (".scripted", "ReviewRefused"),
    "ToolRefused": (".scripted", "ToolRefused"),
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
