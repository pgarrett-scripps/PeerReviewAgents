"""Builder for specialist reviewer nodes.

A reviewer reads the manuscript, optionally consults research tools, and
returns a :class:`ReviewerOutput` (score, confidence, summary, strengths,
weaknesses, questions). The rendered markdown body that lands on disk
is produced by ``ReviewerOutput.to_markdown(role)`` — so the structured
fields are the single source of truth and nothing parses YAML
frontmatter back out of the body.

The manuscript block is sent with prompt-cache markup (on providers
that support it) so the parallel reviewer fan-out shares one
provider-side cache entry.
"""

from __future__ import annotations

from ...observability import node_context
from ..schemas import ReviewerOutput
from ..utils.agent_states import ReviewReport, ReviewState
from ..utils.agent_utils import context_block
from ..utils.llm import make_llm
from ..utils.structured import (
    invoke_structured,
    invoke_structured_after_tools,
)

_INSTRUCTIONS = (
    "Manuscript title: {title}\n\n"
    "You are the {role} on a journal peer-review panel. {mandate} You are "
    "rigorous, fair, and constructive. Ground every critique in specific "
    "evidence from the manuscript above; critique only what the manuscript "
    "actually says — do not invent weaknesses, fabricate missing results, or "
    "assume claims the text does not make. When something is genuinely absent "
    "or unclear, raise it as a question rather than asserting it as a flaw. "
    "Your specialty mandate below "
    "distinguishes HARD issues from SOFT ones. A HARD issue — where a claim, "
    "method, or figure is genuinely unsupported, ambiguous, or non-compliant "
    "as worded — belongs in BOTH your weaknesses and your questions; quote "
    "the specific sentence, figure, or value you are flagging. A SOFT issue — "
    "friction or a fixable improvement that does not undermine the work — is a "
    "minor weakness. Reviewers run in parallel and never see each other's "
    "reports, so if you spot an issue that belongs to another specialty, name "
    "it in one line and attribute it to that reviewer rather than re-deciding "
    "it yourself or dropping it silently. If a target journal is described "
    "above, judge the manuscript against that venue's scope, standards, "
    "and submission limits, and flag misfits (out-of-scope, over-length, "
    "too many display items) where relevant to your specialty. If a review "
    "strictness standard is described above, calibrate your score and how "
    "heavily you weigh weaknesses to that standard.\n\n"
    "Return a structured review with the following fields:\n"
    "  - score (int 1-5): 1=reject, 3=major revision, 4=minor revision, 5=accept\n"
    "  - confidence (int 1-5): certainty in your score — 5=squarely your "
    "expertise with clear manuscript evidence; 3=reasonable read but some "
    "ambiguity; 1-2=outside your subarea or the manuscript is too unclear to "
    "judge. Lower your confidence rather than guessing.\n"
    "  - summary: one-paragraph overall take from your specialty\n"
    "  - strengths: bullet sentences naming strengths\n"
    "  - weaknesses: bullet sentences naming weaknesses with manuscript evidence\n"
    "  - questions: bullet questions for the authors\n\n"
    "Focus strictly on your specialty. Do not rehash unrelated aspects."
)

_SYSTEM = (
    "You are a specialist on a journal peer-review editorial panel. "
    "Your role is given in the user message; follow it strictly. "
    "Return your verdict as the structured ReviewerOutput schema."
)


def make_reviewer_node(
    name: str,
    role: str,
    mandate: str,
    *,
    tool_names: list[str] | None = None,
):
    """Build a LangGraph node for one specialist reviewer.

    ``tool_names`` is a list of logical research-tool names this reviewer
    should call (see :mod:`peerreviewagents.research.tools` for the
    registry). Pass ``None`` (default) for a tool-free reviewer.
    """
    node_name = f"reviewer_{name}"
    bound_tool_names = list(tool_names or [])

    def node(state: ReviewState) -> dict:
        with node_context(node_name):
            config = state["config"]
            llm = make_llm(config, agent=node_name, default_tag="reviewer")
            instructions = _INSTRUCTIONS.format(
                title=state.get("manuscript_title", "Untitled"),
                role=role,
                mandate=mandate,
            )
            cached_prefix = context_block(state)

            try:
                if bound_tool_names:
                    from ...research.tools import get_tools_by_name

                    result = invoke_structured_after_tools(
                        llm,
                        ReviewerOutput,
                        config,
                        _SYSTEM,
                        instructions,
                        get_tools_by_name(bound_tool_names, config),
                        cached_prefix=cached_prefix,
                    )
                else:
                    result = invoke_structured(
                        llm,
                        ReviewerOutput,
                        config,
                        _SYSTEM,
                        instructions,
                        cached_prefix=cached_prefix,
                    )
            except Exception as exc:  # noqa: BLE001
                return {"errors": [f"{name} reviewer failed: {exc}"]}

            output: ReviewerOutput = result.instance  # type: ignore[assignment]
            report: ReviewReport = {
                "reviewer": name,
                "score": float(output.score),
                "confidence": float(output.confidence),
                "body": output.to_markdown(role=role),
            }
            return {"reports": [report], "total_cost": result.cost}

    node.__name__ = node_name
    return node
