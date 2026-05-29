"""Meta-reviewer / Area Chair: synthesize reviews + debate into a draft recommendation."""

from __future__ import annotations

from ...observability import node_context
from ..debate.base import _debate_so_far, _reports_digest
from ..utils.agent_states import ReviewState
from ..utils.agent_utils import (
    manuscript_block,
    run_agent,
    score_summary,
    split_frontmatter,
)
from ..utils.llm import make_llm

_VALID = ("accept", "minor", "major", "reject")

_SYS = (
    "You are the Area Chair synthesizing a peer-review package. Weigh the "
    "specialist reviews (by score and confidence) and the advocate/skeptic "
    "debate into a single balanced meta-review. Be decisive but fair. "
    "Output a markdown document with a YAML frontmatter block carrying "
    "your draft recommendation."
)


def node(state: ReviewState) -> dict:
    with node_context("meta_reviewer"):
        return _run(state)


def _run(state: ReviewState) -> dict:
    llm = make_llm(state["config"], reasoning_effort="high")
    user = (
        f"Reviewer findings:\n{_reports_digest(state)}\n\n"
        f"Debate transcript:\n{_debate_so_far(state)}\n\n"
        f"Numerical signal:\n{score_summary(state)}\n\n"
        "Produce a markdown meta-review with this exact shape:\n\n"
        "---\n"
        "draft_recommendation: <accept|minor|major|reject>\n"
        "---\n"
        "# Meta-Review\n\n"
        "## Synthesis\n"
        "Consensus and key tensions across the panel.\n\n"
        "## Decisive Factors\n"
        "What most drives the outcome.\n\n"
        "Your draft_recommendation should engage with the numerical "
        "signal above: if you diverge from the confidence-weighted "
        "average verdict, name the specific reasoning in Decisive Factors."
    )
    try:
        # Manuscript is the same cached prefix the reviewers populated —
        # near-zero marginal cost, but lets the meta-reviewer ground its
        # synthesis in primary text rather than just the digest.
        result = run_agent(
            llm,
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

    meta, _ = split_frontmatter(result.text)
    rec = str(meta.get("draft_recommendation") or "").strip().lower()
    if rec not in _VALID:
        rec = "major"
    return {
        "meta_review": result.text,
        "draft_recommendation": rec,
        "total_cost": result.cost,
    }
