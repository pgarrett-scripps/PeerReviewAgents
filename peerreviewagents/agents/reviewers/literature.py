from .base import make_reviewer_node

node = make_reviewer_node(
    "literature",
    role="Related-Work & Citations Reviewer",
    mandate=(
        "Check whether the related-work coverage is adequate and fair, whether key "
        "references are missing, and whether claims about prior work are accurate. Use "
        "research tools to verify citations and surface omitted relevant literature, "
        "including biomedical sources when the topic warrants it."
    ),
    tool_names=["find_related_work", "search_biomedical_literature"],
)
