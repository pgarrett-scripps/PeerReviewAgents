"""Editor-in-Chief: final decision + author-facing decision letter."""

from __future__ import annotations

from ..utils.agent_states import ReviewState
from ..utils.agent_utils import run_agent
from ..utils.llm import make_llm

_VALID = ("accept", "minor", "major", "reject")

_SYS = (
    "You are the Editor-in-Chief. Using the meta-review, the integrity panel, and the "
    "draft recommendation, make the FINAL decision and write a professional, "
    "constructive decision letter to the authors."
)


def node(state: ReviewState) -> dict:
    config = state["config"]
    llm = make_llm(config, depth="deep")
    integrity = "\n\n".join(
        f"### {f['reviewer']}\n{f['summary']}\nIssues: {'; '.join(f['weaknesses'][:5]) or 'none'}"
        for f in state.get("integrity_findings", [])
    )
    verdict_line = (
        "Begin the letter with a line `DECISION: <accept|minor|major|reject>`.\n"
        if config.get("emit_verdict", True)
        else "Do not state an explicit accept/reject verdict.\n"
    )
    user = (
        f"Draft recommendation: {state.get('draft_recommendation')}\n\n"
        f"Meta-review:\n{state.get('meta_review', '')}\n\n"
        f"Integrity panel:\n{integrity}\n\n"
        f"{verdict_line}"
        "Then write the decision letter in Markdown with sections:\n"
        "## Decision Letter\n## Summary of Evaluation\n"
        "## Required Revisions (numbered, prioritized, actionable)\n"
        "## Minor Suggestions\n"
    )
    try:
        text = run_agent(llm, _SYS, user)
    except Exception as exc:  # noqa: BLE001
        # Do NOT fabricate a verdict on failure — leave decision empty so the
        # caller knows the editor never rendered one.
        return {"errors": [f"editor failed: {exc}"], "decision": "", "decision_letter": ""}
    return {"decision": _extract_decision(text, state), "decision_letter": text}


def _extract_decision(text: str, state: ReviewState) -> str:
    for line in text.splitlines():
        if line.strip().lower().startswith("decision:"):
            for v in _VALID:
                if v in line.lower():
                    return v
    # No DECISION line in the LLM output — fall back to the meta-reviewer's
    # draft only if it produced a valid verdict; otherwise return empty so
    # downstream code can treat this run as failed.
    draft = state.get("draft_recommendation", "")
    return draft if draft in _VALID else ""
