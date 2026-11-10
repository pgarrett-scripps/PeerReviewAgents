"""Named review policies, finding quarantine and one bounded retry.

The first test is the byte-identity proof: every existing mode, with and
without domain support, must produce exactly the prompts, schemas, tool
configs and protocol records it produced before review limits became data.
The digests below were captured from the engine at commit 1658fd2.
"""
import copy
import hashlib
import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from peerreviewagents.engine import ensemble, model_runtime as runtime, reviewer
from peerreviewagents.engine import review_schema
from peerreviewagents.engine.annotations import quarantine_findings
from peerreviewagents.engine.api import ReviewRequest, capabilities, review_request_from_preset
from peerreviewagents.engine.config import get_review_preset, resolved_review_config, review_presets
from peerreviewagents.engine.evidence_handles import build_registry
from peerreviewagents.engine.review_policy import (AGENTIC, AGENTIC_SESSION, DEFAULT_POLICY_NAME,
                                                   POLICY_VERSION, STANDARD, ReviewPolicy,
                                                   get_review_policy, review_policies)
from peerreviewagents.engine.storage import digest, read, write

PAPER = {
    'id': 'paper', 'title': 'An experiment', 'source_sha256': 'source',
    'blocks': [{'id': 'b1', 'text': 'We studied 5 plants.', 'sha256': digest('We studied 5 plants.')}],
    'sections': [], 'assets': [], 'references': [], 'statistics': {'total_words': 4},
}

REPEATED_TEXT = 'We studied 5 plants. We studied 5 plants. The yield rose by 12 percent.'
REPEATED_PAPER = {
    'id': 'paper', 'title': 'An experiment', 'source_sha256': 'source',
    'blocks': [{'id': 'b1', 'text': REPEATED_TEXT, 'sha256': digest(REPEATED_TEXT)}],
    'sections': [], 'assets': [], 'references': [], 'statistics': {'total_words': 13},
}

MODES = ('plain', 'workspace', 'self-revision', 'independent')

EXPECTED_DIGESTS = {
    'independent|domains=False': '07756cb0d32858851f4e94bf1143d9b8c2ffcc3726d01b8a160b546f555f602c',
    'independent|domains=True': '4a2e9c67b5a1686d069ced6b1103d675fa89d2c815e7da3c52c09ce6c7e08a72',
    'plain|domains=False': '8838ce808b61d6049b8a1785c9cf0065b8cdbbbc301149bb00dc033f0561579c',
    'plain|domains=True': 'cd2e162e9f99a4434350d536833bd02808c6954effc17bfa7cdc39dada38cfe6',
    'self-revision|domains=False': 'ee2172670f8c3a69ff6e7a5fcb1042ae8cc18420122d9cef9861884047c4edf8',
    'self-revision|domains=True': '4ccce1bf05cb76bd789002346f2d7f3cf2a67dfcde1c4dc7188d40d6f19af333',
    'workspace|domains=False': 'a49958d043bb2cd43f7ef09ac92b1a7501e9646db8a1f2402bda0a05917bc53b',
    'workspace|domains=True': '4ba3db7b8b5edd77ecf8e4e47813f17859186c8da4440a427435de7bb9db3ff5',
}


def finding(handle, title='Count'):
    return {'title': title, 'kind': 'question', 'severity': 'moderate',
            'comment': 'Confirm the count.', 'replacement': '', 'counterevidence': '',
            'source_urls': [], 'source_evidence': [], 'anchor_handle': handle,
            'manuscript_evidence_handles': [handle]}


def model_response(*handles, titles=None):
    """One model answer. The default title matches the byte-identity baseline."""
    names = titles or ['Count'] * len(handles)
    return {'summary': 'Review',
            'findings': [finding(handle, name) for handle, name in zip(handles, names)],
            'limitations': [], 'investigation_summary': 'Checked.', 'discarded_hypotheses': []}


def workspace(directory, paper=PAPER):
    """Write one minimal verified manuscript workspace and a billing record."""
    root = Path(directory) / 'paper'
    write(root / 'paper.json', paper)
    (root / 'manuscript.md').write_text(paper['blocks'][0]['text'])
    write(root / 'manifest.json', {'files': {
        name: digest((root / name).read_bytes()) for name in ('paper.json', 'manuscript.md')}})
    billing = Path(directory) / 'billing.json'
    write(billing, {'checked_at': time.time(), 'no_paid_credits_verified': True})
    return root, Path(directory) / 'run', billing


