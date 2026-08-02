"""Registry of editorial compliance auditors — run in parallel, feed the editor.

Auditors mirror the reviewer registry but emit factual checklists
(:class:`~peerreviewagents.agents.schemas.AuditOutput`) rather than scored
opinions, and their output is routed only to the Editor-in-Chief.
"""

from __future__ import annotations

from typing import Callable

from . import citation_integrity, methods_completeness, revision_compliance

# Each auditor is a parallel branch fanned out from START (or the desk-screen
# gate) whose edge lands on the editor.
AUDITORS: list[tuple[str, Callable]] = [
    ("methods_completeness", methods_completeness.node),
    ("citation_integrity", citation_integrity.node),
]

# Auditors that only apply to a revision round. Compliance has nothing to
# check on a first pass — there is no prior required-revision list — so it
# joins the lane only when one exists.
REVISION_AUDITORS: list[tuple[str, Callable]] = [
    ("revision_compliance", revision_compliance.node),
]

AUDITOR_NAMES = [name for name, _ in AUDITORS]
REVISION_AUDITOR_NAMES = [name for name, _ in REVISION_AUDITORS]
ALL_AUDITOR_NAMES = AUDITOR_NAMES + REVISION_AUDITOR_NAMES


def get_auditor_nodes(revision: bool = False) -> list[tuple[str, Callable]]:
    """The audit lane for this run; ``revision`` adds the compliance auditor."""
    if revision:
        return list(AUDITORS) + list(REVISION_AUDITORS)
    return list(AUDITORS)
