"""Editor-in-Chief: final decision + author-facing decision letter."""

from __future__ import annotations

from ...observability import node_context
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
    "You are the Editor-in-Chief. Using the meta-review, the author's "
    "rebuttal, and the panel's numerical signal, make the FINAL decision "
    "and write a professional, constructive decision letter to the "
    "authors. Weigh the rebuttal: a concession is evidence the manuscript "
    "can improve in revision; a credible disagreement (with manuscript "
    "quote) is evidence a reviewer misread; a load-bearing critique the "
    "author cannot rebut is evidence of a fundamental flaw. Output a "
    "markdown decision letter with a YAML frontmatter block carrying the "
    "decision."
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
        "Produce a markdown decision letter with this exact shape:\n\n"
        "---\n"
        "decision: <accept|minor|major|reject>\n"
        "---\n"
        "## Decision Letter\n\n"
        "## Summary of Evaluation\n\n"
        "## Required Revisions\n"
        "1. Numbered, prioritized, actionable.\n\n"
        "## Minor Suggestions\n"
        "- bullet items.\n\n"
        "If the rebuttal credibly addressed a reviewer's concern, note "
        "that you weighed it in Summary of Evaluation rather than "
        "restating the original critique as a revision requirement."
    )
    try:
        # Editor needs primary-source access to weigh disputed claims;
        # cached_prefix shares the reviewer block's cache entry.
        result = run_agent(
            llm,
            _SYS,
            user,
            cached_prefix=manuscript_block(state),
        )
    except Exception as exc:  # noqa: BLE001
        # Do NOT fabricate a verdict on failure — leave decision empty so
        # the caller knows the editor never rendered one.
        return {"errors": [f"editor failed: {exc}"], "decision": "", "decision_letter": ""}

    meta, _ = split_frontmatter(result.text)
    decision = str(meta.get("decision") or "").strip().lower()
    if decision not in _VALID:
        # Fall back to the meta-reviewer's draft only if it's valid;
        # otherwise leave empty so downstream treats this run as failed.
        draft = state.get("draft_recommendation", "")
        decision = draft if draft in _VALID else ""
    return {
        "decision": decision,
        "decision_letter": result.text,
        "total_cost": result.cost,
    }
