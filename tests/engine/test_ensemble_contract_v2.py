import unittest

from peerreviewagents.engine.debate_revision import issue_ledger
from peerreviewagents.engine.ensemble_debate import augment_ledger, ensemble_schema, normalize_new_decisions
from peerreviewagents.engine.evidence_handles import with_evidence_handles, constrain_manuscript_handles, build_registry


def finding():
    return {'title': 'Control replication', 'block_id': 'b2',
            'quote': 'The control sample was measured once.', 'kind': 'question',
            'severity': 'moderate', 'comment': 'Clarify whether the control was replicated.',
            'replacement': '', 'evidence_blocks': ['b2'], 'counterevidence': '',
            'source_urls': [], 'source_evidence': []}


def embedded(selected=False):
    return {'scientific_status': 'supported_clarification' if selected else 'unresolved',
            'presentation': 'primary' if selected else 'supporting',
            'strongest_criticism': 'Replication is not described.',
            'strongest_author_defense': 'The outcome is reported.',
            'decisive_question': 'Was the control replicated?',
            'decision_reason': 'The manuscript leaves the control design unclear.',
            'evidence': [{'block_id': 'b2', 'quote': 'The control sample was measured once.'}] if selected else [],
            'secondary_finding': None,
            'component': {'treatment': 'preserved' if selected else 'deferred',
                          'final_finding_number': 2 if selected else 0,
                          'final_quote': finding()['comment'] if selected else '',
                          'reason': 'The candidate remains consequential.', 'evidence': []}}


class EnsembleContractV2Tests(unittest.TestCase):
    def setUp(self):
        self.paper = {'title': 'Study', 'blocks': [
            {'id': 'b1', 'text': 'Replicate counts are not specified.'},
            {'id': 'b2', 'text': 'The control sample was measured once.'},
        ]}
        self.original = {'title': 'Replication', 'block_id': 'b1',
                         'quote': 'Replicate counts are not specified.', 'kind': 'question',
                         'severity': 'major', 'comment': 'Report replicate counts.',
                         'replacement': '', 'evidence_blocks': ['b1'],
                         'counterevidence': '', 'source_urls': []}
        self.ledger = issue_ledger({'findings': [self.original]})

    def value(self):
        known = {'issue_id': 'issue-1', 'scientific_status': 'supported_clarification',
                 'presentation': 'primary', 'strongest_criticism': 'Counts are absent.',
                 'strongest_author_defense': 'The result is reported.',
                 'decisive_question': 'Are replicates defined?',
                 'decision_reason': 'Counts are not reported.',
                 'evidence': [{'block_id': 'b1', 'quote': 'Replicate counts are not specified.'}],
                 'secondary_finding': None,
                 'components': [{'component_id': 'issue-1-part-1', 'treatment': 'preserved',
                                 'final_finding_number': 1, 'final_quote': 'Report replicate counts.',
                                 'reason': 'This remains consequential.', 'evidence': []}]}
        return {'summary': 'Review', 'findings': [self.original, finding()], 'limitations': [],
                'decisions': [known],
                'new_candidates': [{'issue_id': 'new-1', 'finding': finding(), 'decision': embedded(True)}]}

    def test_schema_requires_complete_embedded_scientific_judgment(self):
        schema = ensemble_schema()
        candidate = schema['properties']['new_candidates']['items']
        decision = candidate['properties']['decision']
        self.assertIn('decision', candidate['required'])
        self.assertNotIn('issue_id', decision['properties'])
        self.assertNotIn('components', decision['properties'])
        self.assertIn('secondary_finding', decision['properties'])
        top_decision = schema['properties']['decisions']['items']
        self.assertIn('secondary_finding', top_decision['required'])
        self.assertEqual(set(decision['required']), set(decision['properties']))
        self.assertNotIn('component_id', decision['properties']['component']['properties'])
        self.assertEqual(set(decision['properties']['component']['required']),
                         set(decision['properties']['component']['properties']))

    def test_normalizer_materializes_deterministic_ids_without_mutation(self):
        original = self.value()
        normalized = normalize_new_decisions(original)
        self.assertIn('decision', original['new_candidates'][0])
        self.assertNotIn('decision', normalized['new_candidates'][0])
        decision = normalized['decisions'][-1]
        self.assertEqual(decision['issue_id'], 'new-1')
        self.assertEqual(decision['components'][0]['component_id'], 'new-1-part-1')
        self.assertEqual(decision['scientific_status'], 'supported_clarification')

    def test_normalizer_rejects_duplicate_top_level_or_missing_embedded_decision(self):
        missing = self.value()
        missing['new_candidates'][0].pop('decision')
        with self.assertRaisesRegex(ValueError, 'embedded decision'):
            normalize_new_decisions(missing)
        duplicate = self.value()
        duplicate['decisions'].append({'issue_id': 'new-1'})
        with self.assertRaisesRegex(ValueError, 'Top-level decisions'):
            normalize_new_decisions(duplicate)

    def test_selected_new_candidate_validates_after_materialization(self):
        value = normalize_new_decisions(self.value())
        augmented = augment_ledger(value, self.ledger, self.paper)
        self.assertEqual(augmented['issues'][-1]['issue_id'], 'new-1')

    def test_nested_new_candidate_source_references_become_handles(self):
        schema = with_evidence_handles(ensemble_schema())
        source = schema['properties']['new_candidates']['items']['properties']['finding']['properties']['source_evidence']['items']
        self.assertIn('source_handle', source['properties'])
        self.assertNotIn('evidence_id', source['properties'])
        evidence = schema['properties']['new_candidates']['items']['properties']['decision']['properties']['evidence']['items']
        self.assertIn('evidence_handle', evidence['properties'])
        self.assertNotIn('block_id', evidence['properties'])

    def test_nullable_secondary_references_are_constrained_handles(self):
        schema = constrain_manuscript_handles(with_evidence_handles(ensemble_schema()), build_registry(self.paper))
        secondary = schema['properties']['decisions']['items']['properties']['secondary_finding']['anyOf'][0]
        self.assertNotIn('block_id', secondary['properties'])
        self.assertEqual(secondary['properties']['anchor_handle'], {'$ref':'#/$defs/manuscriptEvidenceHandle'})
        self.assertIn('source_handle', secondary['properties']['source_evidence']['items']['properties'])


if __name__ == '__main__':
    unittest.main()
