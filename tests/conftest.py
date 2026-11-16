"""Shared test guards and the path that makes the manuscript builder importable.

The suite makes no model call. After 0.9 there is exactly one way it could: the
starter launches the ``claude`` executable it finds on ``PATH``. Every test that
exercises a launch installs its own stub on ``PATH`` first, and the suite is run
with no real ``claude`` or ``codex`` reachable, so a test that forgot to stub
fails as a missing executable — a blocked run — rather than spending somebody's
subscription.

What is left to guard here is credentials. A key in the environment is what
turns a stray launch from an error into a billed call, so every test runs with
one removed, whatever the developer has exported.

Beside the guards is ``make_base``, the fixture form of
:func:`manuscripts.build_base`: a manuscript workspace built from pieces of
text, without a PDF and without the converter. The builder itself lives in
``manuscripts.py`` beside this file, and this folder is put on ``sys.path`` here
so ``from manuscripts import build_base`` reads the same module from every test,
including the ones in subfolders.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from manuscripts import build_base  # noqa: E402  (needs the path above)

CREDENTIAL_VARIABLES = ('ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN', 'OPENAI_API_KEY')


@pytest.fixture(autouse=True)
def no_model_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test sees a provider credential, however the machine is configured."""
    for variable in CREDENTIAL_VARIABLES:
        monkeypatch.delenv(variable, raising=False)


@pytest.fixture
def no_claude_cli(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Make the only provider executable unreachable for one test."""
    empty = tmp_path / 'no-executables'
    empty.mkdir(exist_ok=True)
    monkeypatch.setenv('PATH', str(empty))


@pytest.fixture
def make_base(tmp_path):
    """:func:`manuscripts.build_base`, writing into this test's own folder."""
    def build(pieces, documents=None, references=None, manifest=None, folder=None):
        return build_base(folder or (tmp_path / 'manuscript'), pieces, documents,
                          references, manifest)
    return build
