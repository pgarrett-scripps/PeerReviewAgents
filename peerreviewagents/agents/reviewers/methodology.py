from .base import make_reviewer_node

node = make_reviewer_node(
    "methodology",
    role="Methodology Reviewer",
    mandate=(
        "Scrutinize the soundness of the study design, experimental setup, controls, "
        "and whether the methods can actually support the stated conclusions. Flag "
        "confounds, missing baselines, and threats to validity."
    ),
)
