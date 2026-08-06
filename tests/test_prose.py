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


# --- the gate: a broken conversion never reaches an agent ------------------


def _ingest(text, sections=None, caveman=None):
    """An ingest record shaped exactly as the loader stores one."""
    return {
        "format": "markdown",
        "tool": "rustypaper 9.9.9",
        "prose": prose.analyze(text, sections=sections, caveman=caveman).to_dict(),
    }


_FUSED = "whicharemosteffectivewhenasmallwelldefinedsitecanbeengaged"
_CLEAN = "We measured the thing carefully and reported it. " * 40
_BROKEN = f"Some words here {_FUSED} " * 12
# Enough fused tokens to pass FUSED_DEGRADED but not FUSED_BROKEN.
_DEGRADED = _CLEAN + f" {_FUSED} " * 3


def test_a_clean_manuscript_passes_the_gate():
    from peerreviewagents.ingest import loader

    loader.require_readable(_ingest(_CLEAN), {})  # does not raise


def test_a_broken_conversion_stops_the_run():
    import pytest

    from peerreviewagents.ingest import loader

    with pytest.raises(loader.ManuscriptUnreadable) as exc:
        loader.require_readable(_ingest(_BROKEN), {})
    # The message has to tell a submitter this is about the file, not the work.
    assert "conversion failure, not an assessment" in str(exc.value)
    assert exc.value.verdict == prose.BROKEN


def test_a_stopped_run_is_not_a_rejection():
    """The exception carries no verdict, decision or letter — by design.

    A desk rejection is a judgment about a manuscript. This is a statement
    about a file, and the two must not arrive looking the same.
    """
    from peerreviewagents.ingest import loader

    assert not hasattr(loader.ManuscriptUnreadable, "decision")
    assert issubclass(loader.ManuscriptUnreadable, RuntimeError)


def test_degraded_passes_the_default_gate():
    from peerreviewagents.ingest import loader

    record = _ingest(_DEGRADED)
    assert prose.verdict_of(record) == prose.DEGRADED
    loader.require_readable(record, {})  # does not raise


def test_the_gate_can_be_tightened_to_degraded():
    import pytest

    from peerreviewagents.ingest import loader

    with pytest.raises(loader.ManuscriptUnreadable):
        loader.require_readable(_ingest(_DEGRADED), {"conversion_gate": "degraded"})


def test_the_gate_can_be_turned_off():
    from peerreviewagents.ingest import loader

    loader.require_readable(_ingest(_BROKEN), {"conversion_gate": "off"})


def test_an_unmeasured_manuscript_passes():
    """A caller that bypassed the parser is not a failed conversion."""
    from peerreviewagents.ingest import loader

    assert prose.verdict_of({}) == prose.CLEAN
    loader.require_readable({}, {})
    loader.require_readable(None, {})


def test_a_nonsense_gate_value_falls_back_to_the_default():
    from peerreviewagents.ingest import loader

    assert loader.conversion_gate({"conversion_gate": "yes please"}) == "broken"
    assert loader.conversion_gate(None) == "broken"


# --- the advisory: damage short of the gate is named to the panel ----------


def test_a_clean_manuscript_carries_no_advisory():
    from peerreviewagents.agents.utils import agent_utils

    assert agent_utils._conversion_notice(_state(_CLEAN)) == ""


def test_a_degraded_manuscript_warns_the_reviewers():
    from peerreviewagents.agents.utils import agent_utils

    notice = agent_utils._conversion_notice(_state(_DEGRADED))
    assert "converter's, not the authors'" in notice
    assert "run together" in notice


def test_the_advisory_only_names_damage_that_was_found():
    """No blanket list. A paper with fused words is not told about hyphens."""
    from peerreviewagents.agents.utils import agent_utils

    notice = agent_utils._conversion_notice(_state(_DEGRADED))
    assert "hyphens survive" not in notice


