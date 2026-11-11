"""Offline checks for the Claude Code subscription transport, using a scripted CLI."""
import base64
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from peerreviewagents.engine import model_runtime as runtime

FAKE_CLAUDE = '''#!{python}
import json, os, sys, time
scenario = json.load(open(os.environ["PAPER_WORKBENCH_FAKE_CLAUDE"]))
payload = sys.stdin.read()
with open(scenario["launches"], "a") as stream:
    stream.write(json.dumps({{"argv": sys.argv[1:], "stdin": payload, "cwd": os.getcwd(),
                             "env": {{key: os.environ.get(key) for key in scenario["env_keys"]}}}}) + "\\n")
for index, event in enumerate(scenario["events"]):
    if index == len(scenario["events"]) - 1:
        time.sleep(scenario.get("pause", 0))
    print(json.dumps(event), flush=True)
sys.exit(scenario.get("returncode", 0))
'''
MODEL = 'claude-sonnet-5-5'
LIMITS = {'type': 'rate_limit_event', 'rate_limit_info': {
    'status': 'allowed', 'overageStatus': 'rejected', 'overageDisabledReason': 'member_zero_credit_limit',
    'isUsingOverage': False}}
ENV_KEYS = ['ANTHROPIC_API_KEY', 'CLAUDECODE', 'CLAUDE_CODE_DISABLE_CLAUDE_MDS', 'CLAUDE_CODE_DISABLE_AUTO_MEMORY',
            'CLAUDE_CODE_DISABLE_ATTACHMENTS', 'CLAUDE_CODE_NO_MODEL_FALLBACK']
PNG = b'\x89PNG\r\n\x1a\nfigure'
JPEG = b'\xff\xd8\xffphoto'


def init(tools=('StructuredOutput',), key='none', model=MODEL, servers=()):
    return {'type': 'system', 'subtype': 'init', 'apiKeySource': key, 'tools': list(tools),
            'mcp_servers': list(servers), 'model': model, 'claude_code_version': '2.1.299'}


def result(value=None, model=MODEL, **extra):
    value = {'answer': 'ok'} if value is None else value
    return {'type': 'result', 'subtype': 'success', 'is_error': False, 'structured_output': value,
            'result': json.dumps(value), 'permission_denials': [], 'modelUsage': {model: {'canonicalModel': model}},
            'usage': {'input_tokens': 5, 'cache_read_input_tokens': 20, 'cache_creation_input_tokens': 10,
                      'output_tokens': 7}, **extra}


def tool_turn(name, is_error=False):
    return [{'type': 'assistant', 'message': {'content': [{'type': 'tool_use', 'id': 'call-1', 'name': name, 'input': {}}]}},
            {'type': 'user', 'message': {'content': [{'type': 'tool_result', 'tool_use_id': 'call-1', 'is_error': is_error,
                                                      'content': 'tool output'}]}}]


