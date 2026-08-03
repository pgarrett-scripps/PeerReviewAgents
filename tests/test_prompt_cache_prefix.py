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
