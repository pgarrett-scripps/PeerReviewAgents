"""Top-level re-exports for convenience.

Library usage:

    from peerreviewagents import plan_review, review_options, run_review_options

Those three are one review: ``plan_review`` resolves a manuscript PDF into a
run folder, ``review_options`` validates a request in the same words every
surface uses, and ``run_review_options`` runs it and writes the reports. The
command line, the MCP server and the web UI all go through exactly these.
``capabilities`` reports what this install can run without starting anything.

The names resolve lazily (PEP 562) so that ``import peerreviewagents.engine``
stays as cheap as it is on its own, and so this module never imports the web
UI's dependencies for a caller that only wants the engine.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # Static tooling sees the real objects, not the fallback.
    from .engine.api import capabilities
    from .engine.bundle import plan_review, review_options, run_review_options

__all__ = ["capabilities", "plan_review", "review_options", "run_review_options"]

_LAZY = {
    "capabilities": (".engine.api", "capabilities"),
    "plan_review": (".engine.bundle", "plan_review"),
    "review_options": (".engine.bundle", "review_options"),
    "run_review_options": (".engine.bundle", "run_review_options"),
}


def __getattr__(name: str):
    try:
        module, attribute = _LAZY[name]
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None
    from importlib import import_module

    value = getattr(import_module(module, __name__), attribute)
    # Cache on the module so later lookups skip this function entirely.
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_LAZY))
