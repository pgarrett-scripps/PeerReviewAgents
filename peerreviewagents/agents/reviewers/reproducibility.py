from .base import make_reviewer_node

node = make_reviewer_node(
    "reproducibility",
    role="Reproducibility Reviewer",
    mandate=(
        "Assess whether data, code, hyperparameters, procedures, software "
        "versions, and analysis decisions are described well enough for an "
        "independent group to reproduce the work. Note missing details that "
        "would block a replication attempt."
    ),
)
