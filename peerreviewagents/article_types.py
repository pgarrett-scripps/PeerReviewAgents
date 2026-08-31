"""Manuscript types: a venue-general taxonomy of submission kinds.

An article type is the *kind* of submission — a full research Article, a
Letter, a Review, a Perspective, and so on. Unlike the per-venue
:class:`~peerreviewagents.journals.JournalProfile`, the taxonomy itself is
largely universal: what a "Letter" or a "Review" fundamentally is, and how a
reviewer should weigh it, does not change much from journal to journal. So the
general description and review framing for each type live here, shared across
every venue.

What *is* venue-specific — word/abstract caps, presubmission requirements — is
supplied per journal as optional overrides in the journal TOML (see
``[article_types]`` in a profile) and folded into the rendered block at
runtime. A journal that supplies no overrides still gets the general framing.

A selected type is rendered to a prompt block via :func:`article_type_block`,
stored once in the run state, and folded into the shared cached prefix by
:func:`peerreviewagents.agents.utils.agent_utils.context_block`. That prefix is
consumed by the evaluative agents (reviewers, debate synthesizer, editor), so naming
the manuscript type tells the panel what kind of work it is judging — a Letter
or Review is not held to a research Article's bar for novel experimental data.

When no type is selected the block is empty and a run is byte-identical to the
pre-article-type pipeline, mirroring how strictness level 3 renders to ``""``.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ArticleType:
    """One universal manuscript type.

    ``description`` says what the type *is*; ``review_framing`` says how a
    reviewer should adjust their expectations for it (the part that actually
    steers the panel). Both are venue-general — per-venue specifics (word
    caps, presubmission rules) arrive as overrides at render time.
    """

    key: str
    name: str
    description: str
    review_framing: str


# The shared taxonomy. Keys are kebab-case slugs used for selection. This set
# covers the common ACS/Nature/Cell-style menu; a venue advertises the subset
# it accepts (and any caps) in its own profile, but does not redefine what a
# type means.
ARTICLE_TYPES: dict[str, ArticleType] = {
    "article": ArticleType(
        key="article",
        name="Research Article",
        description=(
            "A full-length, original research contribution presenting new data "
            "and the conclusions drawn from it."
        ),
        review_framing=(
            "Hold it to the full standard for original research: novelty, "
            "methodological rigor, completeness of evidence, statistical "
            "support, and reproducibility."
        ),
    ),
    "letter": ArticleType(
        key="letter",
        name="Letter",
        description=(
            "A short opinion or commentary piece on the field, optionally "
            "supported by brief data and references."
        ),
        review_framing=(
            "Judge the clarity, relevance, and soundness of the argument "
            "rather than the completeness of new experimental data; do not "
            "hold it to a full research article's evidentiary bar."
        ),
    ),
    "communication": ArticleType(
        key="communication",
        name="Communication",
        description=(
            "A concise, time-sensitive account of significant new findings that "
            "merit expedited handling."
        ),
        review_framing=(
            "Weigh the urgency and significance of the result and whether the "
            "core claims are adequately supported despite the brevity; do not "
            "penalize it for the depth or breadth expected of a full article."
        ),
    ),
    "perspective": ArticleType(
        key="perspective",
        name="Perspective",
        description=(
            "The author's forward-looking view of a new direction in the field. "
            "It is not an account or analysis of the author's own research."
        ),
        review_framing=(
            "Judge the insight, balance, and significance of the proposed "
            "direction and its grounding in the literature, not the presence of "
            "new experimental data."
        ),
    ),
    "review": ArticleType(
        key="review",
        name="Review",
        description=(
            "A comprehensive, critical synthesis of work in a selected area of "
            "the literature."
        ),
        review_framing=(
            "Judge the thoroughness of coverage, the critical organization and "
            "insight (a bare list of citations is inadequate), and the current "
            "importance of the field; new data is not expected."
        ),
    ),
    "technical-note": ArticleType(
        key="technical-note",
        name="Technical Note",
        description=(
            "A brief description of a novel instrument, method, or software "
            "advance."
        ),
        review_framing=(
            "Require a clear demonstration of the advantage over existing "
            "approaches; judge practical utility and validation rather than "
            "broad biological significance."
        ),
    ),
    "tutorial": ArticleType(
        key="tutorial",
        name="Tutorial",
        description=(
            "An instructional article that teaches the reader how to accomplish "
            "a specific technique or application, covering the relevant "
            "background."
        ),
        review_framing=(
            "Judge pedagogical clarity, correctness, and coverage of the "
            "relevant background; new data is not expected."
        ),
    ),
    "conference-paper": ArticleType(
        key="conference-paper",
        name="Conference Paper",
        description=(
            "An archival submission to a peer-reviewed machine-learning "
            "conference (e.g. ICLR, NeurIPS, ICML): a complete research paper "
            "reviewed competitively, with an author-rebuttal phase and a binary "
            "accept/reject outcome for the proceedings."
        ),
        review_framing=(
            "Judge it by conference norms, not journal norms: a competitive but "
            "attainable bar (a solid, complete contribution should pass), a "
            "~9-page main-text budget with an unlimited appendix, and an "
            "accept-vs-reject decision rather than a revise-and-resubmit. Weigh "
            "contribution, empirical or theoretical soundness, and "
            "reproducibility; treat fixable presentation issues as addressable "
            "in rebuttal. Verdicts of accept/minor mean it clears the bar; "
            "major/reject mean it does not — do not default to major revision."
        ),
    ),
    "grant-proposal": ArticleType(
        key="grant-proposal",
        name="Grant Proposal",
        description=(
            "A research-funding application (e.g. NIH R01, NSF standard "
            "proposal, ERC grant): a proposal of work to be carried out, "
            "not a report of completed research."
        ),
        review_framing=(
            "Judge it as a proposal of FUTURE work, not a finished manuscript. "
            "Weigh significance/impact (does success meaningfully advance the "
            "field?), innovation, and the soundness and feasibility of the "
            "proposed approach, alongside the rigor of the plan. Preliminary "
            "data matters only as evidence of feasibility — the absence of "
            "complete results is expected and is NOT a weakness; do not demand "
            "the finished evidence you would require of a research article. "
            "Where the funder profile describes investigator/environment, "
            "budget, or broader/societal-impact criteria, weigh those too. Map "
            "the score and verdict to a funding decision rather than a "
            "publication one: accept/minor mean fundable (high priority); major "
            "means not fundable as submitted but worth resubmitting with "
            "revisions; reject means not competitive / would not be funded. Do "
            "not default to major revision — say plainly whether it would be "
            "funded."
        ),
    ),
    "exploratory-grant": ArticleType(
        key="exploratory-grant",
        name="Exploratory / Seed Grant Proposal",
        description=(
            "A short, higher-risk exploratory or seed funding application "
            "(e.g. NIH R21): early-stage or high-risk/high-reward work where "
            "preliminary data is limited or absent by design."
        ),
        review_framing=(
            "Judge it as an EXPLORATORY proposal of future work with an "
            "explicitly higher risk tolerance. Weigh the potential impact and "
            "innovation of the idea over exhaustive rigor or completeness: "
            "preliminary data is NOT required and its absence must not be "
            "penalized, and the scope is deliberately smaller than a full "
            "research grant. Still require that the approach is plausible and "
            "the aims are feasible within the mechanism's limits. Map the score "
            "and verdict to a funding decision: accept/minor mean fundable; "
            "major means not fundable as submitted but worth resubmitting; "
            "reject means not competitive. Do not default to major revision, "
            "and do not down-score it merely for being preliminary or risky — "
            "that is the point of the mechanism."
        ),
    ),
}


def normalize_article_type(value: object) -> str:
    """Coerce ``value`` to a valid article-type key, ``""``, or raise.

    ``None`` / empty string means *no type selected* and returns ``""`` (the
    block renders empty). Otherwise the value is lowercased and its spaces /
    underscores normalized to hyphens before lookup, so ``"Technical Note"``
    and ``"technical_note"`` both resolve. An unknown key raises ``ValueError``
    listing the valid keys — callers that want to fail fast (CLI, web form)
    surface it; the graph degrades to ``""`` so a library caller never crashes
    mid-run.
    """
    if value is None:
        return ""
    key = str(value).strip().lower().replace(" ", "-").replace("_", "-")
    if not key:
        return ""
    if key not in ARTICLE_TYPES:
        valid = ", ".join(ARTICLE_TYPES)
        raise ValueError(
            f"unknown article type {value!r}; expected one of: {valid}"
        )
    return key


def article_type_label(key: str) -> str:
    """Human-readable name for a key (e.g. ``"Letter"``), or the key itself."""
    at = ARTICLE_TYPES.get(key)
    return at.name if at else key


def article_type_block(
    key: str,
    *,
    max_words: int = 0,
    abstract_max_words: int = 0,
    notes: str = "",
) -> str:
    """Render the prompt block for an article type.

    Returns ``""`` for an empty/unknown key so the shared cached prefix is
    unchanged when no type is in use. ``max_words`` / ``abstract_max_words`` /
    ``notes`` are the optional per-venue overrides supplied by the selected
    journal; each is included only when set.
    """
    at = ARTICLE_TYPES.get(key)
    if at is None:
        return ""
    lines = [
        "=== MANUSCRIPT TYPE ===",
        f"Type: {at.name}",
        f"This submission is a {at.name}: {at.description}",
        f"When reviewing: {at.review_framing}",
    ]
    limits: list[str] = []
    if max_words:
        limits.append(f"main text ≤ {max_words} words")
    if abstract_max_words:
        limits.append(f"abstract ≤ {abstract_max_words} words")
    if limits:
        lines.append(f"Length limits for this type at this venue: {'; '.join(limits)}.")
    if notes:
        lines.append(notes.strip())
    lines.append("=== END MANUSCRIPT TYPE ===")
    return "\n".join(lines)
