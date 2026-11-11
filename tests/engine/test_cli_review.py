"""What ``peerreview review`` does when the operator names nothing.

The engine grew out of a bounded study, and its command line still carried the
study's limits as the default: medium effort, at most five findings in about
900 words. These tests pin the default a person gets instead, keep the study
budget reachable by name, and keep every ablation arm in ``docs/study-arms.md``
reachable by the command that document prints.
"""
import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from peerreviewagents.engine import ingest, reviewer, run_report
from peerreviewagents.engine.__main__ import build_parser, dispatch
from peerreviewagents.engine.models import DEFAULT_MODEL, DEFAULT_MODELS
from peerreviewagents.engine.review_policy import (COMMAND_LINE_POLICY_NAME, DEFAULT_POLICY_NAME,
                                                   get_review_policy)
from peerreviewagents.engine.storage import write

CUTOFF = ['--literature-cutoff', '2026-09-25']


def dispatched(argv):
    """Parse one review command and return the call reviewer.review received."""
    parser = build_parser()
    args = parser.parse_args(argv)
    with patch.object(reviewer, 'review', return_value={'status': 'complete'}) as call, \
         contextlib.redirect_stdout(io.StringIO()):
        dispatch(parser, args)
    return call.call_args


def review_help():
    """The help text a person actually reads for ``peerreview review``."""
    parser = build_parser()
    out = io.StringIO()
    with contextlib.redirect_stdout(out), self_exiting():
        parser.parse_args(['review', '--help'])
    return out.getvalue()


@contextlib.contextmanager
def self_exiting():
    try:
        yield
    except SystemExit:
        pass


