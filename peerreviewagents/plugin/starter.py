"""Run one review in this process, agent by agent, and account for it.

The server owns the review; this module owns the run. It validates what it was
asked for, opens the run's one tool server, hands the panel to an orchestrator,
and writes down what happened:

* **Nothing is relayed.** The stage order, the fan-out, the retries and every
  recorder tool are :mod:`peerreviewagents.plugin.orchestrator`'s, called here
  in this process. There is no driving session, no workflow script and no agent
  reading a dict back to us — so there is also no race between a session's
  turn ending and the review still running, which is what the headless driver
  this replaces was mostly written against.
* **One agent is one task.** Each slot is run through a
  :class:`peerreviewagents.plugin.runner.Runner`: Claude Code by default, a
  scripted runner in the tests. Nothing here knows how an agent is run, which
  is why the whole panel can be exercised without a model.
* **The budget is real.** :class:`orchestrator.Budget` holds the per-agent and
  per-run ceilings and the wall clock, hands each agent what the run has left,
  and is the one record the usage files and ``runtime.json`` are written from.
* **A model this login cannot run is refused first.** The runner is asked
  before anything is written, because Claude Code's own answer to a model it
  cannot serve is to quietly run a different one, which would leave the run
  recording an arm it did not run.
* **The reports are rendered last.** ``write_review`` writes ``REVIEW.md``
  before any of this accounting exists on disk, so the reports are written once
  more over the top, with the real totals.

A rate limit, a breached budget or a missing stage leaves the run ``blocked``,
which is an outcome and not an error to paper over.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
import time
from pathlib import Path

from .. import problems as problem_list
from ..engine import code_source
from ..engine.storage import digest, read, write
from ..names import COMPLETE_STATUSES, FINDING_SCHEMA_VERSION, TOKEN_FIELDS
from . import claude_code, orchestrator
from . import models as model_plan
from . import runner as agent_runner
from .claude_code import ClaudeCodeRunner
from .server import (
    ARMS,
    CITATION_WORKERS,
    DEFAULT_ARM,
    FIX_ARM,
    MANUSCRIPT_DIRECTORY,
    MAX_CITATION_WORKERS,
    MAX_FIX_CHECKS,
    REVIEWABLE_ARMS,
    RUN_DIRECTORY,
    SPECIALIST_TRACKS,
    WORKFLOW_VERSION,
    checker_slots,
    citation_slots,
    plan_slots,
)
from .service import TOOL_PREFIX, TOOLS, Service

SERVER = 'peerreview'
PLUGIN = 'pra'
# The token ceilings are a guard against an agent that has run away, not a
# budget this package manages on anyone's behalf. The turn cap is the first
# line of defence and these are the second, so they are set well above what a
# working agent spends and are measured against the *cumulative* spend of a
# session — every call's usage added up, which is what the service bills.
#
# That unit is why these numbers are what they are. They were calibrated
# against a per-agent figure the workflow harness reported, which was the
# largest single call rather than the session total
# (:data:`~peerreviewagents.names.TOKEN_FIELDS` sets the two side by side), and
# 500,000 in the old unit stopped a reader that had viewed all 29 pages of a
# real manuscript, repaired every piece and sealed the manuscript: it spent
# 592,106 cumulative tokens to hold 144,072 at its widest. Provisional until
# one full real review has been measured end to end, which is the measurement
# that will replace the guesswork in all six of these numbers.
PER_AGENT_TOKENS = 3_000_000
PER_RUN_TOKENS = 30_000_000
WALL_TIMEOUT = 5400
# A fix check is a much smaller run than a review and its ceilings say so. One
# checker reads the new draft once and answers up to ten questions about it
# with no literature, no code and no synthesis; there is no editor, no
# specialist and no citation ledger. A review's ceiling would never fire here,
# which makes it no ceiling at all: a checker stuck in a search loop would burn
# a review's budget on ten problems before the wall clock noticed. Raised by
# the same factor as the review's, for the same reason and with the same
# caveat: they were set in the old unit and no fix check has been measured in
# this one.
FIX_PER_AGENT_TOKENS = 900_000
FIX_PER_RUN_TOKENS = 4_500_000
FIX_WALL_TIMEOUT = 2700
#: What `check-draft` runs when no track is named. The three a draft of your
#: own is usually wrong about, and the default the prompt and the recorded plan
#: must agree on.
DRAFT_CHECK_TRACKS = ('stats', 'numbers', 'methods-reporting')
#: How much of an agent's own last word a blocked reason carries. It is a
#: model's prose, not a status line, so it is quoted and cut rather than kept.
LAST_WORD_CHARACTERS = 600
#: What the recorder slot is now: this process. The slot still exists — the
#: server mints a claim code for it and the run key reissues the others — but
#: nothing runs a model for it, and its row in the record says so rather than
#: reporting a model that never ran.
IN_PROCESS = 'in process'
#: Shared with :mod:`peerreviewagents.plugin.claude_code`, so clearing it here
#: clears the answer the runner caches too.
VERSIONS = claude_code.VERSIONS


def tool_names():
    """Every server tool under the name an agent calls it by."""
    return tuple(TOOL_PREFIX + name for name in sorted(TOOLS))


def plugin_dir():
    """The plugin that ships inside this package.

    It is package data, not a checkout: an installed wheel has no ``plugins/``
    folder beside it. Nothing is loaded as a plugin any more — the skills in it
    shell out to this command — but it is still what the run's provenance
    hashes, so a review can say which copy of the package produced it.
    """
    return Path(__file__).resolve().parent / PLUGIN


def claude_binary():
    """Claude Code, resolved the way a shell resolves it, or ``None``.

    One resolution for the whole package: the runner starts the path this
    returns, so a provenance line and a session can never disagree about which
    binary was meant.
    """
    return claude_code.executable()


def claude_version(binary=None):
    """What ``claude --version`` prints, for provenance. ``None`` if it will not say."""
    return claude_code.version(binary or claude_code.BINARY)


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def recorded_progress(review):
    """What the server last wrote in ``progress.json``, or ``{}``.

    Read after the panel has stopped, and read defensively: a file caught
    half-rewritten is not an answer about the run.
    """
    path = Path(review) / 'progress.json'
    if not path.is_file():
        return {}
    try:
        current = read(path)
    except (OSError, ValueError, UnicodeDecodeError):
        return {}
    return current if isinstance(current, dict) else {}


def recorder_row(panel):
    """The recorder slot's whole accounting: this process, and what it called.

    The recorder was an agent with a model, a token spend and a state of its
    own. It is this orchestrator now, so the row says ``in process`` and
    carries no model — a row claiming the model it was once planned with would
    read as an agent that ran.
    """
    return {'label': 'recorder', 'slot': 'recorder', 'attempt': 1, 'agent': 'recorder',
            'observed_tokens': 0,
            # Zero rather than absent, and zero in every unit: no session ran
            # for this slot, so there is nothing to not record.
            **{field: 0 for field in TOKEN_FIELDS},
            'tool_calls': getattr(panel, 'calls', 0) or 0,
            'seconds': round(panel.budget.seconds, 3) if panel.budget else 0.0,
            'model': '', 'state': IN_PROCESS,
            'detail': 'the run itself: every recorder tool was called in this process, '
                      'with no model'}


def agent_rows(budget, panel=None):
    """One row per attempt, in the vocabulary the engine's readers use.

    :class:`orchestrator.Budget` keeps the run's own words —
    ``observed_tokens``, ``tool_calls``, the runner's ending. ``runtime.json``
    is read by :mod:`peerreviewagents.engine.usage`, which was written against
    the harness's stream, so the names are translated here in one place and the
    runner's own ending is kept beside the translation rather than lost.

    The token fields are the exception to the translation: they go in under
    their own names, :data:`~peerreviewagents.names.TOKEN_FIELDS`, because
    there is no older name for them to be mistaken for. ``tokens`` beside them
    is the cumulative total, which is what this file has always meant by it.
    """
    rows = list(sorted(budget.rows, key=lambda row: row['label']))
    if panel is not None:
        rows.append(recorder_row(panel))
    return [{'label': row['label'],
             'agentType': row['agent'],
             'attempt': row['attempt'],
             'tokens': row['observed_tokens'],
             **{field: row.get(field) for field in TOKEN_FIELDS},
             'toolCalls': row['tool_calls'],
             'model': row['model'] or None,
             # `done` is the word every reader of this file already knows for a
             # slot that finished; `ending` is the runner's own.
             'state': 'done' if row['state'] in (agent_runner.ANSWERED, IN_PROCESS)
             else row['state'],
             'ending': row['state'],
             'seconds': row['seconds'],
             'detail': row['detail'] or None} for row in rows]


def write_usage(review, rows):
    """Usage the engine's own report reader can find, one receipt per attempt.

    ``observed_tokens`` is the cumulative spend, as it always was, and
    :data:`~peerreviewagents.names.TOKEN_FIELDS` is the same number broken into
    its parts beside it — plus ``tokens_peak_context``, the largest single call,
    which is the figure a run from before the orchestrator reported per agent.
    """
    for row in rows:
        slot = str(row['slot']).replace(' ', '-').replace('/', '-')
        folder = Path(review) / 'usage' / slot
        folder.mkdir(parents=True, exist_ok=True)
        attempt = int(row.get('attempt') or 1)
        write(folder / f'{slot}-{attempt}.started.json',
              {'label': row['label'], 'attempt': attempt, 'agent': row.get('agent')})
        with (folder / 'usage.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(json.dumps({'label': row['label'], 'attempt': attempt,
                                     'observed_tokens': row.get('observed_tokens') or 0,
                                     **{field: row.get(field) or 0
                                        for field in TOKEN_FIELDS},
                                     'tool_calls': row.get('tool_calls') or 0,
                                     'model': row.get('model') or None,
                                     # The runner's own word for how it ended,
                                     # and the sentence it left with it.
                                     'state': row.get('state'),
                                     'detail': row.get('detail') or None}) + '\n')


def write_identity(review, source, arm, reviewers, cutoff=None, tracks=None, models=None,
                   reviewer_models=None):
    """Say which paper and which plan this folder was, when the server never did.

    The server writes ``protocol.json`` and ``manifest.json`` from the imported
    manuscript, which is the full record and is left alone wherever it exists.
    A run that never reached ``ingest`` — a tool server that would not come up,
    a refused manuscript — has neither, and a folder that cannot name its own
    paper or arm is not a result anybody can file. These two are the thin
    version: the source hash, the plan, the versions, and nothing the run did
    not actually learn.

    ``models`` is the per-slot assignment and ``reviewer_models`` the ordered
    list the core reviewers were to be drawn from. Both are written, because
    the list is what a later comparison has to be pinned to and a folder
    holding only the per-slot mapping cannot say what order it came from.
    """
    review, source = Path(review), Path(source)
    try:
        source_sha256 = digest(source.read_bytes()) if source.is_file() else None
    except OSError:
        source_sha256 = None
    plan = ARMS.get(arm) or {}
    # The run was going to run the arm's own default tracks, so a folder that
    # records `specialists: []` for a full two-track run says the plan was
    # something it never was.
    default = DRAFT_CHECK_TRACKS if arm == 'draft-check' else SPECIALIST_TRACKS
    specialists = [track for track in (tracks or default) if track in SPECIALIST_TRACKS]
    wrote = []
    if not (review / 'protocol.json').is_file():
        write(review / 'protocol.json', {
            'provider': 'claude-code',
            'mode': arm,
            'arm': arm,
            'reviewers': reviewers,
            'paper_id': None,
            # The models this run was going to use. A run that never reached
            # ingest still says which model each slot was asked for, so a
            # folder that holds no review is still an arm of the experiment.
            'model': model_plan.describe(models or {}),
            'models': dict(models or {}),
            'reviewer_models': list(reviewer_models or ()),
            'literature_cutoff': cutoff,
            'version': WORKFLOW_VERSION,
            'finding_schema_version': FINDING_SCHEMA_VERSION,
            'pdf': str(source),
            'agents': {},
            'stage': 'startup',
            'written_by': 'starter: the run stopped before the server recorded its protocol',
            'plan': {'arm': arm, 'reviewers': reviewers,
                     'specialists': specialists if plan.get('specialists') else [],
                     'editor': plan.get('editor'), 'revision': plan.get('revision'),
                     'summary': plan.get('summary'),
                     'models': dict(models or {}),
                     'reviewer_models': list(reviewer_models or ())},
        })
        wrote.append('protocol.json')
    if not (review / 'manifest.json').is_file():
        write(review / 'manifest.json', {
            'paper_id': None,
            'source_sha256': source_sha256,
            'pdf': str(source),
            'code': {},
            'workflow_version': WORKFLOW_VERSION,
            'finding_schema_version': FINDING_SCHEMA_VERSION,
            'claude_code_version': claude_version() or os.environ.get(
                'PEERREVIEW_CLAUDE_VERSION'),
            'written_by': 'starter: the run stopped before the manuscript was imported',
        })
        wrote.append('manifest.json')
    return wrote


def finish(review, status, reason, budget, wall, result=None, panel=None, runner=None):
    """Write the run's own accounting: ``runtime.json``, then every receipt."""
    review = Path(review)
    rows = list(budget.rows) + ([recorder_row(panel)] if panel is not None else [])
    write(review / 'runtime.json', {
        'status': status,
        'blocked_reason': reason,
        'wall_seconds': round(wall, 1),
        'tokens_total': budget.spent(),
        'agents': agent_rows(budget, panel),
        'rate_limits': [{'reason': limit} for limit in budget.limits],
        'per_agent_token_ceiling': budget.per_agent,
        'per_run_token_ceiling': budget.per_run,
        'claude_code_version': claude_version() or os.environ.get('PEERREVIEW_CLAUDE_VERSION'),
        'runner': runner.describe() if runner is not None else None,
        # What the orchestrator itself returned: the route, the stages it
        # closed, the counts the server recorded. The old `result` was the
        # driving session's own last event, which no longer exists.
        'result': result or None,
        'notes': list(panel.notes) if panel is not None else [],
        'recorded': now(),
    })
    write_usage(review, rows)
    progress = review / 'progress.json'
    if status == 'blocked' and progress.is_file():
        current = read(progress)
        if current.get('status') not in COMPLETE_STATUSES:
            write(progress, {**current, 'status': 'blocked',
                             'error': reason or current.get('error'), 'updated': now()})


