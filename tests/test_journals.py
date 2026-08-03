"""Tests for journal profiles: loading, listing, rendering, and the
context-block injection that carries venue context into agent prompts.
"""

from __future__ import annotations

import textwrap

import pytest

from peerreviewagents.agents.utils.agent_utils import context_block, manuscript_block
from peerreviewagents.default_config import get_config
from peerreviewagents.journals import (
    JournalProfile,
    list_journals,
    load_journal,
)


def _write_journal(directory, slug: str, body: str) -> None:
    (directory / f"{slug}.toml").write_text(textwrap.dedent(body), encoding="utf-8")


@pytest.fixture()
def journals_dir(tmp_path):
    d = tmp_path / "journals"
    d.mkdir()
    _write_journal(d, "test-journal", """
        name = "Test Journal"
        field = "testology"
        impact_factor = 9.9
        impact_factor_year = 2025
        max_words = 4000
        max_figures = 6
        guidelines = "Be rigorous."
    """)
    # A second, sparse journal (only name) to prove optionality.
    _write_journal(d, "sparse", 'name = "Sparse Journal"\n')
    # Template-style file must be ignored by list_journals.
    _write_journal(d, "_template", 'name = ""\n')
    return d


def _cfg(journals_dir, **extra) -> dict:
    return {"journals_dir": str(journals_dir), **extra}


# --- loading ---------------------------------------------------------------


def test_load_journal_parses_fields(journals_dir):
    p = load_journal("test-journal", _cfg(journals_dir))
    assert isinstance(p, JournalProfile)
    assert p.slug == "test-journal"
    assert p.name == "Test Journal"
    assert p.impact_factor == 9.9
    assert p.max_words == 4000


def test_load_journal_empty_slug_returns_none(journals_dir):
    assert load_journal("", _cfg(journals_dir)) is None
    assert load_journal(None, _cfg(journals_dir)) is None


def test_load_journal_unknown_slug_raises_with_available(journals_dir):
    with pytest.raises(FileNotFoundError) as exc:
        load_journal("does-not-exist", _cfg(journals_dir))
    # Error lists valid slugs so the CLI can guide the user.
    assert "test-journal" in str(exc.value)


def test_sparse_journal_only_requires_name(journals_dir):
    p = load_journal("sparse", _cfg(journals_dir))
    assert p.name == "Sparse Journal"
    assert p.impact_factor == 0.0
    assert p.guidelines == ""


# --- listing ---------------------------------------------------------------


def test_list_journals_excludes_template_and_sorts(journals_dir):
    profiles = list_journals(_cfg(journals_dir))
    slugs = [p.slug for p in profiles]
    assert "_template" not in slugs
    assert slugs == ["sparse", "test-journal"]  # sorted by name


def test_list_journals_missing_dir_is_empty(tmp_path):
    assert list_journals({"journals_dir": str(tmp_path / "nope")}) == []


# --- rendering -------------------------------------------------------------


def test_prompt_block_includes_key_fields(journals_dir):
    block = load_journal("test-journal", _cfg(journals_dir)).to_prompt_block()
    assert "=== TARGET JOURNAL ===" in block
    assert "Test Journal" in block
    assert "4000 words" in block
    assert "6 display items" in block
    assert block.rstrip().endswith("=== END TARGET JOURNAL ===")


def test_prompt_block_omits_empty_fields(journals_dir):
    block = load_journal("sparse", _cfg(journals_dir)).to_prompt_block()
    assert "Sparse Journal" in block
    assert "impact factor" not in block.lower()
    assert "Submission limits" not in block


# --- config wiring ---------------------------------------------------------


def test_target_journal_config_defaults_to_general():
    assert get_config().get("target_journal") == "general"


def test_general_profile_exists_and_renders():
    # The default profile must resolve against the real journals/ dir and
    # produce a non-empty prompt block, or default runs would silently
    # degrade to venue-agnostic.
    profile = load_journal("general", get_config())
    assert profile is not None
    assert profile.to_prompt_block().strip()


def test_target_journal_env_and_kwarg_precedence(monkeypatch):
    monkeypatch.setenv("PEERREVIEW_TARGET_JOURNAL", "from-env")
    assert get_config()["target_journal"] == "from-env"
    # Explicit kwarg (CLI flag) wins over the env var.
    assert get_config(target_journal="from-flag")["target_journal"] == "from-flag"


# --- context-block injection ----------------------------------------------


def test_context_block_without_journal_is_just_the_manuscript():
    state = {"manuscript_md": "Hello world.", "config": {}}
    assert context_block(state) == [manuscript_block(state)]


def test_context_block_leads_with_the_manuscript_then_the_journal(journals_dir):
    """The manuscript is block 0, byte-identical to what every other agent
    sends.

    This ordering is the cache contract. The directives used to come first,
    which made a stable prefix for the agents that read them and shared
    nothing with the bare manuscript_block the debate, rebuttal and scout
    send — so the same manuscript was cached once per group. Manuscript
    first means both groups match on it and only the directives are written
    on top.
    """
    block = load_journal("test-journal", _cfg(journals_dir)).to_prompt_block()
    state = {"manuscript_md": "Hello world.", "config": {}, "journal_block": block}
    blocks = context_block(state)
    assert blocks[0] == manuscript_block(state)
    assert "=== TARGET JOURNAL ===" in blocks[1]
