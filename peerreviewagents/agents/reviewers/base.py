"""Builder for specialist reviewer nodes.

A reviewer reads the manuscript, optionally consults research tools, and
returns Markdown with an explicit score and confidence. The Markdown itself
is the durable source of truth; tolerant extraction promotes the few scalars
and lists downstream stages need, but formatting cannot discard the review.

The manuscript block is sent with prompt-cache markup (on providers
that support it) so the parallel reviewer fan-out shares one
provider-side cache entry.

**The panel is blind to the round.** A reviewer reviews the manuscript in
front of it, every round, and is never told that a previous round exists.
There is no revision path here and no second schema: round 3 renders the
same prompt as round 1 and returns the same :class:`ReviewerOutput`.

That is a correction, not an economy. Reviewers used to be shown their own
prior report and a "what changed" summary, which asked them to judge a
revision — and telling a panel it is looking at a revision creates the
incentive to find progress. On a live byte-identical resubmission it
produced a novelty reviewer raising 3 → 5 "because the revision
successfully addresses the concerns", against a manuscript in which
nothing had been revised. Every guard that path carried (a stuck-score
challenge, goalpost-drift counting, a diff veto) existed to police a
psychology the framing itself created.

Round-over-round continuity now lives entirely on the editor's numbered
required-revisions list — the actual contract with the authors — checked
against the new draft by the revision-compliance auditor. The panel's job
is the one it is good at: judging this manuscript on its merits.

The single exception is the response verifier's pointer block, which
reaches a reviewer when the authors submitted a letter. It survives
because it says nothing about rounds: it names passages of *this*
manuscript and asks the reviewer to read them (see
:meth:`ResponseVerificationOutput.panel_block`).
"""

from __future__ import annotations

from collections.abc import Callable

from ...observability import node_context
from ..schemas import ReviewerOutput
from ..utils.agent_states import ReviewReport, ReviewState
from ..utils.agent_utils import REFERENCES_NOTE, context_block, references_block
from ..utils.llm import make_llm
from ..utils.structured import (
    StructuredResult,
    invoke_structured_after_tools,
)

