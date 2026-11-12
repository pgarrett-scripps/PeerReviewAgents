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
from pathlib import Path

import pytest

import peerreviewagents
from peerreviewagents import names
from peerreviewagents.cli.main import report_main
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
            'severity': 'major', 'kind': 'question', 'block_id': 'b0003',
            'quote': 'the reported total', 'escalate': False,
            'schema_version': names.FINDING_SCHEMA_VERSION, **extra}


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
                                      'findings_delivered': 3 if findings else 0,
                                      'findings_submitted': 4 if findings else 0,
                                      'stages': {'required': ['core-1'], 'missing': []}})
    _write(review / 'runtime.json', {
        'status': status, 'blocked_reason': None, 'wall_seconds': 247.0,
        'tokens_total': 636299, 'claude_code_version': '2.1.322',
        'result': {'subtype': 'success', 'is_error': False},
        'agents': [{'label': 'core-1', 'tokens': 400000, 'toolCalls': 24, 'state': 'completed',
                    'model': 'claude-sonnet-5-5'},
                   {'label': 'spec-stats', 'tokens': 236299, 'toolCalls': 9,
                    'state': 'completed', 'model': 'claude-sonnet-5-5'}]})
    body = [_finding('aaa1', 'The total does not follow from its parts'),
            _finding('bbb2', 'The stated n disagrees with Table 2', slot='spec-stats',
                     track='stats', escalate=True)] if findings else []
    _write(review / 'editor-package.json', {
        'summary': 'Two issues in the reported totals.',
        'findings': body,
        'appendix': ([_finding('ccc3', 'A duplicate of the first')] if findings else []),
        'escalation_rulings': ([{'finding_uid': 'bbb2', 'ruling': 'upheld'}] if findings else []),
        'limitations': ['No access to the raw data.'],
        'verdict': {'significance': 'moderate'},
        'findings_by_track': {'core': 1, 'stats': 1},
    })
    _rows(review / 'findings.jsonl', body)
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
    assert run['findings_source'] == 'editor-package'
    assert [f['finding_uid'] for f in run['body']] == ['aaa1', 'bbb2']
    assert [f['finding_uid'] for f in run['appendix']] == ['ccc3']
    assert run['counts'] == {'findings': 3, 'body': 2, 'appendix': 1, 'quarantined': 0,
                             'findings_delivered': 3, 'findings_submitted': 4}
    assert run['identity']['source_sha256'] == 'cafe' * 16
    assert run['identity']['arm'] == 'full-two-track'
    assert run['runtime']['tokens_total'] == 636299
    assert run['reports']['audits'] == {'stats': str(review / 'audit_stats.md')}
    assert run['reports']['markdown'] == str(review / 'REVIEW.md')


def test_read_run_reports_the_runtime_of_each_agent(tmp_path):
    review = complete_run(tmp_path)

    rows = {row['slot']: row for row in read_run(review)['agents']}

    assert rows['core-1']['tokens'] == 400000
    assert rows['core-1']['tool_calls'] == 11  # what the server served it
    assert rows['core-1']['session_calls'] == 24  # what the session saw it do
    assert rows['spec-stats']['track'] == 'stats'
    assert rows['spec-stats']['model'] == 'claude-sonnet-5-5'


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
    assert [f['finding_uid'] for f in run['body']] == ['aaa1']
    assert run['appendix'] == []


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
    elsewhere = _write(tmp_path / 'elsewhere' / 'editor-package.json',
                       {'summary': 'not this run', 'findings': []})
    (review / 'editor-package.json').unlink()
    (review / 'editor-package.json').symlink_to(elsewhere)

    with pytest.raises(ValueError, match='editor-package.json'):
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


# -- the public names -------------------------------------------------------


def test_the_top_level_exports_every_supported_name():
    for name in peerreviewagents.__all__:
        assert getattr(peerreviewagents, name) is not None, name
    assert set(peerreviewagents.__all__) <= set(dir(peerreviewagents))


def test_the_public_constants_are_what_the_server_writes():
    assert peerreviewagents.RUN_DIRECTORY == srv.RUN_DIRECTORY == 'review'
    assert peerreviewagents.MANUSCRIPT_DIRECTORY == srv.MANUSCRIPT_DIRECTORY == 'manuscript'
    assert peerreviewagents.FINDING_SCHEMA_VERSION == srv.FINDING_SCHEMA_VERSION
    assert peerreviewagents.RUN_STATUSES == ('running', 'blocked', 'complete',
                                             'complete-without-findings')
    assert peerreviewagents.COMPLETE_STATUSES == ('complete', 'complete-without-findings')


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


def test_export_draft_no_longer_emits_the_debate_keys():
    paper = {'id': 'p1', 'source_sha256': 'abc', 'blocks': [{'id': 'b1', 'text': 'A sentence.',
                                                             'sha256': 'x'}]}
    review = {'id': 'r1', 'paper_id': 'p1', 'findings': [], 'limitations': [],
              'raw_response': {'component_decisions': [{'kept': True}],
                               'epistemic_audit': {'checked': True}}}

    exported = peerreviewagents.export_draft(paper, review, {'paper_id': 'p1', 'items': {}})

    assert 'component_decisions' not in exported
    assert 'epistemic_audit' not in exported
    assert exported['proposed_blocks'] == paper['blocks']
