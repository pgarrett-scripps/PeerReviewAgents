"""Checks for scientific source integrity and bounded investigation."""
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from manuscripts import build_base
from peerreviewagents.engine.annotations import export_draft, normalize_review, verify_workspace
from peerreviewagents.engine.calculator import calculate
from peerreviewagents.engine.investigation import Investigation


class SourceIntegrity(unittest.TestCase):
    """The manuscript is a file on disk, and a finding is a position in it.

    Every test here builds a real workspace — ``paper.md`` and the map over it —
    because that is what the engine anchors against now. A finding carries the
    line it cites rather than a block id, and an exported draft carries the
    document as it stands beside the document the accepted edits would leave.
    """

    TEXT = 'A 🧠 finding has 12 samples. A second statement.'

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.base = self.manuscript(self.TEXT)
        self.finding = {'title': 'Clarify unit', 'quote': '12 samples', 'kind': 'edit',
                        'replacement': '12 donors', 'evidence_lines': [1],
                        'comment': 'Clarify unit'}

    def manuscript(self, text, name='manuscript'):
        """A one-paragraph workspace, loaded, under its own folder."""
        return build_base(self.root / name, [{'kind': 'paragraph', 'section': 'Results',
                                              'page': 1, 'text': text}])

    def review(self, findings=None, base=None):
        return normalize_review(base or self.base, {'findings': findings or [self.finding]},
                                'review')

    def decisions(self, review, base=None):
        return {'paper_id': (base or self.base).paper_id, 'review_id': 'review',
                'items': {a['id']: 'accepted' for a in review['findings']}}

    def test_unicode_and_immutable_source(self):
        r = self.review()
        out = export_draft(self.base, r, self.decisions(r))
        self.assertIn('12 donors', out['proposed_documents']['paper.md'])
        # Nothing was written: the manuscript on disk and the base in hand both
        # still read as they did, and the package carries the before as well.
        self.assertEqual(out['original_documents'], dict(self.base.documents))
        self.assertIn('12 samples', (self.root / 'manuscript' / 'paper.md').read_text('utf-8'))
        self.assertEqual(r['findings'][0]['issue_status'], 'open')

    def test_stale_source_rejected(self):
        r = self.review()
        changed = self.manuscript('A 🧠 finding has 14 samples. A second statement.', 'redraft')
        with self.assertRaises(ValueError):
            export_draft(changed, r, self.decisions(r, changed))

    def test_overlapping_edits_rejected(self):
        r = self.review([self.finding, {**self.finding, 'quote': 'samples',
                                        'replacement': 'participants'}])
        with self.assertRaises(ValueError):
            export_draft(self.base, r, self.decisions(r))

    def test_novel_numbers_withheld(self):
        r = self.review([{**self.finding, 'replacement': '24 samples'}])
        self.assertEqual(r['findings'][0]['kind'], 'question')
        with self.assertRaises(ValueError):
            export_draft(self.base, r, self.decisions(r))

    def test_gene_identifier_is_not_a_new_measurement(self):
        r = self.review([{**self.finding, 'replacement': '12 samples with ELK1 expression'}])
        self.assertEqual(r['findings'][0]['kind'], 'edit')

    def test_ambiguous_anchor_is_retained(self):
        repeated = self.manuscript('aaa', 'repeated')
        r = self.review([{**self.finding, 'quote': 'aa'}], repeated)
        self.assertEqual(r['findings'][0]['placement'], 'unplaced')
        self.assertEqual(len(r['findings']), 1)

    def test_missing_anchor_does_not_discard_valid_findings(self):
        r = self.review([self.finding, {**self.finding, 'quote': 'absent'}])
        self.assertEqual(r['annotation_counts'], {'total': 2, 'anchored': 1})

    def test_manifest_tampering_rejected(self):
        verify_workspace(self.root / 'manuscript')
        # The lines are the citation, so the file holding them is the thing that
        # may not change under a review: an edited `paper.md` is refused by name.
        (self.root / 'manuscript' / 'paper.md').write_text('Something else.\n', encoding='utf-8')
        with self.assertRaises(ValueError):
            verify_workspace(self.root / 'manuscript')

    def test_calculator_blocks_code_and_unbounded_values(self):
        self.assertAlmostEqual(calculate('mean([2,4,6])'), 4)
        for expression in ['__import__("os")', '2**100000', '[x for x in [1]]', 'open("file")']:
            with self.assertRaises(ValueError):
                calculate(expression)

    def server(self, folder='manuscript', limit=1):
        out = self.root / folder / 'out'
        return Investigation({'paper': str(self.root / folder), 'output': str(out),
                              'cache': str(self.root / folder / 'cache'), 'tool_limit': limit})

    def test_tool_limit_and_notes(self):
        server = self.server()
        self.assertTrue(server.call('save_note', {'text': 'A hypothesis',
                                                  'evidence_lines': [1]})['saved'])
        self.assertIn('notice', server.call('search', {'query': 'samples'}))
        notes = self.root / 'manuscript' / 'out' / 'notes.jsonl'
        self.assertEqual(len(notes.read_text(encoding='utf-8').splitlines()), 1)

    def test_reading_the_paper_is_free_and_searching_is_not(self):
        """``read_paper`` is outside the allowance; it is also what every agent reads."""
        server = self.server()
        for _ in range(3):
            served = server.call('read_paper', {})
            self.assertIn('    1| A 🧠 finding has 12 samples.', served['text'])
            self.assertEqual(served['parts'], 1)
            self.assertIs(served['tool_receipt']['spent'], False)
        self.assertEqual(server.calls, 0)
        found = server.call('search', {'query': '12 samples'})
        self.assertEqual([m['line'] for m in found['matches']], [1])
        self.assertIs(found['tool_receipt']['spent'], True)
        self.assertEqual(server.calls, 1)
        log = self.root / 'manuscript' / 'out' / 'tool-log.jsonl'
        logged = [json.loads(line) for line in log.read_text(encoding='utf-8').splitlines()]
        self.assertEqual([row['tool'] for row in logged], ['read_paper'] * 3 + ['search'])
        self.assertTrue(all('text' not in row['result'] for row in logged),
                        'the log records which part was served, not the manuscript')


if __name__ == '__main__':
    unittest.main()
