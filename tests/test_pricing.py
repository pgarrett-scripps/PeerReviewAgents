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
