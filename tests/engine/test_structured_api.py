"""Offline checks for the public structured subscription transport."""
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from peerreviewagents.engine import StructuredRequest, run_structured, usage_summary
from peerreviewagents.engine.storage import write


class StructuredAPI(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.billing = self.root / 'billing.json'
        write(self.billing, {'checked_at': time.time(), 'no_paid_credits_verified': True})

    def request(self, **overrides):
        return StructuredRequest(**{'output': self.root / 'run', 'billing_confirmation': self.billing,
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

    def test_claude_model_uses_claude_code_with_its_own_billing_record(self):
        from peerreviewagents.engine.billing import claude_billing_record
        with patch('peerreviewagents.engine.ensemble.require_subscription'), \
             patch('peerreviewagents.engine.model_runtime.model_call', return_value={'score': 1}) as call:
            with self.assertRaises(ValueError):
                run_structured(self.request(model='claude-sonnet-5-5'))
            call.assert_not_called()
            write(self.billing, claude_billing_record({'loggedIn': True, 'authMethod': 'claude.ai'}))
            run_structured(self.request(model='claude-sonnet-5-5', output=self.root / 'claude-run'))
        self.assertEqual(call.call_args.args[0]['provider'], 'claude-code')
        self.assertFalse(call.call_args.kwargs['tools'])

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

    def test_stale_billing_cannot_start_a_call(self):
        write(self.billing, {'checked_at': 0, 'no_paid_credits_verified': True})
        with patch('peerreviewagents.engine.model_runtime.model_call') as call:
            with self.assertRaises(ValueError):
                run_structured(self.request())
            call.assert_not_called()