def supersede(review):
    """Say that the run state in this folder belongs to an attempt that is over.

    A retry writes into the same folder, and until it ends, the files from the
    attempt before it are the only ones there: ``read_run`` and ``peerreview
    report`` would answer with the old run's status and the old run's cause
    while the new one is underway. Both files are marked as running again, the
    state they replace is kept whole under ``superseded`` so nothing is lost,
    and the server's own ``ingest`` overwrites ``progress.json`` outright a
    moment later.
    """
    review, marked = Path(review), {}
    for name, reason_key in (('runtime.json', 'blocked_reason'), ('progress.json', 'error')):
        path = review / name
        if not path.is_file():
            continue
        try:
            previous = read(path)
        except (ValueError, UnicodeDecodeError):
            previous = {'status': None, 'unreadable': True}
        if not isinstance(previous, dict):
            previous = {'status': None, 'unreadable': True}
        if previous.get('status') == 'running':
            continue
        ended = previous.get('status') or 'in an unrecorded state'
        detail = previous.get(reason_key)
        write(path, {**{key: previous[key] for key in ('reviewers', 'arm') if key in previous},
                     'status': 'running', reason_key: None,
                     'note': f'a new run started in this folder; the attempt before it ended '
                             f'{ended}' + (f': {detail}' if detail else ''),
                     'superseded': previous, 'updated': now()})
        marked[name] = previous.get('status')
    return marked


