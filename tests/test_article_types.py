"""Tests for the article-type taxonomy: the shared registry, per-journal
cap overrides, validation, config wiring, and context-block injection."""

from __future__ import annotations

import textwrap

import pytest

from peerreviewagents.agents.utils.agent_utils import context_block, manuscript_block
from peerreviewagents.article_types import (
    ARTICLE_TYPES,
    article_type_block,
    article_type_label,
    normalize_article_type,
)
from peerreviewagents.default_config import get_config
from peerreviewagents.journals import load_journal
from peerreviewagents.strictness import strictness_block


# --- registry & rendering --------------------------------------------------


def test_empty_key_renders_empty():
    # No type selected must emit nothing so a default run is byte-identical
    # to the pre-article-type pipeline.
    assert article_type_block("") == ""
    assert article_type_block("nonexistent") == ""


@pytest.mark.parametrize("key", list(ARTICLE_TYPES))
def test_known_types_render_a_block(key):
    block = article_type_block(key)
    assert block.startswith("=== MANUSCRIPT TYPE ===")
    assert block.strip().endswith("=== END MANUSCRIPT TYPE ===")
    assert article_type_label(key) in block
    # The general framing always renders, even without per-venue caps.
    assert "When reviewing:" in block
    # No caps supplied => no length line.
    assert "Length limits" not in block


def test_overrides_inject_caps_and_notes():
    block = article_type_block(
        "letter", max_words=5500, abstract_max_words=150, notes="Inquiry first."
    )
    assert "main text ≤ 5500 words" in block
    assert "abstract ≤ 150 words" in block
    assert "Inquiry first." in block


# --- validation ------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, ""),
        ("", ""),
        ("review", "review"),
        ("Technical Note", "technical-note"),
        ("technical_note", "technical-note"),
        ("  Letter  ", "letter"),
    ],
)
def test_normalize_accepts_and_canonicalizes(value, expected):
    assert normalize_article_type(value) == expected


@pytest.mark.parametrize("value", ["nope", "research", "essay"])
def test_normalize_rejects_unknown(value):
    with pytest.raises(ValueError):
        normalize_article_type(value)


# --- per-journal overrides --------------------------------------------------


def test_journal_supplies_caps_for_its_types(tmp_path):
    d = tmp_path / "journals"
    d.mkdir()
    (d / "demo.toml").write_text(
        textwrap.dedent(
            """
            name = "Demo Journal"

            [article_types.letter]
            max_words = 5500
            notes = "Presubmission inquiry recommended."
            """
        ),
        encoding="utf-8",
    )
    profile = load_journal("demo", {"journals_dir": str(d)})
    limits = profile.article_type_limits("letter")
    assert limits is not None
    assert limits.max_words == 5500
    assert limits.notes == "Presubmission inquiry recommended."
    # A type the venue doesn't differentiate has no override.
    assert profile.article_type_limits("review") is None


def test_jpr_profile_defines_all_seven_types():
    profile = load_journal("journal-of-proteome-research")
    for key in (
        "article",
        "letter",
        "communication",
        "perspective",
        "review",
        "technical-note",
        "tutorial",
    ):
        assert profile.article_type_limits(key) is not None


# --- config wiring ---------------------------------------------------------


def test_article_type_config_defaults_to_empty():
    assert get_config().get("article_type") == ""


def test_article_type_env_and_kwarg_precedence(monkeypatch):
    monkeypatch.setenv("PEERREVIEW_ARTICLE_TYPE", "review")
    assert get_config()["article_type"] == "review"
    # Explicit kwarg (CLI flag) wins over the env var.
    assert get_config(article_type="letter")["article_type"] == "letter"


# --- context-block injection ----------------------------------------------


def test_context_block_includes_article_type_when_set():
    state = {
        "manuscript_md": "Hello world.",
        "config": {},
        "article_type_block": article_type_block("review"),
    }
    combined = context_block(state)
    assert "=== MANUSCRIPT TYPE ===" in combined
    assert manuscript_block(state) in combined


def test_context_block_orders_journal_type_strictness_manuscript():
    state = {
        "manuscript_md": "Hello world.",
        "config": {},
        "journal_block": "=== TARGET JOURNAL ===\nName: T\n=== END TARGET JOURNAL ===",
        "article_type_block": article_type_block("letter"),
        "strictness_block": strictness_block(4),
    }
    combined = context_block(state)
    assert (
        combined.index("TARGET JOURNAL")
        < combined.index("MANUSCRIPT TYPE")
        < combined.index("REVIEW STRICTNESS")
        < combined.index("=== MANUSCRIPT ===")
    )
