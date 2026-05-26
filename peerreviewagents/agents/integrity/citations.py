"""Citation-hygiene + claim-support auditor (integrity panel).

Differs from the generic make_integrity_node factory in two ways: it loads
research tools (for abstract lookup) and it keeps the References section
verbatim instead of letting body truncation eat it.
"""

from __future__ import annotations

from ..utils.agent_states import ReviewState
from ..utils.agent_utils import fit_manuscript, parse_report, run_agent
from ..utils.llm import make_llm

_NAME = "citations"
_ROLE = "Citation Verification Auditor"

_TEMPLATE = """Write Markdown with sections:

## Summary
One paragraph on overall citation hygiene and how well references back claims.

## Strengths
- bullets (well-supported claims, complete reference list, accurate attributions)

## Weaknesses
- For each issue, quote the manuscript sentence and the citation token verbatim.
- Tag every weakness with one of: [MISSING-REF], [UNUSED-REF], [UNSUPPORTED-CLAIM], [MISATTRIBUTION].

## Assessment
Score: <1-5>
Confidence: <1-5>
"""


def node(state: ReviewState) -> dict:
    config = state["config"]
    references = (state.get("sections") or {}).get("references", "").strip()

    if not references:
        report_md = (
            "## Summary\nNo References section was detected in the manuscript.\n\n"
            "## Strengths\n- (none — no references to evaluate)\n\n"
            "## Weaknesses\n- [MISSING-REF] No References section is present, so all "
            "in-text citations are unmatched by definition.\n\n"
            "## Assessment\nScore: 2\nConfidence: 5\n"
        )
        return {"integrity_findings": [parse_report(report_md, _NAME)]}

    llm = make_llm(config, depth="quick")
    tools = []
    if config.get("research_enabled"):
        from ...research.tools import get_research_tools

        tools = get_research_tools(config)

    body = fit_manuscript(state)

    _tool_hint = (
        f" (available tools: {', '.join(t.name for t in tools)})"
        if tools
        else ""
    )
    system = (
        f"You are the {_ROLE} on the research-integrity panel. Your job has two parts: "
        "(1) CITATION HYGIENE — work through the manuscript body and enumerate every "
        "in-text citation token you find (numeric `[1]`/`[1,2]`/`[1-3]`, superscripts "
        "`^1`/`^{1,2}`, author-year `(Smith et al., 2020)` or narrative `Smith et al. "
        "(2020)`, and any journal-specific variants). For each, decide whether it has "
        "a matching entry in the References section provided below. Also list any "
        "References entry that is never cited. "
        "(2) CLAIM SUPPORT — pick the load-bearing cited claims (results, prior-art "
        "comparisons, mechanism statements) and use research tools"
        f"{_tool_hint} to fetch the cited paper's abstract; "
        "judge whether the abstract supports the surrounding sentence. "
        "Be specific: quote sentences, name citation tokens. Don't fabricate issues."
    )

    user = (
        f"Draft recommendation: {state.get('draft_recommendation')}\n\n"
        f"Meta-review (context only):\n{(state.get('meta_review') or '')[:2000]}\n\n"
        f"=== REFERENCES (verbatim, complete) ===\n{references}\n=== END REFERENCES ===\n\n"
        f"=== MANUSCRIPT BODY ===\n{body}\n=== END BODY ===\n\n"
        f"{_TEMPLATE}"
    )

    try:
        text = run_agent(llm, system, user, tools)
        return {"integrity_findings": [parse_report(text, _NAME)]}
    except Exception as exc:  # noqa: BLE001
        return {"errors": [f"{_NAME} integrity check failed: {exc}"]}


node.__name__ = f"integrity_{_NAME}"
