"""Tests for deterministic evidence planning, execution, and validation."""

import base64
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from peerreviewagents.engine.evidence_tasks import (
    EVIDENCE_REVIEW,
    PLAN,
    complete_inspected_images,
    normalize_plan,
    planner_prompt,
    prepare_evidence,
    validate_task_results,
)
from peerreviewagents.engine.storage import digest, write


class FakeInvestigation:
    instances = []

    def __init__(self, config):
        self.config = config
        self.calls = []
        self.__class__.instances.append(self)

    def call(self, name, arguments):
        self.calls.append((name, arguments))
        if name == "view_page":
            return {
                "label": f"Original PDF page {arguments['page']}",
                "image": base64.b64encode(b"synthetic-png-page").decode(),
            }
        if name == "read_blocks":
            blocks = [
                {"id": block_id, "text": "The treatment used 12 samples."}
                for block_id in arguments["ids"]
            ]
            return {"blocks": blocks}
        if name == "search":
            return {"matches": [{"id": "b2", "text": "A matched control was used."}]}
        return {"expression": arguments["expression"], "result": 4, "receipt": "calculation-receipt"}


class FailedPageInvestigation(FakeInvestigation):
    def call(self, name, arguments):
        self.calls.append((name, arguments))
        if name == "view_page":
            return {"request_rejected": "Synthetic page failure"}
        return super().call(name, arguments)


