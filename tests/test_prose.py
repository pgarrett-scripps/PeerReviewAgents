"""What the deterministic text statistics measure, and what they refuse to.

The refusals matter as much as the measurements. Every number here ends up in
a report a human reads, and a confident wrong count — "this manuscript cites
nothing", "the main text is 15 000 words" — is worse than an absent one.
"""

from __future__ import annotations

from peerreviewagents.ingest import prose

# --- sentence splitting ----------------------------------------------------


def test_abbreviations_do_not_end_sentences():
    text = (
        "We follow Smith et al. and report the result. "
        "Values were normalised (cf. Fig. 3) before analysis."
    )
    assert len(prose.split_sentences(text)) == 2


def test_decimals_and_initials_do_not_end_sentences():
    text = "The threshold was 0.05 in every run. J. R. Smith reported 1.96 as the cutoff."
    assert len(prose.split_sentences(text)) == 2


def test_sentences_are_still_split_on_real_boundaries():
    text = "We did the thing. It worked. Did it? Yes!"
    assert len(prose.split_sentences(text)) == 4


def test_empty_text_has_no_sentences():
    assert prose.split_sentences("   \n  ") == []


# --- markdown is not prose -------------------------------------------------


def test_tables_math_and_code_are_excluded_from_word_counts():
    text = (
        "# Results\n\n"
        "The effect was present.\n\n"
        "| gene | count |\n| --- | --- |\n| TP53 | 12 |\n\n"
        "$$\\sum_{i=1}^{n} x_i$$\n\n"
        "```python\nprint('hello world here')\n```\n"
    )
    stats = prose.analyze(text)
    # "Results", "The effect was present" -> 5 words, and nothing from the
    # table, the equation, or the code block.
    assert stats.counts.words == 5
    assert stats.counts.table_rows == 3
    assert stats.counts.display_math == 1


def test_heading_text_counts_but_heading_marks_do_not():
    stats = prose.analyze("## Materials and Methods\n\nWe used a microscope.\n")
    assert stats.counts.words == 7


# --- health ----------------------------------------------------------------


def test_a_clean_conversion_is_clean():
    text = " ".join(["The sample was prepared and then measured carefully."] * 40)
    health = prose.analyze(text).health
    assert health.verdict == prose.CLEAN
    assert health.notes == []
    assert health.usable


def test_fused_words_break_the_verdict():
    # The real failure mode, verbatim in shape: spaces lost between words.
    fused = "whicharemosteffectivewhenasmallwelldefinedsitecanbeengaged"
    text = " ".join([f"Some ordinary words here {fused}"] * 12)
    health = prose.analyze(text).health
    assert health.verdict == prose.BROKEN
    assert not health.usable
    assert any("fused" in n for n in health.notes)


def test_hyphenated_line_breaks_degrade_but_never_break():
    # BERT reads perfectly and scores 33.7 per 1000 words on this signal, so
    # it must not be able to condemn a manuscript on its own.
    text = "\n".join(["the mea-\nsurement was taken"] * 60)
    health = prose.analyze(text).health
    assert health.verdict == prose.DEGRADED
    assert health.usable


def test_a_failed_section_split_is_reported_not_hidden():
    text = "Some prose that never matched a heading. " * 30
    health = prose.analyze(text, sections={"_preamble": text}).health
    assert health.preamble_share == 1.0
    assert not health.sections_usable
    assert any("no known section" in n for n in health.notes)


def test_preamble_share_is_unknown_without_a_section_map():
    health = prose.analyze("Some prose.").health
    assert health.preamble_share is None
    assert not health.sections_usable


# --- counts ----------------------------------------------------------------


def test_main_text_excludes_the_reference_list():
    body = "We measured the thing carefully. " * 10
    refs = "[1] Smith J. A paper. Journal. 2020.\n[2] Jones B. Another. Journal. 2021.\n"
    stats = prose.analyze(body + refs, sections={"introduction": body, "references": refs})
    assert stats.counts.references_separable
    assert stats.counts.reference_words > 0
    assert stats.counts.main_text_words == stats.counts.words - stats.counts.reference_words


def test_main_text_is_withheld_when_references_were_not_found():
    # A reference list is 19% of a paper's words at the median and 48% at the
    # worst, so a main-text count guessed without one is not worth having.
    body = "We measured the thing carefully. " * 10
    stats = prose.analyze(body, sections={"_preamble": body})
    assert not stats.counts.references_separable
    assert stats.counts.main_text_words is None


# --- density ---------------------------------------------------------------


def test_hedges_and_boosters_are_counted_separately():
    text = (
        "The result may suggest an effect, and could potentially generalise. "
        "This clearly demonstrates a novel and unprecedented advance."
    )
    density = prose.analyze(text).density
    assert density.hedges_per_1k > 0
    assert density.boosters_per_1k > 0


def test_multiword_hedges_match():
    density = prose.analyze("The finding is consistent with prior work. " * 20).density
    assert density.hedges_per_1k > 0