_INSTRUCTIONS = (
    "Manuscript title: {title}\n\n"
    "You are the {role} on a journal peer-review panel. You are rigorous, "
    "fair, and constructive.\n\n"
    "## Your mandate\n\n"
    "{mandate}\n\n"
    "## How to work\n\n"
    "Two passes, in this order. The first is the one that matters.\n\n"
    "**First, the load-bearing claims.** Find the two or three claims the "
    "contribution actually rests on — the ones that, if they did not hold, "
    "would leave nothing here worth publishing. For each: locate the specific "
    "result offered as evidence, then ask the question a referee exists to "
    "ask — does that result establish that claim, or would something else "
    "produce the same result? Name the alternative explicitly, and say what "
    "would distinguish it from the authors' account. Give these the room the "
    "argument needs — up to about 150 words each. They are your primary "
    "weaknesses and they go first in the list.\n\n"
    "**Then sweep.** Work the mandate above for anything the first pass did "
    "not reach. This matters, but it is secondary, and the order is not "
    "cosmetic: a long list of small correct observations is not a substitute "
    "for one serious objection. A review that only sweeps has not done the "
    "job, however many items it returns. **One sentence per sweep item.** At "
    "most eight of them, and fewer is normal — if you are straining to reach "
    "eight you have left the useful ones behind.\n\n"
    "## Length\n\n"
    "Write like a referee with other papers to read, not like someone being "
    "paid by the word. Every sentence should either name a specific problem or "
    "give the evidence for one.\n\n"
    "Cut, everywhere: restating what the manuscript says before critiquing it "
    "(the editor has read it); prefaces like 'it is worth noting that' or 'the "
    "authors should consider'; the same objection re-worded in weaknesses and "
    "again in questions; praise that is really just a summary; and closing "
    "paragraphs that recap what you just said.\n\n"
    "This is a budget on padding, not on substance. The load-bearing critique "
    "gets its full 150 words and should use them where the argument earns it. "
    "Terse is not the same as shallow, and a short review that misses the "
    "objection that decides the paper has failed at the only thing that "
    "mattered.\n\n"
    "## Invention, and what is not invention\n\n"
    "Take the manuscript at its word about what was done and what was found. "
    "Do not assert it says something it does not say, report results it does "
    "not report, or attribute to the authors a claim they never made. "
    "Critiquing a paper they did not write helps nobody and costs you your "
    "credibility on the points that are real.\n\n"
    "That is a rule about facts, not about inference — and the difference is "
    "the whole job. Proposing an explanation the authors did not consider is "
    "not invention. It is the most valuable thing you can do here. Granting "
    "every reported fact and then showing the conclusion does not follow from "
    "it — because a confound was never excluded, because the comparison does "
    "not isolate what it claims to, because a different mechanism predicts "
    "the same data — is a HARD weakness, and it belongs in your report stated "
    "as one. The evidence you cite for it is the manuscript's own design and "
    "its own results. Do not demote it to a polite question because the "
    "authors did not raise it themselves: that they did not is the point.\n\n"
    "Questions are for what you genuinely cannot determine from the text, not "
    "for objections you are reluctant to make.\n\n"
    # A property of this pipeline, not of any venue. It lived in one journal
    # profile for a while, which meant every other profile produced referees
    # that assessed figures they had never seen.
    "## What you cannot inspect\n\n"
    "You read this manuscript as text. You cannot run its code, execute its "
    "experiments, check a derivation line by line, or see its figures — a "
    "figure reaches you only through whatever the surrounding text says about "
    "it. Where a claim rests on something you cannot inspect, say so "
    "explicitly and lower your confidence. Do not score it as a flaw and do "
    "not wave it through: 'I could not verify this' and 'this is unsupported' "
    "are different findings, and a report that blurs them tells the authors "
    "something false about their own paper.\n\n"
    "## The rest\n\n"
    "A HARD issue — a claim, method, or figure genuinely unsupported, "
    "ambiguous, or non-compliant as worded — belongs in BOTH your weaknesses "
    "and your questions; quote the specific sentence, figure, or value you "
    "are flagging, and name the one thing that would settle it: the figure, "
    "panel, table, statistic, control, subset or analysis you want, specific "
    "enough that the authors could start on it without writing back to ask "
    "what you meant. 'Validate this orthogonally' is not that; 'report the "
    "same count restricted to the subset where X holds, alongside the current "
    "one' is. A SOFT issue — friction or a fixable improvement that does "
    "not undermine the work — is a minor weakness, and needs no such remedy. "
    "Reviewers run in parallel "
    "and never see each other's reports, so if you spot an issue belonging to "
    "another specialty, name it in one line and attribute it to that reviewer "
    "rather than re-deciding it yourself or dropping it silently — your own "
    "remit comes first and at full length, and those one-liners go last, "
    "never ahead of a finding only you were assigned to make. If a target "
    "journal is described above, judge the manuscript against that venue's "
    "scope, standards, and submission limits, and flag misfits (out-of-scope, "
    "over-length, too many display items) where relevant to your specialty. "
    "If a review strictness standard is described above, calibrate your score "
    "and how heavily you weigh weaknesses to that standard.\n\n"
    "Write ordinary Markdown. Put these two lines at the TOP of the final "
    "answer so they survive truncation, then write the review under whatever "
    "clear Markdown headings fit the substance:\n\n"
    "SCORE: <1-5 or N/A>\n"
    "CONFIDENCE: <1-5>\n\n"
    "The score scale is 1=reject, 3=major revision, "
    "4=minor revision, 5=accept.\n"
    "Return N/A ONLY if this manuscript contains nothing your dimension "
    "covers — a data-analysis review of a paper with no quantitative analysis "
    "in it, say. Then add `N/A REASON:` and still write the assessment: "
    "'this paper has no statistics to check' is useful to a reader.\n"
    "N/A is NOT for work you judge harshly. Thin, unclear, missing what "
    "you expected, or evidence you cannot verify are all LOW SCORES, not "
    "N/A. If you can form any view of this paper on your dimension, give a "
    "number. Do not use a high score to mean 'nothing here concerned me "
    "because there was nothing here' — that inflates the panel and is exactly "
    "what N/A exists to prevent.\n"
    "Confidence is certainty in your score — 5=squarely your "
    "expertise with clear manuscript evidence; 3=reasonable read but some "
    "ambiguity; 1-2=outside your subarea or the manuscript is too unclear to "
    "judge. Lower your confidence rather than guessing.\n"
    "Include your overall take from your specialty, 4 sentences at most. "
    "Lead with the verdict, not with what the paper is about.\n"
    "Include at most 3 strengths, one sentence each. A strength is something the "
    "authors did well, not a description of what they did.\n"
    "Put weaknesses with the load-bearing ones first at full length, then the "
    "sweep at one sentence each. Each carries its manuscript evidence.\n"
    "Ask only what you actually need answered, one line each. Do "
    "not restate a weakness as a question — the HARD ones are already paired, "
    "and anything else repeated here is the same point billed twice.\n\n"
    "Focus strictly on your specialty. Do not rehash unrelated aspects. "
    "Return Markdown prose, never JSON or a tool call."
)

