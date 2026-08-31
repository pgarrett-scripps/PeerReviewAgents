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
    # Optional derived indexes. They may be empty even when body contains a
    # complete review; no editorial agent treats them as the source of truth.
    weaknesses: list[str]
    questions: list[str]
    body: str
    # explicit | normalized | unavailable | structured_fallback. Recording
    # provenance prevents an inferred scalar from masquerading as one the
    # reviewer actually printed.
    score_source: str


class AuditReport(TypedDict):
    """One editorial compliance auditor's output.

    Audits run in parallel with the reviewer panel but feed ONLY the editor:
    they are factual checklists (missing reagent IDs, unresolvable citations),
    not scientific-merit opinions, so they are kept out of ``reports`` and
    never enter the confidence-weighted score, the debate, or the
    debate synthesis. ``body`` is the rendered markdown from
    :class:`~peerreviewagents.agents.schemas.AuditOutput`; ``hard_gaps`` /
    ``soft_gaps`` are promoted scalars so the editor prompt and summary can
    show counts without parsing the body.
    """
    auditor: str        # "methods_completeness" | "citation_integrity"
    title: str          # human-facing title for rendering
    hard_gaps: int | None
    soft_gaps: int | None
    # Revision-compliance auditor only: one entry per required revision, as
    # ``{id, status, blocking}``. The editor's round-delta needs per-item
    # outcomes, and recovering them by parsing the rendered body would put
    # a string dependency on someone else's markdown.
    findings: list[dict]
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
    # The bibliography as the converter typed it: one dict per entry, in the
    # order the manuscript prints them, carrying `raw` always and `label`,
    # `authors`, `title`, `year`, `doi`, `arxiv` where they parsed. Empty for
    # a submission with no document model behind it (Markdown, LaTeX) and for
    # a converter that types no reference blocks — so the two agents that read
    # it keep the path that reads the prose. Passed only to those two, as a
    # cached block after the shared prefix (agent_utils.references_block).
    references: list[dict]
    # How the manuscript was read, from ingest.loader.Manuscript.ingest:
    # format, tool (with version), caveman level, chars, and the reason the
    # structured backend was not used. Carried on the state because it is
    # published in a review's provenance — a reader checking a quotation
    # against the PDF needs to know whether the panel read a conversion of
    # it, and whether that conversion was compressed.
    ingest: dict
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

    # --- revision round (set only when config["revision_of"] is given) ---
    # The previous round's structured record (peerreviewagents.rounds.RoundRecord).
    # Its presence switches the two agents that are allowed to know: the
    # compliance auditor, which gets the previous letter's required-revision
    # list, and the editor, which gets a round-over-round delta. The reviewer
    # panel is deliberately NOT one of them — it reviews the manuscript in
    # front of it and is never told a previous round exists. None on a first
    # round.
    prior_round: Any
    # The real author's response letter, as submitted. Untrusted input, and
    # the only input written by someone with a stake in the verdict. Two
    # editor-facing agents read it as quoted data — the response verifier and
    # the revision-compliance auditor — and NEITHER forwards it as prose. The
    # panel sees it only as the verifier's checked pointers.
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

    # Set only after every reviewer and auditor in the fan-out has returned.
    # False is terminal: synthesis and the editor must not issue a decision
    # from an accidental subset of the requested panel.
    panel_complete: bool
    # True when synthesis proceeded under the configured quorum rather than
    # with every requested specialist. Such a run may have a useful verdict,
    # but is explicitly not publication-ready and is counted as degraded.
    panel_degraded: bool
    # Explicit lifecycle: running -> panel_complete -> publishable, or a
    # terminal incomplete state. Optional enrichment failures may coexist
    # with publication_ready=True.
    run_status: str
    publication_ready: bool

    # --- editorial compliance audit lane (parallel, feeds only the editor) ---
    # Auditors fan out alongside the reviewers but their output bypasses the
    # debate/synthesis and lands directly in the editor's prompt. Separate
    # channel so it stays out of score_summary() and the panel verdict.
    audits: Annotated[list[AuditReport], operator.add]

    # --- debate ---
    debate: Annotated[list[DebateTurn], operator.add]
    debate_round: int

    # --- panel gaps ---
    # What the three technical reviewers missed. Written between the panel
    # and the debate; empty when the stage is off, when it failed, or when
    # none of those three lanes reported.
    panel_gaps: str

    # --- synthesis ---
    # The debate synthesizer's condensed account of the finished debate: the
    # one version the editor reads (the raw transcript is published but not
    # fed to the editor). Empty when debate is ablated; a failure marker
    # when the synthesizer errored, in which case the editor falls back to
    # the raw transcript.
    debate_synthesis: str

    # --- author rebuttal ---
    # Free-text markdown the "author" agent writes to defend the
    # manuscript against the reviewer panel before the editor decides.
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
