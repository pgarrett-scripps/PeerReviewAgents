"""Conditional edges controlling the debate loop."""

from __future__ import annotations

from ..agents.utils.agent_states import ReviewState


def should_continue_debate(state: ReviewState) -> str:
    """After the skeptic closes a round, loop or move to synthesis."""
    rounds_done = state.get("debate_round", 0)
    max_rounds = state["config"].get("max_debate_rounds", 2)
    return "advocate" if rounds_done < max_rounds else "meta_reviewer"
