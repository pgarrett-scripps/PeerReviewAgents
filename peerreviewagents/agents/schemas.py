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

from pydantic import BaseModel, Field

Verdict = Literal["accept", "minor", "major", "reject"]


# --- Specialist reviewer ----------------------------------------------------


class ReviewerOutput(BaseModel):
    """One specialist reviewer's verdict + critique."""

    score: int = Field(
        ..., ge=1, le=5,
        description="1=reject, 2=major-reject, 3=major-revision, "
                    "4=minor-revision, 5=accept",
    )
    confidence: int = Field(
        ..., ge=1, le=5,
        description="How confident the reviewer is in their score (1=low, 5=high).",
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

    def to_markdown(self, role: str = "Reviewer") -> str:
        parts: list[str] = [
            f"# {role}",
            "",
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


# --- Memory reflection ------------------------------------------------------


class MemoryReflection(BaseModel):
    """LLM-extracted lesson learned by comparing a past decision to its outcome."""

    lesson: str = Field(
        ...,
        description="One to four sentences. What this run got right or wrong, and why.",
    )
    applies_when: list[str] = Field(
        default_factory=list,
        description="Short cues (manuscript topic, panel pattern, decision context) "
                    "that mark when this lesson is relevant in future runs.",
    )

