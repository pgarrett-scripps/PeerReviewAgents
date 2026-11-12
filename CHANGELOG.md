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

- `peerreviewagents/plugin/pra`, the plugin that now runs every review: a
  workflow that fans out N core reviewers and six specialist tracks and then an
  editor, agent files that pin a model and a turn limit per slot, and three
  skills (`peer-review-manuscript`, `check-draft`, `follow-up`). It ships
  inside the package, so an installed wheel carries the plugin it runs; the
  starter resolves it from the installed package rather than from a checkout,
  and `peerreview review` starts the server with its own interpreter through
  `PEERREVIEW_MCP_COMMAND`.
- One-time claim codes. `ingest` mints a code per slot and returns them to the
  recorder alone; an agent claims its slot with the code in its own prompt, a
  code works once, and a slot already held is not re-claimed. A retry gets a
  fresh code from `reissue_claim_code`, which only the recorder's run key
  opens. Before this, naming a slot was being it: a core reviewer could claim
  `recorder` and read every other reviewer's findings.
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
- `peerreviewagents.read_run(path)`, which reads any run folder: the status,
  the cause and the stage of a run that stopped, the findings de-duplicated to
  the latest row per `finding_uid` (from `editor-package.json` when the editor
  got that far, from the `findings.jsonl` ledger when it did not), body against
  appendix, the escalations and the editor's rulings, the manifest identity and
  source hash, the report paths and the runtime of each agent. Complete,
  complete without findings, blocked, killed by the starter on a ceiling, a
  timeout, a rate limit or a session error, and start-up blocked with only
  `startup.json` and `runtime.json` are all folders it reads. It refuses a run
  file that is a symlink, by name, and raises `runs.NotARunFolder` for a folder
  that is not a run. The first application to show PRA runs had to reach into
  private names and parse these files by hand. It returns the same 32 keys for
  every folder, among them `publishable` — status `complete`, or
  `complete-without-findings` with nothing quarantined, the findings from a
  readable editor package, a guard verdict on the record and nothing in
  `problems` — which is the flag to gate publishing on, with
  `not_publishable_reasons` naming every reason when it is false, and
  `problems`, which names every file or line that could not be read. A finding
  the guard quarantined does not hold a review back: the guard threw out a
  quote it could not anchor, and the rest of the review is whole. A review with
  no findings in it is publishable when the reviewers simply raised nothing, and
  not publishable when the guard emptied it, where the reason says how many were
  held back: `counts['quarantined']` is what tells the two apart.
  `identity['provider']`, `identity['code_hashes']` (the manifest's digest per
  engine and plugin file) and `startup` (the harness's own start-up event and
  the probe's problems) are the three facts the first integration was still
  reading out of the folder by hand.
- One agent is one row in `agents`. The stream names a task and the protocol
  names a slot, and for the recorder those are different words — `ingest` and
  `write review` against `recorder` — so one agent appeared as three rows with
  its spend under names the protocol does not use. The tokens and session calls
  of a slot's tasks are now added to that slot, `labels` says which tasks they
  were, and a stream row carrying nothing but a label is the harness's
  placeholder for an agent it never ran and is left out. `runtime.json` keeps
  its raw record; this is the reader's reading of it.
- `peerreviewagents.runs.run_status`, the one rule that decides a run's status,
  used by `read_run` and by the report writer, so a folder cannot read one way
  through the library and another way in its own `REVIEW.md`. A limit that
  fired after the review was written is a `note`, not a `cause`, and the run is
  complete.
- `review_main(argv, prog=...)` and `report_main(argv, prog=...)`: the two commands as
  functions — argument list in, the command's JSON on stdout, the exit code
  returned rather than raised — so a wrapper does not copy `starter.main` or
  import `starter.build_parser`, `starter.exit_code` or `cli.main.report`, and
  names itself in its own `--help`.
- `plugin_directory()`, `claude_code_version()`, `review_parser(prog=...)`,
  `report_parser(prog=...)` and `CLI_COMMANDS` on the top level: the plugin that
  ships inside the package, the installed Claude Code probe (`None`, never an
  exception, when it is not on this PATH), the real parsers behind the two
  commands so a wrapper can diff its documented options against them instead of
  against a copy, and the two command names. These were `plugin.starter.plugin_dir`,
  `plugin.starter.claude_version`, `plugin.starter.build_parser` and
  `cli.main.COMMANDS`/`cli.main.report_parser`, which an integration was still
  importing from private modules.
- `locate_run(path)` and `NotARunFolder` on the top level: the run-folder
  resolution `read_run` and `write_reports` do first — `(run folder, review
  folder)` from either spelling, a symlinked `review/` refused by name — and
  the error all three raise, for an app that lists a directory of runs before
  reading any of them. They were `runs.locate` and `runs.NotARunFolder`.
- `peerreviewagents.workspace`, the workspace tools an application needs to
  write files the engine reads back and verifies: `digest`, `read_json`,
  `write_json`, `normalize_review`, `save_review`, `words` and `text_of`. Each
  is the engine's own object under a name that says what it is, so there is no
  second digest, second JSON writer or second review normalisation to disagree
  with the engine about what a workspace holds. The first application to keep
  its own revisions beside a workspace was importing all seven from
  `engine.storage`, `engine.annotations` and `engine.ingest`. The
  whole-workspace operations stay on the top level: `import_paper`,
  `verify_workspace`, `export_draft`.
- `peerreviewagents.names`, a module that imports nothing, holding the names an
  integration needs as constants: `RUN_DIRECTORY`, `MANUSCRIPT_DIRECTORY`,
  `RUN_STATUSES`, `COMPLETE_STATUSES`, `CLI_COMMANDS` and
  `FINDING_SCHEMA_VERSION`. They were
  literals inside `plugin.server`, which an app had to import a launcher and an
  MCP SDK to read. The server now imports them from there, so there is one
  definition and nothing to drift.

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
- A `finding_uid` is the slot plus the quotation and the evidence handles, not
  the model-written title: a reviewer restating the same issue in better words
  filed one finding, not two, and two reviewers who agree stay two rows for the
  editor to merge. Storage merges nothing, and `reviewers_raising` is counted
  from the editor's own duplicate rulings.
- A reviewer's ledger shows its own stage only. The full ledger named every
  slot in the run, so a core reviewer could read that six specialists would
  check its work — which is one of the things the arms compare.
- The tool allowance belongs to the slot: a retry continues the first
  attempt's count instead of buying 30 more calls.
- The supported library surface is `review`, `review_main`, `report_main`,
  `read_run`, `write_reports`, `import_paper`, `verify_workspace`,
  `export_draft` and the five constants, all importable from
  `peerreviewagents` and documented in `README.md` and
  `docs/INTEGRATIONS.md`. The callables still resolve lazily, so reading a run
  imports neither the launcher nor the MCP SDK.
- `write_reports(run)` takes a run folder on its own: the workspace argument is
  needed only when the manuscript lives outside the run.
- `export_draft` no longer emits `component_decisions` or `epistemic_audit`.
  They were the debate's keys, and 0.9 has no debate, so every export carried
  two empty fields a consumer had to interpret.

### Fixed

- `read_run` returns a reading for a damaged folder instead of raising, which
  is the state a killed run is normally in: a truncated `runtime.json`, a
  half-written last line in a usage receipt, a status file holding a list, a
  ledger with bytes that are not UTF-8. Each one is named in `problems` and
  everything else still comes back; `usage['unknown']` marks observed tokens as
  a lower bound when a receipt could not be read in full. `peerreview report`
  printed a traceback for exactly the runs whose cause one wants to read.
- `read_run` and `REVIEW.md` agreed on `running` and `blocked` but not on a run
  whose review was written before a rate limit fired: one read `progress.json`
  first and the other `runtime.json`. Both now call `run_status`.
- `report_main` returns `1` with the message on stderr for a folder that is not
  a run and for a run file that is a symlink, as documented, instead of raising
  `SystemExit` with a string or leaking a `ValueError`. `review` raises
  `ValueError` for an unusable argument — an unknown arm, a missing manuscript,
  a plugin directory with no plugin — before writing anything, and
  `review_main` turns it into the message and the code `2`.
- A status word `read_run` does not know is `unknown` with a line in
  `problems`, rather than passed through to a consumer as if it were a status.
- A symlinked `review/` directory and a symlinked `usage.jsonl` were followed
  while a symlinked `audit_*.md` was dropped in silence. Every symlink anywhere
  under the review folder is now refused by name.
- A `finding_uid` the editor placed in both the body and the appendix was
  counted twice, in `findings` and again in `escalations`. It is one finding,
  in `body`.
- A corrupt `editor-package.json` on a complete run fell back to the ledger in
  silence and read as a whole review. The fallback is reported in `problems`
  and the run is not `publishable`.
- `write_identity` recorded `specialists: []` for a start-up-blocked
  `full-two-track` run, so the folder said the plan was something it was not.
  It records the arm's default tracks when none were named.
- A retry in a folder that already holds a finished or killed run read as the
  old run's block until the new one ended. The starter supersedes the previous
  `runtime.json` and `progress.json` at the start, keeping the old state under
  `superseded`.
- A finding submitted after the stage closed, after the editor ruled, or after
  the run was blocked is refused rather than stored and then dropped from the
  package.
- `mark_blocked` needs the recorder's run key, and never flips a finished run
  to blocked. `write_review` computes the status and the guard status it
  reports instead of hard-coding `clean` and calling a run with an appendix
  finding `complete-without-findings`.
- An out folder that already holds a different manuscript, or a different arm,
  is refused at ingest instead of reviewing the first PDF under the second
  plan.
- A workflow that ends without writing the review or marking the run blocked
  leaves the run `blocked` with a reason, not `running`. A missing `claude`
  binary says so rather than blaming the plugin.
- `protocol.json` and `manifest.json` are written at ingest, before any agent
  reports, instead of only by `write_review` and `mark_blocked`. A run killed
  on a token ceiling used to leave a folder that could not say which paper it
  held; and when the probe never launched a session, the starter writes a thin
  pair itself — the arm, the PDF and its hash — without ever overwriting the
  server's own.
- `peerreview report` renders a blocked, killed or start-up-blocked run and
  prints its cause. It refused anything without a `protocol.json` with "not a
  review run folder", which is the message for exactly the runs whose cause one
  wants to read.
- The starter drains the session's stderr on its own thread, so a chatty
  session cannot deadlock the run, and it drops `ANTHROPIC_AUTH_TOKEN` as well
  as `ANTHROPIC_API_KEY` from the child environment. The rate-limit ceiling
  reads the real `rate_limit_event` shape, and provenance records the version
  the `claude` binary reports.
- Windows: every file the engine, the server and the starter open is opened as
  UTF-8, and the starter reads the session's pipes with reader threads instead
  of `select`, which does not take a pipe there.

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
