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

**Revision rounds.** When the state carries a ``prior_round``, the same
node re-reviews a revised draft instead: it emits
:class:`RevisionReviewerOutput`, which forces a ruling on every point this
reviewer raised before, and it is shown its own prior report, the section
diff, and — only if the authors' letter survived verification — the
pointers that letter offers. It is never shown another reviewer's report;
independence is why eight verdicts are worth more than one, and it does not
lapse because this is a second look.

The revision path also carries the score-consistency guard (see
:func:`_challenge_stuck_score`), which catches the characteristic failure
of a second round: a reviewer marking its own points resolved and then
declining to move the score anyway.
"""

from __future__ import annotations

from collections.abc import Callable

from ...ingest.diff import render_diff_block
from ...observability import AgentEvent, current_node, emit, node_context
from ..schemas import ReviewerOutput, RevisionReviewerOutput
from ..utils.agent_states import ReviewReport, ReviewState
from ..utils.agent_utils import context_block
from ..utils.llm import make_llm
from ..utils.structured import (
    StructuredResult,
    invoke_structured,
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
    "Return a structured review with the following fields:\n"
    "  - score (int 1-5, or null): 1=reject, 3=major revision, "
    "4=minor revision, 5=accept.\n"
    "    Return null ONLY if this manuscript contains nothing your dimension "
    "covers — a data-analysis review of a paper with no quantitative analysis "
    "in it, say. Then set not_applicable_reason and still write the summary: "
    "'this paper has no statistics to check' is useful to a reader.\n"
    "    Null is NOT for work you judge harshly. Thin, unclear, missing what "
    "you expected, or evidence you cannot verify are all LOW SCORES, not "
    "N/A. If you can form any view of this paper on your dimension, give a "
    "number. Do not use a high score to mean 'nothing here concerned me "
    "because there was nothing here' — that inflates the panel and is exactly "
    "what null exists to prevent.\n"
    "  - not_applicable_reason: required when score is null, one sentence "
    "naming what is absent that puts this paper outside your dimension\n"
    "  - confidence (int 1-5): certainty in your score — 5=squarely your "
    "expertise with clear manuscript evidence; 3=reasonable read but some "
    "ambiguity; 1-2=outside your subarea or the manuscript is too unclear to "
    "judge. Lower your confidence rather than guessing.\n"
    "  - summary: your overall take from your specialty, 4 sentences at most. "
    "Lead with the verdict, not with what the paper is about.\n"
    "  - strengths: at most 3, one sentence each. A strength is something the "
    "authors did well, not a description of what they did.\n"
    "  - weaknesses: the load-bearing ones first at full length, then the "
    "sweep at one sentence each. Each carries its manuscript evidence.\n"
    "  - questions: only what you actually need answered, one line each. Do "
    "not restate a weakness as a question — the HARD ones are already paired, "
    "and anything else repeated here is the same point billed twice.\n\n"
    "Focus strictly on your specialty. Do not rehash unrelated aspects."
)

_SYSTEM = (
    "You are a specialist on a journal peer-review editorial panel. "
    "Your role is given in the user message; follow it strictly. "
    "Return your verdict as the structured ReviewerOutput schema."
)


# --- revision round ---------------------------------------------------------

_REVISION_SYSTEM = (
    "You are a specialist on a journal peer-review editorial panel, "
    "re-reviewing a manuscript you reviewed once already, now that the authors "
    "have revised it. Your role and mandate are in the user message; follow "
    "them strictly. Rule on every point you raised before, under the id you "
    "were given, and never invent an id. Return your verdict as the structured "
    "RevisionReviewerOutput schema."
)

_REVISION_HEADER = (
    "Manuscript title: {title}\n\n"
    "You are the {role} on a journal peer-review panel. You reviewed an "
    "earlier draft of this manuscript in round {round_no}; the authors have "
    "since revised it, and the text above is the REVISED draft. Your job now "
    "is to judge what the revision did about the critique you gave — not to "
    "review the manuscript again from scratch.\n\n"
    "{mandate}\n\n"
    "As in the previous round you are shown your own review and nothing from "
    "any other reviewer, and you are the only one ruling on your points."
)

# Shown instead of the prior report when this reviewer has none on record —
# a panel that gained a specialist since round 1, or a prior round that ended
# at the desk. The schema still demands a prior_score, so it needs an answer.
_NO_PRIOR_REPORT = (
    "## Your review in the previous round\n\n"
    "You have no report on record for the previous round, so there are no "
    "points of yours to rule on: leave prior_points empty, set prior_score "
    "equal to the score you give now, and judge the revised draft on its own "
    "terms."
)

# Appended under the verifier's pointer list. That list is already framed as
# pointers, but the reviewer has to be told the same thing in its own
# instructions: this is the only channel by which the authors' words reach it,
# and routing them through verification buys nothing if they read as findings.
_AUTHOR_POINTERS_NOTE = (
    "Every line above is a pointer, not a finding. The authors wrote them, and "
    "the authors want a better outcome than they got last round: they are an "
    "interested party, not a source. Go to the passage named, read what it now "
    "says, and rule on the point yourself. The manuscript is the evidence; "
    "their letter never is. A pointer you cannot corroborate in the text "
    "changes nothing, and \"the authors say they addressed it\" is never a "
    "reason to mark anything resolved."
)

_REVISION_TASK = (
    "## What to return\n\n"
    "Return the structured RevisionReviewerOutput schema.\n"
    "  - prior_score: the score you gave last round, copied from your report "
    "above. Do not re-derive it.\n"
    "  - prior_points: one ruling for EVERY weakness id listed above — none "
    "skipped, including any you now consider minor. Mark it resolved, partial, "
    "or outstanding, and cite the revised text you read to decide. For "
    "anything short of resolved, say what you looked for and where, so the "
    "authors know what would satisfy it.\n"
    "  - new_issues: prefer none. This round judges the response to the "
    "critique that was given, not a fresh hunt for faults. Raise something new "
    "only if the revision created it, or if staying silent would let a real "
    "defect through — and either way set caused_by_the_revision honestly. The "
    "\"what changed\" block above tells you which it is; the editor is shown "
    "the flag, and an issue that was equally visible in the draft you already "
    "reviewed is a goalpost moved, not a finding.\n"
    "  - score: your score for the REVISED manuscript on the same 1-5 scale "
    "(1=reject, 3=major revision, 4=minor revision, 5=accept).\n"
    "  - score_rationale: why the score moved, or specifically what still "
    "holds it where it was.\n"
    "  - confidence, summary, strengths, questions: as in your first review.\n\n"
    "On the score: an improvement has to be earned, and when it is earned it "
    "has to be given. If the authors did what you asked, the score moves — "
    "reporting your own points resolved and then leaving the score untouched "
    "is not rigor, it is a verdict that contradicts its own evidence. Equally, "
    "do not reward effort: a point is resolved when the text answers it, not "
    "when the authors say they tried. If real concerns stand, keep the score "
    "and name exactly what stands."
)

# The re-ask. It quotes the reviewer's own rulings back at it and demands one
# of two specific answers, because what is being caught is not a wrong number
# — it is a verdict nobody has had to justify.
_CONSISTENCY_CHALLENGE = (
    "## One thing has to be reconciled before this is recorded\n\n"
    "You reported every point you raised in round {round_no} as resolved, you "
    "recorded no issue that the revision itself created, and you scored the "
    "revised manuscript {score}/5 against your previous {prior_score}/5.\n\n"
    "Your rulings, in your own words:\n"
    "{rulings}\n\n"
    "Your stated reason for the score:\n"
    "> {rationale}\n\n"
    "Those do not fit together. If nothing you asked for is still open and the "
    "revision broke nothing, then by your own account there is nothing left "
    "holding the score down. Answer with a corrected RevisionReviewerOutput "
    "that resolves it whichever of these two ways is true:\n"
    "  1. The score should move. Raise it to what the revised manuscript now "
    "earns, and say so in score_rationale.\n"
    "  2. Something genuinely does still hold it down. Then name it exactly: "
    "downgrade the prior point(s) you called resolved to partial or "
    "outstanding, citing the evidence you overlooked, or record the blocker as "
    "a new issue with caused_by_the_revision set honestly.\n\n"
    "What does not answer this: a fresh objection you could have raised on the "
    "previous draft, or the same rationale restated. Raising the bar once the "
    "authors have met it is precisely the failure this check exists to catch. "
    "Leave the rest of your review as it stands — change only the part that is "
    "wrong. If the work earned a better score, give it."
)

# Appended to the body when the reviewer was challenged and answered with
# neither a moved score nor a named blocker. The verdict stands as the
# reviewer left it — the guard forces an explanation, it does not manufacture
# a number no reviewer endorsed — but the editor sees that it went
# unexplained.
_UNRESOLVED_GUARD_NOTE = (
    "\n\n---\n\n"
    "_Consistency check: this reviewer marked every point it raised as "
    "resolved and recorded no issue introduced by the revision, yet did not "
    "move its score. Asked once to either raise the score or name what still "
    "holds it down, it did neither. The score above is the reviewer's own and "
    "has not been adjusted._"
)


def make_reviewer_node(
    name: str,
    role: str,
    mandate: str,
    *,
    tool_names: list[str] | None = None,
    mandate_extra: Callable[[ReviewState], str] | None = None,
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

    The returned node handles both a first review and a revision round;
    ``state["prior_round"]`` decides which, so the graph wires the same eight
    reviewers either way and every one of them re-reviews.
    """
    node_name = f"reviewer_{name}"
    bound_tool_names = list(tool_names or [])

    def node(state: ReviewState) -> dict:
        with node_context(node_name, run_id=state["config"].get("run_id", "")):
            llm = make_llm(state["config"], agent=node_name, default_tag="reviewer")
            # Byte-identical across the fan-out in both modes, so a revision
            # round still shares one provider-side cache entry. Everything
            # round-specific goes in the user turn, after the breakpoint.
            cached_prefix = context_block(state)
            run_pass = (
                _revision_pass if state.get("prior_round") is not None else _first_pass
            )
            extra = mandate_extra(state) if mandate_extra else ""
            return run_pass(
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


def _first_pass(
    state: ReviewState,
    llm,
    cached_prefix: str,
    *,
    name: str,
    role: str,
    mandate: str,
    tool_names: list[str],
) -> dict:
    """Review a draft this panel has not seen before."""
    instructions = _INSTRUCTIONS.format(
        title=state.get("manuscript_title", "Untitled"),
        role=role,
        mandate=mandate,
    )
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
        "body": output.to_markdown(role=role),
    }
    return {"reports": [report], "total_cost": result.cost}


