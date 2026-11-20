"""The surface an application is allowed to build on, and every folder it meets.

An app that shows reviews — the paper-workbench migration was the first — has
to answer four questions about a folder on disk: did it finish, why did it
stop, what did it find, what did it cost. Before 0.9 shipped that meant opening
six files by hand, knowing which of them a stopped run never wrote, and
importing private names out of ``plugin.server`` and ``plugin.starter``. These
tests pin the public answer: :func:`peerreviewagents.read_run` on one folder of
every kind a review can leave behind, the two commands as functions, and the
constants that say what a run folder is called.

Every test here is offline. Nothing starts Claude Code: the one launch test
runs with nothing on ``PATH``, which is also the check that a missing provider
is a blocked run that still identifies itself.
"""

from __future__ import annotations

import contextlib
import io
import json
import time
from pathlib import Path

import pytest
from manuscripts import build_base

import peerreviewagents
from peerreviewagents import issues as issue_model
from peerreviewagents import names, runs, scripted
from peerreviewagents.cli.main import report_main
from peerreviewagents.engine import citation_ledger
from peerreviewagents.plugin import runner as agent_runner
from peerreviewagents.plugin import server as srv
from peerreviewagents.plugin import starter
from peerreviewagents.runs import NotARunFolder, read_run

FIXTURE = Path(__file__).resolve().parent / 'fixtures' / 'single-column.pdf'


def _write(path: Path, value) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding='utf-8')
    return path


def _rows(path: Path, records) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(''.join(json.dumps(record) + '\n' for record in records), encoding='utf-8')
    return path


def _finding(uid, title, **extra):
    return {'finding_uid': uid, 'title': title, 'slot': 'core-1', 'track': 'core',
            'severity': 'major', 'kind': 'question', 'document': 'paper.md', 'line': 7,
            'quote': 'the reported total', 'escalate': False,
            'schema_version': names.FINDING_SCHEMA_VERSION, **extra}


def _issue_row(**fields):
    """One issue row in the shape the server writes: every field, null where it does not apply."""
    row = {'rank': None, 'members': [], 'raised_by': 0, 'rebuttal_document': None,
           'rebuttal_line': None, 'rebuttal_quote': None, 'audits': []}
    row.update(fields)
    assert sorted(row) == sorted(issue_model.ISSUE_FIELDS), sorted(row)
    return row


def _issues(raised):
    """The two issues the editor grouped the fixture's findings into."""
    total, stated_n = raised
    return [
        _issue_row(
            id='i01', name='The reported total does not follow from its parts',
            severity='major', category='results-and-numbers', rank=1,
            statement='The total in the abstract is not the sum of the groups it is drawn '
                      'from, and the headline conclusion rests on the larger number.',
            members=[issue_model.member_of(total)], raised_by=1),
        _issue_row(
            id='i02', name='The stated n disagrees with Table 2',
            severity='minor', category='statistics-and-inference', rank=2,
            statement='Table 2 counts fewer animals than the methods report, so a reader '
                      'cannot tell which n the tests were run over.',
            members=[issue_model.member_of(stated_n)], raised_by=1),
    ]


def complete_run(tmp_path, status='complete', findings=True) -> Path:
    """A run folder as ``write_review`` leaves it: the files, the reports, the ledger."""
    review = tmp_path / 'run' / names.RUN_DIRECTORY
    _write(review / 'protocol.json', {
        'provider': 'claude-code', 'arm': 'full-two-track', 'mode': 'full-two-track',
        'reviewers': 2, 'paper_id': 'paper-7', 'literature_cutoff': '2024-01-01',
        'version': 'plugin-two-track-v1',
        'finding_schema_version': names.FINDING_SCHEMA_VERSION,
        'plan': {'arm': 'full-two-track', 'reviewers': 2, 'specialists': ['stats'],
                 'editor': True, 'revision': False},
        'agents': {'core-1': {'role': 'core', 'track': 'core', 'tool_calls': 11,
                              'model': 'claude-sonnet-5-5'},
                   'spec-stats': {'role': 'specialist', 'track': 'stats', 'tool_calls': 6,
                                  'model': 'claude-sonnet-5-5'}},
    })
    _write(review / 'manifest.json', {'paper_id': 'paper-7', 'source_sha256': 'cafe' * 16,
                                      'pdf': str(tmp_path / 'paper.pdf'),
                                      'workflow_version': 'plugin-two-track-v1',
                                      'claude_code_version': '2.1.322'})
    _write(review / 'progress.json', {'status': status, 'error': None, 'arm': 'full-two-track',
                                      'findings_delivered': 2 if findings else 0,
                                      'findings_submitted': 3 if findings else 0,
                                      'review_guard_status': 'clean',
                                      'stages': {'required': ['core-1'], 'missing': []}})
    _write(review / 'runtime.json', {
        'status': status, 'blocked_reason': None, 'wall_seconds': 247.0,
        'tokens_total': 636299, 'claude_code_version': '2.1.322',
        'result': {'subtype': 'success', 'is_error': False},
        'agents': [{'label': 'core-1', 'tokens': 400000, 'toolCalls': 24, 'state': 'completed',
                    'model': 'claude-sonnet-5-5', 'ending': 'answered',
                    'detail': 'the agent answered and was then stopped',
                    'tokens_cumulative': 400000, 'tokens_input_uncached': 24000,
                    'tokens_cache_write': 76000, 'tokens_cache_read': 288000,
                    'tokens_output': 12000, 'tokens_peak_context': 144072},
                   {'label': 'spec-stats', 'tokens': 236299, 'toolCalls': 9,
                    'state': 'completed', 'model': 'claude-sonnet-5-5'}]})
    raised = [_finding('aaa1', 'The total does not follow from its parts'),
              _finding('bbb2', 'The stated n disagrees with Table 2', slot='spec-stats',
                       track='stats', escalate=True)] if findings else []
    issue_rows = _issues(raised) if findings else []
    _write(review / 'issues.json', {
        'schema_version': issue_model.ISSUES_SCHEMA_VERSION,
        'issues_source': 'editor',
        'summary': 'Two issues in the reported totals.',
        'review_guard_status': 'clean',
        'issues': issue_rows,
        'counts': issue_model.counts(issue_rows),
        'escalation_rulings': ([{'finding_uid': 'bbb2', 'ruling': 'upheld'}] if findings else []),
        'limitations': ['No access to the raw data.'],
        'verdict': {'significance': 'moderate'},
        'findings_by_track': {'core': 1, 'stats': 1},
    })
    _rows(review / 'findings.jsonl', raised)
    (review / 'audit_stats.md').write_text('# stats audit\n', encoding='utf-8')
    (review / 'REVIEW.md').write_text('# Review\n', encoding='utf-8')
    return review


def blocked_run(tmp_path) -> Path:
    """A run the server marked blocked: a stage that never reported."""
    review = tmp_path / 'run' / names.RUN_DIRECTORY
    _write(review / 'protocol.json', {'arm': 'reviewers-only', 'reviewers': 3,
                                      'paper_id': 'paper-9', 'agents': {}})
    _write(review / 'manifest.json', {'paper_id': 'paper-9', 'source_sha256': 'beef' * 16})
    _write(review / 'progress.json', {'status': 'blocked',
                                      'error': 'Stages that never reported: core-3'})
    _write(review / 'runtime.json', {'status': 'blocked',
                                     'blocked_reason': 'Stages that never reported: core-3',
                                     'wall_seconds': 88.0, 'tokens_total': 120000, 'agents': []})
    _rows(review / 'findings.jsonl', [_finding('aaa1', 'An issue filed before the stall')])
    return review


def killed_run(tmp_path) -> Path:
    """A run the starter killed: the server never got to change its own status."""
    review = tmp_path / 'run' / names.RUN_DIRECTORY
    _write(review / 'protocol.json', {'arm': 'single', 'reviewers': 1, 'paper_id': 'paper-3',
                                      'agents': {'core-1': {'role': 'core', 'track': 'core'}}})
    _write(review / 'manifest.json', {'paper_id': 'paper-3', 'source_sha256': 'f00d' * 16})
    _write(review / 'progress.json', {'status': 'running', 'error': None})
    _write(review / 'runtime.json', {
        'status': 'blocked',
        'blocked_reason': 'core-1 passed the per-agent token ceiling of 500,000',
        'wall_seconds': 1203.0, 'tokens_total': 512000,
        'rate_limits': [], 'per_agent_token_ceiling': 500000,
        'agents': [{'label': 'core-1', 'tokens': 512000, 'state': 'running'}]})
    return review


def startup_blocked_run(tmp_path) -> Path:
    """The probe refused to launch: startup.json and runtime.json, and nothing else."""
    review = tmp_path / 'run' / names.RUN_DIRECTORY
    _write(review / 'startup.json', {'probe': None, 'problems': ['the pra plugin was not loaded'],
                                     'tools_expected': [], 'at': '2026-11-12T00:00:00+00:00'})
    _write(review / 'runtime.json', {
        'status': 'blocked',
        'blocked_reason': 'the start-up probe failed: the pra plugin was not loaded',
        'wall_seconds': 0.0, 'tokens_total': 0, 'agents': []})
    return review


# -- read_run, one folder of every kind -------------------------------------


def test_read_run_reads_a_complete_run(tmp_path):
    review = complete_run(tmp_path)

    run = read_run(review)

    assert (run['status'], run['complete'], run['blocked']) == ('complete', True, False)
    assert run['cause'] is None
    assert run['findings_source'] == 'issues'
    assert run['issues_source'] == 'editor'
    assert [issue['id'] for issue in run['issues']] == ['i01', 'i02']
    assert [f['finding_uid'] for f in run['findings']] == ['aaa1', 'bbb2']
    assert run['counts'] == {
        'findings': 2, 'issues': 2, 'required': 1, 'suggestions': 1, 'issue_members': 2,
        'by_severity': {'major': 1, 'moderate': 0, 'minor': 1},
        'by_category': {'results-and-numbers': 1, 'statistics-and-inference': 1},
        'quarantined': 0, 'quarantined_superseded': 0,
        'findings_delivered': 2, 'findings_submitted': 3}
    assert run['identity']['source_sha256'] == 'cafe' * 16
    assert run['identity']['arm'] == 'full-two-track'
    assert run['runtime']['tokens_total'] == 636299
    assert run['reports']['audits'] == {'stats': str(review / 'audit_stats.md')}
    assert run['reports']['markdown'] == str(review / 'REVIEW.md')
    assert run['reports']['issues'] == str(review / 'issues.json')


