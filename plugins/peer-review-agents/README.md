# Peer Review Agents plugin

This plugin loads the local PeerReviewAgents MCP server and manuscript review skill.

For Claude Code and Claude Desktop's Code tab, run
`bash scripts/install-claude.sh` from the repository root on macOS or Linux.
On Windows run
`powershell -NoProfile -ExecutionPolicy Bypass -File scripts/install-claude.ps1`.
Both installers download Python automatically. Install and sign in to the standalone
Claude Code CLI first. The configured plugin uses absolute executable paths,
so it can launch from Desktop without your terminal's PATH. VS Code is not required.
See [the setup guide](https://github.com/pgarrett-scripps/PeerReviewAgents/blob/main/docs/INTEGRATIONS.md#claude-code-and-claude-desktop).

For other clients or manual plugin installation, install the Python runtime before enabling the plugin:

```bash
uv tool install --python ">=3.10,<3.14" "/absolute/path/to/PeerReviewAgents[mcp]"
```

The `peerreview-mcp` command must be available on the coding agent's `PATH`. See the repository's `docs/INTEGRATIONS.md` for installation, upgrades, supported providers, data handling, and troubleshooting.

No PeerReviewAgents service or account is used. The selected coding agent provider uses the authentication already configured in its local CLI.