def rerender(review, workspace=None):
    """Render REVIEW.md again, now that the run's own accounting is on disk.

    ``write_review`` writes the reports while the run is still open, which is
    before ``runtime.json`` or a single usage receipt exists: that copy can
    only report no tokens and no models. This is the first moment the whole
    accounting exists, so the reports are written once more over the top.
    """
    review = Path(review)
    if not (review / 'protocol.json').is_file():
        return None
    try:
        from ..engine.run_report import write_reports
        paths = write_reports(review, workspace, review)
        return {key: str(value) for key, value in paths.items()}
    except Exception as exc:  # noqa: BLE001 - a report must never lose a finished review
        return {'error': str(exc)}


def failures_line(budget):
    """Every agent that did not answer, as one sentence, or ``''``."""
    rows = budget.failures()
    if not rows:
        return ''
    said = []
    for row in rows:
        detail = str(row.get('detail') or '')[:LAST_WORD_CHARACTERS]
        said.append(f'{row["label"]} ({row["state"]}' + (f': {detail}' if detail else '') + ')')
    return 'these agents did not answer: ' + '; '.join(said)


def runner_for(runner=None):
    """What runs the agents: Claude Code unless a caller brought its own."""
    return runner if runner is not None else ClaudeCodeRunner()


def runner_missing(runner):
    """The line that says this runner cannot run anything at all, or ``''``.

    Asked with no models, because it is not a question about the plan: Claude
    Code that is not installed refuses every model there is.
    """
    problems = runner.unavailable({})
    return str(problems[0]) if problems else ''


