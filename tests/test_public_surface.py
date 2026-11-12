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
from peerreviewagents import names, runs
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
                                      'review_guard_status': 'clean',
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
        'review_guard_status': 'clean',
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
    try:
        (review / 'editor-package.json').symlink_to(elsewhere)
    except OSError as exc:  # Windows without the privilege. The guard is still there.
        pytest.skip(f'this machine cannot create a symlink to test against: {exc}')

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
    # `names` is where they are defined; the server imports them from there, so
    # the top level and the writer cannot drift apart.
    assert peerreviewagents.RUN_DIRECTORY == names.RUN_DIRECTORY == 'review'
    assert peerreviewagents.MANUSCRIPT_DIRECTORY == names.MANUSCRIPT_DIRECTORY == 'manuscript'
    assert peerreviewagents.FINDING_SCHEMA_VERSION == names.FINDING_SCHEMA_VERSION
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

    assert run['counts']['findings'] == 3, 'the editor package was whole and was read'
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
    elsewhere = _write(tmp_path / 'elsewhere' / 'editor-package.json', {'findings': []})
    (review / 'editor-package.json').unlink()
    try:
        (review / 'editor-package.json').symlink_to(elsewhere)
    except OSError as exc:  # Windows without the privilege. The guard is still there.
        pytest.skip(f'this machine cannot create a symlink to test against: {exc}')

    assert report_main([str(review)]) == 1
    assert 'symlink' in capsys.readouterr().err


def test_an_unknown_status_is_unknown_and_publishable_says_what_is_showable(tmp_path):
    """``complete`` answers whether the run finished; ``publishable`` whether to show it."""
    assert read_run(complete_run(tmp_path / 'clean'))['publishable'] is True

    # A review with no findings in it, and nothing quarantined: the reviewers
    # read the manuscript and raised nothing. "Nothing make-or-break" is a
    # review, and the editor wrote a package for it, so it is publishable.
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
    package = json.loads((guarded / 'editor-package.json').read_text(encoding='utf-8'))
    _write(guarded / 'editor-package.json', {**package, 'review_guard_status': 'quarantined'})
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
    unanchored quote, a truncated editor package and a killed run are three
    different events, and one of them is not a reason to hold the review at all.
    """
    whole = read_run(complete_run(tmp_path / 'whole'))
    assert (whole['publishable'], whole['not_publishable_reasons']) == (True, [])

    for name, prepare, expected in (
        ('ledger', lambda review: (review / 'editor-package.json').write_text(
            '{"summary": "truncated', encoding='utf-8'), 'ledger'),
        ('killed', lambda review: _write(review / 'runtime.json', {
            'status': 'blocked', 'blocked_reason': 'per-run token ceiling', 'agents': []}), None),
        ('no-guard', lambda review: [_write(review / name, _without_guard(review / name))
                                     for name in ('editor-package.json', 'progress.json')],
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


def test_a_uid_in_the_body_and_the_appendix_is_one_finding(tmp_path):
    """The editor placing a finding twice is a placement, not two findings."""
    review = complete_run(tmp_path)
    package = json.loads((review / 'editor-package.json').read_text(encoding='utf-8'))
    _write(review / 'editor-package.json', {**package, 'appendix': package['findings']})

    run = read_run(review)

    assert [f['finding_uid'] for f in run['body']] == ['aaa1', 'bbb2']
    assert run['appendix'] == []
    assert run['counts'] == {'findings': 2, 'body': 2, 'appendix': 0, 'quarantined': 0,
                             'findings_delivered': 3, 'findings_submitted': 4}
    assert [f['finding_uid'] for f in run['escalations']] == ['bbb2']


def test_a_damaged_editor_package_on_a_complete_run_is_not_a_whole_review(tmp_path):
    """The ledger fallback is real findings, and it is not the editor's review."""
    review = complete_run(tmp_path)
    (review / 'editor-package.json').write_text('{"summary": "Two issues', encoding='utf-8')

    run = read_run(review)

    assert (run['status'], run['complete']) == ('complete', True)
    assert run['findings_source'] == 'ledger'
    assert run['publishable'] is False
    assert any('editor-package.json' in problem for problem in run['problems'])

    # Readable JSON holding the wrong shape is the same thing: a package this
    # version cannot read, not a review with nothing in its body.
    other = complete_run(tmp_path / 'wrong-shape')
    package = json.loads((other / 'editor-package.json').read_text(encoding='utf-8'))
    _write(other / 'editor-package.json', {**package, 'findings': 'see the appendix'})

    shaped = read_run(other)

    assert shaped['body'] == [] and shaped['publishable'] is False
    assert any("findings is not a list" in problem for problem in shaped['problems'])


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
    assert 'probe failed' in second['cause']


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