def _revision_pass(
    state: ReviewState,
    llm,
    cached_prefix: str,
    *,
    name: str,
    role: str,
    mandate: str,
    tool_names: list[str],
) -> dict:
    """Re-review a revised draft against this reviewer's own prior critique."""
    config = state["config"]
    prior = state["prior_round"]
    instructions = _revision_instructions(state, name=name, role=role, mandate=mandate)

    try:
        result = _call_model(
            llm, RevisionReviewerOutput, config, _REVISION_SYSTEM, instructions,
            tool_names=tool_names, cached_prefix=cached_prefix,
        )
    except Exception as exc:  # noqa: BLE001
        return {"errors": [f"{name} reviewer failed: {exc}"]}

    output: RevisionReviewerOutput = result.instance  # type: ignore[assignment]
    cost = result.cost
    guard_note = ""
    if output.score_is_inconsistent():
        answered, challenge_cost = _challenge_stuck_score(
            llm, config, instructions, cached_prefix, output, prior.round
        )
        cost += challenge_cost
        if answered is not None:
            output = answered
        else:
            guard_note = _UNRESOLVED_GUARD_NOTE

    report: ReviewReport = {
        "reviewer": name,
        "score": None if output.score is None else float(output.score),
        "confidence": float(output.confidence),
        "weaknesses": _carried_weaknesses(output, prior, name),
        "questions": list(output.questions),
        # Structured, so the editor's round-delta can count goalpost drift
        # without reading it back out of the rendered body.
        "new_issues": [
            {"issue": i.issue, "caused_by_the_revision": i.caused_by_the_revision}
            for i in output.new_issues
        ],
        "body": output.to_markdown(role=role) + guard_note,
    }
    return {"reports": [report], "total_cost": cost}


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
    """Structured call, through the research-tool loop when this reviewer has one."""
    if tool_names and config.get("research_enabled", True):
        from ...research.tools import get_tools_by_name

        return invoke_structured_after_tools(
            llm,
            schema,
            config,
            system,
            instructions,
            get_tools_by_name(tool_names, config),
            cached_prefix=cached_prefix,
        )
    return invoke_structured(
        llm, schema, config, system, instructions, cached_prefix=cached_prefix
    )


