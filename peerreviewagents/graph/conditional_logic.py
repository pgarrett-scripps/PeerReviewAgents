"""Conditional edges: the desk-screen gate and the debate loop."""

from __future__ import annotations

from langgraph.graph import END

from ..agents.utils.agent_states import ReviewState


def make_desk_route(targets: list[str]):
    """Build a desk-screen router that fans out to ``targets`` on a pass.

    The desk node's successor is not fixed: a first-round run fans straight
    out to the panel, while a revision round with an author statement routes
    through the response verifier first, so the letter is adjudicated before
    any reviewer exists to be persuaded by it. Both still short-circuit to
    END on a desk reject.
    """

    def route(state: ReviewState):
        if state.get("desk_rejected"):
            return END
        return list(targets)

    return route


def should_continue_debate(
    state: ReviewState, final_target: str = "debate_synthesizer",
) -> list[str] | str:
    """After the join closes a round, fan out another round or move on.

    Runs from the debate join node, which has already incremented
    ``debate_round`` — both debaters of the next round launch in parallel.
    """
    rounds_done = state.get("debate_round", 0)
    max_rounds = state["config"].get("max_debate_rounds", 2)
    if rounds_done < max_rounds:
        return ["advocate", "skeptic"]
    return final_target
