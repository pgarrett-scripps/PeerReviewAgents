"""Tests for the review-strictness dial: rendering, validation, config
wiring, and context-block injection."""

from __future__ import annotations

import pytest

from peerreviewagents.agents.utils.agent_utils import context_block, manuscript_block
from peerreviewagents.default_config import get_config
from peerreviewagents.strictness import (
    DEFAULT_LEVEL,
    MAX_LEVEL,
    MIN_LEVEL,
    normalize_strictness,
    strictness_block,
    strictness_label,
)


# --- rendering -------------------------------------------------------------


def test_balanced_default_renders_empty():
    # Level 3 must emit nothing so a default run is byte-identical to the
    # pre-strictness pipeline.
    assert strictness_block(DEFAULT_LEVEL) == ""


@pytest.mark.parametrize("level", [1, 2, 4, 5])
def test_nondefault_levels_render_a_directive(level):
    block = strictness_block(level)
    assert block.startswith("=== REVIEW STRICTNESS ===")
    assert block.strip().endswith("=== END REVIEW STRICTNESS ===")
    assert f"{level}/{MAX_LEVEL}" in block
    assert strictness_label(level) in block


def test_lenient_and_strict_directives_differ():
    assert strictness_block(1) != strictness_block(5)


# --- validation ------------------------------------------------------------


@pytest.mark.parametrize("value,expected", [(1, 1), (3, 3), (5, 5), ("4", 4)])
def test_normalize_accepts_valid_levels(value, expected):
    assert normalize_strictness(value) == expected


@pytest.mark.parametrize("value", [0, 6, -1, 99, "x", None, "2.5"])
def test_normalize_rejects_out_of_range_or_nonint(value):
    with pytest.raises(ValueError):
        normalize_strictness(value)


def test_level_bounds_are_inclusive():
    assert normalize_strictness(MIN_LEVEL) == MIN_LEVEL
    assert normalize_strictness(MAX_LEVEL) == MAX_LEVEL


# --- config wiring ---------------------------------------------------------


def test_review_strictness_config_defaults_to_three():
    assert get_config().get("review_strictness") == 3


def test_review_strictness_env_and_kwarg_precedence(monkeypatch):
    monkeypatch.setenv("PEERREVIEW_STRICTNESS", "5")
    assert get_config()["review_strictness"] == 5
    # Explicit kwarg (CLI flag) wins over the env var.
    assert get_config(review_strictness=2)["review_strictness"] == 2


# --- context-block injection ----------------------------------------------


def test_context_block_without_strictness_equals_manuscript():
    # No strictness block + no journal => exactly the manuscript block.
    state = {"manuscript_md": "Hello world.", "config": {}}
    assert context_block(state) == manuscript_block(state)


def test_context_block_includes_strictness_when_set():
    state = {
        "manuscript_md": "Hello world.",
        "config": {},
        "strictness_block": strictness_block(5),
    }
    combined = context_block(state)
    assert "=== REVIEW STRICTNESS ===" in combined
    assert manuscript_block(state) in combined


def test_context_block_orders_journal_before_strictness():
    state = {
        "manuscript_md": "Hello world.",
        "config": {},
        "journal_block": "=== TARGET JOURNAL ===\nName: Test\n=== END TARGET JOURNAL ===",
        "strictness_block": strictness_block(4),
    }
    combined = context_block(state)
    # Journal context leads, strictness follows, manuscript last.
    assert combined.index("TARGET JOURNAL") < combined.index("REVIEW STRICTNESS")
    assert combined.index("REVIEW STRICTNESS") < combined.index("=== MANUSCRIPT ===")