def refuse_models(runner, wanted):
    """Raise if this login cannot run the models the plan asks for.

    The runner owns the question, because the answer is about the login rather
    than about the plan: an ``availableModels`` allowlist that leaves one of
    these out is a bad argument, refused before anything is written, because a
    model substituted behind our back would leave the run recording an arm it
    did not run.

    A runner that is not installed at all is not refused here. Nothing is wrong
    with what the caller asked for, so that run is opened, identified and
    marked blocked at start-up like any other run that could not begin — see
    :func:`conduct`.

    A name no version of this package knows is the one refusal the runner is
    never asked about. It is a misspelling, not a login, and a machine with no
    harness installed has to answer it the same way as a machine with one:
    otherwise ``--reviewer-models claude-nonesuch-9`` reads as "blocked at
    start-up, no harness" and sends the reader to their installation over a
    typo.
    """
    unknown = model_plan.problems(dict(wanted))
    if unknown:
        raise ValueError('; '.join(unknown))
    if runner_missing(runner):
        return
    problems = runner.unavailable(dict(wanted))
    if problems:
        raise ValueError('; '.join(problems))


def conduct(panel_class, review_dir, request, budget, runner, directory=None, watch=None):
    """Open the tools, run one panel to its end, and say how it ended.

    Returns ``(result, blocked, stage, panel)``: the orchestrator's own answer
    or ``None``, the reason the run is blocked or ``None``, how far it got, and
    the panel itself for its notes and its tool count.

    ``stage`` is ``startup`` where nothing was spent — the tool server would
    not come up, or the server refused to open the run — and ``session``
    everywhere else. :func:`exit_code` turns the first into the code 2, because
    the fix is on this machine and no subscription usage was spent.
    """
    service = Service(plugin_root=directory)
    # A runner whose agents are in this process calls the tools directly, and
    # this is the only place that holds the service they belong to.
    runner.opened(service)
    panel = panel_class(service, runner, budget, watch)
    started = time.monotonic()
    missing = runner_missing(runner)
    if missing:
        # Nothing to run the agents with. The run folder is written and
        # identified all the same, because the caller asked for a review of a
        # named paper and the folder is what says which one it was.
        service.stop()
        write(Path(review_dir) / 'startup.json',
              {'runner': runner.describe(), 'server': None, 'problems': [missing],
               'tools_expected': list(tool_names()), 'at': now()})
        return None, missing, 'startup', panel
    try:
        address = service.listen() if runner.remote else None
    except RuntimeError as problem:
        write(Path(review_dir) / 'startup.json',
              {'runner': runner.describe(), 'server': None, 'problems': [str(problem)],
               'tools_expected': list(tool_names()), 'at': now()})
        return None, f'the run\'s tool server could not be started: {problem}', 'startup', panel
    write(Path(review_dir) / 'startup.json',
          {'runner': runner.describe(), 'server': address, 'problems': [],
           'tools_expected': list(tool_names()), 'at': now()})
    try:
        return panel.run(request), None, 'session', panel
    except orchestrator.Blocked as stop:
        return None, stop.reason, 'session', panel
    except ValueError as refused:
        # The server refused to open the run at all — a manuscript it will not
        # import, a folder already holding another plan. Nothing ran, so this
        # is a start-up failure and not a review that stopped.
        stage = 'startup' if not budget.rows else 'session'
        return None, str(refused), stage, panel
    except Exception as broken:  # noqa: BLE001 - a crash is an outcome, not a lost folder
        return (None, f'the run stopped with an unexpected error after '
                f'{time.monotonic() - started:.0f}s: {type(broken).__name__}: {broken}',
                'session', panel)
    finally:
        service.stop()


def settle_run(wall_timeout):
    """Put this run's deadline and Claude Code's version where the tools look.

    Both are read from the environment by code that cannot see this function:
    ``server.run_deadline`` for a tool deciding whether it has time to wait,
    and the provenance for the version. In-process now, so they are set here
    rather than passed into a child.
    """
    os.environ['PEERREVIEW_RUN_DEADLINE'] = f'{time.time() + float(wall_timeout):.0f}'
    version = claude_version()
    if version:
        os.environ['PEERREVIEW_CLAUDE_VERSION'] = version
    return version