def test_the_advisory_rides_inside_the_manuscript_block():
    """Not a second cached block: the panel shares one prefix, and a separate
    block would make every reviewer write the manuscript again."""
    from peerreviewagents.agents.utils import agent_utils

    state = _state(_DEGRADED, manuscript_md=_DEGRADED, sections={})
    block = agent_utils.manuscript_block(state)
    assert block.startswith("=== MANUSCRIPT ===")
    assert "converter's, not the authors'" in block
    assert block.count("=== MANUSCRIPT ===") == 1


def test_a_section_only_degradation_says_nothing_to_reviewers():
    """`preamble_share` degrades the verdict but is not about the words, so
    there is nothing to warn a reviewer about."""
    from peerreviewagents.agents.utils import agent_utils

    state = _state(_CLEAN, sections={"_preamble": _CLEAN, "methods": "x"})
    assert state["ingest"]["prose"]["health"]["verdict"] == prose.DEGRADED
    assert agent_utils._conversion_notice(state) == ""


def test_the_shared_manuscript_block_carries_no_statistics():
    """Density reaches exactly one prompt, the clarity reviewer's, and that
    path has its own tests further down. What must never happen is a statistic
    entering the block every agent shares: it would put sentence-length figures
    in front of the methodologist, the auditors and the editor, none of whom
    asked for them, and it would do it inside the cached prefix.
    """
    import inspect

    from peerreviewagents.agents.utils import agent_utils

    src = inspect.getsource(agent_utils)
    for term in ("sentence_len", "hedges_per_1k", "boosters_per_1k", "mattr"):
        assert term not in src


# --- where the gate sits, and why ------------------------------------------


def test_the_desk_node_is_wired_in_for_the_gate_alone():
    """With triage off, the gate still has somewhere to run."""
    from peerreviewagents.agents.editor import desk_screen

    bare = {"desk_screen_mode": "off"}
    assert desk_screen.node_enabled(bare) is True
    assert desk_screen.node_enabled({**bare, "conversion_gate": "off"}) is False


# --- the one statistic that reaches a prompt --------------------------------


def _clarity_note(text, sections=None, caveman=None):
    from peerreviewagents.agents.reviewers import clarity

    return clarity._stats_note({"ingest": _ingest(text, sections, caveman)})


def test_clarity_receives_the_numbers_it_can_act_on():
    note = _clarity_note(_CLEAN)
    assert "median sentence length" in note
    assert "passive constructions" in note


def test_clarity_is_not_given_the_numbers_that_are_not_its_remit():
    """Hedges and boosters are overclaiming, which is rigor's and novelty's
    verdict. MATTR has no reference distribution, so a reviewer handed 0.47
    would invent a comparison for it."""
    note = _clarity_note(_CLEAN)
    for term in ("hedg", "booster", "MATTR", "mattr", "p-value", "citation"):
        assert term not in note, f"{term} should not reach the clarity prompt"


def test_the_numbers_arrive_labelled_unreliable():
    note = _clarity_note(_CLEAN)
    assert "rough" in note.lower()
    assert "conversion is imperfect" in note


def test_the_note_forbids_reporting_a_statistic_as_a_finding():
    """The framing is the guardrail. Without it an agent handed a number
    writes a finding about the number."""
    note = _clarity_note(_CLEAN)
    assert "do not report a statistic as a finding" in note
    assert "the page is right" in note
    assert "is not a defect" in note


def test_a_damaged_conversion_warns_harder():
    clean, damaged = _clarity_note(_CLEAN), _clarity_note(_DEGRADED)
    assert "worse than usual" in damaged
    assert "worse than usual" not in clean


def test_compression_withholds_the_note_entirely():
    """The compressor strips the function words sentence length and passive
    voice are measured on, so the numbers would describe it, not the author."""
    assert _clarity_note(_CLEAN, caveman="hard") == ""


def test_an_unmeasured_manuscript_gets_no_note():
    from peerreviewagents.agents.reviewers import clarity

    assert clarity._stats_note({"ingest": {}}) == ""
    assert clarity._stats_note({}) == ""


