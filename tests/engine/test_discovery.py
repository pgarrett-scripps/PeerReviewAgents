"""Offline checks for investigation records and guarded review handoff."""
import copy
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from peerreviewagents.engine import reviewer
from peerreviewagents.engine.annotations import export_draft
from peerreviewagents.engine.discovery import AREAS, DISCOVERY, INVESTIGATION_REVIEW, coverage_record
from peerreviewagents.engine.evidence_handles import build_registry, with_evidence_handles, constrain_manuscript_handles
from peerreviewagents.engine.review_schema import with_external_evidence
from peerreviewagents.engine.storage import digest, read, write


def checks():
    return [{'area': area, 'status': 'examined', 'reason': 'Considered the reported count.',
             'evidence': [{'block_id': 'b1', 'quote': '5 plants'}]} for area in AREAS]


class DiscoveryChecks(unittest.TestCase):
    def test_investigator_checkpoint_does_not_follow_later_source_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            out=Path(directory)
            write(out/'sources/cards.json',[{'phase':'investigator'}])
            write(out/'source-cache/record.json',{'original':True})
            write(out/'execution/workspace/tool-config.json',{'tool_limit':4})
            reviewer.save_initial_checkpoint(out,'original prompt',{}, {'findings':[]},[])
            write(out/'sources/cards.json',[{'phase':'debate'}])
            self.assertEqual(read(out/'initial-checkpoint/sources/cards.json'),[{'phase':'investigator'}])
            checkpoint=read(out/'initial-checkpoint/input.json')
            self.assertEqual(checkpoint['prompt'],'original prompt')
            with self.assertRaisesRegex(ValueError,'already exists'):
                reviewer.save_initial_checkpoint(out,'new prompt',{}, {'findings':[]},[])

    def test_self_revision_shares_budget_and_preserves_edit_guard(self):
        handle = build_registry(self.paper).handles[0].handle_id
        model = {'summary':'Review', 'findings':[{'title':'Count', 'kind':'edit',
            'severity':'moderate', 'comment':'Confirm count.', 'replacement':'7 plants',
            'counterevidence':'', 'source_urls':[], 'source_evidence':[],
            'anchor_handle':handle, 'manuscript_evidence_handles':[handle]}],
            'limitations':[], 'investigation_summary':'Checked.', 'discarded_hypotheses':[]}
        with tempfile.TemporaryDirectory() as directory:
            root, out = Path(directory)/'paper', Path(directory)/'run'
            write(root/'paper.json', self.paper)
            (root/'manuscript.md').write_text('We studied 5 plants.')
            write(root/'manifest.json', {'files':{n:digest((root/n).read_bytes()) for n in ('paper.json','manuscript.md')}})
            billing=Path(directory)/'billing.json'
            write(billing, {'checked_at':time.time(),'no_paid_credits_verified':True})
            calls=[]
            def fake(protocol, execution, prompt, schema, **kwargs):
                calls.append({'protocol':protocol,'prompt':prompt,'config':read(execution/'workspace/tool-config.json')})
                return copy.deepcopy(model)
            with patch.object(reviewer,'require_subscription'), patch.object(reviewer,'model_call',side_effect=fake), patch.object(reviewer,'inspected_images',return_value=[]):
                reviewer.review(root,out,billing,'2025-01-01',mode='self-revision',model='gpt-5.6-terra')
            self.assertEqual([c['config']['tool_limit'] for c in calls],[4,2])
            self.assertEqual(calls[0]['config']['source_state'],calls[1]['config']['source_state'])
            self.assertEqual(calls[0]['protocol']['max_calls_per_condition'],2)
            self.assertIn('actual manipulated contrast',calls[0]['prompt'])
            self.assertIn('actual manipulated contrast',calls[1]['prompt'])
            self.assertIn('YOUR PRIOR DRAFT:',calls[1]['prompt'])
            self.assertNotIn('7 plants',calls[1]['prompt'])
            self.assertEqual(read(out/'editor-package.json')['findings'][0]['kind'],'question')
            self.assertEqual(read(out/'responses/self-revision-model.json'),model)

    def setUp(self):
        self.paper = {'id': 'paper', 'title': 'An experiment', 'source_sha256': 'source',
                      'blocks': [{'id': 'b1', 'text': 'We studied 5 plants.',
                                  'sha256': digest('We studied 5 plants.')}],
                      'sections': [], 'assets': [], 'references': [],
                      'statistics': {'total_words': 4}}

    def test_coverage_requires_source_quotes_and_complete_area_accounting(self):
        response = {'coverage_checks': checks()}
        valid = coverage_record(self.paper, response)
        self.assertTrue(all(row['validation'] == 'valid' for row in valid['areas']))
        self.assertEqual(response, {'coverage_checks': checks()})
        for problem in ('missing', 'duplicate', 'foreign_quote', 'foreign_id', 'no_evidence'):
            changed = copy.deepcopy(response)
            rows = changed['coverage_checks']
            if problem == 'missing':
                rows.pop(0)
            elif problem == 'duplicate':
                rows.append(copy.deepcopy(rows[0]))
            elif problem == 'foreign_quote':
                rows[0]['evidence'][0]['quote'] = '10 plants'
            elif problem == 'foreign_id':
                rows[0]['evidence'][0]['block_id'] = 'other'
            else:
                rows[0]['evidence'] = []
            with self.subTest(problem=problem):
                record = coverage_record(self.paper, changed)
                self.assertEqual(record['areas'][0]['status'], 'unresolved')
                self.assertEqual(record['areas'][0]['validation'], 'invalid')

    def test_investigator_allows_bounded_candidate_inventory(self):
        self.assertEqual(INVESTIGATION_REVIEW['properties']['findings']['maxItems'], 10)
        self.assertIn('up to ten distinct, anchored candidate findings', DISCOVERY)
        self.assertIn('Fewer candidates are preferable to padding', DISCOVERY)
        self.assertIn('For a clinical trial', DISCOVERY)
        self.assertIn('For an ecology field study', DISCOVERY)
        self.assertIn('only when the manuscript design matches', DISCOVERY)

    def test_unresolved_and_inapplicable_are_not_promoted_to_examined(self):
        rows = checks()
        for row in rows[:2]:
            row['evidence'] = []
        rows[0]['status'] = 'unresolved'
        rows[0]['reason'] = 'Relevant figure was not inspected.'
        rows[1]['status'] = 'not_applicable'
        rows[1]['reason'] = 'No intervention was performed.'
        record = coverage_record(self.paper, {'coverage_checks': rows})
        self.assertEqual([row['status'] for row in record['areas'][:2]],
                         ['unresolved', 'not_applicable'])
        self.assertTrue(all(row['validation'] == 'valid' for row in record['areas']))
        missing = coverage_record(self.paper, {})
        self.assertTrue(all(row['status'] == 'unresolved' for row in missing['areas']))
        malformed = coverage_record(self.paper, {'coverage_checks': [None, {'area': []}]})
        self.assertEqual(malformed['unexpected_areas'], [None, []])
        self.assertTrue(all(row['validation'] == 'invalid' for row in malformed['areas']))

    def test_panel_uses_guarded_findings_and_preserves_raw_outputs(self):
        handle = build_registry(self.paper).handles[0].handle_id
        finding = {'title': 'Count', 'block_id': 'b1', 'quote': '5 plants',
                   'kind': 'edit', 'severity': 'moderate', 'comment': 'Confirm count.',
                   'replacement': '7 plants', 'evidence_blocks': ['b1'],
                   'counterevidence': '', 'source_urls': [], 'source_evidence': []}
        first = {'summary': 'Check count.', 'findings': [finding], 'limitations': [],
                 'investigation_summary': 'Checked the count.', 'discarded_hypotheses': [],
                 'coverage_checks': checks()}
        final = {key: copy.deepcopy(value) for key, value in first.items() if key != 'coverage_checks'}
        final['findings'][0]['replacement'] = '9 plants'
        final['decisions'] = [{'issue_id':'issue-1','scientific_status':'supported_clarification',
            'presentation':'primary','strongest_criticism':'The count needs confirmation.',
            'strongest_author_defense':'The count is reported.', 'decisive_question':'Is the count correct?',
            'decision_reason':'Keep the question, not an unsupported numerical replacement.',
            'evidence':[{'block_id':'b1','quote':'5 plants'}],
            'components':[{'component_id':'issue-1-part-1','treatment':'preserved',
                'final_finding_number':1,'final_quote':'Confirm count.',
                'reason':'The question remains explicit.','evidence':[]}]}]
        first_model = copy.deepcopy(first)
        first_model['findings'][0].pop('block_id')
        first_model['findings'][0].pop('quote')
        first_model['findings'][0].pop('evidence_blocks')
        first_model['findings'][0].update(anchor_handle=handle,
                                          manuscript_evidence_handles=[handle])
        for row in first_model['coverage_checks']:
            row['evidence'] = [{'evidence_handle': handle}]
        final_model = copy.deepcopy(final)
        final_model['findings'][0].pop('block_id')
        final_model['findings'][0].pop('quote')
        final_model['findings'][0].pop('evidence_blocks')
        final_model['findings'][0].update(anchor_handle=handle,
                                          manuscript_evidence_handles=[handle])
        final_model['decisions'][0]['evidence'] = [{'evidence_handle': handle}]
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root, out = base / 'paper', base / 'run'
            write(root / 'paper.json', self.paper)
            (root / 'manuscript.md').write_text('[b1] We studied 5 plants.')
            files = {name: digest((root / name).read_bytes()) for name in ('paper.json', 'manuscript.md')}
            write(root / 'manifest.json', {'files': files})
            billing = base / 'billing.json'
            write(billing, {'checked_at': time.time(), 'no_paid_credits_verified': True})
            seen = []

            def fake_call(protocol, execution, prompt, schema, **kwargs):
                seen.append({'prompt': prompt, 'schema': schema,
                             'config': read(execution / 'workspace/tool-config.json')})
                return copy.deepcopy(first_model if len(seen) == 1 else final_model)

            with patch.object(reviewer, 'require_subscription'), \
                    patch.object(reviewer, 'model_call', side_effect=fake_call) as invoke, \
                    patch.object(reviewer, 'inspected_images', return_value=[]):
                result = reviewer.review(root, out, billing, '2024-01-01')
            self.assertEqual(result['status'], 'complete')
            self.assertEqual(invoke.call_count, 2)
            self.assertEqual(seen[0]['schema'],
                             constrain_manuscript_handles(with_evidence_handles(with_external_evidence(INVESTIGATION_REVIEW)),
                                                          build_registry(self.paper)))
            self.assertEqual(seen[0]['config']['source_state'], seen[1]['config']['source_state'])
            self.assertEqual(seen[0]['config']['external_request_limit'], 6)
            self.assertEqual([row['config']['tool_limit'] for row in seen], [4, 2])
            self.assertNotIn('7 plants', seen[1]['prompt'])
            self.assertIn('edit_withheld', seen[1]['prompt'])
            self.assertIn('coverage_record', seen[1]['prompt'])
            self.assertEqual(read(out / 'responses/workspace-model.json'), first_model)
            self.assertEqual(read(out / 'responses/debate-model.json'), final_model)
            hydrated_first = read(out / 'responses/workspace.json')
            hydrated_final = read(out / 'responses/debate.json')
            self.assertEqual(hydrated_first['findings'][0]['block_id'], 'b1')
            self.assertEqual(hydrated_first['findings'][0]['quote'], 'We studied 5 plants.')
            self.assertEqual(hydrated_final['decisions'][0]['evidence'][0]['quote'],
                             'We studied 5 plants.')
            self.assertGreater(hydrated_first['evidence_handle_audit']['transformation_count'], 0)
            self.assertGreater(hydrated_final['evidence_handle_audit']['transformation_count'], 0)
            package = read(out / 'editor-package.json')
            self.assertEqual(package['findings'][0]['kind'], 'question')
            self.assertIsNone(package['findings'][0]['replacement'])
            self.assertEqual(package['source_findings'][0]['finding']['kind'], 'question')
            self.assertEqual(package['result_map']['paper_id'], self.paper['id'])
            self.assertEqual(package['epistemic_audit']['issues'][0]['scientific_truth_status'],
                             'not_mechanically_determined')
            for review_path in (root / 'reviews').glob('*.json'):
                saved = read(review_path)
                if review_path.name.startswith(('panel-', 'workspace-')):
                    exported = export_draft(self.paper, saved, {'paper_id': 'paper', 'items': {}})
                    self.assertEqual(len(exported['coverage_record']['areas']), len(AREAS))
                    self.assertEqual(exported['proposed_blocks'], self.paper['blocks'])
                    self.assertEqual(exported['result_map']['version'], 'result-map-v1')
                    if review_path.name.startswith('panel-'):
                        self.assertEqual(exported['epistemic_audit'], package['epistemic_audit'])
            self.assertEqual(read(root / 'manifest.json')['files'], files)


if __name__ == '__main__':
    unittest.main()
