"""Typed state objects flowing through the review graph."""

from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict


class ReviewReport(TypedDict):
    """A single specialist reviewer's output.

    ``body`` is the rendered markdown report produced from the agent's
    :class:`~peerreviewagents.agents.schemas.ReviewerOutput` via
    ``to_markdown()``. The ``score`` and ``confidence`` scalars are
    promoted to top-level fields so downstream consumers (score_summary,
    debate digest, reports.py) can read them without touching the body.
    """
    reviewer: str
    # 1 (reject) .. 5 (accept), per-reviewer confidence-weighted score
    score: float
    confidence: float
    # Promoted alongside the scalars so a revision round can give this
    # reviewer its own prior points back, addressable by id, without parsing
    # them out of the rendered body.
    weaknesses: list[str]
    questions: list[str]
    body: str


class AuditReport(TypedDict):
    """One editorial compliance auditor's output.

    Audits run in parallel with the reviewer panel but feed ONLY the editor:
    they are factual checklists (missing reagent IDs, unresolvable citations),
    not scientific-merit opinions, so they are kept out of ``reports`` and
    never enter the confidence-weighted score, the debate, or the
    meta-review. ``body`` is the rendered markdown from
    :class:`~peerreviewagents.agents.schemas.AuditOutput`; ``hard_gaps`` /
    ``soft_gaps`` are promoted scalars so the editor prompt and summary can
    show counts without parsing the body.
    """
    auditor: str        # "methods_completeness" | "citation_integrity"
    title: str          # human-facing title for rendering
    hard_gaps: int
    soft_gaps: int
    body: str


class DebateTurn(TypedDict):
    role: str  # "advocate" | "skeptic"
    round: int
    content: str


class ReviewState(TypedDict, total=False):
    """The shared blackboard for a single manuscript review run."""

    # --- inputs ---
    manuscript_path: str
    manuscript_title: str
    manuscript_md: str           # normalized full text
    sections: dict[str, str]     # section name -> text
    # Optional supplementary information, parsed from a separate SI file when
    # one is provided (config["supplement_path"]). Passed in FULL (untruncated)
    # only to agents that opt in — currently just the methods_completeness
    # auditor. Empty string / empty dict when no SI was supplied; agents that
    # don't opt in never see it.
    supplement_md: str
    supplement_sections: dict[str, str]
    config: dict
    # Target-venue context. Rendered prompt block from the selected
    # JournalProfile.to_prompt_block(); empty string when no target
    # journal is set (venue-agnostic review). Parsed once in
    # PeerReviewGraph.initial_state and shared by every agent.
    journal_block: str
    # Manuscript-type context. Rendered prompt block naming the kind of
    # submission (Article, Letter, Review, ...) and how to weigh it, plus any
    # per-type word caps from the target journal. Empty string when no
    # article type is selected. Built once in PeerReviewGraph.initial_state.
    article_type_block: str
    # Review-strictness directive. Rendered prompt block from
    # peerreviewagents.strictness.strictness_block(level); empty string at
    # the balanced default (level 3). Built once in
    # PeerReviewGraph.initial_state and folded into the evaluative agents'
    # shared cached prefix by context_block().
    strictness_block: str

    # --- optional desk screen (triage gate before the reviewer fan-out) ---
    # Set by the desk_screen node when enabled. desk_rejected=True means the
    # editor rejected at the desk; the run short-circuits to END with
    # decision="reject" and no reviewer reports. desk_screen holds the
    # rendered screening note (written to desk_screen.md).
    desk_rejected: bool
    desk_screen: str
    # --- submission-integrity screen (deterministic, runs at the desk) ---
    # Rendered evidence report from ingest.integrity.IntegrityScan when the
    # scan found concealed text or reviewer-directed instructions; empty
    # string on a clean file. Written to integrity.md. When the file conceals
    # instructions aimed at an automated reviewer, the desk node also sets
    # desk_rejected/decision from this same report.
    integrity: str

    # --- revision round (set only when config["revision_of"] is given) ---
    # The previous round's structured record (peerreviewagents.rounds.RoundRecord).
    # Its presence is what puts every agent into revision mode: reviewers get
    # their own prior report, the compliance auditor gets the required-revision
    # list, and the editor gets a round-over-round delta. None on a first round.
    prior_round: Any
    # Section-aware v1 -> v2 comparison (ingest.diff.ManuscriptDiff). Always
    # present in a revision round; `available=False` when the previous draft
    # could not be recovered from the ingest cache.
    manuscript_diff: Any
    # The real author's response letter, as submitted. Untrusted input: it is
    # never placed in an agent's prompt as prose. Only the verifier reads it,
    # and only its adjudicated output reaches the panel.
    author_statement: str
    # Rendered ResponseVerificationOutput markdown; written to
    # author_response_verification.md. Empty when no statement was supplied.
    response_verification: str
    # The verifier's panel-facing block: corroborated pointers only, no
    # conclusions. This is the ONLY channel by which the author's letter
    # reaches a reviewer.
    verified_claims_block: str

    # --- reviewer pass (parallel writers, hence reducers) ---
    reports: Annotated[list[ReviewReport], operator.add]

    # --- editorial compliance audit lane (parallel, feeds only the editor) ---
    # Auditors fan out alongside the reviewers but their output bypasses the
    # debate/meta-review and lands directly in the editor's prompt. Separate
    # channel so it stays out of score_summary() and the panel verdict.
    audits: Annotated[list[AuditReport], operator.add]

    # --- debate ---
    debate: Annotated[list[DebateTurn], operator.add]
    debate_round: int

    # --- synthesis ---
    meta_review: str
    draft_recommendation: str

    # --- author rebuttal ---
    # Free-text markdown the "author" agent writes to defend the
    # manuscript against the reviewer panel before the editor decides.
    # Sits between the meta-review and the editor.
    author_rebuttal: str

    # --- final ---
    decision: str                # accept | minor | major | reject
    decision_letter: str
    # The editor's structured asks, kept alongside the rendered letter so the
    # round record can assign stable ids to them and a later round can check
    # them off. Without these, referencing round N-1 would mean parsing
    # numbered bullets back out of markdown.
    required_revisions: list[str]
    minor_suggestions: list[str]

    # --- post-decision: venue recommendations ---
    # Rendered markdown from JournalRecommendationsOutput.to_markdown().
    journal_recommendations: str

    # --- bookkeeping ---
    errors: Annotated[list[str], operator.add]
    # Sum of OpenRouter-reported per-call USD cost across every LLM
    # invocation in the run. Surfaced in summary.md so users can size
    # the cost of pointing this at a 50-page preprint.
    total_cost: Annotated[float, operator.add]
