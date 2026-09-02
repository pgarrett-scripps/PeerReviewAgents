# Peer Review Agents plugin

This plugin loads the local PeerReviewAgents MCP server and manuscript review skill.

Install the Python runtime before enabling the plugin:

```bash
uv tool install --python ">=3.10,<3.14" "/absolute/path/to/PeerReviewAgents[mcp]"
```

The `peerreview-mcp` command must be available on the coding agent's `PATH`. See the repository's `docs/INTEGRATIONS.md` for installation, upgrades, supported providers, data handling, and troubleshooting.

No PeerReviewAgents service or account is used. The selected coding agent provider uses the authentication already configured in its local CLI.
