from . import citations as _citations
from .base import make_integrity_node

rigor = make_integrity_node(
    "rigor",
    role="Rigor & Overclaiming Auditor",
    mandate=(
        "Check whether conclusions are proportionate to the evidence and flag "
        "overgeneralization, cherry-picking, or claims unsupported by the results."
    ),
)

reproducibility = make_integrity_node(
    "reproducibility",
    role="Reproducibility Auditor",
    mandate=(
        "Assess whether data, code, parameters, and procedures are described well "
        "enough for an independent group to reproduce the work."
    ),
)

ethics = make_integrity_node(
    "ethics",
    role="Ethics & Compliance Auditor",
    mandate=(
        "Check for ethical concerns: human/animal subjects approvals, consent, dual "
        "use, undisclosed conflicts, data privacy, and responsible-use issues."
    ),
)

NODES = [
    ("rigor", rigor),
    ("reproducibility", reproducibility),
    ("ethics", ethics),
    ("citations", _citations.node),
]
