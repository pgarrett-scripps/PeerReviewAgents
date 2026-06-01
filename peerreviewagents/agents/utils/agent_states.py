"""Typed state objects flowing through the review graph."""

from __future__ import annotations

import operator
from typing import Annotated, TypedDict


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
    config: dict
    # Target-venue context. Rendered prompt block from the selected
    # JournalProfile.to_prompt_block(); empty string when no target
    # journal is set (venue-agnostic review). Parsed once in
    # PeerReviewGraph.initial_state and shared by every agent.
    journal_block: str

    # --- reviewer pass (parallel writers, hence reducers) ---
    reports: Annotated[list[ReviewReport], operator.add]

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

    # --- post-decision: venue recommendations ---
    # Rendered markdown from JournalRecommendationsOutput.to_markdown().
    journal_recommendations: str

    # --- bookkeeping ---
    errors: Annotated[list[str], operator.add]
    # Sum of OpenRouter-reported per-call USD cost across every LLM
    # invocation in the run. Surfaced in summary.md so users can size
    # the cost of pointing this at a 50-page preprint.
    total_cost: Annotated[float, operator.add]
