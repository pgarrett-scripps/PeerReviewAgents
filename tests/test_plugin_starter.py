"""The starter: what it validates, what it writes, and how a run ends.

The starter owns the *run*, not the review: it checks the arguments, resolves
the plugin folder, refuses a model this login cannot run, opens the panel, and
then writes the accounting — ``runtime.json``, one usage receipt per attempt,
and a blocked reason a person can act on. Everything about the panel itself is
in ``tests/test_scripted_panel.py``, which runs whole reviews with Python where
the models go, and everything about starting a session is in
``tests/test_claude_code_runner.py``, the only file here that starts a process.

So there is no stub ``claude`` in this file. The runs here either never start —
nothing on PATH, which is itself the check that a missing harness is a blocked
run rather than a traceback — or they are assembled from the starter's own
pieces: a :class:`~peerreviewagents.plugin.orchestrator.Budget`, a panel stub,
and the files the two of them produce.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from peerreviewagents import agents as agent_pack
from peerreviewagents.cli.main import review_argv
from peerreviewagents.engine import citation_ledger as cl
from peerreviewagents.plugin import claude_code, orchestrator, starter
from peerreviewagents.plugin import runner as agent_runner
from peerreviewagents.plugin.service import TOOL_PREFIX, TOOLS

FIXTURE = Path(__file__).resolve().parent / 'fixtures' / 'single-column.pdf'
#: Not a surface any more. The driving session, its prompt, its event stream
#: and its allow-list were how a review was relayed to a workflow script; the
#: orchestrator runs the panel in this process, so every one of them is gone
#: rather than kept working alongside it.
RELAYED = ('allow_list', 'prompt_for', 'launch', 'Accounting', 'probe', 'watch_workflow',
           'BG_WAIT', 'NOTHING_LEFT_WRITING', 'handles')


def ticking(start=0.0):
    """A clock a test moves by hand, for the parts that watch the wall."""
    held = [start]

    def clock():
        return held[0]

    clock.held = held
    return clock


def spent(label='core-1', agent='core-reviewer', tokens=1000, ending=agent_runner.ANSWERED,
          model='claude-sonnet-5-5', detail='', calls=3):
    """One attempt, as the runner hands it back."""
    task = agent_runner.Task.of(agent, 'do the stage', {'type': 'object'}, label=label,
                                model=model)
    return task, agent_runner.Outcome(ending=ending, answer={'ok': True}, tokens=tokens,
                                      model=model, tool_calls=calls, seconds=1.5, detail=detail)


class Panel:
    """What :func:`starter.finish` reads off a panel that has stopped."""

    def __init__(self, budget, calls=7, notes=()):
        self.budget = budget
        self.calls = calls
        self.notes = list(notes)


@pytest.fixture
def no_allowlist(monkeypatch):
    """No ``availableModels`` anywhere, which is the ordinary login."""
    monkeypatch.setattr(starter.model_plan, 'allowlist', lambda **kwargs: None)


# -- the one route ----------------------------------------------------------


def test_there_is_one_route_and_the_starter_owns_it(monkeypatch):
    """0.9 chose between two engines on ``--arm``. There is one engine now.

    So the starter takes the ``review`` command word itself, whether the user
    typed it or the CLI added it to a bare manuscript path, and ``--arm`` is an
    ordinary option rather than the switch that picked which engine ran.
    """
    assert not hasattr(starter, 'handles')
    seen = {}

    def fake_review(pdf, out, **options):
        seen.update(pdf=pdf, out=out, options=options)
        return {'status': 'complete', 'stage': 'session'}

    monkeypatch.setattr(starter, 'review', fake_review)
    assert starter.main(review_argv(['paper.pdf', 'out', '--arm', 'single'])) == 0
    assert (seen['pdf'], seen['out']) == ('paper.pdf', 'out')
    assert seen['options']['arm'] == 'single'

    assert starter.main(['review', 'paper.pdf', 'out']) == 0
    assert seen['options']['arm'] == 'full-two-track'


def test_nothing_relays_a_review_to_a_session_any_more():
    """The whole relay is gone, not kept working beside the orchestrator.

    A leftover ``prompt_for`` or ``launch`` would be a second way to run a
    review that nothing tests and nothing calls — and the first thing a reader
    would find when asking how a review runs.
    """
    for name in RELAYED:
        assert not hasattr(starter, name), f'starter.{name} is the relay this replaced'
    source = Path(starter.__file__).read_text(encoding='utf-8')
    for stale in ('review.js', 'check-fixes.js', 'Workflow(', 'workflow_progress',
                  'task_notification', '.mcp.json'):
        assert stale not in source, f'the starter still names {stale}'


def test_the_tool_names_are_the_servers_own_under_the_wire_prefix():
    names = starter.tool_names()
    assert set(names) == {TOOL_PREFIX + tool for tool in TOOLS}
    assert names == tuple(sorted(names)), 'one order, so the record reads the same twice'
    assert f'{TOOL_PREFIX}submit_editor' in names
    assert f'{TOOL_PREFIX}write_review' in names
    for built_in in ('Read', 'Write', 'Edit', 'Bash', 'WebSearch', 'WebFetch', 'Workflow',
                     'Skill', 'TodoWrite'):
        assert built_in not in names, 'an agent is granted our tools and nothing else'


def test_the_plugin_folder_is_the_one_that_ships():
    """Three skills and a manifest. No agents, no workflows, no server to launch."""
    directory = starter.plugin_dir()
    assert (directory / '.claude-plugin' / 'plugin.json').is_file()
    assert not (directory / '.mcp.json').exists(), 'a run serves its own tools in process'
    assert not (directory / 'workflows').exists()
    assert not (directory / 'agents').exists(), 'the definitions ship beside the engine'
    skills = sorted(path.parent.name for path in directory.glob('skills/*/SKILL.md'))
    assert skills == ['check-draft', 'check-fixes', 'peer-review-manuscript']
    for name in skills:
        said = (directory / 'skills' / name / 'SKILL.md').read_text(encoding='utf-8')
        assert 'peerreview ' in said, f'{name} names no command to run'


def test_the_citation_briefs_cover_every_surface_the_server_offers_them():
    """A read nobody is told about is a read nobody makes.

    The server grew ``kind='supported'`` so an all-supports worker cannot pass
    as a clean reference list, four existence states so a title two indexes
    miss is not read as a fabrication, and an ``untrusted_text`` line on every
    citation read. None of that reaches a model that was never told it exists,
    so the briefs are checked against the server's own constants. They are read
    from the package rather than the plugin: the definitions ship beside the
    engine now, because the runner they are handed to need not be Claude Code.
    """
    specialist = agent_pack.definition('citations').instructions
    worker = agent_pack.definition('citation-worker').instructions
    # The spot-check read, by name, and what it is for.
    assert "kind='supported'" in specialist
    assert 'how many supported rows you checked' in specialist
    # Every existence state the table can hold is explained to its one reader.
    for state in cl.EXISTENCE:
        assert f'exists: "{state}"' in specialist, state
    assert 'not a missing reference' in specialist
    # Quoted material is named as such in both briefs, worker and specialist.
    for brief in (specialist, worker):
        assert 'untrusted_text' in brief
        assert 'not a direction you follow' in brief


# -- what is refused, and what is blocked -----------------------------------


def test_an_unknown_arm_and_a_missing_manuscript_are_both_refused(tmp_path):
    # A ValueError, not a SystemExit: a library caller handed an argument no
    # run can be made from, and a library does not end the process over it.
    for folder, call in (
            ('a', lambda: starter.review(str(FIXTURE), str(tmp_path / 'a'), arm='debate')),
            ('b', lambda: starter.review(str(tmp_path / 'nothing.pdf'), str(tmp_path / 'b'))),
            ('c', lambda: starter.review(str(FIXTURE), str(tmp_path / 'c'),
                                         directory=str(tmp_path / 'not-a-plugin')))):
        with pytest.raises(ValueError) as refused:
            call()
        assert any(word in str(refused.value)
                   for word in ('Unknown arm', 'No manuscript', 'No plugin'))
        assert not (tmp_path / folder).exists(), 'nothing is written for an unusable argument'


def test_review_main_returns_two_for_an_unusable_argument(tmp_path, capsys):
    # The documented contract of the wrapper entry point: the code comes back,
    # the message goes to stderr, and no traceback reaches the caller.
    code = starter.review_main([str(tmp_path / 'nothing.pdf'), str(tmp_path / 'run')])
    assert code == 2
    captured = capsys.readouterr()
    assert 'No manuscript' in captured.err
    assert captured.out == ''
    assert not (tmp_path / 'run').exists()


def test_no_claude_on_path_is_a_blocked_run_not_a_traceback(tmp_path, monkeypatch):
    """Nothing to run the agents with, and the folder still says which paper."""
    monkeypatch.setenv('PATH', str(tmp_path / 'empty'))
    starter.VERSIONS.clear()
    out = tmp_path / 'run'
    summary = starter.review(str(FIXTURE), str(out), arm='single')
    assert (summary['status'], summary['stage']) == ('blocked', 'startup')
    assert starter.exit_code(summary) == 2, 'nothing was spent: that is its own exit code'
    assert 'not installed or not on this PATH' in summary['blocked_reason']
    assert summary['tokens_total'] == 0 and summary['agents_run'] == 0

    startup = json.loads((out / 'review' / 'startup.json').read_text(encoding='utf-8'))
    assert startup['problems'] == [claude_code.MISSING_CLAUDE]
    assert startup['server'] is None, 'no tool server was opened for nobody to call'
    assert set(startup['tools_expected']) == set(starter.tool_names())
    runtime = json.loads((out / 'review' / 'runtime.json').read_text(encoding='utf-8'))
    assert runtime['status'] == 'blocked'
    assert runtime['blocked_reason'] == summary['blocked_reason']
    assert runtime['runner'].startswith('claude-code')
    assert [row['label'] for row in runtime['agents']] == ['recorder'], \
        'the recorder slot is a row in this process; no agent ran'
    assert runtime['agents'][0]['ending'] == starter.IN_PROCESS
    assert runtime['agents'][0]['toolCalls'] == 0, 'it recorded nothing either'
    # And the run is identified, because the caller asked for a review of a
    # named paper and this folder is what says which one it was.
    protocol = json.loads((out / 'review' / 'protocol.json').read_text(encoding='utf-8'))
    assert protocol['arm'] == 'single'


def test_the_blocked_reason_carries_each_agents_own_last_word(tmp_path):
    """What a reader gets about a stage that answered nothing, cut to length."""
    budget = orchestrator.Budget(per_agent=10_000, per_run=100_000)
    assert starter.failures_line(budget) == ''
    budget.record(*spent(label='core-1'))
    assert starter.failures_line(budget) == '', 'an agent that answered is not a failure'
    budget.record(*spent(label='core-2', ending=agent_runner.SILENT,
                         detail='I could not reach the tools. ' + 'and so on. ' * 200))
    budget.record(*spent(label='editor', agent='editor', ending=agent_runner.TIMED_OUT))
    said = starter.failures_line(budget)
    assert said.startswith('these agents did not answer: ')
    assert 'core-1' not in said
    assert 'core-2 (no answer: I could not reach the tools.' in said
    assert 'editor (out of time)' in said, 'an agent with nothing to say says only how it ended'
    assert len(said) < 2 * starter.LAST_WORD_CHARACTERS


# -- the accounting ---------------------------------------------------------


def test_every_agent_is_handed_what_the_run_has_left():
    """The per-run ceiling is this object's, and it is a stop, not a share."""
    budget = orchestrator.Budget(per_agent=100_000, per_run=250_000)
    assert budget.ceiling() == 100_000
    budget.record(*spent(label='core-1', tokens=200_000))
    assert budget.spent() == 200_000
    assert budget.ceiling() == 50_000, 'what is left, not the per-agent ceiling'
    budget.record(*spent(label='core-1#2', tokens=90_000))
    assert budget.ceiling() == 0
    assert budget.by_agent() == {'core-1': 290_000}, 'a retry folds into the slot it retried'
    assert [row['attempt'] for row in budget.rows] == [1, 2]
    assert budget.failures() == []
    # Either ceiling alone still bounds an agent.
    assert orchestrator.Budget(per_agent=5).ceiling() == 5
    assert orchestrator.Budget(per_run=5).ceiling() == 5