def test_read_run_says_what_state_the_manuscript_is_in(tmp_path):
    """Four folders, four answers, none of them needing draft.json by name.

    An app showing a stopped run could not tell one whose Read stage died —
    the paper is converted, in a draft, with no line numbers — from one that
    stopped before the import, without opening this package's own working
    file. The state is the public answer to that.
    """
    review = complete_run(tmp_path)
    workspace = review.parent / names.MANUSCRIPT_DIRECTORY
    assert read_run(review)['manuscript'] == {
        'state': 'absent', 'sealed': False, 'reason': '', 'reader_model': '',
        'lost_pages': [], 'note': '', 'documents': [], 'folder': None}

    # The draft the converter writes, before anything sealed it.
    workspace.mkdir(parents=True)
    _write(workspace / 'draft.json', {'schema_version': names.DRAFT_SCHEMA_VERSION})
    drafted = read_run(review)['manuscript']
    assert (drafted['state'], drafted['sealed']) == ('draft', False)
    assert drafted['folder'] == str(workspace)

    # The base an import writes with no reader: the draft is gone, and the
    # manifest says in its own words why nothing read the pages.
    (workspace / 'draft.json').unlink()
    build_base(workspace, [{'kind': 'title', 'text': 'A manuscript', 'section': 'Title'},
                           {'kind': 'paragraph', 'text': 'One sentence.', 'section': 'Body'}])
    converted = read_run(review)['manuscript']
    assert (converted['state'], converted['sealed']) == ('sealed-without-read', True)
    assert converted['documents'] == [names.MAIN_DOCUMENT]
    assert converted['reason'] and converted['reader_model'] == ''

    # And the base the Read stage seals, which names the model that read it.
    build_base(workspace, [{'kind': 'title', 'text': 'A manuscript', 'section': 'Title'}],
               manifest={'ai_read': {'read': True, 'reader_model': 'claude-sonnet-5-5',
                                     'note': 'Table 4 was inside the converter\'s Table 3.',
                                     'lost_pages': [{'page': 2, 'words': 36, 'rotation': 0,
                                                     'added': [{'piece': 'p3',
                                                                'document': names.MAIN_DOCUMENT,
                                                                'lines': [3, 4]}]}]}})
    read = read_run(review)['manuscript']
    assert (read['state'], read['sealed']) == ('sealed-by-reader', True)
    assert read['reader_model'] == 'claude-sonnet-5-5' and read['reason'] == ''
    assert read['lost_pages'][0]['added'][0]['lines'] == [3, 4]
    assert read['note'] == 'Table 4 was inside the converter\'s Table 3.', \
        'what the reader knew about the document and a reviewer could not work out'
    assert read['state'] in names.MANUSCRIPT_STATES


def test_read_run_reports_the_runtime_of_each_agent(tmp_path):
    review = complete_run(tmp_path)

    rows = {row['slot']: row for row in read_run(review)['agents']}

    assert rows['core-1']['tokens'] == 400000
    assert rows['core-1']['tool_calls'] == 11  # what the server served it
    assert rows['core-1']['session_calls'] == 24  # what the session saw it do
    assert rows['spec-stats']['track'] == 'stats'
    assert rows['spec-stats']['model'] == 'claude-sonnet-5-5'


def test_read_run_reports_how_each_agent_ended_and_what_it_spent(tmp_path):
    """The runner's own words, and the tokens in the unit each one means.

    An application reading these rows had to guess two things from an absence:
    that the recorder ran in this process, because its model was empty, and
    what ``tokens`` was a count of. Both are words in the row now.
    """
    review = complete_run(tmp_path)

    rows = {row['slot']: row for row in read_run(review)['agents']}

    assert rows['core-1']['ending'] == 'answered'
    assert rows['core-1']['detail'] == 'the agent answered and was then stopped'
    assert [rows['core-1'][field] for field in names.TOKEN_FIELDS] == [
        400000, 24000, 76000, 288000, 12000, 144072]
    assert sum(rows['core-1'][field] for field in names.TOKEN_FIELDS[1:5]) == 400000, \
        'the four parts are the cumulative total broken up'
    assert rows['core-1'][names.TOKEN_PEAK] < rows['core-1']['tokens_cumulative'], \
        'the peak is one call; the total is the session'
    assert all(rows['spec-stats'][field] is None for field in names.TOKEN_FIELDS), \
        'not recorded is None, never zero'
    assert rows['spec-stats']['ending'] is None and rows['spec-stats']['detail'] is None


def test_read_run_carries_the_escalations_and_their_rulings(tmp_path):
    run = read_run(complete_run(tmp_path))

    assert [f['finding_uid'] for f in run['escalations']] == ['bbb2']
    assert run['escalation_rulings'] == [{'finding_uid': 'bbb2', 'ruling': 'upheld'}]


def test_read_run_reads_a_run_that_completed_without_findings(tmp_path):
    run = read_run(complete_run(tmp_path, status='complete-without-findings', findings=False))

    assert run['status'] == 'complete-without-findings'
    assert run['complete'] is True
    assert (run['cause'], run['findings'], run['counts']['findings']) == (None, [], 0)


def test_read_run_reads_a_blocked_run_and_keeps_the_findings_it_had(tmp_path):
    run = read_run(blocked_run(tmp_path))

    assert run['status'] == 'blocked'
    assert run['cause'] == 'Stages that never reported: core-3'
    assert run['findings_source'] == 'ledger'
    assert [f['finding_uid'] for f in run['findings']] == ['aaa1']
    assert run['issues'] == []


def test_read_run_reads_a_run_the_starter_killed(tmp_path):
    """The server's last word was `running`; the starter's is why it stopped."""
    run = read_run(killed_run(tmp_path))

    assert run['status'] == 'blocked'
    assert 'per-agent token ceiling' in run['cause']
    assert run['stage'] == 'session'
    assert run['identity']['paper_id'] == 'paper-3'


def test_read_run_reads_a_startup_blocked_run(tmp_path):
    run = read_run(startup_blocked_run(tmp_path))

    assert run['status'] == 'blocked'
    assert run['cause'] == 'the start-up probe failed: the pra plugin was not loaded'
    assert run['stage'] == 'startup'
    assert run['startup_problems'] == ['the pra plugin was not loaded']
    assert (run['findings'], run['agents'], run['findings_source']) == ([], [], 'none')


def test_read_run_de_duplicates_the_ledger_to_the_latest_row_per_finding(tmp_path):
    """findings.jsonl is append-only: a restated finding is a row, not a finding."""
    review = tmp_path / 'run' / names.RUN_DIRECTORY
    _write(review / 'progress.json', {'status': 'blocked', 'error': 'the session ended'})
    _rows(review / 'findings.jsonl', [
        _finding('aaa1', 'First wording'),
        _finding('bbb2', 'Another issue'),
        _finding('aaa1', 'Second wording, same evidence'),
    ])

    run = read_run(review)

    assert [(f['finding_uid'], f['title']) for f in run['findings']] == [
        ('aaa1', 'Second wording, same evidence'), ('bbb2', 'Another issue')]


def test_read_run_takes_the_run_folder_as_well_as_the_review_folder(tmp_path):
    review = complete_run(tmp_path)

    assert read_run(review.parent)['review'] == str(review)
    assert read_run(review)['run'] == str(review.parent)


def test_read_run_refuses_a_symlinked_run_file(tmp_path):
    """A symlink in a run file's place is some other file's contents, not this run's."""
    review = complete_run(tmp_path)
    elsewhere = _write(tmp_path / 'elsewhere' / 'issues.json',
                       {'summary': 'not this run', 'issues': []})
    (review / 'issues.json').unlink()
    try:
        (review / 'issues.json').symlink_to(elsewhere)
    except OSError as exc:  # Windows without the privilege. The guard is still there.
        pytest.skip(f'this machine cannot create a symlink to test against: {exc}')

    with pytest.raises(ValueError, match='issues.json'):
        read_run(review)


def test_read_run_refuses_a_folder_that_is_not_a_run(tmp_path):
    (tmp_path / 'notes.txt').write_text('nothing here', encoding='utf-8')

    with pytest.raises(NotARunFolder, match='protocol.json'):
        read_run(tmp_path)

    with pytest.raises(NotARunFolder):
        read_run(tmp_path / 'does-not-exist')


def test_read_run_survives_a_truncated_file(tmp_path):
    """A process killed mid-write leaves half a JSON file. That is not an exception."""
    review = startup_blocked_run(tmp_path)
    (review / 'progress.json').write_text('{"status": "run', encoding='utf-8')

    assert read_run(review)['status'] == 'blocked'


# -- a review driven from a script ------------------------------------------
#
# Every folder above is hand-built: `complete_run` writes the six files a
# finished review holds in the shape this version writes them, which is a copy
# of the engine's output and drifts the moment the engine moves.
# `scripted_review` is the other way round — the lane writes the folder, with
# no model in it — so these tests are both the fixture a consumer builds and
# the check that the shape above is still the shape.


def test_a_scripted_review_writes_the_folder_a_consumer_renders(tmp_path):
    """The whole run, no model: the stages in order, the folder they leave behind."""
    folder = tmp_path / 'run'

    review = peerreviewagents.scripted_review(FIXTURE, folder)

    assert review.ran == ('reader', 'core-1', 'editor')
    assert (review.status, review.blocked_reason) == ('complete', None)
    uids = [row['finding_uid'] for row in review.slot('core-1').answers('submit_finding')]
    assert review.read()['status'] == 'complete'

    run = read_run(folder)
    assert (run['status'], run['complete']) == ('complete', True)
    assert run['issues_source'] == 'editor'
    assert [member['finding_uid'] for member in run['issues'][0]['members']] == uids
    assert run['counts']['by_severity'] == {'major': 1, 'moderate': 0, 'minor': 0}
    assert Path(run['reports']['markdown']).exists()
    assert Path(run['reports']['issues']).exists()
    assert run['identity']['arm'] == 'reviewers-only'


