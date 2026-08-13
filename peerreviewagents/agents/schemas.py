"""Pydantic schemas for every agent boundary, plus markdown renderers.

Each agent emits a typed pydantic instance via
``llm.with_structured_output(Schema)``. The rendered markdown body that
ends up on disk and in the web UI is produced by the schema's
``.to_markdown()`` method, so the structured fields stay the single
source of truth — no YAML-frontmatter parsing, no string matching for
verdicts, no duplicated scalars to keep in sync.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

Verdict = Literal["accept", "minor", "major", "reject"]


# --- Specialist reviewer ----------------------------------------------------


class ReviewerOutput(BaseModel):
    """One specialist reviewer's verdict + critique.

    ``score`` is nullable because not every dimension applies to every paper,
    and a reviewer with nothing in its remit to judge was previously forced to
    invent a number. It reliably invented a flattering one: on a qualitative
    interview study the data-analysis reviewer wrote that there were "no
    p-values, confidence intervals, effect sizes, sample-size calculations, or
    statistical claims to evaluate" and then scored the paper 5/5 — the
    highest data-analysis score in that corpus — pulling the panel mean up on
    the strength of having nothing to review. The ethics reviewer did the same
    thing more quietly across every paper, never once scoring below 4.

    A null score means "outside my remit for this manuscript" and is left out
    of the panel mean rather than counted as a good one. It is not an escape
    from a hard judgement: something missing that *should* be there is a low
    score with high confidence, not N/A.
    """

    score: int | None = Field(
        default=None, ge=1, le=5,
        description="1=reject, 2=major-reject, 3=major-revision, "
                    "4=minor-revision, 5=accept. Use null ONLY when the "
                    "manuscript contains nothing within your dimension to "
                    "judge at all — for example a data-analysis review of a "
                    "paper with no quantitative analysis in it. Null is NOT "
                    "for work that is poor, thin, or missing something you "
                    "expected to find: that is a low score. If you can form "
                    "any view of this paper on your dimension, give a number.",
    )
    not_applicable_reason: str = Field(
        default="",
        description="Required when score is null, ignored otherwise. One "
                    "sentence naming what is absent from the manuscript that "
                    "puts it outside your dimension.",
    )

    confidence: int = Field(
        ..., ge=1, le=5,
        description="Certainty in the score: 5=squarely the reviewer's expertise "
                    "with clear manuscript evidence, 3=reasonable read with some "
                    "ambiguity, 1-2=outside their subarea or the manuscript is too "
                    "unclear to judge. Prefer lowering confidence over guessing.",
    )
    summary: str = Field(
        ...,
        description="One-paragraph overall take from this specialist's perspective.",
    )
    strengths: list[str] = Field(
        default_factory=list,
        description="Bullet sentences naming strengths grounded in manuscript evidence.",
    )
    weaknesses: list[str] = Field(
        default_factory=list,
        description="Bullet sentences naming weaknesses grounded in manuscript evidence.",
    )
    questions: list[str] = Field(
        default_factory=list,
        description="Bullet questions for the authors.",
    )

    @model_validator(mode="after")
    def _abstention_must_be_justified(self) -> ReviewerOutput:
        """A null score without a reason is rejected, not accepted silently.

        Describing the reason as required in the field description was not
        enough. Observed on the first live run: a clarity reviewer returned a
        null score with no reason at all, while its own summary called the
        manuscript "generally well-structured and mostly clear" — it plainly
        held an opinion and used the null as a way out of committing to one.

        That is the same defect as the forced-number problem this field was
        added to fix, pointed the other way: a reviewer that abstains from a
        dimension it can judge removes its verdict from the panel instead of
        inflating it. Rejecting the output makes the structured-output layer
        ask again, which costs one retry and turns an unenforceable
        instruction into a constraint.
        """
        if self.score is None and not self.not_applicable_reason.strip():
            raise ValueError(
                "score is null but not_applicable_reason is empty. Give a null "
                "score only when the manuscript contains nothing your dimension "
                "covers, and say what is absent. If you can form any view of "
                "this paper on your dimension — including a poor one — give a "
                "number instead."
            )
        return self

    def to_markdown(self, role: str = "Reviewer") -> str:
        parts: list[str] = [
            f"# {role}",
            "",
        ]
        # State an unscorable dimension at the top of the report rather than
        # leaving a reader to notice a missing number further down.
        if self.score is None:
            parts += [
                "**Not applicable to this manuscript — no score given, and "
                "this dimension is excluded from the panel mean.**",
                "",
                self.not_applicable_reason.strip()
                or "This manuscript contains nothing within this reviewer's remit.",
                "",
            ]
        parts += [
            "## Summary",
            self.summary.strip() or "(no summary provided)",
        ]
        if self.strengths:
            parts += ["", "## Strengths", *(f"- {s}" for s in self.strengths)]
        if self.weaknesses:
            parts += ["", "## Weaknesses", *(f"- {w}" for w in self.weaknesses)]
        if self.questions:
            parts += ["", "## Questions", *(f"- {q}" for q in self.questions)]
        return "\n".join(parts)


# --- Specialist reviewer, revision round ------------------------------------

ResolutionStatus = Literal["resolved", "partial", "outstanding"]


class PriorPointVerdict(BaseModel):
    """A reviewer's ruling on one weakness it raised in an earlier round."""

    id: str = Field(
        ...,
        description="The id of the prior weakness being ruled on, exactly as "
                    "given to you (e.g. 'methodology-2'). Never invent ids.",
    )
    status: ResolutionStatus = Field(
        ...,
        description="resolved = the revised manuscript fully answers it; "
                    "partial = moved in the right direction but incomplete; "
                    "outstanding = not meaningfully addressed.",
    )
    evidence: str = Field(
        ...,
        description="For resolved/partial: quote or locate the revised text "
                    "that answers it. For outstanding: say what you looked for "
                    "and where, so the authors know what would satisfy it.",
    )


