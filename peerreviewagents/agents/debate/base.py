"""Builder for debate nodes (Advocate / Skeptic).

Each debater emits ordinary Markdown. That exact response is stored on the
DebateTurn ``content`` field and passed to the next debater and editor.
"""

from __future__ import annotations

from ...observability import node_context
from ..utils.agent_states import ReviewState
from ..utils.agent_utils import context_block
from ..utils.llm import make_llm
from ..utils.structured import invoke_markdown


def _reports_digest(state: ReviewState) -> str:
    """Concatenate each reviewer's rendered markdown body.

    Bigger than the prior summary+weaknesses digest, but the manuscript
    block dominates the prompt and is cached — the extra reviewer text
    is cheap, and giving the debaters the full reviews instead of a
    compressed summary improves grounding.
    """
    out = []
    for r in state.get("reports", []):
        head = (
            f"(score {r['score']}, conf {r['confidence']})"
            if isinstance(r.get("score"), (int, float))
            else "(not applicable to this manuscript)"
        )
        out.append(f"### {r['reviewer']} {head}\n{r['body'].strip()}")
    return "\n\n".join(out)


def panel_gaps_block(state: ReviewState) -> str:
    """The gap finder's findings, or a line saying it did not run.

    Every stage that weighs the panel gets this. A gap nobody on the panel
    caught is worthless if the editor never reads it, and an absent block
    reads as "there were no gaps" rather than "nothing looked" — so say
    which.
    """
    text = (state.get("panel_gaps") or "").strip()
    return text or "(no gap audit was run for this manuscript)"


def _debate_so_far(state: ReviewState) -> str:
    turns = state.get("debate", [])
    if not turns:
        return "(no prior turns)"
    return "\n\n".join(f"[{t['role']} r{t['round']}] {t['content']}" for t in turns)


def make_debate_node(role: str, stance: str):
    def node(state: ReviewState) -> dict:
        with node_context(role, run_id=state["config"].get("run_id", "")):
            config = state["config"]
            llm = make_llm(config, agent=f"debate_{role.lower()}", default_tag="debate")
            rnd = state.get("debate_round", 0) + 1
            system = (
                f"You are the {role} in an editorial debate about whether to accept a "
                f"manuscript. {stance} Argue concisely (≤250 words) and ground every "
                "claim in specific text from the manuscript above (quote sections or "
                "figures by name) and in the specialist reviews — argue FROM the panel's "
                "findings and the primary text; do not invent new findings of your own. "
                "Engage directly with the other side's MOST RECENT argument rather than "
                "restating your own opening, and concede any point they have genuinely "
                "established — in a debate, credibility comes from picking real battles, "
                "not from defending the indefensible. Write your complete turn "
                "as ordinary Markdown; no JSON or fixed headings are required."
            )
            user = (
                f"Reviewer findings:\n{_reports_digest(state)}\n\n"
                f"Debate so far:\n{_debate_so_far(state)}\n\n"
                f"Make your argument for this round (round {rnd})."
            )
            try:
                # Use the exact manuscript + directive prefix already warmed
                # by the desk and panel. A bare-manuscript variant creates a
                # second large cache entry for no semantic benefit.
                result = invoke_markdown(
                    llm,
                    config,
                    system,
                    user,
                    cached_prefix=context_block(state),
                    min_chars=80,
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