def test_only_clarity_gets_a_stats_note():
    """Scoped to one reviewer. If this fails, someone wired the numbers into a
    reviewer whose remit they do not describe."""
    import inspect
    import pathlib

    from peerreviewagents.agents import reviewers

    d = pathlib.Path(inspect.getfile(reviewers)).parent
    wired = sorted(
        f.stem for f in d.glob("*.py")
        if f.stem not in ("base", "__init__") and "mandate_extra" in f.read_text()
    )
    assert wired == ["clarity"], f"stats reached {wired}"


def test_the_note_rides_in_the_mandate_not_the_cached_prefix():
    """The cached prefix is the manuscript block all eight reviewers share.
    Varying it per reviewer would split one cache entry into eight."""
    import inspect

    from peerreviewagents.agents.reviewers import base

    src = inspect.getsource(base.make_reviewer_node)
    assert "mandate=mandate + extra" in src
    assert "cached_prefix = context_block(state)" in src


# --- the text fingerprint ---------------------------------------------------


def test_the_ingest_record_fingerprints_the_text_not_the_file():
    """Measured, and the reason the field exists: three downloads of one
    bioRxiv PDF over ten hours gave three different file checksums at an
    identical 1,689,095 bytes, while the converted text came back
    byte-identical every time. A caller asking "same draft?" off the file hash
    gets "no" for every bioRxiv paper.
    """
    import hashlib
    import tempfile
    from pathlib import Path

    from peerreviewagents.ingest.loader import load_manuscript_record

    body = "# A Paper\n\n" + "We measured the thing and reported it. " * 40
    with tempfile.TemporaryDirectory() as tmp:
        a, b = Path(tmp) / "a.md", Path(tmp) / "b.md"
        a.write_text(body)
        b.write_text(body)
        ra = load_manuscript_record(str(a), {})
        rb = load_manuscript_record(str(b), {})

    assert ra.ingest["text_sha256"] == rb.ingest["text_sha256"], \
        "same text in two files must fingerprint the same"
    assert ra.ingest["text_sha256"] == hashlib.sha256(
        ra.text.encode("utf-8")
    ).hexdigest(), "the fingerprint must be of the text that was returned"


def test_different_text_fingerprints_differently():
    import tempfile
    from pathlib import Path

    from peerreviewagents.ingest.loader import load_manuscript_record

    with tempfile.TemporaryDirectory() as tmp:
        a, b = Path(tmp) / "a.md", Path(tmp) / "b.md"
        a.write_text("# A\n\n" + "One sentence here. " * 40)
        b.write_text("# A\n\n" + "One sentence here. " * 40 + "And one more.")
        ra = load_manuscript_record(str(a), {})
        rb = load_manuscript_record(str(b), {})
    assert ra.ingest["text_sha256"] != rb.ingest["text_sha256"]


# --- an empty response is a failed call, not a review -----------------------


def test_an_empty_tool_loop_falls_back_instead_of_extracting_nothing():
    """The failure that published a fabricated 1/5.

    invoke_structured_after_tools converts free text to JSON in a second call.
    Given empty free text, the extraction prompt ("convert the assistant text
    below") is answered on its own terms — nvidia/nemotron-3-ultra returned a
    review whose summary read "The user requested conversion of assistant text
    to JSON... neither the source text nor the schema were included", scored
    the paper 1/5, and passed schema validation. No exception, no error, panel
    reported 8 of 8 scored.

    A blank response must take the same route as a raised one.
    """
    from peerreviewagents.agents.utils import structured
    from peerreviewagents.agents.utils.agent_utils import RunResult

    calls = {"run_agent": 0, "fallback": 0, "extract": 0}

    def fake_run_agent(*a, **k):
        calls["run_agent"] += 1
        return RunResult(text="   \n  ", cost=0.0)   # reasoning-only response

    def fake_invoke_structured(*a, **k):
        calls["fallback"] += 1
        return structured.StructuredResult(instance=object(), cost=0.0)

    def fake_try(*a, **k):
        calls["extract"] += 1
        raise AssertionError("must not extract from an empty response")

    real = (structured.run_agent, structured.invoke_structured, structured._try_structured)
    structured.run_agent = fake_run_agent
    structured.invoke_structured = fake_invoke_structured
    structured._try_structured = fake_try
    try:
        structured.invoke_structured_after_tools(
            llm=None, schema=object, config={}, system_prompt="s",
            user_prompt="u", tools=[],
        )
    finally:
        (structured.run_agent, structured.invoke_structured,
         structured._try_structured) = real

    assert calls["fallback"] == 1, "an empty response must fall back"
    assert calls["extract"] == 0, "nothing may be extracted from nothing"


