"""The driver's input check, which exists because a bad input cost a real run.

One control candidate in the Stage A corpus was PMC's "Preparing to download"
HTML page saved under a `.pdf` name. The engine cannot ingest it, so the review
came back `blocked` after a few thousand tokens and stopped the matrix. The
driver now refuses a queue holding such a file before it starts any session, so
the failure is a message about a file to fix rather than a wasted run.
"""

from __future__ import annotations

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


def test_a_dry_run_flags_the_bad_input_instead_of_refusing(tmp_path, monkeypatch, capsys):
    """`--dry-run` is for looking at the queue, so it prints the problem."""
    queue = specs(write(tmp_path, 'good.pdf', b'%PDF-1.4\n'),
                  write(tmp_path, 'bad.pdf', HTML))
    monkeypatch.setattr(matrix, 'jobs', lambda *args, **kwargs: queue)
    state = matrix.drive(tmp_path, dry_run=True)
    printed = capsys.readouterr().out
    assert 'NOT A PDF' in printed and printed.count('NOT A PDF') == 1
    assert state['stopped_because'] == ''
