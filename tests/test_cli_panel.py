"""`--show-panel` and `--single-model`: seeing the bill before paying it.

The panel table exists because the only way to learn which model an agent
would run used to be running it. It needs no API key and no manuscript, and
it runs the same config validation and provider/model-id shape check a real
run does — a preflight for exactly the mismatches that otherwise surface
mid-run with half the panel already billed.
"""

from __future__ import annotations

import pytest

from peerreviewagents.cli.main import _show_panel, build_parser, config_from_args
from peerreviewagents.panel import PIPELINE_AGENTS


@pytest.fixture(autouse=True)
def _isolated_config(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PEERREVIEW_SINGLE_MODEL", raising=False)
    monkeypatch.delenv("PEERREVIEW_REASONING_MODEL", raising=False)
    monkeypatch.delenv("PEERREVIEW_PROVIDER", raising=False)
    # The whole point: the table must not need a key.
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)


def _panel_lines(capsys) -> list[str]:
    return [line for line in capsys.readouterr().out.splitlines() if line.strip()]


def test_show_panel_lists_every_agent_without_an_api_key(capsys):
    _show_panel({"provider": "openrouter",
                 "reasoning_model": "anthropic/claude-opus-5"})
    lines = _panel_lines(capsys)
    assert lines[0].split()[:3] == ["agent", "tag", "provider"]
    body = "\n".join(lines[1:])
    for agent, _tag, _effort in PIPELINE_AGENTS:
        assert agent in body
    # One row per agent, plus the header — plain and grep-able.
    assert len(lines) == len(PIPELINE_AGENTS) + 1


def test_show_panel_shows_the_tag_split_and_the_rate(capsys):
    _show_panel({
        "provider": "openrouter",
        "reasoning_model": "anthropic/claude-opus-5",
        "models": {"reviewer": {"model": "anthropic/claude-haiku-4.5"}},
    })
    rows = {line.split()[0]: line for line in _panel_lines(capsys)[1:]}
    assert "claude-haiku-4.5" in rows["reviewer_data_analysis"]
    assert "$1/$5 per Mtok" in rows["reviewer_data_analysis"]
    assert "claude-opus-5" in rows["editor"]
    assert "$5/$25 per Mtok" in rows["editor"]
    # The editor's call-site default effort is visible, not a mystery.
    assert "medium" in rows["editor"]


def test_show_panel_marks_models_the_table_cannot_price(capsys):
    _show_panel({"provider": "openrouter",
                 "reasoning_model": "deepseek/deepseek-chat"})
    body = "\n".join(_panel_lines(capsys)[1:])
    assert "unpriced" in body


def test_show_panel_is_a_preflight_for_shape_mismatches(capsys):
    """provider=openrouter with a bare Anthropic id fails here, at the desk,
    instead of mid-run after money is spent."""
    with pytest.raises(SystemExit) as exc:
        _show_panel({"provider": "openrouter",
                     "reasoning_model": "claude-haiku-4-5"})
    assert exc.value.code == 1


def test_the_flag_is_wired_end_to_end(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["peerreview", "--show-panel"])
    from peerreviewagents.cli.main import run

    run()  # no manuscript, no key, no SystemExit
    assert "editor" in capsys.readouterr().out


# --- --single-model ------------------------------------------------------------


def _project_toml(tmp_path) -> None:
    (tmp_path / "peerreview.toml").write_text(
        '[models.reviewer]\nmodel = "anthropic/claude-haiku-4.5"\n'
        '[agent_models]\neditor = "synthesis"\n'
    )


def test_single_model_flattens_the_tables_from_the_cli(tmp_path, recwarn):
    """The In Silico dance, upstreamed: --model on a config with a per-agent
    split used to review nothing on the named model while the tables billed
    Claude; --single-model makes the flag mean what it says."""
    _project_toml(tmp_path)
    args = build_parser().parse_args(
        ["--single-model", "--reasoning-model", "deepseek/deepseek-chat"]
    )
    cfg = config_from_args(args)
    assert cfg["models"] == {}
    assert cfg["agent_models"] == {}
    assert cfg["reasoning_model"] == "deepseek/deepseek-chat"
    assert not [w for w in recwarn if issubclass(w.category, UserWarning)]


def test_without_the_flag_the_conflict_warns(tmp_path):
    _project_toml(tmp_path)
    args = build_parser().parse_args(["--reasoning-model", "deepseek/deepseek-chat"])
    with pytest.warns(UserWarning, match="single-model"):
        config_from_args(args)


def test_the_flag_defaults_to_unset_so_toml_can_own_it(tmp_path):
    _project_toml(tmp_path)
    args = build_parser().parse_args([])
    cfg = config_from_args(args)
    assert cfg["single_model"] is False
    assert cfg["models"] != {}
