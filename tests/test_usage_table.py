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


# --- attribution when the callback runs off the node's thread ---------------
#
# LangChain does not guarantee that on_llm_end runs on the thread that made
# the call. The node *name* was already protected against that by capturing
# it at model-construction time; the run id was not, so an off-thread callback
# produced a correctly named event filed under the un-keyed mailbox. Since
# `_usage_table` asks for one run's rows, those agents were missing from the
# report entirely — and the TUI, which registers without a run id, kept
# showing them, so the live view and the written report disagreed.

from concurrent.futures import ThreadPoolExecutor  # noqa: E402
from types import SimpleNamespace  # noqa: E402

from peerreviewagents.observability import StreamingCallback  # noqa: E402


def _llm_result(input_tokens=1000, output_tokens=100, cache_read=500):
    msg = SimpleNamespace(
        usage_metadata={
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "input_token_details": {"cache_read": cache_read},
        },
        response_metadata={"model_name": "anthropic/claude-opus-5"},
    )
    return SimpleNamespace(generations=[[SimpleNamespace(message=msg)]])


def _callback(node):
    """Built the way the provider factory builds it."""
    return StreamingCallback(
        default_model="anthropic/claude-opus-5", default_node=node, default_run=RUN
    )


def test_usage_lands_in_the_run_when_the_callback_runs_off_thread():
    cb = _callback("reviewer_rigor")
    with ThreadPoolExecutor(max_workers=1) as ex:
        ex.submit(cb.on_llm_end, _llm_result()).result()
    rows = node_usage(RUN)
    assert "reviewer_rigor" in rows, "agent vanished from its own run's usage table"
    assert rows["reviewer_rigor"][0] == 1000


def test_every_parallel_agent_appears_not_just_a_few():
    """The reported symptom: only some agents showed a cost."""
    names = [f"reviewer_{n}" for n in
             ("methodology", "data_analysis", "novelty", "clarity",
              "literature", "rigor", "reproducibility", "ethics")]

    def run_agent(name):
        cb = _callback(name)
        # Node context on this thread, callback dispatched off it.
        with ThreadPoolExecutor(max_workers=1) as ex:
            ex.submit(cb.on_llm_end, _llm_result()).result()

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(run_agent, names))

    rows = node_usage(RUN)
    assert set(rows) == set(names), f"missing: {set(names) - set(rows)}"


def test_cache_totals_survive_an_off_thread_callback():
    """The summary's prompt-cache line reads from the same bucket."""
    from peerreviewagents.observability import cache_totals

    cb = _callback("editor")
    with ThreadPoolExecutor(max_workers=1) as ex:
        ex.submit(cb.on_llm_end, _llm_result(cache_read=900)).result()
    assert cache_totals(RUN)[0] == 900


def test_a_callback_with_no_captured_run_still_records_somewhere():
    """Belt and braces: unattributed spend is not silently dropped."""
    from peerreviewagents.observability import _DEFAULT_RUN

    cb = StreamingCallback(default_model="anthropic/claude-opus-5", default_node="stray")
    with ThreadPoolExecutor(max_workers=1) as ex:
        ex.submit(cb.on_llm_end, _llm_result()).result()
    assert "stray" in node_usage(_DEFAULT_RUN)
