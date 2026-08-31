"""Model-table validation: config that silently pays for the wrong model.

Every warning tested here is a config In Silico actually shipped. A
``[models.screen]`` block for a tag no agent resolves through sat inert for
weeks (this repo's own docs advertised it as an example); an ``[agent_models]``
entry spelled ``data_analysis`` where the agent is ``reviewer_data_analysis``
silently kept that reviewer on the cheap tag; and an explicit
``--model minimax/minimax-m3`` was out-ranked by the tag tables for every
tagged agent — minimax reviewed nothing, and the run billed the lab for
Claude.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import peerreviewagents
from peerreviewagents.default_config import get_config
from peerreviewagents.panel import KNOWN_AGENTS, KNOWN_TAGS, PIPELINE_AGENTS
from peerreviewagents.runtime.providers import resolve_model


@pytest.fixture(autouse=True)
def _isolated_config(tmp_path, monkeypatch):
    """Keep the machine's real config layers out of these tests."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PEERREVIEW_SINGLE_MODEL", raising=False)
    monkeypatch.delenv("PEERREVIEW_REASONING_MODEL", raising=False)
    monkeypatch.delenv("PEERREVIEW_OPENAI_BASE_URL", raising=False)


def _toml(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "case.toml"
    path.write_text(text)
    return path


def test_default_is_the_graded_condensed_debate_pipeline():
    cfg = get_config()
    assert cfg["provider"] == "anthropic"
    assert cfg["max_debate_rounds"] == 2
    assert cfg["enable_debate"] is True
    assert cfg["enable_journal_recommender"] is False

    assert resolve_model(
        cfg, agent="reviewer_scientific_validity", default_tag="reviewer",
    ).model == "claude-haiku-4-5"
    assert resolve_model(
        cfg, agent="audit_methods_completeness", default_tag="audit",
    ).model == "claude-haiku-4-5"
    assert resolve_model(
        cfg, agent="debate_synthesizer", default_tag="synthesis",
    ).model == "claude-sonnet-5"
    assert resolve_model(
        cfg, agent="debate_advocate", default_tag="debate",
    ).model == "claude-sonnet-5"
    assert resolve_model(
        cfg, agent="editor", default_tag="synthesis",
    ).model == "claude-opus-5"


# --- 1a: unknown [models.<tag>] ----------------------------------------------


def test_a_models_screen_block_warns(tmp_path):
    """The In Silico incident verbatim: there is no 'screen' tag."""
    path = _toml(tmp_path, '[models.screen]\nmodel = "anthropic/claude-haiku-4.5"\n')
    with pytest.warns(UserWarning, match=r"\[models\.screen\].*silently inert"):
        get_config(config_path=path)


def test_known_tags_do_not_warn(tmp_path, recwarn):
    path = _toml(
        tmp_path,
        '[models.reviewer]\nmodel = "anthropic/claude-haiku-4.5"\n'
        '[models.synthesis]\nmodel = "anthropic/claude-opus-5"\neffort = "high"\n',
    )
    get_config(config_path=path)
    assert not [w for w in recwarn if issubclass(w.category, UserWarning)]


# --- 1b: unknown [agent_models.<name>] ----------------------------------------


def test_a_bare_reviewer_name_warns_with_the_prefixed_hint(tmp_path):
    """In Silico wrote `data_analysis = ...`; the agent key is
    `reviewer_data_analysis`, so the override moved nothing."""
    path = _toml(
        tmp_path,
        '[agent_models]\ndata_analysis = { model = "anthropic/claude-opus-5" }\n',
    )
    with pytest.warns(UserWarning, match=r"'reviewer_data_analysis'"):
        get_config(config_path=path)


def test_a_known_agent_key_does_not_warn(tmp_path, recwarn):
    path = _toml(
        tmp_path,
        '[agent_models]\n'
        'reviewer_data_analysis = { model = "anthropic/claude-opus-5" }\n'
        'editor = "synthesis"\n',
    )
    get_config(config_path=path)
    assert not [w for w in recwarn if issubclass(w.category, UserWarning)]


def test_an_agent_pointed_at_an_undefined_tag_warns(tmp_path):
    """A string selection resolves through [models]; a tag that exists
    nowhere resolves to the empty spec and the override does nothing."""
    path = _toml(tmp_path, '[agent_models]\neditor = "synthesys"\n')
    with pytest.warns(UserWarning, match="names a tag"):
        get_config(config_path=path)


# --- 1c: typo'd spec fields ----------------------------------------------------


def test_a_misspelled_spec_field_warns(tmp_path):
    path = _toml(tmp_path, '[models.reviewer]\nmodle = "anthropic/claude-haiku-4.5"\n')
    with pytest.warns(UserWarning, match="modle"):
        get_config(config_path=path)


def test_the_typo_check_reaches_inline_agent_specs(tmp_path):
    path = _toml(
        tmp_path,
        '[agent_models]\neditor = { model = "anthropic/claude-opus-5", efort = "high" }\n',
    )
    with pytest.warns(UserWarning, match="efort"):
        get_config(config_path=path)


# --- 1d: explicit model out-ranked by the tables --------------------------------


_SPLIT = '[models.reviewer]\nmodel = "anthropic/claude-haiku-4.5"\n'


def test_an_explicit_model_alongside_tag_tables_warns_and_names_single_model(tmp_path):
    path = _toml(tmp_path, _SPLIT)
    with pytest.warns(UserWarning, match="single-model"):
        get_config(config_path=path, reasoning_model="minimax/minimax-m3")


def test_a_toml_reasoning_model_is_a_default_not_a_conflict(tmp_path, recwarn):
    """Only a kwargs-level model (CLI --reasoning-model) is knowably explicit;
    a TOML model is the fallback the tables are meant to refine."""
    path = _toml(tmp_path, 'reasoning_model = "anthropic/claude-sonnet-5"\n' + _SPLIT)
    get_config(config_path=path)
    assert not [w for w in recwarn if issubclass(w.category, UserWarning)]


# --- 1e: single_model ------------------------------------------------------------


def test_single_model_clears_the_tables_and_silences_the_conflict(tmp_path, recwarn):
    path = _toml(tmp_path, _SPLIT + '[agent_models]\neditor = "synthesis"\n')
    cfg = get_config(
        config_path=path, reasoning_model="minimax/minimax-m3", single_model=True
    )
    assert cfg["models"] == {}
    assert cfg["agent_models"] == {}
    assert cfg["reasoning_model"] == "minimax/minimax-m3"
    assert not [w for w in recwarn if issubclass(w.category, UserWarning)]


def test_single_model_is_reachable_from_the_environment(tmp_path, monkeypatch):
    path = _toml(tmp_path, _SPLIT)
    monkeypatch.setenv("PEERREVIEW_SINGLE_MODEL", "1")
    cfg = get_config(config_path=path)
    assert cfg["single_model"] is True
    assert cfg["models"] == {}


def test_single_model_defaults_off(tmp_path):
    path = _toml(tmp_path, _SPLIT)
    cfg = get_config(config_path=path)
    assert cfg["single_model"] is False
    assert cfg["models"] != {}


# --- the roster cannot rot silently ---------------------------------------------
#
# panel.KNOWN_AGENTS / KNOWN_TAGS are hand-maintained mirrors of the
# `agent=` / `default_tag=` call sites (regen instructions in panel.py).
# This test re-derives both from source, so adding an agent without teaching
# the roster fails here instead of shipping a validator that cries wolf.

# Capture to the argument separator; a trailing ")" from a call that ends on
# this argument is stripped below (the f-string spellings contain parens).
_AGENT_ARG = re.compile(r"\bagent=([^,\n]+)")
_LITERAL = re.compile(r'^"([a-z_]+)"$')
_DEFAULT_TAG = re.compile(r'default_tag="([a-z_]+)"')
_DEBATE_ROLE = re.compile(r'make_debate_node\(\s*"([a-z_]+)"')

# Dynamic spellings at the call sites, each covered by a roster below.
_ACCOUNTED_DYNAMIC = {
    'f"debate_{role.lower()}"',   # -> debate roles, derived from make_debate_node
    'f"audit_{AUDITOR_NAME}"',    # -> ALL_AUDITOR_NAMES
    "node_name",                  # -> reviewer_/audit_ prefixed rosters
    "agent",                      # make_llm's pass-through parameter
}


def _call_site_sources() -> list[str]:
    root = Path(peerreviewagents.__file__).parent
    return [
        p.read_text()
        for sub in ("agents", "eval")
        for p in (root / sub).rglob("*.py")
    ]


def test_roster_covers_every_call_site():
    literals: set[str] = set()
    debate_roles: set[str] = set()
    tags: set[str] = set()
    for text in _call_site_sources():
        tags.update(_DEFAULT_TAG.findall(text))
        debate_roles.update(_DEBATE_ROLE.findall(text))
        for raw in _AGENT_ARG.findall(text):
            arg = raw.strip()
            while arg.endswith(")") and arg.count(")") > arg.count("("):
                arg = arg[:-1].rstrip()
            m = _LITERAL.match(arg)
            if m:
                literals.add(m.group(1))
            else:
                assert arg in _ACCOUNTED_DYNAMIC, (
                    f"unrecognized agent= spelling {arg!r}: teach this test "
                    "how to derive it and add the agent to panel.py"
                )

    assert tags, "no default_tag= call sites found — did the layout move?"
    assert tags <= KNOWN_TAGS, f"tags missing from panel.KNOWN_TAGS: {tags - KNOWN_TAGS}"
    assert literals <= KNOWN_AGENTS, (
        f"agents missing from panel.py: {literals - KNOWN_AGENTS}"
    )
    assert {f"debate_{r}" for r in debate_roles} <= KNOWN_AGENTS

    from peerreviewagents.agents.auditors import ALL_AUDITOR_NAMES
    from peerreviewagents.agents.reviewers import ALL_REVIEWER_NAMES

    assert {f"reviewer_{n}" for n in ALL_REVIEWER_NAMES} <= KNOWN_AGENTS
    assert {f"audit_{n}" for n in ALL_AUDITOR_NAMES} <= KNOWN_AGENTS


def test_every_pipeline_agent_declares_a_known_tag():
    assert {tag for _n, tag, _e in PIPELINE_AGENTS} <= KNOWN_TAGS