def run_review(directory, mode='plain', domain_support=True, policy=None, paper=PAPER,
               response=None, **kwargs):
    """Run one review against a fake transport and capture every model call."""
    root, out, billing = workspace(directory, paper)
    handle = build_registry(paper).handles[0].handle_id
    value = response if response is not None else model_response(handle)
    captured = []

    def fake(protocol, execution, prompt, schema, **options):
        captured.append({'prompt': prompt, 'schema': schema, 'protocol': protocol,
                         'tool_config': read(execution / 'workspace' / 'tool-config.json'),
                         'label': options.get('label'), 'tools': options.get('tools')})
        return copy.deepcopy(value)

    with patch.object(reviewer, 'require_subscription'), \
         patch.object(reviewer, 'model_call', side_effect=fake), \
         patch.object(reviewer, 'inspected_images', return_value=[]):
        reviewer.review(root, out, billing, '2025-01-01', mode=mode, domain_support=domain_support,
                        model='gpt-5.6-terra', policy=policy, **kwargs)
    return captured, out, root


def scenario_digest(mode, domain_support):
    with tempfile.TemporaryDirectory() as directory:
        captured, out, root = run_review(directory, mode, domain_support)
        text = json.dumps(captured, sort_keys=True, ensure_ascii=False)
        for path in (str(out), str(root), directory):
            text = text.replace(path, '<tmp>')
        return hashlib.sha256(text.encode()).hexdigest()


class DefaultPolicyIsUnchanged(unittest.TestCase):
    def test_every_existing_mode_sends_byte_identical_prompts_and_limits(self):
        for mode in MODES:
            for domain_support in (True, False):
                key = f'{mode}|domains={domain_support}'
                with self.subTest(scenario=key):
                    self.assertEqual(scenario_digest(mode, domain_support), EXPECTED_DIGESTS[key])

    def test_standard_policy_reproduces_the_shared_instructions_and_schema(self):
        self.assertEqual(STANDARD.shared_instructions(), review_schema.COMMON)
        self.assertEqual(STANDARD.final_review_schema(), review_schema.capped_findings())
        self.assertEqual(STANDARD.final_review_schema()['properties']['findings']['maxItems'], 5)
        self.assertEqual(STANDARD.protocol_overlay(), {})
        self.assertIs(get_review_policy(None), STANDARD)
        self.assertIs(get_review_policy(DEFAULT_POLICY_NAME), STANDARD)

    def test_default_ensemble_protocol_record_is_explicit_and_unchanged(self):
        self.assertEqual(ensemble._protocol('gpt-5.6-terra', 2, 750000), {
            'provider': 'codex', 'model': 'gpt-5.6-terra', 'effort': 'medium',
            'held_out_runs_authorized': False, 'max_calls_per_condition': 1,
            'observed_token_stop': 750000, 'call_timeout_seconds': 900,
            'max_source_images': 10, 'image_budget_version': 'evidence-v1', 'mode': 'ensemble',
            'reviewers': 2, 'total_generation_calls': 3, 'token_launch_budget': 750000,
            'independent_tool_allowance': 4, 'debate_tool_allowance': 2, 'version': 'ensemble-v3',
        })
        self.assertEqual(ensemble._protocol('gpt-5.6-terra', 2, 750000, 'standard-v1'),
                         ensemble._protocol('gpt-5.6-terra', 2, 750000))

    def test_every_other_preset_keeps_the_standard_policy(self):
        for name, record in review_presets().items():
            if name != 'workspace-agentic-v1':
                self.assertEqual(record['policy'], 'standard-v1')

    def test_an_unknown_policy_is_refused(self):
        with self.assertRaisesRegex(ValueError, 'Unknown review policy'):
            get_review_policy('workspace-agentic-v2')
        with self.assertRaisesRegex(ValueError, 'effort'):
            ReviewPolicy(name='bad', effort='exhaustive')
        with self.assertRaisesRegex(ValueError, 'bounded retry'):
            ReviewPolicy(name='bad', max_attempts=3)


