"""The starter: the probe, the stream accounting and the blocked outcomes.

Every test here runs offline. One of them puts a stub ``claude`` on PATH that
replays a canned stream; the rest run with nothing on PATH at all, which is
also the check that a missing provider is a blocked run rather than a
traceback. No test can reach Codex or an API key: the stub records the argv it
was called with, and PATH holds nothing else.
"""

from __future__ import annotations

import json
import os
import re
import stat
import sys
import time
from pathlib import Path

import pytest

from peerreviewagents.cli.main import review_argv
from peerreviewagents.engine import citation_ledger as cl
from peerreviewagents.plugin import starter

FIXTURE = Path(__file__).resolve().parent / 'fixtures' / 'single-column.pdf'

STUB = '''import json, os, re, sys

argv = sys.argv[1:]
if argv == ['--version']:
    print('2.1.999 (Claude Code stub)', flush=True)
    sys.exit(0)
log = os.environ['PEERREVIEW_STUB_LOG']
with open(log, 'a', encoding='utf-8') as stream:
    stream.write(json.dumps(argv) + '\\n')
prompt = argv[argv.index('-p') + 1] if '-p' in argv else ''
print(json.dumps({
    'type': 'system', 'subtype': 'init',
    'plugins': [{'name': 'pra', 'path': '/plugins/pra'}],
    'mcp_servers': [{'name': 'plugin:pra:peerreview', 'status': 'connected'}],
    'agents': ['pra:core-reviewer', 'pra:editor', 'pra:recorder'],
    'tools': json.loads(os.environ['PEERREVIEW_STUB_TOOLS']),
    'api_key_seen': 'ANTHROPIC_API_KEY' in os.environ,
}), flush=True)
if prompt.startswith('Reply with'):
    print(json.dumps({'type': 'result', 'subtype': 'success', 'result': 'ready'}), flush=True)
    sys.exit(0)
print(json.dumps({'type': 'result', 'subtype': 'success',
                  'result': 'the workflow is still running'}), flush=True)
print(json.dumps({'type': 'system', 'subtype': 'task_progress', 'workflow_progress': [
    {'label': 'core-1', 'tokens': 120000, 'toolCalls': 18, 'state': 'completed',
     'attempt': 1, 'agentType': 'pra:core-reviewer', 'model': 'claude-sonnet-5-5'},
    {'label': 'write review', 'tokens': 4000, 'toolCalls': 2, 'state': 'completed',
     'attempt': 1, 'agentType': 'pra:recorder', 'model': 'claude-haiku-5-5'}]}), flush=True)
print(json.dumps({'type': 'system', 'subtype': 'task_notification',
                  'task': {'status': 'completed'}}), flush=True)
# What the server would have written, written from inside the session: the
# starter has to read the real file rather than be handed one by the test.
wanted = os.environ.get('PEERREVIEW_STUB_PROGRESS') or ''
found = re.search(r'run folder to (.+?)[,.](?:\\s|$)', prompt)
if wanted and found:
    review = os.path.join(found.group(1), 'review')
    os.makedirs(review, exist_ok=True)
    with open(os.path.join(review, 'progress.json'), 'w', encoding='utf-8') as stream:
        stream.write(wanted)
print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False, 'num_turns': 7,
                  'session_id': 'stub', 'result': {'status': 'complete'}}), flush=True)
'''


RACE_STUB = '''import json, os, re, sys, time

argv = sys.argv[1:]
if argv == ['--version']:
    print('2.1.999 (Claude Code stub)', flush=True)
    sys.exit(0)
log = os.environ['PEERREVIEW_STUB_LOG']
with open(log, 'a', encoding='utf-8') as stream:
    stream.write(json.dumps(argv) + '\\n')
prompt = argv[argv.index('-p') + 1] if '-p' in argv else ''
print(json.dumps({
    'type': 'system', 'subtype': 'init',
    'plugins': [{'name': 'pra', 'path': '/plugins/pra'}],
    'mcp_servers': [{'name': 'plugin:pra:peerreview', 'status': 'connected'}],
    'agents': ['pra:core-reviewer', 'pra:editor', 'pra:recorder'],
    'tools': json.loads(os.environ['PEERREVIEW_STUB_TOOLS']),
}), flush=True)
if prompt.startswith('Reply with'):
    print(json.dumps({'type': 'result', 'subtype': 'success', 'result': 'ready'}), flush=True)
    sys.exit(0)
# A few turns, the last of which says the review is running in the background.
# Then the turn ends: no task notification ever arrives, and the event stream
# closes with the workflow alive. This is the shape that killed a live review.
print(json.dumps({'type': 'system', 'subtype': 'task_progress', 'workflow_progress': [
    {'label': 'core-1', 'tokens': 120000, 'toolCalls': 18, 'state': 'completed',
     'attempt': 1, 'agentType': 'pra:core-reviewer', 'model': 'claude-sonnet-5-5'}]}), flush=True)
print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False, 'num_turns': 7,
                  'session_id': 'stub',
                  'result': 'The review workflow is running in the background, no result yet.'}),
      flush=True)
sys.stdout.flush()
os.close(1)
# The workflow carries on without the turn that launched it. With a progress
# file to write it finishes; with none it never does.
time.sleep(float(os.environ.get('PEERREVIEW_STUB_DELAY') or 1.5))
wanted = os.environ.get('PEERREVIEW_STUB_PROGRESS') or ''
found = re.search(r'run folder to (.+?)[,.](?:\\s|$)', prompt)
if wanted and found:
    review = os.path.join(found.group(1), 'review')
    os.makedirs(review, exist_ok=True)
    half = os.path.join(review, 'progress.json.part')
    with open(half, 'w', encoding='utf-8') as stream:
        stream.write(wanted)
    os.replace(half, os.path.join(review, 'progress.json'))
# This session does not exit on its own: the driver has to end it, and only
# once the review is written or nothing is coming.
time.sleep(600)
'''


