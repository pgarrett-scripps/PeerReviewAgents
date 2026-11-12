"""The driver's input check, which exists because a bad input cost a real run.

One control candidate in the Stage A corpus was PMC's "Preparing to download"
HTML page saved under a `.pdf` name. The engine cannot ingest it, so the review
came back `blocked` after a few thousand tokens and stopped the matrix. The
driver now refuses a queue holding such a file before it starts any session, so
the failure is a message about a file to fix rather than a wasted run.
"""

from __future__ import annotations

import json

import matrix

HTML = (b'<html>\n  <head>\n    <title>Preparing to download...</title>\n'
        b'  </head>\n</html>\n')


def write(folder, name, data):
    path = folder / name
    path.write_bytes(data)
    return path


def specs(*paths):
    """Job specs of the shape `jobs()` returns, as far as the check reads them."""
    return [{'id': f'paper{index}--statistic--full-two-track--r3', 'pdf': str(path),
             'stage': 'main', 'paper': f'paper{index}', 'kind': 'statistic',
             'arm': 'full-two-track', 'reviewers': 3, 'key': '',
             'run': str(path.parent / 'runs' / f'paper{index}'),
             'log': str(path.parent / 'logs' / f'paper{index}.log')}
            for index, path in enumerate(paths)]


def test_a_pdf_is_a_pdf(tmp_path):
    assert matrix.is_pdf(write(tmp_path, 'real.pdf', b'%PDF-1.7\n1 0 obj\n'))


def test_a_saved_html_page_is_not_a_pdf(tmp_path):
    assert not matrix.is_pdf(write(tmp_path, 'fake.pdf', HTML))


def test_an_unreadable_file_is_not_a_pdf(tmp_path):
    """A missing or unreadable file is refused rather than raising."""
    assert not matrix.is_pdf(tmp_path / 'absent.pdf')
    assert not matrix.is_pdf(tmp_path)


def test_a_file_shorter_than_the_header_is_not_a_pdf(tmp_path):
    assert not matrix.is_pdf(write(tmp_path, 'stub.pdf', b'%PD'))


def test_unusable_names_only_the_bad_inputs(tmp_path):
    good = write(tmp_path, 'good.pdf', b'%PDF-1.4\n')
    bad = write(tmp_path, 'bad.pdf', HTML)
    refused = matrix.unusable(specs(good, bad))
    assert [name for name, _ in refused] == ['paper1--statistic--full-two-track--r3']
    assert [path for _, path in refused] == [str(bad)]


def test_drive_refuses_the_queue_and_runs_nothing(tmp_path, monkeypatch, capsys):
    """One bad input stops the whole queue, before any session is started."""
    queue = specs(write(tmp_path, 'good.pdf', b'%PDF-1.4\n'),
                  write(tmp_path, 'bad.pdf', HTML))
    monkeypatch.setattr(matrix, 'jobs', lambda *args, **kwargs: queue)
    monkeypatch.setattr(matrix, 'execute', lambda *args, **kwargs: 1 / 0)
    state = matrix.drive(tmp_path, peerreview='/bin/false')
    assert state['ran'] == 0 and state['skipped'] == 0 and state['tokens'] == 0
    assert 'not PDFs' in state['stopped_because']
    printed = capsys.readouterr().out
    assert 'refused paper1--statistic--full-two-track--r3' in printed
    assert 'does not start with %PDF' in printed


def test_drive_runs_a_queue_whose_inputs_are_all_pdfs(tmp_path, monkeypatch):
    """The check is not in the way: a clean queue reaches `execute`."""
    queue = specs(write(tmp_path, 'good.pdf', b'%PDF-1.4\n'))
    seen = []
    monkeypatch.setattr(matrix, 'jobs', lambda *args, **kwargs: queue)
    monkeypatch.setattr(matrix, 'execute',
                        lambda spec, *args, **kwargs: seen.append(spec['id']) or 0)
    monkeypatch.setattr(matrix, 'result',
                        lambda spec, exit_code: {'id': spec['id'], 'kind': spec['kind'],
                                                 'status': 'complete', 'tokens': 7,
                                                 'findings_total': 0, 'minutes': 0.1,
                                                 'caught': False, 'guard_status': 'clean'})
    monkeypatch.setattr(matrix, 'record', lambda root, row: None)
    state = matrix.drive(tmp_path, peerreview='/bin/false')
    assert seen == ['paper0--statistic--full-two-track--r3']
    assert state['ran'] == 1 and state['stopped_because'] == ''