def test_a_scripted_run_leaves_the_runtime_rows_a_real_run_leaves(tmp_path):
    """The point of running through the orchestrator: a folder of the same shape.

    A scripted run is accounted for like any other — one runtime row per agent
    with an ending and the token fields, the recorder in this process, the
    runner named — which is what a consumer's fixture of a *finished* run has
    to hold if the reader it tests is the one that reads a real one.
    """
    review = peerreviewagents.scripted_review(FIXTURE, tmp_path / 'run')

    runtime = json.loads((review.review / 'runtime.json').read_text(encoding='utf-8'))
    rows = {row['label']: row for row in runtime['agents']}
    assert set(rows) == {'reader', 'core-1', 'editor', 'recorder'}
    ran = [rows[label] for label in ('reader', 'core-1', 'editor')]
    assert [row['ending'] for row in ran] == ['answered'] * 3
    assert all(row['tokens_cumulative'] and row['tokens_peak_context'] for row in ran)
    assert all(row['toolCalls'] and row['state'] == 'done' for row in ran)
    assert rows['core-1']['agentType'] == 'core-reviewer'
    assert (rows['recorder']['ending'], rows['recorder']['model']) == (starter.IN_PROCESS, None)
    assert runtime['tokens_total'] == sum(row['tokens'] for row in ran)
    assert review.summary['runner'] == 'scripted'


def test_a_scripted_review_holds_every_slot_the_arm_planned(tmp_path):
    """The specialists too, and a track that found nothing closes as such."""
    review = peerreviewagents.scripted_review(FIXTURE, tmp_path / 'run', arm='full-two-track',
                                              reviewers=2, tracks=['stats'])

    # Every planned slot ran, and the reader ran before them. `ran` is arrival
    # order, and the seats of one stage are started together, so which of them
    # arrives first is the thread scheduler's business and not the plan's.
    planned = srv.plan_slots('full-two-track', 2, ['stats'])
    assert review.ran[0] == 'reader'
    assert set(review.ran[1:]) == set(planned)
    assert review.ran[-1] == planned[-1] == 'editor'  # the editor runs after the panel
    assert review.slot('spec-stats').last('submit_report')['closed']
    assert review.summary['stages']['none_found'] == ['spec-stats']
    assert review.summary['issues']['issue_members'] == 2
    assert read_run(tmp_path / 'run')['counts']['findings'] == 2


def test_a_scripted_slot_is_confined_to_the_tools_its_agent_is_granted(tmp_path):
    """The grant boundary is the agent definition's, and a script meets it as one."""
    def editor(slot):
        assert 'submit_issues' in slot.granted
        # The editor's definition grants it no submit_finding, as a model's
        # prompt would not name one; the boundary is held before the call.
        with pytest.raises(agent_runner.NotGranted, match='submit_finding'):
            slot.submit_finding(title='The editor files a finding', line=7,
                                quote=slot.line(7), severity='major',
                                comment='A slot that may not do this does not reach the server.')
        with pytest.raises(AttributeError, match='rewrite_the_paper'):
            slot.rewrite_the_paper()
        scripted.DEFAULT_SCRIPTS['editor'](slot)

    review = peerreviewagents.scripted_review(FIXTURE, tmp_path / 'run', {'editor': editor})

    assert review.status == 'complete'
    assert review.slot('editor').last('submit_editor')['accepted']


def test_a_scripted_review_raises_the_refusal_a_model_would_have_read(tmp_path):
    """A refused tool raises where the call was made, and again out of the run."""
    def editor(slot):
        slot.submit_editor(summary='Closed with nothing grouped.')

    with pytest.raises(peerreviewagents.ReviewRefused) as refused:
        peerreviewagents.scripted_review(FIXTURE, tmp_path / 'run', {'editor': editor})

    assert refused.value.tool == 'submit_editor'
    assert refused.value.answer['findings_without_issue_uids']
    assert read_run(tmp_path / 'run')['status'] == 'blocked'


def test_a_script_that_answers_nothing_blocks_the_run_it_was_in(tmp_path):
    """The one outcome a script cannot have by returning: an agent that said nothing."""
    review = peerreviewagents.scripted_review(FIXTURE, tmp_path / 'run',
                                              {'core-1': lambda slot: scripted.NO_ANSWER})

    assert review.status == 'blocked'
    assert 'core-1' in review.blocked_reason
    assert review.summary['stages']['attempts']['core-1'] == 2  # the one retry, then blocked


def test_a_scripted_review_can_run_the_citations_lane_with_no_network(tmp_path):
    """``offline=True``: the lane ends in seconds, says why, and opens no socket.

    The citations lane is the one stage that leaves the machine, and a consumer
    covering it had two choices before this: let the lookups go to the live
    indexes, or block sockets in the test suite. Behind a firewall the first
    one cost minutes per run — the lane waited out a retry gap for hosts that
    could not be reached — which is the same thing a user offline met.

    What the folder says is the point: the references are *not checked*, with
    the reason on the stage record, and nothing in the table claims to be
    evidence about those works.
    """
    folder = tmp_path / 'run'
    started = time.monotonic()

    review = peerreviewagents.scripted_review(FIXTURE, folder, arm='full-two-track',
                                              offline=True)

    assert review.status == 'complete'
    # No retry gap was waited out: a gap is five minutes, and three of them is
    # what this lane used to spend before reporting the rows unchecked.
    assert time.monotonic() - started < 60
    stages = json.loads((review.review / 'citations' / 'stages.json').read_text(encoding='utf-8'))
    resolve = stages['resolve']
    assert resolve['hosts_reached'] is False
    assert resolve['waited'] == 0
    assert resolve['remaining'] == resolve['references'] > 0
    assert 'no lookup host could be reached' in resolve['unchecked_reason']
    assert resolve['lookups'] > 0, 'the hosts were asked before the lane gave up'

    citations = read_run(folder)['citations']
    assert citations['counts']['resolved'] == 0
    assert any('no lookup host could be reached' in line for line in citations['problems'])
    # And the opener is the run's, not the process's: nothing is left behind.
    assert srv.STATE['opener'] is None


def test_the_scripted_review_is_public_and_puts_back_the_run_it_found(tmp_path):
    """It is a top-level name, and the run attached before it is attached after."""
    assert peerreviewagents.scripted_review is scripted.scripted_review
    assert {'scripted_review', 'ScriptedReview', 'ReviewRefused'} <= set(peerreviewagents.__all__)
    assert issubclass(scripted.ReviewRefused, scripted.ToolRefused)

    held = object()
    srv.STATE['run'] = held
    peerreviewagents.scripted_review(FIXTURE, tmp_path / 'run')

    assert srv.STATE['run'] is held
    srv.STATE['run'] = None


# -- a run blocked at start-up, from a script --------------------------------
#
# `startup_blocked_run` above is the hand-built version of this folder, and
# what an app renders when the machine it ran on had no Claude Code on it.
# `missing=` is the same folder written by the starter's own runner-missing
# path, so a consumer's fixture of a blocked-at-start-up run is the folder a
# real run leaves rather than one file written from memory.


def test_a_scripted_review_is_blocked_at_start_up_by_a_runner_that_is_missing(tmp_path):
    """Nothing ran, the folder says why, and it still names the paper it was for."""
    folder = tmp_path / 'run'

    review = peerreviewagents.scripted_review(FIXTURE, folder,
                                              missing='claude is not installed')

    assert (review.status, review.blocked_reason) == ('blocked', 'claude is not installed')
    assert review.summary['stage'] == 'startup'
    assert review.ran == ()
    assert review.summary['tokens_total'] == 0

    startup = json.loads((review.review / 'startup.json').read_text(encoding='utf-8'))
    assert startup['problems'] == ['claude is not installed']
    assert (startup['runner'], startup['server']) == ('scripted', None)
    assert startup['tools_expected'] == list(starter.tool_names())
    assert startup['at']

    runtime = json.loads((review.review / 'runtime.json').read_text(encoding='utf-8'))
    assert (runtime['status'], runtime['blocked_reason']) == ('blocked',
                                                             'claude is not installed')
    # The recorder is this process and is accounted for; no seat was started.
    assert [row['label'] for row in runtime['agents']] == ['recorder']
    assert runtime['tokens_total'] == 0


def test_a_review_blocked_by_a_missing_runner_reads_as_one_a_consumer_knows(tmp_path):
    """The reading of the folder, which is what an app shows: blocked, at start-up."""
    folder = tmp_path / 'run'

    peerreviewagents.scripted_review(FIXTURE, folder, arm='single', missing='claude is missing')

    run = read_run(folder)
    assert (run['status'], run['blocked'], run['complete']) == ('blocked', True, False)
    assert (run['stage'], run['cause']) == ('startup', 'claude is missing')
    assert run['publishable'] is False
    # Identified, because the caller asked for a review of a named paper.
    assert run['identity']['arm'] == 'single'
    assert run['identity']['pdf'] == str(FIXTURE)
    assert run['identity']['source_sha256']
    # Nothing was imported and no stage ran, so there is no manuscript and no read.
    assert run['manuscript']['state'] == 'absent'
    assert (run['read'], run['issues'], run['findings']) == (None, [], [])


def test_a_scripted_fix_check_is_blocked_at_start_up_by_the_same_runner(tmp_path):
    """The other command's folder, read by the other reader."""
    folder = tmp_path / 'check'

    check = peerreviewagents.scripted_fix_check(FIXTURE, folder, [
        {'id': 'i01', 'statement': 'A problem the first review raised.', 'severity': 'major',
         'evidence': [{'document': '', 'quote': 'Converter'}]}],
        missing='claude is not installed')

    assert (check.status, check.summary['stage']) == ('blocked', 'startup')
    startup = json.loads((check.review / 'startup.json').read_text(encoding='utf-8'))
    assert startup['problems'] == ['claude is not installed']

    read = runs.read_fix_check(folder)
    assert (read['status'], read['blocked']) == ('blocked', True)
    assert read['cause'] == 'claude is not installed'
    assert read['arm'] == 'fix-check'
    # No lane claimed anything, so there is nothing checked and nothing to say
    # about the checks: the check is not incomplete, it never started.
    assert (read['checks'], read['checked'], read['read']) == ([], 0, None)
    assert read['incomplete'] is False


