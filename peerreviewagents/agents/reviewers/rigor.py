from .base import make_reviewer_node

node = make_reviewer_node(
    "rigor",
    role="Rigor & Overclaiming Reviewer",
    mandate=(
        "Check whether the manuscript's conclusions are proportionate to its "
        "evidence. Flag overgeneralization, cherry-picked results, results "
        "stated as causal when only correlational, and headline claims that "
        "the experiments don't actually support."
    ),
)