def install_stub(tmp_path, monkeypatch, progress=None, script=None):
    """A fake ``claude`` on PATH, and nothing else: no codex, no real provider.

    ``progress`` is what the stub session writes to ``review/progress.json``,
    the way the server would: no test pre-writes that file, because then the
    starter's reading of it is never exercised. ``script`` is which stub
    session runs — :data:`STUB` by default, or :data:`RACE_STUB` for the
    session that ends its turn with the workflow still going.
    """
    binaries = tmp_path / 'bin'
    binaries.mkdir()
    body = script or STUB
    if os.name == 'nt':
        # A shebang means nothing on Windows. A real install puts `claude.cmd`
        # on PATH and the shell finds it through PATHEXT, so the stub does too.
        script_file = binaries / 'stub.py'
        script_file.write_text(body, encoding='utf-8')
        (binaries / 'claude.cmd').write_text(
            f'@"{sys.executable}" "{script_file}" %*\n', encoding='utf-8')
    else:
        stub = binaries / 'claude'
        stub.write_text(f'#!{sys.executable}\n{body}', encoding='utf-8')
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    log = tmp_path / 'argv.jsonl'
    monkeypatch.setenv('PATH', str(binaries))
    monkeypatch.setenv('PEERREVIEW_STUB_LOG', str(log))
    monkeypatch.setenv('PEERREVIEW_STUB_TOOLS', json.dumps(list(starter.tool_names())))
    monkeypatch.setenv('PEERREVIEW_STUB_PROGRESS', json.dumps(progress) if progress else '')
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'must-not-be-used')
    starter.VERSIONS.clear()  # each stub is a new binary path, not the cached one
    return log


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



# -- the launch -------------------------------------------------------------


def test_the_allow_list_names_every_server_tool_and_nothing_else():
    allowed = starter.allow_list().split(',')
    assert allowed[:3] == ['Workflow', 'Skill', 'TodoWrite']
    assert set(starter.tool_names()) == set(allowed[3:])
    assert all(name.startswith('mcp__plugin_pra_peerreview__') for name in allowed[3:])
    assert 'mcp__plugin_pra_peerreview__submit_editor' in allowed
    assert 'mcp__plugin_pra_peerreview__write_review' in allowed
    for forbidden in ('Read', 'Write', 'Edit', 'Bash', 'WebSearch', 'WebFetch'):
        assert forbidden not in allowed


def test_the_plugin_folder_is_the_one_that_ships():
    directory = starter.plugin_dir()
    assert (directory / '.claude-plugin' / 'plugin.json').is_file()
    assert (directory / '.mcp.json').is_file()
    assert (directory / 'workflows' / 'review.js').is_file()


def test_the_reference_lookups_are_sized_for_a_long_reference_list():
    """Two numbers in two files, and the shorter one used to truncate the list.

    The workflow asked the recorder for twelve lookup calls of twenty
    references; the recorder's agent file allowed it eight turns in all. A
    review with more than a hundred and forty references therefore stopped
    part-way through its reference list, and the only trace was a count.
    """
    directory = starter.plugin_dir()
    workflow = (directory / 'workflows' / 'review.js').read_text(encoding='utf-8')
    numbers = dict(re.findall(r'^const ([A-Z_]+) = (\d+)$', workflow, re.MULTILINE))
    batch, ceiling = int(numbers['LOOKUP_LIMIT']), int(numbers['RESOLVE_CALL_CEILING'])
    recorder = (directory / 'agents' / 'recorder.md').read_text(encoding='utf-8')
    turns = int(re.search(r'^maxTurns: (\d+)$', recorder, re.MULTILINE).group(1))
    assert ceiling < turns, 'the agent file must allow every call the workflow asks for'
    assert (ceiling - 1) * batch >= 400, 'a long review has hundreds of references'
    # The call count is the paper's, and what it cannot reach is said out loud.
    assert 'Math.ceil(referenceCount / LOOKUP_LIMIT)' in workflow
    assert 'reported as never looked up' in workflow
    assert 'reported as never judged' in workflow


def test_the_citation_briefs_cover_every_surface_the_server_offers_them():
    """A read nobody is told about is a read nobody makes.

    The server grew ``kind='supported'`` so an all-supports worker cannot pass
    as a clean reference list, four existence states so a title two indexes
    miss is not read as a fabrication, and an ``untrusted_text`` line on every
    citation read. None of that reaches a model that was never told it exists,
    so the briefs are checked against the server's own constants.
    """
    directory = starter.plugin_dir()
    specialist = (directory / 'agents' / 'citations.md').read_text(encoding='utf-8')
    worker = (directory / 'agents' / 'citation-worker.md').read_text(encoding='utf-8')
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