def test_a_start_up_refusal_that_is_not_the_missing_runner_still_raises(tmp_path):
    """A caller's own mistake is not a run folder: it is raised where the call was.

    The missing runner is returned because the folder is what the call was for.
    A manuscript the server will not import is the other kind of start-up
    failure, and nothing but the caller can fix it.
    """
    not_a_pdf = tmp_path / 'paper.pdf'
    not_a_pdf.write_bytes(b'this is not a PDF at all\n')

    with pytest.raises(peerreviewagents.ReviewRefused) as refused:
        peerreviewagents.scripted_review(not_a_pdf, tmp_path / 'run')

    assert refused.value.tool == 'ingest'
    assert refused.value.answer['request_rejected']

    # The same paper with a missing runner is blocked by the runner: a machine
    # with nothing to run the agents with never opens the manuscript at all.
    blocked = peerreviewagents.scripted_review(not_a_pdf, tmp_path / 'again',
                                               missing='claude is not installed')

    assert blocked.blocked_reason == 'claude is not installed'


def test_a_scripted_runner_that_is_missing_refuses_every_model_it_is_asked(tmp_path):
    """The one line the starter reads, and the answer a seat gets if it is reached."""
    runner = agent_runner.ScriptedRunner({'*': lambda slot: None},
                                         missing='claude is not installed')

    assert runner.unavailable({}) == ['claude is not installed']
    assert runner.unavailable({'core-1': 'claude-opus-5'}) == ['claude is not installed']
    assert starter.runner_missing(runner) == 'claude is not installed'
    assert agent_runner.ScriptedRunner().unavailable({'core-1': 'claude-opus-5'}) == []
    assert starter.runner_missing(agent_runner.ScriptedRunner()) == ''


# -- the public names -------------------------------------------------------


def test_the_top_level_exports_every_supported_name():
    for name in peerreviewagents.__all__:
        assert getattr(peerreviewagents, name) is not None, name
    assert set(peerreviewagents.__all__) <= set(dir(peerreviewagents))


def test_the_public_constants_are_what_the_server_writes():
    # `names` is where they are defined; the server imports them from there, so
    # the top level and the writer cannot drift apart.
    assert peerreviewagents.RUN_DIRECTORY == names.RUN_DIRECTORY == 'review'
    assert peerreviewagents.MANUSCRIPT_DIRECTORY == names.MANUSCRIPT_DIRECTORY == 'manuscript'
    assert peerreviewagents.FINDING_SCHEMA_VERSION == names.FINDING_SCHEMA_VERSION
    assert peerreviewagents.RUN_STATUSES == ('running', 'blocked', 'complete',
                                             'complete-without-findings')
    assert peerreviewagents.COMPLETE_STATUSES == ('complete', 'complete-without-findings')


def test_review_arms_says_what_each_arm_runs_before_a_run_exists():
    """The answer to "will this arm take code?", asked without starting a run.

    An app offering a review against the code had no way to ask: the arm table
    is private, so it hard-coded the arm names that run specialists and went
    stale the next time one was added. Each flag here is checked against what
    the engine does with that arm rather than against the table it is read
    from, which is the only way this test can fail when the two disagree.
    """
    arms = peerreviewagents.review_arms()

    assert set(arms) == set(srv.REVIEWABLE_ARMS), 'an arm an application cannot see'
    assert list(arms) == sorted(arms)
    assert [name for name, row in arms.items() if row['default']] == [srv.DEFAULT_ARM]
    for name, row in arms.items():
        settings = srv.ARMS[name]
        assert row['reviewers'] == settings['reviewers'], name
        assert (row['specialists'], row['editor'], row['revision']) \
            == (settings['specialists'], settings['editor'], settings['revision']), name
        assert row['summary'] == settings['summary'], name
        # The code review is a specialist lane, so `code` is whether the arm
        # plans specialists — pinned against the plan, and against the refusal
        # a caller would have met instead of an answer.
        planned = srv.plan_slots(name, 2, code=True)
        assert ('spec-code' in planned) is row['code'], name
        if not row['code']:
            with pytest.raises(ValueError, match='runs no specialists'):
                starter.review(FIXTURE, Path('nowhere'), arm=name, code=str(FIXTURE.parent))
        assert row['citations'] is bool(settings['specialists']
                                        and 'check_citation' in settings['tools']), name
    # What each lane is given is not on this surface: the call budgets and the
    # tool lists are the engine's, and an app that branched on them would be
    # pinned to them.
    assert 'tools' not in arms[srv.DEFAULT_ARM]

    arms[srv.DEFAULT_ARM]['code'] = 'edited by the caller'
    assert peerreviewagents.review_arms()[srv.DEFAULT_ARM]['code'] is True


def test_every_arm_this_surface_offers_is_an_arm_a_review_runs(tmp_path):
    """An arm a listing offers and the command refuses is a broken button.

    `fix-check` was in both the listing and the ``--arm`` choices while
    :func:`review` refused it by name, so an application that offered every
    arm it was shown offered one that could not run. Each arm here is pushed
    through the command and through the parser: the arm is accepted when the
    next thing refused is something else about the call, and this test is how
    an arm added to the table without a thought about `review` is caught.
    """
    nothing = tmp_path / 'no-plugin'
    for name in peerreviewagents.review_arms():
        with pytest.raises(ValueError) as past_the_arm:
            starter.review(FIXTURE, tmp_path / name, arm=name, directory=nothing)
        assert str(past_the_arm.value).startswith('No plugin at'), name
        assert starter.build_parser().parse_args(
            [str(FIXTURE), str(tmp_path / name), '--arm', name]).arm == name

    # And the arm that is not a review is refused by both, by name.
    with pytest.raises(ValueError, match='check-fixes'):
        starter.review(FIXTURE, tmp_path / 'fix', arm=srv.FIX_ARM, directory=nothing)
    with pytest.raises(SystemExit), contextlib.redirect_stderr(io.StringIO()) as said:
        starter.build_parser().parse_args([str(FIXTURE), str(tmp_path / 'fix'),
                                           '--arm', srv.FIX_ARM])
    assert srv.FIX_ARM not in said.getvalue().split('choose from')[-1]


def test_read_run_statuses_are_all_in_the_published_tuple(tmp_path):
    folders = [complete_run(tmp_path / 'one'),
               complete_run(tmp_path / 'two', status='complete-without-findings'),
               blocked_run(tmp_path / 'three'), killed_run(tmp_path / 'four'),
               startup_blocked_run(tmp_path / 'five')]

    assert all(read_run(folder)['status'] in peerreviewagents.RUN_STATUSES
               for folder in folders)


# -- the two commands as functions ------------------------------------------


def test_review_main_is_the_public_entry_point(tmp_path, monkeypatch):
    seen = {}

    def fake_review(pdf, out, **options):
        seen.update(pdf=pdf, out=out, options=options)
        return {'status': 'complete', 'stage': 'session'}

    monkeypatch.setattr(starter, 'review', fake_review)
    pdf = tmp_path / 'paper.pdf'
    pdf.write_bytes(b'%PDF-1.7\n')

    with contextlib.redirect_stdout(io.StringIO()) as out:
        code = starter.review_main([str(pdf), str(tmp_path / 'run'), '--reviewers', '1'])

    assert code == 0
    assert seen['options']['reviewers'] == 1
    assert json.loads(out.getvalue())['status'] == 'complete'
    assert peerreviewagents.review_main is starter.review_main


def test_review_main_names_the_wrapper_in_its_own_help():
    with pytest.raises(SystemExit):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            starter.review_main(['--help'], prog='workbench review')

    assert 'workbench review' in out.getvalue()


def test_report_main_names_the_wrapper_in_its_own_help():
    """Both entry points take ``prog``, so a wrapping app's help says one name."""
    with pytest.raises(SystemExit):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            report_main(['--help'], prog='workbench report')

    printed = out.getvalue()
    assert 'workbench report' in printed
    assert 'peerreview report' not in printed


def test_report_main_renders_a_blocked_run_and_prints_the_cause(tmp_path):
    review = blocked_run(tmp_path)

    with contextlib.redirect_stdout(io.StringIO()) as out:
        code = report_main([str(review.parent)])

    printed = json.loads(out.getvalue())
    assert code == 0
    assert printed['status'] == 'blocked'
    assert printed['cause'] == 'Stages that never reported: core-3'
    assert 'Stages that never reported: core-3' in (review / 'REVIEW.md').read_text(
        encoding='utf-8')
    assert peerreviewagents.report_main is report_main


def test_report_main_renders_a_startup_blocked_run(tmp_path):
    """The old message for this folder was "that is not a finished run folder"."""
    review = startup_blocked_run(tmp_path)

    with contextlib.redirect_stdout(io.StringIO()) as out:
        assert report_main([str(review)]) == 0

    assert 'probe failed' in json.loads(out.getvalue())['cause']
    assert 'probe failed' in (review / 'REVIEW.md').read_text(encoding='utf-8')


def test_write_reports_needs_no_workspace_path(tmp_path):
    """A run folder is enough, and a workspace that was never imported is not an error."""
    review = complete_run(tmp_path)

    paths = peerreviewagents.write_reports(review)
    assert paths['markdown'].is_file()

    again = peerreviewagents.write_reports(review, tmp_path / 'run' / 'manuscript')
    assert again['text'].is_file()


# -- the run files a stopped run leaves behind ------------------------------


