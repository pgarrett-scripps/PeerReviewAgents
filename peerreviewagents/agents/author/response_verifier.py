"""Response verifier: adjudicate the real authors' letter before anyone reads it.

The authors' response is the one input to this pipeline written by someone
with a direct stake in the verdict. Handing it to the panel as prose would
let a persuasive letter do the reviewers' work for them, and a dishonest one
manipulate them outright. So it never reaches them as prose.

This node stands between the letter and everyone else. It runs *before* the
reviewer fan-out (see ``build_graph``) and converts the letter into
:class:`ResponseVerificationOutput` — a list of claims, each checked against
the manuscript and labelled corroborated / overstated / contradicted /
unlocatable.

The governing rule, which the prompts must enforce and the graph makes
structural:

    **Only the manuscript supplies evidence. The letter can only point at it.**

So the panel-facing block (``ResponseVerificationOutput.panel_block``)
contains corroborated *pointers* and no conclusions: "the authors ask you to
re-read §3.2". A reviewer re-reads and decides. A claim that points nowhere
checkable cannot move anything, and passages that try to instruct the review
rather than argue about the science are recorded separately for the editor
and carry no weight at all.

.. note::
   Implementation stub — the node contract (signature, state keys read and
   written, ``ResponseVerificationOutput`` schema) is fixed here so the rest
   of the graph can be wired and tested against it.
"""

from __future__ import annotations

from ...observability import node_context
from ..utils.agent_states import ReviewState


def node(state: ReviewState) -> dict:
    """Turn the author statement into checked claims.

    Reads: ``author_statement`` (raw letter), ``prior_round``,
    ``manuscript_diff``, plus the usual manuscript context.
    Writes: ``response_verification`` (rendered markdown for the editor and
    the report), ``verified_claims_block`` (the pointer-only block the panel
    sees), and ``total_cost``.

    Fail-open: if verification cannot be completed, both blocks stay empty,
    which means the panel simply never hears from the letter. Failing closed
    would be worse — an unverified letter reaching the reviewers is the exact
    outcome this node exists to prevent.
    """
    with node_context("response_verifier", run_id=state["config"].get("run_id", "")):
        return _run(state)


def _run(state: ReviewState) -> dict:
    raise NotImplementedError("response_verifier is not implemented yet")
