"""Meta-reviewer / Area Chair: synthesize reviews + debate into a draft recommendation."""

from __future__ import annotations

import re

from ..debate.base import _debate_so_far, _reports_digest
from ..utils.agent_states import ReviewState
from ..utils.agent_utils import run_agent
from ..utils.llm import make_llm

_SYS = (
    "You are the Area Chair synthesizing a peer-review package. Weigh the specialist "
    "reviews (by score and confidence) and the advocate/skeptic debate into a single "
    "balanced meta-review. Be decisive but fair."
)


def node(state: ReviewState) -> dict:
    llm = make_llm(state["config"], depth="deep")
    user = (
        f"Reviewer findings:\n{_reports_digest(state)}\n\n"
        f"Debate transcript:\n{_debate_so_far(state)}\n\n"
        "Write a meta-review in Markdown with sections:\n"
        "## Synthesis (the consensus and key tensions)\n"
        "## Decisive Factors (what most drives the outcome)\n"
        "## Draft Recommendation (one of: accept | minor | major | reject, with rationale)\n"
    )
    try:
        text = run_agent(llm, _SYS, user)
    except Exception as exc:  # noqa: BLE001
        return {"errors": [f"meta_reviewer failed: {exc}"], "meta_review": "", "draft_recommendation": "major"}
    rec = _extract_rec(text)
    return {"meta_review": text, "draft_recommendation": rec}


def _extract_rec(text: str) -> str:
    low = text.lower()
    m = re.search(r"#+\s*draft\s+recommendation.*?$(.*?)(?=^#+\s|\Z)", low, re.IGNORECASE | re.MULTILINE | re.DOTALL)
    scope = m.group(1) if m else low
    for key in ("reject", "major", "minor", "accept"):
        if re.search(rf"\b{key}\b", scope):
            return key
    # Fall back to full text if section search yielded nothing.
    for key in ("reject", "major", "minor", "accept"):
        if re.search(rf"\b{key}\b", low):
            return key
    return "major"
