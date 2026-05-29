"""Builder for debate nodes (Advocate / Skeptic)."""

from __future__ import annotations

from ...observability import node_context
from ..utils.agent_states import ReviewState
from ..utils.agent_utils import body_only, manuscript_block, run_agent
from ..utils.llm import make_llm


def _reports_digest(state: ReviewState) -> str:
    """Concatenate each reviewer's markdown body (frontmatter stripped).

    Bigger than the prior summary+weaknesses digest, but the manuscript
    block dominates the prompt and is cached — the extra reviewer text
    is cheap, and giving the debaters the full reviews instead of a
    compressed summary improves grounding.
    """
    out = []
    for r in state.get("reports", []):
        out.append(
            f"### {r['reviewer']} (score {r['score']}, conf {r['confidence']})\n"
            f"{body_only(r['body']).strip()}"
        )
    return "\n\n".join(out)


def _debate_so_far(state: ReviewState) -> str:
    turns = state.get("debate", [])
    if not turns:
        return "(no prior turns)"
    return "\n\n".join(f"[{t['role']} r{t['round']}] {t['content']}" for t in turns)


def make_debate_node(role: str, stance: str):
    def node(state: ReviewState) -> dict:
        with node_context(role):
            config = state["config"]
            llm = make_llm(config)
            rnd = state.get("debate_round", 0) + 1
            system = (
                f"You are the {role} in an editorial debate about whether to accept a "
                f"manuscript. {stance} Argue concisely (max ~250 words), engage directly "
                "with the other side's points, and ground every claim in specific text "
                "from the manuscript above (quote sections or figures by name)."
            )
            user = (
                f"Reviewer findings:\n{_reports_digest(state)}\n\n"
                f"Debate so far:\n{_debate_so_far(state)}\n\n"
                f"Make your argument for this round."
            )
            try:
                # Manuscript goes as cached_prefix — byte-identical to the
                # reviewer prefix, so this lands a prompt-cache hit rather
                # than paying full input-token price per debate turn.
                result = run_agent(
                    llm,
                    system,
                    user,
                    cached_prefix=manuscript_block(state),
                )
            except Exception as exc:  # noqa: BLE001
                update: dict = {"errors": [f"{role} failed: {exc}"]}
                # Always advance the round counter, even on failure, so the
                # debate loop terminates instead of recursing forever.
                if role == "skeptic":
                    update["debate_round"] = state.get("debate_round", 0) + 1
                return update
            update = {
                "debate": [{"role": role, "round": rnd, "content": result.text}],
                "total_cost": result.cost,
            }
            if role == "skeptic":  # skeptic closes a round
                update["debate_round"] = state.get("debate_round", 0) + 1
            return update

    node.__name__ = f"debate_{role}"
    return node
