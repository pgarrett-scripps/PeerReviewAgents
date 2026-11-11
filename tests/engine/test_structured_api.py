"""Offline checks for the public structured subscription transport."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from peerreviewagents.engine import StructuredRequest, run_structured, usage_summary
from peerreviewagents.engine.models import DEFAULT_MODEL, DEFAULT_PROVIDER


class StructuredAPI(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def request(self, **overrides):
        return StructuredRequest(**{'output': self.root / 'run',
            'model': 'gpt-6-astra', 'prompt': 'Return a grade', 'schema': {'type': 'object'}, **overrides})

    def test_unsupported_model_and_invalid_limits_fail_before_inference(self):
        for args in ({'model': 'unknown'}, {'prompt': ''}, {'schema': []}, {'token_budget': True}, {'timeout_seconds': 0}):
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.request(**args)

    def test_shared_transport_is_tool_free_and_never_retries(self):
        request = self.request()
        with patch('peerreviewagents.engine.ensemble.require_subscription') as auth, patch('peerreviewagents.engine.model_runtime.model_call', return_value={'score': 1}) as call:
            artifact = run_structured(request)
            auth.assert_called_once()
            self.assertEqual(artifact['value'], {'score': 1})
            self.assertFalse(call.call_args.kwargs['tools'])
            self.assertEqual(call.call_args.args[0]['provider'], 'codex')
            self.assertEqual(call.call_args.args[0]['model'], 'gpt-6-astra')
            self.assertEqual(call.call_args.args[0]['max_calls_per_condition'], 1)
            with self.assertRaises(ValueError):
                run_structured(request)
            self.assertEqual(call.call_count, 1)

    def test_a_claude_model_runs_through_claude_code_and_is_the_default(self):
        with patch('peerreviewagents.engine.ensemble.require_subscription'), \
             patch('peerreviewagents.engine.model_runtime.model_call', return_value={'score': 1}) as call:
            run_structured(self.request(model='claude-sonnet-5-5', output=self.root / 'claude-run'))
            self.assertEqual(call.call_args.args[0]['provider'], 'claude-code')
            self.assertFalse(call.call_args.kwargs['tools'])
            run_structured(self.request(output=self.root / 'default-run', model=DEFAULT_MODEL))
        self.assertEqual(call.call_args.args[0]['provider'], DEFAULT_PROVIDER)
        self.assertEqual(call.call_args.args[0]['model'], 'claude-sonnet-5-5')

    def test_source_images_are_validated_and_forwarded_without_tools(self):
        image = self.root / 'figure.png'
        image.write_bytes(b'figure')
        request = self.request(source_images=(image,))
        with patch('peerreviewagents.engine.ensemble.require_subscription'), \
             patch('peerreviewagents.engine.model_runtime.model_call', return_value={'score': 1}) as call:
            run_structured(request)
        self.assertEqual(call.call_args.kwargs['images'], (image,))
        self.assertFalse(call.call_args.kwargs['tools'])
        with self.assertRaises(ValueError):
            self.request(output=self.root / 'other', source_images=(self.root / 'missing.png',))

    def test_failure_keeps_receipt_summary(self):
        request = self.request()
        with patch('peerreviewagents.engine.ensemble.require_subscription'), patch('peerreviewagents.engine.model_runtime.model_call', side_effect=RuntimeError('failure')):
            with self.assertRaises(RuntimeError):
                run_structured(request)
        self.assertTrue((request.output / 'usage-summary.json').exists())
        self.assertEqual(usage_summary(request.output)['calls'], 0)
