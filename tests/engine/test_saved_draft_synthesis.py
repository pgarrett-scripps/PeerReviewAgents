import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from peerreviewagents.engine import SavedDraftSynthesisRequest, run_saved_draft_synthesis
from peerreviewagents.engine.source_packet import source_packet_manifest
from peerreviewagents.engine.storage import digest, write
from peerreviewagents.engine.model_runtime import source_manifest
import peerreviewagents.engine
import peerreviewagents.engine.api as core_api


class SavedDraftSynthesisTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.workspace = self.root / 'workspace'
        self.workspace.mkdir()
        (self.workspace / 'manifest.json').write_text('{}')
        self.packet = self.root / 'packet'
        self.packet.mkdir()
        (self.packet / 'index.json').write_text('{"sources": []}')
        self.image = self.root / 'figure.png'
        self.image.write_bytes(b'figure')
        self.original = self.root / 'original'
        debate = self.original / 'debate'
        debate.mkdir(parents=True)
        records = []
        for index, tokens in ((1, 30), (2, 40)):
            reviewer = self.original / f'reviewer-{index}'
            source_review = reviewer / 'editor-package.json'
            source_review.parent.mkdir(parents=True)
            source_review.write_text('{"summary":"draft-' + str(index) + '"}')
            write(reviewer / 'protocol.json', {'literature_cutoff': '2026-10-06'})
            (reviewer / 'call.started.json').write_text('{}')
            (reviewer / 'usage.jsonl').write_text(json.dumps({
                'observed_tokens': tokens, 'usage_unknown': False}) + '\n')
            records.append({'review': {'summary': f'draft-{index}', 'findings': []}, 'provenance': {
                'paper_id': 'paper-1', 'reviewer_id': f'reviewer-{index}',
                'source_review_path': str(source_review),
                'source_review_sha256': digest(source_review.read_bytes()),
                'origin': 'fresh_independent_session'}})
        write(debate / 'input-reviews.json', records)
        write(debate / 'manifest.json', {
            'review_input_sha256': digest((debate / 'input-reviews.json').read_bytes()),
            'images': {str(self.image.resolve()): digest(self.image.read_bytes())}})
        write(debate / 'protocol.json', {'debate_tool_allowance': 2, 'token_launch_budget': 123})
        packet_hashes = source_packet_manifest(self.packet)
        write(self.original / 'core-artifact.json', {
            'api_version': 'paper-workbench-core-v1', 'artifact_version': 'paper-workbench-run-v1',
            'paper_id': 'paper-1',
            'source_manifest_sha256': digest((self.workspace / 'manifest.json').read_bytes()),
            'workflow': 'ensemble', 'model_requested': 'gpt-5.6-terra',
            'resolved_configuration': {'synthesis_policy': 'debate', 'reviewers': 2},
            'source_packet_manifest_after': packet_hashes,
            'engine_source': source_manifest(Path(peerreviewagents.engine.__file__).parent),
            'core_source': source_manifest(Path(core_api.__file__).parent),
            'usage': {'calls': 3, 'receipts': 3, 'observed_tokens': 90, 'unknown': False}})
        self.request = SavedDraftSynthesisRequest(workspace=self.workspace,
            original_output=self.original, output=self.root / 'ordinary',
            literature_cutoff='2026-10-06', model='gpt-5.6-terra',
            source_images=(self.image,), source_packet=self.packet, token_budget=123)

    def tearDown(self):
        self.temporary.cleanup()

    def synthesize(self, request=None):
        def fake(workspace, output, cutoff, records, **kwargs):
            Path(output).mkdir(parents=True)
            (Path(output) / 'call.started.json').write_text('{}')
            (Path(output) / 'usage.jsonl').write_text(json.dumps({
                'observed_tokens': 20, 'usage_unknown': False}) + '\n')
            return {'summary': 'ordinary', 'findings': [],
                    'synthesis_provenance': {'policy': 'ordinary', 'generation_calls': 1, 'tool_limit': 2}}
        with patch('peerreviewagents.engine.api.load_manuscript', return_value={'id': 'paper-1'}), \
             patch('peerreviewagents.engine.ensemble.debate_reviews', side_effect=fake):
            return run_saved_draft_synthesis(request or self.request)

    def test_reuses_exact_drafts_and_reports_new_usage_separately(self):
        artifact = self.synthesize()
        self.assertEqual(artifact['editor_package']['summary'], 'ordinary')
        self.assertEqual(artifact['workflow'], 'ensemble')
        self.assertEqual(artifact['execution_scope'], 'saved-draft-synthesis')
        provenance = artifact['editor_package']['synthesis_provenance']
        self.assertTrue(provenance['reused_drafts'])
        self.assertEqual(provenance['discovery_calls'], 0)
        self.assertEqual(artifact['usage']['calls'], 1)
        self.assertEqual(artifact['usage_scope'], 'new_synthesis_stage_only')
        self.assertEqual(artifact['reused_discovery_usage']['calls'], 2)
        self.assertEqual(artifact['reused_discovery_usage']['observed_tokens'], 70)
        self.assertEqual(artifact['original_full_workflow_usage']['calls'], 3)
        self.assertEqual(len(artifact['reused_discovery_receipts']), 4)

    def test_rejects_incomplete_images_and_changed_drafts(self):
        other = self.root / 'other.png'
        other.write_bytes(b'other')
        request = SavedDraftSynthesisRequest(**{**self.request.__dict__,
            'output': self.root / 'wrong-images', 'source_images': (other,)})
        with self.assertRaisesRegex(ValueError, 'complete original image set'):
            self.synthesize(request)
        source_review = self.original / 'reviewer-1' / 'editor-package.json'
        source_review.write_text('{"changed":true}')
        request = SavedDraftSynthesisRequest(**{**self.request.__dict__, 'output': self.root / 'changed'})
        with self.assertRaisesRegex(ValueError, 'input review changed'):
            self.synthesize(request)

    def test_rejects_packet_or_stage_budget_substitution(self):
        (self.packet / 'index.json').write_text('{"changed": true}')
        request = SavedDraftSynthesisRequest(**{**self.request.__dict__, 'output': self.root / 'packet-change'})
        with self.assertRaisesRegex(ValueError, 'original frozen evidence packet'):
            self.synthesize(request)
        (self.packet / 'index.json').write_text('{"sources": []}')
        request = SavedDraftSynthesisRequest(**{**self.request.__dict__,
            'output': self.root / 'budget-change', 'token_budget': 124})
        with self.assertRaisesRegex(ValueError, 'tool and token budget'):
            self.synthesize(request)


    def test_the_cutoff_comes_from_the_original_run_and_a_mismatch_is_refused(self):
        """Saved drafts were written under one cutoff, so synthesis reuses it."""
        inherited = SavedDraftSynthesisRequest(**{**self.request.__dict__,
            'output': self.root / 'inherited', 'literature_cutoff': None})
        self.assertEqual(inherited.literature_cutoff, '2026-10-06')
        with self.assertRaisesRegex(ValueError, 'original run literature cutoff'):
            SavedDraftSynthesisRequest(**{**self.request.__dict__,
                'output': self.root / 'later', 'literature_cutoff': '2026-11-02'})
        (self.original / 'reviewer-1' / 'protocol.json').unlink()
        with self.assertRaisesRegex(ValueError, 'reviewer-1 protocol'):
            SavedDraftSynthesisRequest(**{**self.request.__dict__,
                'output': self.root / 'missing'})

    def test_rejects_wrong_expected_discovery_count(self):
        artifact_path = self.original / 'core-artifact.json'
        artifact = json.loads(artifact_path.read_text())
        artifact['resolved_configuration']['reviewers'] = 3
        write(artifact_path, artifact)
        with self.assertRaisesRegex(ValueError, 'expected discovery count'):
            self.synthesize()

    def test_rejects_original_engine_or_core_drift(self):
        artifact_path = self.original / 'core-artifact.json'
        artifact = json.loads(artifact_path.read_text())
        artifact['core_source'] = {'changed': True}
        write(artifact_path, artifact)
        with self.assertRaisesRegex(ValueError, 'engine or public core changed'):
            self.synthesize()


if __name__ == '__main__':
    unittest.main()
