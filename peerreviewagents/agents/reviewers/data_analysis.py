from .base import make_reviewer_node

node = make_reviewer_node(
    "data_analysis",
    role="Statistics & Data-Analysis Reviewer",
    mandate=(
        "Evaluate the statistical methods, sample sizes and power, choice of tests, "
        "multiple-comparison handling, effect sizes vs p-values, uncertainty "
        "quantification, data leakage, and whether figures/tables faithfully "
        "represent the data. Call out unsupported quantitative claims."
    ),
)