def test_a_run_out_of_time_tokens_or_credit_says_which_before_it_starts_another():
    clock = ticking()
    budget = orchestrator.Budget(per_agent=10, per_run=1000, wall=60, clock=clock)
    assert budget.exhausted() == ''
    clock.held[0] = 61
    assert 'ran out of time' in budget.exhausted() and '60 second(s)' in budget.exhausted()

    tokens = orchestrator.Budget(per_agent=10, per_run=1000)
    tokens.record(*spent(tokens=1000))
    assert 'reached its token ceiling of 1,000' in tokens.exhausted()

    limited = orchestrator.Budget(per_agent=10, per_run=10_000)
    task, outcome = spent()
    outcome.record = {'rate_limits': ['the seven_day rate-limit window is at 99%']}
    limited.record(task, outcome)
    assert limited.limits == ['the seven_day rate-limit window is at 99%']
    assert 'at 99%, so the stages left were not started' in limited.exhausted()


def test_the_recorder_is_a_row_in_this_process_not_an_agent_that_ran():
    """The slot still exists; no model runs it, and the row says so.

    A row carrying the model the recorder was once planned with would read as
    an agent that ran, and the usage report would price a session nobody
    started.
    """
    budget = orchestrator.Budget(per_agent=10_000, per_run=100_000)
    budget.record(*spent(label='core-1', tokens=4000))
    budget.record(*spent(label='reader', agent='reader', tokens=6000))
    rows = starter.agent_rows(budget, Panel(budget, calls=12))
    assert [row['label'] for row in rows] == ['core-1', 'reader', 'recorder'], 'sorted, then us'
    recorder = rows[-1]
    assert recorder['model'] is None and recorder['tokens'] == 0
    assert recorder['ending'] == starter.IN_PROCESS
    assert recorder['state'] == 'done', 'the word every reader of this file already knows'
    assert recorder['toolCalls'] == 12, 'every recorder tool was called in this process'
    assert 'with no model' in recorder['detail']
    reviewer = rows[0]
    assert (reviewer['agentType'], reviewer['tokens']) == ('core-reviewer', 4000)
    assert reviewer['state'] == 'done' and reviewer['ending'] == agent_runner.ANSWERED
    assert starter.agent_rows(budget) == rows[:-1], 'no panel, no recorder row'


