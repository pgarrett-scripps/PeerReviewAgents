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
import stat
import sys
from pathlib import Path

from peerreviewagents.cli.main import plugin_route, review_argv
from peerreviewagents.plugin import starter

FIXTURE = Path(__file__).resolve().parent / 'fixtures' / 'single-column.pdf'

STUB = '''import json, os, sys

log = os.environ['PEERREVIEW_STUB_LOG']
argv = sys.argv[1:]
with open(log, 'a') as stream:
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
print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False, 'num_turns': 7,
                  'session_id': 'stub', 'result': {'status': 'complete'}}), flush=True)
'''


def install_stub(tmp_path, monkeypatch):
    """A fake ``claude`` on PATH, and nothing else: no codex, no real provider."""
    binaries = tmp_path / 'bin'
    binaries.mkdir()
    stub = binaries / 'claude'
    stub.write_text(f'#!{sys.executable}\n{STUB}')
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    log = tmp_path / 'argv.jsonl'
    monkeypatch.setenv('PATH', str(binaries))
    monkeypatch.setenv('PEERREVIEW_STUB_LOG', str(log))
    monkeypatch.setenv('PEERREVIEW_STUB_TOOLS', json.dumps(list(starter.tool_names())))
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'must-not-be-used')
    return log


# -- the two routes ---------------------------------------------------------


def test_only_arm_sends_review_to_the_plugin_route():
    assert plugin_route(['review', 'paper.pdf', 'out']) is False
    assert plugin_route(['review', 'paper.pdf', 'out', '--reviewers', '3']) is False
    assert plugin_route(['review', 'paper.pdf', 'out', '--arm', 'single']) is True
    assert plugin_route(['review', 'paper.pdf', 'out', '--arm=single']) is True
    assert plugin_route(['review', '--arm', 'single', '--help']) is False
    assert plugin_route(['import', 'paper.pdf', '--arm', 'single']) is False
    assert plugin_route([]) is False


def test_a_bare_pdf_with_an_arm_still_reaches_the_plugin_route():
    rewritten = review_argv(['paper.pdf', 'out', '--arm', 'single'])
    assert rewritten[0] == 'review'
    assert plugin_route(rewritten) is True
    assert starter.handles(rewritten) is True
    assert starter.handles(['review', 'paper.pdf', 'out']) is False


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


def test_no_claude_on_path_is_a_blocked_run_not_a_traceback(tmp_path, monkeypatch):
    monkeypatch.setenv('PATH', str(tmp_path / 'empty'))
    out = tmp_path / 'run'
    code = starter.review(str(FIXTURE), str(out), arm='single')
    assert code == 2
    startup = json.loads((out / 'review' / 'startup.json').read_text())
    assert startup['problems']
    assert startup['tools_expected']
    runtime = json.loads((out / 'review' / 'runtime.json').read_text())
    assert runtime['status'] == 'blocked'
    assert 'probe failed' in runtime['blocked_reason']


def test_an_unknown_arm_and_a_missing_manuscript_are_both_refused(tmp_path):
    for call in (lambda: starter.review(str(FIXTURE), str(tmp_path / 'a'), arm='debate'),
                 lambda: starter.review(str(tmp_path / 'nothing.pdf'), str(tmp_path / 'b'))):
        try:
            call()
        except SystemExit as stop:
            assert 'Unknown arm' in str(stop) or 'No manuscript' in str(stop)
        else:
            raise AssertionError('the run should not have started')


def test_the_stub_run_is_launched_the_way_the_spec_requires(tmp_path, monkeypatch):
    log = install_stub(tmp_path, monkeypatch)
    out = tmp_path / 'run'
    (out / 'review').mkdir(parents=True)
    (out / 'review' / 'progress.json').write_text(json.dumps(
        {'status': 'complete', 'findings_delivered': 4, 'quarantined_findings_count': 0,
         'stages': {'missing': []}}))

    code = starter.review(str(FIXTURE), str(out), reviewers=3, arm='full-two-track')
    assert code == 0

    calls = [json.loads(line) for line in log.read_text().splitlines()]
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

    runtime = json.loads((out / 'review' / 'runtime.json').read_text())
    assert runtime['status'] == 'complete'
    assert runtime['blocked_reason'] is None
    assert runtime['tokens_total'] == 124000
    assert {row['label']: row['model'] for row in runtime['agents']} == {
        'core-1': 'claude-sonnet-5-5', 'write review': 'claude-haiku-5-5'}
    assert runtime['result']['num_turns'] == 7
    usage = (out / 'review' / 'usage' / 'core-1' / 'usage.jsonl').read_text()
    assert json.loads(usage.splitlines()[0])['observed_tokens'] == 120000
    assert (out / 'review' / 'usage' / 'core-1' / 'core-1-1.started.json').is_file()
    assert (out / 'review' / 'usage' / 'write-review').is_dir()


def test_a_complete_run_is_not_claimed_when_the_server_wrote_no_progress(tmp_path, monkeypatch):
    install_stub(tmp_path, monkeypatch)
    out = tmp_path / 'run'
    code = starter.review(str(FIXTURE), str(out), arm='single')
    assert code == 1
    runtime = json.loads((out / 'review' / 'runtime.json').read_text())
    assert runtime['status'] == 'blocked'
    assert runtime['blocked_reason'] == 'the server wrote no progress file'


