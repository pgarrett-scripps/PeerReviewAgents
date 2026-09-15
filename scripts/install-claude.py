#!/usr/bin/env python3
"""Install a Claude Code plugin with paths resolved for this computer."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MARKETPLACE = "peer-review-agents-configured"
PLUGIN = f"peer-review-agents@{MARKETPLACE}"


def executable(name: str, override: str | None = None) -> str:
    if override:
        candidate = Path(override).expanduser().absolute()
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
        raise RuntimeError(f"Executable does not exist or cannot be run: {candidate}")
    native = Path.home() / ".local" / "bin" / (name + (".exe" if os.name == "nt" else ""))
    if native.is_file() and os.access(native, os.X_OK):
        return str(native)
    found = shutil.which(name)
    if found:
        return os.path.abspath(found)
    raise RuntimeError(
        f"Cannot find {name}. Install the standalone Claude Code CLI and sign in "
        "before running this installer. See https://code.claude.com/docs/en/quickstart"
        if name == "claude" else f"Cannot find {name}."
    )


def install_runtime(destination: Path) -> str:
    environment = destination / "runtime"
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    try:
        uv = executable("uv")
    except RuntimeError:
        uv = None
    if uv:
        if not python.exists():
            subprocess.run(
                [uv, "venv", "--python", ">=3.10,<3.14", str(environment)], check=True
            )
        subprocess.run(
            [uv, "pip", "install", "--python", str(python), "--upgrade", f"{REPO}[mcp]"],
            check=True,
        )
    else:
        if not (3, 10) <= sys.version_info < (3, 14):
            raise RuntimeError("Install uv or run this installer with Python 3.10 through 3.13.")
        if not python.exists():
            subprocess.run([sys.executable, "-m", "venv", str(environment)], check=True)
        subprocess.run(
            [str(python), "-m", "pip", "install", "--upgrade", f"{REPO}[mcp]"], check=True
        )
    return executable(
        "peerreview-mcp",
        str(environment / ("Scripts/peerreview-mcp.exe" if os.name == "nt" else "bin/peerreview-mcp")),
    )


def write_marketplace(destination: Path, runtime: str, claude: str) -> Path:
    marketplace = destination / "marketplace"
    plugin = marketplace / "plugins" / "peer-review-agents"
    (plugin / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    (marketplace / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    source = REPO / "plugins" / "peer-review-agents"
    shutil.copytree(source / "skills", plugin / "skills", dirs_exist_ok=True)
    config = {"mcpServers": {"peer-review-agents": {
        "command": runtime,
        "args": [],
        "env": {"PEERREVIEW_CLAUDE_PATH": claude},
    }}}
    encoded = json.dumps(config, indent=2) + "\n"
    (plugin / ".mcp.json").write_text(encoded, encoding="utf-8")
    manifest = json.loads((source / ".claude-plugin" / "plugin.json").read_text())
    # Cache identity changes when paths or the installed skill change.
    digest = hashlib.sha256(encoded.encode())
    for path in sorted((plugin / "skills").rglob("*")):
        if path.is_file():
            digest.update(path.read_bytes())
    manifest["version"] = manifest["version"].split("+")[0] + "+local." + digest.hexdigest()[:12]
    (plugin / ".claude-plugin" / "plugin.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    (marketplace / ".claude-plugin" / "marketplace.json").write_text(json.dumps({
        "name": MARKETPLACE,
        "owner": {"name": "PeerReviewAgents contributors"},
        "plugins": [{"name": "peer-review-agents", "source": "./plugins/peer-review-agents"}],
    }, indent=2) + "\n", encoding="utf-8")
    return marketplace


def register_plugin(claude: str, marketplace: Path) -> None:
    result = subprocess.run(
        [claude, "plugin", "marketplace", "list", "--json"],
        check=True, capture_output=True, text=True,
    )
    existing = next((item for item in json.loads(result.stdout) if item["name"] == MARKETPLACE), None)
    if existing:
        source = existing.get("path") or existing.get("installLocation")
        if not source or Path(source).resolve() != marketplace.resolve():
            raise RuntimeError(
                f"{MARKETPLACE} already points to {source}. Reuse that data directory "
                "or remove that marketplace in Claude before changing locations."
            )
        subprocess.run([claude, "plugin", "marketplace", "update", MARKETPLACE], check=True)
    else:
        subprocess.run([claude, "plugin", "marketplace", "add", str(marketplace)], check=True)
    subprocess.run([claude, "plugin", "install", PLUGIN, "--scope", "user"], check=True)
    subprocess.run([claude, "plugin", "update", PLUGIN, "--scope", "user"], check=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--claude", help="Path to a standalone Claude Code executable")
    parser.add_argument("--runtime", help="Use an existing peerreview-mcp executable")
    default_root = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / ".local" / "share")))
    parser.add_argument("--data-dir", type=Path, default=default_root / "peerreviewagents" / "claude")
    parser.add_argument("--configure-only", action="store_true", help="Prepare files without registering in Claude")
    args = parser.parse_args()
    try:
        claude = executable("claude", args.claude or os.environ.get("PEERREVIEW_CLAUDE_PATH"))
        subprocess.run([claude, "--version"], check=True)
        destination = args.data_dir.expanduser().absolute()
        destination.mkdir(parents=True, exist_ok=True)
        runtime = executable("peerreview-mcp", args.runtime) if args.runtime else install_runtime(destination)
        marketplace = write_marketplace(destination, runtime, claude)
        if not args.configure_only:
            register_plugin(claude, marketplace)
        print(f"PRA MCP executable: {runtime}")
        print(f"Claude Code executable: {claude}")
        print(f"Configured marketplace: {marketplace}")
        print("Restart Claude Desktop or start a new Claude Code session to load the plugin.")
        print("If an older peer-review-agents-local plugin is enabled, disable it to avoid duplicate tools.")
        return 0
    except (RuntimeError, OSError, subprocess.CalledProcessError, ValueError) as exc:
        print(f"PRA installation failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
