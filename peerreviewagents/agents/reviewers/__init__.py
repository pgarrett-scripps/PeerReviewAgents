"""Registry of available specialist reviewers."""

from __future__ import annotations

import warnings

from . import clarity, data_analysis, literature, methodology, novelty

REGISTRY = {
    "methodology": methodology.node,
    "data_analysis": data_analysis.node,
    "novelty": novelty.node,
    "clarity": clarity.node,
    "literature": literature.node,
}


def get_reviewer_nodes(names):
    unknown = [n for n in names if n not in REGISTRY]
    if unknown:
        warnings.warn(
            f"Unknown reviewer name(s) skipped: {', '.join(unknown)}. "
            f"Available: {', '.join(sorted(REGISTRY))}.",
            stacklevel=2,
        )
    return [(n, REGISTRY[n]) for n in names if n in REGISTRY]