def test_no_claude_on_path_is_a_blocked_run_not_a_traceback(tmp_path, monkeypatch):
    monkeypatch.setenv('PATH', str(tmp_path / 'empty'))
    out = tmp_path / 'run'
    summary = starter.review(str(FIXTURE), str(out), arm='single')
    assert (summary['status'], summary['stage']) == ('blocked', 'startup')
    assert starter.exit_code(summary) == 2, 'nothing was spent: that is its own exit code'
    startup = json.loads((out / 'review' / 'startup.json').read_text(encoding='utf-8'))
    assert startup['problems']
    assert startup['tools_expected']
    runtime = json.loads((out / 'review' / 'runtime.json').read_text(encoding='utf-8'))
    assert runtime['status'] == 'blocked'
    assert 'probe failed' in runtime['blocked_reason']


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


def test_the_stub_run_is_launched_the_way_the_spec_requires(tmp_path, monkeypatch):
    log = install_stub(tmp_path, monkeypatch, progress={
        'status': 'complete', 'findings_delivered': 4, 'quarantined_findings_count': 0,
        'stages': {'missing': []}})
    out = tmp_path / 'run'

    summary = starter.review(str(FIXTURE), str(out), reviewers=3, arm='full-two-track')
    assert starter.exit_code(summary) == 0
    assert summary['status'] == 'complete'
    assert summary['tokens_total'] == 124000
    assert summary['models'] == ['claude-haiku-5-5', 'claude-sonnet-5-5']
    assert summary['findings_delivered'] == 4

    calls = [json.loads(line) for line in log.read_text(encoding='utf-8').splitlines()]
    assert len(calls) == 2, 'the probe runs first, then the review'
    for call in calls:
        assert '--bare' not in call
        assert call[call.index('--permission-mode') + 1] == 'dontAsk'
        assert call[call.index('--allowed-tools') + 1] == starter.allow_list()
        assert call[call.index('--output-format') + 1] == 'stream-json'
        assert call[call.index('--plugin-dir') + 1] == str(starter.plugin_dir())
        assert 'codex' not in ' '.join(call)
    assert calls[0][calls[0].index('-p') + 1].startswith('Reply with')
    assert 'peer-review-manuscript' in calls[1][calls[1].index('-p') + 1]

    runtime = json.loads((out / 'review' / 'runtime.json').read_text(encoding='utf-8'))
    assert runtime['status'] == 'complete'
    assert runtime['blocked_reason'] is None
    assert runtime['claude_code_version'] == '2.1.999 (Claude Code stub)', \
        'provenance records the harness that actually ran, asked of the binary'
    assert runtime['tokens_total'] == 124000
    assert {row['label']: row['model'] for row in runtime['agents']} == {
        'core-1': 'claude-sonnet-5-5', 'write review': 'claude-haiku-5-5'}
    assert runtime['result']['num_turns'] == 7
    usage = (out / 'review' / 'usage' / 'core-1' / 'usage.jsonl').read_text(encoding='utf-8')
    assert json.loads(usage.splitlines()[0])['observed_tokens'] == 120000
    assert (out / 'review' / 'usage' / 'core-1' / 'core-1-1.started.json').is_file()
    assert (out / 'review' / 'usage' / 'write-review').is_dir()


def test_a_complete_run_is_not_claimed_when_the_server_wrote_no_progress(tmp_path, monkeypatch):
    install_stub(tmp_path, monkeypatch)
    out = tmp_path / 'run'
    summary = starter.review(str(FIXTURE), str(out), arm='single')
    assert starter.exit_code(summary) == 1
    runtime = json.loads((out / 'review' / 'runtime.json').read_text(encoding='utf-8'))
    assert runtime['status'] == 'blocked'
    assert runtime['blocked_reason'].startswith('the server wrote no progress file')
    # And why the driver stopped waiting for one: the session is gone, so
    # nothing can write that file now.
    assert 'the session exited' in runtime['blocked_reason']


def test_a_workflow_that_closed_nothing_leaves_the_run_blocked_not_running(tmp_path, monkeypatch):
    """A session can end with every stage still open. That is not a review.

    The server only writes ``complete`` from ``write_review`` and ``blocked``
    from ``mark_blocked``; a workflow that falls through does neither, and a run
    left at ``running`` would read as still in flight forever.
    """
    install_stub(tmp_path, monkeypatch, progress={'status': 'running', 'stages': {'missing': []}})
    out = tmp_path / 'run'
    summary = starter.review(str(FIXTURE), str(out), arm='single')
    assert summary['status'] == 'blocked'
    assert 'without writing the review or marking the run blocked' in summary['blocked_reason']
    assert 'running' in summary['blocked_reason']
    assert starter.exit_code(summary) == 1
    on_disk = json.loads((out / 'review' / 'progress.json').read_text(encoding='utf-8'))
    assert on_disk['status'] == 'blocked' and on_disk['error'] == summary['blocked_reason']