class NewIssue(BaseModel):
    """An issue this reviewer is raising for the first time in this round."""

    issue: str = Field(..., description="The issue, grounded in manuscript evidence.")
    caused_by_the_revision: bool = Field(
        ...,
        description="True ONLY if the revision itself created this issue (new "
                    "text, changed numbers, a fix that broke something else). "
                    "False means it was equally visible in the previous draft "
                    "and you did not raise it then. Answer honestly: raising "
                    "old issues late moves the goalposts on the authors, and "
                    "the editor is shown this flag.",
    )


class RevisionReviewerOutput(BaseModel):
    """One specialist's re-review of a revised manuscript.

    Extends the round-1 verdict with an explicit ruling on each point this
    reviewer raised before, so score movement is *derivable* — the editor can
    check the new score against what the reviewer says it resolved rather
    than taking the number on faith.
    """

    prior_score: int | None = Field(
        default=None, ge=1, le=5,
        description="The score you gave this manuscript in the previous round, "
                    "copied from the report shown to you. Do not re-derive it. "
                    "Null when your prior report gave no score.",
    )
    # Nullable for the same reason and under the same contract as
    # ReviewerOutput.score: a reviewer that abstained last round is told to
    # "return null again" if the revision still gives its dimension nothing to
    # judge, and a schema that forbids null at that moment forces the invented
    # number the nullable score exists to prevent.
    score: int | None = Field(
        default=None, ge=1, le=5,
        description="Your score for the REVISED manuscript. 1=reject, "
                    "3=major-revision, 4=minor-revision, 5=accept. Use null "
                    "ONLY when the revised manuscript still contains nothing "
                    "within your dimension to judge; null is NOT for work you "
                    "judge harshly.",
    )
    not_applicable_reason: str = Field(
        default="",
        description="Required when score is null, ignored otherwise. One "
                    "sentence naming what is absent from the revised "
                    "manuscript that puts it outside your dimension.",
    )
    confidence: int = Field(..., ge=1, le=5, description="Certainty in the new score.")
    prior_points: list[PriorPointVerdict] = Field(
        default_factory=list,
        description="One entry for EVERY weakness you raised in the previous "
                    "round, by id. Do not skip any, including ones you now "
                    "consider unimportant.",
    )
    new_issues: list[NewIssue] = Field(
        default_factory=list,
        description="Issues not raised in your previous review. Prefer an "
                    "empty list: a revision round judges the response to the "
                    "critique that was given, not a fresh hunt for faults.",
    )
    summary: str = Field(
        ...,
        description="One paragraph: what the revision did about your concerns "
                    "and where that leaves the manuscript from your specialty.",
    )
    score_rationale: str = Field(
        ...,
        description="Why the new score differs from (or matches) your prior "
                    "score. If your points are resolved and you did not raise "
                    "the score, name specifically what still holds it down.",
    )
    strengths: list[str] = Field(default_factory=list)
    questions: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _abstention_must_be_justified(self) -> RevisionReviewerOutput:
        """Same constraint as ReviewerOutput: a null score must name its reason.

        The revision round inherits the round-1 failure mode unchanged — a
        reviewer that can form a view but abstains removes its verdict from
        the panel instead of committing to one. Rejecting the output makes the
        structured-output layer ask again.
        """
        if self.score is None and not self.not_applicable_reason.strip():
            raise ValueError(
                "score is null but not_applicable_reason is empty. Give a null "
                "score only when the revised manuscript contains nothing your "
                "dimension covers, and say what is absent. If you can form any "
                "view of this paper on your dimension — including a poor one — "
                "give a number instead."
            )
        return self

    def resolved_count(self) -> int:
        return sum(1 for p in self.prior_points if p.status == "resolved")

    def outstanding_count(self) -> int:
        return sum(1 for p in self.prior_points if p.status == "outstanding")

    def drifted_issues(self) -> list[NewIssue]:
        """New issues the reviewer admits were visible last round."""
        return [i for i in self.new_issues if not i.caused_by_the_revision]

    def score_is_inconsistent(self) -> bool:
        """True when the reviewer resolved everything yet withheld the score.

        The mechanical guard behind the revision round: if every prior point
        is resolved and the revision introduced no new issue, a score that
        did not move is a contradiction the reviewer has to answer for. It
        does not decide who is right — it decides that someone must explain.
        """
        if not self.prior_points:
            return False
        # A null on either side leaves nothing to compare: a score that moved
        # from or to an abstention is not "held", it changed category.
        if self.score is None or self.prior_score is None:
            return False
        all_resolved = all(p.status == "resolved" for p in self.prior_points)
        revision_caused = any(i.caused_by_the_revision for i in self.new_issues)
        return all_resolved and not revision_caused and self.score <= self.prior_score

    def to_markdown(self, role: str = "Reviewer") -> str:
        if self.score is None or self.prior_score is None:
            # No arithmetic across an abstention — an arrow would claim a
            # movement between a number and a score that was never given.
            arrow = "→"
        else:
            delta = self.score - self.prior_score
            arrow = "→" if delta == 0 else ("↑" if delta > 0 else "↓")
        prior_shown = "n/a" if self.prior_score is None else f"{self.prior_score}/5"
        now_shown = "n/a" if self.score is None else f"{self.score}/5"
        parts: list[str] = [
            f"# {role} — Revision Review",
            "",
            f"**Score:** {prior_shown} {arrow} {now_shown} "
            f"(confidence {self.confidence}/5)",
            "",
        ]
        # Same placement as ReviewerOutput: an unscorable dimension is stated
        # at the top rather than left for a reader to infer from "n/a".
        if self.score is None:
            parts += [
                "**Not applicable to this manuscript — no score given, and "
                "this dimension is excluded from the panel mean.**",
                "",
                self.not_applicable_reason.strip()
                or "This manuscript contains nothing within this reviewer's remit.",
                "",
            ]
        parts += [
            "## Summary",
            self.summary.strip() or "(no summary provided)",
            "",
            "## Score rationale",
            self.score_rationale.strip() or "(none given)",
        ]
        if self.prior_points:
            parts += ["", "## Points from the previous round"]
            for p in self.prior_points:
                parts.append(f"- **[{p.id}] {p.status}** — {p.evidence.strip()}")
        if self.new_issues:
            parts += ["", "## Issues raised this round"]
            for i in self.new_issues:
                origin = ("introduced by the revision" if i.caused_by_the_revision
                          else "was visible in the previous draft")
                parts.append(f"- {i.issue} _({origin})_")
        if self.strengths:
            parts += ["", "## Strengths", *(f"- {s}" for s in self.strengths)]
        if self.questions:
            parts += ["", "## Questions", *(f"- {q}" for q in self.questions)]
        return "\n".join(parts)


