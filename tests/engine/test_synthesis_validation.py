"""Offline regression checks for in-session synthesis validation."""
import copy
import json
import tempfile
import unittest
from pathlib import Path

from peerreviewagents.engine.debate_revision import issue_ledger
from peerreviewagents.engine.ensemble_references import recover
from peerreviewagents.engine.evidence_handles import build_registry
from peerreviewagents.engine.investigation import Investigation, specs
from peerreviewagents.engine.model_runtime import tool_profile
from peerreviewagents.engine.storage import digest, read, write
from peerreviewagents.engine.synthesis_validation import validate_draft, destination_diagnostics, preflight_receipts


class SynthesisValidationTests(unittest.TestCase):
    def setUp(self):
        text = 'We studied five plants.'
        self.paper = {'id': 'paper', 'blocks': [{'id': 'b1', 'text': text, 'sha256': digest(text)}]}
        self.finding = {'title': 'Allocation', 'kind': 'question', 'severity': 'moderate',
                        'block_id': 'b1', 'quote': text, 'evidence_blocks': ['b1'],
                        'comment': 'Please clarify allocation.', 'replacement': '',
                        'counterevidence': '', 'source_urls': []}
        self.ledger = issue_ledger({'findings': [self.finding]})
        self.handle = build_registry(self.paper).handles[0].handle_id
        f = {k: v for k, v in self.finding.items() if k not in ('block_id', 'quote', 'evidence_blocks')}
        f.update(anchor_handle=self.handle, manuscript_evidence_handles=[self.handle])
        self.raw = {'summary': 'Review', 'findings': [f], 'limitations': [], 'new_candidates': [],
                    'decisions': [{'issue_id': 'issue-1', 'scientific_status': 'supported_clarification',
                    'presentation': 'primary', 'strongest_criticism': 'Allocation is unclear.',
                    'strongest_author_defense': 'A count is supplied.', 'decisive_question': 'How allocated?',
                    'decision_reason': 'Clarification is needed.', 'evidence': [{'evidence_handle': self.handle}],
                    'secondary_finding': None, 'components': [{'component_id': 'issue-1-part-1',
                    'treatment': 'preserved', 'final_finding_number': 1,
                    'final_quote': self.finding['comment'], 'reason': 'Retain the request.', 'evidence': []}]}]}

    def test_valid_draft_and_format_recovery_leave_input_unchanged(self):
        self.raw['decisions'][0]['components'][0]['final_quote'] = 'please clarify allocation'
        before = copy.deepcopy(self.raw)
        checked = validate_draft(self.raw, self.ledger, self.paper)
        self.assertTrue(checked['valid'])
        self.assertEqual(checked['problems'], [])
        self.assertEqual(self.raw, before)
        self.assertEqual(len(checked['reference_recovery']['changed_fields']), 1)

    def test_empty_output_and_missing_content_are_not_repaired(self):
        empty = copy.deepcopy(self.raw)
        empty['findings'] = []
        result = validate_draft(empty, self.ledger, self.paper)
        self.assertFalse(result['valid'])
        self.assertEqual(result['problems'][0]['code'], 'missing_final_finding')
        self.raw['decisions'][0]['components'][0]['final_quote'] = 'Please report the sampling dates.'
        result = validate_draft(self.raw, self.ledger, self.paper)
        self.assertFalse(result['valid'])
        self.assertEqual(result['problems'][0]['code'], 'missing_final_quote')

    def test_secondary_feedback_names_missing_fields_and_preserves_required_wording(self):
        secondary = copy.deepcopy(self.raw['findings'][0])
        secondary.pop('source_urls')
        self.raw['findings'] = []
        decision = self.raw['decisions'][0]
        decision.update(presentation='supporting', secondary_finding=secondary)
        decision['components'][0].update(treatment='deferred', final_finding_number=0, final_quote='')
        result = validate_draft(self.raw, self.ledger, self.paper)
        self.assertFalse(result['valid'])
        self.assertIn('source_urls', result['error'])
        self.assertIn('source_evidence', result['error'])
        self.assertEqual(result['problems'][0]['issue_id'], 'issue-1')
        decision['secondary_finding'] = None
        result = validate_draft(self.raw, self.ledger, self.paper)
        self.assertFalse(result['valid'])
        self.assertIn('requires an adjudicated finding', result['error'])
        secondary.update(source_urls=[], source_evidence=[])
        decision['secondary_finding'] = secondary
        self.assertTrue(validate_draft(self.raw, self.ledger, self.paper)['valid'])

    def test_invalid_evidence_is_still_rejected(self):
        self.raw['findings'][0]['anchor_handle'] = 'invented'
        self.assertFalse(validate_draft(self.raw, self.ledger, self.paper)['valid'])

    def test_recovery_reports_the_later_error_not_the_repaired_error(self):
        from peerreviewagents.engine.evidence_handles import hydrate_response
        value = hydrate_response(self.raw, build_registry(self.paper))['response']
        value['decisions'][0]['components'][0]['final_quote'] = 'please clarify allocation'
        hypothesis = {'issue_id': 'issue-2', 'source_kind': 'discarded_hypothesis',
                      'components': [{'component_id': 'issue-2-part-1', 'text': 'Was allocation biased?'}]}
        ledger = copy.deepcopy(self.ledger)
        ledger['issues'].append(hypothesis)
        decision = copy.deepcopy(value['decisions'][0])
        decision.update(issue_id='issue-2', scientific_status='contradicted', presentation='omitted')
        decision['components'][0].update(component_id='issue-2-part-1', treatment='deferred',
                                         final_finding_number=0, final_quote='')
        value['decisions'].append(decision)
        result = recover(value, ledger, self.paper)
        self.assertIsNone(result['recovered'])
        self.assertEqual(result['audit']['error'], 'Rejected hypothesis must resolve every component with source evidence')
        problems = destination_diagnostics(value, ledger, result['changed_fields'])
        self.assertEqual([p['code'] for p in problems], ['rejected_hypothesis_unresolved_component'])

    def test_preflight_is_separate_bounded_persistent_and_does_not_publish(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write(root/'paper.json', self.paper)
            write(root/'manifest.json', {'files': {'paper.json': digest((root/'paper.json').read_bytes())}})
            write(root/'ledger.json', self.ledger)
            config = {'paper': str(root), 'output': str(root/'out'), 'cache': str(root/'cache'),
                      'tool_limit': 1, 'synthesis_ledger': str(root/'ledger.json')}
            server = Investigation(config)
            server.call('calculate', {'expression': '2+2'})
            first = server.call('validate_synthesis', {'draft_json': json.dumps(self.raw)})
            self.assertTrue(first['valid'])
            self.assertEqual(server.calls, 1)
            restarted = Investigation(config)
            second = restarted.call('validate_synthesis', {'draft_json': '{'})
            self.assertFalse(second['valid'])
            exhausted = server.call('validate_synthesis', {'draft_json': json.dumps(self.raw)})
            self.assertIn('exhausted', exhausted['error'])
            self.assertEqual(len(list((root/'out/synthesis-preflight').glob('*.json'))), 2)
            self.assertFalse((root/'reviews').exists())
            saved = read(root/'out/synthesis-preflight/check-1.json')
            self.assertEqual(saved['result']['draft_sha256'], first['draft_sha256'])
            self.assertTrue(preflight_receipts(root/'out/synthesis-preflight', self.raw)['submitted_draft_preflight_passed'])
            changed = {**self.raw, 'summary': 'Edited after validation'}
            self.assertFalse(preflight_receipts(root/'out/synthesis-preflight', changed)['submitted_draft_preflight_passed'])

    def test_valid_draft_reports_a_complete_component_ledger(self):
        checked = validate_draft(self.raw, self.ledger, self.paper)
        self.assertTrue(checked['valid'])
        self.assertEqual(checked['component_ledger'], {'issues': 1, 'decided_issues': 1, 'expected_components': 1,
                                                       'accounted_components': 1, 'complete': True})

    def test_tool_exposure_matches_runtime_allowlist_only_for_synthesis(self):
        self.assertNotIn('validate_synthesis', tool_profile({})[1])
        for domain in (False, True):
            config = {'synthesis_ledger': 'ledger.json'}
            if domain:
                config['tool_profile'] = 'domains-v1'
            self.assertEqual(set(tool_profile(config)[1]), {s['name'] for s in specs(domain, True)})


class ComponentAccountingTests(unittest.TestCase):
    """Missing component records must be named, never repaired."""
    def setUp(self):
        text = 'We studied five plants across two sites.'
        self.paper = {'id': 'paper', 'blocks': [{'id': 'b1', 'text': text, 'sha256': digest(text)}]}
        def finding(comment):
            return {'title': 'Allocation', 'kind': 'question', 'severity': 'moderate', 'block_id': 'b1',
                    'quote': text, 'evidence_blocks': ['b1'], 'comment': comment, 'replacement': '',
                    'counterevidence': '', 'source_urls': []}
        self.ledger = issue_ledger({'findings': [finding('Please clarify allocation. Report the site counts.'),
                                                 finding('Please state the sampling dates.')]})
        self.handle = build_registry(self.paper).handles[0].handle_id
        final = {k: v for k, v in finding('Please clarify allocation and report the site counts.').items()
                 if k not in ('block_id', 'quote', 'evidence_blocks')}
        final.update(anchor_handle=self.handle, manuscript_evidence_handles=[self.handle])
        def decision(issue_id, parts):
            return {'issue_id': issue_id, 'scientific_status': 'supported_clarification', 'presentation': 'primary',
                    'strongest_criticism': 'Allocation is unclear.', 'strongest_author_defense': 'Counts exist.',
                    'decisive_question': 'How allocated?', 'decision_reason': 'Clarify.',
                    'evidence': [{'evidence_handle': self.handle}], 'secondary_finding': None,
                    'components': [{'component_id': f'{issue_id}-part-{j}', 'treatment': 'preserved',
                                    'final_finding_number': 1, 'final_quote': 'Please clarify allocation',
                                    'reason': 'Retained.', 'evidence': []} for j in range(1, parts + 1)]}
        self.raw = {'summary': 'Review', 'findings': [final], 'limitations': [], 'new_candidates': [],
                    'decisions': [decision('issue-1', 2), decision('issue-2', 1)]}

    def codes(self, result):
        return {p['code']: p for p in result['problems']}

    def test_complete_fixture_is_valid(self):
        result = validate_draft(self.raw, self.ledger, self.paper)
        self.assertTrue(result['valid'], result.get('error'))
        self.assertTrue(result['component_ledger']['complete'])
        self.assertEqual(result['component_ledger']['expected_components'], 3)

    def test_empty_components_name_every_missing_identifier(self):
        for decision in self.raw['decisions']:
            decision['components'] = []
        before = copy.deepcopy(self.raw)
        result = validate_draft(self.raw, self.ledger, self.paper)
        self.assertFalse(result['valid'])
        self.assertEqual(self.raw, before)
        missing = [p for p in result['problems'] if p['code'] == 'missing_components']
        self.assertEqual({p['issue_id']: p['component_ids'] for p in missing},
                         {'issue-1': ['issue-1-part-1', 'issue-1-part-2'], 'issue-2': ['issue-2-part-1']})
        self.assertEqual(result['component_ledger']['accounted_components'], 0)
        self.assertFalse(result['component_ledger']['complete'])

    def test_one_missing_component_is_named(self):
        self.raw['decisions'][0]['components'].pop()
        result = validate_draft(self.raw, self.ledger, self.paper)
        self.assertFalse(result['valid'])
        self.assertEqual(self.codes(result)['missing_components']['component_ids'], ['issue-1-part-2'])
        self.assertEqual(result['component_ledger']['accounted_components'], 2)

    def test_duplicated_component_is_named(self):
        components = self.raw['decisions'][0]['components']
        components.append(copy.deepcopy(components[0]))
        result = validate_draft(self.raw, self.ledger, self.paper)
        self.assertFalse(result['valid'])
        self.assertEqual(self.codes(result)['duplicate_components']['component_ids'], ['issue-1-part-1'])

    def test_misassigned_component_names_its_owner(self):
        moved = self.raw['decisions'][1]['components'].pop()
        self.raw['decisions'][0]['components'].append(moved)
        result = validate_draft(self.raw, self.ledger, self.paper)
        self.assertFalse(result['valid'])
        problems = self.codes(result)
        self.assertEqual(problems['misassigned_components']['issue_id'], 'issue-1')
        self.assertEqual(problems['misassigned_components']['components'],
                         [{'component_id': 'issue-2-part-1', 'belongs_to': 'issue-2'}])
        self.assertEqual(problems['missing_components']['issue_id'], 'issue-2')

    def test_unknown_component_and_missing_decision_are_named(self):
        self.raw['decisions'][0]['components'][1]['component_id'] = 'issue-1-part-9'
        self.raw['decisions'].pop()
        result = validate_draft(self.raw, self.ledger, self.paper)
        self.assertFalse(result['valid'])
        problems = self.codes(result)
        self.assertEqual(problems['unknown_components']['component_ids'], ['issue-1-part-9'])
        self.assertEqual(problems['missing_decision']['issue_id'], 'issue-2')
        self.assertEqual(problems['missing_decision']['component_ids'], ['issue-2-part-1'])

    def test_complete_ledger_is_confirmed_when_another_error_blocks_validation(self):
        # Paper 0317140 had all components in its first check, then dropped them after a handle error.
        self.raw['findings'][0]['manuscript_evidence_handles'].append('mh-invented')
        result = validate_draft(self.raw, self.ledger, self.paper)
        self.assertFalse(result['valid'])
        self.assertIn('Unknown evidence handle', result['error'])
        self.assertTrue(result['component_ledger']['complete'])
        self.assertFalse([p for p in result['problems'] if p['code'].endswith('components')])

    def test_final_validation_stays_strict(self):
        from peerreviewagents.engine.debate_revision import validate_revision
        from peerreviewagents.engine.evidence_handles import hydrate_response
        value = hydrate_response(self.raw, build_registry(self.paper))['response']
        value.pop('new_candidates')
        value['decisions'][0]['components'] = []
        with self.assertRaisesRegex(ValueError, 'exactly one treatment'):
            validate_revision(value, self.ledger, self.paper)

    def test_contract_lists_required_identifiers(self):
        from peerreviewagents.engine.synthesis_validation import component_contract
        contract = component_contract(self.ledger)
        self.assertIn('"issue-1": ["issue-1-part-1", "issue-1-part-2"]', contract)
        self.assertIn('"issue-2": ["issue-2-part-1"]', contract)

    def test_schema_requires_one_decision_per_issue_and_known_component_ids(self):
        from peerreviewagents.engine.ensemble_debate import constrain_ledger_identifiers, ensemble_schema
        base = ensemble_schema()
        schema = constrain_ledger_identifiers(base, self.ledger)
        self.assertEqual(base, ensemble_schema())
        decisions = schema['properties']['decisions']
        self.assertEqual((decisions['minItems'], decisions['maxItems']), (2, 2))
        self.assertEqual(decisions['items']['properties']['issue_id']['enum'], ['issue-1', 'issue-2'])
        components = decisions['items']['properties']['components']
        self.assertEqual(components['minItems'], 1)
        self.assertEqual(components['items']['properties']['component_id']['enum'],
                         ['issue-1-part-1', 'issue-1-part-2', 'issue-2-part-1'])
        embedded = schema['properties']['new_candidates']['items']['properties']['decision']['properties']
        self.assertNotIn('component_id', embedded['component']['properties'])