def test_a_session_that_ends_its_turn_does_not_take_the_workflow_with_it(tmp_path, monkeypatch):
    """The pilot's second run, reproduced: the race the driver used to lose.

    The session launches the review as a background workflow, says in its last
    turn that it is running, and stops. Its event stream closes with the
    workflow alive, and the driver used to terminate the process there — which
    killed the workflow, because the workflow runs inside that process. Nine
    stages were reported and the editor was still going.

    What must happen instead: the driver keeps waiting on the folder the server
    writes, the editor package lands, and the run is complete.
    """
    install_stub(tmp_path, monkeypatch, script=RACE_STUB, progress={
        'status': 'complete', 'findings_delivered': 63, 'quarantined_findings_count': 1,
        'stages': {'required': ['editor'], 'reported': ['editor'], 'missing': [],
                   'editor_accepted': True}})
    out = tmp_path / 'run'

    summary = starter.review(str(FIXTURE), str(out), arm='single', wall_timeout=120)

    assert summary['status'] == 'complete', summary['blocked_reason']
    assert summary['blocked_reason'] is None
    assert starter.exit_code(summary) == 0
    assert summary['stages']['missing'] == []
    assert summary['stages']['editor_accepted'] is True, 'the editor package is the review'
    assert summary['findings_delivered'] == 63 and summary['quarantined'] == 1
    # Three events, no task notification, and the stream closed long before the
    # review did. None of those is what the run waited for.
    assert summary['stream_events'] == 3
    assert summary['wall_seconds'] < 60, 'the run folder answered, not the wall clock'
    runtime = json.loads((out / 'review' / 'runtime.json').read_text(encoding='utf-8'))
    assert runtime['status'] == 'complete' and runtime['tokens_total'] == 120000


def test_a_workflow_that_never_finishes_is_blocked_with_the_true_cause(tmp_path, monkeypatch):
    """The other half of waiting: a review that is never coming.

    The same session, with no review to write. Waiting on the folder must not
    mean waiting forever, and the cause recorded must be the one that was hit —
    the wall clock — not a guess about what the workflow was doing.
    """
    install_stub(tmp_path, monkeypatch, script=RACE_STUB)  # and so it writes nothing, ever
    monkeypatch.setenv('PEERREVIEW_STUB_DELAY', '600')
    out = tmp_path / 'run'

    summary = starter.review(str(FIXTURE), str(out), arm='single', wall_timeout=3)

    assert summary['status'] == 'blocked'
    assert summary['blocked_reason'] == 'the session passed its wall-clock limit'
    assert starter.exit_code(summary) == 1
    assert 3 <= summary['wall_seconds'] < 60
    runtime = json.loads((out / 'review' / 'runtime.json').read_text(encoding='utf-8'))
    assert runtime['status'] == 'blocked'
    assert runtime['blocked_reason'] == summary['blocked_reason']
    assert summary.get('findings_delivered') is None, 'there is no review to report'


def test_the_watch_ends_a_run_only_when_nothing_is_writing_the_folder(tmp_path):
    """The states the driver may give up in, and the one it may not.

    A review that is still writing its folder keeps its session, however long
    that session has been silent — an agent can read for minutes between tool
    calls. A folder that has stopped changing is a review that has stopped, and
    how long the driver waits before accepting that depends on what it knows:
    a moment once the session's process is gone, because nothing can write the
    folder then; twenty seconds once the session itself reported the workflow
    task finished; the long window on a closed stream alone.
    """
    accounting = starter.Accounting(10, 10)
    progress = tmp_path / 'progress.json'
    watch = starter.Completion(tmp_path, accounting, tick=0.0)

    def quiet_for(seconds):
        watch.still_since = time.monotonic() - seconds

    assert watch(closed=False, alive=True) is None, 'a talking session is never cut short'
    assert watch(closed=True, alive=True) is None, 'nor is one that has only stopped talking'

    progress.write_text(json.dumps({'status': 'running', 'updated': 'one'}), encoding='utf-8')
    assert watch(True, True) is None, 'a folder that just changed is a review at work'
    quiet_for(60)
    assert watch(True, True) is None, 'sixty seconds of silence is not a dead review'
    assert watch(True, False) == starter.NOTHING_LEFT_WRITING
    assert 'the session exited' in watch.note and '60s' in watch.note

    accounting.finished = True
    quiet_for(5)
    assert watch(True, True) is None, 'the server may still be writing the last word'
    quiet_for(60)
    assert watch(True, True) == starter.NOTHING_LEFT_WRITING
    assert 'reported finishing' in watch.note

    # A folder that is still being written keeps the run, whatever the session
    # is doing and whatever it said.
    progress.write_text(json.dumps({'status': 'running', 'updated': 'two'}), encoding='utf-8')
    assert watch(True, False) is None

    progress.write_text(json.dumps({'status': 'complete'}), encoding='utf-8')
    assert watch(True, True) == starter.REVIEW_WRITTEN
    progress.write_text(json.dumps({'status': 'blocked', 'error': 'said so'}), encoding='utf-8')
    assert watch(False, True) == starter.RUN_MARKED_BLOCKED
    # Both are reasons the loop ended, and neither is a cause of failure.
    assert starter.REVIEW_WRITTEN in starter.SETTLED
    assert starter.RUN_MARKED_BLOCKED in starter.SETTLED


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


def test_a_missing_claude_says_so_rather_than_blaming_the_plugin(tmp_path, monkeypatch):
    monkeypatch.setenv('PATH', str(tmp_path / 'empty'))
    problems, found = starter.probe(starter.plugin_dir(), tmp_path)
    assert problems == [starter.MISSING_CLAUDE]
    assert 'not installed or not on this PATH' in problems[0]
    assert 'server tool(s) absent' not in ' '.join(problems), \
        'absent tools describe a misconfigured plugin, not a missing harness'
    assert found['raw'] is None


