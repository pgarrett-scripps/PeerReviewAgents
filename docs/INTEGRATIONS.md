# Integrations

There is one integration in 0.9: the `pra` plugin for Claude Code. It is also
the only way a review runs, whether you start it from the command line or from
inside a session, so there is nothing to wire up twice.

```
peerreview review     peerreviewagents/plugin/pra   peerreviewagents.plugin.server
  (the command)      →     (workflow, agents,     →   (23 MCP tools, the ledger,
                            skills, .mcp.json)         the run files)
```

The command launches a headless Claude Code session on the plugin. A session
you drive yourself loads the same plugin and the same server. Either way the
server owns the manuscript, the agent identities, the tool allowances, the
budgets and the files.

## Requirements

- The standalone Claude Code CLI, signed in to a subscription, with `claude` on
  `PATH`. Claude Desktop alone does not install it; see
  [the quickstart](https://code.claude.com/docs/en/quickstart).
- This package installed in any Python environment — a checkout or a wheel. The
  plugin ships inside it, and `peerreview review` starts the server with the
  interpreter running the command, passed in `PEERREVIEW_MCP_COMMAND`. A
  session you load by hand has nothing setting that variable, so there the
  server starts through the `peerreview-plugin-server` console script and that
  shell's PATH has to carry it.
- Poppler's `pdftotext` on `PATH`, which does the PDF text extraction:
  `sudo apt install poppler-utils`, `brew install poppler`, or `choco install
  poppler`.
- No API key. A run strips `ANTHROPIC_API_KEY` from the session's environment
  and never passes `--bare`, so it cannot silently fall back to one.

## From the command line

```bash
peerreview review manuscript.pdf run-output
```

Before spending anything, the starter runs a throwaway session and reads its
start-up event: the review only begins once that event shows the `pra` plugin
loaded, the `peerreview` server **connected**, and every tool the agents are
allowed to call present. What it saw is written to
`run-output/review/startup.json` either way, and a failed probe is a `blocked`
run with its reasons, not a traceback.

## Inside a Claude Code session

From a checkout:

```bash
claude --plugin-dir /absolute/path/to/PeerReviewAgents/peerreviewagents/plugin/pra
```

Or add the repository as a local marketplace and install it by name:

```bash
claude plugin marketplace add /absolute/path/to/PeerReviewAgents
claude plugin install pra@peer-review-agents-local
```

Then ask for one of its three skills:

| Skill | What it runs |
| --- | --- |
| `peer-review-manuscript` | a full review: core reviewers, specialists, editor |
| `check-draft` | the specialist tracks you name on a draft of your own |
| `follow-up` | what changed since a previous run of the same paper |

The agents may call only the server's tools — the allow list the starter passes
is `Workflow`, `Skill`, `TodoWrite` and the 23 `peerreview` tools, and nothing
else. No `Read`, `Write`, `Edit`, `Bash`, `WebSearch` or `WebFetch` reaches a
reviewer, which is why a quotation cannot come from anywhere but a source
block.

## Other MCP clients

The server is a plugin server. It takes `--plugin-root`, derives a per-run salt
for agent tokens, and expects the workflow to claim slots in order, so pointing
another client at it directly gives you the tools without the protocol that
makes them meaningful. There is no supported generic-client configuration in
0.9, and the standalone `peerreview-mcp` server that offered one was deleted
with the rest of the 0.8 surface.

If you want a review from another client, have it run `peerreview review` as a
subprocess and read the run folder. The command is the integration boundary.

## From Python: the supported surface

An application written in Python does not have to shell out. These names are
the supported surface, all importable from `peerreviewagents` itself. Nothing
else in the package is: if what you need is only reachable from
`peerreviewagents.plugin` or `peerreviewagents.engine`, treat it as a gap in
this list and say so, because those modules change without notice.

```python
from peerreviewagents import (
    review, review_main, report_main,      # run a review; the two commands
    read_run, write_reports,               # read and re-render a run folder
    import_paper, verify_workspace, export_draft,
    RUN_DIRECTORY, MANUSCRIPT_DIRECTORY,   # "review" and "manuscript"
    RUN_STATUSES, COMPLETE_STATUSES, FINDING_SCHEMA_VERSION,
)
```

The callables resolve lazily, so `import peerreviewagents` costs nothing and an
app that only reads finished runs never imports the launcher or the MCP SDK.
The constants are plain values from `peerreviewagents.names`, which imports
nothing at all.

### Wrapping the two commands

`review_main(argv, prog=...)` and `report_main(argv)` are `peerreview review`
and `peerreview report` as functions: they take the argument list, print the
JSON the command prints, and **return** the exit code instead of raising it.
`prog` is the name in the wrapper's own `--help`, so your command can call
itself what it is:

```python
import sys
from peerreviewagents import review_main

raise SystemExit(review_main(sys.argv[2:], prog="workbench review"))
```

That is the whole reason they exist. Copying `starter.main`, or reaching for
`starter.build_parser` and `starter.exit_code`, means your wrapper breaks when
an option is added; these two do not.

### Reading a run folder

`read_run(path)` answers the four questions an application has about a folder
on disk — did it finish, why did it stop, what did it find, what did it cost —
without opening a file yourself:

```python
from peerreviewagents import read_run

run = read_run("run-output")              # the run folder or its review/ folder
if run["blocked"]:
    print("stopped:", run["cause"], f"({run['stage']})")
for finding in run["body"]:
    print(finding["severity"], finding["title"], finding["quote"])
print(run["runtime"]["tokens_total"], "tokens",
      [(a["slot"], a["tokens"]) for a in run["agents"]])
```

It works on every kind of folder a review can leave behind, which is the point:

| Folder | What `read_run` gives you |
| --- | --- |
| complete | `status` `complete`, findings from `editor-package.json`, body and appendix |
| complete without findings | `status` `complete-without-findings`, `findings` empty, `cause` `None` |
| blocked by the server | the cause from `progress.json`, findings from the ledger |
| killed by the starter | the cause from `runtime.json` — token ceiling, timeout, rate limit, session error — even though `progress.json` still says `running` |
| start-up blocked | `stage` `startup`, `startup_problems`, and the identity the starter wrote |

Keys: `status`, `complete`, `blocked`, `cause`, `stage`, `findings`, `body`,
`appendix`, `findings_source`, `counts`, `quarantined`, `escalations`,
`escalation_rulings`, `summary`, `verdict`, `limitations`, `identity`,
`stages`, `runtime`, `agents`, `usage`, `reports`, `startup_problems`, `files`,
`run`, `review`.

Three rules it enforces so you do not have to:

- **One row per finding.** `findings.jsonl` is append-only, so a finding
  restated during the run is several rows. `read_run` keeps the latest row per
  `finding_uid`, in the order the findings were first seen, from the editor
  package when there is one and from the ledger when there is not —
  `findings_source` says which.
- **A folder that is not a run is an error, not an empty answer.** It raises
  `peerreviewagents.runs.NotARunFolder` (a `ValueError`) naming the files it
  looked for. A truncated file inside a real run is not an error: a process
  killed mid-write is exactly the case this function is for.
- **A run file that is a symlink is refused, by name.** A symlink in
  `editor-package.json`'s place is some other file's contents presented as this
  run's result.

`write_reports(run)` re-renders `REVIEW.md` and `REVIEW.txt` from a run folder
with no model call. The workspace path is optional: a run folder holds its own
manuscript, so pass one only when the manuscript lives somewhere else.

### Run statuses

`RUN_STATUSES` is `("running", "blocked", "complete",
"complete-without-findings")` and `COMPLETE_STATUSES` is the last two. A status
outside that tuple is a bug in this package, not something to handle. Compare
against these rather than against string literals, and use `RUN_DIRECTORY` and
`MANUSCRIPT_DIRECTORY` rather than writing `"review"` and `"manuscript"` into
your own paths.

## What 0.9 removed

| Gone | Where it still exists |
| --- | --- |
| `peerreview-mcp`, the standalone MCP server | the `v0.8.0` release |
| The Codex plugin and the Codex provider | the `v0.8.0` release |
| Factory Droid and Pi packages | the `v0.8.0` release |
| The desktop installers (`scripts/install-claude.*`) | the `v0.8.0` release |
| The local web UI (`peerreview serve`) | the `v0.8.0` release |
| The LangGraph panel | the `legacy-langgraph-v0.6.1` tag |

None of it was kept as a shim. A client built against 0.8's MCP tools or
`peerreviewagents.engine.api` has to move to `peerreview review` and the run
folder.

## Data handling

- PDF conversion, the ledger, the run files and the reports are local.
- The Claude Code session receives the manuscript text and page images it is
  asked for, through its normal authenticated subscription service.
- PeerReviewAgents adds no telemetry, no remote storage, no account and no
  billing of its own.
- Literature and identifier lookups are live HTTP requests to public scholarly
  services, bounded by a per-run external-request budget, and removed entirely
  from the `manuscript-only` arm. Every lookup is recorded with the identity it
  resolved.
- There is no API key and no API-backed provider: the review runs only on a
  signed-in `claude` CLI.

## Troubleshooting

### The probe says the plugin was not loaded

`claude --plugin-dir` has to point at `peerreviewagents/plugin/pra`, not at the
repository root. Check `run-output/review/startup.json`: it records the plugins,
MCP servers, agents and slash commands the session reported.

### The probe says the server did not connect

`peerreview review` launches the server with its own interpreter, so confirm
that interpreter can import it:

```bash
python -c "import peerreviewagents.plugin.server"
```

In a session you started yourself, the launcher is the
`peerreview-plugin-server` script instead: check that `which
peerreview-plugin-server` finds it.

A missing MCP SDK is the usual cause on an old environment. It is a dependency
in 0.9, so `pip install -e .` installs it; re-run that after upgrading.

### `claude` is missing or not signed in

```bash
claude --version
```

Install and authenticate it, then retry. A run with no `claude` on `PATH` is a
`blocked` run whose reason says so.

### The run is `blocked`

That is an outcome, not a crash. `runtime.json` holds the reason — a rate
limit, a breached token ceiling, the wall-clock limit, a missing stage — and
`peerreview report <run>` renders what was saved. Rate limits are recorded with
the time the session reported, so a blocked run says when it can be retried.

### A review stopped when I closed the terminal

The review is that process. It is not a background job and there is no server
holding it; re-run the command. Everything the server had already written
stays on disk, and `peerreview report` reads it.