def test_the_reviewers_can_be_pinned_to_one_model_so_the_arms_compare(tmp_path, monkeypatch):
    """Stage A's earlier results ran every reviewer on one model, and a rerun has to.

    The default is now one model per reviewer, so an arm that is being compared
    against a recorded result pins them, and the row says which models the run
    asked for rather than leaving the two arms to be pooled.
    """
    queue = specs(write(tmp_path, 'good.pdf', b'%PDF-1.4\n'))
    commands = []
    monkeypatch.setattr(matrix, 'jobs', lambda *args, **kwargs: queue)
    monkeypatch.setattr(matrix.subprocess, 'run',
                        lambda command, **kwargs: commands.append(command) or Finished(0))
    monkeypatch.setattr(matrix, 'result',
                        lambda spec, exit_code: {'id': spec['id'], 'kind': spec['kind'],
                                                 'status': 'complete', 'tokens': 7,
                                                 'findings_total': 0, 'minutes': 0.1,
                                                 'caught': False, 'guard_status': 'clean'})
    monkeypatch.setattr(matrix, 'record', lambda root, row: None)
    state = matrix.drive(tmp_path, peerreview='/bin/false', reviewer_models='sonnet')
    assert state['reviewer_models'] == 'sonnet'
    assert commands[0][commands[0].index('--reviewer-models') + 1] == 'sonnet'

    commands.clear()
    matrix.drive(tmp_path, peerreview='/bin/false')
    assert '--reviewer-models' not in commands[0], 'unset leaves the review its own default'


class Finished:
    """What ``subprocess.run`` returns, as far as ``execute`` reads it."""

    def __init__(self, returncode):
        self.returncode = returncode


def test_the_pin_is_off_by_default_on_the_command_line():
    assert matrix.build_parser().parse_args([]).reviewer_models == ''
    pinned = matrix.build_parser().parse_args(['--reviewer-models', 'sonnet,opus'])
    assert pinned.reviewer_models == 'sonnet,opus'


def planted_root(tmp_path, *slugs):
    """A Stage A data root holding a key and an altered PDF per slug."""
    (tmp_path / 'keys').mkdir(exist_ok=True)
    (tmp_path / 'altered').mkdir(exist_ok=True)
    for slug in slugs:
        pdf = write(tmp_path / 'altered', f'{slug}.pdf', b'%PDF-1.4\n')
        (tmp_path / 'keys' / f'{slug}.json').write_text(
            json.dumps({'paper': slug, 'kind': 'statistic', 'altered_pdf': str(pdf)}),
            encoding='utf-8')
    return tmp_path


def test_a_rerun_goes_to_a_folder_of_its_own(tmp_path):
    """The run being compared against must still be there afterwards."""
    root = planted_root(tmp_path, 'trial-pmc8938265', 'psychology-pmc7609822')
    specs = matrix.reruns(root, ('trial-pmc8938265',), 'briefs-fixed')
    assert [spec['id'] for spec in specs] == [
        'trial-pmc8938265--statistic--full-two-track--r3--briefs-fixed']
    assert specs[0]['stage'] == 'rerun' and specs[0]['arm'] == 'full-two-track'
    assert specs[0]['reviewers'] == 3 and specs[0]['tag'] == 'briefs-fixed'
    first = str(root / 'runs' / 'trial-pmc8938265--statistic--full-two-track--r3')
    assert specs[0]['run'] != first and specs[0]['run'].startswith(first)


def test_a_rerun_of_a_paper_that_is_not_planted_is_refused(tmp_path):
    root = planted_root(tmp_path, 'trial-pmc8938265')
    try:
        matrix.reruns(root, ('trial-pmc8938265', 'not-a-paper'), 'briefs-fixed')
    except SystemExit as stop:
        assert 'not-a-paper' in str(stop)
    else:
        raise AssertionError('a slug that names no planted paper should stop the run')


