"""What ``peerreview review`` does when the operator names nothing.

The engine grew out of a bounded study, and its command line still carried the
study's limits as the default: medium effort, at most five findings in about
900 words. These tests pin the default a person gets instead, keep the study
budget reachable by name, and keep every ablation arm in ``docs/study-arms.md``
reachable by the command that document prints.

The one-command form owns the whole job, so it also has to behave when the job
goes wrong: a second run must not spend the subscription again over a finished
review, a run that stops must still leave a report and a readable cause, a
different PDF must not be reviewed against an earlier import, and a path that
is neither a PDF nor a workspace must be refused by name.
"""
import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from peerreviewagents.engine import ingest, reviewer, run_report
from peerreviewagents.engine.__main__ import build_parser, dispatch
from peerreviewagents.engine.api import ReviewRequest
from peerreviewagents.engine.config import resolved_review_config
from peerreviewagents.engine.models import DEFAULT_MODEL, DEFAULT_MODELS
from peerreviewagents.engine.review_policy import (DEFAULT_POLICY_NAME, STUDY_POLICY_NAME,
                                                   get_review_policy)
from peerreviewagents.engine.storage import write

CUTOFF = ['--literature-cutoff', '2026-09-25']


@contextlib.contextmanager
def imported_workspace():
    """One minimal imported manuscript workspace, because a path that is not
    one is now refused before any model call."""
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / 'paper'
        write(root / 'paper.json', {'id': 'paper'})
        yield root


def dispatched(argv):
    """Parse one review command and return the call reviewer.review received."""
    parser = build_parser()
    with imported_workspace() as root:
        args = parser.parse_args([str(root) if token == 'paper' else token for token in argv])
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
        self.assertEqual(passed.kwargs['policy'], DEFAULT_POLICY_NAME)
        self.assertEqual(passed.kwargs['reviewers'], 2)
        self.assertEqual(passed.kwargs['synthesis_policy'], 'debate')
        policy = get_review_policy(DEFAULT_POLICY_NAME)
        self.assertEqual(policy.effort, 'high')
        self.assertIsNone(policy.finding_cap)
        self.assertIsNone(policy.candidate_finding_cap)
        self.assertEqual(policy.tool_calls, 30)
        self.assertEqual(policy.max_attempts, 2)

    def test_one_default_policy_on_every_surface(self):
        """The command line used to run the agentic allowance while the request
        object, the engine functions and the library default ran the study
        budget. One review asked for in four ways is now one review."""
        self.assertEqual(DEFAULT_POLICY_NAME, 'agentic-v1')
        self.assertIs(get_review_policy(None), get_review_policy(DEFAULT_POLICY_NAME))
        self.assertEqual(dispatched(['review', 'paper', 'run', *CUTOFF]).kwargs['policy'],
                         DEFAULT_POLICY_NAME)
        request = ReviewRequest(workspace='workspace', output='run')
        self.assertEqual(request.policy_name(), DEFAULT_POLICY_NAME)
        self.assertEqual(resolved_review_config(request)['policy'], DEFAULT_POLICY_NAME)

    def test_the_default_policy_is_allowed_on_the_ensemble_it_is_the_default_for(self):
        """``workspace-agentic-v1`` names one session and is refused on an
        ensemble. The default must not be, or no bare review would run."""
        self.assertFalse(get_review_policy(DEFAULT_POLICY_NAME).single_session)
        self.assertTrue(get_review_policy('workspace-agentic-v1').single_session)
        self.assertEqual(get_review_policy(DEFAULT_POLICY_NAME).session, 'agentic-passes')

    def test_the_study_budget_is_still_reachable_by_name(self):
        passed = dispatched(['review', 'paper', 'run', *CUTOFF,
                             '--policy', STUDY_POLICY_NAME])
        self.assertEqual(passed.kwargs['policy'], STUDY_POLICY_NAME)
        study = get_review_policy(STUDY_POLICY_NAME)
        self.assertEqual((study.effort, study.finding_cap, study.word_budget),
                         ('medium', 5, 900))
        self.assertIsNot(get_review_policy(None), study)

    def test_the_study_policy_still_records_no_protocol_overlay(self):
        """It is no longer the default, but it is still the one policy a run
        made under the engine's original limits can be resumed under."""
        self.assertEqual(get_review_policy(STUDY_POLICY_NAME).protocol_overlay(), {})
        self.assertIn('review_policy', get_review_policy(DEFAULT_POLICY_NAME).protocol_overlay())

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


