"""Builder for debate nodes (Advocate / Skeptic)."""

from __future__ import annotations

from ..utils.agent_states import ReviewState
from ..utils.agent_utils import run_agent
from ..utils.llm import make_llm


def _reports_digest(state: ReviewState) -> str:
    out = []
    for r in state.get("reports", []):
        out.append(
            f"### {r['reviewer']} (score {r['score']}, conf {r['confidence']})\n"
            f"{r['summary']}\n"
            f"Weaknesses: {'; '.join(r['weaknesses'][:5]) or 'none noted'}"
        )
    return "\n\n".join(out)


def _debate_so_far(state: ReviewState) -> str:
    turns = state.get("debate", [])
    if not turns:
        return "(no prior turns)"
    return "\n\n".join(f"[{t['role']} r{t['round']}] {t['content']}" for t in turns)


def make_debate_node(role: str, stance: str):
    def node(state: ReviewState) -> dict:
        config = state["config"]
        llm = make_llm(config, depth="deep")
        rnd = state.get("debate_round", 0) + 1
        system = (
            f"You are the {role} in an editorial debate about whether to accept a "
            f"manuscript. {stance} Argue concisely (max ~250 words), engage directly "
            "with the other side's points, and reference reviewer findings."
        )
        user = (
            f"Reviewer findings:\n{_reports_digest(state)}\n\n"
            f"Debate so far:\n{_debate_so_far(state)}\n\n"
            f"Make your argument for this round."
        )
        try:
            content = run_agent(llm, system, user)
        except Exception as exc:  # noqa: BLE001
            update: dict = {"errors": [f"{role} failed: {exc}"]}
            # Always advance the round counter, even on failure, so the
            # debate loop terminates instead of recursing forever.
            if role == "skeptic":
                update["debate_round"] = state.get("debate_round", 0) + 1
            return update
        update = {"debate": [{"role": role, "round": rnd, "content": content}]}
        if role == "skeptic":  # skeptic closes a round
            update["debate_round"] = state.get("debate_round", 0) + 1
        return update

    node.__name__ = f"debate_{role}"
    return node
