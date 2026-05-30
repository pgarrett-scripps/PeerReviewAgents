from .base import make_reviewer_node

node = make_reviewer_node(
    "novelty",
    role="Novelty & Contribution Reviewer",
    mandate=(
        "Assess originality and significance relative to prior work. Use research "
        "tools to find closely related papers (and recent preprints) and judge whether "
        "the contribution is incremental or substantive. Identify overclaimed novelty."
    ),
    tool_names=["find_related_work", "search_preprints"],
)
