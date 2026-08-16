"""Journal recommender node.

Takes the manuscript, the panel's verdict, and the editor's required
revisions; returns a tiered set of venue
suggestions (as-is / after-revision / alternative) as Markdown. LLM knowledge
only — no literature search — so this adds one LLM
call and no external API dependencies.
"""

from __future__ import annotations

from ...observability import node_context
from ..utils.agent_states import ReviewState
from ..utils.agent_utils import context_block, score_summary
from ..utils.llm import make_llm
from ..utils.structured import invoke_markdown

_SYS = (
    "You are advising a manuscript's authors on which venues to submit to, "
    "given the editorial panel's verdict. Be realistic: don't suggest top "
    "venues for manuscripts the panel found weak, and don't pigeonhole "
    "strong work into low-tier outlets. Tie each venue's fit to the paper's "
    "actual topic, scope, and methodology — not generic prestige. You work "
    "from model knowledge only (no venue lookup), so prefer well-established "
    "outlets and flag any fit uncertainty in `notes` rather than inventing a "
    "precise match or a venue that may not exist. Use venue names exactly as "
    "authors would write them (e.g. 'Nature Methods', 'JMLR', 'Bioinformatics'). "
    "Write ordinary Markdown with at most 3 venues per bucket; fewer is fine "
    "when you don't have good candidates. No JSON or fixed schema is required."
)


def node(state: ReviewState) -> dict:
    with node_context("journal_recommender", run_id=state["config"].get("run_id", "")):
        return _run(state)


def _run(state: ReviewState) -> dict:
    config = state["config"]
    llm = make_llm(config, agent="journal_recommender", default_tag="synthesis")

    reviewer_names = ", ".join(r.get("reviewer", "?") for r in state.get("reports") or [])
    decision = state.get("decision") or "(no decision)"
    decision_letter = state.get("decision_letter") or "(no decision letter)"

    journal = (state.get("journal_block") or "").strip()
    target_block = (
        f"The authors' intended target venue:\n{journal}\n\n"
        "Use the as_is / after_revision buckets to judge the manuscript's "
        "prospects AT THIS TARGET (and comparable venues); use alternative "
        "for fallbacks if the target is out of reach.\n\n"
    ) if journal else ""

    user = (
        f"Manuscript title: {state.get('manuscript_title', 'Untitled')}\n\n"
        "The full manuscript is provided above; judge venue fit (topic, "
        "scope, methodology, results depth) against it.\n\n"
        f"{target_block}"
        f"Reviewer panel: {reviewer_names}\n"
        f"Numerical signal:\n{score_summary(state)}\n\n"
        f"Editor's final decision: {decision}\n\n"
        f"Editor's decision letter:\n{decision_letter}\n\n"
        "Produce three buckets of venue suggestions:\n"
        "  - as_is: where this paper is realistic at its current quality (matching "
        "the editor's verdict). If the verdict is 'reject', leave this empty.\n"
        "  - after_revision: where it becomes realistic after the required revisions "
        "in the decision letter are addressed.\n"
        "  - alternative: fallback outlets (preprint server, workshop, narrower "
        "specialty journal) if the paper can't reach the headline venues.\n\n"
        "For each suggestion, be specific about why the venue fits the topic AND "
        "be candid about acceptance odds at that venue given the verdict."
    )
    try:
        # Reuse the exact prefix already warmed by the desk, panel and debate.
        result = invoke_markdown(
            llm,
            config,
            _SYS,
            user,
            cached_prefix=context_block(state),
            min_chars=100,
        )
    except Exception as exc:  # noqa: BLE001
        return {
            "errors": [f"journal_recommender failed: {exc}"],
            "journal_recommendations": "",
        }

    return {
        "journal_recommendations": result.text,
        "total_cost": result.cost,
    }


node.__name__ = "journal_recommender"