class AgenticPolicy(unittest.TestCase):
    def test_the_preset_is_one_long_high_effort_session_without_a_finding_cap(self):
        preset = get_review_preset('workspace-agentic-v1')
        self.assertEqual((preset.workflow, preset.reviewers, preset.policy),
                         ('independent', 1, 'workspace-agentic-v1'))
        self.assertIs(get_review_policy('workspace-agentic-v1'), AGENTIC)
        self.assertEqual(AGENTIC.effort, 'high')
        self.assertGreaterEqual(AGENTIC.investigator_tool_limit('independent'), 30)
        self.assertGreaterEqual(AGENTIC.investigator_tool_limit('plain', domain_support=False), 30)
        self.assertGreater(AGENTIC.call_timeout_seconds, STANDARD.call_timeout_seconds)
        self.assertIsNone(AGENTIC.finding_cap)
        self.assertIsNone(AGENTIC.candidate_finding_cap)
        self.assertNotIn('maxItems', AGENTIC.final_review_schema()['properties']['findings'])
        self.assertNotIn('maxItems', AGENTIC.candidate_review_schema()['properties']['findings'])
        self.assertEqual(AGENTIC.max_attempts, 2)
        self.assertEqual(AGENTIC.protocol_overlay(), {
            'review_policy': 'workspace-agentic-v1', 'review_policy_version': POLICY_VERSION,
            'max_attempts': 2, 'review_policy_record': AGENTIC.record()})

    def test_an_agentic_run_sends_the_session_instruction_and_the_wider_limits(self):
        with tempfile.TemporaryDirectory() as directory:
            captured, _, _ = run_review(directory, mode='plain', policy='workspace-agentic-v1')
        self.assertEqual(len(captured), 1)
        call = captured[0]
        self.assertTrue(call['prompt'].startswith(AGENTIC_SESSION))
        self.assertIn('At most 30 tool calls.', call['prompt'])
        self.assertNotIn('at most five findings', call['prompt'])
        self.assertEqual(call['tool_config']['tool_limit'], 30)
        self.assertEqual(call['protocol']['effort'], 'high')
        self.assertEqual(call['protocol']['call_timeout_seconds'], 7200)
        self.assertEqual(call['protocol']['investigator_tool_limit'], 30)
        self.assertEqual(call['protocol']['max_attempts'], 2)
        self.assertEqual(call['protocol']['review_policy'], 'workspace-agentic-v1')
        self.assertNotIn('maxItems', call['schema']['properties']['findings'])
        # Quote validation is untouched: every finding still references one of the
        # finite manuscript handles, so a quote can only come from the manuscript.
        items = call['schema']['properties']['findings']['items']
        self.assertEqual(items['properties']['anchor_handle'],
                         {'$ref': '#/$defs/manuscriptEvidenceHandle'})
        self.assertEqual(call['schema']['$defs']['manuscriptEvidenceHandle']['enum'],
                         [handle.handle_id for handle in build_registry(PAPER).handles])
        self.assertIn('anchor_handle', items['required'])

    def test_the_api_exposes_the_preset_and_the_policy(self):
        advertised = capabilities()
        self.assertIn('workspace-agentic-v1', advertised['presets'])
        self.assertIn('workspace-agentic-v1', advertised['policies'])
        self.assertEqual(advertised['default_policy'], 'standard-v1')
        self.assertEqual(advertised['review_policies'], review_policies())
        self.assertEqual(advertised['retry_policy']['max_attempts_ceiling'], 2)
        self.assertEqual(advertised['reasoning_efforts'], ['low', 'medium', 'high'])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            request = review_request_from_preset(
                'workspace-agentic-v1', workspace=root / 'paper', output=root / 'run',
                billing_confirmation=root / 'billing.json', literature_cutoff='2026-09-25')
            self.assertEqual((request.workflow, request.reviewers, request.policy),
                             ('independent', 1, 'workspace-agentic-v1'))
            self.assertEqual(request.token_budget, 1500000)
            self.assertEqual(resolved_review_config(request)['policy'], 'workspace-agentic-v1')
            explicit = ReviewRequest(workspace=root / 'paper', output=root / 'run',
                                     billing_confirmation=root / 'billing.json',
                                     literature_cutoff='2026-09-25', workflow='independent',
                                     reviewers=1, policy='workspace-agentic-v1')
            self.assertEqual(resolved_review_config(explicit)['policy'], 'workspace-agentic-v1')
            with self.assertRaisesRegex(ValueError, 'Unknown review policy'):
                ReviewRequest(workspace=root / 'paper', output=root / 'run',
                              billing_confirmation=root / 'billing.json',
                              literature_cutoff='2026-09-25', policy='nonexistent-v1')

    def test_the_cli_exposes_the_preset_and_the_policy(self):
        from peerreviewagents.engine.__main__ import apply_preset, build_parser
        parser = build_parser()
        base = ['review', 'paper', 'run', '--billing-confirmation', 'b.json',
                '--literature-cutoff', '2026-09-25']
        args = parser.parse_args(base + ['--preset', 'workspace-agentic-v1'])
        apply_preset(parser, args)
        self.assertEqual((args.mode, args.reviewers, args.token_budget, args.policy),
                         ('independent', 1, 1500000, 'workspace-agentic-v1'))
        direct = parser.parse_args(base + ['--policy', 'workspace-agentic-v1'])
        self.assertEqual(direct.policy, 'workspace-agentic-v1')
        apply_preset(parser, direct)
        self.assertEqual(direct.mode, 'ensemble')
        self.assertIsNone(parser.parse_args(base).policy)
        with self.assertRaises(SystemExit):
            apply_preset(parser, parser.parse_args(
                base + ['--preset', 'workspace-agentic-v1', '--reviewers', '3']))
        with self.assertRaises(SystemExit):
            apply_preset(parser, parser.parse_args(
                base + ['--preset', 'manuscript-only-single-v1']))