def test_ingest_writes_the_protocol_and_manifest_before_any_agent_reports(tmp_path):
    srv.STATE['run'] = None
    try:
        opened = srv.ingest(str(FIXTURE), str(tmp_path / 'run'), reviewers=1, arm='single')
        assert 'request_rejected' not in opened, opened
        review = tmp_path / 'run' / names.RUN_DIRECTORY
        manifest = json.loads((review / 'manifest.json').read_text(encoding='utf-8'))
        protocol = json.loads((review / 'protocol.json').read_text(encoding='utf-8'))
    finally:
        srv.STATE['run'] = None

    assert manifest['paper_id'] == opened['paper_id']
    assert manifest['source_sha256']
    assert protocol['arm'] == 'single'
    run = read_run(review)
    assert run['status'] == 'running'
    assert run['identity']['paper_id'] == opened['paper_id']


def test_a_startup_blocked_run_says_which_paper_and_which_arm_it_was(tmp_path, no_claude_cli):
    """With no claude on PATH nothing is spent, and the folder still identifies itself."""
    summary = starter.review(str(FIXTURE), str(tmp_path / 'run'), reviewers=1, arm='single')

    assert summary['status'] == 'blocked' and summary['stage'] == 'startup'
    run = read_run(tmp_path / 'run')
    assert run['stage'] == 'startup'
    assert run['identity']['arm'] == 'single'
    assert run['identity']['source_sha256']  # the PDF, hashed without importing it
    assert run['identity']['pdf'] == str(FIXTURE)
    assert 'claude is not installed' in run['cause']


def test_the_server_keeps_its_own_protocol_when_the_starter_runs_after_it(tmp_path):
    """The starter's thin identity never overwrites the server's full record."""
    review = complete_run(tmp_path)
    before = (review / 'protocol.json').read_text(encoding='utf-8')

    assert starter.write_identity(review, tmp_path / 'paper.pdf', 'single', 1) == []
    assert (review / 'protocol.json').read_text(encoding='utf-8') == before


# -- the draft export ------------------------------------------------------


def test_export_draft_no_longer_emits_the_debate_keys(tmp_path):
    base = build_base(tmp_path / 'manuscript',
                      [{'kind': 'paragraph', 'text': 'A sentence.'}])
    review = {'id': 'r1', 'paper_id': base.paper_id, 'findings': [], 'limitations': [],
              'raw_response': {'component_decisions': [{'kept': True}],
                               'epistemic_audit': {'checked': True}}}

    exported = peerreviewagents.export_draft(base, review,
                                             {'paper_id': base.paper_id, 'items': {}})

    assert 'component_decisions' not in exported
    assert 'epistemic_audit' not in exported
    # Nothing was decided, so the proposal is the manuscript as it stands: the
    # documents themselves, because a draft is now text and not a block list.
    assert exported['proposed_documents'] == base.documents


# -- a damaged folder is still a reading ------------------------------------


def test_read_run_reads_a_damaged_folder_instead_of_raising(tmp_path):
    """Every way a killed process leaves a file behind, in one folder each.

    A truncated ``runtime.json``, a status file holding a list, a half-written
    last line in a usage receipt and a ledger with bytes that are not UTF-8 are
    the ordinary state of a run somebody stopped. Each one used to be a
    traceback out of ``read_run`` — and so out of ``peerreview report``, which
    is where the reason a run stopped is read.
    """
    review = complete_run(tmp_path / 'damaged')
    (review / 'runtime.json').write_text('{"status": "comp', encoding='utf-8')
    _write(review / 'progress.json', ['status', 'complete'])
    (review / 'usage' / 'core-1').mkdir(parents=True)
    (review / 'usage' / 'core-1' / 'usage.jsonl').write_text(
        json.dumps({'label': 'core-1', 'request_sha256': 'a1', 'attempt': 1,
                    'observed_tokens': 400000}) + '\n{"label": "core-1", "observed_tok',
        encoding='utf-8')

    run = read_run(review)

    assert run['counts']['findings'] == 2, 'the issues document was whole and was read'
    assert run['usage']['observed_tokens'] == 400000
    assert run['usage']['unknown'] is True, 'a receipt nobody could read is unresolved spend'
    named = ' | '.join(run['problems'])
    for name in ('runtime.json', 'progress.json', 'usage.jsonl'):
        assert name in named, named
    assert set(run) == set(read_run(complete_run(tmp_path / 'whole'))), 'the same keys either way'

    # A ledger with undecodable bytes is read as far as it goes, and says so.
    ledger = killed_run(tmp_path / 'bytes')
    (ledger / 'findings.jsonl').write_bytes(
        b'{"finding_uid": "aaa1", "title": "a \xff\xfe total"}\n{"finding_uid": "bb\n')

    rough = read_run(ledger)

    assert [f['finding_uid'] for f in rough['findings']] == ['aaa1']
    assert any('findings.jsonl' in problem for problem in rough['problems'])
    assert rough['publishable'] is False


def test_a_limit_after_the_review_was_written_is_a_note_not_a_block(tmp_path):
    """One rule decides the status, and ``REVIEW.md`` is rendered from it.

    ``starter.finish`` records a rate limit or a ceiling that fired after
    ``write_review``, so the two files disagree: the server says complete and
    the starter says blocked. The review was written, so the run is complete and
    the limit is a note.
    """
    review = complete_run(tmp_path)
    _write(review / 'runtime.json', {
        'status': 'blocked', 'blocked_reason': 'the five-hour rate limit was reached',
        'wall_seconds': 300.0, 'tokens_total': 700000, 'agents': []})

    run = read_run(review)

    assert (run['status'], run['complete'], run['blocked']) == ('complete', True, False)
    assert run['cause'] is None
    assert 'rate limit' in run['note']
    assert runs.run_status({'status': 'complete'}, {'status': 'blocked'})['status'] == 'complete'

    paths = peerreviewagents.write_reports(review)
    line = next(text for text in Path(paths['markdown']).read_text(encoding='utf-8').splitlines()
                if 'Run status' in text)
    assert 'complete' in line
    assert 'blocked' not in line.split('complete')[0], line


def test_report_main_returns_a_code_instead_of_raising(tmp_path, capsys):
    """The documented contract: the code comes back, the message goes to stderr."""
    (tmp_path / 'notes.txt').write_text('nothing here', encoding='utf-8')

    assert report_main([str(tmp_path)]) == 1
    assert 'protocol.json' in capsys.readouterr().err

    review = complete_run(tmp_path / 'linked')
    elsewhere = _write(tmp_path / 'elsewhere' / 'issues.json', {'issues': []})
    (review / 'issues.json').unlink()
    try:
        (review / 'issues.json').symlink_to(elsewhere)
    except OSError as exc:  # Windows without the privilege. The guard is still there.
        pytest.skip(f'this machine cannot create a symlink to test against: {exc}')

    assert report_main([str(review)]) == 1
    assert 'symlink' in capsys.readouterr().err


def test_an_unknown_status_is_unknown_and_publishable_says_what_is_showable(tmp_path):
    """``complete`` answers whether the run finished; ``publishable`` whether to show it."""
    assert read_run(complete_run(tmp_path / 'clean'))['publishable'] is True

    # A review with no findings in it, and nothing quarantined: the reviewers
    # read the manuscript and raised nothing. "Nothing make-or-break" is a
    # review, and the editor wrote an issues document for it, so it is
    # publishable.
    none = read_run(complete_run(tmp_path / 'none', status='complete-without-findings',
                                 findings=False))
    assert (none['status'], none['complete'], none['publishable']) == (
        'complete-without-findings', True, True)
    assert none['counts']['findings'] == 0
    assert none['not_publishable_reasons'] == []

    # The same status, the other way round: the reviewers did raise findings and
    # the guard threw out every one of them, so what they raised is not in the
    # review and there is nothing to publish. The reason says how many.
    emptied = complete_run(tmp_path / 'emptied', status='complete-without-findings',
                           findings=False)
    _rows(emptied / 'quarantined.jsonl', [{'finding_uid': 'ddd4', 'reason': 'no such block'},
                                          {'finding_uid': 'eee5', 'reason': 'quote not found'}])

    held_all = read_run(emptied)

    assert (held_all['status'], held_all['publishable']) == ('complete-without-findings', False)
    assert any('quarantined 2' in reason and 'nothing to publish' in reason
               for reason in held_all['not_publishable_reasons']), \
        held_all['not_publishable_reasons']

    guarded = complete_run(tmp_path / 'guarded')
    document = json.loads((guarded / 'issues.json').read_text(encoding='utf-8'))
    _write(guarded / 'issues.json', {**document, 'review_guard_status': 'quarantined'})
    _rows(guarded / 'quarantined.jsonl', [{'finding_uid': 'ddd4', 'reason': 'no such block'}])

    held = read_run(guarded)

    # The guard threw out a finding it could not anchor, which is the guard
    # working. What is left is a whole review, so it is publishable and the
    # quarantined row is there for a person to weigh.
    assert (held['status'], held['guard_status'], held['publishable']) == (
        'complete', 'quarantined', True)
    assert held['not_publishable_reasons'] == []
    assert held['counts']['quarantined'] == 1

    odd = complete_run(tmp_path / 'odd')
    progress = json.loads((odd / 'progress.json').read_text(encoding='utf-8'))
    _write(odd / 'progress.json', {**progress, 'status': 'finished'})
    _write(odd / 'runtime.json', {'status': 'finished', 'agents': []})

    strange = read_run(odd)

    assert (strange['status'], strange['complete'], strange['publishable']) == (
        'unknown', False, False)
    assert any('finished' in problem for problem in strange['problems'])
    assert any('not complete' in reason for reason in strange['not_publishable_reasons'])


def _without_guard(path: Path) -> dict:
    """The JSON at *path* with its guard verdict taken out, as an older run has."""
    held = json.loads(Path(path).read_text(encoding='utf-8'))
    return {key: value for key, value in held.items() if key != 'review_guard_status'}


