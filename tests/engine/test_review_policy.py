"""Named review policies, finding quarantine and one bounded retry.

The first tests are the byte-identity proof: every existing mode and stage,
with and without domain support, must produce exactly the prompts, schemas,
tool configs and protocol records it produced before review limits became
data. The digests below were captured from the engine at commit 1658fd2.
"""
import contextlib
import copy
import hashlib
import io
import json
import re
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
from peerreviewagents.engine.baseline import run_manuscript_only
from peerreviewagents.engine.config import get_review_preset, resolved_review_config, review_presets
from peerreviewagents.engine.debate_revision import debate_revision_schema, revision_prompt
from peerreviewagents.engine.ensemble_debate import NEW_CANDIDATE_LIMIT, ensemble_schema
from peerreviewagents.engine.evidence_handles import build_registry
from peerreviewagents.engine.investigation_policy import with_tool_checks
from peerreviewagents.engine.models import DEFAULT_MODEL, DEFAULT_PROVIDER
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

# The panel, ensemble debate and manuscript-only stages, captured the same way.
# The run id is the digest of the run manifest, so it changes with any code
# change and is normalized out: the frozen text is everything else.
RUN_ID = re.compile(r'(?<=[a-z])-[0-9a-f]{10}\b')

STAGE_DIGESTS = {
    'panel|domains=True': '3a6b831cf6f4f686645add47c276ac5b7ae264a5a202136d514657bd12174764',
    'panel|domains=False': 'ab36739b95ac2d6c8a6b72d90636ddd63b4950248973e8c189c320e0e3d82b09',
    'ensemble-debate': 'eea3881f7d43439448b3248e4ed7b59d5216620da79ec75779c931990c4173fd',
    'manuscript-only': '827799000d7b7a6d5b4c4eba5946e0f0d00fc924c3f9df350bdcecf6b58e12c7',
}

STAGE_CALLS = {'panel|domains=True': 2, 'panel|domains=False': 2,
               'ensemble-debate': 1, 'manuscript-only': 1}


class Captured(Exception):
    """Stop a run once the stage under test has been recorded."""


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
    """Write one minimal verified manuscript workspace."""
    root = Path(directory) / 'paper'
    write(root / 'paper.json', paper)
    (root / 'manuscript.md').write_text(paper['blocks'][0]['text'])
    write(root / 'manifest.json', {'files': {
        name: digest((root / name).read_bytes()) for name in ('paper.json', 'manuscript.md')}})
    return root, Path(directory) / 'run'


def run_review(directory, mode='plain', domain_support=True, policy=None, paper=PAPER,
               response=None, **kwargs):
    """Run one review against a fake transport and capture every model call."""
    root, out = workspace(directory, paper)
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
        reviewer.review(root, out, '2025-01-01', mode=mode, domain_support=domain_support,
                        model='gpt-5.6-terra', policy=policy, **kwargs)
    return captured, out, root


def record_stage(captured, protocol, execution, prompt, schema, **options):
    """Record one model call. A tool-free stage writes no tool config at all."""
    config = execution / 'workspace' / 'tool-config.json'
    captured.append({'prompt': prompt, 'schema': schema, 'protocol': protocol,
                     'tool_config': read(config) if config.exists() else None,
                     'label': options.get('label'), 'tools': options.get('tools')})


def normalize(captured, paths):
    text = json.dumps(captured, sort_keys=True, ensure_ascii=False)
    for path in paths:
        text = text.replace(str(path), '<tmp>')
    return RUN_ID.sub('-<run>', text)


def panel_payload(directory, domain_support=True):
    """Both panel stages: the workspace inventory call and the debate call."""
    root, out = workspace(directory)
    handle = build_registry(PAPER).handles[0].handle_id
    captured = []

    def fake(protocol, execution, prompt, schema, **options):
        record_stage(captured, protocol, execution, prompt, schema, **options)
        if options.get('label') == 'debate':
            raise Captured()
        return copy.deepcopy(model_response(handle))

    with patch.object(reviewer, 'require_subscription'), \
         patch.object(reviewer, 'model_call', side_effect=fake), \
         patch.object(reviewer, 'inspected_images', return_value=[]):
        with contextlib.suppress(Captured):
            reviewer.review(root, out, '2025-01-01', mode='panel',
                            domain_support=domain_support, model='gpt-5.6-terra')
    return captured, (out, root, directory)


def debate_payload(directory, policy=None):
    """The ensemble synthesis call over two recorded independent reviews."""
    root, out = workspace(directory)
    captured = []
    base = {'title': 'Count', 'kind': 'question', 'severity': 'moderate', 'block_id': 'b1',
            'quote': 'We studied 5 plants.', 'evidence_blocks': ['b1'], 'replacement': '',
            'counterevidence': '', 'source_urls': []}
    records = [{'review': {'findings': [{**base, 'comment': f'Confirm the count {index}.'}]},
                'provenance': {'paper_id': 'paper', 'origin': 'test'}} for index in (1, 2)]

    def fake(protocol, execution, prompt, schema, **options):
        record_stage(captured, protocol, execution, prompt, schema, **options)
        raise Captured()

    with patch.object(ensemble, 'require_subscription'), \
         patch.object(ensemble, 'model_call', side_effect=fake):
        with contextlib.suppress(Captured):
            ensemble.debate_reviews(root, out, '2025-01-01', records,
                                    model='gpt-5.6-terra', policy=policy)
    return captured, (out, root, directory)


