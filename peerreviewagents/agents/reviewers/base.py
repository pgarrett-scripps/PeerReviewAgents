"""Builder for specialist reviewer nodes.

A reviewer reads the manuscript, optionally consults research tools, and emits a
structured markdown report that parse_report() turns into a ReviewReport.
"""

from __future__ import annotations

from ..utils.agent_states import ReviewState
from ..utils.agent_utils import fit_manuscript, parse_report, run_agent
from ..utils.llm import make_llm

_REPORT_TEMPLATE = """Write your review in GitHub-flavored Markdown with EXACTLY these sections:

## Summary
One paragraph: what the paper claims and your overall take from a {role} standpoint.

## Strengths
- bullet points

## Weaknesses
- bullet points (be specific; cite sections/figures/claims)

## Questions
- bullet points the authors must address

## Assessment
Score: <1-5, where 1=reject, 3=major revision, 4=minor revision, 5=accept>
Confidence: <1-5>

Focus strictly on your specialty as a {role}. Do not rehash unrelated aspects."""


def make_reviewer_node(name: str, role: str, mandate: str, uses_research: bool = False):
    def node(state: ReviewState) -> dict:
        config = state["config"]
        llm = make_llm(config, depth="quick")
        tools = []
        if uses_research and config.get("research_enabled"):
            from ...research.tools import get_research_tools

            tools = get_research_tools(config)

        system = (
            f"You are the {role} on a journal peer-review panel. {mandate} "
            "You are rigorous, fair, and constructive. Ground critiques in specific "
            "evidence from the manuscript."
        )
        manuscript = fit_manuscript(state)
        user = (
            f"Manuscript title: {state.get('manuscript_title', 'Untitled')}\n\n"
            f"=== MANUSCRIPT ===\n{manuscript}\n=== END ===\n\n"
            + _REPORT_TEMPLATE.format(role=role)
        )
        try:
            text = run_agent(llm, system, user, tools)
            return {"reports": [parse_report(text, name)]}
        except Exception as exc:  # noqa: BLE001
            return {"errors": [f"{name} reviewer failed: {exc}"]}

    node.__name__ = f"reviewer_{name}"
    return node
