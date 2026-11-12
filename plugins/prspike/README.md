# prspike: build step 1 of 0.9

One question: does a plugin with a workflow script, a tool-restricted subagent
and a Python MCP server run in a headless Claude Code session on the
subscription login? It does. This folder is the proof, not the 0.9 engine, and
nothing in `peerreviewagents/engine` was changed to make it work.

Run it:

```
plugins/prspike/spike.sh [manuscript.pdf] [run-folder]
```

With no arguments it reviews `tests/fixtures/single-column.pdf` into
`.spike-run`. It needs `jq`, a Poppler install and `.venv` (`uv sync --extra
dev`), and it never passes `--bare`.

## The parts

| Part | File | What it proves |
| --- | --- | --- |
| MCP server | `peerreviewagents/plugin/spike_server.py` | `ingest`, `read_blocks`, `submit_finding` over stdio, no model calls, reusing the engine's import, workspace check and block payload |
| Subagent | `agents/core-reviewer.md` | `tools:` holding only the three MCP tools, `maxTurns: 8`, `background: false` |
| Workflow | `workflows/spike.js` | the stages are a script: ingest, review, check |
| Skill | `skills/peer-review-spike/SKILL.md` | a one-line start for the workflow |
| Starter | `spike.sh` | the headless launch, the start-up assertion and the on-disk check |

## The run, 2026-11-11

Claude Code 2.1.322. Route: the plugin workflow script. The Stop-hook fallback
was not needed and was not built.

```
claude -p "Use the peer-review-spike skill from the prspike plugin. Run the spike on the manuscript at <pdf> and write the run folder to <out>." \
  --plugin-dir plugins/prspike \
  --permission-mode dontAsk \
  --allowed-tools "Workflow,Skill,TodoWrite,mcp__plugin_prspike_peerreview__ingest,mcp__plugin_prspike_peerreview__read_blocks,mcp__plugin_prspike_peerreview__submit_finding" \
  --output-format stream-json --verbose --forward-subagent-text
```

The `system/init` event listed the plugin at its path, the agent
`prspike:core-reviewer`, `plugin:prspike:peerreview=connected`, and the three
tools under the name the docs give: `mcp__plugin_prspike_peerreview__<tool>`.
The two agents took 20.1 s, 8 tool calls and 20,483 tokens between them; the
main loop added ~121k, nearly all of it cached context. One finding was stored
with a receipt, and `findings.jsonl` held exactly one line.

## What the spike changes in the 0.9 plan

1. **The run is a background task.** Under `-p` the Workflow tool returns at
   once and the CLI emits a `result` event saying the workflow is still
   running, then a second `result` after `system/task_notification`. A starter
   that reads the first `result` reports a finished review that has not
   happened. Read the last one, or wait for `task_notification`.
2. **Per-agent token use is visible after all.** The spec says token ceilings
   are advisory because the server cannot see inside a subagent. True of the
   server, false of the starter: `system/task_progress` carries a
   `workflow_progress` array with each agent's `tokens`, `toolCalls`,
   `durationMs`, `state`, `attempt`, `agentType` and `model`. Budgets and the
   per-agent model in provenance can be enforced and recorded there instead of
   being taken from the agent's own word.
3. **Pin the model in each agent file.** Both agents inherited the launching
   session's model (`claude-fable-5-2` here). A study arm that does not set
   `model:` is not reproducible.
4. **A workflow script cannot check the ledger.** It has no filesystem access,
   so "check a finding was stored" inside the script can only mean checking the
   receipt the agent reports. The spec's `list_findings` is what a verifier
   stage needs; until then the starter does the authoritative check on disk, as
   `spike.sh` does.
5. **One server process serves every agent.** The reviewer called `ingest`
   again and attached to the run the first agent created, so server-owned run
   state across agents (tokens, the stage ledger, status) works as the spec
   assumes.
6. **`StructuredOutput` needs no allow rule.** The schema-forced output tool
   was called by an agent whose `tools:` list and the session's allow list both
   omit it. `Workflow` and `Skill` do need to be allowed under `dontAsk`.