class TheDefaultReview(unittest.TestCase):
    def test_a_bare_review_command_runs_the_agentic_ensemble(self):
        passed = dispatched(['review', 'paper', 'run', *CUTOFF])
        self.assertEqual(passed.args[3], 'ensemble')
        self.assertEqual(passed.kwargs['policy'], COMMAND_LINE_POLICY_NAME)
        self.assertEqual(passed.kwargs['reviewers'], 2)
        self.assertEqual(passed.kwargs['synthesis_policy'], 'debate')
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
        passed = dispatched(['review', 'paper', 'run', *CUTOFF,
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
        passed = dispatched(['review', 'paper', 'run', *CUTOFF,
                             '--preset', 'workspace-ensemble-v1'])
        self.assertEqual(passed.kwargs['policy'], 'standard-v1')

    def test_claude_code_is_the_transport_and_codex_is_asked_for_by_name(self):
        """Nothing selects Codex implicitly: it takes --provider codex."""
        default = dispatched(['review', 'paper', 'run', *CUTOFF])
        self.assertEqual(default.kwargs['model'], DEFAULT_MODEL)
        self.assertEqual(DEFAULT_MODEL, DEFAULT_MODELS['claude-code'])
        codex = dispatched(['review', 'paper', 'run', *CUTOFF, '--provider', 'codex'])
        self.assertEqual(codex.kwargs['model'], DEFAULT_MODELS['codex'])


class TheStudyArms(unittest.TestCase):
    """One parse-and-dispatch check per arm named in docs/study-arms.md."""

    def test_the_debate_off_arm_actually_runs_an_ordinary_synthesis(self):
        passed = dispatched(['review', 'paper', 'run', *CUTOFF,
                             '--preset', 'workspace-ensemble-ordinary-v1'])
        self.assertEqual(passed.args[3], 'ensemble')
        self.assertEqual(passed.kwargs['synthesis_policy'], 'ordinary')
        self.assertEqual(passed.kwargs['reviewers'], 2)

    def test_the_single_reviewer_arm_runs_one_reviewer_and_no_synthesis(self):
        passed = dispatched(['review', 'paper', 'run', *CUTOFF,
                             '--preset', 'workspace-independent-v1'])
        self.assertEqual(passed.args[3], 'independent')
        self.assertEqual(passed.kwargs['reviewers'], 1)
        one_of_two = dispatched(['review', 'paper', 'run', *CUTOFF, '--reviewers', '1'])
        self.assertEqual((one_of_two.args[3], one_of_two.kwargs['reviewers']), ('ensemble', 1))

    def test_the_one_long_session_arm_carries_its_own_mode(self):
        passed = dispatched(['review', 'paper', 'run', *CUTOFF,
                             '--preset', 'workspace-agentic-v1'])
        self.assertEqual(passed.args[3], 'independent')
        self.assertEqual(passed.kwargs['policy'], 'workspace-agentic-v1')
        self.assertEqual(passed.kwargs['token_budget'], 1500000)

    def test_the_study_budget_arm_names_only_the_policy(self):
        passed = dispatched(['review', 'paper', 'run', *CUTOFF, '--policy', DEFAULT_POLICY_NAME])
        self.assertEqual((passed.args[3], passed.kwargs['reviewers']), ('ensemble', 2))
        self.assertEqual(passed.kwargs['policy'], DEFAULT_POLICY_NAME)

    def test_the_tools_off_baseline_is_refused_on_the_command_line(self):
        """The manuscript-only arm runs through the API, and says so."""
        parser = build_parser()
        args = parser.parse_args(['review', 'paper', 'run', *CUTOFF,
                                  '--preset', 'manuscript-only-single-v1'])
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()) as noise:
            dispatch(parser, args)
        self.assertIn('review_request_from_preset', noise.getvalue())

    def test_the_unlisted_modes_still_run_and_stay_out_of_the_help(self):
        for mode in ('plain', 'workspace', 'self-revision'):
            with self.subTest(mode=mode):
                passed = dispatched(['review', 'paper', 'run', *CUTOFF, '--mode', mode])
                self.assertEqual(passed.args[3], mode)
        help_text = review_help()
        self.assertIn('{ensemble,independent,panel}', help_text)
        self.assertIn('docs/study-arms.md', help_text)
        self.assertNotIn('self-revision', help_text)
        self.assertNotIn(',plain', help_text)


class TheOneCommandReview(unittest.TestCase):
    """``peerreview review manuscript.pdf run-output`` is the whole job."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.pdf = self.root / 'manuscript.pdf'
        self.pdf.write_bytes(b'%PDF-1.7\n')
        self.bundle = self.root / 'run-output'

    def run_command(self, *extra):
        parser = build_parser()
        args = parser.parse_args(['review', str(self.pdf), str(self.bundle), *CUTOFF, *extra])
        reports = {'markdown': self.bundle / 'REVIEW.md', 'text': self.bundle / 'REVIEW.txt'}
        with patch.object(ingest, 'import_paper',
                          return_value={'title': 'Study', 'statistics': {'total_words': 4}}
                          ) as imported, \
             patch.object(reviewer, 'review', return_value={'status': 'complete'}) as review, \
             patch.object(run_report, 'write_reports', return_value=reports) as report, \
             contextlib.redirect_stdout(io.StringIO()):
            dispatch(parser, args)
        return imported, review, report

    def test_a_pdf_is_imported_reviewed_and_reported_in_one_command(self):
        imported, review, report = self.run_command()
        self.assertEqual(imported.call_args.args[0], self.pdf)
        self.assertEqual(Path(imported.call_args.args[1]), self.bundle / 'manuscript')
        self.assertEqual(Path(review.call_args.args[0]), self.bundle / 'manuscript')
        self.assertEqual(Path(review.call_args.args[1]), self.bundle / 'review')
        self.assertEqual(Path(report.call_args.args[0]), self.bundle / 'review')
        self.assertEqual(Path(report.call_args.args[1]), self.bundle / 'manuscript')
        self.assertEqual(Path(report.call_args.args[2]), self.bundle)

    def test_re_running_the_command_reuses_the_imported_manuscript(self):
        """The manuscript is immutable, so a resumed run must not re-import it."""
        write(self.bundle / 'manuscript/paper.json', {'id': 'paper'})
        imported, review, _ = self.run_command()
        imported.assert_not_called()
        self.assertEqual(Path(review.call_args.args[0]), self.bundle / 'manuscript')

    def test_a_workspace_directory_keeps_the_previous_behaviour(self):
        """A directory is reviewed in place: no import, no bundle, no report."""
        workspace = self.root / 'workspace'
        write(workspace / 'paper.json', {'id': 'paper'})
        parser = build_parser()
        args = parser.parse_args(['review', str(workspace), str(self.root / 'run'), *CUTOFF])
        with patch.object(ingest, 'import_paper') as imported, \
             patch.object(reviewer, 'review', return_value={'status': 'complete'}) as review, \
             patch.object(run_report, 'write_reports') as report, \
             contextlib.redirect_stdout(io.StringIO()):
            dispatch(parser, args)
        imported.assert_not_called()
        report.assert_not_called()
        self.assertEqual(Path(review.call_args.args[0]), workspace)
        self.assertEqual(Path(review.call_args.args[1]), self.root / 'run')

    def test_a_path_that_is_neither_a_workspace_nor_a_pdf_is_refused(self):
        parser = build_parser()
        args = parser.parse_args(['review', str(self.root / 'absent.pdf'),
                                  str(self.bundle), *CUTOFF])
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()) as noise:
            dispatch(parser, args)
        self.assertIn('neither a manuscript workspace nor a PDF file', noise.getvalue())


if __name__ == '__main__':
    unittest.main()
