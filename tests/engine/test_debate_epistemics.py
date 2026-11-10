"""Epistemic status remains separate from debate accounting."""
import copy
import unittest

from peerreviewagents.engine.annotations import normalize_review
from peerreviewagents.engine.debate_revision import issue_ledger, revision_package, validate_revision
from peerreviewagents.engine.storage import digest


class DebateEpistemicTests(unittest.TestCase):
    def setUp(self):
        text = "Participants completed protocol A before protocol B."
        self.paper = {
            "id": "paper",
            "title": "Protocol order",
            "source_sha256": "source",
            "blocks": [{"id": "b1", "text": text, "sha256": digest(text)}],
        }
        self.note = "Protocol order may confound comparisons. Within-protocol contrasts may remain valid."
        self.ledger = issue_ledger({"findings": [], "discarded_hypotheses": [self.note]})
        comment = "Could protocol order affect cross-protocol comparisons? Please retain the valid within-protocol contrasts."
        self.finding = {
            "title": "Protocol order",
            "block_id": "b1",
            "quote": text,
            "kind": "question",
            "severity": "moderate",
            "comment": comment,
            "replacement": "",
            "evidence_blocks": ["b1"],
            "counterevidence": "Within-protocol contrasts may remain valid.",
            "source_urls": [],
        }
        self.evidence = [{"block_id": "b1", "quote": text}]
        self.value = {
            "summary": "A scoped question remains.",
            "findings": [self.finding],
            "limitations": [],
            "decisions": [{
                "issue_id": "hypothesis-1",
                "scientific_status": "supported_concern",
                "presentation": "primary",
                "strongest_criticism": "Order may affect cross-protocol comparisons.",
                "strongest_author_defense": "Within-protocol contrasts remain interpretable.",
                "decisive_question": "Can order be separated from protocol?",
                "decision_reason": "The narrower question remains useful.",
                "evidence": self.evidence,
                "components": [{
                    "component_id": component["component_id"],
                    "treatment": "preserved",
                    "final_finding_number": 1,
                    "final_quote": comment,
                    "reason": "The scoped question retains the qualification.",
                    "evidence": [],
                } for component in self.ledger["issues"][0]["components"]],
            }],
        }

    def test_restored_hypothesis_is_not_promoted_to_confirmed_defect(self):
        checked = validate_revision(self.value, self.ledger, self.paper)
        record = checked["epistemic_audit"]["issues"][0]
        self.assertEqual(record["assertion_status"], "unverified_hypothesis")
        self.assertEqual(record["selection_status"], "selected")
        self.assertEqual(record["source_link_status"], "exact_quotes_validated")
        self.assertEqual(record["scientific_truth_status"], "not_mechanically_determined")
        self.assertTrue(record["restored_from_hypothesis"])

    def test_selection_does_not_control_assertion_status(self):
        selected = validate_revision(self.value, self.ledger, self.paper)["epistemic_audit"]["issues"][0]
        omitted_value = copy.deepcopy(self.value)
        omitted_value["findings"] = []
        decision = omitted_value["decisions"][0]
        decision.update(scientific_status="unresolved", presentation="supporting", evidence=[])
        for component in decision["components"]:
            component.update(treatment="deferred", final_finding_number=0,
                             final_quote="", reason="More evidence is needed.")
        omitted = validate_revision(omitted_value, self.ledger, self.paper)["epistemic_audit"]["issues"][0]
        self.assertEqual(selected["assertion_status"], omitted["assertion_status"])
        self.assertNotEqual(selected["selection_status"], omitted["selection_status"])

    def test_history_preserves_defenses_without_recursive_packages(self):
        first_check = validate_revision(self.value, self.ledger, self.paper)
        first_package = revision_package(self.value, self.ledger, first_check)
        next_ledger = issue_ledger(normalize_review(self.paper, first_package, "round-one"))
        second_value = copy.deepcopy(self.value)
        second_value["decisions"][0]["strongest_author_defense"] = (
            "Order was fixed by operational constraints, while within-protocol estimates remain useful."
        )
        second_check = validate_revision(second_value, next_ledger, self.paper)
        record = second_check["epistemic_audit"]["issues"][0]
        self.assertEqual(len(record["decision_history"]), 2)
        self.assertEqual(len(record["qualifications"]), 2)
        second_package = revision_package(second_value, next_ledger, second_check)
        third_ledger = issue_ledger(normalize_review(self.paper, second_package, "round-two"))
        third_check = validate_revision(second_value, third_ledger, self.paper)
        third = third_check["epistemic_audit"]["issues"][0]
        self.assertEqual(len(third["decision_history"]), 2)
        self.assertTrue(all("decision_history" not in row for row in third["decision_history"]))
        self.assertTrue(all(row["inherited"] for row in third["decision_history"][:-1]))

    def test_candidate_concern_keeps_source_link_and_truth_separate(self):
        finding_ledger = issue_ledger({"findings": [self.finding]})
        value = copy.deepcopy(self.value)
        value["decisions"][0]["issue_id"] = "issue-1"
        for component, source in zip(value["decisions"][0]["components"],
                                     finding_ledger["issues"][0]["components"]):
            component["component_id"] = source["component_id"]
        record = validate_revision(value, finding_ledger, self.paper)["epistemic_audit"]["issues"][0]
        self.assertEqual(record["assertion_status"], "source_linked_concern")
        self.assertEqual(record["scientific_truth_status"], "not_mechanically_determined")

    def test_package_exposes_audit_and_limitations(self):
        checked = validate_revision(self.value, self.ledger, self.paper)
        package = revision_package(self.value, self.ledger, checked)
        self.assertEqual(package["epistemic_audit"]["version"], "debate-epistemic-audit-v1")
        self.assertTrue(any("not scientific truth" in item
                            for item in package["epistemic_audit"]["limitations"]))


if __name__ == "__main__":
    unittest.main()
