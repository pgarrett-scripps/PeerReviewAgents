import copy
import tempfile
import unittest
from pathlib import Path

from peerreviewagents.engine.scaffold_checks import build_report, compact_summary
from peerreviewagents.engine.storage import digest, write


SCAFFOLD = Path("/home/ty/Repos/paper-scaffold")


class ScaffoldChecks(unittest.TestCase):
    def workspace(self, root, *, structured=True):
        text = (
            "The result may possibly support the model. "
            "This is is a deliberately long sentence with enough ordinary words to exceed "
            "the scaffold threshold because it continues through many clauses and remains "
            "part of the extracted scientific prose for editorial review."
        )
        paper = {
            "id": "paper-1",
            "source_sha256": "source",
            "xml_sha256": "xml" if structured else None,
            "blocks": [
                {"id": "b1", "kind": "paragraph", "text": text,
                 "sha256": digest(text), "section": "Results", "document": "main",
                 "citations": ["r1", "missing"] if structured else []},
                {"id": "b2", "kind": "reference", "text": "Reference one",
                 "sha256": digest("Reference one"), "section": "References",
                 "document": "main", "citations": []},
            ],
            "references": [
                {"id": "r1", "raw": "Reference one"},
                {"id": "r2", "raw": "Reference two"},
            ],
        }
        write(root / "paper.json", paper)
        write(root / "manifest.json", {
            "paper_id": paper["id"],
            "files": {"paper.json": digest((root / "paper.json").read_bytes())},
        })
        return paper

    def test_report_reuses_scaffold_and_preserves_source(self):
        if not SCAFFOLD.is_dir():
            self.skipTest("Paper Scaffold checkout unavailable")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paper = self.workspace(root)
            before = copy.deepcopy(paper)
            report = build_report(root, SCAFFOLD)

            self.assertTrue(report["source"]["verified"])
            self.assertEqual(report["source"]["checked_block_ids"], ["b1"])
            self.assertEqual(
                {row["name"] for row in report["provenance"]["reused_functions"]},
                {"readability.metrics", "prose_check.check"},
            )
            self.assertGreater(report["word_counts"]["overall"], 20)
            self.assertIn("flesch_kincaid_grade", report["readability"]["overall"])
            rules = {row["rule"] for row in report["prose_findings"]}
            self.assertIn("doubled-word", rules)
            self.assertIn("double-hedge", rules)
            self.assertTrue(all(row["disposition"] == "advisory"
                                for row in report["prose_findings"]))
            self.assertEqual(before, __import__("json").loads((root / "paper.json").read_text()))

    def test_structured_citation_linkage_is_conservative(self):
        if not SCAFFOLD.is_dir():
            self.skipTest("Paper Scaffold checkout unavailable")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.workspace(root)
            citation = build_report(root, SCAFFOLD)["citations"]
            self.assertEqual(citation["scaffold_check"], "unavailable")
            self.assertEqual(citation["workspace_linkage"], "checked")
            self.assertEqual(citation["unknown_citation_ids"], ["missing"])
            self.assertEqual(citation["uncited_reference_ids"], ["r2"])

    def test_pdf_only_import_does_not_invent_citation_coverage(self):
        if not SCAFFOLD.is_dir():
            self.skipTest("Paper Scaffold checkout unavailable")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.workspace(root, structured=False)
            citation = build_report(root, SCAFFOLD)["citations"]
            self.assertEqual(citation["workspace_linkage"], "unavailable")
            self.assertIsNone(citation["cited_reference_count"])
            self.assertEqual(citation["uncited_reference_ids"], [])

    def test_tampered_source_is_rejected(self):
        if not SCAFFOLD.is_dir():
            self.skipTest("Paper Scaffold checkout unavailable")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paper = self.workspace(root)
            paper["blocks"][0]["text"] = "Changed"
            write(root / "paper.json", paper)
            with self.assertRaisesRegex(ValueError, "Manuscript source changed"):
                build_report(root, SCAFFOLD)

    def test_compact_summary_keeps_review_boundaries(self):
        report = {
            "paper_id": "p",
            "source": {"verified": True, "checked_block_ids": ["b1"]},
            "word_counts": {"overall": 10},
            "readability": {"overall": {"words": 10}},
            "prose_findings": [{"rule": "long-sentence"}, {"rule": "long-sentence"}],
            "citations": {"workspace_linkage": "unavailable",
                          "unknown_citation_ids": [], "uncited_reference_ids": []},
            "provenance": {"scaffold_version": "1", "scaffold_revision": "abc"},
            "scope": {"note": "Advisory only."},
        }
        summary = compact_summary(report)
        self.assertEqual(summary["prose_advisories_by_rule"], {"long-sentence": 2})
        self.assertEqual(len(summary["prose_advisory_items"]), 2)
        self.assertEqual(summary["prose_advisories_truncated"], 0)
        self.assertEqual(summary["advisory_note"], "Advisory only.")


if __name__ == "__main__":
    unittest.main()
