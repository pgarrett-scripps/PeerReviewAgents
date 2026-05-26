"""Registry of available specialist reviewers."""

from . import clarity, data_analysis, literature, methodology, novelty

REGISTRY = {
    "methodology": methodology.node,
    "data_analysis": data_analysis.node,
    "novelty": novelty.node,
    "clarity": clarity.node,
    "literature": literature.node,
}


def get_reviewer_nodes(names):
    return [(n, REGISTRY[n]) for n in names if n in REGISTRY]
