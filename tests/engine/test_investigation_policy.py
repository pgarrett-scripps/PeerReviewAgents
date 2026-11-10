import unittest
from peerreviewagents.engine.investigation_policy import audit_tool_checks, with_tool_checks
from peerreviewagents.engine.review_schema import REVIEW
from peerreviewagents.engine.domain_registry import route


class InvestigationPolicyTests(unittest.TestCase):
    def test_missing_fabricated_failed_and_note_calls_do_not_complete(self):
        check = {'question': 'Does the table support the claim?', 'call_index': 1,
                 'tool': 'view_page', 'outcome': 'unresolved', 'interpretation': 'The table leaves this unresolved.'}
        self.assertFalse(audit_tool_checks({}, [])['complete'])
        self.assertFalse(audit_tool_checks({'tool_checks': [check]}, [])['complete'])
        for tool, payload in [('view_page', {'request_rejected': 'Bad page'}),
                              ('lookup_source', {'eligible': False}), ('save_note', {'saved': True})]:
            row = {'tool': tool, 'result': {**payload, 'tool_receipt': {'call_index': 1, 'tool': tool}}}
            self.assertFalse(audit_tool_checks({'tool_checks': [{**check, 'tool': tool}]}, [row])['complete'])
        row = {'tool': 'view_page', 'result': {'label': 'Original page 2',
               'tool_receipt': {'call_index': 1, 'tool': 'view_page'}}}
        self.assertTrue(audit_tool_checks({'tool_checks': [check]}, [row])['complete'])
        self.assertFalse(audit_tool_checks({'tool_checks': [check]}, [row, row])['complete'])

    def test_schema_does_not_change_baseline(self):
        schema = with_tool_checks(REVIEW)
        self.assertIn('tool_checks', schema['required'])
        self.assertNotIn('tool_checks', REVIEW['properties'])

    def test_empty_search_has_receipt_but_no_returned_evidence(self):
        row={'tool':'search','result':{'matches':[],
             'tool_receipt':{'call_index':1,'tool':'search'}}}
        review={'tool_checks':[{'question':'Check a phrase','call_index':1,'tool':'search',
                               'outcome':'unresolved','interpretation':'No match was returned.'}]}
        audit=audit_tool_checks(review,[row])
        self.assertFalse(audit['complete'])
        self.assertTrue(audit['checks'][0]['receipt_verified'])
        self.assertFalse(audit['checks'][0]['evidence_returned'])
        self.assertTrue(audit_tool_checks(review,[row],version='targeted-check-v1')['complete'])

    def test_ecology_routes_body_not_reference_or_generic_language(self):
        for text in ['We study natal dispersal in wild birds.', 'Breeding success varies among habitats.']:
            profile = route({'blocks': [{'id': 'b1', 'text': text, 'section': 'Methods'}]})
            self.assertIn('ecology-conservation', [p['id'] for p in profile['selected']])
            profile = route({'blocks': [{'id': 'b1', 'text': text, 'section': 'References'}]})
            self.assertNotIn('ecology-conservation', [p['id'] for p in profile['selected']])
        profile = route({'blocks': [{'id': 'b1', 'text': 'The population received treatment.', 'section': 'Methods'}]})
        self.assertNotIn('ecology-conservation', [p['id'] for p in profile['selected']])
