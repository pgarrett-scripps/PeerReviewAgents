"""Per-agent usage accounting.

The run total says what a review cost. This says which agent to look at —
and only the second tells you what to change. C-09 had to be decomposed by
hand from prompt sizes because nothing recorded it.
"""

from __future__ import annotations

import pytest

from peerreviewagents.observability import (
    AgentEvent,
    emit,
    node_usage,
    reset_cache_totals,
)
from peerreviewagents.reports import _usage_table

RUN = "test-run-usage"


@pytest.fixture(autouse=True)
def clean():
    reset_cache_totals(RUN)
    yield
    reset_cache_totals(RUN)


def usage(node, **kw):
    emit(AgentEvent(kind="usage", node=node, run_id=RUN, **kw))


def test_usage_is_attributed_to_the_agent_that_spent_it():
    usage("reviewer_rigor", input_tokens=50_000, output_tokens=900, cost_usd=0.12)
    usage("editor", input_tokens=12_000, output_tokens=2_000, cost_usd=0.31)
    rows = node_usage(RUN)
    assert rows["reviewer_rigor"][0] == 50_000
    assert rows["editor"][4] == pytest.approx(0.31)


def test_repeated_calls_by_one_agent_accumulate():
    """Agents retry, and the tool loop invokes more than once. A row that
    showed only the last call would understate exactly the agents worth
    looking at."""
    usage("reviewer_novelty", input_tokens=1_000, output_tokens=10, cost_usd=0.01)
    usage("reviewer_novelty", input_tokens=2_000, output_tokens=20, cost_usd=0.02)
    assert node_usage(RUN)["reviewer_novelty"][0] == 3_000
    assert node_usage(RUN)["reviewer_novelty"][4] == pytest.approx(0.03)


def test_a_fully_uncached_agent_is_visible_as_such():
    """The signal the table exists for: an agent sending the manuscript with
    no cache reads is writing its own entry instead of sharing the common
    one."""
    usage("debate_advocate", input_tokens=60_000, cache_write_tokens=60_000, cost_usd=0.22)
    table = _usage_table({"config": {"run_id": RUN}})
    row = next(ln for ln in table.splitlines() if "debate_advocate" in ln)
    assert "| 0% |" in row


def test_a_cache_hit_shows_a_high_share():
    usage("reviewer_clarity", input_tokens=60_000, cache_read_tokens=59_000, cost_usd=0.02)
    table = _usage_table({"config": {"run_id": RUN}})
    row = next(ln for ln in table.splitlines() if "reviewer_clarity" in ln)
    assert "| 98% |" in row


def test_rows_are_ordered_by_spend():
    """The expensive agent is the one you want at the top."""
    usage("cheap", input_tokens=100, cost_usd=0.01)
    usage("expensive", input_tokens=100, cost_usd=0.90)
    body = _usage_table({"config": {"run_id": RUN}})
    assert body.index("| expensive |") < body.index("| cheap |")


def test_no_usage_writes_no_table():
    assert _usage_table({"config": {"run_id": "never-ran"}}) == ""


def test_totals_sum_the_rows():
    usage("a", input_tokens=10, output_tokens=1, cache_read_tokens=5, cost_usd=0.10)
    usage("b", input_tokens=30, output_tokens=3, cache_read_tokens=15, cost_usd=0.20)
    total = next(
        ln for ln in _usage_table({"config": {"run_id": RUN}}).splitlines()
        if "**total**" in ln
    )
    assert "| 40 |" in total and "| 4 |" in total and "0.3000" in total
    assert "| 50% |" in total


# --- attribution ------------------------------------------------------------


def test_the_callback_falls_back_to_the_agent_it_was_built_for():
    """The bug that made the first usage.md useless.

    `current_node()` is a thread-local set by the node. LangChain does not
    guarantee a callback runs on the node's thread, and when it doesn't the
    node reads empty — so every event in the run collapsed into one
    "(unattributed)" row and the table answered nothing. The factory knows
    which agent it is building for, so the name is captured there.
    """
    from peerreviewagents.observability import StreamingCallback

    cb = StreamingCallback(default_model="claude-sonnet-5", default_node="reviewer_rigor")
    assert cb._node() == "reviewer_rigor"


def test_an_explicit_node_context_still_wins():
    """The thread-local is the more specific answer when it is set — a shared
    model reused across agents must not report the name it was built with."""
    from peerreviewagents.observability import StreamingCallback, node_context

    cb = StreamingCallback(default_model="m", default_node="reviewer_rigor")
    with node_context("editor", run_id=RUN):
        assert cb._node() == "editor"
    assert cb._node() == "reviewer_rigor"


def test_the_factory_passes_the_agent_name_to_the_callback():
    """End to end: build a model the way an agent does and check the callback
    it carries knows which agent it belongs to."""
    from peerreviewagents.runtime.providers import make_chat_model

    llm = make_chat_model(
        {"provider": "anthropic", "reasoning_model": "claude-haiku-4-5"},
        agent="reviewer_methodology",
        default_tag="reviewer",
    )
    cbs = [c for c in (llm.callbacks or []) if hasattr(c, "_default_node")]
    assert cbs and cbs[0]._default_node == "reviewer_methodology"