def test_finish_writes_the_runs_own_accounting_and_one_receipt_per_attempt(tmp_path,
                                                                          monkeypatch):
    """``runtime.json`` and the usage files are written from the budget alone.

    A run whose provenance says ``null`` cannot be reproduced, so the version
    is asked of the binary and the environment is only a fallback for a caller
    that knows better.
    """
    monkeypatch.setattr(starter, 'claude_version', lambda binary=None: '2.1.322')
    budget = orchestrator.Budget(per_agent=10_000, per_run=100_000)
    budget.record(*spent(label='core-1', tokens=4000))
    budget.record(*spent(label='core-1#2', tokens=1500, ending=agent_runner.SILENT))
    panel = Panel(budget, calls=9, notes=['the citations lane was not run'])
    (tmp_path / 'progress.json').write_text(json.dumps({'status': 'reviewing'}),
                                            encoding='utf-8')

    starter.finish(tmp_path, 'blocked', 'because', budget, 12.0, result={'status': 'blocked'},
                   panel=panel, runner=agent_runner.ScriptedRunner())

    runtime = json.loads((tmp_path / 'runtime.json').read_text(encoding='utf-8'))
    assert runtime['status'] == 'blocked' and runtime['blocked_reason'] == 'because'
    assert runtime['wall_seconds'] == 12.0 and runtime['tokens_total'] == 5500
    assert runtime['claude_code_version'] == '2.1.322'
    assert runtime['runner'] == 'scripted'
    assert runtime['per_agent_token_ceiling'] == 10_000
    assert runtime['per_run_token_ceiling'] == 100_000
    assert runtime['notes'] == ['the citations lane was not run']
    assert runtime['result'] == {'status': 'blocked'}
    assert [row['label'] for row in runtime['agents']] == ['core-1', 'core-1#2', 'recorder']

    receipts = (tmp_path / 'usage' / 'core-1' / 'usage.jsonl').read_text(encoding='utf-8')
    rows = [json.loads(line) for line in receipts.splitlines()]
    assert [row['attempt'] for row in rows] == [1, 2]
    assert [row['observed_tokens'] for row in rows] == [4000, 1500]
    assert rows[1]['state'] == agent_runner.SILENT
    assert (tmp_path / 'usage' / 'core-1' / 'core-1-1.started.json').is_file()
    assert (tmp_path / 'usage' / 'core-1' / 'core-1-2.started.json').is_file()
    recorder = json.loads((tmp_path / 'usage' / 'recorder' / 'usage.jsonl')
                          .read_text(encoding='utf-8').splitlines()[0])
    assert (recorder['model'], recorder['observed_tokens']) == (None, 0)
    assert recorder['state'] == starter.IN_PROCESS

    # A blocked run says so in the file the server was writing, too.
    patched = json.loads((tmp_path / 'progress.json').read_text(encoding='utf-8'))
    assert patched['status'] == 'blocked' and patched['error'] == 'because'

    monkeypatch.setattr(starter, 'claude_version', lambda binary=None: None)
    monkeypatch.setenv('PEERREVIEW_CLAUDE_VERSION', '2.1.999')
    starter.finish(tmp_path, 'blocked', 'because', budget, 1.0)
    again = json.loads((tmp_path / 'runtime.json').read_text(encoding='utf-8'))
    assert again['claude_code_version'] == '2.1.999'
    assert again['runner'] is None and again['notes'] == []


