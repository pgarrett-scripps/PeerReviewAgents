"""``peerreview`` has two commands after 0.9, and one of them runs the plugin.

Every other command the engine used to offer — ``import``, ``verify``,
``investigate``, ``checks``, ``add-review``, ``result-map`` and the ``serve``
web UI — is gone with the orchestration it drove, and so is the ``--arm``
discriminator that told the plugin route apart from the engine's own ``review``
while both existed. These tests pin the surface that is left: a review goes to
the plugin and nowhere else, a run already on disk can be re-rendered offline,
and a command that no longer exists fails instead of being quietly ignored.
"""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path

import pytest

from peerreviewagents.cli import main as cli
from peerreviewagents.plugin import starter
from peerreviewagents.plugin.server import DEFAULT_ARM

PDF = b"%PDF-1.7\nnot a real manuscript\n"


@pytest.fixture
def started(monkeypatch):
    """Capture the call the CLI makes into the plugin starter, without a session."""
    seen: dict = {}

    def fake_review(pdf, out, **options):
        seen['pdf'], seen['out'], seen['options'] = pdf, out, options
        return {'status': 'complete', 'stage': 'session'}

    monkeypatch.setattr(starter, 'review', fake_review)
    return seen


def _pdf(tmp_path, name='paper.pdf') -> Path:
    path = tmp_path / name
    path.write_bytes(PDF)
    return path


def _run(argv: list[str], monkeypatch) -> int | str | None:
    monkeypatch.setattr('sys.argv', ['peerreview', *argv])
    with contextlib.redirect_stdout(io.StringIO()):
        with pytest.raises(SystemExit) as exit_info:
            cli.run()
    return exit_info.value.code


def _fails(argv: list[str], monkeypatch) -> str:
    monkeypatch.setattr('sys.argv', ['peerreview', *argv])
    with pytest.raises(SystemExit) as exit_info:
        with contextlib.redirect_stdout(io.StringIO()):
            with contextlib.redirect_stderr(io.StringIO()) as err:
                cli.run()
    assert exit_info.value.code not in (0, None), argv
    return err.getvalue() + str(exit_info.value.code)


# -- the review -------------------------------------------------------------


def test_a_review_goes_to_the_plugin_with_the_full_two_track_arm(tmp_path, monkeypatch, started):
    manuscript = _pdf(tmp_path)

    assert _run(['review', str(manuscript), str(tmp_path / 'run')], monkeypatch) == 0

    assert started['pdf'] == str(manuscript)
    assert started['out'] == str(tmp_path / 'run')
    assert started['options']['arm'] == DEFAULT_ARM == 'full-two-track'
    assert started['options']['reviewers'] == 3


def test_a_bare_manuscript_path_is_the_same_review(tmp_path, monkeypatch, started):
    """``peerreview paper.pdf run/`` means ``peerreview review paper.pdf run/``."""
    manuscript = _pdf(tmp_path)

    assert _run([str(manuscript), str(tmp_path / 'run')], monkeypatch) == 0

    assert started['pdf'] == str(manuscript)
    assert started['options']['arm'] == DEFAULT_ARM


def test_options_before_the_manuscript_path_are_still_a_review(tmp_path, monkeypatch, started):
    """A flag's value must not be read as the command name."""
    manuscript = _pdf(tmp_path)

    _run(['--arm', 'single', '--reviewers', '1', str(manuscript), str(tmp_path / 'run')],
         monkeypatch)
    assert started['options']['arm'] == 'single'
    assert started['options']['reviewers'] == 1

    # `--flag=value` carries its value, so the next argument is the manuscript.
    _run(['--arm=reviewers-only', str(manuscript), str(tmp_path / 'run2')], monkeypatch)
    assert started['options']['arm'] == 'reviewers-only'


def test_the_arm_is_an_option_and_no_longer_a_discriminator(tmp_path, monkeypatch, started):
    """0.9 had to see ``--arm`` to choose the plugin. There is nothing else to choose."""
    manuscript = _pdf(tmp_path)
    assert not hasattr(cli, 'plugin_route')
    assert not hasattr(starter, 'handles')

    _run(['review', str(manuscript), str(tmp_path / 'run')], monkeypatch)
    assert started['options']['arm'] == DEFAULT_ARM


