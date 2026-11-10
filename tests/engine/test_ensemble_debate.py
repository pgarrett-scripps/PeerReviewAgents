"""Offline contract checks for ensemble debate additions."""
import copy
import unittest

from peerreviewagents.engine.debate_revision import issue_ledger
from peerreviewagents.engine.ensemble_debate import augment_ledger, ensemble_schema


class EnsembleDebateTests(unittest.TestCase):
    def setUp(self):
        self.paper = {'title': 'Study', 'blocks': [
            {'id': 'b1', 'text': 'Replicate counts are not specified.'},
            {'id': 'b2', 'text': 'The control sample was measured once.'},
            {'id': 'b3', 'text': 'The outcome was reported.'},
        ]}
        original = {'title': 'Replication', 'block_id': 'b1',
                    'quote': 'Replicate counts are not specified.', 'kind': 'question',
                    'severity': 'major', 'comment': 'Report replicate counts.',
                    'replacement': '', 'evidence_blocks': ['b1'],
                    'counterevidence': '', 'source_urls': []}
        self.ledger = issue_ledger({'findings': [original]})
        self.original = original

    def decision(self, issue_id, *, selected=False, status='supported_clarification'):
        component = {'component_id': issue_id + '-part-1',
                     'treatment': 'preserved' if selected else 'deferred',
                     'final_finding_number': 1 if selected else 0,
                     'final_quote': 'Report replicate counts.' if selected else '',
                     'reason': 'The reporting question remains consequential.',
                     'evidence': []}
        return {'issue_id': issue_id, 'scientific_status': status,
                'presentation': 'primary' if selected else 'supporting',
                'strongest_criticism': 'Reporting is incomplete.',
                'strongest_author_defense': 'An outcome is reported.',
                'decisive_question': 'Are replicates defined?',
                'decision_reason': 'The manuscript does not provide the count.',
                'evidence': [{'block_id': 'b1', 'quote': 'Replicate counts are not specified.'}],
                'components': [component]}

    def value(self):
        existing = self.decision('issue-1', selected=True)
        candidate = {'issue_id': 'new-1', 'finding': {
            'title': 'Control replication', 'block_id': 'b2',
            'quote': 'The control sample was measured once.', 'kind': 'question',
            'severity': 'moderate', 'comment': 'Clarify whether the control was replicated.',
            'replacement': '', 'evidence_blocks': ['b2'], 'counterevidence': '',
            'source_urls': [], 'source_evidence': []}}
        new = self.decision('new-1', status='unresolved')
        new['evidence'] = []
        new['components'][0]['reason'] = 'The available manuscript evidence is incomplete.'
        return {'summary': 'Review', 'findings': [self.original], 'limitations': [],
                'decisions': [existing, new], 'new_candidates': [candidate]}

    def test_valid_new_finding_has_provenance_and_preserves_existing_accountability(self):
        before = copy.deepcopy(self.ledger)
        augmented = augment_ledger(self.value(), self.ledger, self.paper)
        self.assertEqual(self.ledger, before)
        self.assertEqual(augmented['issues'][0], before['issues'][0])
        added = augmented['issues'][-1]
        self.assertEqual(added['source_kind'], 'debate_new_candidate')
        self.assertEqual(added['components'][0]['component_id'], 'new-1-part-1')
        self.assertEqual(added['provenance']['anchor']['block_id'], 'b2')

    def test_foreign_anchor_is_rejected_and_distinct_comments_can_share_an_anchor(self):
        foreign = self.value()
        foreign['new_candidates'][0]['finding']['block_id'] = 'missing'
        with self.assertRaisesRegex(ValueError, 'quote is absent'):
            augment_ledger(foreign, self.ledger, self.paper)
        duplicate = self.value()
        second = copy.deepcopy(duplicate['new_candidates'][0])
        second['issue_id'] = 'new-2'
        second['finding']['comment'] = 'Clarify how the single control measurement was interpreted.'
        duplicate['new_candidates'].append(second)
        duplicate['decisions'].append(self.decision('new-2', status='unresolved'))
        duplicate['decisions'][-1]['evidence'] = []
        augmented = augment_ledger(duplicate, self.ledger, self.paper)
        self.assertEqual([issue['issue_id'] for issue in augmented['issues']],
                         ['issue-1', 'new-1', 'new-2'])

    def test_duplicate_ids_and_invalid_external_source_are_rejected(self):
        duplicate = self.value()
        duplicate['new_candidates'].append(copy.deepcopy(duplicate['new_candidates'][0]))
        with self.assertRaisesRegex(ValueError, 'identifiers must be unique'):
            augment_ledger(duplicate, self.ledger, self.paper)
        source = self.value()
        source['new_candidates'][0]['finding']['source_evidence'] = [
            {'evidence_id': 'source-missing', 'quote': 'No record'}]
        with self.assertRaisesRegex(ValueError, 'External finding evidence'):
            augment_ledger(source, self.ledger, self.paper)

    def test_schema_bounds_reserved_ids_and_nested_finding(self):
        schema = ensemble_schema()
        candidates = schema['properties']['new_candidates']
        self.assertEqual(candidates['maxItems'], 5)
        self.assertEqual(candidates['items']['properties']['issue_id']['pattern'], '^new-[1-5]$')
        source = candidates['items']['properties']['finding']['properties']['source_evidence']['items']
        self.assertIn('evidence_id', source['properties'])

    def test_selected_new_candidate_passes_full_validation(self):
        value = self.value()
        finding = value['new_candidates'][0]['finding']
        value['findings'].append(copy.deepcopy(finding))
        decision = value['decisions'][1]
        decision.update(scientific_status='supported_clarification', presentation='primary',
                        evidence=[{'block_id': 'b2',
                                   'quote': 'The control sample was measured once.'}])
        decision['components'][0].update(
            treatment='preserved', final_finding_number=2,
            final_quote=finding['comment'],
            reason='The selected finding preserves the full candidate comment.')
        augmented = augment_ledger(value, self.ledger, self.paper)
        self.assertEqual(augmented['issues'][-1]['issue_id'], 'new-1')

    def test_deferred_validation_keeps_new_candidate_policy_checks(self):
        value = self.value()
        value['decisions'][1]['presentation'] = 'omitted'
        with self.assertRaisesRegex(ValueError, 'supporting secondary'):
            augment_ledger(value, self.ledger, self.paper, validate=False)


if __name__ == '__main__':
    unittest.main()