def test_a_finished_run_is_not_patched_to_blocked(tmp_path, monkeypatch):
    monkeypatch.setattr(starter, 'claude_version', lambda binary=None: None)
    budget = orchestrator.Budget(per_agent=10, per_run=10)
    (tmp_path / 'progress.json').write_text(json.dumps({'status': 'complete'}), encoding='utf-8')
    starter.finish(tmp_path, 'blocked', 'late rate limit', budget, 1.0)
    assert json.loads(
        (tmp_path / 'progress.json').read_text(encoding='utf-8'))['status'] == 'complete'


def test_a_half_written_progress_file_is_not_an_answer_about_the_run(tmp_path):
    """The server rewrites that file while the driver is reading it.

    A truncated read is nothing the run can be judged on, so it reads as
    nothing and the next look gets the whole of it.
    """
    progress = tmp_path / 'progress.json'
    progress.write_text('{"status": "comp', encoding='utf-8')
    assert starter.recorded_progress(tmp_path) == {}
    progress.write_text('[]', encoding='utf-8')
    assert starter.recorded_progress(tmp_path) == {}, 'nor is a file of the wrong shape'
    assert starter.recorded_progress(tmp_path / 'nowhere') == {}
    progress.write_text(json.dumps({'status': 'complete'}), encoding='utf-8')
    assert starter.recorded_progress(tmp_path) == {'status': 'complete'}