def test_a_pinned_model_names_itself_in_every_job_id(tmp_path):
    """Two arms under different models must never share a run folder.

    `already_done` skips a finished folder, so a `main` run under pinned models
    landing in the recorded run's folder would be read as already done and the
    pinned arm would never run at all. The suffix is what keeps them apart, and
    with nothing pinned the ids are byte-identical to the recorded ones.
    """
    root = planted_root(tmp_path, 'trial-pmc8938265')
    plain = matrix.jobs(root, ('pilot', 'reviewers1'), (), ())
    pinned = matrix.jobs(root, ('pilot', 'reviewers1'), (), (),
                         reviewer_models='claude-sonnet-5-5,opus')
    tail = '--sonnet-opus'
    assert all(spec['id'].endswith(tail) for spec in pinned)
    assert all(spec['tag'] == 'sonnet-opus' for spec in pinned)
    assert [spec['id'] for spec in plain] == [spec['id'][:-len(tail)] for spec in pinned]
    assert all('tag' not in spec for spec in plain), 'an unpinned run is the plain matrix'
    assert [spec['run'] for spec in plain] != [spec['run'] for spec in pinned]

    tagged = matrix.jobs(root, ('pilot',), (), (), tag='briefs-fixed',
                         reviewer_models='sonnet')
    assert all(spec['id'].endswith('--briefs-fixed') for spec in tagged), '--tag wins'


def test_the_rerun_ids_already_on_disk_do_not_move(tmp_path):
    """`rerun` is the one stage that had a suffix before the models did."""
    assert matrix.id_suffix('rerun') == 'rerun'
    assert matrix.id_suffix('rerun', reviewer_models='haiku') == 'haiku'
    assert matrix.id_suffix('rerun', 'briefs-fixed', 'haiku') == 'briefs-fixed'
    assert matrix.id_suffix('main') == ''
    assert matrix.id_suffix('main', reviewer_models='claude-haiku-5-5') == 'haiku'
    assert matrix.models_slug('') == '' and matrix.models_slug('  ,  ') == ''


def test_reruns_are_not_in_the_default_stages(tmp_path):
    """A plain matrix run must never start a rerun of anything."""
    root = planted_root(tmp_path, 'trial-pmc8938265')
    assert matrix.jobs(root, matrix.STAGES, (), (), 'rerun')
    default = matrix.build_parser().parse_args([])
    assert default.stage is None and default.rerun_paper == []
    assert 'rerun' in matrix.STAGES


def test_a_rescore_keeps_a_row_for_a_stage_it_was_not_asked_about(tmp_path, monkeypatch):
    """Rescoring rewrites the results file, so it must rebuild every row.

    A rescore of the matrix must not delete the rerun rows, and a rescore of
    the reruns must not delete the matrix.
    """
    root = planted_root(tmp_path, 'trial-pmc8938265')
    (root / 'runs').mkdir(exist_ok=True)
    kept = {'id': 'trial-pmc8938265--statistic--full-two-track--r3--briefs-fixed',
            'stage': 'rerun', 'paper': 'trial-pmc8938265', 'kind': 'statistic',
            'arm': 'full-two-track', 'reviewers': 3, 'tokens': 11,
            'run': str(root / 'runs' / 'kept-run')}
    (root / 'runs' / 'results.jsonl').write_text(json.dumps(kept) + '\n', encoding='utf-8')
    monkeypatch.setattr(matrix, 'already_done', lambda run: True)
    monkeypatch.setattr(matrix, 'result',
                        lambda spec, exit_code: {'id': spec['id'], 'kind': spec['kind'],
                                                 'stage': spec['stage'], 'status': 'complete',
                                                 'tokens': 11, 'findings_total': 0,
                                                 'minutes': 0.1, 'caught': False,
                                                 'guard_status': 'clean'})
    matrix.drive(root, stages=('main',), rescore_only=True)
    written = [json.loads(text) for text
               in (root / 'runs' / 'results.jsonl').read_text(encoding='utf-8').splitlines()
               if text.strip()]
    assert kept['id'] in [row['id'] for row in written]


def test_a_dry_run_flags_the_bad_input_instead_of_refusing(tmp_path, monkeypatch, capsys):
    """`--dry-run` is for looking at the queue, so it prints the problem."""
    queue = specs(write(tmp_path, 'good.pdf', b'%PDF-1.4\n'),
                  write(tmp_path, 'bad.pdf', HTML))
    monkeypatch.setattr(matrix, 'jobs', lambda *args, **kwargs: queue)
    state = matrix.drive(tmp_path, dry_run=True)
    printed = capsys.readouterr().out
    assert 'NOT A PDF' in printed and printed.count('NOT A PDF') == 1
    assert state['stopped_because'] == ''
