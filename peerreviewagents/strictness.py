"""Review strictness: a 1-5 dial that makes the panel easier or harsher.

Strictness is the evaluative analogue of the target-journal mechanism in
:mod:`peerreviewagents.journals`. A level is rendered to a prompt block via
:func:`strictness_block`, stored once in the run state, and folded into the
shared cached prefix by
:func:`peerreviewagents.agents.utils.agent_utils.context_block`. That prefix
is consumed by exactly the evaluative agents — the reviewers, the
debate synthesizer, and the editor — so the dial changes how the manuscript is
*judged* without touching the author-rebuttal, the debaters, or the journal
recommender.

The scale is a slider-friendly integer:

    1  Very lenient
    2  Lenient
    3  Balanced   (default — emits an EMPTY block, i.e. today's behavior)
    4  Strict
    5  Very strict

Level 3 deliberately renders to ``""`` so a default run is byte-identical to
the pre-strictness pipeline: existing prompts, the provider-side cache, and
report output are all unchanged unless the user opts in.
"""

from __future__ import annotations

MIN_LEVEL = 1
MAX_LEVEL = 5
DEFAULT_LEVEL = 3

LABELS: dict[int, str] = {
    1: "Very lenient",
    2: "Lenient",
    3: "Balanced",
    4: "Strict",
    5: "Very strict",
}

# Per-level directive folded into the evaluative agents' shared context.
# Level 3 has no entry: a balanced review needs no extra instruction, and an
# empty block keeps the default run identical to the old behavior.
_DIRECTIVES: dict[int, str] = {
    1: (
        "Apply a deliberately lenient bar. Give the manuscript the benefit of "
        "the doubt: reward the core contribution and the questions it opens, "
        "and treat anything that could be fixed in revision as minor. Reserve "
        "low scores and rejection for fundamental, unfixable flaws, and when "
        "in doubt lean toward acceptance or minor revision."
    ),
    2: (
        "Apply a lenient bar. Weigh strengths generously and give borderline "
        "concerns the benefit of the doubt; do not let fixable weaknesses "
        "drive a harsh score. When a weakness is plausibly addressable in "
        "revision, prefer a revision verdict over rejection."
    ),
    4: (
        "Apply a demanding, top-venue bar. Hold the manuscript to a high "
        "standard of evidence, rigor, and novelty, and treat unaddressed "
        "weaknesses as blocking rather than cosmetic. When in doubt lean "
        "toward the more critical score, and reserve high scores for work "
        "that clearly clears the bar."
    ),
    5: (
        "Apply an exacting, highest-tier bar. Demand strong, complete evidence "
        "for every claim and scrutinize methods, novelty, and reproducibility "
        "without charity. Treat any significant unaddressed weakness as grounds "
        "for rejection, and default to the more critical verdict whenever there "
        "is doubt. Reserve acceptance for work that is rigorous, novel, and "
        "essentially complete."
    ),
}


def normalize_strictness(value: object) -> int:
    """Coerce ``value`` to a valid strictness level or raise ``ValueError``.

    Accepts ints or int-like strings (``"4"``). Callers that want to fail
    fast (CLI, web form) surface the ``ValueError``; the graph degrades to
    the default instead so a library caller never crashes mid-run.
    """
    try:
        level = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise ValueError(
            f"invalid review strictness {value!r}; expected an integer "
            f"{MIN_LEVEL}-{MAX_LEVEL}"
        )
    if not MIN_LEVEL <= level <= MAX_LEVEL:
        raise ValueError(
            f"review strictness {level} out of range; expected {MIN_LEVEL}-{MAX_LEVEL}"
        )
    return level


def strictness_label(level: int) -> str:
    """Human-readable name for a level (e.g. ``"Strict"``)."""
    return LABELS.get(level, "Balanced")


def strictness_block(level: int) -> str:
    """Render the prompt block for a strictness level.

    Returns ``""`` for the balanced default (or any level with no directive)
    so the shared cached prefix is unchanged when strictness is not in use.
    """
    directive = _DIRECTIVES.get(level)
    if not directive:
        return ""
    return (
        "=== REVIEW STRICTNESS ===\n"
        f"Level: {level}/{MAX_LEVEL} ({strictness_label(level)})\n"
        f"{directive}\n"
        "=== END REVIEW STRICTNESS ==="
    )
