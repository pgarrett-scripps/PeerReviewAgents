"""Builder for debate nodes (Advocate / Skeptic).

The two debaters run in PARALLEL within a round. In round 1 each argues its
case from the manuscript and the specialist reports alone, blind to the
other side, so neither position is framed as a reaction to the other. From
round 2 on, each also reads the complete previous exchange and must engage
with it. The round counter is advanced by the graph's join node after both
turns land — a debater never advances it, because with parallel writers a
per-node increment would race.

Each debater emits ordinary Markdown. That exact response is stored on the
DebateTurn ``content`` field and passed to the next round, the debate
synthesizer, and the published transcript.
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


def _sorted_turns(state: ReviewState) -> list:
    """Debate turns in (round, advocate-first) order.

    Within a round the two turns are written concurrently, so the raw list
    order depends on scheduling. Every rendering of the transcript sorts,
    so the published artifact and each prompt are deterministic.
    """
    order = {"advocate": 0, "skeptic": 1}
    return sorted(
        state.get("debate", []),
        key=lambda t: (t.get("round", 0), order.get(t.get("role", ""), 2)),
    )


def _debate_so_far(state: ReviewState) -> str:
    turns = _sorted_turns(state)
    if not turns:
        return "(no prior turns)"
    return "\n\n".join(f"[{t['role']} r{t['round']}] {t['content']}" for t in turns)


# The five specialists may share one underlying model, so the same worry
# surfacing in several reports is one concern repeated, not several
# independent confirmations. This used to live in the pre-debate curator's
# prompt; with that stage gone it rides with every debater directly.
_CORROBORATION_GUARD = (
    "The specialist reports may share one underlying model: a concern "
    "repeated across reports is one concern repeated, not independent "
    "corroboration, and you must not count report multiplicity as evidence. "
)


def make_debate_node(role: str, stance: str):
    def node(state: ReviewState) -> dict:
        with node_context(role, run_id=state["config"].get("run_id", "")):
            config = state["config"]
            llm = make_llm(config, agent=f"debate_{role.lower()}", default_tag="debate")
            rnd = state.get("debate_round", 0) + 1
            if rnd == 1:
                engagement = (
                    "This is the OPENING round: the other side argues in "
                    "parallel and you cannot see them. Build your own case "
                    "from the panel's findings and the manuscript — do not "
                    "anticipate, quote, or argue with an opponent you have "
                    "not read. "
                )
                prior_block = ""
            else:
                engagement = (
                    "Engage directly with the other side's MOST RECENT "
                    "argument rather than restating your own opening, and "
                    "concede any point they have genuinely established — in "
                    "a debate, credibility comes from picking real battles, "
                    "not from defending the indefensible. "
                )
                prior_block = f"Debate so far:\n{_debate_so_far(state)}\n\n"
            system = (
                f"You are the {role} in an editorial debate about whether to accept a "
                f"manuscript. {stance} Argue concisely (≤250 words) and ground every "
                "claim in specific text from the manuscript above (quote sections or "
                "figures by name) and in the specialist reviews — argue FROM the panel's "
                "findings and the primary text; do not invent new findings of your own. "
                f"{_CORROBORATION_GUARD}{engagement}Write your complete turn "
                "as ordinary Markdown; no JSON or fixed headings are required."
            )
            user = (
                "Primary reviewer findings:\n"
                f"{_reports_digest(state)}\n\n"
                f"{prior_block}"
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
                # The join node advances the round whether or not a turn
                # landed, so a failed debater costs its turn, not the run.
                return {"errors": [f"{role} failed: {exc}"]}

            return {
                "debate": [{"role": role, "round": rnd, "content": result.text}],
                "total_cost": result.cost,
            }

    node.__name__ = f"debate_{role}"
    return node
