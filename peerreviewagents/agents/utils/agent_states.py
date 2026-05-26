"""Typed state objects flowing through the review graph."""

from __future__ import annotations

import operator
from typing import Annotated, TypedDict


class ReviewReport(TypedDict):
    """A single specialist reviewer's structured output."""
    reviewer: str
    summary: str
    strengths: list[str]
    weaknesses: list[str]
    questions: list[str]
    # 1 (reject) .. 5 (accept), per-reviewer confidence-weighted score
    score: float
    confidence: float
    body: str  # full markdown report


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

    # --- reviewer pass (parallel writers, hence reducers) ---
    reports: Annotated[list[ReviewReport], operator.add]

    # --- debate ---
    debate: Annotated[list[DebateTurn], operator.add]
    debate_round: int

    # --- synthesis ---
    meta_review: str
    draft_recommendation: str

    # --- integrity ---
    integrity_findings: Annotated[list[ReviewReport], operator.add]

    # --- final ---
    decision: str                # accept | minor | major | reject
    decision_letter: str

    # --- bookkeeping ---
    errors: Annotated[list[str], operator.add]
