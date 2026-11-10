"""The editor handoff remains auditable outside the viewer."""
import unittest

from peerreviewagents.engine.annotations import export_draft, normalize_review
from peerreviewagents.engine.reviewer import editor_package
from peerreviewagents.engine.storage import digest


class ExportHandoff(unittest.TestCase):
    def test_rejected_and_pending_findings_survive_export(self):
        text = 'The outcome was measured at death.'
        paper = {'id': 'paper', 'source_sha256': 'source', 'blocks': [
            {'id': 'b1', 'text': text, 'sha256': digest(text)}]}
        concern = {'title': 'Timing', 'block_id': 'b1', 'quote': text,
                   'kind': 'question', 'comment': 'Clarify temporal inference.',
                   'replacement': '', 'evidence_blocks': ['b1']}
        disposition = {'source_finding_id': 'r1-f1', 'disposition': 'rejected',
                       'final_finding_numbers': [], 'reason': 'The conclusion is explicitly associative.',
                       'evidence_blocks': ['b1']}
        final = {'summary': 'Debated review', 'findings': [], 'source_dispositions': [disposition]}
        package = editor_package(final, [{'findings': [concern, {**concern, 'title': 'Sampling'}]}])
        normalized = normalize_review(paper, package, 'review')
        exported = export_draft(paper, normalized, {
            'paper_id': 'paper', 'review_id': 'review', 'items': {}})
        self.assertEqual(len(exported['source_findings']), 2)
        self.assertEqual(exported['source_dispositions'], [disposition])
        self.assertEqual(exported['handoff']['verification_queue_count'], 1)
        self.assertFalse(exported['findings'][0]['endorsed'])
        self.assertEqual(exported['original_blocks'], exported['proposed_blocks'])


if __name__ == '__main__':
    unittest.main()