def test_publishable_is_never_false_without_saying_why(tmp_path):
    """A flag that holds a review back has to name the reason that held it.

    An application cannot act on ``False`` alone: a guard that threw out one
    unanchored quote, a truncated issues document and a killed run are three
    different events, and one of them is not a reason to hold the review at all.
    """
    whole = read_run(complete_run(tmp_path / 'whole'))
    assert (whole['publishable'], whole['not_publishable_reasons']) == (True, [])

    for name, prepare, expected in (
        ('ledger', lambda review: (review / 'issues.json').write_text(
            '{"summary": "truncated', encoding='utf-8'), 'ledger'),
        ('killed', lambda review: _write(review / 'runtime.json', {
            'status': 'blocked', 'blocked_reason': 'per-run token ceiling', 'agents': []}), None),
        ('no-guard', lambda review: [_write(review / name, _without_guard(review / name))
                                     for name in ('issues.json', 'progress.json')],
         'guard'),
    ):
        review = complete_run(tmp_path / name)
        if name == 'killed':
            _write(review / 'progress.json', {'status': 'running'})
        prepare(review)

        run = read_run(review)

        assert run['publishable'] is False, name
        assert run['not_publishable_reasons'], f'{name} is held back with nothing named'
        if expected:
            assert any(expected in reason for reason in run['not_publishable_reasons']), \
                run['not_publishable_reasons']


def test_read_run_refuses_every_kind_of_symlink_by_name(tmp_path):
    """A directory, a receipt and an audit are refused the same way as a report."""
    for kind, named in (('review', 'review'), ('usage', 'usage.jsonl'),
                        ('audit', 'audit_stats.md')):
        review = complete_run(tmp_path / kind)
        elsewhere = tmp_path / kind / 'elsewhere'
        elsewhere.mkdir(parents=True, exist_ok=True)
        try:
            if kind == 'review':
                moved = review.parent / 'real-review'
                review.rename(moved)
                review.symlink_to(moved, target_is_directory=True)
            elif kind == 'usage':
                target = _rows(elsewhere / 'usage.jsonl',
                               [{'label': 'core-1', 'observed_tokens': 9}])
                (review / 'usage' / 'core-1').mkdir(parents=True)
                (review / 'usage' / 'core-1' / 'usage.jsonl').symlink_to(target)
            else:
                (elsewhere / 'audit_stats.md').write_text('# not this run\n', encoding='utf-8')
                (review / 'audit_stats.md').unlink()
                (review / 'audit_stats.md').symlink_to(elsewhere / 'audit_stats.md')
        except OSError as exc:  # Windows without the privilege. The guard is still there.
            pytest.skip(f'this machine cannot create a symlink to test against: {exc}')

        with pytest.raises(ValueError, match=named):
            read_run(review.parent if kind == 'review' else review)


def test_a_uid_two_issues_hold_as_evidence_is_one_finding(tmp_path):
    """The members are evidence, so the same uid under two issues is one finding.

    An issue's members carry the uid, the slot, the quote and the block, which
    is what makes ``issues.json`` readable without the ledger beside it. A run
    whose ledger is gone is read from those members, and a uid two issues cite
    is still the one finding it was.
    """
    review = complete_run(tmp_path)
    document = json.loads((review / 'issues.json').read_text(encoding='utf-8'))
    document['issues'][1]['members'] += document['issues'][0]['members']
    _write(review / 'issues.json', document)
    (review / 'findings.jsonl').unlink()

    run = read_run(review)

    assert [f['finding_uid'] for f in run['findings']] == ['aaa1', 'bbb2']
    assert run['counts']['findings'] == 2
    assert run['counts']['issue_members'] == 2
    assert [f['finding_uid'] for f in run['escalations']] == ['bbb2']


def test_a_damaged_issues_document_on_a_complete_run_is_not_a_whole_review(tmp_path):
    """The ledger fallback is real findings, and it is not the editor's review."""
    review = complete_run(tmp_path)
    (review / 'issues.json').write_text('{"summary": "Two issues', encoding='utf-8')

    run = read_run(review)

    assert (run['status'], run['complete']) == ('complete', True)
    assert run['findings_source'] == 'ledger'
    assert run['publishable'] is False
    assert any('issues.json' in problem for problem in run['problems'])

    # Readable JSON holding the wrong shape is the same thing: an issues
    # document this version cannot read, not a review with no issues in it.
    other = complete_run(tmp_path / 'wrong-shape')
    document = json.loads((other / 'issues.json').read_text(encoding='utf-8'))
    _write(other / 'issues.json', {**document, 'issues': 'see the suggestions'})

    shaped = read_run(other)

    assert shaped['issues'] == [] and shaped['publishable'] is False
    assert any("issues is not a list" in problem for problem in shaped['problems'])


def test_a_malformed_field_of_an_issues_document_is_reported_not_raised(tmp_path):
    """``members`` held as a string crashed the reader it was handed to.

    ``member_uids`` called ``member.get`` on whatever the list held, so an
    ``issues.json`` whose ``members`` was a string was iterated one character at
    a time into ``str.get`` and raised an ``AttributeError`` out of
    :func:`read_run` — a malformed file took the reader down instead of being
    reported by it, and an app that lists run folders could not read past it.
    Every field is read through one tolerant reader now: what cannot be read is
    skipped, named in ``problems``, and the run is not publishable with that
    reason.
    """
    cases = {
        'members-string': ('see the findings', 'whose members is a str'),
        'members-holding-a-string': (['aaa1'], '1 members entry(s) that are not objects (str)'),
        'members-number': (7, 'whose members is a int'),
    }
    for name, (held, said) in cases.items():
        review = complete_run(tmp_path / name)
        document = json.loads((review / 'issues.json').read_text(encoding='utf-8'))
        document['issues'] = [{**document['issues'][0], 'members': held}]
        _write(review / 'issues.json', document)

        run = read_run(review)

        assert run['status'] == 'complete', name
        assert run['publishable'] is False, name
        problem, = [line for line in run['problems'] if 'members' in line]
        assert said in problem and 'issues.json' in problem, (name, problem)
        assert any('named in `problems`' in reason
                   for reason in run['not_publishable_reasons']), run['not_publishable_reasons']
        # The issue itself is still reported: it is the evidence under it that
        # could not be read, and the counts say so rather than guessing.
        assert [issue['id'] for issue in run['issues']] == ['i01'], name
        assert run['counts']['issue_members'] == 0, name
        # And the review re-renders from that folder instead of raising.
        assert set(peerreviewagents.write_reports(review)) == {'markdown', 'text'}

    # The neighbouring fields of a row, and a row that is not an object at all.
    other = complete_run(tmp_path / 'neighbours')
    document = json.loads((other / 'issues.json').read_text(encoding='utf-8'))
    document['issues'] = [{**document['issues'][0], 'audits': 'stats'}, 'i02']
    _write(other / 'issues.json', document)

    run = read_run(other)

    assert run['publishable'] is False
    assert [issue['id'] for issue in run['issues']] == ['i01']
    for said in ('audits', 'not objects'):
        assert any(said in line for line in run['problems']), (said, run['problems'])
    assert set(peerreviewagents.write_reports(other)) == {'markdown', 'text'}


def test_the_last_readers_of_a_damaged_document_report_it_rather_than_raising(tmp_path):
    """Three readers still ended on a type, each on a path of its own.

    A fuzz pass over the readers found them: the member fallback `read_run`
    takes when the ledger is gone iterated ``members`` itself rather than
    through the shared guard, so a ``members`` of ``7`` raised a TypeError on
    the one path with nothing to fall back on; ``escalation_rulings`` of ``7``
    did the same beside it; and a member whose ``title`` was a number was
    concatenated into a TypeError out of ``write_reports``, the call that has to
    work on a damaged folder because it is how the folder gets read at all.

    What this pins is that each of them answers: the reader comes back and the
    rest of the run reads. A type ``field_problems`` does not name — the rulings
    here — is dropped rather than reported, which is the sweep that is not in
    this change.
    """
    # The ledger is gone, so the member rows are the only findings there are.
    review = complete_run(tmp_path / 'no-ledger')
    document = json.loads((review / 'issues.json').read_text(encoding='utf-8'))
    document['issues'] = [{**document['issues'][0], 'members': 7}]
    _write(review / 'issues.json', document)
    (review / 'findings.jsonl').unlink()

    run = read_run(review)

    assert run['findings'] == [] and run['publishable'] is False
    assert any('whose members is a int' in problem for problem in run['problems'])
    assert [issue['id'] for issue in run['issues']] == ['i01']

    # A ruling that is not a list, beside it.
    ruled = complete_run(tmp_path / 'rulings')
    _write(ruled / 'issues.json', {**document, 'issues': document['issues'],
                                   'escalation_rulings': 7})

    assert read_run(ruled)['escalations'] is not None

    # And a member the renderer prints, held as a number.
    titled = complete_run(tmp_path / 'title')
    document = json.loads((titled / 'issues.json').read_text(encoding='utf-8'))
    first = document['issues'][0]
    document['issues'] = [{**first, 'members': [{**first['members'][0], 'title': 7}]}]
    _write(titled / 'issues.json', document)

    assert set(peerreviewagents.write_reports(titled)) == {'markdown', 'text'}
    assert '7' in (titled / 'REVIEW.md').read_text(encoding='utf-8')

    # And the rows themselves, held as anything at all, through the renderer:
    # the guard every reader of this document shares.
    for held in (7, True, 1.5, 'see the suggestions'):
        folder = complete_run(tmp_path / f'rows-{held}')
        _write(folder / 'issues.json', {**document, 'issues': held})

        assert set(peerreviewagents.write_reports(folder)) == {'markdown', 'text'}
        assert read_run(folder)['publishable'] is False


