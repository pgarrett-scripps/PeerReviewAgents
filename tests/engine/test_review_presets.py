"""Offline checks for versioned review presets and the tool-free baseline."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from peerreviewagents.engine import (ReviewRequest, capabilities, review_presets,
                                  review_request_from_preset, run_review)
from peerreviewagents.engine.baseline import run_manuscript_only
from peerreviewagents.engine.config import resolved_review_config
from peerreviewagents.engine.storage import digest, read, write


class ReviewPresetTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.workspace = self.root / 'workspace'
        text = 'The study included twelve samples.'
        self.paper = {
            'id': 'paper', 'title': 'Example', 'source_sha256': 'source',
            'blocks': [{'id': 'b1', 'text': text, 'sha256': digest(text)}],
            'references': [], 'assets': [], 'sections': {},
            'statistics': {'total_words': 6},
        }
        write(self.workspace / 'paper.json', self.paper)
        write(self.workspace / 'manifest.json', {'files': {
            'paper.json': digest((self.workspace / 'paper.json').read_bytes())}})

    def request(self, **overrides):
        values = {
            'workspace': self.workspace, 'output': self.root / 'run',
            'billing_confirmation': self.root / 'billing.json',
            'literature_cutoff': '2026-09-25', 'workflow': 'manuscript-only',
            'reviewers': 1,
        }
        return ReviewRequest(**{**values, **overrides})

    def test_presets_are_versioned_and_factory_preserves_existing_routes(self):
        presets = review_presets()
        self.assertEqual(set(presets), {
            'manuscript-only-single-v1', 'workspace-independent-v1',
            'workspace-ensemble-v1', 'workspace-ensemble-ordinary-v1',
        })
        self.assertTrue(all(item['configuration_version'] == 'paper-workbench-config-v1'
                            for item in presets.values()))
        independent = review_request_from_preset(
            'workspace-independent-v1', workspace=self.workspace,
            output=self.root / 'independent', billing_confirmation=self.root / 'billing.json',
            literature_cutoff='2026-09-25')
        ensemble = review_request_from_preset(
            'workspace-ensemble-v1', workspace=self.workspace,
            output=self.root / 'ensemble', billing_confirmation=self.root / 'billing.json',
            literature_cutoff='2026-09-25')
        ordinary = review_request_from_preset(
            'workspace-ensemble-ordinary-v1', workspace=self.workspace,
            output=self.root / 'ordinary', billing_confirmation=self.root / 'billing.json',
            literature_cutoff='2026-09-25')
        self.assertEqual((independent.workflow, independent.reviewers), ('independent', 1))
        self.assertEqual((ensemble.workflow, ensemble.reviewers), ('ensemble', 2))
        self.assertEqual((ordinary.workflow, ordinary.reviewers, ordinary.synthesis_policy),
                         ('ensemble', 2, 'ordinary'))
        self.assertEqual(resolved_review_config(ensemble)['evidence_policy'], 'live')
        self.assertEqual(ReviewRequest(
            workspace=self.workspace, output=self.root / 'default',
            billing_confirmation=self.root / 'billing.json',
            literature_cutoff='2026-09-25').workflow, 'ensemble')
        self.assertIn('manuscript-only', capabilities()['workflows'])
        with self.assertRaises(ValueError):
            review_request_from_preset(
                'workspace-ensemble-v1', workspace=self.workspace,
                output=self.root / 'invalid', billing_confirmation=self.root / 'billing.json',
                literature_cutoff='2026-09-25', token_budget=0)

    def test_preset_topology_cannot_be_silently_changed(self):
        request = self.request(preset='workspace-ensemble-v1')
        with self.assertRaisesRegex(ValueError, 'do not match'):
            resolved_review_config(request)
        with self.assertRaisesRegex(ValueError, 'exactly one'):
            self.request(reviewers=2)
        with self.assertRaisesRegex(ValueError, 'only for ensemble'):
            self.request(synthesis_policy='ordinary')

    def test_baseline_delivers_complete_text_and_enforces_no_tools(self):
        request = self.request()
        normalized = {'findings': [], 'summary': 'Review', 'limitations': []}
        with patch('peerreviewagents.engine.ensemble._prepare'), \
             patch('peerreviewagents.engine.model_runtime.model_call', return_value={}) as model, \
             patch('peerreviewagents.engine.evidence_handles.hydrate_review_response',
                   return_value={'response': normalized, 'audit': {}}), \
             patch('peerreviewagents.engine.annotations.save_review',
                   return_value={'findings': []}):
            package = run_manuscript_only(request, self.paper,
                                          resolved_review_config(request))
        prompt = model.call_args.args[2]
        self.assertIn('The study included twelve samples.', prompt)
        self.assertFalse(model.call_args.kwargs['tools'])
        self.assertEqual(model.call_args.kwargs['images'], [])
        self.assertEqual(package['summary'], 'Review')
        protocol = read(request.output / 'protocol.json') if (request.output / 'protocol.json').exists() else None
        self.assertIsNone(protocol)
        manifest = read(request.output / 'run-manifest.json')
        self.assertEqual(manifest['configuration']['evidence_profile'], 'manuscript-only-no-tools')
        self.assertFalse((request.output / 'execution' / 'workspace' / 'tool-config.json').exists())

    def test_baseline_deduplicates_identical_image_copies_by_content(self):
        first = self.root / 'first.png'
        second = self.root / 'second.png'
        first.write_bytes(b'identical pixels')
        second.write_bytes(b'identical pixels')
        request = self.request(source_images=(first, second))
        normalized = {'findings': [], 'summary': 'Review', 'limitations': []}
        with patch('peerreviewagents.engine.ensemble._prepare'), \
             patch('peerreviewagents.engine.model_runtime.model_call', return_value={}) as model, \
             patch('peerreviewagents.engine.evidence_handles.hydrate_review_response',
                   return_value={'response': normalized, 'audit': {}}), \
             patch('peerreviewagents.engine.annotations.save_review',
                   return_value={'findings': []}):
            run_manuscript_only(request, self.paper, resolved_review_config(request))
        self.assertEqual(model.call_args.kwargs['images'], [first.resolve()])

    def test_run_artifact_persists_resolved_configuration(self):
        request = self.request()
        with patch('peerreviewagents.engine.baseline.run_manuscript_only') as baseline:
            def save_package(_request, _paper, _config):
                write(request.output / 'editor-package.json', {'summary': 'Review', 'findings': []})
            baseline.side_effect = save_package
            result = run_review(request)
        self.assertEqual(result['resolved_configuration']['workflow'], 'manuscript-only')
        self.assertEqual(len(result['resolved_configuration_sha256']), 64)
        self.assertEqual(read(request.output / 'core-artifact.json'), result)


if __name__ == '__main__':
    unittest.main()
