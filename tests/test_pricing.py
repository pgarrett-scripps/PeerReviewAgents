"""Cost estimation across the spellings a model id arrives in.

The same model reaches `estimate_cost` differently depending on the provider
(`anthropic/claude-opus-5` via OpenRouter, `claude-opus-5` direct,
`claude-haiku-4-5-20251001` as a dated snapshot). These have to agree — the
number ends up in published provenance.
"""

from __future__ import annotations

import pytest

from peerreviewagents.observability import (
    _PRICING_USD_PER_M,
    _normalize_model_key,
    estimate_cost,
)

MTOK = 1_000_000


def rate(model: str) -> tuple[float, float]:
    return estimate_cost(model, MTOK, 0), estimate_cost(model, 0, MTOK)


@pytest.mark.parametrize(
    "openrouter_slug,direct_id",
    [
        ("anthropic/claude-opus-5", "claude-opus-5"),
        ("anthropic/claude-sonnet-5", "claude-sonnet-5"),
        ("anthropic/claude-opus-4.8", "claude-opus-4-8"),
        ("anthropic/claude-opus-4.7", "claude-opus-4-7"),
        ("anthropic/claude-sonnet-4.6", "claude-sonnet-4-6"),
        ("anthropic/claude-haiku-4.5", "claude-haiku-4-5"),
    ],
)
def test_spellings_agree(openrouter_slug, direct_id):
    assert rate(openrouter_slug) == rate(direct_id)


def test_dated_snapshot_matches_alias():
    assert rate("claude-haiku-4-5-20251001") == rate("claude-haiku-4-5")


@pytest.mark.parametrize(
    "model,expected",
    [
        ("claude-opus-5", (5.0, 25.0)),
        ("claude-sonnet-5", (3.0, 15.0)),
        ("claude-fable-5", (10.0, 50.0)),
        ("claude-haiku-4-5", (1.0, 5.0)),
        # Retired-era Opus really is the expensive tier — not a stale default.
        ("claude-3-opus", (15.0, 75.0)),
    ],
)
def test_known_rates(model, expected):
    assert rate(model) == expected


def test_current_opus_is_not_priced_at_the_legacy_tier():
    """The regression: direct ids missed the table and got the legacy rate."""
    assert estimate_cost("claude-opus-5", MTOK, 0) == 5.0
    assert estimate_cost("claude-opus-5", MTOK, 0) != 15.0


def test_o3_reflects_the_june_2025_reprice():
    """OpenAI cut o3 from (10, 40) to (2, 8); quoting the launch price
    overstates that route's cost fivefold."""
    assert rate("o3") == (2.0, 8.0)


def test_unknown_model_falls_back_to_current_generation():
    # Something unreleased should guess current pricing, not the 2024 tier.
    assert rate("claude-opus-99") == (5.0, 25.0)


def test_unrecognized_model_is_zero_not_fabricated():
    assert estimate_cost("some-local-llama", MTOK, MTOK) == 0.0
    assert estimate_cost(None, MTOK, MTOK) == 0.0


def test_table_keys_are_already_normalized():
    """A key that doesn't survive normalization is unreachable."""
    for key in _PRICING_USD_PER_M:
        assert _normalize_model_key(key) == key, f"{key} is unreachable"


# --- prompt cache -----------------------------------------------------------
#
# The manuscript is threaded to every agent as a shared cached prefix, so
# whether it is being hit is the single biggest lever on what a review costs.
# Before these, the cost figure could not express the difference.


def test_a_cache_hit_is_cheaper_than_sending_the_tokens_plain():
    plain = estimate_cost("claude-opus-5", MTOK, 0)
    hit = estimate_cost("claude-opus-5", MTOK, 0, cache_read_tokens=MTOK)
    assert hit == pytest.approx(plain * 0.10)


def test_the_read_discount_is_per_provider_not_anthropics_everywhere():
    """Anthropic's 0.10 was being applied to OpenAI cache reads, which OpenAI
    only discounts to 0.25-0.5x — under-billing every cached OpenAI call by
    up to 5x. OpenAI bills at the 0.5 floor here: over-quoting is the
    survivable error."""
    plain = estimate_cost("gpt-4o", MTOK, 0)
    hit = estimate_cost("gpt-4o", MTOK, 0, cache_read_tokens=MTOK)
    assert hit == pytest.approx(plain * 0.50)