def test_an_issues_document_stamped_with_an_unknown_version_is_refused_by_name(tmp_path):
    """A stamp is only worth writing if a reader checks it."""
    review = complete_run(tmp_path)
    document = json.loads((review / 'issues.json').read_text(encoding='utf-8'))
    _write(review / 'issues.json', {**document, 'schema_version': 'review-issues-v9'})

    run = read_run(review)

    assert run['issues'] == [] and run['publishable'] is False
    assert run['findings_source'] == 'ledger', 'the findings are still real'
    assert any('review-issues-v9' in problem for problem in run['problems']), run['problems']

    # No stamp at all is the same refusal with its own reason: a folder from
    # before the issues document existed, not a review with no issues in it.
    older = complete_run(tmp_path / 'older')
    stored = json.loads((older / 'issues.json').read_text(encoding='utf-8'))
    stored.pop('schema_version')
    _write(older / 'issues.json', stored)

    reading = read_run(older)

    assert reading['issues'] == [] and reading['publishable'] is False
    assert any('no schema_version' in problem for problem in reading['problems'])

    # And the reading is forward only: the one stamp this release writes is the
    # only one it reads. `review-issues-v2` was written and superseded inside
    # this release and never shipped in one, so reading it would be a
    # compatibility path for a file nobody has.
    assert issue_model.KNOWN_ISSUES_SCHEMAS == (issue_model.ISSUES_SCHEMA_VERSION,)
    superseded = complete_run(tmp_path / 'superseded')
    held = json.loads((superseded / 'issues.json').read_text(encoding='utf-8'))
    _write(superseded / 'issues.json', {**held, 'schema_version': 'review-issues-v2'})

    earlier = read_run(superseded)

    assert earlier['issues'] == [] and earlier['publishable'] is False
    assert any('review-issues-v2' in problem for problem in earlier['problems'])


def test_every_issue_row_carries_every_field_of_the_shape(tmp_path):
    """Absent or null was the consumer's problem either way; always-present is the rule."""
    review = complete_run(tmp_path)
    document = json.loads((review / 'issues.json').read_text(encoding='utf-8'))

    for row in document['issues']:
        assert sorted(row) == sorted(issue_model.ISSUE_FIELDS), row['id']
    for row in read_run(review)['issues']:
        assert sorted(row) == sorted(issue_model.ISSUE_FIELDS), row['id']

    # The no-editor path writes the same shape; a consumer reads one document.
    synthesized = issue_model.synthesize([
        {'finding_uid': 'aaa1', 'slot': 'core-1', 'track': 'core', 'severity': 'major',
         'kind': 'issue', 'title': 'A finding on its own', 'comment': 'Why it matters.'}])
    assert sorted(synthesized[0]) == sorted(issue_model.ISSUE_FIELDS)
    assert sorted(synthesized[0]['members'][0]) == sorted(issue_model.MEMBER_FIELDS)


def test_a_startup_blocked_run_records_the_tracks_the_arm_would_have_run(tmp_path):
    """``specialists: []`` for a full two-track run said the plan was something it was not."""
    review = tmp_path / 'run' / names.RUN_DIRECTORY
    review.mkdir(parents=True)

    starter.write_identity(review, tmp_path / 'paper.pdf', 'full-two-track', 3)

    assert read_run(review)['identity']['specialists'] == list(srv.SPECIALIST_TRACKS)

    draft = tmp_path / 'draft' / names.RUN_DIRECTORY
    draft.mkdir(parents=True)

    starter.write_identity(draft, tmp_path / 'paper.pdf', 'draft-check', 0)

    assert read_run(draft)['identity']['specialists'] == list(starter.DRAFT_CHECK_TRACKS)
    assert read_run(draft)['identity']['arm'] == 'draft-check'


def test_a_retry_in_the_same_folder_does_not_read_as_the_old_block(tmp_path, no_claude_cli):
    """Until the new run ends, the folder held the old run's cause and nothing said so."""
    review = killed_run(tmp_path)
    assert read_run(review)['cause'].startswith('core-1 passed')

    starter.supersede(review)

    again = read_run(review)
    assert (again['status'], again['cause']) == ('running', None)
    runtime = json.loads((review / 'runtime.json').read_text(encoding='utf-8'))
    assert 'core-1 passed' in runtime['superseded']['blocked_reason']

    # The same thing through `review`: a new run in a folder that already holds
    # a finished one reports its own reason, not the one before it.
    finished = complete_run(tmp_path / 'again')
    summary = starter.review(str(FIXTURE), str(finished.parent), arm='single')

    assert summary['stage'] == 'startup'
    second = read_run(finished)
    assert second['status'] == 'blocked'
    assert 'claude is not installed' in second['cause']


def test_the_readme_names_every_key_read_run_returns(tmp_path):
    """The documented key list is the whole key list, or an app plans around a gap."""
    readme = (Path(__file__).resolve().parent.parent / 'README.md').read_text(encoding='utf-8')

    for key in read_run(complete_run(tmp_path)):
        assert f'`{key}`' in readme, f'README.md does not name the {key} key'


# -- what a wrapper used to reach into private modules for -------------------


def test_the_installation_checks_are_public_and_are_the_ones_the_review_uses():
    """A wrapper checks the install through this surface, not `plugin.starter`."""
    assert peerreviewagents.plugin_directory is starter.plugin_dir
    assert peerreviewagents.claude_code_version is starter.claude_version

    plugin = peerreviewagents.plugin_directory()

    assert (plugin / '.claude-plugin' / 'plugin.json').is_file(), plugin
    # Either a version string or None, and nothing raised when Claude Code is
    # not installed: that is the whole contract a precondition check needs.
    version = peerreviewagents.claude_code_version()
    assert version is None or isinstance(version, str)


def test_the_three_parsers_and_the_command_list_are_public():
    """The real parsers, so a wrapper's documented options cannot drift from them."""
    assert (peerreviewagents.CLI_COMMANDS == names.CLI_COMMANDS
            == ('review', 'check-fixes', 'report'))
    # The CLI module keeps its historical name for the same tuple.
    from peerreviewagents.cli import main as cli_main

    assert cli_main.COMMANDS is names.CLI_COMMANDS
    assert peerreviewagents.review_parser is starter.build_parser
    assert peerreviewagents.report_parser is cli_main.report_parser
    assert peerreviewagents.check_fixes_parser is starter.check_fixes_parser

    review = peerreviewagents.review_parser(prog='wrapper review')
    report = peerreviewagents.report_parser(prog='wrapper report')
    check = peerreviewagents.check_fixes_parser(prog='wrapper check-fixes')

    assert review.prog == 'wrapper review' and report.prog == 'wrapper report'
    assert check.prog == 'wrapper check-fixes'
    options = {action.dest for action in review._actions}
    assert {'workspace', 'output', 'arm'} <= options, sorted(options)
    assert {'run', 'workspace', 'output_dir'} <= {a.dest for a in report._actions}
    # The check takes the problems and not the arm: there is one arm it runs,
    # and an `--arm` would be a way of asking it to run a review.
    checking = {action.dest for action in check._actions}
    assert {'workspace', 'output', 'problems'} <= checking, sorted(checking)
    assert 'arm' not in checking


def test_read_run_carries_the_facts_an_integration_read_from_the_folder(tmp_path):
    """The provider, the code digests and the start-up probe, without opening a file."""
    review = complete_run(tmp_path)
    _write(review / 'manifest.json', {**json.loads((review / 'manifest.json').read_text()),
                                      'code': {'peerreviewagents/runs.py': 'ab' * 32,
                                               'plugin/pra/agents/stats.md': 'cd' * 32}})
    _write(review / 'startup.json', {'probe': {'ok': True, 'pdf': str(tmp_path / 'paper.pdf')},
                                     'problems': [], 'tools_expected': ['pra:begin_review']})

    run = read_run(review)

    assert run['identity']['provider'] == 'claude-code'
    assert run['identity']['code_hashes'] == {'peerreviewagents/runs.py': 'ab' * 32,
                                              'plugin/pra/agents/stats.md': 'cd' * 32}
    assert run['startup'] == {'probe': {'ok': True, 'pdf': str(tmp_path / 'paper.pdf')},
                              'problems': []}
    assert run['problems'] == []


def test_the_three_new_facts_are_present_and_empty_when_the_files_do_not_hold_them(tmp_path):
    """A run that stopped early still answers; it answers None or empty."""
    run = read_run(startup_blocked_run(tmp_path))

    assert run['identity']['provider'] is None
    assert run['identity']['code_hashes'] == {}
    assert run['startup']['probe'] is None
    assert run['startup']['problems'] == ['the pra plugin was not loaded']


def test_a_damaged_provider_code_map_and_probe_are_named_in_problems(tmp_path):
    review = complete_run(tmp_path)
    _write(review / 'protocol.json', {**json.loads((review / 'protocol.json').read_text()),
                                      'provider': 7})
    _write(review / 'manifest.json', {**json.loads((review / 'manifest.json').read_text()),
                                      'code': {'runs.py': None, 'stats.md': 'ef' * 32}})
    _write(review / 'startup.json', {'probe': 'launched', 'problems': []})

    run = read_run(review)

    assert run['identity']['provider'] is None
    assert run['identity']['code_hashes'] == {'stats.md': 'ef' * 32}
    assert run['startup']['probe'] is None
    named = ' '.join(run['problems'])
    assert 'provider' in named and 'code map' in named and 'probe' in named, run['problems']


def test_locating_a_run_and_its_error_are_public(tmp_path):
    """An app that lists run folders no longer imports `runs` to resolve one."""
    assert peerreviewagents.locate_run is runs.locate
    assert peerreviewagents.NotARunFolder is runs.NotARunFolder

    review = complete_run(tmp_path)
    root, found = peerreviewagents.locate_run(review.parent)

    assert (root, found) == (review.parent.resolve(), review.resolve())
    assert peerreviewagents.locate_run(review)[1] == review.resolve()
    with pytest.raises(peerreviewagents.NotARunFolder):
        peerreviewagents.locate_run(tmp_path)


def test_the_workspace_helpers_are_the_engine_s_own_objects():
    """An app that writes what the engine reads must use the engine's own tools."""
    from peerreviewagents import workspace
    from peerreviewagents.engine import annotations, ingest, storage

    assert workspace.digest is storage.digest
    assert workspace.read_json is storage.read
    assert workspace.write_json is storage.write
    assert workspace.save_review is annotations.save_review
    assert workspace.normalize_review is annotations.normalize_review
    assert workspace.words is ingest.words
    assert workspace.text_of is ingest.text_of
    assert set(workspace.__all__) == {'digest', 'read_json', 'write_json', 'save_review',
                                      'normalize_review', 'words', 'text_of'}
    for name in workspace.__all__:
        assert callable(getattr(workspace, name)), name