def _revision_instructions(
    state: ReviewState, *, name: str, role: str, mandate: str
) -> str:
    """Assemble the round-N user prompt for one reviewer.

    Every block is scoped to this reviewer or to the manuscript itself.
    Nothing another reviewer wrote, and nothing the authors wrote except the
    verifier's corroborated pointers, is allowed in.
    """
    prior = state["prior_round"]
    diff = state.get("manuscript_diff")
    blocks = [
        _REVISION_HEADER.format(
            title=state.get("manuscript_title", "Untitled"),
            role=role,
            round_no=prior.round,
            mandate=mandate,
        ),
        prior.prior_report_block(name) or _NO_PRIOR_REPORT,
        render_diff_block(diff) if diff is not None else "",
        _author_pointers(state),
        _REVISION_TASK,
    ]
    return "\n\n".join(b for b in blocks if b)


def _author_pointers(state: ReviewState) -> str:
    """The verified pointer list plus its handling rule, or '' when there is none."""
    block = (state.get("verified_claims_block") or "").strip()
    if not block:
        return ""
    return f"{block}\n\n{_AUTHOR_POINTERS_NOTE}"


def _challenge_stuck_score(
    llm,
    config: dict,
    instructions: str,
    cached_prefix: str,
    output: RevisionReviewerOutput,
    round_no: int,
) -> tuple[RevisionReviewerOutput | None, float]:
    """Re-ask once when a reviewer's own rulings contradict its score.

    Returns the corrected verdict and the cost of the extra call, or
    ``(None, cost)`` when the reviewer failed to answer — the caller then
    keeps the original verdict. The guard never edits a score itself: a
    number no reviewer endorsed would be a fabrication dressed as a panel
    opinion, and the panel's scores are what the editor weighs.

    Deliberately tool-free even for reviewers that carry research tools. The
    reviewer is being asked to reconcile what it already wrote; handing it a
    literature search at that moment invites exactly the fresh objection this
    guard exists to prevent.
    """
    emit(AgentEvent(
        kind="log",
        node=current_node(),
        text=(
            "score-consistency guard: every prior point resolved but score held "
            f"at {output.score}/5; re-asking once"
        ),
    ))
    challenge = _CONSISTENCY_CHALLENGE.format(
        round_no=round_no,
        score=output.score,
        prior_score=output.prior_score,
        rulings="\n".join(
            f"- [{p.id}] resolved — {p.evidence.strip()}" for p in output.prior_points
        ),
        rationale=output.score_rationale.strip() or "(none given)",
    )
    try:
        result = invoke_structured(
            llm,
            RevisionReviewerOutput,
            config,
            _REVISION_SYSTEM,
            f"{instructions}\n\n---\n\n{challenge}",
            cached_prefix=cached_prefix,
        )
    except Exception:  # noqa: BLE001
        # A failed re-ask is not a failed review. The first verdict is a real
        # verdict; dropping this reviewer from the panel over the guard would
        # cost the round more than the unexplained contradiction does.
        return None, 0.0

    answered: RevisionReviewerOutput = result.instance  # type: ignore[assignment]
    # A second pass that lands back in the same contradiction has not
    # answered. Neither has one that answers by scoring *lower* than before —
    # the re-ask demands a justification and must not become a way to punish
    # having been asked for one.
    if answered.score_is_inconsistent() or answered.score < output.score:
        return None, result.cost
    return answered, result.cost


