# Local agent integrations

PeerReviewAgents runs on the user's machine. It does not require a hosted PeerReviewAgents service, a PeerReviewAgents account, or a separate model API key when a coding agent provider is selected.

The selected coding agent still sends model prompts through its own authenticated service. PeerReviewAgents does not receive those prompts. Manuscripts, checkpoints, and report files remain in local filesystem paths controlled by the user.

## Architecture

The integration has three shared parts:

1. The Python peer-review pipeline.
2. The local `peerreview-mcp` standard input and output server.
3. The `peer-review-manuscript` Agent Skill.

Each coding agent needs packaging that loads the skill and MCP server. A small subscription adapter is also needed when the review panel should use that agent's authenticated model access.

## Compatibility

| Client | Loads local MCP | Loads the skill | Uses client authentication as a review provider | Validation level |
|---|---:|---:|---:|---|
| Claude Code | Yes | Yes | `claude-code` | Live tested |
| Codex | Yes | Yes | `codex` | Live tested |
| Factory Droid | Yes | Yes | `droid` | Unit tested, live CLI not available in the development environment |
| Pi | Yes, through the packaged MCP adapter | Yes | `pi` | Package and command parsing tested, live login not available in the development environment |
| Other standard input and output MCP clients | Usually | Client dependent | No | Configuration recipe only |

Controller support and provider support are different. A client can call the MCP tools while the review panel uses another provider. For example, Pi can start a review whose provider is `codex` if both CLIs are installed and authenticated.

## Prerequisites

- Python 3.10 through 3.13.
- One supported coding agent CLI installed and authenticated.
- `uv` is recommended. A user-level `pip` installation is the fallback.

From a cloned checkout, install the local runtime:

```bash
./scripts/install-local.sh runtime
```

This installs `peerreview` and `peerreview-mcp` as user-level commands. It does not install or configure an agent plugin.

## Claude Code

Install the runtime and plugin:

```bash
./scripts/install-local.sh claude
```

For source development without marketplace caching:

```bash
claude --plugin-dir /absolute/path/to/PeerReviewAgents
```

Start a review with `provider: claude-code`. The adapter launches a fresh `claude -p` process with tools disabled, safe mode enabled, no session persistence, and schema-constrained JSON output.

Upgrade the runtime and cached plugin:

```bash
./scripts/install-local.sh runtime
claude plugin marketplace update peer-review-agents-local
claude plugin update peer-review-agents@peer-review-agents-local
```

Uninstall the plugin:

```bash
claude plugin uninstall peer-review-agents@peer-review-agents-local
```

## Codex

Install the runtime and plugin:

```bash
./scripts/install-local.sh codex
```

Start a review with `provider: codex`. The adapter launches a fresh ephemeral `codex exec` process in a temporary directory with a read-only sandbox, user rules disabled, and schema-constrained JSON output.

Upgrade by reinstalling after updating the checkout:

```bash
./scripts/install-local.sh runtime
codex plugin add peer-review-agents@peer-review-agents-local
```

Uninstall the plugin:

```bash
codex plugin remove peer-review-agents@peer-review-agents-local
```

## Factory Droid

Factory Droid accepts Claude Code plugin layouts and translates `.mcp.json` automatically. Install the same local marketplace:

```bash
./scripts/install-local.sh droid
```

Start a review with `provider: droid`. The adapter launches `droid exec` in its default read-only mode, disables built-in skills, and uses an empty temporary working directory. The requested JSON schema is included in the prompt because the Droid CLI does not currently expose its SDK schema option as a command-line flag.

Upgrade or uninstall:

```bash
droid plugin update peer-review-agents@peer-review-agents-local --scope user
droid plugin uninstall peer-review-agents@peer-review-agents-local --scope user
```

## Pi

Install the runtime and Pi package:

```bash
./scripts/install-local.sh pi
```

The Pi package includes the review skill and configures the local MCP server through `pi-mcp-adapter`. Start a review with `provider: pi`. The adapter launches a fresh `pi --mode json` process with tools, extensions, skills, context files, session persistence, and project trust disabled.

Upgrade or uninstall:

```bash
pi update /absolute/path/to/PeerReviewAgents/integrations/pi
pi remove /absolute/path/to/PeerReviewAgents/integrations/pi
```

## Generic MCP clients

Any client that supports local standard input and output MCP servers can use this configuration after the runtime is installed:

```json
{
  "mcpServers": {
    "peer-review-agents": {
      "command": "peerreview-mcp",
      "args": []
    }
  }
}
```

The client can start and monitor reviews, list journals, and read completed artifacts. Its own subscription will run the panel only if PeerReviewAgents has a tested provider adapter for that client's noninteractive CLI.

## Data handling

- PDF conversion, orchestration, checkpoints, and report generation run locally.
- The selected coding agent receives the manuscript content needed for each model turn through its normal authenticated service.
- PeerReviewAgents does not add telemetry, remote storage, accounts, or billing.
- Live literature research is off by default in the MCP tool. Enabling it sends research queries to the configured literature services.
- API-backed providers remain available, but they are optional and require their normal environment variables.

## Troubleshooting

### The MCP server does not start

Confirm the command is installed and visible to the agent process:

```bash
command -v peerreview-mcp
```

The MCP command waits for protocol input, so a manual launch appears idle. Stop it with `Ctrl+C`.

### A provider executable is missing

Run the matching command directly:

```bash
claude --version
codex --version
droid --version
pi --version
```

Install and authenticate the missing client, then retry the review.

### The client is installed but not authenticated

Open the client normally and complete its login flow. PeerReviewAgents never reads or copies the client's credential files.

### A background review disappears

Jobs are held by the running MCP server process. Restarting or reinstalling the plugin starts a new server and clears its in-memory job list. Completed report files remain on disk.

### Remove the Python runtime

For an installation created by `uv`:

```bash
uv tool uninstall peerreviewagents
```

For the fallback user-level `pip` installation:

```bash
python3 -m pip uninstall peerreviewagents
```
