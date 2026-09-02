# Peer Review Agents for Pi

This Pi package loads the PeerReviewAgents manuscript review skill and local MCP server. It depends on `pi-mcp-adapter`, which the package manager installs with the package.

Install the Python runtime first:

```bash
uv tool install --python ">=3.10,<3.14" "/absolute/path/to/PeerReviewAgents[mcp]"
```

Install this package from the repository checkout:

```bash
pi install /absolute/path/to/PeerReviewAgents/integrations/pi
```

Use `provider: pi` to run the review panel through the model and authentication configured in Pi. Use another supported provider name when Pi should control a review executed by a different local coding agent.
