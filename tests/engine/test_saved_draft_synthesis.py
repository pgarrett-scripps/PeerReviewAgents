"""Saved-draft synthesis over a real ensemble run, with the model call stubbed.

The original run here is produced by ``run_review`` itself rather than a
hand-written artifact, so the guards this pass applies are checked against the
files a real run actually writes. The drafts are what the pass reuses: its
evidence is retrieved live under the original run's cutoff, exactly as a fresh
review retrieves it.
"""
import contextlib
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from peerreviewagents.engine import (ReviewRequest, SavedDraftSynthesisRequest, capabilities,
                                     ensemble, reviewer, run_review, run_saved_draft_synthesis)
from peerreviewagents.engine.evidence_handles import build_registry
from peerreviewagents.engine.storage import digest, read, write

TEXT = 'We studied five plants.'
PAPER = {'id': 'paper', 'title': 'Study', 'source_sha256': 'original',
         'blocks': [{'id': 'b1', 'text': TEXT, 'sha256': digest(TEXT)}],
         'sections': [], 'assets': [], 'references': [], 'statistics': {'total_words': 4}}

FINDING = {'title': 'Allocation', 'kind': 'question', 'severity': 'moderate',
           'comment': 'Clarify allocation.', 'replacement': '', 'counterevidence': '',
           'source_urls': []}

CUTOFF = '2026-10-06'
MODEL = 'gpt-5.6-terra'


class SavedDraftSynthesisTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.workspace = self.root / 'workspace'
        write(self.workspace / 'paper.json', PAPER)
        write(self.workspace / 'manifest.json', {'files': {
            'paper.json': digest((self.workspace / 'paper.json').read_bytes())}})
        self.handle = build_registry(PAPER).handles[0].handle_id
        self.original = self.root / 'original'
        self.drafts = []
        with self.stubbed_transport():
            self.original_artifact = run_review(ReviewRequest(
                workspace=self.workspace, output=self.original,
                literature_cutoff=CUTOFF, model=MODEL, reviewers=2))
        # The debate stage launches on what the discovery calls left of the run
        # budget, so the matched synthesis pass has to name that same figure.
        self.stage_budget = read(self.original / 'debate/protocol.json')['token_launch_budget']

    def model_call(self, protocol, execution, prompt, schema, **options):
        """One stubbed generation call that leaves the receipts a real call leaves."""
        execution = Path(execution)
        write(execution / 'calls/test.started.json', {})
        (execution / 'usage.jsonl').write_text(json.dumps({'observed_tokens': 100}) + '\n')
        finding = {**FINDING, 'anchor_handle': self.handle, 'source_evidence': [],
                   'manuscript_evidence_handles': [self.handle]}
        if options['label'] == 'independent':
            finding['comment'] = f'Private reviewer request {len(self.drafts) + 1}.'
            self.drafts.append(finding)
            return {'summary': 'Review', 'findings': [copy.deepcopy(finding)], 'limitations': [],
                    'investigation_summary': 'Checked manuscript', 'discarded_hypotheses': []}
        decisions = []
        for index, draft in enumerate(self.drafts, 1):
            ident = f'r{index}-issue-1'
            decisions.append({'issue_id': ident, 'scientific_status': 'supported_clarification',
                'presentation': 'primary', 'strongest_criticism': 'Allocation needs clarification.',
                'strongest_author_defense': 'A sample count is present.',
                'decisive_question': 'How allocated?',
                'decision_reason': 'Retain a proportionate question.',
                'evidence': [{'evidence_handle': self.handle}],
                'components': [{'component_id': ident + '-part-1', 'treatment': 'preserved',
                                'final_finding_number': index, 'final_quote': draft['comment'],
                                'reason': 'The request is retained.', 'evidence': []}]})
        return {'summary': 'Review', 'findings': copy.deepcopy(self.drafts), 'limitations': [],
                'decisions': decisions, 'new_candidates': []}

    @contextlib.contextmanager
    def stubbed_transport(self):
        with patch.object(ensemble, 'require_subscription'), \
             patch.object(reviewer, 'require_subscription'), \
             patch.object(reviewer, 'model_call', side_effect=self.model_call), \
             patch.object(ensemble, 'model_call', side_effect=self.model_call), \
             patch.object(reviewer, 'inspected_images', return_value=[]):
            yield

    def request(self, name='ordinary', **overrides):
        return SavedDraftSynthesisRequest(**{'workspace': self.workspace,
            'original_output': self.original, 'output': self.root / name,
            'source_images': (), 'model': MODEL, 'token_budget': self.stage_budget, **overrides})

    def synthesize(self, name='ordinary', **overrides):
        with self.stubbed_transport():
            return run_saved_draft_synthesis(self.request(name, **overrides))

    def test_reuses_exact_drafts_and_reports_new_usage_separately(self):
        artifact = self.synthesize()
        self.assertEqual(artifact['workflow'], 'ensemble')
        self.assertEqual(artifact['execution_scope'], 'saved-draft-synthesis')
        self.assertEqual(artifact['resolved_configuration']['synthesis_policy'], 'ordinary')
        self.assertEqual(artifact['resolved_configuration']['discovery_calls'], 0)
        provenance = artifact['editor_package']['synthesis_provenance']
        self.assertTrue(provenance['reused_drafts'])
        self.assertEqual(provenance['discovery_calls'], 0)
        self.assertEqual(provenance['original_review_sha256'],
                         [row['provenance']['source_review_sha256']
                          for row in read(self.original / 'debate/input-reviews.json')])
        self.assertEqual(artifact['usage']['calls'], 1)
        self.assertEqual(artifact['usage_scope'], 'new_synthesis_stage_only')
        self.assertEqual(artifact['reused_discovery_usage']['calls'], 2)
        self.assertEqual(artifact['reused_discovery_usage']['observed_tokens'], 200)
        self.assertEqual(artifact['original_full_workflow_usage'],
                         self.original_artifact['usage'])
        self.assertEqual(len(artifact['reused_discovery_receipts']), 4)

    def test_the_synthesis_retrieves_live_evidence_like_a_fresh_review(self):
        """Nothing is frozen with the drafts, so there is no packet to supply."""
        artifact = self.synthesize()
        self.assertEqual(artifact['resolved_configuration']['evidence_policy'], 'live')
        protocol = read(self.root / 'ordinary/protocol.json')
        self.assertEqual(protocol['evidence_policy'], 'live')
        self.assertEqual(protocol['mode'], 'saved-draft-synthesis')
        config = read(self.root / 'ordinary/execution/workspace/tool-config.json')
        for absent in ('source_packet', 'source_packet_manifest'):
            self.assertNotIn(absent, config)
            self.assertNotIn(absent, artifact)
            self.assertNotIn(absent, artifact['editor_package']['synthesis_provenance'])
        self.assertEqual(capabilities()['evidence_policies'], ['none', 'live'])
        with self.assertRaises(TypeError):
            self.request('packet', source_packet=self.root)

    def test_rejects_incomplete_images_and_changed_drafts(self):
        image = self.root / 'figure.png'
        image.write_bytes(b'figure')
        with self.assertRaisesRegex(ValueError, 'complete original image set'):
            self.synthesize('wrong-images', source_images=(image,))
        records_path = self.original / 'debate/input-reviews.json'
        records = read(records_path)
        records[0]['review']['summary'] = 'Rewritten after the fact'
        write(records_path, records)
        with self.assertRaisesRegex(ValueError, 'input review hashes changed'):
            self.synthesize('changed-hash')
        manifest_path = self.original / 'debate/manifest.json'
        manifest = read(manifest_path)
        manifest['review_input_sha256'] = digest(records_path.read_bytes())
        write(manifest_path, manifest)
        with self.assertRaisesRegex(ValueError, 'input review changed'):
            self.synthesize('changed-draft')

    def test_rejects_stage_budget_substitution(self):
        with self.assertRaisesRegex(ValueError, 'tool and token budget'):
            self.synthesize('budget-change', token_budget=self.stage_budget - 1)

    def test_the_cutoff_comes_from_the_original_run_and_a_mismatch_is_refused(self):
        """Saved drafts were written under one cutoff, so synthesis reuses it."""
        self.assertEqual(self.request('inherited').literature_cutoff, CUTOFF)
        with self.assertRaisesRegex(ValueError, 'original run literature cutoff'):
            self.request('later', literature_cutoff='2026-11-02')
        (self.original / 'reviewer-1/protocol.json').unlink()
        with self.assertRaisesRegex(ValueError, 'reviewer-1 protocol'):
            self.request('missing')

    def test_rejects_wrong_expected_discovery_count(self):
        path = self.original / 'core-artifact.json'
        artifact = read(path)
        artifact['resolved_configuration']['reviewers'] = 3
        write(path, artifact)
        with self.assertRaisesRegex(ValueError, 'expected discovery count'):
            self.synthesize('wrong-count')

    def test_rejects_original_engine_drift(self):
        path = self.original / 'core-artifact.json'
        artifact = read(path)
        artifact['engine_source'] = {'changed': True}
        write(path, artifact)
        with self.assertRaisesRegex(ValueError, 'engine changed'):
            self.synthesize('drifted')


if __name__ == '__main__':
    unittest.main()