class ClaudeRuntime(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        fake = self.bin / 'claude'
        fake.write_text(FAKE_CLAUDE.format(python=sys.executable))
        fake.chmod(0o755)
        self.scenario = self.root / 'scenario.json'
        self.launches = self.root / 'launches.jsonl'
        environment = patch.dict(os.environ, {'PATH': str(self.bin) + os.pathsep + os.environ['PATH'],
                                              'PAPER_WORKBENCH_FAKE_CLAUDE': str(self.scenario),
                                              'ANTHROPIC_API_KEY': 'must-not-reach-claude'})
        environment.start()
        self.addCleanup(environment.stop)
        self.out = self.root / 'execution'
        self.protocol = {'provider': 'claude-code', 'model': MODEL, 'effort': 'medium',
                         'observed_token_stop': 1000, 'call_timeout_seconds': 30}
        self.schema = {'type': 'object'}

    def script(self, events, **options):
        runtime.write_json(self.scenario, {'events': events, 'launches': str(self.launches),
                                           'env_keys': ENV_KEYS, **options})

    def launched(self):
        return runtime.jsonl(self.launches)

    def call(self, prompt='prompt', **kwargs):
        protocol = kwargs.pop('protocol', self.protocol)
        return runtime.model_call(protocol, self.out, prompt, self.schema, **kwargs)

    def receipt(self):
        return [runtime.read_json(path) for path in (self.out / 'calls').glob('*.json')
                if len(path.stem) == 64][0]

    def tool_config(self):
        paper = self.root / 'paper'
        paper.mkdir()
        (paper / 'manifest.json').write_text('{}')
        runtime.write_json(self.out / 'workspace' / 'tool-config.json', {'paper': str(paper)})

    def test_structured_call_uses_the_subscription_session_and_is_cached(self):
        self.script([init(), LIMITS, result()])
        self.assertEqual(self.call(), {'answer': 'ok'})
        self.assertEqual(self.call(), {'answer': 'ok'})
        self.assertEqual(len(self.launched()), 1)
        launch = self.launched()[0]
        argv = launch['argv']
        self.assertEqual(argv[argv.index('--model') + 1], MODEL)
        self.assertEqual(argv[argv.index('--effort') + 1], 'medium')
        self.assertEqual(argv[argv.index('--tools') + 1], '')
        self.assertEqual(argv[argv.index('--permission-mode') + 1], 'dontAsk')
        self.assertEqual(argv[argv.index('--system-prompt') + 1], runtime.CLAUDE_SYSTEM_PROMPT)
        for flag in ('--restricted', '--strict-mcp-config', '--no-session-persistence', '--disable-slash-commands'):
            self.assertIn(flag, argv)
        for flag in ('--mcp-config', '--allowedTools', '--dangerously-skip-permissions', '--bare'):
            self.assertNotIn(flag, argv)
        message = json.loads(launch['stdin'])
        self.assertEqual(message['message']['content'], [{'type': 'text', 'text': 'prompt'}])
        self.assertEqual(Path(launch['cwd']).resolve(), (self.out / 'workspace').resolve())
        self.assertIsNone(launch['env']['ANTHROPIC_API_KEY'])
        for key in ENV_KEYS[2:]:
            self.assertEqual(launch['env'][key], '1')
        rows = runtime.jsonl(self.out / 'usage.jsonl')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['observed_tokens'], 42)
        self.assertEqual(rows[0]['models_observed'], [MODEL])
        self.assertEqual(rows[0]['claude_code_version'], '2.1.299')
        self.assertFalse(rows[0]['subscription_limits'][0]['isUsingOverage'])

    def test_tools_are_limited_to_the_manuscript_server_with_explicit_approvals(self):
        self.tool_config()
        approved = runtime.tool_profile({})[1]
        tools = ['StructuredOutput'] + [runtime.CLAUDE_TOOL_PREFIX + name for name in approved]
        self.script([init(tools, servers=[{'name': 'manuscript', 'status': 'connected'}]), LIMITS,
                     *tool_turn('mcp__manuscript__read_blocks'), *tool_turn('StructuredOutput', is_error=True), result()])
        self.assertEqual(self.call(tools=True), {'answer': 'ok'})
        argv = self.launched()[0]['argv']
        servers = json.loads(argv[argv.index('--mcp-config') + 1])['mcpServers']
        self.assertEqual(list(servers), ['manuscript'])
        self.assertEqual(servers['manuscript']['command'], sys.executable)
        self.assertEqual(servers['manuscript']['args'], [str(runtime.HERE / 'manuscript_tools.py'),
                                                         str(self.out / 'workspace' / 'tool-config.json')])
        self.assertEqual(argv[argv.index('--allowedTools') + 1].split(','),
                         [runtime.CLAUDE_TOOL_PREFIX + name for name in approved])
        self.assertEqual(argv[argv.index('--tools') + 1], '')

    def test_images_follow_the_prompt_in_order_and_unsupported_files_never_launch(self):
        png, jpeg, text = self.root / 'a.png', self.root / 'b.jpg', self.root / 'c.png'
        png.write_bytes(PNG)
        jpeg.write_bytes(JPEG)
        text.write_bytes(b'not an image')
        self.script([init(), LIMITS, result()])
        protocol = {**self.protocol, 'max_source_images': 3}
        self.call(protocol=protocol, images=[png, jpeg])
        content = json.loads(self.launched()[0]['stdin'])['message']['content']
        self.assertEqual([block['type'] for block in content], ['text', 'image', 'image'])
        self.assertEqual([block['source']['media_type'] for block in content[1:]], ['image/png', 'image/jpeg'])
        self.assertEqual(base64.b64decode(content[2]['source']['data']), JPEG)
        with self.assertRaisesRegex(runtime.RunBlocked, 'Unsupported'):
            self.call('other prompt', protocol=protocol, images=[text])
        self.assertEqual(len(self.launched()), 1)
        self.assertEqual(runtime.unresolved_attempts(self.out), [])

    def test_api_key_session_is_stopped_and_blocks_later_calls(self):
        self.script([init(key='ANTHROPIC_API_KEY'), LIMITS, result()], pause=10)
        started = time.time()
        with self.assertRaisesRegex(runtime.RunBlocked, 'subscription login'):
            self.call()
        self.assertLess(time.time() - started, 8)
        self.assertTrue(self.receipt()['usage_unknown'])
        with self.assertRaisesRegex(runtime.RunBlocked, 'usage is unknown'):
            self.call('second prompt')
        self.assertEqual(len(self.launched()), 1)

    def test_paid_extra_usage_does_not_stop_the_call(self):
        """How much a run may spend is the account's setting, not the engine's.

        A stream that reports paid extra usage, enabled or in use, runs to
        completion and records the limits it reported."""
        for info in ({'isUsingOverage': True, 'overageStatus': 'rejected'}, {'overageStatus': 'allowed'},
                     {'overageStatus': 'allowed_warning', 'isUsingOverage': False}):
            with self.subTest(info=info):
                self.out = self.root / ('execution-' + str(len(self.launched())))
                self.script([init(), {'type': 'rate_limit_event', 'rate_limit_info': info}, result()])
                self.assertEqual(self.call(), {'answer': 'ok'})
                row = runtime.jsonl(self.out / 'usage.jsonl')[0]
                self.assertTrue(row['ok'])
                self.assertEqual(row['subscription_limits'], [info])

    def test_unverified_or_substituted_sessions_fail_closed_with_usage_recorded(self):
        cases = [([init(), result()], 'no subscription usage limits'),
                 ([init(), LIMITS, result(model='claude-haiku-5-5')], 'Unexpected model'),
                 ([init(model='claude-opus-5-5'), LIMITS, result()], 'Unexpected model'),
                 ([init(), LIMITS, result(permission_denials=[{'tool_name': 'mcp__manuscript__calculate'}])],
                  'tool call failed'),
                 ([init(), LIMITS, *tool_turn('mcp__manuscript__view_page', is_error=True), result()],
                  'tool call failed'),
                 ([init(), LIMITS, result(structured_output=None)], 'no structured object')]
        for index, (events, message) in enumerate(cases):
            with self.subTest(message=message, index=index):
                self.out = self.root / f'execution-{index}'
                self.script(events)
                with self.assertRaisesRegex(runtime.RunBlocked, message):
                    self.call()
                row = runtime.jsonl(self.out / 'usage.jsonl')[0]
                self.assertFalse(row['ok'])
                self.assertEqual(row['observed_tokens'], 42)

    def test_unapproved_tools_missing_server_errors_and_missing_usage_fail(self):
        self.tool_config()
        cases = [([init(['StructuredOutput', 'Bash']), LIMITS, result()], False, 'outside the approved'),
                 ([init(['StructuredOutput', 'mcp__other__fetch'], servers=[{'name': 'manuscript', 'status': 'connected'}]),
                   LIMITS, result()], True, 'outside the approved'),
                 ([init(servers=[{'name': 'manuscript', 'status': 'failed'}]), LIMITS, result()], True, 'did not connect'),
                 ([init(), LIMITS, result(is_error=True, subtype='error_during_execution', result='overloaded')],
                  False, 'Claude Code failed'),
                 ([init(), LIMITS, result(usage={})], False, 'did not report usage')]
        for index, (events, tools, message) in enumerate(cases):
            with self.subTest(message=message, index=index):
                self.out = self.root / f'execution-{index}'
                if tools:
                    runtime.write_json(self.out / 'workspace' / 'tool-config.json',
                                       {'paper': str(self.root / 'paper')})
                self.script(events)
                with self.assertRaisesRegex(runtime.RunBlocked, message):
                    self.call(tools=tools)

    def test_timeout_records_unknown_usage(self):
        self.script([init(), LIMITS, result()], pause=10)
        protocol = {**self.protocol, 'call_timeout_seconds': 1}
        with self.assertRaisesRegex(runtime.RunBlocked, 'timed out'):
            self.call(protocol=protocol)
        self.assertTrue(self.receipt()['usage_unknown'])

    def test_completed_interrupted_call_is_recovered_without_inference(self):
        work = self.out / 'workspace'
        work.mkdir(parents=True)
        identity = runtime.request_identity(self.protocol, 'prompt', self.schema, False, [], work)
        calls = self.out / 'calls'
        runtime.write_json(calls / f'{identity}.started.json', {
            'request_sha256': identity, 'started_at': 1, 'model_requested': MODEL, 'tools': False,
            'ok': False, 'usage_unknown': True})
        (calls / f'{identity}.events.jsonl').write_text('\n'.join(json.dumps(e) for e in (init(), LIMITS)) + '\n')
        with self.assertRaisesRegex(runtime.RunBlocked, 'no complete recoverable receipt'):
            self.call()
        (calls / f'{identity}.events.jsonl').write_text(
            '\n'.join(json.dumps(e) for e in (init(), LIMITS, result({'answer': 'recovered'}))) + '\n')
        self.assertEqual(self.call(), {'answer': 'recovered'})
        receipt = runtime.read_json(calls / f'{identity}.json')
        self.assertTrue(receipt['recovered_after_interruption'])
        self.assertEqual(receipt['observed_tokens'], 42)
        self.assertEqual(self.launched(), [])

    def test_only_pinned_claude_models_are_accepted(self):
        for model in ('claude-opus-5-5', 'claude-sonnet-5-5'):
            runtime.validate_protocol({**self.protocol, 'model': model})
        for provider, model in (('claude-code', 'claude-fable-5-1'), ('claude-code', 'claude-opus-5'),
                                ('claude-code', 'gpt-5.6-terra'), ('codex', MODEL), ('anthropic-api', MODEL)):
            with self.assertRaises(runtime.RunBlocked):
                runtime.validate_protocol({**self.protocol, 'provider': provider, 'model': model})
        self.assertFalse(self.launches.exists())

    def test_oversized_schema_never_launches(self):
        self.script([init(), LIMITS, result()])
        self.schema = {'type': 'object', 'description': 'x' * runtime.CLAUDE_SCHEMA_LIMIT}
        with self.assertRaisesRegex(runtime.RunBlocked, 'too large'):
            self.call()
        self.assertFalse(self.launches.exists())

    def test_python_escaped_patterns_compile_for_claude_with_the_same_matches(self):
        import re
        ids = ['mh-923681e3', 'r1-issue-1-part-2', 'a&b', 'x~y', 'n#1', 'sp ace']
        original = '^(?:' + '|'.join(re.escape(item) for item in ids) + ')$'
        converted = runtime.unicode_pattern(original)
        self.assertEqual(converted, '^(?:mh-923681e3|r1-issue-1-part-2|a&b|x~y|n#1|sp ace)$')
        for probe in ids + ['mh-923681e', 'r1-issue-1-part-20', 'aXb', 'mh_923681e3', '']:
            self.assertEqual(bool(re.fullmatch(original, probe)), bool(re.fullmatch(converted, probe)), probe)
        for kept in (r'^eh-[0-9a-f]{24}$', r'[a\-z]', r'\d+\.\d', r'\(x\)', r'a\|b'):
            self.assertEqual(runtime.unicode_pattern(kept), kept)
        schema = {'type': 'object', 'patternProperties': {r'^k\-1$': {'pattern': r'v\-1'}}}
        self.assertEqual(runtime.claude_schema(schema),
                         {'type': 'object', 'patternProperties': {'^k-1$': {'pattern': 'v-1'}}})
        self.assertEqual(schema['patternProperties'], {r'^k\-1$': {'pattern': r'v\-1'}})

    def test_large_manuscript_handle_pattern_reaches_claude_in_compatible_form(self):
        from peerreviewagents.engine.evidence_handles import build_registry, constrain_manuscript_handles, with_evidence_handles
        from peerreviewagents.engine.review_schema import REVIEW
        from peerreviewagents.engine.storage import digest
        blocks = [{'id': f'b{index:04d}', 'text': f'Sentence {index} reports a value.',
                   'sha256': digest(f'Sentence {index} reports a value.')} for index in range(1000)]
        self.schema = constrain_manuscript_handles(with_evidence_handles(REVIEW), build_registry({'blocks': blocks}))
        original = json.dumps(self.schema)
        self.assertIn('\\\\-', original)
        self.script([init(), LIMITS, result()])
        self.call()
        argv = self.launched()[0]['argv']
        sent = argv[argv.index('--json-schema') + 1]
        self.assertNotIn('\\\\-', sent)
        self.assertEqual(json.dumps(self.schema), original)
        pattern = json.loads(original)['$defs']['manuscriptEvidenceHandle']['pattern']
        self.assertEqual(json.loads(sent)['$defs']['manuscriptEvidenceHandle']['pattern'], pattern.replace('\\-', '-'))

    def test_codex_request_identity_is_unchanged(self):
        work = self.root / 'work'
        work.mkdir()
        codex = {**self.protocol, 'provider': 'codex', 'model': 'gpt-5.6-sol'}
        def identity(protocol):
            return runtime.digest(json.dumps({'prompt': 'p', 'schema': self.schema, 'tools': None,
                                              'protocol': protocol, 'images': []}, sort_keys=True))
        self.assertEqual(runtime.request_identity(codex, 'p', self.schema, False, [], work), identity(codex))
        self.assertNotEqual(runtime.request_identity(self.protocol, 'p', self.schema, False, [], work),
                            identity(self.protocol))


