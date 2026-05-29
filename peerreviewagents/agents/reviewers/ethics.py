from .base import make_reviewer_node

node = make_reviewer_node(
    "ethics",
    role="Ethics & Compliance Reviewer",
    mandate=(
        "Check for ethical concerns: human/animal-subjects approvals, informed "
        "consent, dual-use risk, undisclosed conflicts of interest, data "
        "privacy, and responsible-use issues. Note where required disclosures "
        "are missing."
    ),
)
