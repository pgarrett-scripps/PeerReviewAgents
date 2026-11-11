"""Shared test guards.

The suite makes no model call. That is only true if a test cannot reach a real
provider command line, and "cannot" has to cover both transports: a run that
only stubbed `claude` still found an installed `codex` on PATH and started a
real review on a developer's machine. :func:`unreachable_provider_clis` removes
both, by name and by override variable, and replaces the module every launch
goes through so a call that slips past the lookup fails loudly instead of
spending somebody's subscription.
"""

from __future__ import annotations

import pytest

from peerreviewagents.engine import model_runtime


class _NoSubprocess:
    """Stands in for :mod:`subprocess` inside the model transport."""

    def __getattr__(self, name: str):
        raise AssertionError(
            f"a test tried to launch a provider command line (subprocess.{name}); "
            "the transport must be stubbed instead"
        )


def unreachable_provider_clis(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make both provider CLIs unreachable for the duration of a test."""
    monkeypatch.setenv("PATH", "")
    for variable in model_runtime.CLI_OVERRIDES.values():
        monkeypatch.delenv(variable, raising=False)

    def missing(name: str) -> str:
        raise model_runtime.RunBlocked(model_runtime.CLI_MISSING[name])

    monkeypatch.setattr(model_runtime, "cli_executable", missing)
    monkeypatch.setattr(model_runtime, "subprocess", _NoSubprocess())


@pytest.fixture
def no_provider_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fixture form of :func:`unreachable_provider_clis`."""
    unreachable_provider_clis(monkeypatch)


@pytest.fixture
def stubbed_provider_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    """Answer the transport's CLI lookup without any provider installed.

    A test that stubs the launch itself still has to get past the lookup that
    now runs before it, and the lookup only passed on a machine where the
    provider happened to be installed: every such test failed in CI with "The
    Codex CLI was not found". The lookup answers with a path that does not
    exist, so a launch this fixture's user forgot to stub fails as a missing
    file rather than reaching a real binary.
    """
    monkeypatch.setenv("PATH", "")
    for variable in model_runtime.CLI_OVERRIDES.values():
        monkeypatch.delenv(variable, raising=False)

    def stubbed(name: str) -> str:
        assert name in model_runtime.CLI_MISSING, f"unknown provider CLI: {name}"
        return f"/nonexistent/provider-cli/{name}"

    monkeypatch.setattr(model_runtime, "cli_executable", stubbed)