def test_neither_the_api_key_nor_the_auth_token_reaches_the_session(tmp_path, monkeypatch):
    seen = {}

    def fake_popen(command, **kwargs):
        seen['env'] = kwargs['env']
        seen['command'] = command
        raise FileNotFoundError('stopping here')

    monkeypatch.setenv('ANTHROPIC_API_KEY', 'must-not-be-used')
    monkeypatch.setenv('ANTHROPIC_AUTH_TOKEN', 'must-not-be-used-either')
    monkeypatch.setattr(starter, 'claude_binary', lambda: '/opt/claude/bin/claude')
    monkeypatch.setattr(starter, 'claude_version', lambda binary=None: '2.1.400')
    monkeypatch.setattr(starter.subprocess, 'Popen', fake_popen)
    events, stop, code = starter.launch('hello', tmp_path, 0.0, lambda event: None, tmp_path)
    assert (events, code) == (0, 127)
    assert 'could not be started' in stop
    assert 'ANTHROPIC_API_KEY' not in seen['env']
    assert 'ANTHROPIC_AUTH_TOKEN' not in seen['env']
    assert seen['command'][0] == '/opt/claude/bin/claude'
    assert seen['env']['PEERREVIEW_PLUGIN_RUN'] == '1'
    assert seen['env']['PEERREVIEW_CLAUDE_VERSION'] == '2.1.400'
    assert seen['env']['PEERREVIEW_MCP_COMMAND'] == sys.executable, \
        'the server has to run on the interpreter that started the review'


def test_the_launch_reads_both_pipes_without_select(tmp_path, monkeypatch):
    """stderr is drained for the whole run, and stdout is not polled.

    ``select`` cannot wait on a pipe on Windows, and stderr read only at the end
    fills its pipe and wedges the session that is writing to it.
    """
    source = Path(starter.__file__).read_text(encoding='utf-8')
    assert 'import select' not in source and 'select.select' not in source
    assert 'threading.Thread' in source

    binaries = tmp_path / 'bin'
    binaries.mkdir()
    noisy = ('import sys\n'
             'if sys.argv[1:] == ["--version"]:\n'
             '    print("0.0.0-noisy"); sys.exit(0)\n'
             'for index in range(4000):\n'
             '    sys.stderr.write("plugin diagnostic line %d\\n" % index)\n'
             'print(\'{"type": "result", "subtype": "success"}\', flush=True)\n')
    if os.name == 'nt':
        (binaries / 'noisy.py').write_text(noisy, encoding='utf-8')
        (binaries / 'claude.cmd').write_text(
            f'@"{sys.executable}" "{binaries / "noisy.py"}" %*\n', encoding='utf-8')
    else:
        stub = binaries / 'claude'
        stub.write_text(f'#!{sys.executable}\n{noisy}', encoding='utf-8')
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv('PATH', str(binaries))
    starter.VERSIONS.clear()
    seen, stop, code = starter.launch('hello', tmp_path, time.monotonic() + 60,
                                      lambda event: None, tmp_path)
    assert (seen, stop) == (1, None), 'the event arrived although stderr filled first'
    assert code == 0


# -- the accounting ---------------------------------------------------------


def progress(rows):
    return {'type': 'system', 'subtype': 'task_progress', 'workflow_progress': rows}


def test_the_per_agent_token_ceiling_stops_the_run():
    accounting = starter.Accounting(1000, 1_000_000)
    assert accounting.on_event(progress([{'label': 'core-1', 'tokens': 900}])) is None
    stop = accounting.on_event(progress([{'label': 'core-1', 'tokens': 1200}]))
    assert 'per-agent token ceiling' in stop


def test_the_per_run_token_ceiling_stops_the_run():
    accounting = starter.Accounting(1_000_000, 1500)
    stop = accounting.on_event(progress([{'label': 'core-1', 'tokens': 800},
                                         {'label': 'core-2', 'tokens': 800}]))
    assert 'run passed its token ceiling' in stop


def test_a_retry_is_accounted_separately_from_its_first_attempt():
    accounting = starter.Accounting(1_000_000, 1_000_000)
    accounting.on_event(progress([{'label': 'core-1', 'tokens': 100, 'attempt': 1},
                                  {'label': 'core-1', 'tokens': 200, 'attempt': 2}]))
    assert sorted(accounting.agents) == ['core-1', 'core-1#2']
    assert accounting.total_tokens() == 300


def test_an_unnamed_placeholder_row_is_not_an_agent():
    """The stream emits rows for agents it has not named yet; they carry no usage.

    Inventing a label for one puts an agent in the run record that never ran,
    and the report then lists five empty `agent-N` rows beside the real slots.
    """
    accounting = starter.Accounting(1_000_000, 1_000_000)
    accounting.on_event(progress([{'state': 'pending'}, {'tokens': 0},
                                  {'label': 'core-1', 'tokens': 90}]))
    assert sorted(accounting.agents) == ['core-1']
    assert accounting.total_tokens() == 90


def test_progress_carried_under_the_task_key_is_still_read():
    accounting = starter.Accounting(1_000_000, 1_000_000)
    accounting.on_event({'type': 'system', 'subtype': 'task_progress',
                         'task': {'workflow_progress': [{'label': 'stats', 'tokens': 42}]}})
    assert accounting.total_tokens() == 42


def test_the_first_result_is_not_the_answer():
    """No ``result`` event ends the run, and the second one only ends the wait.

    The first arrives while the review is still running. The one after the task
    notification is the session's own account of the workflow, so it is
    recorded as ``finished`` — which shortens how long the driver waits on the
    run folder — and still returns nothing, because what ends a run is the
    folder the server writes.
    """
    accounting = starter.Accounting(1_000_000, 1_000_000)
    assert accounting.on_event({'type': 'result', 'result': 'still running'}) is None
    assert accounting.finished is False
    assert accounting.on_event({'type': 'system', 'subtype': 'task_notification'}) is None
    assert accounting.on_event({'type': 'result', 'result': 'done'}) is None
    assert accounting.finished is True
    assert accounting.last_result()['result'] == 'done'