class TheTokenCeiling(unittest.TestCase):
    """One ceiling is checked across every stage of an ensemble, so it has to
    be the policy's allowance per stage rather than the study's whole-run
    number: a 750,000 ceiling refused the debate before it started."""

    def test_the_default_ceiling_covers_every_agentic_stage(self):
        policy = get_review_policy(DEFAULT_POLICY_NAME)
        self.assertEqual(policy.observed_token_stop, 1500000)
        self.assertEqual(policy.launch_budget(3), 4500000)
        self.assertEqual(policy.launch_budget(2), 3000000)
        self.assertEqual(policy.launch_budget(), 1500000)

    def test_the_study_budget_keeps_the_ceiling_it_shipped_with(self):
        study = get_review_policy(STUDY_POLICY_NAME)
        self.assertEqual(study.launch_budget(3), 750000)
        self.assertEqual(study.launch_budget(9), 750000)
        self.assertEqual(study.launch_budget(), 750000)

    def test_the_command_line_names_no_ceiling_and_a_named_one_is_used_as_given(self):
        self.assertIsNone(dispatched(['review', 'paper', 'run', *CUTOFF]).kwargs['token_budget'])
        passed = dispatched(['review', 'paper', 'run', *CUTOFF, '--token-budget', '1234'])
        self.assertEqual(passed.kwargs['token_budget'], 1234)

    def test_the_request_object_resolves_the_same_ceiling(self):
        self.assertEqual(ReviewRequest(workspace='w', output='r').token_budget, 4500000)
        self.assertEqual(ReviewRequest(workspace='w', output='r',
                                       policy=STUDY_POLICY_NAME).token_budget, 750000)
        self.assertEqual(ReviewRequest(workspace='w', output='r', workflow='independent',
                                       reviewers=1).token_budget, 1500000)
        self.assertEqual(ReviewRequest(workspace='w', output='r', workflow='manuscript-only',
                                       reviewers=1, policy=STUDY_POLICY_NAME).token_budget, 750000)
        self.assertEqual(ReviewRequest(workspace='w', output='r', token_budget=99).token_budget, 99)


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
        passed = dispatched(['review', 'paper', 'run', *CUTOFF, '--policy', STUDY_POLICY_NAME])
        self.assertEqual((passed.args[3], passed.kwargs['reviewers']), ('ensemble', 2))
        self.assertEqual(passed.kwargs['policy'], STUDY_POLICY_NAME)

    def test_the_tools_off_baseline_is_refused_on_the_command_line(self):
        """The manuscript-only arm runs through the API, and says so."""
        parser = build_parser()
        with imported_workspace() as root:
            args = parser.parse_args(['review', str(root), 'run', *CUTOFF,
                                      '--preset', 'manuscript-only-single-v1'])
            with self.assertRaises(SystemExit), \
                 contextlib.redirect_stderr(io.StringIO()) as noise:
                dispatch(parser, args)
        self.assertIn('review_request_from_preset', noise.getvalue())

    def test_the_documents_describe_the_arms_this_command_line_actually_has(self):
        """The docs claimed a medium-effort default, a ``core-artifact.json``
        every run writes, and that each arm differs by exactly one thing. Two
        were false and the third is only true per reference arm."""
        root = Path(__file__).resolve().parents[2]
        arms = (root / 'docs/study-arms.md').read_text()
        readme = (root / 'peerreviewagents/engine/README.md').read_text()
        self.assertNotIn('`claude-sonnet-5-5` at medium effort', readme)
        self.assertIn(DEFAULT_POLICY_NAME, readme)
        self.assertNotIn('differs by exactly one thing', arms)
        self.assertIn('never by `peerreview review`', arms)
        for reference in ('arms/reference-study', 'arms/reference-default'):
            self.assertIn(reference, arms)
        for arm in ('--policy ' + STUDY_POLICY_NAME, '--reviewers 1',
                    'workspace-ensemble-ordinary-v1', 'workspace-independent-v1',
                    'workspace-agentic-v1', 'manuscript-only-single-v1'):
            with self.subTest(arm=arm):
                self.assertIn(arm, arms)

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

    def finished_run(self, workspace, output, *args, **kwargs):
        """What a real review leaves behind: the protocol a report is built from."""
        write(Path(output) / 'protocol.json', {'mode': 'ensemble'})
        write(Path(output) / 'progress.json', {'status': 'complete'})
        return {'status': 'complete'}

    def run_command(self, *extra, paper=None, review=None):
        parser = build_parser()
        args = parser.parse_args(['review', str(paper or self.pdf), str(self.bundle),
                                  *CUTOFF, *extra])
        reports = {'markdown': self.bundle / 'REVIEW.md', 'text': self.bundle / 'REVIEW.txt'}
        with patch.object(ingest, 'import_paper',
                          return_value={'title': 'Study', 'statistics': {'total_words': 4}}
                          ) as imported, \
             patch.object(reviewer, 'review', **(review or {'side_effect': self.finished_run})
                          ) as reviewed, \
             patch.object(run_report, 'write_reports', return_value=reports) as report, \
             contextlib.redirect_stdout(io.StringIO()) as out:
            dispatch(parser, args)
        return imported, reviewed, report, out.getvalue()

    def refused(self, *extra, paper=None):
        """Run the command expecting a plain argument refusal, not a traceback."""
        parser = build_parser()
        args = parser.parse_args(['review', str(paper or self.pdf), str(self.bundle), *extra])
        with patch.object(reviewer, 'review') as reviewed, \
             self.assertRaises(SystemExit), \
             contextlib.redirect_stdout(io.StringIO()), \
             contextlib.redirect_stderr(io.StringIO()) as noise:
            dispatch(parser, args)
        reviewed.assert_not_called()
        return noise.getvalue()

    def imported_already(self, pdf=None):
        """The bundle as a finished import leaves it: paper plus original PDF."""
        write(self.bundle / 'manuscript/paper.json', {'id': 'paper'})
        (self.bundle / 'manuscript/original.pdf').write_bytes((pdf or self.pdf).read_bytes())

    def test_a_pdf_is_imported_reviewed_and_reported_in_one_command(self):
        imported, review, report, _ = self.run_command()
        self.assertEqual(imported.call_args.args[0], self.pdf)
        self.assertEqual(Path(imported.call_args.args[1]), self.bundle / 'manuscript')
        self.assertEqual(Path(review.call_args.args[0]), self.bundle / 'manuscript')
        self.assertEqual(Path(review.call_args.args[1]), self.bundle / 'review')
        self.assertEqual(Path(report.call_args.args[0]), self.bundle / 'review')
        self.assertEqual(Path(report.call_args.args[1]), self.bundle / 'manuscript')
        self.assertEqual(Path(report.call_args.args[2]), self.bundle)

    def test_a_pdf_without_the_suffix_is_still_imported(self):
        named = self.root / 'manuscript.bin'
        named.write_bytes(b'%PDF-1.7\n')
        imported, review, _, _ = self.run_command(paper=named)
        self.assertEqual(imported.call_args.args[0], named)
        self.assertEqual(Path(review.call_args.args[1]), self.bundle / 'review')

    def test_re_running_the_command_reuses_the_imported_manuscript(self):
        """The manuscript is immutable, so a resumed run must not re-import it."""
        self.imported_already()
        imported, review, _, _ = self.run_command()
        imported.assert_not_called()
        self.assertEqual(Path(review.call_args.args[0]), self.bundle / 'manuscript')

    def test_a_finished_review_stops_the_command_and_names_its_report(self):
        """A second run over a finished review would spend the subscription
        again for a report the folder already has."""
        self.imported_already()
        write(self.bundle / 'review/progress.json', {'status': 'complete'})
        noise = self.refused(*CUTOFF)
        self.assertIn('already holds a finished review', noise)
        self.assertIn(str(self.bundle / 'REVIEW.md'), noise)
        self.assertFalse((self.bundle / 'review-2').exists())

    def test_a_complete_run_without_findings_also_counts_as_finished(self):
        self.imported_already()
        write(self.bundle / 'review/progress.json', {'status': 'complete-without-findings'})
        self.assertIn('already holds a finished review', self.refused(*CUTOFF))

    def test_a_failed_run_is_kept_and_the_next_attempt_gets_its_own_folder(self):
        self.imported_already()
        write(self.bundle / 'review/progress.json', {'status': 'blocked', 'error': 'transport'})
        imported, review, report, _ = self.run_command()
        imported.assert_not_called()
        self.assertEqual(Path(review.call_args.args[1]), self.bundle / 'review-2')
        self.assertEqual(Path(report.call_args.args[0]), self.bundle / 'review-2')
        self.assertTrue((self.bundle / 'review/progress.json').is_file())

    def test_an_interrupted_run_is_treated_as_unfinished(self):
        """A run whose last recorded state is ``running`` never reported."""
        self.imported_already()
        write(self.bundle / 'review/progress.json', {'status': 'running', 'stage': 'reviewer-1'})
        write(self.bundle / 'review-2/progress.json', {'status': 'blocked'})
        _, review, _, _ = self.run_command()
        self.assertEqual(Path(review.call_args.args[1]), self.bundle / 'review-3')

    def test_a_different_pdf_is_refused_rather_than_reviewed_against_the_old_import(self):
        other = self.root / 'other.pdf'
        other.write_bytes(b'%PDF-1.7\nanother manuscript\n')
        self.imported_already(pdf=other)
        noise = self.refused(*CUTOFF)
        self.assertIn('already holds a different manuscript', noise)

    def test_a_stopped_review_still_writes_its_report_and_exits_non_zero(self):
        """The one-command form owns the whole job, so a blocked run reports as
        blocked instead of ending in a traceback with no report."""
        def blocked(workspace, output, *args, **kwargs):
            write(Path(output) / 'protocol.json', {'mode': 'ensemble'})
            write(Path(output) / 'progress.json', {'status': 'blocked', 'error': 'transport lost'})
            raise ValueError('transport lost')

        with self.assertRaises(SystemExit) as stopped:
            _, _, report, out = self.run_command(review={'side_effect': blocked})
            self.assertEqual(Path(report.call_args.args[0]), self.bundle / 'review')
            self.assertIn('blocked', out)
        self.assertEqual(stopped.exception.code, 1)

    def test_a_run_that_never_started_reports_its_cause_in_one_line(self):
        printed = io.StringIO()
        parser = build_parser()
        args = parser.parse_args(['review', str(self.pdf), str(self.bundle), *CUTOFF])
        with patch.object(ingest, 'import_paper',
                          return_value={'title': 'Study', 'statistics': {}}), \
             patch.object(reviewer, 'review', side_effect=RuntimeError('no subscription')), \
             patch.object(run_report, 'write_reports') as report, \
             contextlib.redirect_stdout(printed), self.assertRaises(SystemExit) as stopped:
            dispatch(parser, args)
        self.assertEqual(stopped.exception.code, 1)
        report.assert_not_called()
        self.assertIn('Review did not finish: RuntimeError: no subscription', printed.getvalue())

    def test_a_failed_import_leaves_no_half_imported_manuscript_behind(self):
        """A kept original.pdf would make the different-manuscript refusal
        meaningless, and a kept workspace would be reviewed as a manuscript."""
        def half(pdf, output, *args, **kwargs):
            (Path(output) / 'original.pdf').parent.mkdir(parents=True, exist_ok=True)
            (Path(output) / 'original.pdf').write_bytes(Path(pdf).read_bytes())
            raise ValueError('no pages')

        parser = build_parser()
        args = parser.parse_args(['review', str(self.pdf), str(self.bundle), *CUTOFF])
        with patch.object(ingest, 'import_paper', side_effect=half), \
             patch.object(reviewer, 'review') as reviewed, \
             self.assertRaises(SystemExit), \
             contextlib.redirect_stderr(io.StringIO()) as noise:
            dispatch(parser, args)
        reviewed.assert_not_called()
        self.assertIn('could not be imported', noise.getvalue())
        self.assertFalse((self.bundle / 'manuscript').exists())

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

    def test_a_workspace_run_still_raises_rather_than_swallowing_the_failure(self):
        workspace = self.root / 'workspace'
        write(workspace / 'paper.json', {'id': 'paper'})
        parser = build_parser()
        args = parser.parse_args(['review', str(workspace), str(self.root / 'run'), *CUTOFF])
        with patch.object(reviewer, 'review', side_effect=ValueError('transport lost')), \
             contextlib.redirect_stdout(io.StringIO()), \
             self.assertRaisesRegex(ValueError, 'transport lost'):
            dispatch(parser, args)

    def test_every_path_that_is_neither_a_pdf_nor_a_workspace_is_refused_by_name(self):
        text = self.root / 'notes.txt'
        text.write_text('not a manuscript')
        empty = self.root / 'empty-directory'
        empty.mkdir()
        cases = {self.root / 'absent.pdf': 'does not exist',
                 text: 'is not a PDF',
                 empty: 'not an imported manuscript workspace'}
        for path, expected in cases.items():
            with self.subTest(path=path.name):
                self.assertIn(expected, self.refused(*CUTOFF, paper=path))


if __name__ == '__main__':
    unittest.main()