class ClaudeSubscriptionGuard(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        runtime.write_json(self.root / 'protocol.json', {'provider': 'claude-code'})
        self.status = {'loggedIn': True, 'authMethod': 'claude.ai', 'apiProvider': 'firstParty',
                       'subscriptionType': 'max'}

    def check(self, status=None, provider='claude-code'):
        runtime.write_json(self.root / 'protocol.json', {'provider': provider})
        with patch.object(runtime, 'auth_status', return_value=status or self.status):
            return runtime.require_subscription(self.root)

    def test_a_subscription_login_is_required(self):
        self.assertEqual(self.check()['authMethod'], 'claude.ai')
        for status in ({**self.status, 'loggedIn': False}, {**self.status, 'authMethod': 'api_key'},
                       {**self.status, 'apiProvider': 'bedrock'}):
            with self.subTest(status=status), self.assertRaises(runtime.RunBlocked):
                self.check(status)
        codex = {'loggedIn': True, 'authMethod': 'chatgpt'}
        self.assertEqual(self.check(codex, provider='codex')['authMethod'], 'chatgpt')
        with self.assertRaises(runtime.RunBlocked):
            self.check(codex)

    def test_nothing_gates_on_usage_or_reads_a_recorded_allowance(self):
        """How much usage a run may spend is the operator's setting, not a guard.

        The guard neither reads a recorded allowance nor writes one, so a run
        cannot be blocked, or waved through, by a file in its own directory.
        """
        runtime.write_json(self.root / 'billing_confirmation.json',
                           {'checked_at': 0, 'no_paid_credits_verified': False})
        self.assertEqual(self.check()['authMethod'], 'claude.ai')
        self.assertEqual(sorted(path.name for path in self.root.iterdir()),
                         ['billing_confirmation.json', 'protocol.json'])
        self.assertFalse(hasattr(runtime, 'write_launch_verification'))
        self.assertFalse(hasattr(runtime, 'LAUNCH_VERIFICATION'))


class ProviderDefaults(unittest.TestCase):
    COMMANDS = {'review': ['review', 'workspace', 'output'],
                'investigate': ['investigate', 'workspace', 'output', '--question', 'Does the claim hold?',
                                '--block', 'b1']}
    TARGETS = {'review': 'peerreviewagents.engine.reviewer.review',
               'investigate': 'peerreviewagents.engine.focused_investigation.investigate'}

    def selected_model(self, *options, command='review'):
        from peerreviewagents.engine.__main__ import main
        argv = ['paper-workbench', *self.COMMANDS[command],
                '--literature-cutoff', '2024-01-01', *options]
        with patch.object(sys, 'argv', argv), patch('builtins.print'), \
                patch(self.TARGETS[command], return_value={'ready_for_adjudication': False}) as call:
            main()
        return call.call_args.kwargs['model']

    def test_provider_without_a_model_uses_its_pinned_default(self):
        from peerreviewagents.engine import capabilities
        from peerreviewagents.engine.models import DEFAULT_MODELS, DEFAULT_MODEL, DEFAULT_PROVIDER, provider_for
        self.assertEqual((DEFAULT_PROVIDER, DEFAULT_MODEL), ('claude-code', 'claude-sonnet-5-5'))
        for command in self.COMMANDS:
            with self.subTest(command=command):
                self.assertEqual(self.selected_model(command=command), 'claude-sonnet-5-5')
                self.assertEqual(self.selected_model('--provider', 'codex', command=command), 'gpt-5.6-terra')
                self.assertEqual(self.selected_model('--model', 'gpt-5.6-sol', command=command), 'gpt-5.6-sol')
                self.assertEqual(self.selected_model('--provider', 'claude-code', command=command), 'claude-sonnet-5-5')
                self.assertEqual(self.selected_model('--model', 'claude-opus-5-5', command=command), 'claude-opus-5-5')
                self.assertEqual(self.selected_model('--provider', 'claude-code', '--model', 'claude-opus-5-5',
                                                     command=command), 'claude-opus-5-5')
        with patch('sys.stderr'), self.assertRaises(SystemExit):
            self.selected_model('--provider', 'claude-code', '--model', 'gpt-5.6-terra')
        self.assertEqual(capabilities()['default_models'], DEFAULT_MODELS)
        self.assertEqual({provider: provider_for(model) for provider, model in DEFAULT_MODELS.items()},
                         {provider: provider for provider in DEFAULT_MODELS})


if __name__ == '__main__':
    unittest.main()
