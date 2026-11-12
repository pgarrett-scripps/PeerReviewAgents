"""Where the Stage A data directory comes from, and what happens with none.

The scripts used to carry one machine's path as a default, which is wrong in a
public repository and wrong on anyone else's machine. The directory is now
named by whoever runs them, and a script given no directory says so and stops
rather than inventing one and writing papers into it.
"""

from __future__ import annotations

from pathlib import Path

import corpus
import matrix
import plant
import pytest
import root

SCRIPTS = (matrix, corpus, plant)


def test_the_option_names_the_directory():
    assert root.resolve_root('/tmp/stage-a', environ={}) == Path('/tmp/stage-a')


def test_the_environment_names_it_when_the_option_does_not(tmp_path):
    resolved = root.resolve_root('', environ={root.VARIABLE: str(tmp_path)})
    assert resolved == tmp_path


def test_the_option_wins_over_the_environment(tmp_path):
    resolved = root.resolve_root(str(tmp_path / 'given'),
                                 environ={root.VARIABLE: str(tmp_path / 'env')})
    assert resolved == tmp_path / 'given'


def test_a_leading_tilde_is_expanded():
    assert not str(root.resolve_root('~/stage-a', environ={})).startswith('~')


def test_neither_is_an_error_and_not_a_guess():
    with pytest.raises(root.MissingRoot) as raised:
        root.resolve_root(None, environ={})
    assert root.VARIABLE in str(raised.value)
    assert '--root' in str(raised.value)


def test_a_blank_value_does_not_become_the_working_directory():
    with pytest.raises(root.MissingRoot):
        root.resolve_root('   ', environ={root.VARIABLE: ''})


def test_no_script_carries_a_default_directory():
    for script in SCRIPTS:
        parser = script.build_parser()
        option, = [action for action in parser._actions if action.dest == 'root']
        assert option.default == '', script.__name__


@pytest.mark.parametrize('script, argv', (
    (matrix, ['--dry-run']),
    (corpus, ['roster']),
    (plant, ['paper.pdf', '--survey']),
))
def test_a_script_with_no_directory_stops_with_one_line(script, argv, capsys,
                                                        monkeypatch):
    monkeypatch.delenv(root.VARIABLE, raising=False)
    assert script.main(argv) == 2
    assert capsys.readouterr().err.strip() == root.MESSAGE