_SYSTEM = (
    "You are a specialist on a journal peer-review editorial panel. "
    "Your role is given in the user message; follow it strictly. "
    "Write the complete review as plain text with clearly labelled score, "
    "confidence, summary, strengths, weaknesses, and questions. Do not emit "
    "JSON or a tool call for the final answer."
)

# Appended under the verifier's pointer list. That list is already framed as
# pointers, but the reviewer has to be told the same thing in its own
# instructions: this is the only channel by which the authors' words reach it,
# and routing them through verification buys nothing if they read as findings.
_AUTHOR_POINTERS_NOTE = (
    "Every line above is a pointer, not a finding. The authors wrote them, and "
    "the authors want a favourable assessment: they are an interested party, "
    "not a source. Go to the passage named, read what it says, and judge the "
    "point yourself. The manuscript is the evidence; their account of it never "
    "is. A pointer you cannot corroborate in the text changes nothing, and "
    "\"the authors say this is fine\" is never a reason to drop an objection."
)


def make_reviewer_node(
    name: str,
    role: str,
    mandate: str,
    *,
    tool_names: list[str] | None = None,
    mandate_extra: Callable[[ReviewState], str] | None = None,
    needs_references: bool = False,
):
    """Build a LangGraph node for one specialist reviewer.

    ``tool_names`` is a list of logical research-tool names this reviewer
    should call (see :mod:`peerreviewagents.research.tools` for the
    registry). Pass ``None`` (default) for a tool-free reviewer.

    ``mandate_extra`` appends manuscript-specific material to the mandate at
    run time — currently only the clarity reviewer uses it, to receive the
    deterministic text statistics. It is appended to the *mandate*, which
    lives in the user turn, never in ``cached_prefix``: the prefix is the
    shared manuscript block that all eight reviewers hit, and varying it per
    reviewer would split one cache entry into eight.

    ``needs_references`` opts a reviewer into the converter's typed
    bibliography, appended *after* the shared prefix rather than mixed into
    it — so the blocks the other seven send stay byte-identical and this one
    writes only the reference list on top of the entry they share. Currently
    the literature reviewer alone, whose lane is that list.

    The returned node runs the same way in every round: the graph wires the
    same eight reviewers whether or not a prior round exists, and none of
    them is told which it is.
    """
    node_name = f"reviewer_{name}"
    bound_tool_names = list(tool_names or [])

    def node(state: ReviewState) -> dict:
        with node_context(node_name, run_id=state["config"].get("run_id", "")):
            llm = make_llm(state["config"], agent=node_name, default_tag="reviewer")
            # Byte-identical across the fan-out, so the eight share one
            # provider-side cache entry. Anything per-reviewer goes in the
            # user turn, after the breakpoint.
            cached_prefix = context_block(state)
            extra = mandate_extra(state) if mandate_extra else ""
            if needs_references:
                references = references_block(state)
                if references:
                    cached_prefix = [*cached_prefix, references]
                    extra += REFERENCES_NOTE
            return _review(
                state,
                llm,
                cached_prefix,
                name=name,
                role=role,
                mandate=mandate + extra if extra else mandate,
                tool_names=bound_tool_names,
            )

    node.__name__ = node_name
    return node


