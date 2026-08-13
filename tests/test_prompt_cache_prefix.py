"""The manuscript must be one cached entry shared by every agent that sends it.

A cache write costs 12.5x what a read costs, so the number of *distinct*
prefixes across a run — not the number of sends — is what a review is billed
for. Measured on a 61,700-token paper before this was fixed: 224,691 tokens of
cache writes, about 3.6 whole manuscripts, because the agents that read the
venue directives put them ahead of the manuscript and so shared no prefix with
the agents that send the manuscript alone.
"""

from __future__ import annotations

from peerreviewagents.agents.utils.agent_utils import (
    _build_messages,
    context_block,
    fit_manuscript,
    manuscript_block,
)

STATE = {
    "manuscript_md": "The manuscript body, which is the expensive part.",
    "config": {},
    "journal_block": "=== TARGET JOURNAL ===\nName: Nature\n=== END TARGET JOURNAL ===",
    "strictness_block": "=== REVIEW STRICTNESS ===\nBalanced\n=== END REVIEW STRICTNESS ===",
}


def cached_texts(msgs):
    """The cache-marked block texts of a built message list, in order."""
    return [
        b["text"]
        for b in msgs[0].content
        if isinstance(b, dict) and b.get("cache_control")
    ]


def test_reviewers_and_debaters_share_the_manuscript_block_exactly():
    """The property the saving rests on.

    A reviewer sends manuscript+directives; a debater sends the manuscript
    alone. Anthropic matches the longest cached prefix, so as long as block 0
    is byte-identical the debater reads the reviewer's entry instead of
    writing a second copy of the manuscript.
    """
    reviewer = cached_texts(_build_messages("sys", "user", context_block(STATE)))
    debater = cached_texts(_build_messages("sys", "user", manuscript_block(STATE)))
    assert debater == [reviewer[0]]


def test_the_manuscript_is_not_preceded_by_anything():
    """Anything ahead of the manuscript fragments it. The directives used to
    sit there, which is what made the same text cache once per agent group."""
    blocks = cached_texts(_build_messages("sys", "user", context_block(STATE)))
    assert blocks[0].startswith("=== MANUSCRIPT ===")


def test_each_block_gets_its_own_breakpoint():
    """Without a breakpoint after the manuscript there is no entry at that
    boundary for the manuscript-only agents to match against."""
    msgs = _build_messages("sys", "user", context_block(STATE))
    marked = [b for b in msgs[0].content if isinstance(b, dict) and b.get("cache_control")]
    assert len(marked) == 2


def test_the_agent_system_prompt_stays_outside_the_cache():
    """It differs per agent; inside a breakpoint it would give every agent its
    own entry and defeat the whole arrangement."""
    msgs = _build_messages("SPECIALIST PROMPT", "user", context_block(STATE))
    tail = msgs[0].content[-1]
    assert tail["text"] == "SPECIALIST PROMPT"
    assert "cache_control" not in tail


def test_a_supplement_appends_without_disturbing_the_shared_blocks():
    """The methods auditor adds the SI. It goes last so the blocks before it
    still match what everyone else sends."""
    base = context_block(STATE)
    with_si = [*base, "=== SUPPLEMENTARY INFORMATION ===\nx\n=== END ==="]
    assert cached_texts(_build_messages("sys", "user", with_si))[: len(base)] == base


def test_providers_without_cache_control_get_the_same_text_in_the_same_order():
    msgs = _build_messages("sys", "user", context_block(STATE), cache_supported=False)
    body = msgs[0].content
    assert body.index("=== MANUSCRIPT ===") < body.index("TARGET JOURNAL") < body.index("sys")


def test_a_plain_string_prefix_still_works():
    """Most call sites pass one block; only context_block passes several."""
    msgs = _build_messages("sys", "user", "just the manuscript")
    assert cached_texts(msgs) == ["just the manuscript"]


def test_empty_blocks_are_dropped_rather_than_cached():
    """An empty marked block is a wasted breakpoint, and Anthropic allows
    only four."""
    assert cached_texts(_build_messages("sys", "user", ["a", "", "   ", "b"])) == ["a", "b"]


