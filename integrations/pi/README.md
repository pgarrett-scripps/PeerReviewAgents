# Peer Review Agents for Pi

This Pi package loads the PeerReviewAgents manuscript review skill and local MCP server,
which reviews a manuscript PDF with the evidence-linked review engine and writes
`REVIEW.md` beside the saved run. It depends on `pi-mcp-adapter`, which the package manager installs with the package.

Install the Python runtime first:

```bash
uv tool install --python ">=3.10,<3.14" "/absolute/path/to/PeerReviewAgents[mcp]"
```

Install this package from the repository checkout:

```bash
pi install /absolute/path/to/PeerReviewAgents/integrations/pi
```

Pi drives the review; it does not run the model calls. The engine runs them on a
subscription transport of its own: Claude Code by default, or Codex when
`provider: "codex"` is passed to `start_peer_review`. Pi itself is not a review provider,
so the matching CLI has to be installed and signed in on the same machine.