# --- Revision compliance audit ----------------------------------------------

ComplianceStatus = Literal[
    "addressed", "partial", "not_addressed", "rebutted", "unverifiable"
]
ClaimAccuracy = Literal["corroborated", "overstated", "contradicted", "no_claim"]


class ComplianceFinding(BaseModel):
    """Whether one numbered required revision was carried out."""

    id: str = Field(
        ...,
        description="Id of the required revision, exactly as given (e.g. "
                    "'R1-03'). Never invent ids; report on every one you were given.",
    )
    status: ComplianceStatus = Field(
        ...,
        description="addressed = the manuscript now does what was asked; "
                    "partial = partially done; not_addressed = no relevant "
                    "change; rebutted = not done, but the authors argue it "
                    "should not be (a response, not a failure); unverifiable = "
                    "you cannot tell from the manuscript alone. IMPORTANT: only "
                    "manuscript text can justify 'addressed'. An author's claim "
                    "that they made a change is never sufficient on its own.",
    )
    manuscript_evidence: str = Field(
        default="",
        description="The revised text that substantiates the status — quote or "
                    "locate it. Empty when nothing in the manuscript speaks to it.",
    )
    author_claim: str = Field(
        default="",
        description="What the author's response letter says about this item, "
                    "if anything. Empty when the letter does not mention it.",
    )
    claim_accuracy: ClaimAccuracy = Field(
        default="no_claim",
        description="How the author's claim stands up against the manuscript: "
                    "corroborated = the text shows what they say it does; "
                    "overstated = a change was made but is smaller or narrower "
                    "than claimed; contradicted = the text does not support the "
                    "claim at all; no_claim = the letter is silent on this item.",
    )
    blocking: bool = Field(
        default=False,
        description="If this item is not fully addressed, does that block "
                    "acceptance? Reserve True for gaps that undermine a central "
                    "claim — not for cosmetic or optional items.",
    )


class UndisclosedChange(BaseModel):
    """A substantive change no revision item asked for and the letter omits."""

    section: str = Field(..., description="Where the change appears.")
    change: str = Field(..., description="What changed, quoting the text.")
    concern: str = Field(
        ...,
        description="Why it warrants the editor's attention (e.g. a reported "
                    "value moved, a claim was strengthened, a caveat was "
                    "deleted). Say plainly if it looks routine.",
    )


