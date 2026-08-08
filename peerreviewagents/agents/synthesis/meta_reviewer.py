"""Meta-reviewer / Area Chair: synthesize reviews + debate into a draft recommendation."""

from __future__ import annotations

from ...observability import node_context
from ..debate.base import _debate_so_far, _reports_digest, cross_exam_block
from ..schemas import MetaReviewOutput
from ..utils.agent_states import ReviewState
from ..utils.agent_utils import directives_block, score_summary
from ..utils.llm import make_llm
from ..utils.structured import invoke_structured

_SYS = (
    "You are the Area Chair synthesizing a peer-review package. Weigh the "
    "specialist reviews — by BOTH score and confidence, so a low-confidence "
    "outlier does not swing the verdict the way a high-confidence consensus "
    "does — together with the advocate/skeptic debate into a single balanced "
    "meta-review. Surface genuine tensions between reviewers rather than "
    "averaging them into mush: when the panel splits, name the split and pick "
    "a side with reasoning. Let the debate sharpen the synthesis — note which "
    "critiques survived contact with the advocate. Synthesize and cite the "
    "specialists' findings; do not re-derive or re-audit them yourself. Be "
    "decisive but fair. If a target journal is described in the context above, "
    "calibrate the recommendation to that venue's standards and scope. If a "
    "review strictness standard is described in the context above, calibrate "
    "the recommendation to it as well. "
    "Return the structured MetaReviewOutput schema."
)


def node(state: ReviewState) -> dict:
    with node_context("meta_reviewer", run_id=state["config"].get("run_id", "")):
        return _run(state)


def _run(state: ReviewState) -> dict:
    config = state["config"]
    llm = make_llm(config, agent="meta_reviewer", default_tag="synthesis")
    user = (
        f"Reviewer findings:\n{_reports_digest(state)}\n\n"
        f"Cross-examination — findings drawn from more than one report, "
        f"which no single reviewer made:\n{cross_exam_block(state)}\n\n"
        f"Debate transcript:\n{_debate_so_far(state)}\n\n"
        f"Numerical signal:\n{score_summary(state)}\n\n"
        "Produce a meta-review. Engage with the numerical signal above: if "
        "your draft_recommendation diverges from the confidence-weighted "
        "average verdict, name the specific reasoning in decisive_factors.\n\n"
        "Recommending a verdict harsher than every reviewer gave needs a "
        "reason from the manuscript. The panel already judged this paper "
        "against the target venue's standards — they were given the same "
        "venue description you were — so the venue's selectivity is not a "
        "fresh argument for lowering the verdict, and its headline acceptance "
        "rate is a base rate over all submissions rather than a quota to hold "
        "this one to. If no reviewer thought the work should be rejected, "
        "recommending rejection means the panel collectively missed something "
        "you can point to. Point to it, or do not."
    )
    try:
        # The Area Chair synthesizes the distilled signal (reviews + debate +
        # numerical aggregate), NOT the primary text — feeding it the full
        # manuscript invites it to become a 9th reviewer instead of weighing
        # the panel. Only the venue/strictness directives ride along as the
        # cached prefix so it can calibrate to the target venue's bar.
        result = invoke_structured(
            llm,
            MetaReviewOutput,
            config,
            _SYS,
            user,
            cached_prefix=directives_block(state),
        )
    except Exception as exc:  # noqa: BLE001
        return {
            "errors": [f"meta_reviewer failed: {exc}"],
            "meta_review": "",
            "draft_recommendation": "major",
        }

    output: MetaReviewOutput = result.instance  # type: ignore[assignment]
    return {
        "meta_review": output.to_markdown(),
        "draft_recommendation": output.draft_recommendation,
        "total_cost": result.cost,
    }