def review(pdf, out, reviewers=3, arm=DEFAULT_ARM, literature_cutoff=None, tracks=None,
           per_agent_tokens=PER_AGENT_TOKENS, per_run_tokens=PER_RUN_TOKENS,
           wall_timeout=WALL_TIMEOUT, directory=None, cwd=None,
           reviewer_models=None, specialist_model=None, editor_model=None,
           citation_workers=CITATION_WORKERS, citation_worker_model=None,
           reader_model=None, code=None, runner=None, watch=None):
    """Run one review and return its summary.

    The summary is a dict, not an exit code: ``status``, ``blocked_reason``,
    the run and review folders, the arm, the wall time, the tokens in total and
    per agent, the models each slot was asked to run (``models_requested``) and
    the models the agents reported running (``models``), and the counts the
    server recorded. ``stage`` says how far the run got — ``startup`` means
    nothing was spent. :func:`exit_code` turns it into a process status, and
    nothing here prints: the command prints, a library caller reads the dict
    and the run folder.

    ``reviewer_models`` is the ordered list the core reviewers are drawn from,
    reviewer *i* running ``list[(i - 1) % len(list)]``; the default makes three
    reviewers three different models. ``specialist_model`` and ``editor_model``
    are one model each. There is no recorder model: that slot's work is done in
    this process. ``citation_workers`` is how many
    workers judge the in-text citations against the sources the server
    resolved, and ``citation_worker_model`` is the model they run — Haiku by
    default, because a worker reads one sentence and one stored passage. They
    exist only in an arm that runs the citations specialist.

    ``reader_model`` is the model that reads the manuscript against its own
    page images before the reviewers see it — repairing the equations and
    tables the converter garbled, describing the figures it could only name.
    Every review of a PDF has that stage; there is no way to review the
    converter's text on its own.

    Anything not given here falls back to the ``PEERREVIEW_*_MODEL(S)``
    variables in this process's environment, and only then to the defaults —
    one merge, in :func:`peerreviewagents.plugin.models.settle`, the same one
    the server's ``ingest`` makes, so the plan this function records and the
    plan the server fixes cannot disagree.

    ``code`` is the code this paper is reviewed against: a local folder or a git
    URL. ``ingest`` copies the text source of it into ``<out>/code`` and plans
    one further lane — ``spec-code`` — that reads the paper against that
    snapshot and quotes both sides of what it files. Nothing in the snapshot is
    executed, and a run given no code reports that the code was not reviewed
    rather than saying nothing about it. An arm that runs no specialists is
    refused, because that lane is a specialist.

    ``runner`` is what runs one agent — :class:`ClaudeCodeRunner` by default,
    and a scripted runner in the tests, which is how the whole panel is
    exercised without a model. ``watch`` is called with each phase, tool call
    and line the run says about itself, for a caller showing progress.

    An unknown arm, a manuscript that is not there, a missing plugin and a
    model this login cannot run all raise ``ValueError`` before anything is
    written or any usage is spent — a caller gave an argument no run can be
    made from, which is not a run that stopped. :func:`review_main` turns that
    into a message and the exit code 2.
    """
    if arm not in ARMS:
        raise ValueError(f'Unknown arm {arm}. Arms: ' + ', '.join(REVIEWABLE_ARMS))
    source = Path(pdf).resolve()
    if not source.is_file():
        raise ValueError(f'No manuscript at {source}')
    # The code is checked here for the same reason the arm and the models are:
    # a run opened to review code against an arm that reads none, or against a
    # folder that is not there, would spend a reader before the server refused.
    if code and not ARMS[arm]['specialists']:
        raise ValueError('The ' + str(arm) + ' arm runs no specialists, and the code review is '
                         'one. Run an arm with specialists, or drop --code. Arms that run them: '
                         + ', '.join(name for name in REVIEWABLE_ARMS
                                     if ARMS[name]['specialists']) + '.')
    # The fix-check arm is not a review and this is not the command that runs
    # it: it takes problems rather than reviewers, its own ceilings and its own
    # written document. Refused here by name, after the code refusal above, so
    # that `--code` against an arm that runs no specialists still answers the
    # question the caller actually asked.
    if arm == FIX_ARM:
        raise ValueError('The ' + FIX_ARM + ' arm reads a new draft against problems a review '
                         'already raised, which is not a review: run `peerreview check-fixes '
                         '<pdf> <out> --problems FILE`. Arms this command runs: '
                         + ', '.join(REVIEWABLE_ARMS) + '.')
    if code and not code_source.is_url(code) and not Path(code).expanduser().is_dir():
        raise ValueError('No code folder at ' + str(Path(code).expanduser())
                         + '. Give a folder that exists or a repository URL.')
    if code and not code_source.is_url(code):
        # The out folder is where this run writes its own files, so a snapshot
        # of it would hold the review and not the code. A code folder that
        # *contains* the out folder is the normal case — a run folder under the
        # repository being reviewed — and the snapshot leaves that one folder
        # out rather than refusing the review.
        given, within = Path(code).expanduser().resolve(), Path(out).resolve()
        if given == within or within in given.parents:
            raise ValueError(str(given) + ' is inside the out folder ' + str(within)
                             + ', which is where this run writes its own files: the snapshot '
                             'would hold the run rather than the code. Give the folder the code '
                             'is in — a run folder inside it is left out of the snapshot.')
    root = Path(out).resolve()
    review_dir = root / RUN_DIRECTORY
    directory = Path(directory) if directory else plugin_dir()
    if not (directory / '.claude-plugin' / 'plugin.json').is_file():
        raise ValueError(f'No plugin at {directory}')
    # Which model each slot will run, settled and checked here: a model this
    # login cannot run is refused before anything is spent, because Claude
    # Code's own answer to one is to quietly run a different model.
    settled = model_plan.settle(reviewer_models, specialist_model, editor_model,
                                citation_worker_model, reader_model)
    reviewer_list = list(model_plan.parse_list(settled.get('reviewers'))
                         or model_plan.DEFAULT_REVIEWER_MODELS)
    # The citation workers are planned here too, so a login that cannot run the
    # worker model is refused before anything is spent — the same rule as every
    # other slot. They are not part of `plan_slots`: a worker closes no stage.
    workers = citation_slots(arm, citation_workers, tracks)
    # Whether this arm has a citation lane at all, which is not the same
    # question as whether this run asked for workers in it. ``--citation-workers
    # 0`` is an arm that has the lane and wants nothing judged in it.
    citation_lane = bool(citation_slots(arm, 1, tracks))
    # The reader is planned here too, except where there is no Read stage to
    # run. A folder that already holds a sealed manuscript (an application's
    # own source through `write_manuscript`, or a run picked up after its own
    # seal) has no draft to repair and the server plans no reader slot for it.
    reader_slots = (() if (root / MANUSCRIPT_DIRECTORY / 'manifest.json').is_file()
                    else ('reader',))
    # The recorder is not in here. The server plans the slot — it is the run
    # key's own slot — but this process holds it and starts no session for it,
    # so it has no model to ask for and naming one would have `refuse_models`
    # below turn away a login over a model nothing was going to run. See
    # :func:`recorder_row`.
    wanted = model_plan.assign(
        (*reader_slots, *plan_slots(arm, reviewers, tracks, bool(code)), *workers),
        **settled)
    runner = runner_for(runner)
    refuse_models(runner, wanted)
    review_dir.mkdir(parents=True, exist_ok=True)
    supersede(review_dir)

    settle_run(wall_timeout)
    budget = orchestrator.Budget(per_agent_tokens, per_run_tokens, wall_timeout)
    request = {
        'pdf': str(source),
        'out': str(root),
        'reviewers': reviewers,
        'arm': arm,
        'literature_cutoff': literature_cutoff or '',
        'tracks': list(tracks) if tracks else None,
        'reviewer_models': list(reviewer_list),
        'specialist_model': settled.get('specialist') or '',
        'editor_model': settled.get('editor') or '',
        'citation_workers': len(workers) if citation_lane else CITATION_WORKERS,
        'citation_worker_model': settled.get('citation_worker') or '',
        'reader_model': settled.get('reader') or '',
        'code': code or '',
    }
    started = time.monotonic()
    result, blocked, stage, panel = conduct(orchestrator.Review, review_dir, request, budget,
                                            runner, directory, watch)
    wall = time.monotonic() - started
    recorded = recorded_progress(review_dir)
    status = 'blocked' if blocked else None
    if status is None:
        status = str(recorded.get('status') or '') or None
        if status not in COMPLETE_STATUSES:
            # A panel that ends without write_review or mark_blocked leaves the
            # server's own status at `running`, which would read as a review
            # still in flight long after the run is over.
            blocked = recorded.get('error') or '; '.join(part for part in (
                (f'the panel ended without writing the review or marking the run blocked, the '
                 f'server last recorded {status}' if status
                 else 'the server wrote no progress file'),
                (panel.notes[-1] if panel.notes else ''), failures_line(budget)) if part)
            status = 'blocked'
    # A run that stopped before `ingest` left no protocol and no manifest, so
    # the folder would not name its own paper or arm.
    write_identity(review_dir, source, arm, reviewers, literature_cutoff, tracks, wanted,
                   reviewer_list)
    finish(review_dir, status, blocked, budget, wall, result, panel, runner)
    report = rerender(review_dir, root / MANUSCRIPT_DIRECTORY)
    summary = {
        'status': status,
        'blocked_reason': blocked,
        'stage': stage,
        'run': str(root),
        'review': str(review_dir),
        'report': report,
        'arm': arm,
        'reviewers': reviewers,
        'wall_seconds': round(wall, 1),
        'tokens_total': budget.spent(),
        'tokens_by_agent': {row['label']: row['observed_tokens']
                            for row in sorted(budget.rows, key=lambda r: r['label'])},
        # Two different facts: what was asked for, per slot, and what the
        # agents reported running. They disagree when a model was substituted.
        'models_requested': dict(wanted),
        'models': sorted({row['model'] for row in budget.rows if row['model']}),
        'agents_run': len(budget.rows),
        'runner': runner.describe(),
    }
    progress = review_dir / 'progress.json'
    if progress.is_file():
        done = read(progress)
        summary['findings_delivered'] = done.get('findings_delivered')
        summary['issues'] = done.get('issues')
        summary['quarantined'] = done.get('quarantined_findings_count')
        summary['stages'] = done.get('stages')
        # Whether the code was reviewed, and how much of the snapshot the lane
        # opened. A caller that asked for a code review reads this rather than
        # trusting that asking was enough.
        summary['code'] = done.get('code')
    return summary


