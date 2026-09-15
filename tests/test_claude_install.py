"""Desktop installation must work without a terminal's PATH or an IDE."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
from pathlib import Path

import pytest

from peerreviewagents.runtime.subscriptions import (
    SubscriptionCLIError,
    _completed,
    validate_subscription_cli,
)

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("install_claude", ROOT / "scripts/install-claude.py")
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


def make_executable(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(0o755)
    return path


@pytest.fixture
def clean_home(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("PATH", "")
    monkeypatch.delenv("PEERREVIEW_CLAUDE_PATH", raising=False)
    return tmp_path


def test_native_cli_is_found_without_shell_path(clean_home):
    binary = make_executable(clean_home / ".local/bin" / ("claude.exe" if os.name == "nt" else "claude"))
    assert validate_subscription_cli("claude-code") == str(binary)
    assert installer.executable("claude") == str(binary)


def test_explicit_cli_path_takes_precedence(monkeypatch, clean_home):
    binary = make_executable(clean_home / "custom install" / "claude")
    monkeypatch.setenv("PEERREVIEW_CLAUDE_PATH", str(binary))
    assert validate_subscription_cli("claude-code") == str(binary)


def test_invalid_override_does_not_silently_use_another_cli(monkeypatch, clean_home):
    monkeypatch.setenv("PEERREVIEW_CLAUDE_PATH", "relative/claude")
    with pytest.raises(SubscriptionCLIError, match="executable absolute path"):
        validate_subscription_cli("claude-code")


def test_missing_cli_explains_desktop_prerequisite(clean_home):
    with pytest.raises(SubscriptionCLIError, match="Claude Desktop alone"):
        validate_subscription_cli("claude-code")


def test_custom_path_install_remains_supported(monkeypatch, clean_home):
    monkeypatch.setattr("shutil.which", lambda name: "/opt/custom/claude")
    assert validate_subscription_cli("claude-code") == "/opt/custom/claude"


def test_unlaunchable_cli_produces_actionable_error(monkeypatch):
    def fail(*args, **kwargs):
        raise PermissionError("access denied")

    monkeypatch.setattr(subprocess, "run", fail)
    with pytest.raises(SubscriptionCLIError, match="Could not launch"):
        _completed(["/missing/claude"], "prompt", 1)


def test_cached_plugin_launches_with_no_path_and_spaces(tmp_path):
    runtime = make_executable(tmp_path / "Application Support" / "peerreview-mcp")
    claude = make_executable(tmp_path / "Claude Code" / "claude")
    marketplace = installer.write_marketplace(tmp_path / "install", str(runtime), str(claude))
    plugin = marketplace / "plugins/peer-review-agents"
    config = json.loads((plugin / ".mcp.json").read_text())["mcpServers"]["peer-review-agents"]
    assert config["command"] == str(runtime)
    assert config["env"]["PEERREVIEW_CLAUDE_PATH"] == str(claude)
    assert (plugin / "skills/peer-review-manuscript/SKILL.md").is_file()
    if os.name != "nt":
        subprocess.run([config["command"], *config["args"]], env={"PATH": "", **config["env"]}, check=True)
    manifest_file = plugin / ".claude-plugin/plugin.json"
    original_version = json.loads(manifest_file.read_text())["version"]
    installer.write_marketplace(tmp_path / "install", str(runtime), str(claude))
    assert json.loads(manifest_file.read_text())["version"] == original_version
    installer.write_marketplace(tmp_path / "install", str(runtime), "/another/claude")
    assert json.loads(manifest_file.read_text())["version"] != original_version


def test_missing_claude_stops_before_installing_runtime(monkeypatch, clean_home):
    monkeypatch.setattr("sys.argv", ["install-claude.py", "--data-dir", str(clean_home / "install")])
    assert installer.main() == 1
    assert not (clean_home / "install").exists()


def test_existing_marketplace_is_updated_without_removal(monkeypatch, tmp_path):
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, json.dumps([
            {"name": installer.MARKETPLACE, "path": str(tmp_path)},
        ]))

    monkeypatch.setattr(subprocess, "run", run)
    installer.register_plugin("/native/claude", tmp_path)
    assert ["/native/claude", "plugin", "marketplace", "update", installer.MARKETPLACE] in calls
    assert not any("remove" in call or "add" in call for call in calls)


def test_claude_manifest_versions_match_package():
    import re

    version = re.search(r'^version = "([^"]+)"', (ROOT / "pyproject.toml").read_text(), re.M)[1]
    for relative in [".claude-plugin/plugin.json", "plugins/peer-review-agents/.claude-plugin/plugin.json"]:
        assert json.loads((ROOT / relative).read_text())["version"] == version
