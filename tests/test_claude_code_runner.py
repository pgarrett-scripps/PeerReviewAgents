"""One agent, run as a headless Claude Code session.

Everything here is about the boundary between this package and the harness: the
environment a session is built with rather than inheriting, the flags it is
started with, the stream it is read from, and the four ways a session can end.
The panel itself is tested without a model at all (see
``tests/test_scripted_panel.py``) — this file is the only place a process is
actually started, and it is started as a stub that replays a stream.

The stub matters more than it looks: ``environment()`` carries over a short
allowlist and nothing else, so a test cannot pass anything to the child through
the environment. Each stub therefore carries its own stream in its source,
which is also a standing check that no test secretly depends on a variable
reaching the session.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

from peerreviewagents.agents import definition
from peerreviewagents.plugin import claude_code
from peerreviewagents.plugin import runner as agent_runner
from peerreviewagents.plugin.claude_code import ClaudeCodeRunner
from peerreviewagents.plugin.service import TOOL_PREFIX

SCHEMA = {'type': 'object', 'properties': {'ok': {'type': 'boolean'}}, 'required': ['ok']}
#: A healthy rate-limit event, exactly as the real stream sends it on a
#: subscription with no overage credit: served perfectly well, and reporting a
#: rejected overage while it does.
HEALTHY_RATE_LIMIT = {'type': 'rate_limit_event', 'rate_limit_info': {
    'status': 'allowed', 'overageStatus': 'rejected',
    'overageDisabledReason': 'member_zero_credit_limit',
    'windows': {'five_hour': {'utilization': 0.21}, 'seven_day': {'utilization': 0.55}}}}

STUB = """\
import json
import sys
import time

VERSION = {version!r}
EVENTS = {events!r}
NOISE = {noise!r}
LINGER = {linger!r}

if '--version' in sys.argv:
    print(VERSION)
    sys.exit(0)
for index in range(NOISE):
    sys.stderr.write('plugin diagnostic line %d\\n' % index)
sys.stderr.flush()
for event in EVENTS:
    print(json.dumps(event), flush=True)
if LINGER:
    time.sleep(LINGER)
