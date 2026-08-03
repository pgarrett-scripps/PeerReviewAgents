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


@pytest.mark.parametrize("key", ["grant-proposal", "exploratory-grant"])
def test_grant_types_reframe_as_funding_decision(key):
    # The grant types must re-point the panel at proposed FUTURE work and remap
    # the publication verdict scale onto a funding decision (the conference-paper
    # mechanism), so the editor/meta-reviewer judge fundability, not publishability.
    framing = ARTICLE_TYPES[key].review_framing.lower()
    assert "future work" in framing
    assert "fundable" in framing
    assert "preliminary data" in framing


def test_exploratory_grant_tolerates_missing_preliminary_data():
    framing = ARTICLE_TYPES["exploratory-grant"].review_framing.lower()
    assert "not required" in framing or "not be penalized" in framing


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


# --- funder / grant profiles -----------------------------------------------


@pytest.mark.parametrize("slug", ["nih-r01", "nih-r21", "nsf", "erc"])
def test_funder_profiles_load_and_render(slug):
    # A funder profile is just a JournalProfile whose guidelines carry the
    # grant criteria; it must load and render a prompt block like any venue.
    profile = load_journal(slug)
    assert profile is not None
    block = profile.to_prompt_block()
    assert block.startswith("=== TARGET JOURNAL ===")
    assert profile.guidelines.strip()


def test_funder_profiles_attach_grant_type_notes():
    # The grant-style article type each funder uses carries its page-limit notes.
    assert load_journal("nih-r01").article_type_limits("grant-proposal") is not None
    assert load_journal("nih-r21").article_type_limits("exploratory-grant") is not None
    assert load_journal("nsf").article_type_limits("grant-proposal") is not None
    assert load_journal("erc").article_type_limits("grant-proposal") is not None


def test_funder_profiles_appear_in_listing():
    from peerreviewagents.journals import list_journals

    slugs = {p.slug for p in list_journals()}
    assert {"nih-r01", "nih-r21", "nsf", "erc"} <= slugs


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
    blocks = context_block(state)
    assert blocks[0] == manuscript_block(state)
    assert "=== MANUSCRIPT TYPE ===" in blocks[1]


def test_manuscript_leads_and_directives_funnel_journal_type_strictness():
    state = {
        "manuscript_md": "Hello world.",
        "config": {},
        "journal_block": "=== TARGET JOURNAL ===\nName: T\n=== END TARGET JOURNAL ===",
        "article_type_block": article_type_block("letter"),
        "strictness_block": strictness_block(4),
    }
    blocks = context_block(state)
    assert blocks[0] == manuscript_block(state)
    directives = blocks[1]
    assert (
        directives.index("TARGET JOURNAL")
        < directives.index("MANUSCRIPT TYPE")
        < directives.index("REVIEW STRICTNESS")
    )