def test_a_real_response_still_takes_the_extraction_path():
    from peerreviewagents.agents.utils import structured
    from peerreviewagents.agents.utils.agent_utils import RunResult

    seen = {}

    def fake_run_agent(*a, **k):
        # Long enough to be a review. The fixture used to be one sentence,
        # which the length floor now (correctly) treats as a truncated agent.
        body = (
            "The manuscript claims X, and Fig. 2 shows Y. " * 12
        )
        return RunResult(text=body, cost=0.5)

    def fake_try(llm, schema, messages, config=None):
        seen["text"] = messages[-1].content
        return structured.StructuredResult(instance=object(), cost=0.25)

    def fake_invoke_structured(*a, **k):
        raise AssertionError("must not fall back on a good response")

    real = (structured.run_agent, structured.invoke_structured, structured._try_structured)
    structured.run_agent = fake_run_agent
    structured.invoke_structured = fake_invoke_structured
    structured._try_structured = fake_try
    try:
        out = structured.invoke_structured_after_tools(
            llm=None, schema=object, config={}, system_prompt="s",
            user_prompt="u", tools=[],
        )
    finally:
        (structured.run_agent, structured.invoke_structured,
         structured._try_structured) = real

    assert "Fig. 2" in seen["text"], "the review text must reach the extractor"
    assert out.cost == 0.75, "both calls must be billed"


def test_an_interrupted_agent_is_not_mistaken_for_a_review():
    """A model that says it is not finished must not be published as finished.

    Asked for a final answer after its tool budget ran out, DeepSeek replied
    "Let me verify a few more key citations before finalizing my audit." That
    is 68 characters and non-empty, so the emptiness check passed it through;
    the extraction step reported it had nothing to work with, and the bundle
    published HARD gaps (blocking): 0 for a manuscript nothing had audited.

    Short is not empty, but it is not a review either, and it takes the same
    tools-free fallback a blank response does.
    """
    from peerreviewagents.agents.utils import structured
    from peerreviewagents.agents.utils.agent_utils import RunResult

    reasons = []

    def fake_run_agent(*a, **k):
        return RunResult(
            text="Let me verify a few more key citations before finalizing my audit.",
            cost=0.5,
        )

    def fake_invoke_structured(*a, **k):
        return structured.StructuredResult(instance=object(), cost=0.25)

    def fake_try(*a, **k):
        raise AssertionError("a truncated agent must not reach the extractor")

    real = (structured.run_agent, structured.invoke_structured,
            structured._try_structured, structured.emit)
    structured.run_agent = fake_run_agent
    structured.invoke_structured = fake_invoke_structured
    structured._try_structured = fake_try
    structured.emit = lambda ev, *a, **k: reasons.append(
        getattr(ev, "text", "") or getattr(ev, "tool_error", "")
    )
    try:
        structured.invoke_structured_after_tools(
            llm=None, schema=object, config={}, system_prompt="s",
            user_prompt="u", tools=[],
        )
    finally:
        (structured.run_agent, structured.invoke_structured,
         structured._try_structured, structured.emit) = real

    assert any("66 characters" in r or "characters" in r for r in reasons), \
        f"the fallback must say what it saw, got {reasons}"