def test_the_api_key_never_reaches_the_session(tmp_path, monkeypatch):
    seen = {}

    def fake_popen(command, **kwargs):
        seen['env'] = kwargs['env']
        seen['command'] = command
        raise FileNotFoundError('stopping here')

    monkeypatch.setenv('ANTHROPIC_API_KEY', 'must-not-be-used')
    monkeypatch.setattr(starter.subprocess, 'Popen', fake_popen)
    events, stop, code = starter.launch('hello', tmp_path, 0.0, lambda event: None, tmp_path)
    assert (events, code) == (0, 127)
    assert 'could not be started' in stop
    assert 'ANTHROPIC_API_KEY' not in seen['env']
    assert seen['command'][0] == 'claude'
    assert seen['env']['PEERREVIEW_PLUGIN_RUN'] == '1'


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


def test_progress_carried_under_the_task_key_is_still_read():
    accounting = starter.Accounting(1_000_000, 1_000_000)
    accounting.on_event({'type': 'system', 'subtype': 'task_progress',
                         'task': {'workflow_progress': [{'label': 'stats', 'tokens': 42}]}})
    assert accounting.total_tokens() == 42


def test_the_first_result_is_not_the_answer():
    accounting = starter.Accounting(1_000_000, 1_000_000)
    assert accounting.on_event({'type': 'result', 'result': 'still running'}) is None
    assert accounting.on_event({'type': 'system', 'subtype': 'task_notification'}) is None
    assert accounting.on_event({'type': 'result', 'result': 'done'}) == \
        'the workflow task finished'
    assert accounting.last_result()['result'] == 'done'


def test_a_rate_limit_window_at_the_ceiling_blocks_the_run():
    accounting = starter.Accounting(1_000_000, 1_000_000)
    stop = accounting.on_event({'type': 'rate_limit_event', 'unifiedWindows': {
        'five_hour': {'utilization': 0.2}, 'seven_day': {'utilization': 0.994}}})
    assert 'seven_day rate-limit window' in stop
    assert accounting.rate_limits


def test_a_window_below_the_ceiling_is_left_alone():
    accounting = starter.Accounting(1_000_000, 1_000_000)
    assert accounting.on_event({'type': 'rate_limit_event', 'unifiedWindows': {
        'seven_day': {'utilization': 0.5}}}) is None
    assert accounting.rate_limits == []


def test_a_rate_limit_message_blocks_the_run_whatever_its_shape():
    accounting = starter.Accounting(1_000_000, 1_000_000)
    stop = accounting.on_event({'type': 'system', 'subtype': 'rate_limit_event',
                                'rate_limit': {'status': 'limit_reached'}})
    assert 'rate limit was reached' in stop


def test_an_ordinary_event_is_not_a_rate_limit():
    assert starter.rate_limit_reason(
        {'type': 'assistant', 'message': 'the paper exceeded expectations'}) is None
    accounting = starter.Accounting(1_000_000, 1_000_000)
    assert accounting.on_event(progress([{'label': 'core-1', 'tokens': 10}])) is None


def test_the_prompt_names_the_skill_the_arm_belongs_to():
    full = starter.prompt_for('/p.pdf', '/out', 3, 'full-two-track', None, None)
    assert 'peer-review-manuscript' in full and '3 core reviewer' in full
    assert 'full-two-track' in full
    draft = starter.prompt_for('/p.pdf', '/out', 1, 'draft-check', '2024-01-01', ['stats'])
    assert 'check-draft' in draft and 'stats' in draft and '2024-01-01' in draft


def test_the_version_recorded_with_the_run_comes_from_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv('PEERREVIEW_CLAUDE_VERSION', '2.1.322')
    accounting = starter.Accounting(10, 10)
    (tmp_path / 'progress.json').write_text(json.dumps({'status': 'reviewing'}))
    starter.finish(tmp_path, 'blocked', 'because', accounting, 1.0, None)
    runtime = json.loads((tmp_path / 'runtime.json').read_text())
    assert runtime['claude_code_version'] == '2.1.322'
    assert runtime['status'] == 'blocked' and runtime['wall_seconds'] == 1.0
    patched = json.loads((tmp_path / 'progress.json').read_text())
    assert patched['status'] == 'blocked' and patched['error'] == 'because'


def test_a_finished_run_is_not_patched_to_blocked(tmp_path):
    accounting = starter.Accounting(10, 10)
    (tmp_path / 'progress.json').write_text(json.dumps({'status': 'complete'}))
    starter.finish(tmp_path, 'blocked', 'late rate limit', accounting, 1.0, None)
    assert json.loads((tmp_path / 'progress.json').read_text())['status'] == 'complete'


def test_nothing_in_the_starter_needs_codex_or_a_key():
    source = Path(starter.__file__).read_text()
    assert 'codex' not in source.lower()
    assert source.count('ANTHROPIC_API_KEY') == 1  # popped from the child environment
    assert os.environ.get('PEERREVIEW_PLUGIN_RUN') is None
