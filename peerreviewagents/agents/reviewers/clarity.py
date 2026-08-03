from ...ingest import prose
from ..utils.agent_states import ReviewState
from .base import make_reviewer_node

# Which statistics this reviewer sees, and how to say each one in words a
# referee would use. Deliberately a short list.
#
# Left out, and why, because the temptation to add them back is the whole risk
# here. Hedges and boosters measure overclaiming, which is rigor's and
# novelty's verdict rather than clarity's. MATTR is a lexical-diversity ratio
# with no reference distribution attached, so a reviewer handed 0.47 has
# nothing to compare it against and will invent a comparison. Citations,
# p-values and numbers-per-1000 are evidence density, not presentation. Every
# one of them would arrive as a number begging for a sentence about it.
_LINES = (
    ("sentence_len_median", "median sentence length: {:.0f} words"),
    ("sentence_len_p90", "90th percentile sentence length: {:.0f} words"),
    ("long_sentence_share", "share of sentences over 40 words: {:.0%}"),
    ("passive_per_sentence_approx", "passive constructions: ~{:.2f} per sentence"),
)


def _stats_note(state: ReviewState) -> str:
    """Rough text statistics for the clarity reviewer, heavily caveated.

    This is the one place any statistic reaches a prompt, and it is scoped to
    the one reviewer whose remit is how the paper reads. The framing does the
    real work: an agent handed a bare number writes a finding about the
    number, so every number here arrives already labelled unreliable, with an
    explicit instruction that it is orientation and never a defect on its own.

    Returns '' when the numbers would describe something other than the
    authors' prose. Under caveman compression the density block is not
    computed at all, because the compressor strips the function words that
    sentence length and passive voice are measured on.
    """
    ingest = state.get("ingest") or {}
    stats = ingest.get("prose") or {}
    density, counts = stats.get("density"), stats.get("counts") or {}
    if not density:
        return ""

    measured = [
        text.format(density[key])
        for key, text in _LINES
        if isinstance(density.get(key), (int, float))
    ]
    if not measured:
        return ""

    words = counts.get("main_text_words") or counts.get("words") or 0
    size = f"roughly {words:,} words of main text; " if words else ""

    # A degraded conversion is precisely when these numbers are worst, so the
    # warning gets stronger there rather than staying generic.
    reliability = (
        "This manuscript's conversion was flagged as imperfect, so treat them "
        "as worse than usual."
        if prose.verdict_of(ingest) != prose.CLEAN
        else "They are rough."
    )

    return (
        "\n\n"
        "Rough text statistics, for orientation only:\n"
        f"  {size}" + "; ".join(measured) + ".\n\n"
        f"{reliability} They are counted mechanically off a PDF converted to "
        "markdown, and that conversion is imperfect in ways that hit exactly "
        "these measures: equations, abbreviations and lost spaces all split "
        "sentences in the wrong places, and the passive figure is a regex that "
        "cannot tell 'was performed' from 'was unclear'.\n\n"
        "So do not report a statistic as a finding, and do not quote one of "
        "these numbers in your review. A long median sentence is not a defect; "
        "dense technical prose is often long and perfectly clear. Use them only "
        "as a hint about where to look, and where a number disagrees with what "
        "you read on the page, the page is right."
    )


node = make_reviewer_node(
    "clarity",
    role="Clarity & Presentation Reviewer",
    mandate_extra=_stats_note,
    mandate=(
        "Check specific, enumerable presentation defects rather than giving a "
        "general impression that 'the writing is fine.' Guiding question: can a "
        "competent reader in the field follow the argument and understand "
        "exactly what was done and claimed, without guessing? HARD = meaning is "
        "genuinely ambiguous or unparseable (two competent readers could "
        "reasonably disagree about what a sentence or figure means); SOFT = "
        "friction or redundancy that slows the reader but doesn't block "
        "understanding.\n\n"
        "Cross-cutting HARD: the central contribution/claim is stated "
        "explicitly somewhere, not left implicit; every non-standard term, "
        "abbreviation, and symbol is defined at first use; pronouns and "
        "'this'/'it' references are unambiguous in claim sentences. SOFT: "
        "consistent terminology (the same thing isn't called three names); "
        "paragraphs lead with their point and sections don't repeat each other.\n\n"
        "Conditional checks — apply only where the trigger appears:\n"
        "  - Structure & narrative (full manuscript): the reader can "
        "reconstruct what question is being asked before the results arrive, "
        "and each finding's purpose is clear, not a list of experiments with no "
        "through-line (HARD); smooth motivation→methods→results→interpretation "
        "flow, no orphaned results never referenced again (SOFT).\n"
        "  - Figures & tables: every panel referenced in text exists and every "
        "panel that exists is referenced; axes labeled with units; legends "
        "define every symbol, color, and abbreviation; the figure is "
        "understandable from caption + labels alone (HARD); colorblind-safe "
        "colors, legible fonts, narrative panel order (SOFT).\n"
        "  - Quantitative/comparative statements: quantitative claims state the "
        "actual quantity, not just direction ('increased 3-fold', not only "
        "'increased'); comparatives state compared-to-what ('higher than "
        "vehicle', not 'higher') (HARD); vague qualifiers ('most', 'often') "
        "quantified where possible (SOFT).\n"
        "  - Methods readability: the order of operations is recoverable (no "
        "step depending on an undescribed earlier step); it's clear which "
        "experiments used which conditions/groups (HARD).\n"
        "  - Abstract & framing: the abstract's claims are intelligible "
        "standalone and use terms it introduces (HARD); the intro frames the "
        "gap without overselling (SOFT).\n\n"
        "Flag ambiguity, not taste — a HARD clarity issue is one where meaning "
        "is genuinely unrecoverable, not merely awkward phrasing. A claim "
        "unclear because the evidence is thin is rigor's verdict; a method that "
        "is clear but missing identifiers is methods_completeness's. Quote the "
        "specific sentence or figure and say what a reader cannot determine "
        "from it."
    ),
)