def test_the_two_parsers_and_the_command_list_are_public():
    """The real parsers, so a wrapper's documented options cannot drift from them."""
    assert peerreviewagents.CLI_COMMANDS == names.CLI_COMMANDS == ('review', 'report')
    # The CLI module keeps its historical name for the same tuple.
    from peerreviewagents.cli import main as cli_main

    assert cli_main.COMMANDS is names.CLI_COMMANDS
    assert peerreviewagents.review_parser is starter.build_parser
    assert peerreviewagents.report_parser is cli_main.report_parser

    review = peerreviewagents.review_parser(prog='wrapper review')
    report = peerreviewagents.report_parser(prog='wrapper report')

    assert review.prog == 'wrapper review' and report.prog == 'wrapper report'
    options = {action.dest for action in review._actions}
    assert {'workspace', 'output', 'arm'} <= options, sorted(options)
    assert {'run', 'workspace', 'output_dir'} <= {a.dest for a in report._actions}


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
    """The round trip: write with these names, and `verify_workspace` accepts it."""
    from peerreviewagents import verify_workspace, workspace

    text = 'The cohort of 12 samples was 🧠 imaged twice.'
    paper = {'id': 'paper-1', 'source_sha256': 'ab' * 32,
             'blocks': [{'id': 'b1', 'text': text, 'sha256': workspace.digest(text)}]}
    root = tmp_path / 'workspace'
    workspace.write_json(root / 'paper.json', paper)
    workspace.write_json(root / 'manifest.json', {
        'files': {'paper.json': workspace.digest((root / 'paper.json').read_bytes())}})

    assert verify_workspace(root)['id'] == 'paper-1'
    assert workspace.read_json(root / 'paper.json') == paper

    response = {'findings': [
        {'title': 'Name the unit', 'block_id': 'b1', 'quote': '12 samples',
         'kind': 'question', 'evidence_blocks': ['b1'], 'comment': 'donors or scans?'}]}
    saved = workspace.save_review(root, response, 'r1')

    assert saved['findings'][0]['placement'] == 'anchored'
    assert workspace.read_json(root / 'reviews' / 'r1.json') == saved
    # The same review again is the same file, not a second one and not an error.
    assert workspace.save_review(root, response, 'r1') == saved
    assert workspace.normalize_review(paper, response, 'r2')['findings'][0]['id'] == 'r2-1'


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
                    'toolCalls': 5, 'state': 'completed', 'model': 'claude-sonnet-5-5'},
                   {'label': 'write review', 'agentType': 'pra:recorder', 'tokens': 30000,
                    'toolCalls': 7, 'state': 'completed', 'model': 'claude-sonnet-5-5'},
                   {'label': 'spec-field'}]})

    rows = read_run(review)['agents']

    assert [row['slot'] for row in rows] == ['recorder'], rows
    assert rows[0]['tokens'] == 42000 and rows[0]['session_calls'] == 12
    assert rows[0]['tool_calls'] == 4  # the server's own count, untouched
    assert rows[0]['labels'] == ['ingest', 'write review']
    assert rows[0]['model'] == 'claude-sonnet-5-5'