def _carried_weaknesses(
    output: RevisionReviewerOutput, prior, name: str
) -> list[str]:
    """What this round hands to the next: still-open prior points, then new issues.

    A resolved point is dropped — carrying it forward would put the authors
    back where they started and reopen a question this round settled.
    Anything short of resolved keeps the wording it was first raised in, so a
    third round hands back the same ask rather than a paraphrase of a
    paraphrase, with this round's finding appended as the update.
    """
    prior_report = prior.report_for(name)
    original = {w.id: w.text for w in prior_report.weaknesses} if prior_report else {}

    carried: list[str] = []
    for point in output.prior_points:
        if point.status == "resolved":
            continue
        standing = (
            "still unaddressed" if point.status == "outstanding"
            else "only partly addressed"
        )
        detail = point.evidence.strip()
        text = original.get(point.id, "")
        if text:
            carried.append(f"{text} ({standing} in the revision: {detail})")
        elif detail:
            # An id with no match in the prior record: the reviewer's own
            # account of the point is all there is to carry forward.
            carried.append(f"[{point.id}] {standing} in the revision: {detail}")
    for issue in output.new_issues:
        origin = (
            "introduced by the revision" if issue.caused_by_the_revision
            else "not raised in the previous round"
        )
        carried.append(f"{issue.issue} ({origin})")
    return carried