class EvidenceTasks(unittest.TestCase):
    def setUp(self):
        FakeInvestigation.instances = []
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "workspace"
        self.output = self.root / "evidence"
        self.workspace.mkdir()
        original = b"%PDF-1.4\nsynthetic evidence fixture\n%%EOF\n"
        (self.workspace / "original.pdf").write_bytes(original)
        self.paper = {
            "id": "paper-test",
            "title": "Synthetic experiment",
            "source_sha256": digest(original),
            "statistics": {"pdf_pages": 2, "total_words": 12},
            "assets": [],
            "blocks": [
                {"id": "b1", "text": "The treatment used 12 samples.", "sha256": digest("The treatment used 12 samples.")},
                {"id": "b2", "text": "A matched control was used.", "sha256": digest("A matched control was used.")},
            ],
        }
        write(self.workspace / "paper.json", self.paper)
        write(self.workspace / "manifest.json", {
            "files": {
                "paper.json": digest((self.workspace / "paper.json").read_bytes()),
                "original.pdf": digest(original),
            }
        })
        self.plan = {
            "experiment_map": [{
                "id": "exp1",
                "claim": "Treatment changes the outcome.",
                "intervention_or_comparison": "Treatment compared with matched control.",
                "outcome": "Reported response.",
                "experimental_unit": "Sample.",
                "methods": ["Imaging"],
                "locations": ["Figure 1"],
                "source_blocks": ["b1", "b2"],
                "unresolved_facts": ["Pairing is unknown."],
            }],
            "tasks": [
                {
                    "id": "task1",
                    "question": "Does Figure 1 show the reported comparison?",
                    "source_blocks": ["b1"],
                    "operations": [
                        {
                            "id": "op-page",
                            "required": True,
                            "name": "view_page",
                            "arguments": {"page": 1, "ids": [], "query": "", "expression": ""},
                        },
                        {
                            "id": "op-read",
                            "required": True,
                            "name": "read_blocks",
                            "arguments": {"page": 0, "ids": ["b1"], "query": "", "expression": ""},
                        },
                    ],
                },
                {
                    "id": "task2",
                    "question": "Is the matched control described elsewhere?",
                    "source_blocks": ["b2"],
                    "operations": [
                        {
                            "id": "op-search",
                            "required": True,
                            "name": "search",
                            "arguments": {"page": 0, "ids": [], "query": "matched control", "expression": ""},
                        },
                        {
                            "id": "op-calc",
                            "required": False,
                            "name": "calculate",
                            "arguments": {"page": 0, "ids": [], "query": "", "expression": "mean([2,4,6])"},
                        },
                    ],
                },
            ],
        }

    def tearDown(self):
        self.temporary.cleanup()

    def prepare(self, investigation=FakeInvestigation):
        with patch("peerreviewagents.engine.evidence_tasks.Investigation", investigation):
            return prepare_evidence(self.workspace, self.plan, self.output)

    def test_model_schemas_are_flat_and_bounded(self):
        self.assertNotIn("oneOf", str(PLAN))
        operation = PLAN["properties"]["tasks"]["items"]["properties"]["operations"]["items"]
        self.assertEqual(
            set(operation["properties"]["arguments"]["required"]),
            {"page", "ids", "query", "expression"},
        )
        self.assertEqual(PLAN["properties"]["experiment_map"]["maxItems"], 8)
        self.assertEqual(EVIDENCE_REVIEW["properties"]["findings"]["maxItems"], 8)
        self.assertEqual(EVIDENCE_REVIEW["properties"]["selected_finding_numbers"]["maxItems"], 5)

    def test_prompt_states_source_and_operation_limits(self):
        prompt = planner_prompt(self.paper, "[b1] Manuscript text", {"warning": "advisory"})
        self.assertIn("no more than four operations", prompt)
        self.assertIn("at least one original PDF page", prompt)
        self.assertIn("[b1] Manuscript text", prompt)
        self.assertIn('"warning": "advisory"', prompt)

    def test_normalize_plan_expands_ranges_in_source_order_and_audits_changes(self):
        paper = copy.deepcopy(self.paper)
        paper["blocks"] = [
            {"id": f"b{number:04d}", "text": str(number)}
            for number in range(91, 98)
        ]
        plan = copy.deepcopy(self.plan)
        plan["experiment_map"][0]["source_blocks"] = ["b0091", "b0092–b0096", "b0096"]
        plan["tasks"][0]["source_blocks"] = ["b0092-b0094"]
        plan["tasks"][1]["source_blocks"] = ["b0097"]
        raw = copy.deepcopy(plan)
        normalized, audit = normalize_plan(paper, plan)
        self.assertEqual(plan, raw)
        self.assertEqual(
            normalized["experiment_map"][0]["source_blocks"],
            ["b0091", "b0092", "b0093", "b0094", "b0095", "b0096"],
        )
        self.assertEqual(
            normalized["tasks"][0]["source_blocks"],
            ["b0092", "b0093", "b0094"],
        )
        self.assertEqual(sum(row["change"] == "expanded_range" for row in audit), 2)
        self.assertEqual(sum(row["change"] == "removed_duplicate" for row in audit), 1)
        self.assertEqual(PLAN["properties"]["experiment_map"]["items"]["properties"]["source_blocks"]["maxItems"], 12)

    def test_normalize_plan_rejects_unknown_ambiguous_and_descending_ranges(self):
        for value, message in (
            ("missing", "Unknown or ambiguous"),
            ("b2-b1", "Descending"),
            ("b1-b2-b1", "ambiguous"),
        ):
            plan = copy.deepcopy(self.plan)
            plan["experiment_map"][0]["source_blocks"] = [value]
            with self.assertRaisesRegex(ValueError, message):
                normalize_plan(self.paper, plan)

    def test_executor_eagerly_runs_plan_and_freezes_images_and_receipts(self):
        prepared = self.prepare()
        self.assertEqual(
            FakeInvestigation.instances[0].calls,
            [
                ("view_page", {"page": 1}),
                ("read_blocks", {"ids": ["b1"]}),
                ("search", {"query": "matched control"}),
                ("calculate", {"expression": "mean([2,4,6])"}),
            ],
        )
        self.assertFalse(FakeInvestigation.instances[0].config["allow_literature"])
        self.assertEqual(len(prepared["receipts"]), 4)
        self.assertEqual(len(prepared["images"]), 1)
        image = prepared["images"][0]
        self.assertEqual(digest(Path(image["path"]).read_bytes()), image["sha256"])
        self.assertEqual(prepared["source_manifest_sha256"], digest((self.workspace / "manifest.json").read_bytes()))
        self.assertTrue(all(Path(self.output / "receipts" / f"{row['receipt_id']}.json").is_file() for row in prepared["receipts"]))

        FakeInvestigation.instances = []
        with patch("peerreviewagents.engine.evidence_tasks.Investigation", side_effect=AssertionError("cache should be reused")):
            resumed = prepare_evidence(self.workspace, self.plan, self.output)
        self.assertEqual(resumed, prepared)

    def test_boolean_search_is_a_failed_receipt_without_literal_dispatch(self):
        query = "replicate OR litter OR randomized OR blinded"
        self.plan["tasks"][1]["operations"][0]["arguments"]["query"] = query
        prepared = self.prepare()
        calls = FakeInvestigation.instances[0].calls
        self.assertNotIn(("search", {"query": query}), calls)
        self.assertEqual(len(calls), 3)
        receipt = next(row for row in prepared["receipts"] if row["operation_id"] == "op-search")
        self.assertEqual(receipt["status"], "failed")
        self.assertFalse(receipt["delivered"])
        self.assertEqual(receipt["arguments"], {"query": query})
        self.assertIn("literal manuscript search did not execute", receipt["result"]["request_rejected"])
        validated = validate_task_results(self.paper, self.response_for(prepared), prepared)
        task = next(row for row in validated["task_results"] if row["task_id"] == "task2")
        self.assertEqual(task["status"], "unresolved")
        self.assertIn("A required evidence operation failed", task["validation_problems"])

    def test_resume_rejects_changed_plan_and_image(self):
        prepared = self.prepare()
        changed = {**self.plan, "experiment_map": []}
        with self.assertRaisesRegex(ValueError, "different plan"):
            prepare_evidence(self.workspace, changed, self.output)
        Path(prepared["images"][0]["path"]).write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "image changed"):
            prepare_evidence(self.workspace, self.plan, self.output)

    def test_plan_validation_fails_closed_before_tool_use(self):
        bad = self.plan.copy()
        bad["tasks"] = [dict(self.plan["tasks"][0])]
        bad["tasks"][0]["operations"] = [dict(self.plan["tasks"][0]["operations"][0])]
        bad["tasks"][0]["operations"][0]["arguments"] = {
            "page": 3,
            "ids": [],
            "query": "",
            "expression": "",
        }
        with patch("peerreviewagents.engine.evidence_tasks.Investigation") as investigator:
            with self.assertRaisesRegex(ValueError, "unknown PDF page"):
                prepare_evidence(self.workspace, bad, self.output)
        investigator.assert_not_called()

    def test_failed_required_page_is_not_available_or_completed(self):
        plan = {**self.plan, "tasks": [self.plan["tasks"][0]]}
        self.plan = plan
        prepared = self.prepare(FailedPageInvestigation)
        self.assertEqual(prepared["images"], [])
        failed = prepared["receipts"][0]
        self.assertEqual(failed["status"], "failed")
        self.assertFalse(failed["delivered"])
        response = self.response_for(prepared)
        response["task_results"][0]["evidence_receipt_ids"] = [row["receipt_id"] for row in prepared["receipts"]]
        validated = validate_task_results(self.paper, response, prepared)
        task = validated["task_results"][0]
        self.assertEqual(task["status"], "unresolved")
        self.assertNotIn(failed["receipt_id"], task["evidence_receipt_ids"])
        self.assertIn("A required evidence operation failed", task["validation_problems"])

    def test_complete_inspected_images_rejects_silently_lost_success(self):
        investigation = self.root / "source-investigation"
        investigation.mkdir()
        (investigation / "page_001.png").write_bytes(b"verified-page")
        log = {
            "tool": "view_page",
            "arguments": {"page": 1},
            "result": {"label": "Original PDF page 1"},
        }
        (investigation / "tool-log.jsonl").write_text(json.dumps(log) + "\n")
        with patch("peerreviewagents.engine.reviewer.inspected_images", return_value=[]):
            with self.assertRaisesRegex(ValueError, "missing from the verified image handoff"):
                complete_inspected_images(self.paper, self.workspace, investigation)

    def test_complete_inspected_images_returns_full_verified_set(self):
        investigation = self.root / "source-investigation"
        investigation.mkdir()
        page = investigation / "page_001.png"
        page.write_bytes(b"verified-page")
        log = {
            "tool": "view_page",
            "arguments": {"page": 1},
            "result": {"label": "Original PDF page 1"},
        }
        (investigation / "tool-log.jsonl").write_text(json.dumps(log) + "\n")
        expected = [(page.resolve(), "Original PDF page 1")]
        with patch("peerreviewagents.engine.reviewer.inspected_images", return_value=expected) as collector:
            images = complete_inspected_images(self.paper, self.workspace, investigation)
        self.assertEqual(images, expected)
        collector.assert_called_once_with(self.paper, self.workspace.resolve(), investigation.resolve(), limit=10)

    def response_for(self, prepared):
        task_results = []
        for task in prepared["task_packets"]:
            task_results.append({
                "task_id": task["id"],
                "status": "completed",
                "evidence_receipt_ids": [row["receipt_id"] for row in task["operations"] if row["required"]],
                "observation": "The reported comparison is explicit.",
                "author_defense": "The authors report a matched control.",
                "limitation": "The receipt does not establish interpretation.",
                "evidence_quotes": [{"block_id": task["source_blocks"][0]["id"], "quote": task["source_blocks"][0]["text"]}],
            })
        return {
            "summary": "Evidence review",
            "findings": [{"title": "One"}, {"title": "Two"}],
            "limitations": [],
            "investigation_summary": "Checked tasks.",
            "discarded_hypotheses": [],
            "selected_finding_numbers": [1, 2],
            "task_results": task_results,
        }

    def test_validator_accepts_complete_source_bound_results(self):
        prepared = self.prepare()
        validated = validate_task_results(self.paper, self.response_for(prepared), prepared)
        self.assertEqual([row["status"] for row in validated["task_results"]], ["completed", "completed"])
        self.assertTrue(all(row["validation"] == "valid" for row in validated["task_results"]))
        self.assertEqual(validated["task_result_validation"], [])

    def test_completed_optional_task_requires_one_valid_receipt(self):
        task = dict(self.plan["tasks"][0])
        operation = dict(task["operations"][0])
        operation["required"] = False
        task["operations"] = [operation]
        self.plan = {**self.plan, "tasks": [task]}
        prepared = self.prepare()
        response = self.response_for(prepared)
        self.assertEqual(response["task_results"][0]["evidence_receipt_ids"], [])
        validated = validate_task_results(self.paper, response, prepared)
        result = validated["task_results"][0]
        self.assertEqual(result["status"], "unresolved")
        self.assertIn("Completed task cites no valid evidence receipt", result["validation_problems"])

    def test_validator_downgrades_cross_task_receipts_bad_quotes_and_duplicates(self):
        prepared = self.prepare()
        response = self.response_for(prepared)
        cross_task = response["task_results"][1]["evidence_receipt_ids"][0]
        response["task_results"][0]["evidence_receipt_ids"] = [cross_task]
        response["task_results"][0]["evidence_quotes"] = [{"block_id": "b1", "quote": "not present"}]
        response["task_results"].append(dict(response["task_results"][1]))
        response["selected_finding_numbers"] = [1, 1, 3, True]
        validated = validate_task_results(self.paper, response, prepared)
        self.assertEqual(len(validated["task_results"]), 2)
        self.assertTrue(all(row["status"] == "unresolved" for row in validated["task_results"]))
        self.assertEqual(validated["selected_finding_numbers"], [1])
        self.assertTrue(validated["task_result_validation"])


if __name__ == "__main__":
    unittest.main()
