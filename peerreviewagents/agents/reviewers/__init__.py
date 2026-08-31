"""Registry of specialist reviewers.

The panel is the five-role condensed roster: scientific validity,
quantitative evidence (data_analysis), contribution and prior work,
reporting and reproducibility, and ethics. The historical eight-role panel
was removed with the ``reviewer_panel`` config key; published rounds that
ran it remain readable, but no new run can select it.

``only_reviewers`` (a revision round or correction) re-runs a named subset
and carries the rest of the panel's prior reports forward — see
``PeerReviewGraph._carried_reports``.
"""

from __future__ import annotations

from typing import Callable

from . import (
    contribution_context,
    data_analysis,
    ethics,
    reporting_reproducibility,
    scientific_validity,
)

# Each reviewer is a parallel branch fanned out from START.
REVIEWERS: list[tuple[str, Callable]] = [
    ("scientific_validity", scientific_validity.node),
    ("data_analysis", data_analysis.node),
    ("contribution_context", contribution_context.node),
    ("reporting_reproducibility", reporting_reproducibility.node),
    ("ethics", ethics.node),
]

REVIEWER_NAMES = [name for name, _ in REVIEWERS]
ALL_REVIEWER_NAMES = list(REVIEWER_NAMES)

# Compatibility aliases from when a "condensed" roster sat beside the
# eight-role one; the condensed roster is now simply THE roster.
CONDENSED_REVIEWERS = REVIEWERS
CONDENSED_REVIEWER_NAMES = list(REVIEWER_NAMES)


def get_reviewer_nodes(config: dict | None = None) -> list[tuple[str, Callable]]:
    return list(REVIEWERS)