def test_a_workspace_written_through_the_public_module_is_one_the_engine_verifies(tmp_path):
    """The round trip: a written workspace, and the review `verify_workspace` accepts.

    What comes back from the verification is the manuscript's base — the
    Markdown and the map over it — so the app that saves a review against this
    folder cites a line of a file, not an id of a block it never read.
    """
    from peerreviewagents import verify_workspace, workspace

    text = 'The cohort of 12 samples was 🧠 imaged twice.'
    root = tmp_path / 'workspace'
    base = build_base(root, [{'kind': 'paragraph', 'section': 'Methods', 'text': text}])

    verified = verify_workspace(root)
    assert verified.paper_id == 'paper-1'
    assert verified.line_text(1) == text
    assert workspace.read_json(root / 'map.json') == base.rows

    response = {'findings': [
        {'title': 'Name the unit', 'line': 1, 'quote': '12 samples',
         'kind': 'question', 'evidence_lines': [1], 'comment': 'donors or scans?'}]}
    saved = workspace.save_review(root, response, 'r1')

    assert saved['findings'][0]['placement'] == 'anchored'
    assert workspace.read_json(root / 'reviews' / 'r1.json') == saved
    # The same review again is the same file, not a second one and not an error.
    assert workspace.save_review(root, response, 'r1') == saved
    assert workspace.normalize_review(base, response, 'r2')['findings'][0]['id'] == 'r2-1'


def test_every_citation_a_consumer_reads_locates_the_quote_inside_its_line(tmp_path):
    """The offsets on a finding and on an issue member are the manuscript's own words.

    An app that highlights a quote knows the line and then has to find the
    words inside it — the search the engine already did, done again by code
    that cannot refuse. So every citation it reads carries the offsets too, and
    they all keep one rule::

        anchor_text == base.line_text(line, document)[start:end]

    for a quote inside one line, and from ``start`` in ``line`` to ``end`` in
    ``end_line`` for one that crosses a line break, which a quote spanning two
    table rows does. :func:`peerreviewagents.cited_text` is that read, and this
    holds it against the characters of the file on both paths a consumer has:
    the findings :func:`read_run` returns, and the members of an issue, which
    is all a run whose ledger is gone has left.
    """
    from peerreviewagents import cited_text, workspace

    base = build_base(tmp_path / 'workspace', [
        {'kind': 'paragraph', 'section': 'Results',
         'text': 'Recovery reached 86% in the treated group [1].'},
        {'kind': 'table', 'section': 'Results',
         'table': [['Group', 'Recovery'], ['treated', '86%'], ['controls', '71%']]}])
    anchored = workspace.normalize_review(base, {'findings': [
        {'title': 'Say what recovered', 'quote': 'Recovery reached 86%',
         'kind': 'question', 'comment': 'of what — animals, samples, both?'},
        {'title': 'The rows disagree with the text', 'kind': 'question',
         'quote': 'treated | 86% | | controls | 71%',
         'comment': 'the table reads the other way round'},
        {'title': 'Say what the table counts', 'kind': 'question',
         'quote': 'Group | Recovery | | --- | --- | | treated',
         'comment': 'the header says neither unit'}]}, 'r1')
    places = ('document', 'line', 'end_line', 'start', 'end', 'anchor_text', 'quote')
    raised = [_finding(uid, found['title'], **{key: found[key] for key in places})
              for uid, found in zip(('aaa1', 'bbb2'), anchored['findings'][:2])]
    # One quote inside a line, one across two of them: both cases, one rule.
    assert [(row['line'], row['end_line']) for row in raised] == [(1, 1), (5, 6)]
    # And the third crosses three lines, so the line between is covered whole.
    three = anchored['findings'][2]
    assert (three['line'], three['end_line']) == (3, 5)
    assert cited_text(base, three) == three['anchor_text'] == (
        '| --- | --- |'.join(['Group | Recovery |\n', '\n| treated']))
    # A finding the guard could not place has words and no position: nothing is
    # the honest answer, and the top of the paper is not.
    assert cited_text(base, {'document': 'paper.md', 'line': None, 'end_line': None,
                             'start': None, 'end': None}) == ''

    review = complete_run(tmp_path)
    _rows(review / 'findings.jsonl', raised)
    document = json.loads((review / 'issues.json').read_text(encoding='utf-8'))
    for issue, finding in zip(document['issues'], raised):
        issue['members'] = [issue_model.member_of(finding)]
    _write(review / 'issues.json', document)

    run = read_run(review)
    cited = [*run['findings'], *(m for issue in run['issues'] for m in issue['members'])]
    assert len(cited) == 4
    for row in cited:
        assert cited_text(base, row) == row['anchor_text']
        if row['line'] == row['end_line']:  # The rule as INTEGRATIONS.md states it.
            text = base.line_text(row['line'], row['document'])
            assert text[row['start']:row['end']] == row['anchor_text']

    # A run whose ledger is gone is read from the members, and it may not lose
    # the offsets there: a consumer that underlines a finding is handed the
    # same numbers on that path as on the other.
    (review / 'findings.jsonl').unlink()
    without = read_run(review)

    assert without['findings_source'] == 'issues'
    assert [row['anchor_text'] for row in without['findings']] == [row['anchor_text']
                                                                   for row in raised]
    assert [cited_text(base, row) for row in without['findings']] == [row['anchor_text']
                                                                      for row in raised]


def test_citation_uses_is_the_anchor_rule_an_app_would_otherwise_rewrite(tmp_path):
    """The rows an app builds its own ledger from, built by the engine itself.

    ``cited_text`` reads an anchor back and ``docs/INTEGRATIONS.md`` documents
    a use row's fields, so everything about a row was public except the rule
    that *makes* one: where a sentence ends, how one that crosses a line break
    is spanned, and that ``row_id`` carries the piece's first line rather than
    the sentence's. This holds the public call against the engine's own writer
    and against the characters of the file.
    """
    from peerreviewagents import citation_uses, cited_text

    base = build_base(tmp_path / 'workspace', [
        {'kind': 'heading', 'text': 'Introduction', 'level': 1, 'section': 'Introduction'},
        {'kind': 'paragraph', 'section': 'Introduction',
         'text': 'The assay is standard [1]. We repeated it in three cohorts [2].'}])
    references = [{'id': 'r1', 'label': '1', 'raw': 'A. Author. An assay. 2019.'},
                  {'id': 'r2', 'label': '2', 'raw': 'B. Author. Three cohorts. 2021.'}]

    rows = citation_uses(base, references)

    # The engine's own rows, by the same call: an app and the ledger cannot
    # disagree about which sentence a row is. `filled_at` is the only field
    # that is the moment rather than the manuscript.
    def anchored(row):
        return {key: value for key, value in row.items() if key != 'filled_at'}

    assert [anchored(row) for row in rows] == [anchored(row) for row in
                                               citation_ledger.use_rows(base, references)]
    assert [row['reference_id'] for row in rows] == ['r1', 'r2']
    assert [row['sentence_index'] for row in rows] == [1, 2]
    # Both sentences come off one piece, so both ids carry that piece's first
    # line — the part a second implementation takes from the sentence instead.
    assert [row['row_id'] for row in rows] == ['use:paper.md:3:1:r1', 'use:paper.md:3:2:r2']
    assert {row['piece_lines'][0] for row in rows} == {3}
    for row in rows:
        assert cited_text(base, row) == row['sentence']
        assert set(row) >= {'kind', 'row_id', 'reference_id', 'document', 'line', 'end_line',
                            'start', 'end', 'piece_lines', 'section', 'sentence_index',
                            'sentence', 'marker', 'derivation', 'stage'}

    # A reference list it has none of is not an error: the bracketed markers
    # the paper prints are still uses, and unmatched is what they are.
    unmatched = citation_uses(base, [])
    assert [row['marker'] for row in unmatched] == ['[1]', '[2]']
    assert all(row['reference_id'] is None for row in unmatched)


def test_the_two_text_helpers_read_text_the_way_the_import_does():
    import xml.etree.ElementTree as ET

    from peerreviewagents import workspace

    element = ET.fromstring('<p>The cohort of  12\n samples <i>was</i> imaged.</p>')

    assert workspace.text_of(element) == 'The cohort of 12 samples was imaged.'
    assert workspace.text_of(None) == ''
    assert workspace.words("12 samples, don't re-count") == ['12', 'samples', "don't", 're-count']


def test_one_agent_that_ran_two_tasks_is_one_row(tmp_path):
    """The recorder ingests and later writes the review; that is one agent."""
    review = complete_run(tmp_path)
    _write(review / 'protocol.json', {
        **json.loads((review / 'protocol.json').read_text()),
        'agents': {'recorder': {'role': 'recorder', 'track': 'ingest', 'tool_calls': 4,
                                'model': None}}})
    _write(review / 'runtime.json', {
        **json.loads((review / 'runtime.json').read_text()),
        'agents': [{'label': 'ingest', 'agentType': 'pra:recorder', 'tokens': 12000,
                    'toolCalls': 5, 'state': 'completed', 'model': 'claude-sonnet-5-5',
                    'tokens_cumulative': 12000, 'tokens_peak_context': 9000,
                    'ending': 'answered'},
                   {'label': 'write review', 'agentType': 'pra:recorder', 'tokens': 30000,
                    'toolCalls': 7, 'state': 'completed', 'model': 'claude-sonnet-5-5',
                    'tokens_cumulative': 30000, 'tokens_peak_context': 21000,
                    'ending': 'answered'},
                   {'label': 'spec-field'}]})

    rows = read_run(review)['agents']

    assert [row['slot'] for row in rows] == ['recorder'], rows
    assert rows[0]['tokens'] == 42000 and rows[0]['session_calls'] == 12
    assert rows[0]['tool_calls'] == 4  # the server's own count, untouched
    assert rows[0]['labels'] == ['ingest', 'write review']
    assert rows[0]['model'] == 'claude-sonnet-5-5'
    assert rows[0]['tokens_cumulative'] == 42000, 'two tasks, one spend'
    assert rows[0][names.TOKEN_PEAK] == 21000, \
        'the widest single call of either task, not the two added up'
    assert rows[0]['ending'] == 'answered'
