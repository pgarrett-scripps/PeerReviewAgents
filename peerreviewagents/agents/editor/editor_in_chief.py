"""Editor-in-Chief: final decision + author-facing decision letter."""

from __future__ import annotations

from ...observability import node_context
from ..schemas import EditorDecisionOutput, Verdict
from ..utils.agent_states import ReviewState
from ..utils.agent_utils import context_block, score_summary
from ..utils.llm import make_llm
from ..utils.structured import invoke_structured

_VALID_VERDICTS = ("accept", "minor", "major", "reject")

_SYS = (
    "You are the Editor-in-Chief. Using the meta-review, the author's "
    "rebuttal, and the panel's numerical signal, make the FINAL decision "
    "and write a professional, constructive decision letter to the "
    "authors. Weigh the rebuttal: a concession is evidence the manuscript "
    "can improve in revision; a credible disagreement (with manuscript "
    "quote) is evidence a reviewer misread; a load-bearing critique the "
    "author cannot rebut is evidence of a fundamental flaw. If a target "
    "journal is described in the context above, make the decision against "
    "that venue's bar and scope, and let required revisions reflect its "
    "standards and submission limits. Return the "
    "structured EditorDecisionOutput schema."
)


def node(state: ReviewState) -> dict:
    with node_context("editor"):
        return _run(state)


def _run(state: ReviewState) -> dict:
    config = state["config"]
    llm = make_llm(config, reasoning_effort="high")
    rebuttal = state.get("author_rebuttal") or "(no rebuttal provided)"
    user = (
        f"Numerical signal:\n{score_summary(state)}\n\n"
        f"Draft recommendation: {state.get('draft_recommendation')}\n\n"
        f"Meta-review:\n{state.get('meta_review', '')}\n\n"
        f"Author rebuttal:\n{rebuttal}\n\n"
        "Produce the final decision letter. If the rebuttal credibly "
        "addressed a reviewer's concern, note that you weighed it in "
        "summary_of_evaluation rather than restating the original "
        "critique as a required revision."
    )
    try:
        # Editor needs primary-source access to weigh disputed claims;
        # cached_prefix shares the reviewer block's cache entry.
        result = invoke_structured(
            llm,
            EditorDecisionOutput,
            config,
            _SYS,
            user,
            cached_prefix=context_block(state),
        )
    except Exception as exc:  # noqa: BLE001
        # Do NOT fabricate a verdict on failure — leave decision empty so
        # the caller knows the editor never rendered one.
        return {"errors": [f"editor failed: {exc}"], "decision": "", "decision_letter": ""}

    output: EditorDecisionOutput = result.instance  # type: ignore[assignment]
    decision: Verdict | str = output.decision
    # Schema constrains decision to the Verdict literal, but defensively
    # fall back to the draft if a non-conforming model slipped past.
    if decision not in _VALID_VERDICTS:
        draft = state.get("draft_recommendation", "")
        decision = draft if draft in _VALID_VERDICTS else ""
    return {
        "decision": decision,
        "decision_letter": output.to_markdown(),
        "total_cost": result.cost,
    }