def test_a_run_with_no_review_names_the_agent_that_ended_in_error():
    """The stream usually knows which stage ended the run. Say which.

    "The workflow ended without writing the review" is true of every run that
    falls through, and on the pilot's second paper the stream had already
    reported the editor in state ``error`` with its tokens — the one fact that
    says where to look.
    """
    accounting = starter.Accounting(1_000_000, 1_000_000)
    assert accounting.failures() is None
    accounting.on_event({'type': 'system', 'subtype': 'task_progress', 'workflow_progress': [
        {'label': 'core-1', 'tokens': 120000, 'state': 'completed'},
        {'label': 'editor', 'tokens': 136755, 'state': 'error'}]})
    assert accounting.failures() == 'editor ended in state error after 136,755 tokens'


# The event a real stream sends, captured from a 0.8 run of this repo's own
# runtime tests. A subscription with no overage credit reports `rejected`
# overage on every call it serves perfectly well, so this one is healthy.
HEALTHY_RATE_LIMIT = {
    'type': 'rate_limit_event',
    'rate_limit_info': {'status': 'allowed', 'overageStatus': 'rejected',
                        'overageDisabledReason': 'member_zero_credit_limit',
                        'isUsingOverage': False},
}


def test_the_rate_limit_event_a_real_stream_sends_does_not_block_the_run():
    accounting = starter.Accounting(1_000_000, 1_000_000)
    assert accounting.on_event(HEALTHY_RATE_LIMIT) is None, \
        'a rejected overage status is the normal state of a plan without credit'
    assert accounting.rate_limits == []


def test_a_rate_limit_status_that_is_not_allowed_blocks_the_run():
    accounting = starter.Accounting(1_000_000, 1_000_000)
    event = {'type': 'rate_limit_event',
             'rate_limit_info': dict(HEALTHY_RATE_LIMIT['rate_limit_info'],
                                     status='rejected',
                                     overageDisabledReason='member_zero_credit_limit')}
    stop = accounting.on_event(event)
    assert 'status rejected' in stop and 'member_zero_credit_limit' in stop
    assert accounting.rate_limits


def test_a_window_at_the_ceiling_blocks_the_run_and_one_below_it_does_not():
    accounting = starter.Accounting(1_000_000, 1_000_000)
    below = {'type': 'rate_limit_event', 'rate_limit_info': {
        'status': 'allowed', 'windows': {'seven_day': {'utilization': 0.5}}}}
    assert accounting.on_event(below) is None
    at = {'type': 'rate_limit_event', 'rate_limit_info': {
        'status': 'allowed', 'windows': {'five_hour': {'utilization': 0.2},
                                         'seven_day': {'utilization': 0.994}}}}
    assert 'rate-limit window is at 99%' in accounting.on_event(at)
    assert len(accounting.rate_limits) == 1


def test_an_ordinary_event_is_not_a_rate_limit():
    assert starter.rate_limit_reason(
        {'type': 'assistant', 'message': 'the paper exceeded expectations'}) is None
    accounting = starter.Accounting(1_000_000, 1_000_000)
    assert accounting.on_event(progress([{'label': 'core-1', 'tokens': 10}])) is None


# -- the models -------------------------------------------------------------


@pytest.fixture
def no_allowlist(monkeypatch):
    """No ``availableModels`` anywhere, which is the ordinary login."""
    monkeypatch.setattr(starter.model_plan, 'allowlist', lambda **kwargs: None)


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
    assert seen['recorder_model'] == 'sonnet', 'the recorder is a slot like any other'

    assert starter.review_main(['paper.pdf', 'out']) == 0
    assert seen['reviewer_models'] is None, 'unset is the default, not an empty list'


def test_the_models_are_carried_into_the_session_that_runs_the_workflow(tmp_path, monkeypatch,
                                                                       no_allowlist):
    """The workflow passes no model arguments, so the environment has to say."""
    seen = {}

    def fake_popen(command, **kwargs):
        seen['env'] = kwargs['env']
        raise FileNotFoundError('stopping here')

    monkeypatch.setattr(starter, 'claude_binary', lambda: '/opt/claude/bin/claude')
    monkeypatch.setattr(starter, 'claude_version', lambda binary=None: '2.1.400')
    monkeypatch.setattr(starter, 'probe', lambda directory, cwd: ([], {'raw': None}))
    monkeypatch.setattr(starter.subprocess, 'Popen', fake_popen)
    starter.review(str(FIXTURE), str(tmp_path / 'run'), arm='single',
                   reviewer_models='haiku', editor_model='opus')
    assert seen['env']['PEERREVIEW_REVIEWER_MODELS'] == 'claude-haiku-5-5'
    assert seen['env']['PEERREVIEW_EDITOR_MODEL'] == 'claude-opus-5-5'
    assert 'PEERREVIEW_SPECIALIST_MODEL' not in seen['env'], \
        'nothing is passed on for a slot nobody chose'


def test_a_model_this_login_cannot_run_stops_the_run_before_it_costs_anything(tmp_path,
                                                                              no_allowlist):
    with pytest.raises(ValueError) as refused:
        starter.review(str(FIXTURE), str(tmp_path / 'run'), arm='single',
                       reviewer_models='claude-nonesuch-9')
    assert 'claude-nonesuch-9' in str(refused.value)
    assert '--reviewer-models' in str(refused.value)
    assert not (tmp_path / 'run').exists(), 'no probe, no folder, no usage'