"""


def install(tmp_path, monkeypatch, events=(), version='2.1.400', noise=0, linger=0.0):
    """Put a ``claude`` on PATH that replays this stream and nothing else."""
    binaries = tmp_path / 'bin'
    binaries.mkdir(exist_ok=True)
    source = STUB.format(version=version, events=list(events), noise=noise, linger=linger)
    if os.name == 'nt':
        (binaries / 'stub.py').write_text(source, encoding='utf-8')
        (binaries / 'claude.cmd').write_text(
            f'@"{sys.executable}" "{binaries / "stub.py"}" %*\n', encoding='utf-8')
    else:
        stub = binaries / 'claude'
        stub.write_text(f'#!{sys.executable}\n{source}', encoding='utf-8')
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv('PATH', str(binaries))
    claude_code.VERSIONS.clear()
    return binaries


def task_for(agent='core-reviewer', **over):
    held = definition(agent)
    over.setdefault('address', 'http://127.0.0.1:9/mcp')
    return agent_runner.Task.of(held, 'review this paper', SCHEMA, label='core-1', **over)


def granted(task):
    return [f'{TOOL_PREFIX}{tool}' for tool in task.tools] + [claude_code.ANSWER_TOOL]


def init_event(task, server='connected', tools=None):
    return {'type': 'system', 'subtype': 'init',
            'mcp_servers': [{'name': 'peerreview', 'status': server}],
            'tools': granted(task) if tools is None else list(tools)}


def assistant(text='', tokens=0, calls=0, model='claude-sonnet-5-5'):
    content = [{'type': 'tool_use', 'name': 'submit_finding'} for _ in range(calls)]
    if text:
        content.append({'type': 'text', 'text': text})
    return {'type': 'assistant', 'message': {
        'model': model, 'content': content,
        'usage': {'input_tokens': tokens, 'output_tokens': 0,
                  'cache_read_input_tokens': 0, 'cache_creation_input_tokens': 0}}}


def result_event(answer=None, subtype='success', model='claude-sonnet-5-5', tokens=0):
    event = {'type': 'result', 'subtype': subtype, 'is_error': subtype != 'success',
             'num_turns': 4, 'duration_ms': 1200, 'session_id': 'stub-session',
             'usage': {'input_tokens': tokens}}
    if answer is not None:
        event['structured_output'] = answer
    if model:
        event['modelUsage'] = {model: {'canonicalModel': model}}
    return event


# -- the environment a session is built with --------------------------------


def test_neither_the_api_key_nor_the_auth_token_reaches_the_session(monkeypatch):
    """Subscription login only, and nothing of the session that started us.

    ``CLAUDECODE`` and the ``CLAUDE_CODE_*`` variables told a child it was
    already inside a session, which made its behaviour depend on ours; the two
    key variables would bill an account that is not the one the run is for.
    """
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'must-not-be-used')
    monkeypatch.setenv('ANTHROPIC_AUTH_TOKEN', 'must-not-be-used-either')
    monkeypatch.setenv('CLAUDECODE', '1')
    monkeypatch.setenv('CLAUDE_CODE_SUBAGENT_MODEL', 'haiku')
    monkeypatch.setenv('HOME', '/home/reviewer')
    built = claude_code.environment()
    assert 'ANTHROPIC_API_KEY' not in built and 'ANTHROPIC_AUTH_TOKEN' not in built
    assert 'CLAUDECODE' not in built
    assert not [key for key in built if key.startswith('CLAUDE_CODE_')
                and key != 'CLAUDE_CODE_DISABLE_MEMORY']
    assert built['HOME'] == '/home/reviewer', 'the subscription login lives under HOME'
    assert built['CLAUDE_CODE_DISABLE_MEMORY'] == '1', 'no CLAUDE.md from this machine'
    assert built['PEERREVIEW_PLUGIN_RUN'] == '1'
    assert built['TERM'] == 'dumb' and built['CI'] == '1'
    # Nor can a caller hand one in: the pop is the last word on the subject.
    smuggled = claude_code.environment({'ANTHROPIC_API_KEY': 'nor-this', 'PEERREVIEW_X': '2'})
    assert 'ANTHROPIC_API_KEY' not in smuggled and smuggled['PEERREVIEW_X'] == '2'


def test_nothing_in_the_runner_wants_a_key_or_a_second_harness():
    source = Path(claude_code.__file__).read_text(encoding='utf-8')
    assert 'codex' not in source.lower()
    assert source.count('ANTHROPIC_API_KEY') == 1  # popped from the child environment
    assert source.count('ANTHROPIC_AUTH_TOKEN') == 1
    assert os.environ.get('PEERREVIEW_PLUGIN_RUN') is None


def test_the_session_is_started_with_our_tools_and_no_settings_of_its_own(tmp_path):
    runner = ClaudeCodeRunner()
    task = task_for(model='claude-sonnet-5-5', effort='high', max_turns=42)
    command = runner.command(task, tmp_path)
    flags = {name: (command[index + 1] if index + 1 < len(command) else '')
             for index, name in enumerate(command) if str(name).startswith('--')}
    assert Path(command[0]).stem == 'claude', 'the resolved path, not the bare word'
    assert command[1] == '-p' and command[2] == task.prompt
    assert flags['--setting-sources'] == '', 'no user, project or local settings file'
    assert flags['--tools'] == '', 'no built-ins: nothing reads a file or reaches the web'
    assert '--strict-mcp-config' in command and '--disable-slash-commands' in command
    assert '--no-session-persistence' in command and '--bare' not in command
    assert flags['--model'] == 'claude-sonnet-5-5' and flags['--effort'] == 'high'
    assert flags['--output-format'] == 'stream-json' and '--verbose' in command
    assert json.loads(flags['--json-schema']) == SCHEMA
    assert json.loads(flags['--mcp-config'])['mcpServers']['peerreview'] == {
        'type': 'http', 'url': task.address}
    allowed = flags['--allowed-tools'].split(',')
    assert allowed == granted(task)
    assert all(name.startswith(TOOL_PREFIX) for name in allowed[:-1])
    written = json.loads(Path(flags['--agents']).read_text(encoding='utf-8'))
    held = written[task.agent.name]
    assert held['prompt'] == task.instructions and held['description']
    assert held['omitClaudeMd'] is True, 'no CLAUDE.md is read by the agent either'
    assert (held['model'], held['effort'], held['maxTurns']) == ('claude-sonnet-5-5', 'high', 42)
    assert held['tools'] == allowed


# -- reading one session's stream -------------------------------------------


def test_a_session_that_answers_is_reported_as_it_ran_not_as_it_was_asked(tmp_path, monkeypatch):
    """The answer, the tokens, the tool calls, and the model it really ran."""
    task = task_for(model='claude-opus-5-5')
    install(tmp_path, monkeypatch, events=[
        init_event(task),
        assistant(tokens=40_000, calls=2),
        assistant(text='done', tokens=50_000, calls=1),
        result_event({'ok': True}, model='claude-haiku-5-5', tokens=90_000)])
    outcome = ClaudeCodeRunner().run(task)
    assert outcome.ending == agent_runner.ANSWERED and outcome.closed
    assert outcome.answer == {'ok': True}
    assert outcome.tokens == 90_000, 'the result event is the whole session'
    assert outcome.tool_calls == 3
    assert outcome.model == 'claude-haiku-5-5', 'what ran, never what was asked for'
    assert outcome.record['init'] == {'server': 'connected', 'tools': len(granted(task)),
                                      'missing': [], 'built_ins': []}
    assert outcome.record['result']['num_turns'] == 4
    assert outcome.record['rate_limits'] == []
    assert outcome.seconds > 0


def test_both_pipes_are_read_so_a_loud_session_cannot_wedge(tmp_path, monkeypatch):
    """stderr is drained for the whole run, and stdout is not polled.

    ``select`` cannot wait on a pipe on Windows, and stderr read only at the
    end fills its pipe and wedges the session writing to it.
    """
    source = Path(claude_code.__file__).read_text(encoding='utf-8')
    assert 'import select' not in source and 'select.select' not in source
    assert 'threading.Thread' in source

    task = task_for()
    install(tmp_path, monkeypatch, noise=4000,
            events=[init_event(task), result_event({'ok': True})])
    outcome = ClaudeCodeRunner().run(task)
    assert outcome.ending == agent_runner.ANSWERED, 'the event arrived although stderr filled'


def test_a_session_past_its_token_ceiling_is_stopped_where_it_stands(tmp_path, monkeypatch):
    task = task_for(token_ceiling=100_000)
    install(tmp_path, monkeypatch, linger=90.0, events=[
        init_event(task), assistant(tokens=60_000), assistant(tokens=70_000)])
    outcome = ClaudeCodeRunner().run(task)
    assert outcome.ending == agent_runner.OVER_BUDGET
    assert outcome.tokens == 130_000 and not outcome.closed
    assert outcome.seconds < 60, 'it was stopped, not waited out'


def test_a_session_past_its_deadline_is_stopped_where_it_stands(tmp_path, monkeypatch):
    task = task_for(timeout=1.0)
    install(tmp_path, monkeypatch, linger=90.0, events=[init_event(task)])
    outcome = ClaudeCodeRunner().run(task)
    assert outcome.ending == agent_runner.TIMED_OUT
    assert 1.0 <= outcome.seconds < 60


def test_an_agent_that_never_answers_says_what_it_did_instead(tmp_path, monkeypatch):
    """A silent stage is retried once and then blocks the run, so the record of
    why it was silent is the whole of what a reader gets."""
    task = task_for()
    install(tmp_path, monkeypatch, events=[
        init_event(task, server='failed', tools=[claude_code.ANSWER_TOOL, 'Bash']),
        assistant(text='I could not reach the review tools.', tokens=1200),
        result_event(subtype='error_during_execution')])
    outcome = ClaudeCodeRunner().run(task)
    assert outcome.ending == agent_runner.SILENT and outcome.answer is None
    assert outcome.tokens == 1200
    assert 'the run\'s tool server was failed' in outcome.detail
    assert 'these tools were not in the session' in outcome.detail
    assert 'the session ended error_during_execution' in outcome.detail
    assert 'the agent signed off: I could not reach the review tools.' in outcome.detail
    assert outcome.record['init']['built_ins'] == ['Bash'], \
        'a built-in in the session is a misconfiguration worth recording'
    assert outcome.record['init']['missing'] == sorted(granted(task)[:-1])


def test_a_session_that_never_started_up_is_not_a_review_that_went_quiet(tmp_path, monkeypatch):
    task = task_for()
    install(tmp_path, monkeypatch, events=[])
    outcome = ClaudeCodeRunner().run(task)
    assert outcome.ending == agent_runner.SILENT
    assert 'the session never reported starting up' in outcome.detail


def test_a_rate_limit_in_the_stream_is_carried_out_with_the_outcome(tmp_path, monkeypatch):
    """The panel stops starting agents on this, so it has to leave the stream."""
    task = task_for()
    limited = {'type': 'rate_limit_event', 'rate_limit_info': {
        'status': 'rejected', 'overageDisabledReason': 'member_zero_credit_limit'}}
    install(tmp_path, monkeypatch, events=[
        init_event(task), HEALTHY_RATE_LIMIT, limited, result_event({'ok': True})])
    outcome = ClaudeCodeRunner().run(task)
    assert outcome.ending == agent_runner.ANSWERED
    assert len(outcome.record['rate_limits']) == 1, 'the healthy one is not a limit'
    assert 'status rejected' in outcome.record['rate_limits'][0]


def test_the_raw_stream_is_kept_only_when_something_asks_for_it(tmp_path, monkeypatch):
    task = task_for()
    install(tmp_path, monkeypatch, events=[init_event(task), result_event({'ok': True})])
    runner = ClaudeCodeRunner(keep_events=True)
    runner.events_root = tmp_path / 'events'
    outcome = runner.run(task)
    kept = Path(outcome.record['events'])
    assert kept.name == 'core-1.json'
    assert [event['type'] for event in json.loads(kept.read_text(encoding='utf-8'))] == [
        'system', 'result']
    assert 'events' not in ClaudeCodeRunner().run(task).record


# -- what cannot be run at all ----------------------------------------------


def test_no_address_and_no_claude_are_both_outcomes_rather_than_tracebacks(tmp_path,
                                                                          monkeypatch):
    runner = ClaudeCodeRunner()
    homeless = runner.run(task_for(address=''))
    assert homeless.ending == agent_runner.FAILED
    assert 'no tool server address' in homeless.detail

    monkeypatch.setenv('PATH', str(tmp_path / 'empty'))
    claude_code.VERSIONS.clear()
    outcome = runner.run(task_for())
    assert outcome.ending == agent_runner.FAILED
    assert outcome.detail == claude_code.MISSING_CLAUDE
    assert 'not installed or not on this PATH' in outcome.detail
    assert runner.unavailable({}) == [claude_code.MISSING_CLAUDE], \
        'a missing harness refuses every model there is'
    assert runner.unavailable({'reader': 'claude-sonnet-5-5'}) == [claude_code.MISSING_CLAUDE]
    assert runner.describe() == 'claude-code (version unknown)'


def test_a_model_no_login_can_run_is_named_before_anything_is_started(tmp_path, monkeypatch):
    install(tmp_path, monkeypatch, events=[])
    monkeypatch.setattr(claude_code.model_plan, 'allowlist', lambda **kwargs: None)
    runner = ClaudeCodeRunner()
    assert runner.unavailable({}) == []
    assert runner.unavailable({'core-1': 'claude-sonnet-5-5'}) == []
    refused = runner.unavailable({'core-1': 'claude-nonesuch-9'})
    assert refused and 'claude-nonesuch-9' in refused[0]


def test_what_is_started_is_the_path_the_shell_would_resolve(tmp_path, monkeypatch):
    """A bare word is not a program on every platform.

    Windows puts ``claude.cmd`` on PATH and the shell finds it through PATHEXT;
    the call that starts a child does not consult PATHEXT itself, so a bare
    ``claude`` there is not found at all. The runner starts what
    :func:`claude_code.executable` resolved, and the version is asked of the
    same path, so the two cannot disagree about which binary was meant.
    """
    binaries = install(tmp_path, monkeypatch, events=[])
    found = claude_code.executable()
    assert found and Path(found).exists() and Path(found).parent == binaries
    command = ClaudeCodeRunner().command(task_for(), tmp_path)
    assert command[0] == found, 'the session is started with the resolved path'
    assert claude_code.version() == '2.1.400', 'and the version is asked of it'

    monkeypatch.setenv('PATH', str(tmp_path / 'empty'))
    assert claude_code.executable() is None
    assert ClaudeCodeRunner().command(task_for(), tmp_path)[0] == claude_code.BINARY, \
        'nothing to resolve leaves the bare word, which the run refuses before it starts'


def test_the_version_is_asked_of_the_binary_once_and_named_in_the_provenance(tmp_path,
                                                                            monkeypatch):
    """A run whose provenance says ``null`` cannot be reproduced."""
    install(tmp_path, monkeypatch, version='2.1.400 (Claude Code)', events=[])
    assert claude_code.version() == '2.1.400 (Claude Code)'
    assert ClaudeCodeRunner().describe() == 'claude-code 2.1.400 (Claude Code)'
    # Asked once per process: the second stub is never run.
    install(tmp_path, monkeypatch, version='9.9.9', events=[])
    claude_code.VERSIONS['claude'] = '2.1.400 (Claude Code)'
    assert claude_code.version() == '2.1.400 (Claude Code)'
    claude_code.VERSIONS.clear()
    assert claude_code.version() == '9.9.9'

    monkeypatch.setenv('PATH', str(tmp_path / 'empty'))
    claude_code.VERSIONS.clear()
    assert claude_code.version() is None, 'a binary that is not there says nothing'


# -- the shapes the stream sends --------------------------------------------


def test_a_rate_limit_is_read_from_the_shape_a_real_stream_sends():
    assert claude_code.rate_limit_reason(HEALTHY_RATE_LIMIT) is None, \
        'a rejected overage status is the normal state of a plan without credit'
    rejected = {'type': 'rate_limit_event', 'rate_limit_info': dict(
        HEALTHY_RATE_LIMIT['rate_limit_info'], status='rejected')}
    said = claude_code.rate_limit_reason(rejected)
    assert 'status rejected' in said and 'member_zero_credit_limit' in said
    at = {'type': 'rate_limit_event', 'rate_limit_info': {
        'status': 'allowed', 'windows': {'five_hour': {'utilization': 0.2},
                                         'seven_day': {'utilization': 0.994}}}}
    assert 'seven_day rate-limit window is at 99%' in claude_code.rate_limit_reason(at)
    below = {'type': 'rate_limit_event', 'rate_limit_info': {
        'status': 'allowed', 'windows': {'seven_day': {'utilization': 0.5}}}}
    assert claude_code.rate_limit_reason(below) is None
    # The other name the harness has used for the same block, and two events
    # that are not rate limits at all.
    assert claude_code.rate_limit_reason({'rate_limit': {'status': 'rejected'}})
    assert claude_code.rate_limit_reason(
        {'type': 'assistant', 'message': 'the paper exceeded expectations'}) is None
    assert claude_code.rate_limit_reason('rate_limit_event') is None


def test_the_tokens_an_answer_and_the_model_are_read_defensively():
    assert claude_code.tokens_of({'input_tokens': 10, 'output_tokens': 2,
                                  'cache_read_input_tokens': 5,
                                  'cache_creation_input_tokens': 3}) == 20
    assert claude_code.tokens_of({'input_tokens': None}) == 0
    assert claude_code.tokens_of('nothing') == 0

    assert claude_code.answer_of({'structured_output': {'ok': True}}) == {'ok': True}
    assert claude_code.answer_of({'structuredOutput': {'ok': False}}) == {'ok': False}
    assert claude_code.answer_of({'result': '{"ok": true}'}) == {'ok': True}
    assert claude_code.answer_of({'result': '{"ok": tru'}) is None
    assert claude_code.answer_of({'result': 'I have reviewed the paper.'}) is None
    assert claude_code.answer_of({'result': '[1, 2]'}) is None, 'an answer is an object'

    assert claude_code.ran_as({'modelUsage': {'x': {'canonicalModel': 'claude-opus-5-5'}}},
                              {'claude-haiku-5-5'}) == 'claude-opus-5-5'
    assert claude_code.ran_as({'modelUsage': {'claude-fable-5-2': {}}}, set()) == \
        'claude-fable-5-2'
    assert claude_code.ran_as({}, {'claude-sonnet-5-5', 'claude-haiku-5-5'}) == \
        'claude-haiku-5-5', 'the assistant messages, sorted, when the result will not say'
    assert claude_code.ran_as({}, set()) == ''

    assert claude_code.events_of('{"type": "result"}') == {'type': 'result'}
    assert claude_code.events_of('not json') is None
    assert claude_code.events_of('[1, 2]') is None


@pytest.mark.parametrize('name', ['reader', 'core-reviewer', 'editor', 'fix-check'])
def test_every_definition_a_slot_runs_can_be_turned_into_a_session(tmp_path, name):
    """A definition with no description, no model or no tools is a session that
    will not start, and the first a run would hear of it is the stage failing."""
    task = task_for(name)
    allowed, held = ClaudeCodeRunner().definition(task)
    assert task.agent.description and task.model and task.effort and task.max_turns
    assert task.tools, f'{name} is granted no tools at all'
    assert held[name]['tools'] == allowed
    assert len(allowed) == len(task.tools) + 1
    command = ClaudeCodeRunner().command(task, tmp_path)
    assert command.count('--allowed-tools') == 1
