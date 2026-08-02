"""Registry of specialist reviewers — the full panel unless subset.

All eight run on a normal review. ``only_reviewers`` (a revision round or a
correction) re-runs a named subset and carries the rest of the panel's prior
reports forward, so the aggregate still covers all eight either way — see
``PeerReviewGraph._carried_reports``.
"""

from __future__ import annotations

from typing import Callable

from . import (
    clarity,
    data_analysis,
    ethics,
    literature,
    methodology,
    novelty,
    reproducibility,
    rigor,
)

# Each reviewer is a parallel branch fanned out from START.
REVIEWERS: list[tuple[str, Callable]] = [
    ("methodology", methodology.node),
    ("data_analysis", data_analysis.node),
    ("novelty", novelty.node),
    ("clarity", clarity.node),
    ("literature", literature.node),
    ("rigor", rigor.node),
    ("reproducibility", reproducibility.node),
    ("ethics", ethics.node),
]

REVIEWER_NAMES = [name for name, _ in REVIEWERS]


def get_reviewer_nodes() -> list[tuple[str, Callable]]:
    return list(REVIEWERS)