def exit_code(summary):
    """What the command exits with: 0 reviewed, 2 never started, 1 anything else.

    A run that never started is worth its own code, because no subscription
    usage was spent and the fix is on this machine.
    """
    if summary.get('status') in COMPLETE_STATUSES:
        return 0
    return 2 if summary.get('stage') == 'startup' else 1


def build_parser(prog='peerreview review'):
    parser = argparse.ArgumentParser(
        prog=prog,
        description='Review one manuscript with a panel of Claude Code agents.')
    parser.add_argument('workspace', help='the manuscript PDF')
    parser.add_argument('output', help='the run folder to write')
    parser.add_argument('--arm', default=DEFAULT_ARM, choices=list(REVIEWABLE_ARMS),
                        help='which study arm to run (default: %(default)s)')
    parser.add_argument('--reviewers', type=int, default=3,
                        help='core reviewers, where the arm does not fix it (default: 3)')
    parser.add_argument('--track', action='append', choices=list(SPECIALIST_TRACKS),
                        help='for the draft-check arm: a specialist track, repeatable')
    parser.add_argument('--literature-cutoff', default=None,
                        help='no literature after this date counts against the paper')
    parser.add_argument('--reviewer-models', default=None, metavar='A,B,C',
                        help='the models the core reviewers are drawn from, in order; '
                             'reviewer i runs the i-th, wrapping round (default: '
                             + ', '.join(model_plan.DEFAULT_REVIEWER_MODELS) + ')')
    parser.add_argument('--specialist-model', default=None, metavar='MODEL',
                        help='the model every specialist runs (default: '
                             + model_plan.DEFAULT_SPECIALIST_MODEL + ')')
    parser.add_argument('--editor-model', default=None, metavar='MODEL',
                        help='the model the editor runs (default: '
                             + model_plan.DEFAULT_EDITOR_MODEL + ')')
    parser.add_argument('--citation-workers', type=int, default=CITATION_WORKERS,
                        metavar='N', choices=range(0, MAX_CITATION_WORKERS + 1),
                        help='workers judging the in-text citations against the resolved '
                             'sources, where the arm runs the citations specialist '
                             '(default: %(default)s)')
    parser.add_argument('--citation-worker-model', default=None, metavar='MODEL',
                        help='the model every citation worker runs (default: '
                             + model_plan.DEFAULT_CITATION_WORKER_MODEL + ')')
    parser.add_argument('--reader-model', default=None, metavar='MODEL',
                        help='the model that reads the manuscript against its page images '
                             'before the reviewers, repairing the equations and tables the '
                             'converter garbled and describing the figures (default: '
                             + model_plan.DEFAULT_READER_MODEL + ')')
    parser.add_argument('--code', default=None, metavar='FOLDER_OR_URL',
                        help='the code this paper is reviewed against: a local folder or a git '
                             'URL. Its text source is copied into <out>/code and one further '
                             'lane reads the paper against that snapshot, quoting both sides. '
                             'Nothing in it is ever run')
    parser.add_argument('--per-agent-tokens', type=int, default=PER_AGENT_TOKENS)
    parser.add_argument('--per-run-tokens', type=int, default=PER_RUN_TOKENS)
    parser.add_argument('--timeout', type=int, default=WALL_TIMEOUT,
                        help='wall-clock seconds before the run is blocked')
    parser.add_argument('--plugin-dir', default=None, help='override the plugin folder')
    return parser


