"""Editor-in-Chief: final decision + author-facing decision letter."""

from __future__ import annotations

from ...observability import node_context
from ..schemas import EditorDecisionOutput, Verdict
from ..utils.agent_states import ReviewState
from ..utils.agent_utils import audit_digest, directives_block, score_summary
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
    "author cannot rebut is evidence of a fundamental flaw. You also receive "
    "one or more editorial compliance audits (e.g. methods completeness, "
    "citation integrity). These are factual checklists, NOT opinions or "
    "scores, produced in parallel with the panel. Treat HARD gaps as items "
    "the authors must add and fold them into required_revisions — they are "
    "not by themselves grounds for rejection, UNLESS a gap actually prevents "
    "evaluating the manuscript's central claims (e.g. a load-bearing protocol "
    "delegated to an unresolvable reference, or a key claim resting on a "
    "misattributed citation). Map SOFT gaps and unverifiable items to "
    "minor_suggestions or to questions for the authors. If a target "
    "journal is described in the context above, make the decision against "
    "that venue's bar and scope, and let required revisions reflect its "
    "standards and submission limits. If a review strictness standard is "
    "described in the context above, apply it to the final decision and let "
    "it guide borderline accept/reject calls. Make required_revisions "
    "concrete, checkable actions ordered by importance — not vague directives "
    "like 'improve rigor' — and keep the letter consistent with the verdict "
    "(a minor-revision decision must not read like a rejection). Let the "
    "verdict track the evidence rather than the raw average; if you depart "
    "from the draft recommendation, give the reasoning in "
    "summary_of_evaluation. Return the structured EditorDecisionOutput schema."
)


def node(state: ReviewState) -> dict:
    with node_context("editor", run_id=state["config"].get("run_id", "")):
        return _run(state)


def _run(state: ReviewState) -> dict:
    config = state["config"]
    llm = make_llm(config, agent="editor", default_tag="synthesis", reasoning_effort="medium")
    rebuttal = state.get("author_rebuttal") or "(no rebuttal provided)"
    user = (
        f"Numerical signal:\n{score_summary(state)}\n\n"
        f"Draft recommendation: {state.get('draft_recommendation')}\n\n"
        f"Meta-review:\n{state.get('meta_review', '')}\n\n"
        f"Author rebuttal:\n{rebuttal}\n\n"
        f"Editorial compliance audits (factual checklists — convert HARD gaps "
        f"to required revisions, SOFT/unverifiable to minor suggestions or "
        f"questions):\n{audit_digest(state)}\n\n"
        "Produce the final decision letter. If the rebuttal credibly "
        "addressed a reviewer's concern, note that you weighed it in "
        "summary_of_evaluation rather than restating the original "
        "critique as a required revision."
    )
    try:
        # The Editor decides on the synthesis (meta-review + rebuttal +
        # numerical signal), not by re-reading the manuscript — that trusts
        # the panel's work instead of re-litigating it. Only the
        # venue/strictness directives ride along so the decision is made
        # against the target venue's bar.
        result = invoke_structured(
            llm,
            EditorDecisionOutput,
            config,
            _SYS,
            user,
            cached_prefix=directives_block(state),
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
        # Structured asks travel alongside the rendered letter so the round
        # record can id them for a later revision round to check off.
        "required_revisions": list(output.required_revisions),
        "minor_suggestions": list(output.minor_suggestions),
        "total_cost": result.cost,
    }