class RevisionComplianceOutput(BaseModel):
    """The compliance auditor's factual account of a revision.

    Like every auditor this carries NO score and makes no accept/reject
    judgment — it reports what was and was not done, and how well the
    author's account of it matches the document.
    """

    summary: str = Field(
        ...,
        description="One short paragraph: how much of the required-revision "
                    "list was carried out, and how reliable the response letter "
                    "proved. Factual reporting, not a verdict.",
    )
    findings: list[ComplianceFinding] = Field(
        default_factory=list,
        description="Exactly one entry per required revision you were given.",
    )
    undisclosed_changes: list[UndisclosedChange] = Field(
        default_factory=list,
        description="Substantive changes visible in the diff that no required "
                    "revision asked for and the response letter does not "
                    "mention. Routine copy-editing does not belong here.",
    )

    def addressed_count(self) -> int:
        return sum(1 for f in self.findings if f.status == "addressed")

    def blocking_open(self) -> list[ComplianceFinding]:
        return [
            f for f in self.findings
            if f.blocking and f.status in ("not_addressed", "partial", "unverifiable")
        ]

    def unreliable_claims(self) -> list[ComplianceFinding]:
        return [f for f in self.findings if f.claim_accuracy in ("overstated", "contradicted")]

    def to_markdown(self, title: str = "Revision Compliance") -> str:
        total = len(self.findings)
        parts: list[str] = [
            f"# {title}",
            "",
            "## Summary",
            self.summary.strip() or "(no summary provided)",
            "",
            f"**Addressed: {self.addressed_count()}/{total}** · "
            f"blocking still open: {len(self.blocking_open())} · "
            f"unreliable author claims: {len(self.unreliable_claims())}",
        ]
        if self.findings:
            parts += ["", "## Required revisions"]
            for f in self.findings:
                flag = " **[blocking]**" if f.blocking and f.status != "addressed" else ""
                parts.append(f"- **[{f.id}] {f.status}**{flag}")
                if f.manuscript_evidence:
                    parts.append(f"  - Manuscript: {f.manuscript_evidence.strip()}")
                if f.author_claim:
                    parts.append(
                        f"  - Author claim ({f.claim_accuracy}): {f.author_claim.strip()}"
                    )
        if self.undisclosed_changes:
            parts += ["", "## Changes not asked for and not disclosed"]
            for c in self.undisclosed_changes:
                parts.append(f"- **{c.section}** — {c.change.strip()}")
                parts.append(f"  - {c.concern.strip()}")
        if not self.findings:
            parts += ["", "_No required revisions were carried into this round._"]
        return "\n".join(parts)


# --- Author response verification -------------------------------------------

ClaimVerdict = Literal["corroborated", "overstated", "contradicted", "unlocatable"]


class VerifiedClaim(BaseModel):
    """One assertion from the real author's letter, checked against the text."""

    claim: str = Field(
        ...,
        description="The author's assertion, restated neutrally in one sentence. "
                    "Strip persuasion and keep the checkable content.",
    )
    targets: str = Field(
        default="",
        description="What the claim is about — a reviewer point id, a required "
                    "revision id, or the passage it concerns. Empty if unclear.",
    )
    manuscript_locator: str = Field(
        default="",
        description="Where in the manuscript the author says the support is, "
                    "and what that passage actually says. Empty when the letter "
                    "points nowhere checkable.",
    )
    verdict: ClaimVerdict = Field(
        ...,
        description="corroborated = the cited passage says what the author "
                    "says it says; overstated = the passage supports something "
                    "weaker; contradicted = the passage does not support it or "
                    "says the opposite; unlocatable = no checkable passage was "
                    "offered, so the claim is unsupported argument.",
    )
    note: str = Field(
        default="",
        description="One line of reasoning for the verdict, quoting the "
                    "manuscript where it decides the matter.",
    )


