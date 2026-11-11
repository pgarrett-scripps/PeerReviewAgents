"""What ``peerreview review`` does when the operator names nothing.

The engine grew out of a bounded study, and its command line still carried the
study's limits as the default: medium effort, at most five findings in about
900 words. These tests pin the default a person gets instead, and keep the
study budget reachable by name.
"""
import contextlib
import io
import unittest
from unittest.mock import patch

from peerreviewagents.engine import reviewer
from peerreviewagents.engine.__main__ import build_parser, dispatch
from peerreviewagents.engine.review_policy import (COMMAND_LINE_POLICY_NAME, DEFAULT_POLICY_NAME,
                                                   get_review_policy)


def dispatched(argv):
    """Parse one review command and return the call reviewer.review received."""
    parser = build_parser()
    args = parser.parse_args(argv)
    with patch.object(reviewer, 'review', return_value={'status': 'complete'}) as call, \
         contextlib.redirect_stdout(io.StringIO()):
        dispatch(parser, args)
    return call.call_args


class TheDefaultReview(unittest.TestCase):
    def test_a_bare_review_command_runs_the_agentic_ensemble(self):
        passed = dispatched(['review', 'paper', 'run', '--literature-cutoff', '2026-09-25'])
        self.assertEqual(passed.args[3], 'ensemble')
        self.assertEqual(passed.kwargs['policy'], COMMAND_LINE_POLICY_NAME)
        self.assertEqual(passed.kwargs['reviewers'], 2)
        policy = get_review_policy(COMMAND_LINE_POLICY_NAME)
        self.assertEqual(policy.effort, 'high')
        self.assertIsNone(policy.finding_cap)
        self.assertIsNone(policy.candidate_finding_cap)
        self.assertEqual(policy.tool_calls, 30)
        self.assertEqual(policy.max_attempts, 2)

    def test_the_default_policy_is_allowed_on_the_ensemble_it_is_the_default_for(self):
        """``workspace-agentic-v1`` names one session and is refused on an
        ensemble. The default must not be, or no bare review would run."""
        self.assertFalse(get_review_policy(COMMAND_LINE_POLICY_NAME).single_session)
        self.assertTrue(get_review_policy('workspace-agentic-v1').single_session)
        self.assertEqual(get_review_policy(COMMAND_LINE_POLICY_NAME).session, 'agentic-passes')

    def test_the_study_budget_is_still_reachable_by_name(self):
        passed = dispatched(['review', 'paper', 'run', '--literature-cutoff', '2026-09-25',
                             '--policy', DEFAULT_POLICY_NAME])
        self.assertEqual(passed.kwargs['policy'], DEFAULT_POLICY_NAME)
        study = get_review_policy(DEFAULT_POLICY_NAME)
        self.assertEqual((study.effort, study.finding_cap, study.word_budget),
                         ('medium', 5, 900))

    def test_the_library_default_stays_the_study_budget(self):
        """A caller inside the engine that names no policy keeps the bounded
        limits, so existing run directories stay resumable and byte identical."""
        self.assertIs(get_review_policy(None), get_review_policy(DEFAULT_POLICY_NAME))
        self.assertNotEqual(COMMAND_LINE_POLICY_NAME, DEFAULT_POLICY_NAME)

    def test_a_preset_still_pins_its_own_policy_over_the_default(self):
        passed = dispatched(['review', 'paper', 'run', '--literature-cutoff', '2026-09-25',
                             '--preset', 'workspace-ensemble-v1'])
        self.assertEqual(passed.kwargs['policy'], 'standard-v1')


if __name__ == '__main__':
    unittest.main()