def test_a_retry_in_the_same_folder_does_not_answer_with_the_last_attempt(tmp_path):
    """Until the new run writes, the old run's files are the only ones there."""
    (tmp_path / 'runtime.json').write_text(json.dumps(
        {'status': 'blocked', 'blocked_reason': 'the editor answered nothing twice',
         'arm': 'single', 'reviewers': 1}), encoding='utf-8')
    (tmp_path / 'progress.json').write_text(json.dumps(
        {'status': 'blocked', 'error': 'the editor answered nothing twice'}), encoding='utf-8')

    marked = starter.supersede(tmp_path)

    assert marked == {'runtime.json': 'blocked', 'progress.json': 'blocked'}
    runtime = json.loads((tmp_path / 'runtime.json').read_text(encoding='utf-8'))
    assert runtime['status'] == 'running' and runtime['blocked_reason'] is None
    assert runtime['arm'] == 'single' and runtime['reviewers'] == 1
    assert 'the attempt before it ended blocked: the editor answered nothing twice' in \
        runtime['note']
    assert runtime['superseded']['blocked_reason'] == 'the editor answered nothing twice'
    progress = json.loads((tmp_path / 'progress.json').read_text(encoding='utf-8'))
    assert progress['status'] == 'running' and progress['error'] is None

    # A run already marked running is left alone, and so is a folder with
    # nothing in it; an unreadable file is still marked.
    assert starter.supersede(tmp_path) == {}
    assert starter.supersede(tmp_path / 'nowhere') == {}
    (tmp_path / 'runtime.json').write_text('{"status": "blo', encoding='utf-8')
    assert starter.supersede(tmp_path) == {'runtime.json': None}
    assert json.loads((tmp_path / 'runtime.json').read_text(
        encoding='utf-8'))['superseded']['unreadable'] is True