def test_the_models_asked_for_are_recorded_even_when_no_session_started(tmp_path, monkeypatch,
                                                                        no_allowlist):
    monkeypatch.setenv('PATH', str(tmp_path / 'empty'))
    out = tmp_path / 'run'
    summary = starter.review(str(FIXTURE), str(out), reviewers=2)
    assert summary['models_requested']['core-2'] == 'claude-opus-5-5'
    protocol = json.loads((out / 'review' / 'protocol.json').read_text(encoding='utf-8'))
    assert protocol['models']['core-2'] == 'claude-opus-5-5'
    assert protocol['models']['editor'] == 'claude-fable-5-2'
    assert 'claude-opus-5-5' in protocol['model']
    assert protocol['reviewer_models'] == list(starter.model_plan.DEFAULT_REVIEWER_MODELS), \
        'the ordered list, not only the per-slot mapping it was drawn from'


def test_a_subagent_variable_that_outranks_the_plan_stops_the_run(tmp_path, monkeypatch,
                                                                  no_allowlist):
    """Below 2.1.251 the variable beats the per-spawn override, silently.

    Every slot would then run whatever it names while the run records the plan,
    which is the one disagreement no later reading can repair. The variable is
    the user's, so it is refused rather than unset, and a Claude Code that
    ranks it correctly is left alone.
    """
    monkeypatch.setenv('CLAUDE_CODE_SUBAGENT_MODEL', 'haiku')
    monkeypatch.setattr(starter, 'claude_version', lambda binary=None: '2.1.250')
    with pytest.raises(ValueError) as refused:
        starter.review(str(FIXTURE), str(tmp_path / 'run'), arm='single')
    assert 'CLAUDE_CODE_SUBAGENT_MODEL' in str(refused.value)
    assert os.environ['CLAUDE_CODE_SUBAGENT_MODEL'] == 'haiku', 'refused, never unset'
    assert not (tmp_path / 'run').exists(), 'nothing written, nothing spent'

    monkeypatch.setattr(starter, 'claude_version', lambda binary=None: None)
    with pytest.raises(ValueError) as unreadable:
        starter.review(str(FIXTURE), str(tmp_path / 'run'), arm='single')
    assert 'could not be read' in str(unreadable.value), \
        'a version nobody could read cannot say the plan will hold'

    monkeypatch.setattr(starter, 'claude_version', lambda binary=None: '2.1.251')
    monkeypatch.setattr(starter, 'claude_binary', lambda: None)
    summary = starter.review(str(FIXTURE), str(tmp_path / 'run'), arm='single')
    assert summary['status'] == 'blocked' and summary['stage'] == 'startup', \
        'a fixed Claude Code gets as far as the probe'


def test_the_stub_run_records_what_was_asked_for_beside_what_ran(tmp_path, monkeypatch,
                                                                 no_allowlist):
    install_stub(tmp_path, monkeypatch, progress={
        'status': 'complete', 'findings_delivered': 1, 'stages': {'missing': []}})
    out = tmp_path / 'run'
    summary = starter.review(str(FIXTURE), str(out), reviewers=3, arm='reviewers-only')
    assert summary['models_requested'] == {
        'core-1': 'claude-sonnet-5-5', 'core-2': 'claude-opus-5-5',
        'core-3': 'claude-haiku-5-5', 'editor': 'claude-fable-5-2',
        'recorder': 'claude-haiku-5-5'}
    assert summary['models'] == ['claude-haiku-5-5', 'claude-sonnet-5-5'], \
        'what the stub session reported, which is not what was asked for'


def test_the_prompt_tells_the_session_the_models_are_already_set():
    models = {'core-1': 'claude-sonnet-5-5', 'core-2': 'claude-opus-5-5'}
    text = starter.prompt_for('/p.pdf', '/out', 2, 'reviewers-only', None, None, models)
    assert 'claude-opus-5-5' in text and 'core-2' in text
    assert 'no model arguments' in text
    assert 'claude-opus' not in starter.prompt_for('/p.pdf', '/out', 2, 'reviewers-only',
                                                   None, None)


def test_asking_for_no_citation_workers_says_so_instead_of_saying_nothing(tmp_path, monkeypatch,
                                                                         no_allowlist):
    """``--citation-workers 0`` is a count, and a prompt that omits it is not.

    The arm still has a citations lane, so the session planned the server's own
    default of four workers — the run that was asked to judge nothing judged
    everything, and the cost of the arm being compared was not the cost that
    was configured.
    """
    seen = {}

    def fake_launch(prompt, directory, timeout_at, on_event, cwd, chosen, watch=None,
                    deadline=None):
        seen['prompt'] = prompt
        seen['deadline'] = deadline
        return [], 'the workflow task finished', 0

    monkeypatch.setattr(starter, 'claude_version', lambda binary=None: '2.1.400')
    monkeypatch.setattr(starter, 'probe', lambda directory, cwd: ([], {'raw': None}))
    monkeypatch.setattr(starter, 'launch', fake_launch)
    starter.review(str(FIXTURE), str(tmp_path / 'none'), reviewers=1, arm='full-two-track',
                   citation_workers=0)
    assert 'Run no citation workers' in seen['prompt']
    assert 'leave the in-text uses unjudged' in seen['prompt']
    assert 'worker(s)' not in seen['prompt']

    starter.review(str(FIXTURE), str(tmp_path / 'two'), reviewers=1, arm='full-two-track',
                   citation_workers=2)
    assert 'Judge the citations with 2 worker(s)' in seen['prompt']

    # An arm with no citations lane is the one case that says nothing, because
    # there is no ledger in it to judge.
    starter.review(str(FIXTURE), str(tmp_path / 'bare'), reviewers=1, arm='reviewers-only',
                   citation_workers=0)
    assert 'citation worker' not in seen['prompt'] and 'worker(s)' not in seen['prompt']