def test_an_unknown_arm_is_refused_before_anything_is_written(tmp_path, monkeypatch):
    manuscript = _pdf(tmp_path)
    text = _fails(['review', str(manuscript), str(tmp_path / 'run'), '--arm', 'debate'],
                  monkeypatch)
    assert 'debate' in text
    assert not (tmp_path / 'run').exists()


def test_review_help_names_the_arms(monkeypatch):
    monkeypatch.setattr('sys.argv', ['peerreview', 'review', '--help'])
    with pytest.raises(SystemExit):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            cli.run()
    text = out.getvalue()
    for named in ('full-two-track', 'reviewers-only', 'single', '--reviewers'):
        assert named in text


# -- the report -------------------------------------------------------------


def _run_folder(tmp_path) -> Path:
    review = tmp_path / 'run' / 'review'
    review.mkdir(parents=True)
    (review / 'protocol.json').write_text(json.dumps({
        'arm': 'full-two-track', 'reviewers': 1, 'paper_id': 'abc',
        'plan': {'arm': 'full-two-track', 'reviewers': 1, 'specialists': [], 'editor': True},
        'agents': {'core-1': {'role': 'core', 'track': 'core', 'model': 'claude-sonnet-5-5',
                              'tool_calls': 4}},
    }), encoding='utf-8')
    (review / 'progress.json').write_text(json.dumps({'status': 'complete'}), encoding='utf-8')
    (review / 'runtime.json').write_text(json.dumps({
        'status': 'complete', 'wall_seconds': 61.4, 'tokens_total': 1234,
        'claude_code_version': '2.1.322',
        'agents': [{'label': 'core-1', 'tokens': 1234, 'toolCalls': 9, 'state': 'done',
                    'model': 'claude-sonnet-5-5'}],
    }), encoding='utf-8')
    (review / 'editor-package.json').write_text(json.dumps({
        'summary': 'One finding.', 'findings': [], 'limitations': [],
    }), encoding='utf-8')
    return review


def test_report_rerenders_a_finished_run_offline(tmp_path, monkeypatch):
    review = _run_folder(tmp_path)

    assert _run(['report', str(review)], monkeypatch) == 0

    text = (review / 'REVIEW.md').read_text(encoding='utf-8')
    assert '| Tokens | 1,234 (session stream) |' in text
    assert '| Wall time | 1 min 1 s |' in text
    assert 'claude-sonnet-5-5' in text
    assert (review / 'REVIEW.txt').is_file()


def test_report_takes_the_run_folder_as_well_as_the_review_folder(tmp_path, monkeypatch):
    review = _run_folder(tmp_path)

    assert _run(['report', str(review.parent)], monkeypatch) == 0

    assert (review / 'REVIEW.md').is_file()


def test_report_refuses_a_folder_that_is_not_a_run(tmp_path, monkeypatch):
    assert 'protocol.json' in _fails(['report', str(tmp_path)], monkeypatch)


# -- what is gone -----------------------------------------------------------


@pytest.mark.parametrize('gone', ['import', 'verify', 'investigate', 'checks', 'add-review',
                                  'result-map', 'serve'])
def test_a_command_that_no_longer_exists_fails(gone, tmp_path, monkeypatch):
    """A command that silently became a review would review the wrong thing."""
    _fails([gone, str(_pdf(tmp_path))], monkeypatch)


def test_no_arguments_asks_for_a_command_instead_of_running_anything(monkeypatch):
    assert _fails([], monkeypatch)


def test_the_help_names_the_two_commands_and_nothing_else(monkeypatch):
    parser = cli.build_parser()
    with pytest.raises(SystemExit):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            parser.parse_args(['--help'])
    text = out.getvalue()
    assert 'review' in text and 'report' in text
    for gone in ('import', 'verify', 'investigate', 'checks', 'serve', 'result-map'):
        assert gone not in text


@pytest.mark.parametrize('module', ['peerreviewagents.engine.__main__',
                                    'peerreviewagents.cli.tui'])
def test_the_commands_the_cli_dropped_have_no_module_left(module):
    """``python -m peerreviewagents.engine`` was the other way in. It is gone too."""
    with pytest.raises(ImportError):
        __import__(module)
