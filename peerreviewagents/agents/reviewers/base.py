"""Builder for specialist reviewer nodes.

A reviewer reads the manuscript, optionally consults research tools, and
returns a markdown report with a YAML frontmatter block carrying score +
confidence. The manuscript block is sent with prompt-cache markup so the
parallel reviewer fan-out (and any re-run on the same manuscript) shares
one provider-side cache entry.
"""

from __future__ import annotations

from ...observability import node_context
from ..utils.agent_states import ReviewReport, ReviewState
from ..utils.agent_utils import (
    coerce_int,
    manuscript_block,
    run_agent,
    split_frontmatter,
)
from ..utils.llm import make_llm

_INSTRUCTIONS = (
    "Manuscript title: {title}\n\n"
    "You are the {role} on a journal peer-review panel. {mandate} You are "
    "rigorous, fair, and constructive. Ground critiques in specific "
    "evidence from the manuscript above.\n\n"
    "Output a complete markdown review document. Begin with a YAML "
    "frontmatter block carrying your scores (integers 1-5):\n\n"
    "---\n"
    "score: <1=reject, 3=major revision, 4=minor revision, 5=accept>\n"
    "confidence: <1-5>\n"
    "---\n"
    "# {role}\n\n"
    "## Summary\n"
    "One-paragraph overall take from your specialty.\n\n"
    "## Strengths\n"
    "- bullet sentences\n\n"
    "## Weaknesses\n"
    "- bullet sentences grounded in specific manuscript evidence\n\n"
    "## Questions\n"
    "- bullet sentences\n\n"
    "Focus strictly on your specialty. Do not rehash unrelated aspects. "
    "Emit the frontmatter exactly as shown — score and confidence must "
    "be integers, one per line."
)


def make_reviewer_node(name: str, role: str, mandate: str, uses_research: bool = False):
    node_name = f"reviewer_{name}"

    def node(state: ReviewState) -> dict:
        with node_context(node_name):
            config = state["config"]
            llm = make_llm(config)
            tools = []
            if uses_research:
                from ...research.tools import get_research_tools

                tools = get_research_tools(config)

            system = (
                "You are a specialist on a journal peer-review editorial panel. "
                "Your role is given in the user message; follow it strictly. "
                "Always produce a markdown document with a YAML frontmatter "
                "block containing score and confidence."
            )
            instructions = _INSTRUCTIONS.format(
                title=state.get("manuscript_title", "Untitled"),
                role=role,
                mandate=mandate,
            )
            try:
                result = run_agent(
                    llm,
                    system,
                    instructions,
                    tools,
                    cached_prefix=manuscript_block(state),
                )
                meta, _ = split_frontmatter(result.text)
                score = coerce_int(meta.get("score"), default=3, lo=1, hi=5)
                confidence = coerce_int(meta.get("confidence"), default=3, lo=1, hi=5)
                report: ReviewReport = {
                    "reviewer": name,
                    "score": float(score),
                    "confidence": float(confidence),
                    "body": result.text,
                }
                return {"reports": [report], "total_cost": result.cost}
            except Exception as exc:  # noqa: BLE001
                return {"errors": [f"{name} reviewer failed: {exc}"]}

    node.__name__ = node_name
    return node