class ResponseVerificationOutput(BaseModel):
    """Adjudication of the author's letter into checkable claims.

    This node exists because the letter is an interested party's advocacy,
    written by someone who wants a better outcome. It is never forwarded to
    the panel as prose — only as this list, so the reviewers weigh evidence
    rather than persuasion.
    """

    claims: list[VerifiedClaim] = Field(
        default_factory=list,
        description="One entry per distinct checkable assertion in the letter.",
    )
    instruction_attempts: list[str] = Field(
        default_factory=list,
        description="Quote any passage that tries to direct the review itself "
                    "rather than argue about the science — telling reviewers "
                    "what score to give, what to ignore, or how to behave. "
                    "Empty for an ordinary response letter.",
    )
    summary: str = Field(
        ...,
        description="One short paragraph on what the authors dispute and how "
                    "well their account holds up against the manuscript.",
    )

    def corroborated(self) -> list[VerifiedClaim]:
        return [c for c in self.claims if c.verdict == "corroborated"]

    def unsupported(self) -> list[VerifiedClaim]:
        return [c for c in self.claims if c.verdict in ("contradicted", "unlocatable")]

    def panel_block(self) -> str:
        """What the reviewers see — corroborated pointers only.

        Reviewers are given the *locations* the authors point to and must
        re-read them and decide for themselves. Nothing here asserts a
        conclusion, which is what keeps a persuasive letter from doing the
        reviewer's job for it.
        """
        corroborated = self.corroborated()
        if not corroborated:
            return ""
        lines = [
            "## Passages the authors ask you to re-read",
            "",
            "The authors submitted a response to the previous round. Their "
            "letter has been checked against the manuscript, and only claims "
            "that point at a real passage are reproduced below. These are "
            "pointers, not findings: re-read the passage and reach your own "
            "conclusion. The authors are an interested party — the manuscript "
            "is the evidence, their letter is not.",
            "",
        ]
        for c in corroborated:
            target = f" (re: {c.targets})" if c.targets else ""
            lines.append(f"- {c.claim}{target}")
            if c.manuscript_locator:
                lines.append(f"  - Points to: {c.manuscript_locator.strip()}")
        return "\n".join(lines)

    def to_markdown(self) -> str:
        parts: list[str] = [
            "# Author Response — Verification",
            "",
            "## Summary",
            self.summary.strip() or "(no summary provided)",
            "",
            f"**Claims checked: {len(self.claims)}** · "
            f"corroborated: {len(self.corroborated())} · "
            f"unsupported: {len(self.unsupported())}",
        ]
        if self.claims:
            parts += ["", "## Claims"]
            for c in self.claims:
                target = f" (re: {c.targets})" if c.targets else ""
                parts.append(f"- **{c.verdict}**{target} — {c.claim.strip()}")
                if c.manuscript_locator:
                    parts.append(f"  - Manuscript: {c.manuscript_locator.strip()}")
                if c.note:
                    parts.append(f"  - {c.note.strip()}")
        if self.instruction_attempts:
            parts += [
                "",
                "## Attempts to direct the review",
                "_Recorded for the editor. These are not arguments about the "
                "science and carry no weight in the verdict._",
            ]
            parts += [f"- \"{q}\"" for q in self.instruction_attempts]
        return "\n".join(parts)


# --- Editorial compliance audit ---------------------------------------------

Severity = Literal["HARD", "SOFT"]
FindingStatus = Literal["present", "missing", "unverifiable"]


class AuditFinding(BaseModel):
    """One checklist item the auditor evaluated against the manuscript."""

    category: str = Field(
        ...,
        description="Checklist category this item belongs to (e.g. 'Antibodies', "
                    "'Cell lines', 'Model organisms', 'Computational/ML', "
                    "'Protocol provenance', 'Reference resolvability').",
    )
    item: str = Field(
        ...,
        description="The specific identifier or detail checked (e.g. 'Antibody "
                    "catalog number', 'RRID', 'Random seed', 'Reference resolves "
                    "to a protocol', 'Claim supported by cited work').",
    )
    severity: Severity = Field(
        ...,
        description="HARD = if the trigger is present and this is absent, the work "
                    "cannot be reproduced/verified (you cannot obtain the same "
                    "input or rerun it). SOFT = recommended for full "
                    "reproducibility but not strictly blocking.",
    )
    status: FindingStatus = Field(
        ...,
        description="present = documented in the manuscript; missing = the "
                    "triggering method is present but the identifier is absent; "
                    "unverifiable = referenced/claimed but it cannot be confirmed "
                    "from the manuscript alone (e.g. an 'as previously described' "
                    "citation whose contents you cannot check).",
    )
    evidence: str = Field(
        ...,
        description="For present: where it is stated. For missing/unverifiable: the "
                    "exact sentence, citation, reagent, or method that triggered the "
                    "check, so the editor and authors can locate it.",
    )