def manuscript_only_payload(directory):
    """The single tool-free manuscript-only call."""
    root, out = workspace(directory)
    captured = []

    def fake(protocol, execution, prompt, schema, **options):
        record_stage(captured, protocol, execution, prompt, schema, **options)
        raise Captured()

    request = ReviewRequest(workspace=root, output=out,
                            literature_cutoff='2025-01-01', workflow='manuscript-only',
                            reviewers=1, model='gpt-5.6-terra')
    with patch.object(ensemble, 'require_subscription'), \
         patch.object(runtime, 'model_call', side_effect=fake):
        with contextlib.suppress(Captured):
            run_manuscript_only(request, PAPER, resolved_review_config(request))
    return captured, (out, root, directory)


STAGE_SCENARIOS = {
    'panel|domains=True': lambda directory: panel_payload(directory, True),
    'panel|domains=False': lambda directory: panel_payload(directory, False),
    'ensemble-debate': debate_payload,
    'manuscript-only': manuscript_only_payload,
}


def scenario_digest(mode, domain_support):
    with tempfile.TemporaryDirectory() as directory:
        captured, out, root = run_review(directory, mode, domain_support)
        text = json.dumps(captured, sort_keys=True, ensure_ascii=False)
        for path in (str(out), str(root), directory):
            text = text.replace(path, '<tmp>')
        return hashlib.sha256(text.encode()).hexdigest()


def stage_digest(scenario):
    with tempfile.TemporaryDirectory() as directory:
        captured, paths = STAGE_SCENARIOS[scenario](directory)
        return len(captured), hashlib.sha256(normalize(captured, paths).encode()).hexdigest()


