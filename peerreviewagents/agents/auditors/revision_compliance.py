"""Revision-compliance auditor: did the authors do what the letter asked?

Runs only in a revision round, in the audit lane, feeding only the editor —
the same separation the other auditors keep. It is a factual checklist over
the previous decision letter's numbered required revisions, not an opinion
about the manuscript's merit, and it carries no score.

It answers two questions the panel cannot:

1. **Per item, was it done?** Judged against the revised manuscript, never
   against the authors' description of it.
2. **Does the authors' account match the document?** When a response letter
   was supplied, each claim about an item is checked against the text, so an
   overstated or contradicted claim reaches the editor as what it is.

It also reports substantive changes nobody asked for and the letter does not
mention, which is where quietly altered results would show up.

.. note::
   Implementation stub — the node contract (signature, state keys read and
   written, ``RevisionComplianceOutput`` schema) is fixed here so the rest of
   the graph can be wired and tested against it.
"""

from __future__ import annotations

from ...observability import node_context
from ..utils.agent_states import ReviewState

AUDITOR_NAME = "revision_compliance"
AUDITOR_TITLE = "Revision Compliance"


def node(state: ReviewState) -> dict:
    """Audit the revision against the prior round's required revisions.

    Reads: ``prior_round`` (RoundRecord), ``manuscript_diff``,
    ``author_statement``, plus the usual manuscript context.
    Writes: one ``audits`` entry (``auditor="revision_compliance"``) and
    ``total_cost``.
    """
    with node_context(f"audit_{AUDITOR_NAME}", run_id=state["config"].get("run_id", "")):
        return _run(state)


def _run(state: ReviewState) -> dict:
    raise NotImplementedError("revision_compliance auditor is not implemented yet")