def review_main(argv=None, prog='peerreview review'):
    """``peerreview review`` as a function: parse, run, print the summary, return a code.

    This is the supported entry point for an application that wraps the
    command. It takes the argument list the user typed — with or without a
    leading ``review`` — prints the run summary as JSON on stdout and returns
    the process exit code rather than raising it, so a caller decides what to
    do with a blocked run. ``prog`` is the program name its ``--help`` and its
    errors use, so a wrapper named something else says its own name.

    An argument no run can be made from — a manuscript that is not there, an
    arm that does not exist, a plugin folder that holds no plugin, a model this
    login cannot run — is a message on stderr and the code 2, the same code as
    a run that never started, because nothing was written and no usage was
    spent. Nothing is raised for it.

    Nothing else here needs to be reached for: the summary dict comes from
    :func:`review` and the code from :func:`exit_code`.
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == 'review':
        argv = argv[1:]
    args = build_parser(prog=prog).parse_args(argv)
    try:
        summary = review(args.workspace, args.output, reviewers=args.reviewers, arm=args.arm,
                         literature_cutoff=args.literature_cutoff, tracks=args.track,
                         per_agent_tokens=args.per_agent_tokens,
                         per_run_tokens=args.per_run_tokens,
                         wall_timeout=args.timeout, directory=args.plugin_dir,
                         reviewer_models=args.reviewer_models,
                         specialist_model=args.specialist_model,
                         editor_model=args.editor_model,
                         citation_workers=args.citation_workers,
                         citation_worker_model=args.citation_worker_model,
                         reader_model=args.reader_model,
                         code=args.code)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(summary, indent=2))
    return exit_code(summary)


def check_fixes(pdf, out, problems, per_agent_tokens=FIX_PER_AGENT_TOKENS,
                per_run_tokens=FIX_PER_RUN_TOKENS, wall_timeout=FIX_WALL_TIMEOUT,
                directory=None, cwd=None, specialist_model=None,
                reader_model=None, runner=None, watch=None):
    """Read one new draft against the problems a review raised, and return the summary.

    The same machinery as :func:`review` — the same tool server, the same
    runner, the same budget and the same accounting — for a run that raises
    nothing. ``problems`` is the path to a file of problems in the shape
    :mod:`peerreviewagents.problems` validates, which
    :func:`peerreviewagents.problems_from_run` writes from a finished review.
    The manuscript is the *new* draft and it is read from the PDF as a review
    reads one: there is no way to check a draft nobody has read.

    The summary is a dict, not an exit code: ``status``, ``blocked_reason``,
    the run and review folders, the wall time, the tokens, the models asked for
    and the models the agents reported running, and the check's own counts —
    ``problems``, ``checked``, ``counts`` per verdict, ``not_checked``.
    :func:`exit_code` turns it into a process status and nothing here prints.
    :func:`peerreviewagents.read_fix_check` is the reading of the folder it
    leaves behind.

    The ceilings are the fix check's own and are far below a review's, because
    the run is far smaller: :data:`FIX_PER_AGENT_TOKENS` per agent against
    :data:`PER_AGENT_TOKENS`, :data:`FIX_PER_RUN_TOKENS` for the run against
    :data:`PER_RUN_TOKENS`. All four are provisional — see the comment above
    them — and all four are measured against an agent's cumulative spend.

    There is no ``code`` here, and that is deliberate rather than unfinished:
    every verdict this run can give rests on the new draft — a quotation of it,
    or the server's own confirmation that an old quotation is gone or still
    there — so a code snapshot would add a second input and a second
    verification path to support no verdict at all. A problem whose fix is in
    the code is what ``cannot-tell`` is for, and the reason it carries says so.

    A problems file that cannot be read, a manuscript that is not there, a
    missing plugin and a model this login cannot run all raise ``ValueError``
    before anything is written or any usage is spent.
    """
    source = Path(pdf).resolve()
    if not source.is_file():
        raise ValueError(f'No manuscript at {source}')
    # The problems are read here, before anything is opened, for the same
    # reason the models are: a run opened against a file the server will refuse
    # would spend a reader first, and the complaint would arrive from inside a
    # run folder instead of from the command line.
    document = problem_list.read(problems)
    rows = document['problems']
    if not rows:
        raise ValueError(str(Path(problems)) + ' holds no problems, so there is nothing to check '
                         'the draft against.')
    root = Path(out).resolve()
    review_dir = root / RUN_DIRECTORY
    directory = Path(directory) if directory else plugin_dir()
    if not (directory / '.claude-plugin' / 'plugin.json').is_file():
        raise ValueError(f'No plugin at {directory}')
    settled = model_plan.settle(None, specialist_model, None, None, reader_model)
    reader_slots = (() if (root / MANUSCRIPT_DIRECTORY / 'manifest.json').is_file()
                    else ('reader',))
    slots = checker_slots(FIX_ARM, len(rows))
    wanted = model_plan.assign((*reader_slots, *slots), **settled)
    runner = runner_for(runner)
    refuse_models(runner, wanted)
    review_dir.mkdir(parents=True, exist_ok=True)
    supersede(review_dir)

    checked = min(len(rows), MAX_FIX_CHECKS)
    settle_run(wall_timeout)
    budget = orchestrator.Budget(per_agent_tokens, per_run_tokens, wall_timeout)
    request = {
        'pdf': str(source),
        'out': str(root),
        'problems': str(Path(problems).resolve()),
        'specialist_model': settled.get('specialist') or '',
        'reader_model': settled.get('reader') or '',
    }
    started = time.monotonic()
    result, blocked, stage, panel = conduct(orchestrator.FixCheck, review_dir, request, budget,
                                            runner, directory, watch)
    wall = time.monotonic() - started
    recorded = recorded_progress(review_dir)
    status = 'blocked' if blocked else None
    if status is None:
        status = str(recorded.get('status') or '') or None
        if status not in COMPLETE_STATUSES:
            blocked = recorded.get('error') or '; '.join(part for part in (
                (f'the panel ended without writing the fix check or marking the run blocked, '
                 f'the server last recorded {status}' if status
                 else 'the server wrote no progress file'),
                (panel.notes[-1] if panel.notes else ''), failures_line(budget)) if part)
            status = 'blocked'
    write_identity(review_dir, source, FIX_ARM, 0, models=wanted)
    finish(review_dir, status, blocked, budget, wall, result, panel, runner)
    summary = {
        'status': status,
        'blocked_reason': blocked,
        'stage': stage,
        'run': str(root),
        'review': str(review_dir),
        'arm': FIX_ARM,
        'problems': len(rows),
        'problems_file': str(Path(problems).resolve()),
        'wall_seconds': round(wall, 1),
        'tokens_total': budget.spent(),
        'tokens_by_agent': {row['label']: row['observed_tokens']
                            for row in sorted(budget.rows, key=lambda r: r['label'])},
        'models_requested': dict(wanted),
        'models': sorted({row['model'] for row in budget.rows if row['model']}),
        'agents_run': len(budget.rows),
        'runner': runner.describe(),
        'planned': checked,
    }
    progress = review_dir / 'progress.json'
    if progress.is_file():
        done = read(progress)
        record = done.get('fix_check') if isinstance(done.get('fix_check'), dict) else {}
        summary['checked'] = record.get('checked')
        summary['not_checked'] = record.get('not_checked')
        summary['counts'] = record.get('counts')
        summary['over_cap'] = record.get('over_cap')
        summary['stages'] = done.get('stages')
    return summary


def check_fixes_parser(prog='peerreview check-fixes'):
    """The argument parser behind ``peerreview check-fixes``."""
    parser = argparse.ArgumentParser(
        prog=prog,
        description='Read a new draft against the problems a review already raised. '
                    'Nothing new is raised.')
    parser.add_argument('workspace', help='the new draft, as a PDF')
    parser.add_argument('output', help='the run folder to write')
    parser.add_argument('--problems', required=True, metavar='FILE',
                        help='the problems to check the draft against: a JSON file of them, as '
                             'peerreviewagents.problems_from_run writes from a finished review')
    parser.add_argument('--specialist-model', default=None, metavar='MODEL',
                        help='the model every checker runs (default: '
                             + model_plan.DEFAULT_SPECIALIST_MODEL + ')')
    parser.add_argument('--reader-model', default=None, metavar='MODEL',
                        help='the model that reads the new draft against its page images before '
                             'anything is checked (default: '
                             + model_plan.DEFAULT_READER_MODEL + ')')
    parser.add_argument('--per-agent-tokens', type=int, default=FIX_PER_AGENT_TOKENS)
    parser.add_argument('--per-run-tokens', type=int, default=FIX_PER_RUN_TOKENS)
    parser.add_argument('--timeout', type=int, default=FIX_WALL_TIMEOUT,
                        help='wall-clock seconds before the run is blocked')
    parser.add_argument('--plugin-dir', default=None, help='override the plugin folder')
    return parser


def check_fixes_main(argv=None, prog='peerreview check-fixes'):
    """``peerreview check-fixes`` as a function: parse, run, print the summary, return a code.

    The supported entry point for an application that wraps the command, and
    the twin of :func:`review_main`: it takes the argument list the user typed
    — with or without a leading ``check-fixes`` — prints the summary as JSON on
    stdout and returns the exit code rather than raising it. An argument no run
    can be made from, a problems file among them, is a message on stderr and
    the code 2.
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == 'check-fixes':
        argv = argv[1:]
    args = check_fixes_parser(prog=prog).parse_args(argv)
    try:
        summary = check_fixes(args.workspace, args.output, args.problems,
                              per_agent_tokens=args.per_agent_tokens,
                              per_run_tokens=args.per_run_tokens,
                              wall_timeout=args.timeout, directory=args.plugin_dir,
                              specialist_model=args.specialist_model,
                              reader_model=args.reader_model)
    except (ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(summary, indent=2))
    return exit_code(summary)


def main(argv=None):
    """The console script's entry point. :func:`review_main` is the public name."""
    return review_main(argv)


if __name__ == '__main__':
    raise SystemExit(review_main())
