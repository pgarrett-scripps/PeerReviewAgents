"""Builder for integrity-panel nodes (the 'risk management' analog)."""

from __future__ import annotations

from ..utils.agent_states import ReviewState
from ..utils.agent_utils import fit_manuscript, parse_report, run_agent
from ..utils.llm import make_llm

_TEMPLATE = """Write Markdown with sections:
## Summary
## Strengths
- (what the manuscript does well on this dimension)
## Weaknesses
- (concrete risks/issues)
## Assessment
Score: <1-5>
Confidence: <1-5>
"""


def make_integrity_node(name: str, role: str, mandate: str):
    def node(state: ReviewState) -> dict:
        llm = make_llm(state["config"], depth="quick")
        system = (
            f"You are the {role} on the research-integrity panel. {mandate} Assess the "
            "draft recommendation for hidden risks before the editor finalizes."
        )
        manuscript = fit_manuscript(state)
        user = (
            f"Draft recommendation: {state.get('draft_recommendation')}\n\n"
            f"Meta-review:\n{state.get('meta_review', '')[:4000]}\n\n"
            f"=== MANUSCRIPT ===\n{manuscript}\n=== END ===\n\n{_TEMPLATE}"
        )
        try:
            text = run_agent(llm, system, user)
            return {"integrity_findings": [parse_report(text, name)]}
        except Exception as exc:  # noqa: BLE001
            return {"errors": [f"{name} integrity check failed: {exc}"]}

    node.__name__ = f"integrity_{name}"
    return node
