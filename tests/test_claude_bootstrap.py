"""The Bash installer must bootstrap without Python or uv on PATH."""

from __future__ import annotations

import os
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BASH = shutil.which("bash")
pytestmark = pytest.mark.skipif(os.name == "nt" or not BASH, reason="macOS and Linux Bash installer")


def script(path, body):
    path.write_text("#!/bin/bash\nset -eu\n" + body)
    path.chmod(0o755)
    return path


@pytest.fixture
def bootstrap(tmp_path):
    commands = tmp_path / "commands"
    commands.mkdir()
    for name in ["bash", "dirname", "mktemp", "rm", "sh", "cat", "cp", "mkdir", "tar", "gzip"]:
        (commands / name).symlink_to(shutil.which(name))
    mock_uv = script(tmp_path / "mock-uv", 'printf "%s\\n" "$@" > "$PRA_TEST_LOG"\n')
    uv_installer = script(tmp_path / "uv-installer", '''mkdir -p "$UV_UNMANAGED_INSTALL"
cp "$PRA_TEST_UV" "$UV_UNMANAGED_INSTALL/uv"
''')
    script(commands / "curl", '''printf "%s\\n" "$*" >> "$PRA_TEST_DOWNLOADS"
destination="${!#}"
case "$*" in
  *astral.sh*) cp "$PRA_TEST_INSTALLER" "$destination" ;;
  *) cp "$PRA_TEST_ARCHIVE" "$destination" ;;
esac
''')
    env = {
        **os.environ,
        "PATH": str(commands),
        "PEERREVIEW_BOOTSTRAP_DIR": str(tmp_path / "private bootstrap"),
        "PRA_TEST_LOG": str(tmp_path / "arguments"),
        "PRA_TEST_DOWNLOADS": str(tmp_path / "downloads"),
        "PRA_TEST_INSTALLER": str(uv_installer),
        "PRA_TEST_UV": str(mock_uv),
    }
    assert shutil.which("python3", path=env["PATH"]) is None
    assert shutil.which("uv", path=env["PATH"]) is None
    return env


def run(entry, env, *args):
    return subprocess.run([BASH, str(entry), *args], env=env, capture_output=True, text=True, timeout=15)


def test_bootstraps_without_python_and_preserves_arguments(bootstrap):
    result = run(ROOT / "scripts/install-claude.sh", bootstrap, "--data-dir", "/path with spaces", "--configure-only")
    assert result.returncode == 0, result.stderr
    args = Path(bootstrap["PRA_TEST_LOG"]).read_text().splitlines()
    assert args[:7] == ["run", "--no-project", "--no-config", "--managed-python", "--python", "3.12", str(ROOT / "scripts/install-claude.py")]
    assert args[-3:] == ["--data-dir", "/path with spaces", "--configure-only"]
    assert "astral.sh/uv/install.sh" in Path(bootstrap["PRA_TEST_DOWNLOADS"]).read_text()
    result = run(ROOT / "scripts/install-claude.sh", bootstrap, "--configure-only")
    assert result.returncode == 0, result.stderr
    assert len(Path(bootstrap["PRA_TEST_DOWNLOADS"]).read_text().splitlines()) == 1


def test_existing_shortcut_bootstraps_and_forwards_options(bootstrap):
    result = run(ROOT / "scripts/install-local.sh", bootstrap, "claude", "--runtime", "/my runtime")
    assert result.returncode == 0, result.stderr
    assert Path(bootstrap["PRA_TEST_LOG"]).read_text().splitlines()[-2:] == ["--runtime", "/my runtime"]


def test_standalone_script_downloads_source_and_cleans_temporary_files(bootstrap, tmp_path):
    standalone = tmp_path / "install.sh"
    shutil.copyfile(ROOT / "scripts/install-claude.sh", standalone)
    archive = tmp_path / "source.tar.gz"
    with tarfile.open(archive, "w:gz") as package:
        package.add(ROOT / "scripts/install-claude.py", arcname="PeerReviewAgents-main/scripts/install-claude.py")
    bootstrap["PRA_TEST_ARCHIVE"] = str(archive)
    result = run(standalone, bootstrap, "--configure-only")
    assert result.returncode == 0, result.stderr
    downloads = Path(bootstrap["PRA_TEST_DOWNLOADS"]).read_text()
    assert "PeerReviewAgents/archive/refs/heads/main.tar.gz" in downloads
    source = Path(Path(bootstrap["PRA_TEST_LOG"]).read_text().splitlines()[6])
    assert source.name == "install-claude.py"
    assert not source.exists()


def test_failed_download_stops_before_executing_installer(bootstrap):
    script(Path(bootstrap["PATH"]) / "curl", "exit 22\n")
    result = run(ROOT / "scripts/install-claude.sh", bootstrap)
    assert result.returncode == 22
    assert not Path(bootstrap["PRA_TEST_LOG"]).exists()


def test_help_does_not_download_or_install(bootstrap):
    result = run(ROOT / "scripts/install-claude.sh", bootstrap, "--help")
    assert result.returncode == 0
    assert "No preinstalled Python" in result.stdout
    assert not Path(bootstrap["PRA_TEST_DOWNLOADS"]).exists()
