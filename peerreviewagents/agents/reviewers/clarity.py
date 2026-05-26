from .base import make_reviewer_node

node = make_reviewer_node(
    "clarity",
    role="Clarity & Presentation Reviewer",
    mandate=(
        "Evaluate writing quality, structure, figure/table readability, definition of "
        "terms, and whether a reader in the field could follow and reproduce the "
        "narrative. Note unclear claims and organizational problems."
    ),
)