class QuarantinedFindings(unittest.TestCase):
    def test_an_unanchored_finding_is_quarantined_with_a_reason(self):
        kept, quarantined = quarantine_findings([
            {'id': 'a', 'placement': 'anchored', 'invalid_evidence_blocks': []},
            {'id': 'b', 'placement': 'unplaced', 'invalid_evidence_blocks': [],
             'placement_reason': 'Quote is absent or ambiguous in the selected source block'},
            {'id': 'c', 'placement': 'anchored', 'invalid_evidence_blocks': ['b9']},
        ])
        self.assertEqual([row['id'] for row in kept], ['a'])
        self.assertEqual([row['id'] for row in quarantined], ['b', 'c'])
        self.assertEqual(quarantined[0]['quarantine_reason'],
                         'Quote is absent or ambiguous in the selected source block')
        self.assertIn('b9', quarantined[1]['quarantine_reason'])
        self.assertTrue(all(row['quarantined'] and row['issue_status'] == 'quarantined'
                            for row in quarantined))

    def hydration(self, *findings):
        """A hydration layer that lets an unanchorable quote reach the guard.

        The handle registry refuses an ambiguous anchor outright, so the only
        way a defective finding reaches ``normalize_review`` in production is a
        hydration or schema regression. This stands in for one.
        """
        def fake(result, registry):
            return {'response': {'summary': 'Review', 'findings': list(findings),
                                 'limitations': []},
                    'audit': {'version': 'test', 'transformations': []}}
        return patch.object(reviewer, 'hydrate_review_response', side_effect=fake)

    def unanchorable(self, title='Finding 1'):
        return {'title': title, 'kind': 'question', 'severity': 'moderate',
                'comment': 'Confirm the count.', 'block_id': 'b1',
                'quote': 'We studied 5 plants.', 'evidence_blocks': ['b1', 'b9']}

    def anchored(self, title='Finding 2'):
        return {'title': title, 'kind': 'question', 'severity': 'moderate',
                'comment': 'Confirm the yield.', 'block_id': 'b1',
                'quote': 'The yield rose by 12 percent.', 'evidence_blocks': ['b1']}

    def test_one_invalid_finding_does_not_end_the_run(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.hydration(self.unanchorable(), self.anchored()):
                captured, out, _ = run_review(directory, mode='plain', paper=REPEATED_PAPER)
            self.assertEqual(len(captured), 1)
            guarded = read(out / 'responses' / 'plain-guarded.json')
            quarantine = read(out / 'responses' / 'plain-quarantined.json')
            self.assertEqual(read(out / 'progress.json')['status'], 'complete')
        self.assertEqual(guarded['review_guard_status'], 'partially-quarantined')
        self.assertEqual([row['title'] for row in guarded['findings']], ['Finding 2'])
        self.assertEqual([row['title'] for row in guarded['quarantined_findings']], ['Finding 1'])
        self.assertEqual(quarantine['kept_findings'], 1)
        reason = quarantine['quarantined_findings'][0]['quarantine_reason']
        self.assertIn('ambiguous', reason)
        self.assertIn('b9', reason)
        self.assertTrue(guarded['quarantine_policy'])

    def test_every_finding_quarantined_is_recorded_rather_than_raised(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.hydration(self.unanchorable()):
                _, out, _ = run_review(directory, mode='plain', paper=REPEATED_PAPER)
            guarded = read(out / 'responses' / 'plain-guarded.json')
            self.assertEqual(read(out / 'progress.json')['status'], 'complete')
        self.assertEqual(guarded['review_guard_status'], 'all-findings-quarantined')
        self.assertEqual(guarded['findings'], [])
        self.assertEqual(len(guarded['quarantined_findings']), 1)

    def test_the_registry_still_refuses_an_ambiguous_anchor_outright(self):
        registry = build_registry(REPEATED_PAPER)
        ambiguous = next(h for h in registry.handles if h.text == 'We studied 5 plants.')
        self.assertFalse(ambiguous.anchor_eligible)
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, 'ambiguous'):
                run_review(directory, mode='plain', paper=REPEATED_PAPER,
                           response=model_response(ambiguous.handle_id))


class BoundedRetry(unittest.TestCase):
    def setUp(self):
        self.protocol = {'provider': 'codex', 'model': 'gpt-5.6-terra', 'effort': 'medium',
                         'held_out_runs_authorized': False, 'max_calls_per_condition': 1,
                         'observed_token_stop': 1000, 'call_timeout_seconds': 30,
                         'max_attempts': 2}
        self.schema = {'type': 'object'}

    def succeed(self, command, **kwargs):
        Path(command[command.index('--output-last-message') + 1]).write_text('{"answer": "ok"}')
        kwargs['stdout'].write(json.dumps({'type': 'turn.completed', 'usage': {
            'input_tokens': 7, 'output_tokens': 3}}) + '\n')
        return SimpleNamespace(returncode=0)

    def fail(self, command, **kwargs):
        kwargs['stdout'].write(json.dumps({'type': 'turn.completed', 'usage': {
            'input_tokens': 4, 'output_tokens': 1}}) + '\n')
        kwargs['stdout'].write(json.dumps({'type': 'error', 'message': 'failure'}) + '\n')
        return SimpleNamespace(returncode=1)

    def timeout(self, command, **kwargs):
        raise subprocess.TimeoutExpired(cmd='codex', timeout=30)

    def sequence(self, *behaviours):
        calls = []

        def run(command, **kwargs):
            calls.append(command)
            return behaviours[len(calls) - 1](command, **kwargs)

        return run, calls

    def test_a_failed_call_is_retried_once_in_the_same_run(self):
        run, calls = self.sequence(self.fail, self.succeed)
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            with patch.object(runtime.subprocess, 'run', side_effect=run):
                self.assertEqual(runtime.model_call(self.protocol, out, 'prompt', self.schema),
                                 {'answer': 'ok'})
            self.assertEqual(len(calls), 2)
            rows = runtime.jsonl(out / 'usage.jsonl')
            self.assertEqual([row['attempt'] for row in rows], [1, 2])
            self.assertEqual([row['ok'] for row in rows], [False, True])
            usage = ensemble.observed_usage(out)
            self.assertEqual((usage['calls'], usage['receipts'], usage['retried_attempts']), (2, 2, 1))
            self.assertEqual(usage['observed_tokens'], 15)
            self.assertFalse(usage['unknown'])
            stems = sorted(path.name for path in (out / 'calls').glob('*.json'))
            self.assertTrue(any('.attempt2.json' in name for name in stems))

    def test_a_timed_out_call_is_retried_once(self):
        run, calls = self.sequence(self.timeout, self.succeed)
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            with patch.object(runtime.subprocess, 'run', side_effect=run):
                self.assertEqual(runtime.model_call(self.protocol, out, 'prompt', self.schema),
                                 {'answer': 'ok'})
            self.assertEqual(len(calls), 2)
            self.assertEqual([row['attempt'] for row in runtime.jsonl(out / 'usage.jsonl')], [1, 2])

    def test_a_second_failure_blocks_the_run(self):
        run, calls = self.sequence(self.fail, self.fail)
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            with patch.object(runtime.subprocess, 'run', side_effect=run):
                with self.assertRaises(runtime.StudyBlocked):
                    runtime.model_call(self.protocol, out, 'prompt', self.schema)
                with self.assertRaisesRegex(runtime.StudyBlocked, 'prior attempt failed'):
                    runtime.model_call(self.protocol, out, 'prompt', self.schema)
            self.assertEqual(len(calls), 2)
            self.assertEqual([row['attempt'] for row in runtime.jsonl(out / 'usage.jsonl')], [1, 2])

    def test_the_default_policy_never_retries(self):
        run, calls = self.sequence(self.fail, self.succeed)
        protocol = {key: value for key, value in self.protocol.items() if key != 'max_attempts'}
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            with patch.object(runtime.subprocess, 'run', side_effect=run):
                with self.assertRaises(runtime.StudyBlocked):
                    runtime.model_call(protocol, out, 'prompt', self.schema)
            self.assertEqual(len(calls), 1)

    def test_a_guard_refusal_is_never_retried(self):
        run, calls = self.sequence(self.succeed, self.succeed)
        protocol = {**self.protocol, 'observed_token_stop': 1, 'max_calls_per_condition': 5}
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            write(out / 'usage.jsonl', {})
            (out / 'usage.jsonl').write_text(json.dumps({
                'request_sha256': 'other', 'observed_tokens': 50, 'attempt': 1,
                'ok': True, 'usage_unknown': False}) + '\n')
            with patch.object(runtime.subprocess, 'run', side_effect=run):
                with self.assertRaisesRegex(runtime.StudyBlocked, 'token'):
                    runtime.model_call(protocol, out, 'prompt', self.schema)
            self.assertEqual(len(calls), 0)


class BillingVerifiedOnce(unittest.TestCase):
    def stale_codex_record(self):
        return {'policy': 'included-allowance-v1', 'checked_at': time.time() - 600,
                'source': 'Codex app get_usage_limits', 'purchases_authorized': False,
                'resets_authorized': False, 'usage_snapshot': {'rateLimits': {}}}

    def test_a_later_stage_does_not_recheck_a_record_past_the_codex_window(self):
        from peerreviewagents.engine.billing import verify_billing
        record = self.stale_codex_record()
        with self.assertRaisesRegex(ValueError, 'five minutes'):
            verify_billing(record, provider='codex')
        protocol = ensemble._protocol('gpt-5.6-terra', 2, 750000)
        with tempfile.TemporaryDirectory() as directory:
            billing = Path(directory) / 'billing.json'
            write(billing, record)
            with patch.object(ensemble, 'require_subscription'):
                with self.assertRaisesRegex(ValueError, 'five minutes'):
                    ensemble._prepare(Path(directory) / 'unverified', billing, protocol)
                staged = Path(directory) / 'verified'
                ensemble._prepare(staged, billing, protocol, verified=True)
            self.assertEqual(read(staged / 'billing_confirmation.json'), record)

    def test_a_reviewer_stage_trusts_the_launch_verification(self):
        from peerreviewagents.engine import billing as billing_module
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(billing_module, 'verify_billing') as verify:
                run_review(directory, mode='plain', billing_verified=True)
            verify.assert_not_called()
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(billing_module, 'verify_billing') as verify:
                run_review(directory, mode='plain')
            self.assertEqual(verify.call_count, 1)


if __name__ == '__main__':
    unittest.main()
