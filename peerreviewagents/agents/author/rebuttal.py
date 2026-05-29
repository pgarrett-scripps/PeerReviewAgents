"""Author rebuttal: the manuscript author defending their work.

Real peer review has an author-response phase, and without one every
reviewer critique looks equally fatal: there's nothing distinguishing
"fixable in revision" from "the reviewer misread the paper" from
"actual blocker." This node closes that gap with a single LLM call:
the model plays the author, reads the panel's critiques, and writes a
focused rebuttal that the editor consumes alongside the meta-review.
"""

from __future__ import annotations

from ...observability import node_context
from ..debate.base import _debate_so_far, _reports_digest
from ..utils.agent_states import ReviewState
from ..utils.agent_utils import manuscript_block, run_agent, score_summary
from ..utils.llm import make_llm

_SYS = (
    "You are the author of the manuscript responding to a panel of "
    "specialist reviewers. Write a focused, professional rebuttal that "
    "clearly distinguishes critiques you would address by revision from "
    "critiques you push back on. Two rules: (1) when you disagree, quote "
    "the specific manuscript section or figure that addresses the "
    "reviewer's concern — don't just assert; (2) be honest about "
    "load-bearing critiques. If a critique is fatal, say so. The editor "
    "values an author who can tell the difference between fixable and "
    "fundamental more than one who defends everything."
)


def node(state: ReviewState) -> dict:
    with node_context("author_rebuttal"):
        return _run(state)


def _run(state: ReviewState) -> dict:
    llm = make_llm(state["config"], reasoning_effort="high")
    user = (
        f"Reviewer findings:\n{_reports_digest(state)}\n\n"
        f"Debate transcript:\n{_debate_so_far(state)}\n\n"
        f"Meta-review draft:\n{state.get('meta_review', '')}\n\n"
        f"Numerical signal:\n{score_summary(state)}\n\n"
        "Write a markdown rebuttal with EXACTLY these sections:\n\n"
        "## Concessions\n"
        "Critiques you would address by revision. For each: name the "
        "reviewer who raised it, summarize their point in one line, and "
        "describe the concrete change you would make.\n\n"
        "## Disagreements\n"
        "Critiques you push back on. For each: name the reviewer who "
        "raised it, explain why the manuscript already addresses it, "
        "and quote the specific section/figure/passage that does so.\n\n"
        "## Load-bearing critiques\n"
        "The 1-3 critiques that, if upheld, make acceptance impossible "
        "in this revision cycle. Be candid — name them even when "
        "uncomfortable. If none, say so explicitly.\n"
    )
    try:
        # Author needs primary-source access to defend specific quotes;
        # cached_prefix is byte-identical to the reviewer block so this
        # lands a prompt-cache hit.
        result = run_agent(
            llm,
            _SYS,
            user,
            cached_prefix=manuscript_block(state),
        )
    except Exception as exc:  # noqa: BLE001
        return {
            "errors": [f"author_rebuttal failed: {exc}"],
            "author_rebuttal": "",
        }
    return {
        "author_rebuttal": result.text,
        "total_cost": result.cost,
    }


node.__name__ = "author_rebuttal"
