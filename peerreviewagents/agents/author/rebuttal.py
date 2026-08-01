"""Author rebuttal: the manuscript author defending their work.

Real peer review has an author-response phase, and without one every
reviewer critique looks equally fatal: there's nothing distinguishing
"fixable in revision" from "the reviewer misread the paper" from
"actual blocker." This node closes that gap with a single LLM call:
the model plays the author, reads the panel's critiques, and emits a
structured :class:`AuthorRebuttalOutput` (concessions, disagreements,
load-bearing critiques) that the editor consumes alongside the meta-review.
"""

from __future__ import annotations

from ...observability import node_context
from ..debate.base import _debate_so_far, _reports_digest
from ..schemas import AuthorRebuttalOutput
from ..utils.agent_states import ReviewState
from ..utils.agent_utils import manuscript_block, score_summary
from ..utils.llm import make_llm
from ..utils.structured import invoke_structured

_SYS = (
    "You are the author of the manuscript responding to a panel of "
    "specialist reviewers. Produce a focused, professional rebuttal that "
    "clearly distinguishes critiques you would address by revision from "
    "critiques you push back on. Rules: (1) when you disagree, quote the "
    "specific manuscript section or figure that addresses the reviewer's "
    "concern in `quoted_section` — don't just assert; (2) make each concession "
    "concrete — a `proposed_change` the editor could verify in a revised "
    "draft, not a vague 'we will clarify'; (3) be honest about load-bearing "
    "critiques — if a critique is genuinely fatal this cycle, name it. Respond "
    "to the critiques; do not re-argue the advocate/skeptic debate. Defending "
    "everything — no concessions and no load-bearing critiques — reads as bad "
    "faith and the editor will discount the whole rebuttal. The editor values "
    "an author who can tell fixable from fundamental more than one who defends "
    "everything. Return the structured AuthorRebuttalOutput schema."
)


def node(state: ReviewState) -> dict:
    with node_context("author_rebuttal", run_id=state["config"].get("run_id", "")):
        return _run(state)


def _run(state: ReviewState) -> dict:
    config = state["config"]
    llm = make_llm(config, agent="author_rebuttal", default_tag="synthesis", reasoning_effort="medium")
    user = (
        f"Reviewer findings:\n{_reports_digest(state)}\n\n"
        f"Debate transcript:\n{_debate_so_far(state)}\n\n"
        f"Meta-review draft:\n{state.get('meta_review', '')}\n\n"
        f"Numerical signal:\n{score_summary(state)}\n\n"
        "Compose your rebuttal."
    )
    try:
        # Author needs primary-source access to defend specific quotes;
        # cached_prefix is byte-identical to the reviewer block so this
        # lands a prompt-cache hit.
        result = invoke_structured(
            llm,
            AuthorRebuttalOutput,
            config,
            _SYS,
            user,
            cached_prefix=manuscript_block(state),
        )
    except Exception as exc:  # noqa: BLE001
        return {
            "errors": [f"author_rebuttal failed: {exc}"],
            "author_rebuttal": "",
        }
    output: AuthorRebuttalOutput = result.instance  # type: ignore[assignment]
    return {
        "author_rebuttal": output.to_markdown(),
        "total_cost": result.cost,
    }


node.__name__ = "author_rebuttal"
