"""Conditional edges: the desk-screen gate and the debate loop."""

from __future__ import annotations

from langgraph.graph import END

from ..agents.auditors import AUDITOR_NAMES
from ..agents.reviewers import REVIEWER_NAMES
from ..agents.utils.agent_states import ReviewState

# Reviewer + auditor node names the desk-screen gate fans out to when a
# manuscript passes triage (mirrors the START fan-out in the no-desk-screen
# graph). Both lanes are skipped entirely on a desk-reject.
_REVIEWER_NODES = [f"reviewer_{name}" for name in REVIEWER_NAMES]
_AUDIT_NODES = [f"audit_{name}" for name in AUDITOR_NAMES]


def route_after_desk_screen(state: ReviewState):
    """Desk-reject short-circuits to END; otherwise fan out to reviewers + auditors.

    Returning a list of node names triggers LangGraph's parallel fan-out,
    so a passed manuscript reaches exactly the same reviewer panel and audit
    lane it would have from START in the default (no-desk-screen) graph.
    """
    if state.get("desk_rejected"):
        return END
    return [*_REVIEWER_NODES, *_AUDIT_NODES]


def should_continue_debate(state: ReviewState) -> str:
    """After the skeptic closes a round, loop or move to synthesis."""
    rounds_done = state.get("debate_round", 0)
    max_rounds = state["config"].get("max_debate_rounds", 2)
    return "advocate" if rounds_done < max_rounds else "meta_reviewer"
