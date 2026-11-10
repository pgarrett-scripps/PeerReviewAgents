"""Offline checks for restricted ensemble final-quote recovery."""
import copy
import unittest

from peerreviewagents.engine.debate_revision import issue_ledger
from peerreviewagents.engine.ensemble_references import recover


class EnsembleReferenceRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.paper = {'blocks': [{'id': 'b1', 'text': 'The sample count is not reported.'}]}
        self.finding = {'title': 'Count', 'block_id': 'b1', 'quote': 'sample count',
                        'kind': 'question', 'severity': 'moderate',
                        'comment': 'Please report the sample count', 'replacement': '',
                        'evidence_blocks': ['b1'], 'counterevidence': '', 'source_urls': []}
        self.ledger = issue_ledger({'findings': [self.finding]})

    def value(self, final_quote='please report the sample count.'):
        component = self.ledger['issues'][0]['components'][0]
        return {'summary': 'Review', 'findings': [copy.deepcopy(self.finding)], 'limitations': [],
                'decisions': [{'issue_id': 'issue-1', 'scientific_status': 'supported_clarification',
                    'presentation': 'primary', 'strongest_criticism': 'The count is absent.',
                    'strongest_author_defense': 'The result is described.',
                    'decisive_question': 'What is the count?',
                    'decision_reason': 'The count remains unclear.',
                    'evidence': [{'block_id': 'b1', 'quote': 'sample count'}],
                    'components': [{'component_id': component['component_id'], 'treatment': 'preserved',
                        'final_finding_number': 1, 'final_quote': final_quote,
                        'reason': 'The request is retained.', 'evidence': []}]}]}

    def test_repairs_first_letter_case_and_one_trailing_punctuation(self):
        original = self.value()
        result = recover(original, self.ledger, self.paper)
        self.assertEqual(result['recovered']['decisions'][0]['components'][0]['final_quote'],
                         'Please report the sample count')
        receipt = result['changed_fields'][0]
        self.assertEqual((receipt['issue_id'], receipt['component_id']), ('issue-1', 'issue-1-part-1'))
        self.assertEqual((receipt['before'], receipt['after']),
                         ('please report the sample count.', 'Please report the sample count'))
        self.assertEqual(result['recovered']['findings'], original['findings'])
        self.assertEqual(original['decisions'][0]['components'][0]['final_quote'],
                         'please report the sample count.')

    def test_rejects_ambiguous_and_paraphrased_references(self):
        ambiguous = self.value()
        ambiguous['findings'][0]['comment'] = ('Please report the sample count. '
                                               'Please report the sample count.')
        result = recover(ambiguous, self.ledger, self.paper)
        self.assertIsNone(result['recovered'])
        paraphrased = self.value('Please provide the sample count.')
        self.assertIsNone(recover(paraphrased, self.ledger, self.paper)['recovered'])

    def test_does_not_repair_unrelated_invalid_source_or_destination(self):
        bad_source = self.value()
        bad_source['decisions'][0]['evidence'][0]['quote'] = 'not present'
        self.assertEqual(recover(bad_source, self.ledger, self.paper)['original']['error'],
                         'Invalid decision source quotation')
        wrong_destination = self.value()
        wrong_destination['decisions'][0]['components'][0]['final_finding_number'] = 2
        self.assertIsNone(recover(wrong_destination, self.ledger, self.paper)['recovered'])

    def test_repairs_after_presentation_recovery_exposes_the_quote_error(self):
        ledger = copy.deepcopy(self.ledger)
        second_issue = copy.deepcopy(ledger['issues'][0])
        second_issue['issue_id'] = 'issue-2'
        second_issue['components'][0]['component_id'] = 'issue-2-part-1'
        ledger['issues'].append(second_issue)
        value = self.value('Please report the sample count')
        value['decisions'][0]['presentation'] = 'supporting'
        second = copy.deepcopy(value['decisions'][0])
        second['issue_id'] = 'issue-2'
        second['presentation'] = 'primary'
        second['components'][0].update(component_id='issue-2-part-1',
                                       final_quote='please report the sample count.')
        value['decisions'].append(second)
        result = recover(value, ledger, self.paper)
        self.assertEqual(result['recovered']['decisions'][0]['presentation'], 'merged')
        self.assertEqual(result['recovered']['decisions'][1]['components'][0]['final_quote'],
                         'Please report the sample count')
        self.assertEqual([receipt['field'] for receipt in result['changed_fields']],
                         ['presentation', 'final_quote'])

    def test_case_only_terminal_period_repair_keeps_valid_sibling_unchanged(self):
        ledger = copy.deepcopy(self.ledger)
        second_issue = copy.deepcopy(ledger['issues'][0])
        second_issue['issue_id'] = 'issue-2'
        second_issue['components'][0]['component_id'] = 'issue-2-part-1'
        ledger['issues'].append(second_issue)
        valid_quote = 'Please report the sample count'
        value = self.value(valid_quote)
        value['findings'][0]['comment'] = (
            'Please report the sample count and describe the result as preliminary '
            'pending the planned larger sample.')
        second = copy.deepcopy(value['decisions'][0])
        second['issue_id'] = 'issue-2'
        second['components'][0].update(
            component_id='issue-2-part-1',
            final_quote='Describe the result as preliminary pending the planned larger sample.')
        value['decisions'].append(second)
        result = recover(value, ledger, self.paper)
        components = [decision['components'][0]['final_quote']
                      for decision in result['recovered']['decisions']]
        self.assertEqual(components, [
            valid_quote,
            'describe the result as preliminary pending the planned larger sample.',
        ])
        self.assertEqual(len(result['changed_fields']), 1)
        self.assertEqual(result['changed_fields'][0]['before'],
                         'Describe the result as preliminary pending the planned larger sample.')


if __name__ == '__main__':
    unittest.main()
