"""Meta-reviewer / Area Chair: synthesize reviews + debate into a draft recommendation."""

from __future__ import annotations

from ...observability import node_context
from ...storage.memory import MemoryLog
from ..debate.base import _debate_so_far, _reports_digest
from ..schemas import MetaReviewOutput
from ..utils.agent_states import ReviewState
from ..utils.agent_utils import manuscript_block, score_summary
from ..utils.llm import make_llm
from ..utils.structured import invoke_structured

_SYS = (
    "You are the Area Chair synthesizing a peer-review package. Weigh the "
    "specialist reviews (by score and confidence) and the advocate/skeptic "
    "debate into a single balanced meta-review. Be decisive but fair. "
    "Return the structured MetaReviewOutput schema."
)


def node(state: ReviewState) -> dict:
    with node_context("meta_reviewer"):
        return _run(state)


def _run(state: ReviewState) -> dict:
    config = state["config"]
    llm = make_llm(config, reasoning_effort="high")
    past_context = _past_context(state, config)
    past_block = (
        f"{past_context}\n\n"
        "Use the prior calibration as background — diverge from it only "
        "with specific reasoning grounded in this manuscript.\n\n"
    ) if past_context else ""
    user = (
        f"{past_block}"
        f"Reviewer findings:\n{_reports_digest(state)}\n\n"
        f"Debate transcript:\n{_debate_so_far(state)}\n\n"
        f"Numerical signal:\n{score_summary(state)}\n\n"
        "Produce a meta-review. Engage with the numerical signal above: if "
        "your draft_recommendation diverges from the confidence-weighted "
        "average verdict, name the specific reasoning in decisive_factors."
    )
    try:
        # Manuscript is the same cached prefix the reviewers populated —
        # near-zero marginal cost, but lets the meta-reviewer ground its
        # synthesis in primary text rather than just the digest.
        result = invoke_structured(
            llm,
            MetaReviewOutput,
            config,
            _SYS,
            user,
            cached_prefix=manuscript_block(state),
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


def _past_context(state: ReviewState, config: dict) -> str:
    """BM25-retrieve the top-K resolved lessons most relevant to this
    manuscript. Failures degrade silently to no prior context."""
    k = int(config.get("memory_k", 3) or 0)
    if k <= 0:
        return ""
    title = state.get("manuscript_title", "") or ""
    sections = state.get("sections") or {}
    abstract = sections.get("abstract") or (state.get("manuscript_md", "")[:500])
    query = f"{title}\n{abstract}"
    try:
        return MemoryLog(config["memory_path"]).get_past_context(query, k=k)
    except Exception:  # noqa: BLE001
        return ""
