"""Shared test guards.

The suite makes no model call. After 0.9 there is exactly one way it could: the
starter launches the ``claude`` executable it finds on ``PATH``. Every test that
exercises a launch installs its own stub on ``PATH`` first, and the suite is run
with no real ``claude`` or ``codex`` reachable, so a test that forgot to stub
fails as a missing executable — a blocked run — rather than spending somebody's
subscription.

What is left to guard here is credentials. A key in the environment is what
turns a stray launch from an error into a billed call, so every test runs with
one removed, whatever the developer has exported.
"""

from __future__ import annotations

import pytest

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
