"""Debate synthesizer: condense the finished debate for the editor.

Runs AFTER the parallel debate rounds. The editor deliberately does not
read the raw multi-round transcript — it grows with every round, and the
point of a bounded editorial record is that the decision context does not.
This node reads the complete panel and the full exchange and writes the
one account of the debate the editor sees; the raw transcript is still
published beside it.

Governed by a word budget rather than an issue cap: a fixed item count
amputates a broad debate, while a budget forces prioritization without
deciding in advance how many issues the manuscript has.
"""

from __future__ import annotations

from ...observability import node_context
from ..debate.base import _debate_so_far, _reports_digest
from ..utils.agent_states import ReviewState
from ..utils.agent_utils import directives_block
from ..utils.llm import make_llm
from ..utils.structured import invoke_markdown

_SYS = (
    "You are the Area Chair condensing a finished editorial debate for the "
    "Editor-in-Chief, who will read the full specialist reports and audits "
    "alongside your brief but NOT the debate transcript — your brief is the "
    "only account of the debate the editor sees, so nothing decision-relevant "
    "may be dropped. For every substantive issue the debate engaged: state "
    "the issue and the manuscript evidence cited, the strongest case each "
    "side made, what was conceded, and where it stands — resolved, "
    "unresolved, or fatal if upheld. Also record concerns from the reports "
    "that the debate never engaged, so silence is not read as resolution. "
    "Cluster substantively duplicate criticisms even when reviewers use "
    "different wording; repetition by agents sharing one underlying model is "
    "not independent corroboration, and you must say so where it occurs. "
    "Preserve genuine disagreements rather than averaging them. The panel "
    "and the debaters were given the same venue description you were, so "
    "venue selectivity is not a fresh argument for escalating an issue. Do "
    "not invent new findings, do not decide the paper, do not recommend a "
    "verdict, and do not repeat scores or vote counts. Write concise "
    "ordinary Markdown."
)


def node(state: ReviewState) -> dict:
    with node_context("debate_synthesizer", run_id=state["config"].get("run_id", "")):
        return _run(state)


def _run(state: ReviewState) -> dict:
    config = state["config"]
    llm = make_llm(config, agent="debate_synthesizer", default_tag="synthesis")
    try:
        budget = int(config.get("synthesis_word_budget") or 1200)
    except (TypeError, ValueError):
        budget = 1200
    user = (
        f"Reviewer findings:\n{_reports_digest(state)}\n\n"
        f"Complete debate transcript:\n{_debate_so_far(state)}\n\n"
        "Produce the post-debate synthesis for the editor. The complete "
        f"response must be {budget} words or fewer."
    )
    try:
        # The synthesizer weighs the panel and the debate, NOT the primary
        # text — feeding it the full manuscript invites it to become another
        # reviewer instead of an account of the exchange. Only the
        # venue/strictness directives ride along as the cached prefix so it
        # can calibrate to the target venue's bar.
        result = invoke_markdown(
            llm,
            config,
            _SYS,
            user,
            cached_prefix=directives_block(state),
            min_chars=100,
        )
    except Exception as exc:  # noqa: BLE001
        # No fabricated content on failure. The editor's prompt falls back
        # to the raw transcript when this marker is all it has, so a failed
        # synthesizer costs condensation, not the debate.
        return {
            "errors": [f"debate_synthesizer failed: {exc}"],
            "debate_synthesis": f"(the debate synthesizer did not run: {exc})",
        }

    return {
        "debate_synthesis": result.text,
        "total_cost": result.cost,
    }
