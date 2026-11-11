"""What the one-command review leaves behind when it stops before its first call."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from peerreviewagents.engine import reviewer
from peerreviewagents.engine.bundle import ReviewPaths, run_bundle_review
from peerreviewagents.engine.model_runtime import RunBlocked
from peerreviewagents.engine.storage import read, write

BLOCKED = 'This process cannot access a claude-code subscription login.'


class BlockedRunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bundle = Path(self.tmp.name) / 'paper-review'
        self.workspace = self.bundle / 'manuscript'
        self.run = self.bundle / 'review'
        write(self.workspace / 'paper.json', {'id': 'paper-1', 'title': 'A manuscript'})
        self.paths = ReviewPaths(self.workspace, self.run, self.bundle)

    def blocked(self, *args, **kwargs):
        """What the engine does: the protocol is saved, then the guard refuses."""
        write(self.run / 'protocol.json', {'provider': 'claude-code', 'model': 'claude-sonnet-5-5',
                                           'effort': 'medium', 'mode': 'ensemble', 'reviewers': 2})
        write(self.run / 'manifest.json', {'paper_id': 'paper-1'})
        raise RunBlocked(BLOCKED)

    def test_a_run_blocked_before_its_first_call_records_the_cause(self):
        """The guard runs after protocol.json is written, so the run was
        reported from a progress record that did not exist: the report said
        'Run status: unknown' instead of naming the missing login."""
        with patch.object(reviewer, 'review', self.blocked):
            result = run_bundle_review(self.paths, None)

        self.assertEqual(result['progress']['status'], 'blocked')
        self.assertIn(BLOCKED, result['progress']['error'])
        self.assertIn(BLOCKED, result['failure'])
        saved = read(self.run / 'progress.json')
        self.assertEqual(saved['status'], 'blocked')
        self.assertIn(BLOCKED, saved['error'])
        report = Path(result['reports']['markdown']).read_text()
        self.assertIn('blocked', report)
        self.assertNotIn('unknown', report)

    def test_a_run_that_recorded_its_own_state_keeps_it(self):
        """A run that got far enough to record a status owns that status: the
        bundle must not overwrite it with 'blocked'."""
        def stopped(*args, **kwargs):
            write(self.run / 'protocol.json', {'provider': 'claude-code', 'mode': 'ensemble',
                                               'model': 'claude-sonnet-5-5', 'effort': 'medium',
                                               'reviewers': 2})
            write(self.run / 'manifest.json', {'paper_id': 'paper-1'})
            write(self.run / 'progress.json', {'status': 'incomplete', 'stage': 'reviewer-1'})
            raise RuntimeError('transport lost')

        with patch.object(reviewer, 'review', stopped):
            result = run_bundle_review(self.paths, None)

        self.assertEqual(result['progress']['status'], 'incomplete')
        self.assertEqual(read(self.run / 'progress.json')['stage'], 'reviewer-1')
        self.assertIn('transport lost', result['failure'])


if __name__ == '__main__':
    unittest.main()