def test_the_reports_are_rendered_again_only_once_there_is_a_run_to_report(tmp_path):
    """``write_review`` writes REVIEW.md before any accounting exists on disk."""
    assert starter.rerender(tmp_path) is None, 'no protocol, no run, nothing to render'
    (tmp_path / 'protocol.json').write_text('{"arm": "single"}', encoding='utf-8')
    said = starter.rerender(tmp_path)
    assert isinstance(said, dict) and said, 'a render that failed still answers'


# -- the models -------------------------------------------------------------


def test_the_flags_the_user_typed_reach_the_run(monkeypatch):
    seen = {}

    def fake_review(pdf, out, **options):
        seen.update(options)
        return {'status': 'complete', 'stage': 'session'}

    monkeypatch.setattr(starter, 'review', fake_review)
    assert starter.review_main(['paper.pdf', 'out', '--reviewer-models', 'opus,haiku',
                                '--specialist-model', 'haiku',
                                '--editor-model', 'fable',
                                '--recorder-model', 'sonnet']) == 0
    assert seen['reviewer_models'] == 'opus,haiku'
    assert seen['specialist_model'] == 'haiku'
    assert seen['editor_model'] == 'fable'
    assert seen['recorder_model'] == 'sonnet', 'the recorder is a slot in the plan'

    assert starter.review_main(['paper.pdf', 'out']) == 0
    assert seen['reviewer_models'] is None, 'unset is the default, not an empty list'


def test_a_model_this_login_cannot_run_stops_the_run_before_it_costs_anything(tmp_path,
                                                                              no_allowlist):
    with pytest.raises(ValueError) as refused:
        starter.review(str(FIXTURE), str(tmp_path / 'run'), arm='single',
                       reviewer_models='claude-nonesuch-9')
    assert 'claude-nonesuch-9' in str(refused.value)
    assert '--reviewer-models' in str(refused.value)
    assert not (tmp_path / 'run').exists(), 'no run folder, no usage, nothing spent'


def test_the_runner_owns_the_question_and_a_missing_runner_is_not_a_refusal():
    """A model this login cannot run is a bad argument; no harness is not.

    Nothing is wrong with what the caller asked for when Claude Code is not
    installed, so that run is opened, identified and marked blocked at start-up
    like any other run that could not begin.
    """
    class Asked(agent_runner.Runner):
        def __init__(self, nothing_runs='', refused=()):
            self.nothing_runs = nothing_runs
            self.refused = refused
            self.asked = []

        def unavailable(self, models):
            self.asked.append(dict(models))
            if not models:
                return [self.nothing_runs] if self.nothing_runs else []
            return list(self.refused)

    picky = Asked(refused=['claude-nonesuch-9 is not a model this package knows'])
    with pytest.raises(ValueError) as refused:
        starter.refuse_models(picky, {'core-1': 'claude-nonesuch-9'})
    assert 'claude-nonesuch-9' in str(refused.value)
    assert picky.asked == [{}, {'core-1': 'claude-nonesuch-9'}], \
        'asked with no models first, because that question is not about the plan'
    assert starter.runner_missing(picky) == ''

    missing = Asked(nothing_runs=claude_code.MISSING_CLAUDE,
                    refused=['claude-opus-5-5 cannot be run'])
    assert starter.refuse_models(missing, {'core-1': 'claude-opus-5-5'}) is None
    assert missing.asked == [{}], 'a runner that runs nothing is not asked about the plan'
    assert starter.runner_missing(missing) == claude_code.MISSING_CLAUDE

    healthy = Asked()
    assert starter.refuse_models(healthy, {'core-1': 'claude-opus-5-5'}) is None
    assert starter.runner_missing(healthy) == ''


