import unittest

from peerreviewagents.engine.debate_revision import issue_ledger, revision_package, validate_revision
from peerreviewagents.engine.ensemble_debate import (
    adjudicated_secondary, attach_adjudicated_secondary, audit_only_secondary, augment_ledger,
)
from peerreviewagents.engine.ensemble import _protocol


class EnsembleAdjudicatedSecondaryTests(unittest.TestCase):
    def setUp(self):
        self.paper = {'title': 'Study', 'blocks': [
            {'id': 'b1', 'text': 'Replicate counts are not specified.'},
            {'id': 'b2', 'text': 'The control sample was measured once.'},
        ]}
        self.original = {
            'title': 'Replication', 'block_id': 'b1',
            'quote': 'Replicate counts are not specified.', 'kind': 'question',
            'severity': 'major', 'comment': 'The study has no replication and cannot support its conclusions.',
            'replacement': '', 'evidence_blocks': ['b1'], 'counterevidence': '',
            'source_urls': [],
        }
        self.ledger = issue_ledger({'findings': [self.original]})

    def secondary_finding(self):
        return {
            'title': 'Replication reporting', 'block_id': 'b1',
            'quote': 'Replicate counts are not specified.', 'kind': 'question',
            'severity': 'moderate',
            'comment': 'Please report the number of independent replicates used for each analysis.',
            'replacement': '', 'evidence_blocks': ['b1'], 'counterevidence': '',
            'source_urls': [], 'source_evidence': [],
        }

    def value(self, secondary=None):
        return {'summary': 'Review', 'findings': [], 'limitations': [], 'new_candidates': [],
                'decisions': [{
                    'issue_id': 'issue-1', 'scientific_status': 'supported_clarification',
                    'presentation': 'supporting', 'strongest_criticism': 'Counts are absent.',
                    'strongest_author_defense': 'The reported outcome may still be informative.',
                    'decisive_question': 'How many independent replicates contributed?',
                    'decision_reason': 'The manuscript does not state the replicate counts.',
                    'evidence': [{'block_id': 'b1', 'quote': 'Replicate counts are not specified.'}],
                    'secondary_finding': self.secondary_finding() if secondary is None else secondary,
                    'components': [{
                        'component_id': 'issue-1-part-1', 'treatment': 'deferred',
                        'final_finding_number': 0, 'final_quote': '',
                        'reason': 'This is a lower-priority reporting clarification.', 'evidence': [],
                    }],
                }]}

    def test_requires_model_authored_wording_for_retained_secondary(self):
        value = self.value()
        value['decisions'][0]['secondary_finding'] = None
        with self.assertRaisesRegex(ValueError, 'requires an adjudicated finding'):
            augment_ledger(value, self.ledger, self.paper,
                           require_adjudicated_secondary=True)

    def test_rejects_unanchored_or_nonsecondary_wording(self):
        bad = self.value()
        bad['decisions'][0]['secondary_finding']['quote'] = 'Absent quote'
        with self.assertRaisesRegex(ValueError, 'absent or ambiguous'):
            augment_ledger(bad, self.ledger, self.paper,
                           require_adjudicated_secondary=True)
        bad = self.value()
        bad['decisions'][0]['presentation'] = 'omitted'
        with self.assertRaisesRegex(ValueError, 'supporting presentation'):
            augment_ledger(bad, self.ledger, self.paper,
                           require_adjudicated_secondary=True)

    def test_emits_adjudicated_comment_and_retains_original_for_audit(self):
        value = self.value()
        augmented = augment_ledger(value, self.ledger, self.paper,
                                   require_adjudicated_secondary=True)
        audit = validate_revision(value, augmented, self.paper)
        package = revision_package(value, augmented, audit)
        secondary = adjudicated_secondary(value, augmented, self.paper)
        packaged = attach_adjudicated_secondary(package, secondary)
        self.assertEqual(packaged['adjudicated_secondary'], [{
            'issue_id': 'issue-1', 'finding': self.secondary_finding(),
        }])
        inventory = packaged['candidate_inventory']['secondary_candidates'][0]
        self.assertEqual(inventory['text'], self.secondary_finding()['comment'])
        self.assertEqual(inventory['original_text'], self.original['comment'])
        self.assertEqual(packaged['source_findings'][0]['finding']['comment'], self.original['comment'])
        self.assertEqual(value['decisions'][0]['secondary_finding'], self.secondary_finding())

    def test_selected_or_resolved_decisions_cannot_supply_secondary_wording(self):
        value = self.value()
        decision = value['decisions'][0]
        decision['scientific_status'] = 'contradicted'
        decision['presentation'] = 'omitted'
        decision['secondary_finding'] = self.secondary_finding()
        decision['components'][0].update(treatment='resolved', evidence=decision['evidence'])
        with self.assertRaisesRegex(ValueError, 'Only retained secondary'):
            augment_ledger(value, self.ledger, self.paper,
                           require_adjudicated_secondary=True)

    def test_secondary_replacement_is_rejected_before_the_edit_guard_runs(self):
        value = self.value()
        value['decisions'][0]['secondary_finding']['replacement'] = 'Add replicate counts.'
        with self.assertRaisesRegex(ValueError, 'unguarded replacement'):
            augment_ledger(value, self.ledger, self.paper,
                           require_adjudicated_secondary=True)

    def test_mixed_resolved_and_deferred_components_need_adjudicated_secondary(self):
        original = {**self.original,
                    'comment': 'Report the replicate counts. Explain how replicates were assigned to analyses.'}
        ledger = issue_ledger({'findings': [original]})
        value = self.value()
        decision = value['decisions'][0]
        decision['components'] = [
            {'component_id': 'issue-1-part-1', 'treatment': 'resolved',
             'final_finding_number': 0, 'final_quote': '',
             'reason': 'The manuscript states the counts elsewhere.',
             'evidence': [{'block_id': 'b1', 'quote': 'Replicate counts are not specified.'}]},
            {'component_id': 'issue-1-part-2', 'treatment': 'deferred',
             'final_finding_number': 0, 'final_quote': '',
             'reason': 'The assignment remains unclear.', 'evidence': []},
        ]
        augmented = augment_ledger(value, ledger, self.paper,
                                   require_adjudicated_secondary=True)
        self.assertEqual(adjudicated_secondary(value, augmented, self.paper)[0]['issue_id'], 'issue-1')
        package = revision_package(value, augmented, validate_revision(value, augmented, self.paper))
        self.assertEqual(package['candidate_inventory']['secondary_candidates'][0]['selection_outcome'],
                         'supported_secondary')
        value['decisions'][0]['secondary_finding'] = None
        with self.assertRaisesRegex(ValueError, 'requires an adjudicated finding'):
            augment_ledger(value, ledger, self.paper,
                           require_adjudicated_secondary=True)

    def test_unanchored_unresolved_hypothesis_is_audit_only(self):
        ledger = issue_ledger({'findings': [], 'discarded_hypotheses': [
            'The uninspected image may contain a relevant control.',
        ]})
        value = {'summary': 'Review', 'findings': [], 'limitations': [], 'new_candidates': [],
                 'decisions': [{
                     'issue_id': 'hypothesis-1', 'scientific_status': 'unresolved',
                     'presentation': 'omitted',
                     'strongest_criticism': 'The image was not inspected.',
                     'strongest_author_defense': 'No manuscript anchor supports a finding.',
                     'decisive_question': 'Does the image contain a relevant control?',
                     'decision_reason': 'The available evidence does not answer the hypothesis.',
                     'evidence': [], 'secondary_finding': None,
                     'components': [{
                         'component_id': 'hypothesis-1-part-1', 'treatment': 'deferred',
                         'final_finding_number': 0, 'final_quote': '',
                         'reason': 'This remains an unanchored audit note.', 'evidence': [],
                     }],
                 }]}
        augmented = augment_ledger(value, ledger, self.paper,
                                   require_adjudicated_secondary=True)
        audit = validate_revision(value, augmented, self.paper)
        package = attach_adjudicated_secondary(
            revision_package(value, augmented, audit),
            adjudicated_secondary(value, augmented, self.paper),
            audit_only_secondary(value, augmented),
        )
        self.assertEqual(package['adjudicated_secondary'], [])
        self.assertEqual(package['candidate_inventory']['secondary_candidates'], [])
        self.assertEqual(package['candidate_inventory']['audit_only_candidates'], [{
            'issue_id': 'hypothesis-1',
            'discovery_status': 'investigator_discarded_hypothesis',
            'selection_outcome': 'unresolved_secondary',
            'scientific_status': 'unresolved', 'presentation': 'omitted',
            'component_treatments': ['deferred'], 'title': '',
            'text': 'The uninspected image may contain a relevant control.',
            'decision_reason': 'The available evidence does not answer the hypothesis.',
            'audience': 'audit_only',
        }])

    def test_protocol_and_package_versions_are_ensemble_v3(self):
        self.assertEqual(_protocol('gpt-5.6-terra', 2, 750000)['version'], 'ensemble-v3')


if __name__ == '__main__':
    unittest.main()
