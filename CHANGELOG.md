# Changelog

Notable changes per release. Earlier releases are described in their GitHub
release notes and are reachable by tag; this file starts at 0.9.0, the release
that made the plugin the only way to run a review.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
the project uses [semantic versioning](https://semver.org/spec/v2.0.0.html).

## [0.9.0] — unreleased

The review is a Claude Code plugin. The Python orchestration that ran one
through 0.8.x is deleted, not deprecated: 90 files and about 14,800 lines went,
including 32 test modules, and nothing was left behind as a shim.

### Added

- `plugins/pra`, the plugin that now runs every review: a workflow that fans
  out N core reviewers and six specialist tracks and then an editor, agent
  files that pin a model and a turn limit per slot, and three skills
  (`peer-review-manuscript`, `check-draft`, `follow-up`).
- `peerreviewagents.plugin.server`, the MCP server the agents work through. It
  owns the manuscript, the agent identities and their tokens, the per-track
  tool allow lists, the call and external-request budgets, the finding guard
  and the stage ledger, and it writes the run files. An agent's own account of
  what it did is never taken on trust.
- `peerreviewagents.plugin.starter`, which probes a throwaway session before
  spending a run, launches the headless session, enforces per-agent and
  per-run token ceilings from the session's own stream, and records models,
  wall time and rate limits in `runtime.json`.
- `peerreviewagents.engine.usage`, holding the usage and runtime accounting the
  report writer needs (`observed_usage`, `runtime_usage`, `agent_rows`), which
  used to live inside the deleted `ensemble` module.
- `review-finding-v3`: a `finding_uid` stable across re-reviews of the same
  paper, a `track`, a specialist `escalate` flag, and a `basis` the server
  assigns rather than the agent asserting it.

### Changed

- `peerreview review <pdf> <run folder> [--reviewers N] [--arm NAME]` is the
  review, and a bare manuscript path means the same thing. `--arm` is an
  ordinary option now, not the switch that chose between two engines.
- `peerreview --help` has two commands: `review` and `report`. `report`
  re-renders a finished or blocked run offline.
- `REVIEW.md`'s header reports the run's real totals — tokens, wall time, agent
  count, server and session tool calls, the Claude Code version — and a
  per-agent table of model, calls, tokens and state. Through the first 0.9 runs
  it said "Model calls 0" and "Observed tokens 0", because the recorder renders
  the report from inside the session, before the starter has written any
  accounting; the starter now re-renders once the accounting exists.
- A `computed` basis counts a `calculate` receipt whose inputs appear anywhere
  in the blocks the finding cites, not only inside the quoted sentence. A
  reviewer quoting one sentence rarely quotes every number it combined, and
  under the old rule a whole live run earned no `computed` findings from 17
  `calculate` calls.
- `--arm` names a *workflow* arm: which agents run, not which policy or model
  they run under. `docs/study-arms.md` is rewritten around the six arms.
- The MCP SDK is a dependency rather than the `mcp` extra, because the plugin's
  server is the review and one cannot start without it.
- Arms: `full-two-track` (the default), `reviewers-only`, `single`,
  `single-revision`, `manuscript-only`, and `draft-check` for a draft of your
  own.

### Removed

- The Python orchestration: `ensemble`, `ensemble_debate`, `ensemble_references`,
  `reviewer`, `discovery`, `baseline`, `model_runtime`, `models`, `api`,
  `config`, `bundle`, the three debate modules, `review_policy`,
  `review_schema`, `investigation_policy`, `evidence_handles`,
  `synthesis_validation`, `scaffold_checks`, `manuscript_tools`,
  `focused_investigation`, and the `python -m peerreviewagents.engine
  import|review|report` entry point.
- `run_review`, `ReviewRequest`, `plan_review`, `review_options`,
  `run_review_options`, `capabilities`, `get_config` and the preset machinery.
  `peerreviewagents.engine.api` was the versioned integration boundary and is
  gone; the importable surface is now `review`, `import_paper` and
  `write_reports`.
- The six review modes (`ensemble`, `panel`, `independent`, `plain`,
  `workspace`, `self-revision`) and the three policies (`agentic-v1`,
  `standard-v1`, `workspace-agentic-v1`), with `--mode`, `--policy`,
  `--preset`, `--provider`, `--model` and `--token-budget`.
- The Codex provider, every Codex model and the Codex plugin. A review runs on
  a Claude Code subscription login only.
- The local web UI (`peerreview serve`, `peerreviewagents.web`) and
  `review_service.py`. Re-pointing a browser UI at the plugin is new work, not
  a port.
- The standalone MCP server (`peerreviewagents.mcp`, `bin/peerreview-mcp`, the
  `peerreview-mcp` console script and the repository `.mcp.json` that failed to
  connect from a fresh clone). The plugin ships its own server.
- The installers (`scripts/install-claude.*`, `scripts/install-local.sh`), the
  Pi integration, the old `peer-review-agents` plugin and the Codex marketplace
  manifest. The plugin is loaded from the checkout, with `claude --plugin-dir`
  or the repository's own local marketplace.
- The CLI commands `import`, `verify`, `add-review`, `result-map`, `checks`,
  `investigate` and `serve`.
- `fastapi`, `uvicorn`, `python-multipart` and the `httpx` dev dependency.
- Label-less placeholder rows in `runtime.json`: a stream row with no agent
  label was being written as an agent, so a run showed five empty slots.
