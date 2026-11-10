"""Offline checks for the Markdown and plain-text run reports."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from peerreviewagents.engine.run_report import NOTICE, WIDTH, write_reports
from peerreviewagents.engine.storage import write

EDIT = {'title': 'Soften the prerequisite claim', 'severity': 'major', 'kind': 'edit', 'block_id': 'b1',
        'placement': 'anchored', 'quote': 'These results confirm the model.',
        'comment': 'The curves are consistent with the model but do not confirm it. ' * 3,
        'replacement': 'These results are consistent with the model.', 'counterevidence': 'The mutant stays inactive.'}
QUESTION = {'title': 'Report biological replicates', 'severity': 'moderate', 'kind': 'question', 'block_id': 'b2',
            'placement': 'anchored', 'quote': 'n = 3', 'comment': 'How many biological replicates were used?',
            'replacement': ''}


class RunReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.workspace = self.root / 'workspace'
        write(self.workspace / 'paper.json', {'id': 'paper-1', 'title': 'Dimerization regulates enzyme activity',
                                              'source_url': 'https://example.org/preprint'})
        self.run = self.root / 'run'

    def call(self, folder):
        write(folder / 'execution' / 'calls' / f'{len(list(self.run.rglob("*.started.json")))}.started.json', {})
        with (folder / 'execution' / 'usage.jsonl').open('a') as stream:
            stream.write(json.dumps({'observed_tokens': 100, 'usage_unknown': False}) + '\n')

    def ensemble(self, final=True):
        write(self.run / 'protocol.json', {'provider': 'claude-code', 'model': 'claude-sonnet-5-5', 'effort': 'medium',
                                           'mode': 'ensemble', 'reviewers': 2})
        write(self.run / 'manifest.json', {'paper_id': 'paper-1'})
        write(self.run / 'progress.json', {'status': 'complete'})
        for index in (1, 2):
            reviewer = self.run / f'reviewer-{index}'
            write(reviewer / 'protocol.json', {'literature_cutoff': '2024-04-07'})
            write(reviewer / 'execution' / 'workspace' / 'tool-config.json', {'paper': str(self.workspace)})
            write(reviewer / 'editor-package.json', {'summary': f'Reviewer {index} summary.', 'findings': [QUESTION],
                                                     'investigation_summary': 'Opened one figure page.',
                                                     'limitations': ['No raw data.']})
            self.call(reviewer)
        write(self.run / 'debate' / 'synthesis-validation.json',
              {'valid': True, 'component_ledger': {'accounted_components': 4, 'expected_components': 4}})
        self.call(self.run / 'debate')
        if final:
            write(self.run / 'editor-package.json', {'summary': 'Two findings remain.', 'findings': [EDIT, QUESTION],
                                                     'limitations': ['Supplementary data were unavailable.'],
                                                     'decisions': [{'issue_id': 'r1-issue-1', 'decision_reason': 'kept'}]})

    def reports(self, **options):
        paths = write_reports(self.run, **options)
        return paths['markdown'].read_text(), paths['text'].read_text()

    def test_ensemble_report_has_the_final_review_then_each_reviewer_in_both_formats(self):
        self.ensemble()
        markdown, text = self.reports()
        for expected in ('# Review: Dimerization regulates enzyme activity', '## Final review (synthesis)',
                         '> These results confirm the model.', '> These results are consistent with the model.',
                         '**Strongest counterpoint:** The mutant stays inactive.', '| Literature cutoff | 2024-04-07 |',
                         '| Model | claude-sonnet-5-5 (claude-code, medium effort) |', '| Observed tokens | 300 |',
                         '| Component ledger | 4 of 4 components accounted, synthesis valid |',
                         '**Investigation:** Opened one figure page.', NOTICE):
            self.assertIn(expected, markdown)
        positions = [markdown.index(f'## {name}') for name in
                     ('Final review (synthesis)', 'Reviewer 1 (independent)', 'Reviewer 2 (independent)')]
        self.assertEqual(positions, sorted(positions))
        self.assertNotIn('How the synthesis', markdown)
        self.assertNotIn('r1-issue-1', markdown + text)
        for expected in ('Final review (synthesis)', 'Reviewer 2 (independent)', '    These results are consistent',
                         'Severity: major | Type: edit | Location: block b1', 'Literature cutoff: 2024-04-07'):
            self.assertIn(expected, text)
        for line in text.splitlines():
            self.assertFalse(line.startswith(('#', '>', '| ')) or '**' in line, line)
            if str(self.run) not in line:
                self.assertLessEqual(len(line), WIDTH, line)

    def test_single_review_modes_use_their_saved_result(self):
        write(self.run / 'protocol.json', {'provider': 'codex', 'model': 'gpt-5.6-terra', 'effort': 'medium',
                                           'mode': 'plain', 'paper_id': 'paper-1', 'literature_cutoff': '2024-01-01'})
        write(self.run / 'progress.json', {'status': 'complete'})
        write(self.run / 'responses' / 'plain-guarded.json', {'summary': 'One finding.', 'findings': [EDIT]})
        markdown, _ = self.reports(workspace=self.workspace)
        self.assertIn('## Review\n', markdown)
        self.assertIn('Soften the prerequisite claim', markdown)
        self.assertIn('| Workflow | plain review |', markdown)
        self.assertNotIn('Reviewer 1', markdown)

    def test_failed_run_reports_its_error_without_claiming_validation(self):
        self.ensemble(final=False)
        for index in (1, 2):
            (self.run / f'reviewer-{index}' / 'editor-package.json').unlink()
        write(self.run / 'progress.json', {'status': 'blocked', 'error': 'Claude Code failed. No automatic retry.'})
        markdown, text = self.reports()
        self.assertIn('## Final review (synthesis)\n\nNo final review was produced.', markdown)
        self.assertIn('Run error: Claude Code failed. No automatic retry.', text)
        self.assertNotIn(NOTICE, markdown)

    def test_mismatched_workspace_or_missing_protocol_fails_before_writing(self):
        self.ensemble()
        other = self.root / 'other'
        write(other / 'paper.json', {'id': 'paper-2', 'title': 'Another paper'})
        with self.assertRaisesRegex(ValueError, 'does not match'):
            write_reports(self.run, workspace=other)
        with self.assertRaisesRegex(ValueError, 'protocol.json'):
            write_reports(self.root / 'workspace')
        self.assertFalse((self.run / 'REVIEW.md').exists())

    def test_command_writes_both_reports_to_the_chosen_folder(self):
        from peerreviewagents.engine.__main__ import main
        self.ensemble()
        target = self.root / 'reports'
        with patch.object(sys, 'argv', ['paper-workbench', 'report', str(self.run), '--output-dir', str(target)]), \
                patch('builtins.print'):
            main()
        self.assertTrue((target / 'REVIEW.md').is_file() and (target / 'REVIEW.txt').is_file())
        self.assertFalse((self.run / 'REVIEW.md').exists())


if __name__ == '__main__':
    unittest.main()
