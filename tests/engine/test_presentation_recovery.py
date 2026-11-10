"""Offline checks for the restricted presentation recovery replay."""
import copy
import unittest

from peerreviewagents.engine.debate_presentation import recover
from peerreviewagents.engine.debate_revision import issue_ledger
from peerreviewagents.engine.storage import digest


class PresentationRecoveryTest(unittest.TestCase):
    def setUp(self):
        text = "The sample count is not reported."
        self.paper = {"blocks": [{"id": "b1", "text": text, "sha256": digest(text)}]}
        self.finding = {"title": "Count", "block_id": "b1", "quote": "sample count",
                        "kind": "question", "severity": "moderate",
                        "comment": "Please report the sample count.", "replacement": "",
                        "evidence_blocks": ["b1"]}
        self.ledger = issue_ledger({"findings": [self.finding]})

    def decision(self, presentation="supporting", status="supported_clarification", evidence=None):
        component = self.ledger["issues"][0]["components"][0]
        reference = {"block_id": "b1", "quote": "sample count"}
        return {"issue_id": "issue-1", "scientific_status": status, "presentation": presentation,
                "strongest_criticism": "The count is missing.", "strongest_author_defense": "The result is described.",
                "decisive_question": "What is the count?", "decision_reason": "The count remains unclear.",
                "evidence": evidence if evidence is not None else [reference],
                "components": [{"component_id": component["component_id"], "treatment": "preserved",
                                "final_finding_number": 1, "final_quote": "Please report the sample count.",
                                "reason": "The request is retained.", "evidence": []}]}

    def value(self, decision):
        return {"summary": "Review", "findings": [copy.deepcopy(self.finding)],
                "limitations": [], "decisions": [decision]}

    def test_recovers_only_the_redundant_presentation_label(self):
        original = self.value(self.decision())
        result = recover(original, self.ledger, self.paper)
        self.assertEqual(result["original"]["error"], "Priority conflicts with component destinations")
        self.assertEqual(result["recovered"]["decisions"][0]["presentation"], "merged")
        self.assertEqual(result["recovered"]["decisions"][0]["scientific_status"], "supported_clarification")
        self.assertEqual(result["recovered"]["findings"], original["findings"])
        self.assertEqual(original["decisions"][0]["presentation"], "supporting")
        self.assertTrue(result["audit"]["structural_validity"])
        self.assertEqual(result["changed_fields"][0]["field"], "presentation")

    def test_does_not_swallow_scientific_or_reference_failures(self):
        original = self.value(self.decision(evidence=[]))
        result = recover(original, self.ledger, self.paper)
        self.assertIsNone(result["recovered"])
        self.assertEqual(result["original"]["error"], "Scientific decision requires source evidence")

        original = self.value(self.decision())
        original["decisions"][0]["evidence"][0]["quote"] = "not present"
        result = recover(original, self.ledger, self.paper)
        self.assertIsNone(result["recovered"])
        self.assertEqual(result["original"]["error"], "Invalid decision source quotation")

    def test_preserved_primary_to_merged_and_unpreserved_primary_to_supported_secondary(self):
        result = recover(self.value(self.decision(presentation="omitted")), self.ledger, self.paper)
        self.assertEqual(result["recovered"]["decisions"][0]["presentation"], "merged")

        decision = self.decision(presentation="primary")
        decision["components"][0].update(treatment="deferred", final_finding_number=0, final_quote="")
        result = recover({"summary": "Review", "findings": [], "limitations": [], "decisions": [decision]}, self.ledger, self.paper)
        self.assertEqual(result["recovered"]["decisions"][0]["presentation"], "supporting")

    def test_revalidation_rejects_an_error_after_the_presentation_conflict(self):
        ledger=copy.deepcopy(self.ledger)
        issue=copy.deepcopy(ledger['issues'][0])
        issue['issue_id']='issue-2'
        issue['components'][0]['component_id']='issue-2-part-1'
        ledger['issues'].append(issue)
        value=self.value(self.decision())
        second=self.decision(presentation='primary')
        second['issue_id']='issue-2'
        second['components'][0]['component_id']='issue-2-part-1'
        second['evidence'][0]['quote']='Absent source quotation'
        value['decisions'].append(second)
        result=recover(value,ledger,self.paper)
        self.assertEqual(result['original']['error'],'Priority conflicts with component destinations')
        self.assertIsNone(result['recovered'])
        self.assertEqual(result['audit']['error'],'Invalid decision source quotation')


if __name__ == "__main__":
    unittest.main()
