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
| Standalone Claude Code CLI | Yes | Yes | `claude-code` | Live tested on Linux |
| Claude Desktop Code tab, local sessions | Yes | Yes | `claude-code`, through the standalone CLI | Installer and launch contract tested. Desktop UI not live tested |
| Codex | Yes | Yes | `codex` | Live tested |
| Factory Droid | Yes | Yes | `droid` | Unit tested, live CLI not available in the development environment |
| Pi | Yes, through the packaged MCP adapter | Yes | `pi` | Package and command parsing tested, live login not available in the development environment |
| Other standard input and output MCP clients | Usually | Client dependent | No | Configuration recipe only |

Controller support and provider support are different. A client can call the MCP tools while the review panel uses another provider. For example, Pi can start a review whose provider is `codex` if both CLIs are installed and authenticated.

## Prerequisites

- Python 3.10 through 3.13. The Claude shell installers provision this automatically.
- One supported coding agent CLI installed and authenticated.
- `uv` is recommended. A user-level `pip` installation is the fallback.

From a cloned checkout, install the local runtime:

```bash
./scripts/install-local.sh runtime
```

This installs `peerreview` and `peerreview-mcp` as user-level commands. It does not install or configure an agent plugin.

## Claude Code and Claude Desktop

Use the standalone Claude Code CLI. VS Code and its Claude extension are not required.
The Claude Desktop Code tab can host the plugin, but PRA runs the review panel
through the separately installed, signed-in `claude` command. Installing Desktop
alone does not provide that command, as explained in
[Anthropic's Desktop setup guide](https://code.claude.com/docs/en/desktop-quickstart).

### Install on a new computer

1. Install the [standalone Claude Code CLI](https://code.claude.com/docs/en/quickstart).
2. Open a terminal and run `claude` once to sign in with the account that will run the reviews.
3. Run the shared installer below. It detects your operating system. Python and uv are installed
   automatically when needed. No administrator access or manual Python setup is required.

Use the same command on **macOS, Linux, or Windows with Git Bash**. From a checkout:

```bash
bash scripts/install-claude.sh
```

Without a checkout, download and run the standalone installer:

```bash
curl -fsSL https://raw.githubusercontent.com/pgarrett-scripps/PeerReviewAgents/main/scripts/install-claude.sh -o /tmp/install-pra-claude.sh &&
bash /tmp/install-pra-claude.sh
```

The standalone script downloads the repository itself. Re-run it to upgrade.
On Windows, the script automatically handles native Windows paths and runtime setup.
It uses the [official uv installer](https://docs.astral.sh/uv/reference/installer/)
and [uv-managed Python](https://docs.astral.sh/uv/guides/install-python/), without editing shell profiles.
PRA still runs on Python internally, but users do not need to manage it.

<details>
<summary>Windows without Bash</summary>

Windows does not include Bash by default. If Git Bash is unavailable, run this in
PowerShell instead. There is no need to install Bash or WSL:

```powershell
$installer = Join-Path $env:TEMP "install-pra-claude.ps1"
Invoke-WebRequest -UseBasicParsing -Uri "https://raw.githubusercontent.com/pgarrett-scripps/PeerReviewAgents/main/scripts/install-claude.ps1" -OutFile $installer -ErrorAction Stop
powershell -NoProfile -ExecutionPolicy Bypass -File $installer
```

From a checkout, use `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/install-claude.ps1`.
This launches the same native Windows setup used by the Bash entry point.
It works with Windows PowerShell 5.1 or later.
Its optional flags are `-Claude`, `-Runtime`, `-DataDir`, `-ConfigureOnly`, and `-Help`.

</details>

The installer creates a dedicated PRA environment, installs the MCP extra, and
registers `peer-review-agents@peer-review-agents-configured` for the current user.

The generated plugin stores absolute paths to `peerreview-mcp` and `claude`, so
Desktop does not need to inherit your terminal's PATH. Paths containing spaces
are supported. No editor extension path is discovered or required.

4. Restart Claude Desktop or start a new Claude Code session. In Desktop, select
   the Code tab, a Local session, and the folder containing the manuscript.
5. Enable the configured plugin in the plugin manager and ask it to review the
   manuscript. Use `provider: claude-code` and `model: default`.

If an older `peer-review-agents@peer-review-agents-local` plugin is enabled,
disable it to avoid duplicate tools. The installer leaves other plugins and
settings intact. Desktop Code and the Desktop Chat tab have separate MCP
configuration, as described in the
[Desktop reference](https://code.claude.com/docs/en/desktop#shared-configuration).

### Custom installations and upgrades

For executables in custom locations, pass their absolute paths:

```bash
bash scripts/install-claude.sh --claude /path/to/claude --runtime /path/to/peerreview-mcp
```

The same flags work on all supported platforms. In Git Bash, Windows paths such as
`"C:/Program Files/Claude/claude.exe"` and `/c/Users/name/.local/bin/claude.exe` are accepted.
`--runtime` uses an existing PRA installation instead of installing another copy.
Omit it for the managed environment. Re-run the installer after pulling repository
updates to upgrade the runtime and refresh the cached plugin, then restart Claude.
The `bash scripts/install-local.sh claude` shortcut invokes this same installer
and forwards custom installer options.

The adapter also finds native CLI installations in `~/.local/bin` when that
directory is absent from PATH. `PEERREVIEW_CLAUDE_PATH` selects a custom executable.
An invalid override fails explicitly instead of silently using a different CLI.

For source development, after installing the MCP extra and making its command
available on PATH, load the repository directly:

```bash
claude --plugin-dir /absolute/path/to/PeerReviewAgents
```

Each review turn uses `claude -p` with tools disabled, safe mode enabled, no
session persistence, and schema-constrained JSON output. Use an up-to-date CLI
that supports these flags. The installer does not copy credentials from an IDE.

Uninstall the configured plugin:

```bash
claude plugin uninstall peer-review-agents@peer-review-agents-configured
claude plugin marketplace remove peer-review-agents-configured
```

The installer prints its data directory. Remove that directory to remove the
managed PRA environment and generated marketplace once the plugin is uninstalled.

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
      "args": [],
      "env": {
        "PEERREVIEW_MCP_INPUT_ROOTS": "/absolute/path/to/manuscripts",
        "PEERREVIEW_MCP_OUTPUT_ROOT": "/absolute/path/to/reports"
      }
    }
  }
}
```

The client can start and monitor reviews, list journals, and read completed artifacts. Its own subscription will run the panel only if PeerReviewAgents has a tested provider adapter for that client's noninteractive CLI.

The MCP server reads manuscripts only beneath `PEERREVIEW_MCP_INPUT_ROOTS` and
writes reports only beneath `PEERREVIEW_MCP_OUTPUT_ROOT`. The input root defaults
to the server's working directory and the output root defaults to its `reports/`
directory. Use the platform path separator to provide more than one input root.

## Data handling

- PDF conversion, orchestration, checkpoints, and report generation run locally.
- The selected coding agent receives the manuscript content needed for each model turn through its normal authenticated service.
- PeerReviewAgents does not add telemetry, remote storage, accounts, or billing.
- Live literature research is off by default in the MCP tool. Enabling it sends research queries to the configured literature services.
- API-backed providers remain available, but they are optional and require their normal environment variables.

## Troubleshooting

### The MCP server does not start

For Claude Code or Desktop, re-run the installer above and restart the app.
It prints the absolute runtime and Claude paths configured in the plugin.
For other clients, confirm the command is installed and visible to the agent process:

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