def test_the_session_is_told_when_the_driver_will_end_the_run(tmp_path, monkeypatch,
                                                              no_allowlist):
    """The wall clock crosses the process boundary, because a waiting tool needs it.

    A tool inside the session that is deciding whether to wait — the citation
    resolve stage, whose retry gap is five minutes — cannot see the driver's
    deadline, and the pilot's seven-minute run spent its last rows on a wait
    that was never going to end inside it. It goes over as a POSIX timestamp:
    the driver times itself on a monotonic clock, which means nothing on the
    other side.
    """
    monkeypatch.setattr(starter, 'claude_version', lambda binary=None: '2.1.400')
    environment = starter.child_environment('claude', {'PEERREVIEW_EDITOR_MODEL': 'opus'},
                                            deadline=1_700_000_123.6)
    assert environment['PEERREVIEW_RUN_DEADLINE'] == '1700000124'
    assert environment['PEERREVIEW_EDITOR_MODEL'] == 'opus'
    # A run with no deadline to promise says nothing, rather than saying zero,
    # which a reader would have to parse as a deadline already past.
    assert 'PEERREVIEW_RUN_DEADLINE' not in starter.child_environment('claude')

    seen = {}

    def fake_launch(prompt, directory, timeout_at, on_event, cwd, chosen, watch=None,
                    deadline=None):
        seen['deadline'] = deadline
        return [], 'the workflow task finished', 0

    monkeypatch.setattr(starter, 'probe', lambda directory, cwd: ([], {'raw': None}))
    monkeypatch.setattr(starter, 'launch', fake_launch)
    starter.review(str(FIXTURE), str(tmp_path / 'run'), reviewers=1, arm='reviewers-only',
                   wall_timeout=600)
    # The same wall clock the driver holds itself to, read off the clock the
    # session can also read.
    assert seen['deadline'] == pytest.approx(time.time() + 600, abs=30)


def test_the_prompt_names_the_skill_the_arm_belongs_to():
    full = starter.prompt_for('/p.pdf', '/out', 3, 'full-two-track', None, None)
    assert 'peer-review-manuscript' in full and '3 core reviewer' in full
    assert 'full-two-track' in full
    draft = starter.prompt_for('/p.pdf', '/out', 1, 'draft-check', '2024-01-01', ['stats'])
    assert 'check-draft' in draft and 'stats' in draft and '2024-01-01' in draft


def test_the_version_recorded_with_the_run_is_asked_of_the_binary(tmp_path, monkeypatch):
    """Nothing in the system sets ``PEERREVIEW_CLAUDE_VERSION``, so the starter asks.

    The environment stays as a fallback for a caller that knows better, but a
    run whose provenance says ``null`` cannot be reproduced, and that is what
    reading an unset variable recorded every time.
    """
    monkeypatch.setattr(starter, 'claude_version', lambda binary=None: '2.1.322')
    accounting = starter.Accounting(10, 10)
    (tmp_path / 'progress.json').write_text(json.dumps({'status': 'reviewing'}), encoding='utf-8')
    starter.finish(tmp_path, 'blocked', 'because', accounting, 1.0, None)
    runtime = json.loads((tmp_path / 'runtime.json').read_text(encoding='utf-8'))
    assert runtime['claude_code_version'] == '2.1.322'
    assert runtime['status'] == 'blocked' and runtime['wall_seconds'] == 1.0
    patched = json.loads((tmp_path / 'progress.json').read_text(encoding='utf-8'))
    assert patched['status'] == 'blocked' and patched['error'] == 'because'

    monkeypatch.setattr(starter, 'claude_version', lambda binary=None: None)
    monkeypatch.setenv('PEERREVIEW_CLAUDE_VERSION', '2.1.999')
    starter.finish(tmp_path, 'blocked', 'because', accounting, 1.0, None)
    again = json.loads((tmp_path / 'runtime.json').read_text(encoding='utf-8'))
    assert again['claude_code_version'] == '2.1.999'


def test_a_finished_run_is_not_patched_to_blocked(tmp_path, monkeypatch):
    monkeypatch.setattr(starter, 'claude_version', lambda binary=None: None)
    accounting = starter.Accounting(10, 10)
    (tmp_path / 'progress.json').write_text(json.dumps({'status': 'complete'}), encoding='utf-8')
    starter.finish(tmp_path, 'blocked', 'late rate limit', accounting, 1.0, None)
    assert json.loads(
        (tmp_path / 'progress.json').read_text(encoding='utf-8'))['status'] == 'complete'


def test_nothing_in_the_starter_needs_codex_or_a_key():
    source = Path(starter.__file__).read_text(encoding='utf-8')
    assert 'codex' not in source.lower()
    assert source.count('ANTHROPIC_API_KEY') == 1  # popped from the child environment
    assert source.count('ANTHROPIC_AUTH_TOKEN') == 1
    assert os.environ.get('PEERREVIEW_PLUGIN_RUN') is None