class DefaultPolicyIsUnchanged(unittest.TestCase):
    def test_every_existing_mode_sends_byte_identical_prompts_and_limits(self):
        for mode in MODES:
            for domain_support in (True, False):
                key = f'{mode}|domains={domain_support}'
                with self.subTest(scenario=key):
                    self.assertEqual(scenario_digest(mode, domain_support), EXPECTED_DIGESTS[key])

    def test_the_panel_debate_and_manuscript_only_stages_are_byte_identical_too(self):
        """The modes above are not every stage: panel, ensemble debate and the
        tool-free baseline each route their own limits through the policy."""
        for scenario in STAGE_SCENARIOS:
            with self.subTest(scenario=scenario):
                calls, value = stage_digest(scenario)
                self.assertEqual(calls, STAGE_CALLS[scenario])
                self.assertEqual(value, STAGE_DIGESTS[scenario])

    def test_standard_policy_reproduces_the_shared_instructions_and_schema(self):
        self.assertEqual(STANDARD.shared_instructions(), review_schema.COMMON)
        self.assertEqual(STANDARD.final_review_schema(), review_schema.capped_findings())
        self.assertEqual(STANDARD.final_review_schema()['properties']['findings']['maxItems'], 5)
        self.assertEqual(STANDARD.protocol_overlay(), {})
        self.assertIs(get_review_policy(None), STANDARD)
        self.assertIs(get_review_policy(DEFAULT_POLICY_NAME), STANDARD)
        self.assertEqual(debate_revision_schema('standard-v1'), debate_revision_schema())
        self.assertEqual(ensemble_schema('standard-v1'), ensemble_schema())
        self.assertEqual(with_tool_checks(STANDARD.candidate_review_schema(), STANDARD.tool_checks),
                         with_tool_checks(STANDARD.candidate_review_schema()))

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

    def test_every_bounded_request_limit_is_policy_data(self):
        self.assertEqual((AGENTIC.external_requests, AGENTIC.debate_external_requests,
                          AGENTIC.tool_checks), (24, 8, 30))
        self.assertEqual((STANDARD.external_requests, STANDARD.debate_external_requests,
                          STANDARD.tool_checks), (6, 2, 4))
        for name in ('external_requests', 'debate_external_requests', 'tool_checks'):
            self.assertEqual(AGENTIC.record()[name], getattr(AGENTIC, name))
        standard = with_tool_checks(STANDARD.candidate_review_schema(), STANDARD.tool_checks)
        agentic = with_tool_checks(AGENTIC.candidate_review_schema(), AGENTIC.tool_checks)
        self.assertEqual(standard['properties']['tool_checks']['maxItems'], 4)
        self.assertEqual(agentic['properties']['tool_checks']['maxItems'], 30)

    def test_an_agentic_independent_run_carries_the_wider_external_allowance(self):
        with tempfile.TemporaryDirectory() as directory:
            captured, _, _ = run_review(directory, mode='independent',
                                        policy='workspace-agentic-v1')
        call = captured[0]
        self.assertEqual(call['tool_config']['external_request_limit'], 24)
        self.assertIn('24 external requests are shared', call['prompt'])
        self.assertEqual(call['schema']['properties']['tool_checks']['maxItems'], 30)
        with tempfile.TemporaryDirectory() as directory:
            captured, _, _ = run_review(directory, mode='independent')
        self.assertEqual(captured[0]['tool_config']['external_request_limit'], 6)
        self.assertIn('Six external requests are shared', captured[0]['prompt'])
        self.assertEqual(captured[0]['schema']['properties']['tool_checks']['maxItems'], 4)

    def test_the_agentic_debate_stage_drops_the_cap_and_widens_the_budget(self):
        ledger = {'version': 'debate-ledger-v3', 'issues': [], 'investigation_context': {}}
        prompt = revision_prompt(PAPER, 'We studied 5 plants.', ledger,
                                 policy='workspace-agentic-v1')
        self.assertIn('at most 6000 words', prompt)
        self.assertNotIn('at most five', prompt)
        self.assertIn('at most eight optional source tool calls', prompt)
        schema = debate_revision_schema('workspace-agentic-v1')
        self.assertNotIn('maxItems', schema['properties']['findings'])
        pooled = ensemble_schema('workspace-agentic-v1')
        self.assertNotIn('maxItems', pooled['properties']['findings'])
        # The reserved new-candidate identifier space is not a review limit: the
        # ledger only ever offers new-1..new-5, so this cap stays under both policies.
        self.assertEqual(NEW_CANDIDATE_LIMIT, 5)
        for policy in (None, 'workspace-agentic-v1'):
            self.assertEqual(ensemble_schema(policy)['properties']['new_candidates']['maxItems'],
                             NEW_CANDIDATE_LIMIT)

    def test_the_agentic_debate_stage_carries_the_wider_source_allowance(self):
        with tempfile.TemporaryDirectory() as directory:
            captured, _ = debate_payload(directory, policy='workspace-agentic-v1')
        self.assertEqual(captured[0]['tool_config']['external_request_limit'], 8)
        self.assertEqual(captured[0]['tool_config']['tool_limit'], 8)
        with tempfile.TemporaryDirectory() as directory:
            captured, _ = debate_payload(directory)
        self.assertEqual(captured[0]['tool_config']['external_request_limit'], 2)
        self.assertEqual(captured[0]['tool_config']['tool_limit'], 2)

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
                literature_cutoff='2026-09-25')
            self.assertEqual((request.workflow, request.reviewers, request.policy),
                             ('independent', 1, 'workspace-agentic-v1'))
            self.assertEqual(request.token_budget, 1500000)
            self.assertEqual(resolved_review_config(request)['policy'], 'workspace-agentic-v1')
            explicit = ReviewRequest(workspace=root / 'paper', output=root / 'run',
                                     literature_cutoff='2026-09-25', workflow='independent',
                                     reviewers=1, policy='workspace-agentic-v1')
            self.assertEqual(resolved_review_config(explicit)['policy'], 'workspace-agentic-v1')
            with self.assertRaisesRegex(ValueError, 'Unknown review policy'):
                ReviewRequest(workspace=root / 'paper', output=root / 'run',
                              literature_cutoff='2026-09-25', policy='nonexistent-v1')

    def test_the_api_advertises_the_retry_and_guard_policy_per_policy(self):
        advertised = capabilities()
        self.assertEqual(advertised['reasoning_effort'], STANDARD.effort)
        self.assertEqual(advertised['reasoning_effort_by_policy'],
                         {'standard-v1': 'medium', 'workspace-agentic-v1': 'high'})
        self.assertFalse(advertised['automatic_retries'])
        self.assertEqual(advertised['automatic_retries_by_policy'],
                         {'standard-v1': False, 'workspace-agentic-v1': True})
        self.assertEqual(advertised['retry_policy']['default_attempts'], 1)
        self.assertEqual(advertised['retry_policy']['attempts_by_policy'],
                         {'standard-v1': 1, 'workspace-agentic-v1': 2})
        self.assertIn('timed out', advertised['retry_policy']['retried'])
        self.assertIn('refusal', advertised['retry_policy']['never_retried'])
        self.assertTrue(advertised['retry_policy']['per_attempt_receipts'])
        self.assertEqual(advertised['review_guard_statuses'],
                         ['clean', 'partially-quarantined', 'all-findings-quarantined'])
        self.assertEqual(advertised['zero_surviving_findings_status'],
                         reviewer.COMPLETE_WITHOUT_FINDINGS)

    def test_the_cli_exposes_the_preset_and_the_policy(self):
        from peerreviewagents.engine.__main__ import apply_preset, build_parser
        parser = build_parser()
        base = ['review', 'paper', 'run', '--literature-cutoff', '2026-09-25']
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

    def test_the_removed_billing_gate_is_not_a_hidden_option(self):
        """The gate is gone from the parser, not merely ignored."""
        from peerreviewagents.engine.__main__ import COMMANDS, build_parser
        parser = build_parser()
        for argv in (['review', 'paper', 'run', '--billing-confirmation', 'x'],
                     ['investigate', 'paper', 'run', '--question', 'Does the claim hold?',
                      '--block', 'b1', '--billing-confirmation', 'x'],
                     ['confirm-billing', 'run']):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    parser.parse_args(argv)
        self.assertNotIn('confirm-billing', COMMANDS)

    def test_a_resumed_run_directory_keeps_its_recorded_cutoff(self):
        """Continuing a run on a later day is not changed inputs.

        The cutoff is part of the protocol, so filling today's date into an
        existing run directory would refuse the resume outright."""
        from peerreviewagents.engine import __main__ as cli
        from peerreviewagents.engine.__main__ import build_parser, resolve_cutoff
        parser = build_parser()
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(cli, 'default_literature_cutoff', return_value='2026-11-02'):
            run = Path(directory) / 'run'
            run.mkdir()
            fresh = parser.parse_args(['review', 'paper', str(run)])
            resolve_cutoff(parser, fresh, fresh.output)
            self.assertEqual(fresh.literature_cutoff, '2026-11-02')
            write(run / 'protocol.json', {'literature_cutoff': '2026-09-25'})
            resumed = parser.parse_args(['review', 'paper', str(run)])
            resolve_cutoff(parser, resumed, resumed.output)
            self.assertEqual(resumed.literature_cutoff, '2026-09-25')
            named = parser.parse_args(['review', 'paper', str(run),
                                       '--literature-cutoff', '2026-10-06'])
            resolve_cutoff(parser, named, named.output)
            self.assertEqual(named.literature_cutoff, '2026-10-06')

    def test_the_preset_alone_runs_on_claude_code_with_todays_cutoff(self):
        """``review --preset workspace-agentic-v1 workspace output`` and nothing
        else: no provider, no model, no cutoff. It must reach the engine as one
        agentic Claude Code session dated today."""
        from peerreviewagents.engine import __main__ as cli
        from peerreviewagents.engine.__main__ import build_parser, dispatch
        parser = build_parser()
        args = parser.parse_args(['review', 'paper', 'run',
                                  '--preset', 'workspace-agentic-v1'])
        self.assertIsNone(args.literature_cutoff)
        self.assertIsNone(args.model)
        self.assertIsNone(args.provider)
        # Today's date comes from one function, so the assertion never races the
        # clock between dispatch and the comparison.
        with patch.object(cli, 'default_literature_cutoff', return_value='2026-11-02'), \
             patch.object(reviewer, 'review', return_value={'status': 'complete'}) as call, \
             contextlib.redirect_stdout(io.StringIO()):
            dispatch(parser, args)
        passed = call.call_args
        self.assertEqual(passed.args[2], '2026-11-02')
        self.assertEqual(passed.args[3], 'independent')
        self.assertEqual(passed.kwargs['model'], 'claude-sonnet-5-5')
        self.assertEqual((DEFAULT_PROVIDER, DEFAULT_MODEL),
                         ('claude-code', 'claude-sonnet-5-5'))
        self.assertEqual(passed.kwargs['policy'], 'workspace-agentic-v1')
        self.assertEqual(passed.kwargs['reviewers'], 1)
        self.assertEqual(passed.kwargs['token_budget'], 1500000)

    def test_the_cli_leaves_the_preset_model_open_and_refuses_an_agentic_ensemble(self):
        from peerreviewagents.engine.__main__ import (apply_policy, apply_preset, build_parser,
                                                      resolve_model)
        parser = build_parser()
        base = ['review', 'paper', 'run', '--literature-cutoff', '2026-09-25']
        args = parser.parse_args(base + ['--preset', 'workspace-agentic-v1'])
        apply_preset(parser, args)
        # The preset pins no model, so the default transport decides.
        self.assertIsNone(get_review_preset('workspace-agentic-v1').model)
        self.assertIsNone(args.model)
        resolve_model(parser, args)
        self.assertEqual(args.model, DEFAULT_MODEL)
        named = parser.parse_args(base + ['--preset', 'workspace-agentic-v1',
                                          '--model', 'gpt-5.6-sol'])
        apply_preset(parser, named)
        resolve_model(parser, named)
        self.assertEqual(named.model, 'gpt-5.6-sol')
        # The policy names one long investigative session, so it is refused on
        # the default ensemble, which would have run three long calls instead.
        with self.assertRaises(SystemExit):
            apply_policy(parser, parser.parse_args(base + ['--policy', 'workspace-agentic-v1']))
        chosen = parser.parse_args(base + ['--policy', 'workspace-agentic-v1',
                                           '--mode', 'independent'])
        apply_policy(parser, chosen)
        self.assertEqual(chosen.mode, 'independent')
        apply_policy(parser, parser.parse_args(base + ['--policy', 'standard-v1']))
        apply_policy(parser, parser.parse_args(base))

    def test_the_api_and_the_engine_refuse_an_agentic_ensemble_as_well(self):
        """The CLI was the only surface that refused it.

        ``ReviewRequest(workflow='ensemble', policy='workspace-agentic-v1')``
        was accepted and ran two reviewer calls and a synthesis call, each at
        high effort with a two hour timeout and one bounded retry: up to six
        long calls for a policy that names one session.
        """
        with tempfile.TemporaryDirectory() as directory:
            root, out = workspace(directory)
            common = dict(workspace=root, output=out,
                          literature_cutoff='2025-01-01', policy='workspace-agentic-v1')
            with self.assertRaisesRegex(ValueError, 'one long investigative session'):
                ReviewRequest(**common, workflow='ensemble')
            independent = ReviewRequest(**common, workflow='independent')
            self.assertEqual(independent.policy, 'workspace-agentic-v1')
            # The engine is the last gate, for a caller that reaches it directly.
            with self.assertRaisesRegex(ValueError, 'one long investigative session'):
                ensemble.ensemble_review(root, out, '2025-01-01',
                                         policy='workspace-agentic-v1')
            with self.assertRaisesRegex(ValueError, 'one long investigative session'):
                reviewer.review(root, out, '2025-01-01', mode='ensemble',
                                policy=AGENTIC)
            self.assertFalse(out.exists())


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
            return {'response': {'summary': 'Review',
                                 'findings': copy.deepcopy(list(findings)),
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
            progress = read(out / 'progress.json')
        self.assertEqual(progress['status'], 'complete')
        self.assertEqual(progress['review_guard_status'], 'partially-quarantined')
        self.assertEqual((progress['findings_delivered'], progress['quarantined_findings_count']),
                         (1, 1))
        self.assertNotIn('guard_status_policy', progress)
        self.assertEqual(guarded['review_guard_status'], 'partially-quarantined')
        self.assertEqual([row['title'] for row in guarded['findings']], ['Finding 2'])
        self.assertEqual([row['title'] for row in guarded['quarantined_findings']], ['Finding 1'])
        self.assertEqual(quarantine['kept_findings'], 1)
        reason = quarantine['quarantined_findings'][0]['quarantine_reason']
        self.assertIn('ambiguous', reason)
        self.assertIn('b9', reason)
        self.assertTrue(guarded['quarantine_policy'])

    def test_every_finding_quarantined_is_not_recorded_as_a_clean_success(self):
        """The run still finishes, but it delivered nothing, and says so."""
        with tempfile.TemporaryDirectory() as directory:
            with self.hydration(self.unanchorable()):
                _, out, _ = run_review(directory, mode='plain', paper=REPEATED_PAPER)
            guarded = read(out / 'responses' / 'plain-guarded.json')
            progress = read(out / 'progress.json')
        self.assertEqual(progress['status'], reviewer.COMPLETE_WITHOUT_FINDINGS)
        self.assertEqual(progress['review_guard_status'], 'all-findings-quarantined')
        self.assertEqual((progress['findings_delivered'], progress['quarantined_findings_count']),
                         (0, 1))
        self.assertEqual([stage['stage'] for stage in progress['review_guards']], ['plain'])
        self.assertEqual(progress['guard_status_policy'], reviewer.GUARD_STATUS_POLICY)
        self.assertEqual(guarded['review_guard_status'], 'all-findings-quarantined')
        self.assertEqual(guarded['findings'], [])
        self.assertEqual(len(guarded['quarantined_findings']), 1)

    def test_an_empty_editor_package_carries_the_guard_counts(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.hydration(self.unanchorable()):
                captured, out, _ = run_review(directory, mode='self-revision',
                                              paper=REPEATED_PAPER)
            package = read(out / 'editor-package.json')
            progress = read(out / 'progress.json')
        self.assertEqual(len(captured), 2)
        self.assertEqual(package['findings'], [])
        self.assertEqual(package['review_guard_status'], 'all-findings-quarantined')
        self.assertEqual((package['findings_delivered'], package['quarantined_findings_count']),
                         (0, 2))
        self.assertEqual(progress['status'], reviewer.COMPLETE_WITHOUT_FINDINGS)
        self.assertEqual([stage['stage'] for stage in progress['review_guards']],
                         ['plain', 'self-revision'])

    def test_the_command_line_exits_non_zero_when_nothing_survived(self):
        from peerreviewagents.engine.__main__ import build_parser, dispatch
        parser = build_parser()
        args = parser.parse_args(['review', 'paper', 'run',
                                  '--literature-cutoff', '2026-09-25', '--mode', 'plain'])
        empty = {'status': reviewer.COMPLETE_WITHOUT_FINDINGS, 'findings_delivered': 0,
                 'quarantined_findings_count': 2,
                 'review_guard_status': 'all-findings-quarantined'}
        delivered = {'status': 'complete', 'findings_delivered': 2,
                     'quarantined_findings_count': 0, 'review_guard_status': 'clean'}
        with contextlib.redirect_stdout(io.StringIO()) as printed:
            with patch.object(reviewer, 'review', return_value=empty):
                with self.assertRaises(SystemExit) as caught:
                    dispatch(parser, args)
            self.assertEqual(caught.exception.code, 2)
            with patch.object(reviewer, 'review', return_value=delivered):
                dispatch(parser, args)
        self.assertIn('complete-without-findings', printed.getvalue())

    def test_the_api_artifact_names_the_zero_finding_outcome(self):
        """``run_review`` discarded the engine's status and core-artifact.json
        had none, so an API caller could not tell a run that delivered nothing
        from a clean one: both returned an artifact with an empty package."""
        from peerreviewagents.engine.api import manuscript_only_progress
        from peerreviewagents.engine.api import run_review as run_api_review
        empty = {'status': reviewer.COMPLETE_WITHOUT_FINDINGS, 'findings_delivered': 0,
                 'quarantined_findings_count': 2,
                 'review_guard_status': 'all-findings-quarantined'}
        delivered = {'status': 'complete', 'findings_delivered': 1,
                     'quarantined_findings_count': 0, 'review_guard_status': 'clean'}

        def engine(progress):
            def run(paper_root, output, *args, **kwargs):
                write(output / 'editor-package.json', {'summary': 'Review', 'findings': []})
                return progress
            return run

        with tempfile.TemporaryDirectory() as directory:
            root, out = workspace(directory)
            for index, progress in enumerate((empty, delivered)):
                target = out.with_name(f'run-{index}')
                request = ReviewRequest(workspace=root, output=target,
                                        literature_cutoff='2025-01-01')
                with patch.object(reviewer, 'review', side_effect=engine(progress)):
                    artifact = run_api_review(request)
                self.assertEqual(artifact['status'], progress['status'])
                self.assertEqual(artifact['run_progress'], progress)
                self.assertEqual(read(target / 'core-artifact.json')['status'], progress['status'])
        # The one-call baseline returns a package rather than a progress record.
        self.assertEqual(manuscript_only_progress(
            {'findings': [], 'quarantined_findings': [1, 2],
             'review_guard_status': 'all-findings-quarantined'}),
            {**empty, 'guard_status_policy': reviewer.GUARD_STATUS_POLICY})
        self.assertEqual(manuscript_only_progress({'findings': [1], 'review_guard_status': 'clean'}),
                         delivered)

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

    def die(self, command, **kwargs):
        """A launched attempt that died without a verdict: retryable.

        It reached the model and emitted work, but no turn completed, so no
        token count is observable and nothing was reported about the run.
        """
        kwargs['stdout'].write(json.dumps({'type': 'item.completed', 'item': {
            'type': 'agent_message', 'text': 'working'}}) + '\n')
        return SimpleNamespace(returncode=1)

    def spent(self, command, **kwargs):
        """A turn the model completed, followed by a non-zero exit.

        The generation happened and was charged. Whatever killed the process
        afterwards, there is nothing left to retry.
        """
        kwargs['stdout'].write(json.dumps({'type': 'turn.completed', 'usage': {
            'input_tokens': 4, 'output_tokens': 1}}) + '\n')
        return SimpleNamespace(returncode=1)

    def refuse(self, command, **kwargs):
        """A reported failure is a verdict about this run: never retried."""
        kwargs['stdout'].write(json.dumps({'type': 'turn.completed', 'usage': {
            'input_tokens': 4, 'output_tokens': 1}}) + '\n')
        kwargs['stdout'].write(json.dumps({'type': 'error', 'message': 'policy refusal'}) + '\n')
        return SimpleNamespace(returncode=1)

    def timeout(self, command, **kwargs):
        raise subprocess.TimeoutExpired(cmd='codex', timeout=30)

    def sequence(self, *behaviours):
        calls = []

        def run(command, **kwargs):
            calls.append(command)
            return behaviours[len(calls) - 1](command, **kwargs)

        return run, calls

    def test_a_crashed_call_is_retried_once_in_the_same_run(self):
        run, calls = self.sequence(self.die, self.succeed)
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            with patch.object(runtime.subprocess, 'run', side_effect=run):
                self.assertEqual(runtime.model_call(self.protocol, out, 'prompt', self.schema),
                                 {'answer': 'ok'})
            self.assertEqual(len(calls), 2)
            rows = runtime.jsonl(out / 'usage.jsonl')
            self.assertEqual([row['attempt'] for row in rows], [1, 2])
            self.assertEqual([row['ok'] for row in rows], [False, True])
            self.assertEqual([row['transient_failure'] for row in rows], [True, False])
            usage = ensemble.observed_usage(out)
            self.assertEqual((usage['calls'], usage['receipts'], usage['retried_attempts']), (2, 2, 1))
            # No turn completed on the first attempt, so its spend is a lower
            # bound rather than zero.
            self.assertEqual(usage['observed_tokens'], 10)
            self.assertFalse(usage['unknown'])
            self.assertEqual(usage['unknown_attempts'], 1)
            stems = sorted(path.name for path in (out / 'calls').glob('*.json'))
            self.assertTrue(any('.attempt2.json' in name for name in stems))

    def test_a_refusal_is_never_retried(self):
        run, calls = self.sequence(self.refuse, self.succeed)
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            with patch.object(runtime.subprocess, 'run', side_effect=run):
                with self.assertRaisesRegex(runtime.StudyBlocked, 'Codex failed'):
                    runtime.model_call(self.protocol, out, 'prompt', self.schema)
            self.assertEqual(len(calls), 1)
            rows = runtime.jsonl(out / 'usage.jsonl')
            self.assertEqual([row['transient_failure'] for row in rows], [False])
            # The saved refusal stays final, so a rerun never relaunches it either.
            with patch.object(runtime.subprocess, 'run', side_effect=run):
                with self.assertRaisesRegex(runtime.StudyBlocked, 'prior attempt failed'):
                    runtime.model_call(self.protocol, out, 'prompt', self.schema)
            self.assertEqual(len(calls), 1)

    def test_a_completed_turn_is_never_retried_after_a_non_zero_exit(self):
        """The turn was generated and charged, so a second one is a second spend."""
        run, calls = self.sequence(self.spent, self.succeed)
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            with patch.object(runtime.subprocess, 'run', side_effect=run):
                with self.assertRaisesRegex(runtime.StudyBlocked, 'completed the turn'):
                    runtime.model_call(self.protocol, out, 'prompt', self.schema)
            self.assertEqual(len(calls), 1)
            rows = runtime.jsonl(out / 'usage.jsonl')
            self.assertEqual([row['transient_failure'] for row in rows], [False])
            self.assertEqual([row['observed_tokens'] for row in rows], [5])
            with patch.object(runtime.subprocess, 'run', side_effect=run):
                with self.assertRaisesRegex(runtime.StudyBlocked, 'prior attempt failed'):
                    runtime.model_call(self.protocol, out, 'prompt', self.schema)
            self.assertEqual(len(calls), 1)

    def test_a_timed_out_call_is_retried_once(self):
        run, calls = self.sequence(self.timeout, self.succeed)
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            with patch.object(runtime.subprocess, 'run', side_effect=run):
                self.assertEqual(runtime.model_call(self.protocol, out, 'prompt', self.schema),
                                 {'answer': 'ok'})
            self.assertEqual(len(calls), 2)
            self.assertEqual([row['attempt'] for row in runtime.jsonl(out / 'usage.jsonl')], [1, 2])

    def test_a_timed_out_attempt_is_counted_rather_than_treated_as_zero(self):
        run, _ = self.sequence(self.timeout, self.succeed)
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            with patch.object(runtime.subprocess, 'run', side_effect=run):
                runtime.model_call(self.protocol, out, 'prompt', self.schema)
            usage = ensemble.observed_usage(out)
            self.assertEqual(usage['unknown_attempts'], 1)
            self.assertFalse(usage['unknown'])
            self.assertEqual(usage['observed_tokens'], 10)
            self.assertIn('lower bound', usage['usage_limitation'])
            self.assertEqual([row['attempt'] for row in usage['unaccounted_attempts']], [1])
            with self.assertRaisesRegex(ValueError, 'Unaccounted'):
                ensemble.check_budget(out, 1000, 5)
            ensemble.check_budget(out, 1000, 5, allowed_unknown=1)

    def test_a_successful_retry_does_not_block_the_next_call_in_the_run(self):
        run, calls = self.sequence(self.die, self.succeed, self.succeed)
        protocol = {**self.protocol, 'max_calls_per_condition': 2}
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            with patch.object(runtime.subprocess, 'run', side_effect=run):
                runtime.model_call(protocol, out, 'first', self.schema)
                self.assertEqual(runtime.model_call(protocol, out, 'second', self.schema),
                                 {'answer': 'ok'})
            self.assertEqual(len(calls), 3)
            usage = ensemble.observed_usage(out)
            self.assertEqual((usage['calls'], usage['receipts']), (3, 3))
            self.assertEqual(usage['retried_attempts'], 1)

    def test_a_second_crash_blocks_the_run(self):
        run, calls = self.sequence(self.die, self.die)
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            with patch.object(runtime.subprocess, 'run', side_effect=run):
                with self.assertRaises(runtime.StudyBlocked):
                    runtime.model_call(self.protocol, out, 'prompt', self.schema)
                # A crashed attempt has no observable token count, so the saved
                # receipt refuses the rerun on unknown usage before the spent
                # attempt limit is even reached. Either way it never relaunches.
                with self.assertRaisesRegex(runtime.StudyBlocked, 'unknown usage'):
                    runtime.model_call(self.protocol, out, 'prompt', self.schema)
            self.assertEqual(len(calls), 2)
            self.assertEqual([row['attempt'] for row in runtime.jsonl(out / 'usage.jsonl')], [1, 2])

    def test_the_default_policy_never_retries(self):
        run, calls = self.sequence(self.die, self.succeed)
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

    def test_a_claude_guard_problem_is_never_transient_but_extra_usage_is_no_problem(self):
        init = {'type': 'system', 'subtype': 'init', 'apiKeySource': 'none',
                'tools': ['StructuredOutput', 'Bash'], 'claude_code_version': '1.0.0'}
        outcome = runtime.assess_claude_stream([init], 'claude-opus-5-5', False, 0)
        self.assertIn('tools outside the approved manuscript server', outcome['error'])
        self.assertFalse(outcome['transient_failure'])
        overage = {'type': 'rate_limit_event', 'rate_limit_info': {'isUsingOverage': True,
                                                                   'overageStatus': 'allowed'}}
        self.assertIsNone(runtime.claude_stream_problem(overage, False))
        crashed = runtime.assess_claude_stream([], 'claude-opus-5-5', False, 1)
        self.assertTrue(crashed['transient_failure'])
        self.assertIn('no session configuration', crashed['error'])

    def test_archive_attempt_never_overwrites_a_previous_attempt(self):
        with tempfile.TemporaryDirectory() as directory:
            live = Path(directory) / 'plain'
            live.mkdir()
            (live / 'tool-log.jsonl').write_text(json.dumps({'call_index': 1}) + '\n')
            first = runtime.archive_attempt(live, 2)
            self.assertEqual(first.name, 'plain.attempt1')
            live.mkdir()
            (live / 'tool-log.jsonl').write_text(json.dumps({'call_index': 1}) + '\n')
            second = runtime.archive_attempt(live, 2)
            self.assertEqual(second.name, 'plain.attempt1.2')
            self.assertIsNone(runtime.archive_attempt(live, 2))
            self.assertEqual(len(runtime.jsonl(first / 'tool-log.jsonl')), 1)

    def test_a_retried_investigation_starts_a_fresh_tool_log_and_source_budget(self):
        """A retry shares the request identity, so it must not append to the
        replaced attempt's tool log: call_index restarts and receipts verify.
        It must not inherit the replaced attempt's spent source requests
        either, or a timed-out agentic attempt hands its replacement a budget
        it never used."""
        with tempfile.TemporaryDirectory() as directory:
            root, out = workspace(directory)
            handle = build_registry(PAPER).handles[0].handle_id
            seen = {}

            def fake(protocol, execution, prompt, schema, **options):
                log = out / 'investigations' / 'plain' / 'tool-log.jsonl'
                log.parent.mkdir(parents=True, exist_ok=True)
                log.write_text(json.dumps({'call_index': 1, 'tool': 'read_source_block'}) + '\n')
                write(out / 'sources' / 'requests.json', {'limit': 6, 'requests': ['a', 'b']})
                write(out / 'sources' / 'targets.json', {'count': ['x', 'y']})
                write(out / 'sources' / 'cards.json',
                      [{'evidence_id': 'E1', 'eligible': True, 'evidence_text': 'Five plants.'}])
                seen['archived'] = options['prepare_attempt'](2)
                return copy.deepcopy(model_response(handle))

            with patch.object(reviewer, 'require_subscription'), \
                 patch.object(reviewer, 'model_call', side_effect=fake), \
                 patch.object(reviewer, 'inspected_images', return_value=[]):
                reviewer.review(root, out, '2025-01-01', mode='plain',
                                model='gpt-5.6-terra')
            archived = Path(seen['archived'])
            self.assertEqual(archived.name, 'plain.attempt1')
            self.assertEqual(len(runtime.jsonl(archived / 'tool-log.jsonl')), 1)
            self.assertFalse((out / 'investigations' / 'plain' / 'tool-log.jsonl').exists())
            sources = out / 'sources'
            self.assertFalse((sources / 'requests.json').exists())
            self.assertFalse((sources / 'targets.json').exists())
            self.assertEqual(read(sources / 'requests.attempt1.json')['requests'], ['a', 'b'])
            self.assertEqual(read(sources / 'targets.attempt1.json')['count'], ['x', 'y'])
            # Cards stay: earlier findings already reference their handles, and
            # the raw cache means a re-read of them costs no budget.
            self.assertEqual(read(sources / 'cards.json')[0]['evidence_id'], 'E1')

    def test_the_ensemble_synthesis_call_archives_its_attempt_too(self):
        """The debate stage used to pass no prepare_attempt, so a retried
        agentic synthesis inherited the failed attempt's tool log, its
        synthesis-preflight receipts and its spent source requests."""
        with tempfile.TemporaryDirectory() as directory:
            root, out = workspace(directory)
            seen = {}
            base = {'title': 'Count', 'kind': 'question', 'severity': 'moderate', 'block_id': 'b1',
                    'quote': 'We studied 5 plants.', 'evidence_blocks': ['b1'], 'replacement': '',
                    'counterevidence': '', 'source_urls': []}
            records = [{'review': {'findings': [{**base, 'comment': f'Confirm the count {i}.'}]},
                        'provenance': {'paper_id': 'paper', 'origin': 'test'}} for i in (1, 2)]

            def fake(protocol, execution, prompt, schema, **options):
                stage = out / 'investigations' / 'debate'
                write(stage / 'synthesis-preflight' / 'receipt.json', {'call_index': 1})
                write(out / 'sources' / 'requests.json', {'limit': 2, 'requests': ['a', 'b']})
                seen['archived'] = options['prepare_attempt'](2)
                raise Captured()

            with patch.object(ensemble, 'require_subscription'), \
                 patch.object(ensemble, 'model_call', side_effect=fake):
                with self.assertRaises(Captured):
                    ensemble.debate_reviews(root, out, '2025-01-01', records,
                                            model='gpt-5.6-terra')
            self.assertEqual(Path(seen['archived']).name, 'debate.attempt1')
            self.assertTrue((out / 'investigations' / 'debate.attempt1' /
                             'synthesis-preflight' / 'receipt.json').exists())
            self.assertFalse((out / 'sources' / 'requests.json').exists())
            self.assertEqual(read(out / 'sources' / 'requests.attempt1.json')['requests'], ['a', 'b'])


class SubscriptionCheckedPerStage(unittest.TestCase):
    def ensemble_transport(self, clock, jump):
        """A fake transport for both ensemble stages that advances the clock.

        Each call writes the receipt and usage row the budget check reads, then
        moves the clock on by ``jump`` seconds, the way a long stage does.
        """
        handle = build_registry(PAPER).handles[0].handle_id
        findings, labels = [], []

        def fake(protocol, execution, prompt, schema, **options):
            labels.append(options['label'])
            clock[0] += jump
            write(execution / 'calls' / 'attempt.started.json', {})
            (execution / 'usage.jsonl').write_text(json.dumps({'observed_tokens': 100}) + '\n')
            item = finding(handle)
            if options['label'] == 'independent':
                item = {**item, 'comment': f'Confirm request {len(findings) + 1}.'}
                findings.append(item)
                return {'summary': 'Review', 'findings': [copy.deepcopy(item)], 'limitations': [],
                        'investigation_summary': 'Checked.', 'discarded_hypotheses': []}
            decisions = []
            for index, kept in enumerate(findings, 1):
                issue = f'r{index}-issue-1'
                decisions.append({
                    'issue_id': issue, 'scientific_status': 'supported_clarification',
                    'presentation': 'primary', 'strongest_criticism': 'Count unclear.',
                    'strongest_author_defense': 'The count is stated.',
                    'decisive_question': 'How many plants?', 'decision_reason': 'Retain.',
                    'evidence': [{'evidence_handle': handle}],
                    'components': [{'component_id': issue + '-part-1', 'treatment': 'preserved',
                                    'final_finding_number': index, 'final_quote': kept['comment'],
                                    'reason': 'Kept.', 'evidence': []}]})
            return {'summary': 'Review', 'findings': copy.deepcopy(findings), 'limitations': [],
                    'decisions': decisions, 'new_candidates': []}

        return fake, labels

    def test_the_whole_ensemble_passes_the_real_login_guard_on_every_stage(self):
        """End to end with the real guard: only the transport and the login
        probe are patched out. Every stage checks the subscription login for
        itself, and a long run is no longer refused for outliving a launch
        window: nothing is written that gates on recorded usage."""
        real, clock, auth = time.time, [0.0], []

        def auth_status(provider):
            auth.append(provider)
            return {'loggedIn': True, 'authMethod': 'claude.ai', 'apiProvider': None}

        with tempfile.TemporaryDirectory() as directory:
            root, out = workspace(directory)
            fake, labels = self.ensemble_transport(clock, 50000)
            with patch.object(runtime, 'auth_status', side_effect=auth_status), \
                 patch('time.time', lambda: real() + clock[0]), \
                 patch.object(reviewer, 'model_call', side_effect=fake), \
                 patch.object(ensemble, 'model_call', side_effect=fake), \
                 patch.object(reviewer, 'inspected_images', return_value=[]):
                progress = ensemble.ensemble_review(root, out, '2025-01-01')
            self.assertEqual(progress['status'], 'complete')
            self.assertEqual(labels, ['independent', 'independent', 'ensemble-debate'])
            # The launch check plus one real guard call per stage, all on the
            # default transport, with no model or provider named anywhere.
            self.assertEqual(auth, [DEFAULT_PROVIDER] * 4)
            self.assertEqual(read(out / 'protocol.json')['model'], DEFAULT_MODEL)
            self.assertEqual(sorted(path.name for path in out.rglob('*.json')
                                    if 'billing' in path.name or 'verification' in path.name), [])


if __name__ == '__main__':
    unittest.main()