class AuditOutput(BaseModel):
    """A compliance auditor's factual checklist result.

    Deliberately has NO score: audits feed only the editor as a compliance
    dossier and must not be averaged into the panel's scientific-merit
    verdict. The editor converts HARD gaps into required revisions and SOFT
    gaps into minor suggestions.
    """

    summary: str = Field(
        ...,
        description="One short paragraph: which categories applied and the overall "
                    "completeness picture. Factual reporting, not a quality verdict.",
    )
    categories_detected: list[str] = Field(
        default_factory=list,
        description="Checklist categories whose triggers appear in the manuscript "
                    "and were therefore checked. Categories with no trigger are "
                    "skipped, not reported as gaps.",
    )
    findings: list[AuditFinding] = Field(
        default_factory=list,
        description="One entry per checked item. Always report missing/unverifiable "
                    "items; you may also list notable present ones for the record.",
    )

    def hard_gaps(self) -> int:
        """HARD items that are outright missing — true reproduction blockers."""
        return sum(1 for f in self.findings if f.severity == "HARD" and f.status == "missing")

    def soft_gaps(self) -> int:
        """SOFT-missing plus anything unverifiable (flagged as a question, not a blocker)."""
        return sum(
            1
            for f in self.findings
            if (f.severity == "SOFT" and f.status == "missing") or f.status == "unverifiable"
        )

    def to_markdown(self, title: str = "Editorial Compliance Audit") -> str:
        hard = [f for f in self.findings if f.severity == "HARD" and f.status == "missing"]
        soft = [f for f in self.findings if f.severity == "SOFT" and f.status == "missing"]
        unver = [f for f in self.findings if f.status == "unverifiable"]
        present = [f for f in self.findings if f.status == "present"]

        parts: list[str] = [
            f"# {title}",
            "",
            "## Summary",
            self.summary.strip() or "(no summary provided)",
        ]
        if self.categories_detected:
            parts += ["", "## Categories checked", *(f"- {c}" for c in self.categories_detected)]
        parts += [
            "",
            f"**HARD gaps (blocking): {len(hard)}** · "
            f"SOFT gaps: {len(soft)} · unverifiable: {len(unver)}",
        ]
        if hard:
            parts += ["", "## HARD gaps — reproduction blockers", *(_finding_line(f) for f in hard)]
        if unver:
            parts += ["", "## Unverifiable (raise as questions)", *(_finding_line(f) for f in unver)]
        if soft:
            parts += ["", "## SOFT gaps — recommended", *(_finding_line(f) for f in soft)]
        if present:
            parts += ["", "## Documented (for the record)", *(_finding_line(f) for f in present)]
        if not self.findings:
            parts += ["", "_No applicable checklist items were missing._"]
        return "\n".join(parts)


def _finding_line(f: "AuditFinding") -> str:
    return f"- **[{f.category}] {f.item}** — {f.evidence.strip()}"


# --- Debate turn ------------------------------------------------------------


class DebateOutput(BaseModel):
    """One debate turn (advocate or skeptic)."""

    argument: str = Field(
        ...,
        description="A focused argument (≤250 words) that engages directly "
                    "with the other side's prior points.",
    )
    key_points: list[str] = Field(
        default_factory=list,
        description="2-5 short bullets summarizing the core claims of the argument.",
    )

    def to_markdown(self) -> str:
        parts = [self.argument.strip()]
        if self.key_points:
            parts += ["", "**Key points:**", *(f"- {p}" for p in self.key_points)]
        return "\n".join(parts)


# --- Meta-reviewer ----------------------------------------------------------


class MetaReviewOutput(BaseModel):
    """Area Chair's synthesis of reviewer + debate signal."""

    draft_recommendation: Verdict
    synthesis: str = Field(
        ...,
        description="Consensus across the panel and the key tensions.",
    )
    decisive_factors: str = Field(
        ...,
        description="What most drives the outcome. If diverging from the "
                    "confidence-weighted average verdict, name the reasoning.",
    )

    def to_markdown(self) -> str:
        return "\n".join([
            "# Meta-Review",
            "",
            f"**Draft recommendation:** {self.draft_recommendation}",
            "",
            "## Synthesis",
            self.synthesis.strip(),
            "",
            "## Decisive Factors",
            self.decisive_factors.strip(),
        ])


# --- Panel gaps -------------------------------------------------------------
#
# The three technical reviewers — data_analysis, methodology, rigor — read the
# manuscript independently and never see each other. This stage reads their
# reports together, with the manuscript, and looks for what they missed.
#
# The hard part is that an agent told "find what the reviewers missed" has
# every incentive to manufacture something. The grounding rule is therefore not
# "cite another report" — that would only ever surface points already made, and
# a gap by definition appears in no report — but "cite the manuscript". Every
# finding names the passage, figure or value it concerns, and names the lane
# that should have caught it, so a gap is routed to its specialist rather than
# floating free as a ninth opinion.


GapLane = Literal["data_analysis", "methodology", "rigor"]


class GapFinding(BaseModel):
    finding: str = Field(
        ...,
        description="The finding, stated as a referee would state it, in one "
                    "to three sentences.",
    )
    belongs_to: GapLane = Field(
        ...,
        description="Which reviewer's remit this falls in — the one that "
                    "should have caught it.",
    )
    manuscript_evidence: str = Field(
        ...,
        min_length=20,
        description="The specific sentence, figure, table or value in the "
                    "manuscript this concerns, quoted or named precisely "
                    "enough to look up. Not a paraphrase of a review.",
    )
    kind: Literal["gap", "joined"] = Field(
        ...,
        description="'gap' when no report raised this at all; 'joined' when "
                    "it follows from combining reports without any of them "
                    "stating it.",
    )
    drawn_from: list[str] = Field(
        default_factory=list,
        description="For kind='joined' only: the two or more reports it is "
                    "built from. Leave empty for a gap.",
    )
    why_it_matters: str = Field(
        ...,
        description="What this changes about the manuscript's claims, and "
                    "why the reports as filed are incomplete without it.",
    )
    severity: Literal["HARD", "SOFT"] = Field(
        ...,
        description="HARD if it undermines a claim the paper makes; SOFT if "
                    "it qualifies one.",
    )

    @model_validator(mode="after")
    def _joined_findings_name_their_sources(self) -> GapFinding:
        """A joined finding has to say what it joined.

        Without this the 'joined' label is free — anything can claim to follow
        from the panel — and the distinction between "the reviewers between
        them implied this" and "I thought of this" stops meaning anything to
        the editor reading it.
        """
        if self.kind == "joined" and len(self.drawn_from) < 2:
            raise ValueError(
                "kind is 'joined' but drawn_from names fewer than two "
                "reports. Name the reports it is built from, or set kind to "
                "'gap' if no report raised it."
            )
        return self


