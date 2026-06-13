"""Registry of editorial compliance auditors — run in parallel, feed the editor.

Auditors mirror the reviewer registry but emit factual checklists
(:class:`~peerreviewagents.agents.schemas.AuditOutput`) rather than scored
opinions, and their output is routed only to the Editor-in-Chief.
"""

from __future__ import annotations

from typing import Callable

from . import citation_integrity, methods_completeness

# Each auditor is a parallel branch fanned out from START (or the desk-screen
# gate) whose edge lands on the editor.
AUDITORS: list[tuple[str, Callable]] = [
    ("methods_completeness", methods_completeness.node),
    ("citation_integrity", citation_integrity.node),
]

AUDITOR_NAMES = [name for name, _ in AUDITORS]


def get_auditor_nodes() -> list[tuple[str, Callable]]:
    return list(AUDITORS)