# --- entry lifetime ---------------------------------------------------------


def test_entries_outlive_the_run_by_default():
    """A review takes 10-20 minutes; the provider's default entry lives 5.

    On C-01 that meant 479,205 tokens of cache writes against 32,795 of
    reads — roughly fifteen manuscripts written and one read, because the
    entry kept expiring between stages.
    """
    msgs = _build_messages("sys", "user", context_block(STATE))
    marked = [b for b in msgs[0].content if isinstance(b, dict) and b.get("cache_control")]
    assert all(b["cache_control"].get("ttl") == "1h" for b in marked)


def test_the_provider_default_is_sent_without_a_ttl_field():
    """'5m' is the API's own default and is expressed by omitting the key,
    not by naming it — sending an explicit ttl the API doesn't expect is a
    needless way to break on an older endpoint."""
    msgs = _build_messages("sys", "user", context_block(STATE), cache_ttl="5m")
    marked = [b for b in msgs[0].content if isinstance(b, dict) and b.get("cache_control")]
    assert marked and all("ttl" not in b["cache_control"] for b in marked)


def test_each_block_carries_its_own_control_object():
    """Sharing one dict across blocks would let a later mutation reach back
    into earlier ones."""
    msgs = _build_messages("sys", "user", context_block(STATE))
    marked = [b for b in msgs[0].content if isinstance(b, dict) and b.get("cache_control")]
    assert len({id(b["cache_control"]) for b in marked}) == len(marked)


# --- section fitting ----------------------------------------------------------
#
# fit_manuscript feeds the manuscript block above, so what it returns is both
# what every reviewer reads and the text the shared cache entry is keyed on.
# Two properties matter: dropped sections are disclosed (a reviewer must not
# fault the authors for a limitations section that exists in the PDF), and the
# result is deterministic for a given manuscript + budget (or the notice would
# fragment the one cached prefix into one entry per agent).


def _fitted_state() -> dict:
    sections = {
        "abstract": "A" * 100,
        "introduction": "B" * 100,
        "methods": "C" * 5000,      # alone bigger than the budget below
        "results": "D" * 100,
        "discussion": "E" * 100,
        "conclusion": "F" * 100,
        "related work": "G" * 100,  # not a priority section
    }
    return {
        "manuscript_md": "x" * 9000,
        "config": {},
        "sections": sections,
    }


def test_dropped_sections_are_named_in_the_fitted_text():
    fitted = fit_manuscript(_fitted_state(), budget=700)
    assert "[sections omitted to fit the length budget:" in fitted
    assert "methods" in fitted.rsplit("[", 1)[1]
    assert "related work" in fitted.rsplit("[", 1)[1]


def test_an_oversized_section_does_not_take_the_rest_down_with_it():
    """The packer used to `break` on the first section over budget, so one
    long methods section dropped results, discussion and conclusion that
    would each have fit on their own."""
    fitted = fit_manuscript(_fitted_state(), budget=700)
    for kept in ("## Results", "## Discussion", "## Conclusion"):
        assert kept in fitted
    assert "C" * 5000 not in fitted


def test_the_fitted_text_is_byte_identical_across_agents():
    """The omission notice must not fragment the shared cache prefix: same
    manuscript, same budget, same bytes — every time, for every agent."""
    state = _fitted_state()
    assert fit_manuscript(state, budget=700) == fit_manuscript(state, budget=700)
    assert manuscript_block({**state, "config": {"manuscript_char_budget": 700}}) == \
        manuscript_block({**state, "config": {"manuscript_char_budget": 700}})


def test_a_manuscript_that_fits_carries_no_notice():
    state = {**_fitted_state(), "manuscript_md": "short and sweet"}
    assert fit_manuscript(state, budget=700) == "short and sweet"


def test_tail_truncation_fallback_keeps_its_marker():
    """Too little section structure to pack: the old tail cut, still flagged."""
    state = {**_fitted_state(), "sections": {}}
    fitted = fit_manuscript(state, budget=700)
    assert fitted.startswith("x" * 700)
    assert fitted.endswith("[...manuscript truncated...]")