def test_the_runner_is_claude_code_unless_a_caller_brings_its_own():
    assert isinstance(starter.runner_for(), claude_code.ClaudeCodeRunner)
    scripted = agent_runner.ScriptedRunner()
    assert starter.runner_for(scripted) is scripted
    assert scripted.remote is False, 'its agents are in this process; no server on the wire'


def test_the_models_asked_for_are_recorded_even_when_no_session_started(tmp_path, monkeypatch,
                                                                       no_allowlist):
    monkeypatch.setenv('PATH', str(tmp_path / 'empty'))
    starter.VERSIONS.clear()
    out = tmp_path / 'run'
    summary = starter.review(str(FIXTURE), str(out), reviewers=2)
    assert summary['models_requested']['core-2'] == 'claude-opus-5-5'
    protocol = json.loads((out / 'review' / 'protocol.json').read_text(encoding='utf-8'))
    assert protocol['models']['core-2'] == 'claude-opus-5-5'
    assert protocol['models']['editor'] == 'claude-fable-5-2'
    assert 'claude-opus-5-5' in protocol['model']
    assert protocol['reviewer_models'] == list(starter.model_plan.DEFAULT_REVIEWER_MODELS), \
        'the ordered list, not only the per-slot mapping it was drawn from'
    assert summary['models'] == [], 'nothing ran, so no model reported running'


# -- what the run leaves in the environment ---------------------------------


def test_the_deadline_and_the_version_are_put_where_the_tools_look(monkeypatch):
    """Both are read by code that cannot see the run: in-process now, so set here."""
    monkeypatch.delenv('PEERREVIEW_RUN_DEADLINE', raising=False)
    monkeypatch.delenv('PEERREVIEW_CLAUDE_VERSION', raising=False)
    monkeypatch.setattr(starter, 'claude_version', lambda binary=None: '2.1.400')
    assert starter.settle_run(600) == '2.1.400'
    left = float(os.environ['PEERREVIEW_RUN_DEADLINE']) - starter.time.time()
    assert 590 < left <= 601, 'the deadline is this many seconds from the moment it was set'
    assert os.environ['PEERREVIEW_CLAUDE_VERSION'] == '2.1.400'

    monkeypatch.setattr(starter, 'claude_version', lambda binary=None: None)
    monkeypatch.delenv('PEERREVIEW_CLAUDE_VERSION', raising=False)
    assert starter.settle_run(10) is None
    assert 'PEERREVIEW_CLAUDE_VERSION' not in os.environ, 'nothing is recorded as a version'


def test_the_exit_code_says_whether_anything_was_spent():
    assert starter.exit_code({'status': 'complete', 'stage': 'session'}) == 0
    assert starter.exit_code({'status': 'blocked', 'stage': 'startup'}) == 2
    assert starter.exit_code({'status': 'blocked', 'stage': 'session'}) == 1
    assert starter.exit_code({}) == 1


def test_nothing_in_the_starter_needs_codex_or_a_key():
    """The key and the subscription login are the runner's business now."""
    source = Path(starter.__file__).read_text(encoding='utf-8')
    assert 'codex' not in source.lower()
    assert 'ANTHROPIC_API_KEY' not in source
    assert 'ANTHROPIC_AUTH_TOKEN' not in source
    assert os.environ.get('PEERREVIEW_PLUGIN_RUN') is None
