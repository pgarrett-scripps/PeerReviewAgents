"""Builder for editorial compliance-auditor nodes.

An auditor runs a specific, enumerable checklist against the manuscript and
returns an :class:`~peerreviewagents.agents.schemas.AuditOutput` (summary,
categories checked, per-item findings). Unlike a specialist reviewer it
assigns **no score** and emits **no opinion** — it reports which required
identifiers/details are present, missing, or unverifiable.

Auditors fan out in parallel with the reviewer panel but write to the
separate ``audits`` state channel, which feeds only the Editor-in-Chief.
They never enter the confidence-weighted panel score, the advocate/skeptic
debate, or the meta-review — those reconcile scientific-merit opinions,
which a factual checklist is not.

The manuscript block is sent with prompt-cache markup (on providers that
support it) so auditors share the same provider-side cache entry as the
reviewer fan-out.
"""

from __future__ import annotations

from ...observability import node_context
from ..schemas import AuditOutput
from ..utils.agent_states import AuditReport, ReviewState
from ..utils.agent_utils import context_block, supplement_block
from ..utils.llm import make_llm
from ..utils.structured import (
    invoke_structured,
    invoke_structured_after_tools,
)

_INSTRUCTIONS = (
    "Manuscript title: {title}\n\n"
    "You are the {title_role} on a journal's editorial staff. {mandate}\n\n"
    "Method: first detect which checklist categories are actually in play "
    "(their trigger appears in the manuscript), then check ONLY the items for "
    "those categories. Do not invent gaps for techniques the paper never uses. "
    "Be specific and conservative.\n\n"
    "Severity rule — HARD vs SOFT:\n"
    "  - HARD = 'could a competent lab obtain the exact same input / rerun "
    "this?' If the trigger is present and the identifier is absent, the work "
    "is literally non-repeatable. Mark status=missing.\n"
    "  - SOFT = 'could they run it under the same conditions?' Recommended "
    "but not blocking.\n"
    "When something is referenced rather than written out and you cannot "
    "confirm it from the manuscript alone, mark status=unverifiable (a "
    "question to the authors) — do NOT assume it passes, and do NOT mark it a "
    "HARD missing.\n\n"
    "Return the structured AuditOutput schema: a factual summary, the "
    "categories you detected, and one finding per checked item. You assign no "
    "score and make no accept/reject judgment — that is the editor's job."
)

# Appended to the instructions only when a supplement is actually present and
# the auditor opted in, so the model knows to mine it and attribute evidence.
_SUPPLEMENT_NOTE = (
    "\n\nA SUPPLEMENTARY INFORMATION block is included above, after the "
    "manuscript. Reagent tables, key-resources tables, full protocols, and "
    "detailed methods often live there — check it as well as the main text. "
    "When an identifier is found in (or missing from) the SI, say so in the "
    "evidence field, e.g. '(SI — Key Resources Table)'."
)

_SYSTEM = (
    "You are a compliance auditor on a journal's editorial staff. You do NOT "
    "judge scientific merit, quality, or novelty, and you assign NO score. "
    "You run a specific, enumerable checklist and report which required "
    "identifiers and details are present, missing, or unverifiable, each "
    "grounded in manuscript evidence. Your specific remit is in the user "
    "message; follow it strictly. Return the structured AuditOutput schema."
)


def make_auditor_node(
    name: str,
    title: str,
    mandate: str,
    *,
    tool_names: list[str] | None = None,
    needs_supplement: bool = False,
):
    """Build a LangGraph node for one editorial auditor.

    ``title`` is the human-facing name used in the rendered report and the
    prompt. ``tool_names`` lists research tools the auditor may call (e.g. to
    resolve a citation); pass ``None`` for a tool-free auditor.
    ``needs_supplement`` opts this auditor into receiving the full
    supplementary-information block (when one was provided) appended after the
    manuscript in its cached prefix.
    """
    node_name = f"audit_{name}"
    bound_tool_names = list(tool_names or [])

    def node(state: ReviewState) -> dict:
        with node_context(node_name, run_id=state["config"].get("run_id", "")):
            config = state["config"]
            llm = make_llm(config, agent=node_name, default_tag="audit")
            instructions = _INSTRUCTIONS.format(
                title=state.get("manuscript_title", "Untitled"),
                title_role=title,
                mandate=mandate,
            )
            cached_prefix = context_block(state)
            # Opt-in agents get the full SI appended after the manuscript.
            # No-op when no SI was provided, so the prefix is unchanged.
            if needs_supplement:
                supplement = supplement_block(state)
                if supplement:
                    cached_prefix = f"{cached_prefix}\n\n{supplement}"
                    instructions = instructions + _SUPPLEMENT_NOTE

            try:
                if bound_tool_names and config.get("research_enabled", True):
                    from ...research.tools import get_tools_by_name

                    result = invoke_structured_after_tools(
                        llm,
                        AuditOutput,
                        config,
                        _SYSTEM,
                        instructions,
                        get_tools_by_name(bound_tool_names, config),
                        cached_prefix=cached_prefix,
                    )
                else:
                    result = invoke_structured(
                        llm,
                        AuditOutput,
                        config,
                        _SYSTEM,
                        instructions,
                        cached_prefix=cached_prefix,
                    )
            except Exception as exc:  # noqa: BLE001
                return {"errors": [f"{name} auditor failed: {exc}"]}

            output: AuditOutput = result.instance  # type: ignore[assignment]
            report: AuditReport = {
                "auditor": name,
                "title": title,
                "hard_gaps": output.hard_gaps(),
                "soft_gaps": output.soft_gaps(),
                "body": output.to_markdown(title=title),
            }
            return {"audits": [report], "total_cost": result.cost}

    node.__name__ = node_name
    return node