class PanelGapOutput(BaseModel):
    """What the technical reviewers missed, or an explicit finding of none."""

    findings: list[GapFinding] = Field(
        default_factory=list,
        description="Findings the panel did not make. An empty list is a "
                    "valid and useful answer.",
    )
    nothing_found_reason: str = Field(
        default="",
        description="Required when findings is empty: one sentence on what "
                    "you checked the reports for and did not find missing.",
    )

    @model_validator(mode="after")
    def _silence_must_be_explained(self) -> PanelGapOutput:
        """Finding nothing is legitimate and will often be right. An
        unexplained empty list is indistinguishable from a stage that failed,
        and the editor downstream cannot tell those apart."""
        if not self.findings and not self.nothing_found_reason.strip():
            raise ValueError(
                "findings is empty but nothing_found_reason is not set. Say "
                "in one sentence what you checked for and found no gap in."
            )
        return self

    def to_markdown(self) -> str:
        parts = ["# Panel Gaps", ""]
        if not self.findings:
            parts += [
                "**The technical reviewers left no gap worth reporting.**",
                "",
                self.nothing_found_reason.strip(),
            ]
            return "\n".join(parts)
        parts += [
            "Findings below were not made by the data-analysis, methodology "
            "or rigor reviewer. Each names the manuscript evidence it rests "
            "on and the reviewer whose remit it falls in.",
            "",
        ]
        for i, f in enumerate(self.findings, 1):
            label = "GAP" if f.kind == "gap" else f"JOINED from {', '.join(f.drawn_from)}"
            parts += [
                f"## {i}. [{f.severity}] [{label}] {f.finding.strip()}",
                "",
                f"**Belongs to:** {f.belongs_to}",
                "",
                f"**In the manuscript:** {f.manuscript_evidence.strip()}",
                "",
                f"**Why it matters:** {f.why_it_matters.strip()}",
                "",
            ]
        return "\n".join(parts).rstrip()


# --- Author rebuttal --------------------------------------------------------


class RebuttalConcession(BaseModel):
    reviewer: str = Field(..., description="Name of the reviewer who raised the critique.")
    critique: str = Field(..., description="One-line summary of the critique being conceded.")
    proposed_change: str = Field(..., description="Concrete change the author would make in revision.")


class RebuttalDisagreement(BaseModel):
    reviewer: str = Field(..., description="Name of the reviewer being pushed back on.")
    critique: str = Field(..., description="One-line summary of the critique being disputed.")
    response: str = Field(..., description="Author's response to the critique.")
    quoted_section: str = Field(
        default="",
        description="Specific manuscript section / figure / passage that supports the response.",
    )


class AuthorRebuttalOutput(BaseModel):
    """Author's response to the reviewer panel."""

    concessions: list[RebuttalConcession] = Field(default_factory=list)
    disagreements: list[RebuttalDisagreement] = Field(default_factory=list)
    load_bearing_critiques: list[str] = Field(
        default_factory=list,
        description="1-3 critiques that, if upheld, make acceptance impossible this cycle. "
                    "Empty list means the author considers all critiques addressable.",
    )

    def to_markdown(self) -> str:
        parts: list[str] = ["## Concessions"]
        if self.concessions:
            for c in self.concessions:
                parts.append(f"- **{c.reviewer}** — {c.critique}")
                parts.append(f"  - Proposed change: {c.proposed_change}")
        else:
            parts.append("(none)")

        parts += ["", "## Disagreements"]
        if self.disagreements:
            for d in self.disagreements:
                parts.append(f"- **{d.reviewer}** — {d.critique}")
                parts.append(f"  - Response: {d.response}")
                if d.quoted_section:
                    parts.append(f"  - Manuscript reference: {d.quoted_section}")
        else:
            parts.append("(none)")

        parts += ["", "## Load-bearing critiques"]
        if self.load_bearing_critiques:
            parts += (f"- {x}" for x in self.load_bearing_critiques)
        else:
            parts.append("None — author considers all critiques addressable in revision.")

        return "\n".join(parts)


# --- Editor-in-Chief --------------------------------------------------------


