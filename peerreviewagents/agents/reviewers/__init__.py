"""Registry of specialist reviewers — the full panel always runs."""

from __future__ import annotations

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

# Each reviewer is a parallel branch fanned out from START. Rigor,
# reproducibility, and ethics used to be a separate "integrity panel"
# that ran serially after the meta-review; they're regular reviewers
# now so the whole panel runs concurrently.
REVIEWERS: list[tuple[str, callable]] = [
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


def get_reviewer_nodes() -> list[tuple[str, callable]]:
    return list(REVIEWERS)
