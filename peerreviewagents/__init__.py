"""Top-level re-exports for convenience.

Library usage:

    from peerreviewagents import review, import_paper, write_reports

:func:`review` is the whole review: it starts one headless Claude Code session
on the ``pra`` plugin, enforces the token ceilings from the session's own
stream, and writes the run folder. It is exactly what ``peerreview review``
runs. :func:`import_paper` imports a PDF into a manuscript workspace without
any model call, and :func:`write_reports` renders ``REVIEW.md`` and
``REVIEW.txt`` from a run folder, also without any model call.

The names resolve lazily (PEP 562) so that ``import peerreviewagents`` stays as
cheap as it is on its own, and so a caller that only wants to read a run never
imports the launcher.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # Static tooling sees the real objects, not the fallback.
    from .engine.ingest import import_paper
    from .engine.run_report import write_reports
    from .plugin.starter import review

__all__ = ["review", "import_paper", "write_reports"]

_LAZY = {
    "review": (".plugin.starter", "review"),
    "import_paper": (".engine.ingest", "import_paper"),
    "write_reports": (".engine.run_report", "write_reports"),
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