class EditorDecisionOutput(BaseModel):
    """Editor-in-Chief's final decision + author-facing letter."""

    decision: Verdict
    summary_of_evaluation: str = Field(
        ...,
        description="Editor's synthesis of meta-review + rebuttal + numerical signal.",
    )
    required_revisions: list[str] = Field(
        default_factory=list,
        description="Numbered, prioritized, actionable revision requirements.",
    )
    minor_suggestions: list[str] = Field(
        default_factory=list,
        description="Optional minor suggestions for the authors.",
    )

    def to_markdown(self) -> str:
        parts: list[str] = [
            "# Decision Letter",
            "",
            f"**Decision:** {self.decision}",
            "",
            "## Summary of Evaluation",
            self.summary_of_evaluation.strip(),
        ]
        if self.required_revisions:
            parts += ["", "## Required Revisions"]
            parts += (f"{i + 1}. {r}" for i, r in enumerate(self.required_revisions))
        if self.minor_suggestions:
            parts += ["", "## Minor Suggestions"]
            parts += (f"- {s}" for s in self.minor_suggestions)
        return "\n".join(parts)


# --- Desk screen (optional triage gate) -------------------------------------


class DeskScreenOutput(BaseModel):
    """Editorial triage: whether to desk-reject before the full review.

    Emitted by the optional desk-screen node that runs once, ahead of the
    reviewer fan-out. A ``desk_reject`` of ``True`` short-circuits the
    pipeline to a reject without spending the panel; ``False`` lets the
    manuscript proceed to the full review unchanged.
    """

    desk_reject: bool = Field(
        ...,
        description="True to reject the manuscript at the desk without sending "
                    "it out for full review; False to proceed to the panel.",
    )
    rationale: str = Field(
        ...,
        description="One short paragraph explaining the screening decision, "
                    "addressed to the authors.",
    )
    reasons: list[str] = Field(
        default_factory=list,
        description="Specific grounds for a desk reject (e.g. out of scope for "
                    "the venue, incomplete submission, fundamental flaw, clearly "
                    "below the venue's bar). Empty when the manuscript passes.",
    )

    def to_markdown(self) -> str:
        verdict = "Desk Reject" if self.desk_reject else "Passed Desk Screen"
        parts: list[str] = [
            "# Editorial Desk Screen",
            "",
            f"**Outcome:** {verdict}",
            "",
            "## Rationale",
            self.rationale.strip(),
        ]
        if self.reasons:
            parts += ["", "## Grounds"]
            parts += (f"- {r}" for r in self.reasons)
        return "\n".join(parts)


# --- Journal recommendations ------------------------------------------------


class JournalSuggestion(BaseModel):
    """One candidate venue for the manuscript."""

    name: str = Field(
        ...,
        description="Venue name as authors would write it (e.g. 'Nature Methods', "
                    "'JMLR', 'Bioinformatics', 'arXiv cs.LG').",
    )
    fit_reasoning: str = Field(
        ...,
        description="One to two sentences on why this venue matches the paper's "
                    "topic, scope, and methodology.",
    )
    acceptance_realism: str = Field(
        ...,
        description="One sentence on realistic acceptance odds at this venue given "
                    "the panel's verdict — be candid about tier mismatch when relevant.",
    )


class JournalRecommendationsOutput(BaseModel):
    """Tiered venue recommendations: as-is / after-revision / alternative."""

    as_is: list[JournalSuggestion] = Field(
        default_factory=list,
        description="Up to 3 venues where the manuscript is realistic at its "
                    "current quality (matching the panel's verdict).",
        max_length=3,
    )
    after_revision: list[JournalSuggestion] = Field(
        default_factory=list,
        description="Up to 3 venues that become realistic once the editor's "
                    "required revisions are addressed.",
        max_length=3,
    )
    alternative: list[JournalSuggestion] = Field(
        default_factory=list,
        description="Up to 3 fallback outlets (preprint server, workshop, "
                    "specialty journal) if the paper can't reach the headline venues.",
        max_length=3,
    )
    notes: str = Field(
        default="",
        description="Optional 1-2 sentences on topic-fit caveats or venue "
                    "considerations the authors should know.",
    )

    def to_markdown(self) -> str:
        parts: list[str] = [
            "# Journal Recommendations",
            "",
            "## As-is (current quality)",
        ]
        parts.extend(_render_bucket(
            self.as_is,
            empty="No headline venue is realistic at the current quality — see Alternative.",
        ))
        parts += ["", "## After required revisions"]
        parts.extend(_render_bucket(
            self.after_revision,
            empty="No higher-tier venue identified post-revision; the as-is venues remain the realistic targets.",
        ))
        parts += ["", "## Alternative outlets"]
        parts.extend(_render_bucket(
            self.alternative,
            empty="(none suggested)",
        ))
        if self.notes:
            parts += ["", "## Notes", self.notes.strip()]
        return "\n".join(parts)


def _render_bucket(suggestions: list[JournalSuggestion], *, empty: str) -> list[str]:
    if not suggestions:
        return [f"_{empty}_"]
    lines: list[str] = []
    for s in suggestions:
        lines.append(f"- **{s.name}**")
        lines.append(f"  - Fit: {s.fit_reasoning.strip()}")
        lines.append(f"  - Realism: {s.acceptance_realism.strip()}")
    return lines
