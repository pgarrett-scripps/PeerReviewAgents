"""Contract checks for the matched ordinary synthesis ablation."""
import unittest

from peerreviewagents.engine.ensemble import _protocol
from peerreviewagents.engine.ensemble_debate import (ensemble_schema, ordinary_synthesis_prompt)


class OrdinarySynthesisTests(unittest.TestCase):
    def test_prompt_is_direct_synthesis_with_the_same_decision_contract(self):
        paper = {'title': 'Example'}
        ledger = {'issues': [{'issue_id': 'r1-issue-1', 'components': [
            {'component_id': 'r1-issue-1-part-1', 'text': 'Clarify the sample unit.'}]}]}
        prompt = ordinary_synthesis_prompt(paper, '[b1] manuscript text', ledger)
        self.assertIn('Synthesize the supplied independent manuscript reviews', prompt)
        self.assertIn('r1-issue-1', prompt)
        self.assertIn('[b1] manuscript text', prompt)
        self.assertNotIn('strongest author defense', prompt.casefold())
        self.assertNotIn('opposing arguments', prompt.casefold())
        schema = ensemble_schema()
        self.assertIn('decisions', schema['properties'])
        self.assertIn('new_candidates', schema['properties'])

    def test_matched_protocol_has_one_synthesis_call_and_two_tools(self):
        protocol = _protocol('gpt-5.6-terra', 2, 750000)
        self.assertEqual(protocol['total_generation_calls'], 3)
        self.assertEqual(protocol['debate_tool_allowance'], 2)


if __name__ == '__main__':
    unittest.main()