def _review(
    state: ReviewState,
    llm,
    cached_prefix: str,
    *,
    name: str,
    role: str,
    mandate: str,
    tool_names: list[str],
) -> dict:
    """Review the manuscript as it stands, knowing nothing of any other round."""
    instructions = _instructions(state, role=role, mandate=mandate)
    try:
        result = _call_model(
            llm, ReviewerOutput, state["config"], _SYSTEM, instructions,
            tool_names=tool_names, cached_prefix=cached_prefix,
        )
    except Exception as exc:  # noqa: BLE001
        return {"errors": [f"{name} reviewer failed: {exc}"]}

    output: ReviewerOutput = result.instance  # type: ignore[assignment]
    report: ReviewReport = {
        "reviewer": name,
        # None when this dimension had nothing to judge in this manuscript.
        # Kept as None all the way through rather than coerced to a number:
        # every aggregate downstream filters it out, and a placeholder here
        # would be indistinguishable from a real score by the time it got there.
        "score": None if output.score is None else float(output.score),
        "not_applicable_reason": output.not_applicable_reason.strip(),
        "confidence": float(output.confidence),
        # Promoted so the round record can id each weakness and hand
        # this reviewer its own points back in a later round. Reading
        # them out of `body` would mean parsing markdown.
        "weaknesses": list(output.weaknesses),
        "questions": list(output.questions),
        "body": (
            result.raw_text
            if result.raw_text.lstrip().startswith("#")
            else f"# {role}\n\n{result.raw_text}"
        ) if result.raw_text else output.to_markdown(role=role),
        "score_source": result.score_source or "structured_fallback",
    }
    update: dict = {"reports": [report], "total_cost": result.cost}
    if result.warnings:
        update["errors"] = [f"{name} reviewer degraded: {w}" for w in result.warnings]
    return update


def _call_model(
    llm,
    schema,
    config: dict,
    system: str,
    instructions: str,
    *,
    tool_names: list[str],
    cached_prefix: str,
) -> StructuredResult:
    """Write prose once, then recover only the metadata used downstream.

    Keeping the manuscript-reading call free of the large ReviewerOutput tool
    contract prevents a missing scalar near the end from discarding and
    regenerating the entire review. Explicit Markdown metadata is parsed
    directly; a tiny normalizer is used only when those scalar labels are
    missing, and cannot rewrite the scientific assessment.
    """
    tools = []
    if tool_names and config.get("research_enabled", True):
        from ...research.tools import get_tools_by_name

        tools = get_tools_by_name(tool_names, config)
    return invoke_structured_after_tools(
        llm,
        schema,
        config,
        system,
        instructions,
        tools,
        cached_prefix=cached_prefix,
    )


def _instructions(state: ReviewState, *, role: str, mandate: str) -> str:
    """Assemble the user turn: the mandate, then the authors' pointers if any.

    Nothing here depends on ``prior_round``. A round-3 prompt and a round-1
    prompt over the same manuscript are byte-identical unless the authors
    submitted a letter, and that is the property the blinding rests on — so
    it is asserted directly in the adversarial suite rather than left to
    inspection of this function.
    """
    blocks = [
        _INSTRUCTIONS.format(
            title=state.get("manuscript_title", "Untitled"),
            role=role,
            mandate=mandate,
        ),
        _author_pointers(state),
    ]
    return "\n\n".join(b for b in blocks if b)


def _author_pointers(state: ReviewState) -> str:
    """The verified pointer list plus its handling rule, or '' when there is none.

    Empty on a first round by construction: a response letter answers a
    previous review, so the CLI refuses one without ``--revision-of`` and the
    verifier writes nothing.
    """
    block = (state.get("verified_claims_block") or "").strip()
    if not block:
        return ""
    return f"{block}\n\n{_AUTHOR_POINTERS_NOTE}"
