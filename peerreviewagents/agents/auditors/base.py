"""Builder for editorial compliance-auditor nodes.

An auditor runs a specific, enumerable checklist against the manuscript and
returns an ordinary Markdown checklist. Unlike a specialist reviewer it
assigns **no score** and emits **no opinion** — it reports which required
identifiers/details are present, missing, or unverifiable.

Auditors fan out in parallel with the reviewer panel but write to the
separate ``audits`` state channel, which feeds only the Editor-in-Chief.
They never enter the confidence-weighted panel score, the advocate/skeptic
debate, or the synthesis — those reconcile scientific-merit opinions,
which a factual checklist is not.

The manuscript block is sent with prompt-cache markup (on providers that
support it) so auditors share the same provider-side cache entry as the
reviewer fan-out.
"""

from __future__ import annotations

from ...observability import node_context
from ..utils.agent_states import AuditReport, ReviewState
from ..utils.agent_utils import (
    REFERENCES_NOTE,
    context_block,
    references_block,
    supplement_block,
    unreliable_reference_parse,
)
from ..utils.llm import make_llm
from ..utils.structured import invoke_markdown

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
    "Write a factual Markdown report naming the categories checked and each "
    "finding with its HARD or SOFT severity and present, missing, or "
    "unverifiable status. Use any clear organization; no JSON, schema, or "
    "fixed headings are required. You assign no score and make no "
    "accept/reject judgment — that is the editor's job."
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
    "message; follow it strictly. Return ordinary Markdown, never JSON."
)


def make_auditor_node(
    name: str,
    title: str,
    mandate: str,
    *,
    tool_names: list[str] | None = None,
    needs_supplement: bool = False,
    needs_references: bool = False,
    requires_reliable_references: bool = False,
):
    """Build a LangGraph node for one editorial auditor.

    ``title`` is the human-facing name used in the rendered report and the
    prompt. ``tool_names`` lists research tools the auditor may call (e.g. to
    resolve a citation); pass ``None`` for a tool-free auditor.
    ``needs_supplement`` opts this auditor into receiving the full
    supplementary-information block (when one was provided) appended after the
    manuscript in its cached prefix. ``needs_references`` opts it into the
    converter's typed bibliography, for an auditor whose checklist is about
    the reference list itself.
    """
    node_name = f"audit_{name}"
    bound_tool_names = list(tool_names or [])

    def node(state: ReviewState) -> dict:
        with node_context(node_name, run_id=state["config"].get("run_id", "")):
            if requires_reliable_references:
                parse_problem = unreliable_reference_parse(state)
                if parse_problem:
                    body = (
                        "# Citation Audit Not Performed\n\n"
                        f"The typed bibliography is unreliable because {parse_problem}. "
                        "This is an **ingest limitation, not a manuscript finding**. "
                        "No missing-reference, incomplete-bibliography, or citation-"
                        "resolvability criticism may be inferred from this audit or "
                        "passed to the authors. Re-run the citation audit from a source "
                        "file or corrected PDF conversion if citation integrity must be "
                        "assessed."
                    )
                    return {
                        "audits": [{
                            "auditor": name,
                            "title": title,
                            "hard_gaps": None,
                            "soft_gaps": None,
                            "findings": [],
                            "body": body,
                        }],
                        "total_cost": 0.0,
                    }
            config = state["config"]
            llm = make_llm(config, agent=node_name, default_tag="audit")
            instructions = _INSTRUCTIONS.format(
                title=state.get("manuscript_title", "Untitled"),
                title_role=title,
                mandate=mandate,
            )
            cached_prefix = context_block(state)
            # Opt-in blocks are appended, never interleaved, for the same
            # reason in both cases: the blocks before them stay byte-identical
            # to what every other agent sends, so this agent reads the shared
            # manuscript entry and writes only its own block on top instead of
            # writing a second copy of the manuscript because its prefix
            # differed. Both are no-ops when the material is absent.
            if needs_references:
                references = references_block(state)
                if references:
                    cached_prefix = [*cached_prefix, references]
                    instructions = instructions + REFERENCES_NOTE
            if needs_supplement:
                supplement = supplement_block(state)
                if supplement:
                    cached_prefix = [*cached_prefix, supplement]
                    instructions = instructions + _SUPPLEMENT_NOTE

            try:
                tools = []
                if bound_tool_names and config.get("research_enabled", True):
                    from ...research.tools import get_tools_by_name

                    tools = get_tools_by_name(bound_tool_names, config)
                result = invoke_markdown(
                    llm,
                    config,
                    _SYSTEM,
                    instructions,
                    tools=tools,
                    cached_prefix=cached_prefix,
                    min_chars=160,
                )
            except Exception as exc:  # noqa: BLE001
                return {"errors": [f"{name} auditor failed: {exc}"]}

            report: AuditReport = {
                "auditor": name,
                "title": title,
                # Counts are derived convenience metadata. Unknown is honest:
                # the editor reads the full report and does not need a parser
                # to decide whether a named gap matters.
                "hard_gaps": None,
                "soft_gaps": None,
                "findings": [],
                "body": result.text,
            }
            update: dict = {"audits": [report], "total_cost": result.cost}
            if result.warnings:
                update["errors"] = [f"{name} auditor degraded: {w}" for w in result.warnings]
            return update

    node.__name__ = node_name
    return node