def test_providers_without_a_trusted_read_rate_get_no_discount():
    """Gemini's cache billing includes a storage fee this table cannot
    express, so its reads bill plain rather than at an invented discount."""
    plain = estimate_cost("gemini-2-5-pro", MTOK, 0)
    hit = estimate_cost("gemini-2-5-pro", MTOK, 0, cache_read_tokens=MTOK)
    assert hit == pytest.approx(plain)


def test_a_five_minute_cache_write_costs_a_quarter_more():
    plain = estimate_cost("claude-opus-5", MTOK, 0)
    write = estimate_cost("claude-opus-5", MTOK, 0, cache_write_tokens=MTOK, cache_ttl="5m")
    assert write == pytest.approx(plain * 1.25)


def test_an_hour_long_cache_write_costs_double():
    """The TTL the pipeline actually configures, and the bug this pins.

    DEFAULT_CACHE_TTL is "1h", which Anthropic bills at 2x base against the
    5m write's 1.25x. This priced every write at 1.25 regardless, and cache
    writes are the largest component of input cost under a parallel fan-out
    where ten agents all write and none can read what the others have not
    finished writing. Every cost this pipeline reported was low, on the one
    number a user consults to decide whether to run it again.
    """
    plain = estimate_cost("claude-opus-5", MTOK, 0)
    write = estimate_cost("claude-opus-5", MTOK, 0, cache_write_tokens=MTOK, cache_ttl="1h")
    assert write == pytest.approx(plain * 2.0)


def test_an_unspecified_ttl_bills_at_the_rate_the_pipeline_uses():
    """Fail expensive, not cheap. An omitted TTL is priced at 1h because that
    is what the pipeline configures, and over-quoting is the survivable error.
    """
    from peerreviewagents.agents.utils.agent_utils import DEFAULT_CACHE_TTL

    assert DEFAULT_CACHE_TTL == "1h"
    silent = estimate_cost("claude-opus-5", MTOK, 0, cache_write_tokens=MTOK)
    explicit = estimate_cost(
        "claude-opus-5", MTOK, 0, cache_write_tokens=MTOK, cache_ttl=DEFAULT_CACHE_TTL
    )
    assert silent == pytest.approx(explicit)


def test_cached_tokens_are_a_component_of_input_not_an_addition():
    """The bug this signature exists to prevent.

    LangChain folds cache reads into ``input_tokens`` for Anthropic. If
    ``estimate_cost`` added the cache argument on top instead of carving it
    out, every cached call would be billed for its manuscript twice.
    """
    half = MTOK // 2
    both = estimate_cost("claude-opus-5", MTOK, 0, cache_read_tokens=half)
    # 500k plain + 500k at a tenth = 550k billable, not 1.5M.
    assert both == pytest.approx(estimate_cost("claude-opus-5", 550_000, 0))


def test_the_cache_actually_changes_the_reported_number():
    """The regression that hid the question for months.

    Pricing every input token at the full rate made a review with a working
    prompt cache cost exactly what one with no cache cost, so 'nothing is
    getting cached' and 'everything is getting cached' produced the same
    figure and neither could be told from the other.
    """
    uncached = estimate_cost("claude-opus-5", MTOK, 0)
    cached = estimate_cost("claude-opus-5", MTOK, 0, cache_read_tokens=MTOK)
    assert cached != uncached


def test_cache_counts_larger_than_the_input_total_do_not_credit_the_run():
    """A provider reporting cached tokens outside its input total must not
    drive the estimate negative."""
    cost = estimate_cost("claude-opus-5", 1000, 0, cache_read_tokens=MTOK)
    assert cost >= 0.0


# --- reading the counts off a real response shape ---------------------------