def test_citation_style_is_detected():
    numeric = "We build on prior work [1]. Others disagree [2, 3]. See also [4-7]. " * 12
    assert prose.analyze(numeric).density.citation_style == "numeric"
    ay = "As shown (Smith et al., 2020) and later (Jones and Lee, 2021). " * 12
    assert prose.analyze(ay).density.citation_style == "author_year"


def test_too_few_citations_reports_undetected_not_none():
    # Superscript-numeral venues convert to bare digits and the count
    # collapses. "none" would be a false claim about the manuscript.
    text = "We measured the thing carefully and reported it plainly. " * 60
    density = prose.analyze(text + " [1]").density
    assert density.citation_style == "undetected"


def test_p_values_split_exact_from_threshold():
    density = prose.analyze("We found p = 0.032 here and p < 0.05 there.").density
    assert density.p_values_exact == 1
    assert density.p_values_threshold == 1


def test_mattr_is_not_dragged_down_by_length():
    """Plain TTR falls as a document grows; MATTR must not."""
    sentence = "The quick brown fox jumps over a lazy dog near the riverbank today. "
    short = prose.analyze(sentence * 20).density.mattr
    long = prose.analyze(sentence * 200).density.mattr
    assert abs(short - long) < 0.05


def test_long_sentences_are_measured():
    long_sentence = "We " + "measured and recorded and analysed and reported " * 12 + "it."
    density = prose.analyze(long_sentence + " Short one.").density
    assert density.long_sentence_share > 0
    assert density.sentence_len_p90 > 40


# --- caveman ---------------------------------------------------------------


def test_compression_withholds_density_entirely():
    # Caveman strips articles and function words, so "sentence length" and
    # "hedging" would describe the compressor rather than the author.
    text = "We designed two-player game to investigate signatures of updating beliefs. " * 20
    stats = prose.analyze(text, caveman="hard")
    assert stats.density is None
    assert stats.caveman == "hard"
    # Health and raw size still describe the text the panel will actually read.
    assert stats.health.verdict == prose.CLEAN
    assert stats.counts.words > 0


def test_caveman_off_is_not_compression():
    stats = prose.analyze("We did the thing. " * 20, caveman="off")
    assert stats.density is not None
    assert stats.caveman is None


# --- serialisation ---------------------------------------------------------


def test_to_dict_is_json_safe():
    import json

    stats = prose.analyze("We did the thing carefully. " * 20, sections={"methods": "x"})
    payload = json.loads(json.dumps(stats.to_dict()))
    assert payload["health"]["verdict"] == prose.CLEAN
    assert payload["counts"]["words"] > 0
    assert payload["density"]["citation_style"] == "undetected"


def test_analyze_survives_degenerate_input():
    for text in ("", "   ", "\n\n\n", "a", "###", "|||"):
        stats = prose.analyze(text)
        assert stats.counts.words >= 0
        assert stats.health.verdict in (prose.CLEAN, prose.DEGRADED, prose.BROKEN)


# --- report surface --------------------------------------------------------


def _state(text, sections=None, caveman=None, **extra):
    stats = prose.analyze(text, sections=sections, caveman=caveman)
    state = {
        "manuscript_title": "A Paper",
        "config": {"run_id": "test"},
        "ingest": {"format": "markdown", "tool": "rustypaper 9.9.9", "prose": stats.to_dict()},
        "reports": [],
    }
    state.update(extra)
    return state


def test_stats_report_renders_counts():
    from peerreviewagents import reports

    body = "We measured the thing carefully and reported it. " * 30
    refs = "[1] Smith J. A paper. Journal. 2020.\n"
    out = reports._prose_report(
        _state(body + refs, sections={"methods": body, "references": refs})
    )
    assert "# Manuscript Statistics" in out
    assert "Conversion health: **clean**" in out
    assert "Main text (excluding references):" in out
    assert "Sentence length:" in out


def test_stats_report_says_main_text_is_unavailable_rather_than_guessing():
    from peerreviewagents import reports

    body = "We measured the thing carefully. " * 30
    out = reports._prose_report(_state(body, sections={"_preamble": body}))
    assert "Main text: unavailable" in out


def test_stats_report_withholds_prose_under_compression():
    from peerreviewagents import reports

    out = reports._prose_report(_state("We did thing. " * 30, caveman="hard"))
    assert "Not measured" in out
    assert "Sentence length:" not in out


def test_summary_is_silent_when_conversion_is_clean():
    from peerreviewagents import reports

    assert reports._ingest_health_line(_state("We did the thing. " * 40)) == ""


def test_summary_warns_when_conversion_is_broken():
    from peerreviewagents import reports

    fused = "whicharemosteffectivewhenasmallwelldefinedsitecanbeengaged"
    line = reports._ingest_health_line(_state(f"Some words here {fused} " * 12))
    assert "broken" in line
    assert "fused" in line


def test_no_stats_file_when_nothing_was_measured():
    from peerreviewagents import reports

    assert reports._prose_report({"ingest": {}, "config": {}}) == ""
