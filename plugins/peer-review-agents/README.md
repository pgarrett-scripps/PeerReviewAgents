# Peer Review Agents plugin

This plugin loads the local PeerReviewAgents MCP server and manuscript review skill. The
server runs the evidence-linked review engine: it imports a manuscript PDF, reviews it with
independent reviewers and an audited debate synthesis, and writes `REVIEW.md` beside the
saved run. Every finding is anchored to an exact quotation in the manuscript.

For Claude Code and Claude Desktop's Code tab, run
`bash scripts/install-claude.sh` from the repository root on macOS, Linux, or Windows
with Git Bash. The installer detects your platform and downloads Python automatically.
Install and sign in to the standalone
Claude Code CLI first. The configured plugin uses absolute executable paths,
so it can launch from Desktop without your terminal's PATH. VS Code is not required.
See [the setup guide](https://github.com/pgarrett-scripps/PeerReviewAgents/blob/main/docs/INTEGRATIONS.md#claude-code-and-claude-desktop).

For other clients or manual plugin installation, install the Python runtime before enabling the plugin:

```bash
uv tool install --python ">=3.10,<3.14" "/absolute/path/to/PeerReviewAgents[mcp]"
```

The `peerreview-mcp` command must be available on the coding agent's `PATH`. See the repository's `docs/INTEGRATIONS.md` for installation, upgrades, supported providers, data handling, and troubleshooting.

No PeerReviewAgents service or account is used. A review runs on a subscription transport
whose authentication is already configured in its local CLI: Claude Code by default, or
Codex when `provider: "codex"` is named. No API key provider is used.

`PEERREVIEW_MCP_INPUT_ROOTS` limits which folders a manuscript may be read from, and
`PEERREVIEW_MCP_OUTPUT_ROOT` sets where reviews are written. Both default to the working
directory the server was launched in.
