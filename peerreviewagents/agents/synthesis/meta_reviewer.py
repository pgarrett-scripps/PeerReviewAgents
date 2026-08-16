"""Meta-reviewer / Area Chair: synthesize reviews + debate into a draft recommendation."""

from __future__ import annotations

from ...observability import node_context
from ..debate.base import _debate_so_far, _reports_digest, panel_gaps_block
from ..utils.agent_states import ReviewState
from ..utils.agent_utils import directives_block, score_summary
from ..utils.llm import make_llm
from ..utils.structured import invoke_markdown

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
    "Write an ordinary Markdown meta-review. State any draft recommendation "
    "in prose, but do not return JSON or a fixed schema."
)


def node(state: ReviewState) -> dict:
    with node_context("meta_reviewer", run_id=state["config"].get("run_id", "")):
        return _run(state)


def _run(state: ReviewState) -> dict:
    config = state["config"]
    llm = make_llm(config, agent="meta_reviewer", default_tag="synthesis")
    user = (
        f"Reviewer findings:\n{_reports_digest(state)}\n\n"
        f"Gaps the technical reviewers missed, audited against the "
        f"manuscript:\n{panel_gaps_block(state)}\n\n"
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
        result = invoke_markdown(
            llm,
            config,
            _SYS,
            user,
            cached_prefix=directives_block(state),
            min_chars=100,
        )
    except Exception as exc:  # noqa: BLE001
        # No fabricated verdict on failure: a hardcoded "major" here reads
        # downstream as a real Area Chair recommendation, and the editor's
        # own fallback once adopted it as the FINAL decision. Emit an empty
        # recommendation and a marker the editor prompt renders honestly.
        return {
            "errors": [f"meta_reviewer failed: {exc}"],
            "meta_review": f"(the meta-reviewer did not run: {exc})",
            "draft_recommendation": "",
        }

    return {
        "meta_review": result.text,
        # This stage is advisory and inactive in the current graph. The editor
        # reads the full prose if it is ever re-enabled; no scalar extracted
        # from its formatting is allowed to become a shadow verdict.
        "draft_recommendation": "",
        "total_cost": result.cost,
    }