def _msg(usage_metadata=None, response_metadata=None):
    from langchain_core.messages import AIMessage

    m = AIMessage(content="x")
    if usage_metadata is not None:
        m.usage_metadata = usage_metadata
    m.response_metadata = response_metadata or {}
    return m


def test_cache_tokens_read_langchains_normalized_details():
    from peerreviewagents.agents.utils.agent_utils import cache_tokens

    msg = _msg(usage_metadata={
        "input_tokens": 50_000,
        "output_tokens": 500,
        "total_tokens": 50_500,
        "input_token_details": {"cache_read": 48_000, "cache_creation": 0},
    })
    assert cache_tokens(msg) == (48_000, 0)


def test_cache_tokens_fall_back_to_anthropics_raw_field_names():
    """A response that skipped LangChain's usage adapter carries the raw
    spelling; without the fallback it reads as an uncached call."""
    from peerreviewagents.agents.utils.agent_utils import cache_tokens

    msg = _msg(response_metadata={"usage": {
        "cache_read_input_tokens": 48_000,
        "cache_creation_input_tokens": 1_200,
    }})
    assert cache_tokens(msg) == (48_000, 1_200)


def test_openrouters_reported_cost_beats_the_pricing_table():
    """The metadata shape the streaming subclass in runtime.providers writes:
    OpenRouter's raw usage dict — nonstandard `cost` key included — kept under
    response_metadata["token_usage"], where the non-streaming path already
    puts it. DeepSeek has no pricing-table row, so before the subclass a live
    DeepSeek run priced every call at 0.0; the reported number is what the
    account was actually billed and must win over any estimate."""
    from peerreviewagents.agents.utils.agent_utils import _call_cost

    msg = _msg(
        usage_metadata={
            "input_tokens": 51_000, "output_tokens": 800, "total_tokens": 51_800,
        },
        response_metadata={
            "model_name": "deepseek/deepseek-chat",
            "token_usage": {
                "prompt_tokens": 51_000,
                "completion_tokens": 800,
                "total_tokens": 51_800,
                "cost": 0.0123,
            },
        },
    )
    assert _call_cost(msg) == 0.0123


def test_reported_cost_wins_even_when_the_table_could_estimate():
    """A priced model routed through OpenRouter still uses the reported spend:
    the table is a guess about list prices, the response is the bill."""
    from peerreviewagents.agents.utils.agent_utils import _call_cost

    msg = _msg(
        usage_metadata={"input_tokens": 1_000_000, "output_tokens": 0},
        response_metadata={
            "model_name": "anthropic/claude-opus-5",
            "token_usage": {"prompt_tokens": 1_000_000, "cost": 4.2},
        },
    )
    assert _call_cost(msg) == 4.2
    assert _call_cost(msg) != estimate_cost("anthropic/claude-opus-5", 1_000_000, 0)


def test_the_streaming_callback_prefers_the_reported_cost_too():
    """Same preference on the observability path that fills the usage table."""
    from types import SimpleNamespace

    from peerreviewagents.observability import _extract_usage

    msg = SimpleNamespace(
        usage_metadata={"input_tokens": 51_000, "output_tokens": 800},
        response_metadata={
            "model_name": "deepseek/deepseek-chat",
            "token_usage": {"prompt_tokens": 51_000, "cost": 0.0123},
        },
    )
    result = SimpleNamespace(generations=[[SimpleNamespace(message=msg)]])
    in_tok, out_tok, cost, model, _read, _write = _extract_usage(result)
    assert (in_tok, out_tok) == (51_000, 800)
    assert cost == 0.0123


def test_a_cached_reviewer_call_is_priced_below_an_uncached_one():
    """End to end through the call-cost path the agents actually use."""
    from peerreviewagents.agents.utils.agent_utils import _call_cost

    common = {"input_tokens": 50_000, "output_tokens": 500, "total_tokens": 50_500}
    meta = {"model_name": "claude-opus-5"}

    cold = _call_cost(_msg(usage_metadata=dict(common), response_metadata=meta))
    warm = _call_cost(_msg(
        usage_metadata=dict(common, input_token_details={"cache_read": 48_000}),
        response_metadata=meta,
    ))
    assert warm < cold
