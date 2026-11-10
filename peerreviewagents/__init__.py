"""Top-level re-exports for convenience.

Library usage:
    from peerreviewagents import PeerReviewGraph, get_config, write_reports

These three names are resolved lazily (PEP 562). Importing them eagerly here
would pull langgraph, textual and fastapi into every ``import
peerreviewagents.*``, including ``peerreviewagents.engine``, which needs none
of them. ``from peerreviewagents import PeerReviewGraph`` and
``peerreviewagents.PeerReviewGraph`` both still work and still cost the same
imports; only code that never touches these names avoids them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # Static tooling sees the real objects, not the fallback.
    from .default_config import get_config
    from .graph.review_graph import PeerReviewGraph
    from .reports import write_reports

__all__ = ["PeerReviewGraph", "get_config", "write_reports"]

_LAZY = {
    "PeerReviewGraph": (".graph.review_graph", "PeerReviewGraph"),
    "get_config": (".default_config", "get_config"),
    "write_reports": (".reports", "write_reports"),
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
