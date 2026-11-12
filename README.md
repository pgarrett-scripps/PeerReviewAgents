# PeerReviewAgents

[![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.21781895.svg)](https://doi.org/10.5281/zenodo.21781895)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

Evidence-linked peer review of a manuscript PDF, on a Claude Code
subscription. One command imports the PDF into immutable source blocks and runs
the `pra` plugin over them: several core reviewers and six specialist tracks
work the paper in parallel and in isolation, then an editor pools what they
found into one package with a decision and a reason for every finding. There is
no API key — the models run on your signed-in Claude Code login.

Every finding carries the block it is anchored to and the exact quotation it
rests on. A finding whose quote does not match the manuscript does not ship.

```bash
peerreview review manuscript.pdf manuscript-review
```

That writes `manuscript-review/`, holding the imported manuscript, the run, and
`REVIEW.md` and `REVIEW.txt`.

## 0.9.0: the plugin is the only way to run a review

0.9.0 deleted the Python orchestration. Through 0.8.x a review was a Python
program that called a model CLI per reviewer: `ensemble`, `reviewer`,
`model_runtime`, the audited debate, six modes, three policies, `ReviewRequest`
and `run_review`. All of it is gone, along with the Codex provider, the web UI
and the standalone MCP server.

What runs a review now is a Claude Code plugin — `peerreviewagents/plugin/pra`,
shipped inside the package, so an installed wheel carries it — whose workflow
fans out the agents and whose MCP server owns the manuscript, the agent
identities, the tool allowances, the budgets, the stage ledger and the run
files. No agent's account of its own work is taken on trust: it can only ask
the server for text and hand it findings, and the server decides what a finding
rests on.

This is a break, not a deprecation. There is no shim and no legacy path. If you
need the 0.8 engine it is frozen at the `v0.8.0` release, where it still works
unchanged; the LangGraph panel that preceded it is frozen at
`legacy-langgraph-v0.6.1`.

## Install

Install it anywhere a Python environment runs. `peerreview review` starts the
plugin's MCP server with the very interpreter running the command — it passes
it in `PEERREVIEW_MCP_COMMAND` — so neither the venv's name nor its place
matters, and a plain `pip install peerreviewagents` can run a review with no
checkout at all.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .
```

Or with [uv](https://docs.astral.sh/uv/), which is what CI uses:

```bash
uv venv && source .venv/bin/activate
uv pip install -e .
```

Dependencies are `rustypaper` (PDF → source blocks), Pillow, and the MCP SDK.
The SDK is a dependency rather than an extra in 0.9: the plugin's server *is*
the review, so a review cannot start without it. No OCR, no provider SDK, and
no paid service beyond the subscription you already have.

PDF text extraction is Poppler's `pdftotext`, which is not a Python package:

```bash
sudo apt install poppler-utils     # Debian, Ubuntu
brew install poppler               # macOS
choco install poppler              # Windows
```

Then install and sign in to the standalone Claude Code CLI, because that is
what a run launches. Claude Desktop alone does not install it; get it from
[the Claude Code quickstart](https://code.claude.com/docs/en/quickstart) and
make sure `claude` is on your `PATH`. A run never passes `--bare`, so it
cannot fall back to an API key: `ANTHROPIC_API_KEY` is removed from the
session's environment.

How much subscription usage a run may spend is your own account setting. The
run records every token the session reports in `runtime.json` and stops itself
at its own ceilings (500,000 tokens per agent, 6,000,000 per run, 90 minutes
wall clock, all overridable).

## One review

```bash
# The whole thing: import, review, report.
peerreview review manuscript.pdf run-output

# A bare path means the same review.
peerreview manuscript.pdf run-output

# Five core reviewers instead of three.
peerreview review manuscript.pdf run-output --reviewers 5

# Bound what published work is eligible (defaults to today).
peerreview review manuscript.pdf run-output --literature-cutoff 2026-01-05

# Re-render the reports of a finished run. No model call.
peerreview report run-output
```

There are two commands and no third. `review` is the only way to run a review;
`report` only re-reads what a run already wrote. `peerreview review -h` lists
the review's options:

| Flag | Default | What it changes |
| --- | --- | --- |
| `--reviewers` | `3` | Core reviewers, 1 to 8. They never see each other's findings |
| `--arm` | `full-two-track` | Which workflow runs; see [`docs/study-arms.md`](docs/study-arms.md) |
| `--track` | all six | Repeatable; limits the specialist tracks (used by `draft-check`) |
| `--literature-cutoff` | today | Latest eligible publication date |
| `--per-agent-tokens` | `500000` | Per-agent token ceiling, enforced from the session stream |
| `--per-run-tokens` | `6000000` | Per-run token ceiling |
| `--timeout` | `5400` | Wall-clock limit in seconds |
| `--plugin-dir` | the checkout's | Override the folder the plugin is loaded from |

Set `PEERREVIEW_CLAUDE_VERSION` to have the run record which Claude Code
version produced it; nothing else in the run depends on it.

### What a run writes

```
<run>/manuscript/             the immutable import: original.pdf, paper.json
                              source blocks, page images, the manifest
<run>/review/REVIEW.md        the review, and REVIEW.txt beside it
<run>/review/editor-package.json   the editor's decisions, one per finding
<run>/review/findings.jsonl   every stored finding, with its basis
<run>/review/reviewer-<n>/    each core reviewer's own package view
<run>/review/audit_<track>.md the specialist audits
<run>/review/protocol.json    arm, slots, models, versions, provenance hashes
<run>/review/manifest.json    the paper's identity and its source hash
<run>/review/progress.json    stages and status
<run>/review/runtime.json     tokens and models per agent, wall time, limits
<run>/review/usage/<slot>/    the session stream's own usage records
```

`protocol.json` and `manifest.json` are written when the manuscript is
ingested, before any agent reports, so a run that stopped halfway still says
which paper it was, which arm it ran and what the source hashed to. A run whose
probe never launched a session gets a thin pair written by the starter instead,
which never overwrites the server's own.

`peerreview report` works on a blocked or stopped run too, and reports what was
saved rather than failing. A rate limit, a breached budget or a missing stage
leaves the run `blocked`: that is an outcome recorded in `runtime.json` and
`progress.json`, not an error to paper over.

### What the review guarantees

- **Anchored or quarantined.** A finding's quotation must match a source block
  exactly. One that does not is quarantined by the server, not reported.
- **A basis the agent cannot assert.** The server labels each finding
  `computed` when a `calculate` receipt covers the numbers the cited blocks
  print, `two-quotes` when a second anchored quotation corroborates it, and
  `judgement` otherwise.
- **Isolation.** Core reviewers never see each other's findings and are not
  told the specialists exist. The editor sees the pool in an order seeded by
  the run salt, so slot order carries no weight.
- **Tools per track.** Every slot has an allow list. A citations specialist
  asking for figure pages is refused.
- **One decision per finding.** The editor rules on each, merges duplicates by
  `finding_uid`, puts at most eight in the body, and must rule by name on every
  specialist escalation. Leaving a `computed` finding out of the body needs an
  anchored rebuttal quotation.

[`peerreviewagents/plugin/pra/README.md`](peerreviewagents/plugin/pra/README.md)
documents the plugin itself: the agents, the workflow, the 23 server tools and
the arms.

## Manuscript ingest

PDFs are converted to source blocks by [rustypaper][rustypaper], which keeps
headings, tables and display mathematics, and reads a two-column page in
reading order. It is a compiled Rust extension shipped as a per-platform wheel,
and a required dependency: `pip install -e .` pulls it in.

**There is no fallback, on purpose.** An earlier version fell back to `pypdf`'s
flat text layer. On one real submission that fused 2% of all words into runs
like `comparableefficacyatlowerdoseusingonlycausallyavailableinformation`, lost
about a sixth of the content, and flattened every heading and table into prose;
rustypaper read the same file with 3 fused tokens instead of 235. A reviewer
given the first version reviews a document the authors did not write, and a
silent fallback arranges for that to happen on exactly the runs nobody is
watching. A missing or failing converter is an error.

Every import records how the manuscript was read — converter and version,
block and page counts, and the manifest hash the review is anchored to. Publish
it. A reader checking a quoted sentence against the PDF needs to know which
conversion was reviewed.

Publisher JATS XML is preferred when it exists: it gives real text and parsed
references, where PDF-only references stay provisional. Source page images are
attached so a reviewer can inspect a figure.

**Convert here, not before.** Handing the review a Markdown file you converted
yourself looks equivalent and is not: the run records which converter read the
manuscript, and a conversion done elsewhere would be recorded as though this
one did it. Give it the PDF.

[rustypaper]: https://github.com/pgarrett-scripps/rustypaper

## Inside Claude Code

The same plugin is what you load interactively. From a checkout:

```bash
claude --plugin-dir /absolute/path/to/PeerReviewAgents/peerreviewagents/plugin/pra
```

Or add the repository as a marketplace and install it by name:

```bash
claude plugin marketplace add /absolute/path/to/PeerReviewAgents
claude plugin install pra@peer-review-agents-local
```

Loaded by hand like this, nothing sets `PEERREVIEW_MCP_COMMAND`, so the server
starts through the `peerreview-plugin-server` console script instead: install
this package in the environment that shell uses, or the plugin loads with its
tools absent. `peerreview review` needs nothing of the sort.

It carries three skills: `peer-review-manuscript` for a full review,
`check-draft` for a draft of your own, and `follow-up` for what changed since
the last run. Each one runs the same server and the same guarantees as
`peerreview review`.

## As a library

```python
from peerreviewagents import review, read_run

run = review("manuscript.pdf", "run-output", reviewers=3)
print(run["status"], run["tokens_total"], run["report"]["markdown"])

summary = read_run("run-output")          # any run folder, finished or not
print(summary["status"], summary["cause"], len(summary["body"]))
```

Everything an application is meant to use is importable from the top level and
nothing it needs is private. The callables resolve lazily, so
`import peerreviewagents` stays cheap and reading a run never imports the
launcher.

| Name | What it is |
| --- | --- |
| `review(pdf, out, ...)` | the whole review; exactly what `peerreview review` runs |
| `review_main(argv, prog=...)` | that command as a function: JSON on stdout, exit code returned |
| `report_main(argv)` | `peerreview report` as a function, same contract |
| `read_run(path)` | read any run folder: status, cause, findings, costs |
| `locate_run(path)`, `NotARunFolder` | `(run folder, review folder)` from either spelling, and the error both it and `read_run` raise for a folder that is not a run |
| `write_reports(run, workspace=None, output_dir=None)` | re-render `REVIEW.md` and `REVIEW.txt`, offline |
| `import_paper(pdf, workspace)` | import a PDF into a manuscript workspace, no model call |
| `verify_workspace(workspace)` | re-check an import against its own manifest, returns the paper |
| `export_draft(paper, review, decisions)` | apply the accepted anchored edits to a copy of the blocks |
| `peerreviewagents.workspace` | the workspace tools an app needs to write files the engine reads back: `digest`, `read_json`, `write_json`, `normalize_review`, `save_review`, `words`, `text_of` |
| `plugin_directory()` | the `pra` plugin that ships inside this package, for `claude --plugin-dir` |
| `claude_code_version()` | what `claude --version` prints, or `None` when Claude Code is not on this PATH |
| `review_parser(prog=...)`, `report_parser(prog=...)` | the real parsers behind the two commands, for checking a wrapper's own options against them |
| `RUN_DIRECTORY`, `MANUSCRIPT_DIRECTORY` | `"review"` and `"manuscript"`, the two folders in a run |
| `RUN_STATUSES`, `COMPLETE_STATUSES` | the statuses a run can record, and the two that mean it finished |
| `CLI_COMMANDS` | `("review", "report")`, every command `peerreview` has |
| `FINDING_SCHEMA_VERSION` | the schema every stored finding is stamped with |

`review` returns the run's summary and prints nothing: `status`,
`blocked_reason`, `stage`, the run and review folders, the report paths, the
arm, the wall time, the total and per-agent tokens, the models each slot ran
on, and the finding counts the server recorded. A `blocked` run returns the
same shape with the reason filled in: there is no exception for a review that
did not work and no stdout to parse. An argument that cannot be used at all —
an unknown arm, a manuscript that is not there, a plugin directory that holds
no plugin — raises `ValueError` before anything is written, and nothing else is
raised. `peerreviewagents.plugin.starter.exit_code(summary)` is what
the command turns it into — `0` reviewed, `2` never started, `1` otherwise, and
`review_main` returns it for you.

`plugin_directory()` and `claude_code_version()` are the two preconditions a
wrapper checks before offering a review: the plugin that ships in this package,
and the Claude Code the review runs on (`None` when it is not installed, not an
exception). `CLI_COMMANDS`, `review_parser` and `report_parser` are for checking
a wrapper against the commands rather than running one: compare your own
documented options against the real parsers, and an option added here shows up
as a difference instead of as an option your wrapper silently drops.

### Reading a run folder

`read_run` is for the other side of a review: a folder on disk that something
else produced. It works on every kind of folder a review can leave behind —
complete, complete without findings, blocked by the server, killed by the
starter on a token ceiling, a wall-clock limit, a rate limit or a session
error, and stopped by a start-up probe that never launched a session, which
leaves only `startup.json` and `runtime.json`. It takes the run folder or the
`review/` folder inside it, raises `peerreviewagents.runs.NotARunFolder` for a
folder that is neither, and refuses a run file that is a symlink, by name.

It returns a dict with the same keys for every folder, in every state. All of
them:

| Key | What it holds |
| --- | --- |
| `run`, `review` | the run folder and the `review/` folder inside it, as strings |
| `status` | one of `RUN_STATUSES`, or `unknown` for a status file that was never written or holds a word this version does not know |
| `complete`, `blocked` | the two questions asked of a status, already answered |
| `publishable`, `not_publishable_reasons` | whether the whole review is here to show, and why not when it is not (see below) |
| `cause`, `note` | why a run that is not complete stopped, and a limit that fired after the review was written |
| `stage` | `startup` when no session ever ran and no subscription usage was spent, `session` once one did |
| `findings`, `body`, `appendix` | the findings, de-duplicated per `finding_uid`, split the way the editor placed them |
| `findings_source` | `editor-package`, `ledger` or `none` |
| `counts` | `findings`, `body`, `appendix`, `quarantined`, and the server's own `findings_delivered`/`findings_submitted` |
| `quarantined`, `guard_status` | what the guard threw out, and `clean` or `quarantined` |
| `escalations`, `escalation_rulings` | the specialist findings that asked for a ruling, and the rulings given |
| `summary`, `verdict`, `limitations` | the editor's own three, or `None`/`[]` |
| `identity` | paper id, source hash, PDF path, arm, reviewers, specialist tracks, versions, `provider`, and `code_hashes` — the manifest's digest of each engine and plugin file that served the run |
| `stages` | the stages the run recorded, or `None` |
| `runtime` | wall time, total tokens, rate limits, both token ceilings, the Claude Code version |
| `agents` | one row per agent slot: role, track, model, tool calls, tokens, state |
| `usage` | observed subscription tokens from the usage receipts, with `unknown` set if any receipt was unreadable |
| `reports` | absolute paths to what exists — `REVIEW.md`, `REVIEW.txt`, `editor-package.json`, and `audits` keyed by track |
| `startup` | the harness's own start-up event as `probe` (`None` when no probe was written or it holds the wrong shape) and the probe's own `problems` |
| `startup_problems` | what the start-up probe refused to launch over |
| `problems` | every file that could not be read in full, named |
| `files` | the run files that exist, by name |

Findings come from `editor-package.json` when the editor got that far and from
the `findings.jsonl` ledger when it did not, de-duplicated to the latest row
per `finding_uid` either way: the ledger is append-only, so a finding restated
during the run is one finding, not three. A uid the editor placed in both lists
is in `body` only, so each finding is counted once.

**The status rule.** One function decides it — `peerreviewagents.runs.run_status`
— and `REVIEW.md` is rendered from the same answer, so a folder never reads one
way through the library and another way in its own report. A status recorded in
`progress.json` that means finished wins over a later block in `runtime.json`:
the starter records a rate limit or a token ceiling that fired *after*
`write_review`, and a review that was written is written. That late limit
becomes `note` rather than `cause`. Otherwise `runtime.json` decides, then
`progress.json`, then a run with a cause but no status is `blocked`, and
anything else is `unknown`. `complete-without-findings` means the review was
written with no findings in it, which happens two ways: the reviewers raised
nothing, or the guard quarantined everything they raised. `counts['quarantined']`
is what tells them apart.

**`publishable`** is the one flag to gate publishing on. It is true when the
status is `complete` or `complete-without-findings`, the findings came from a
readable `editor-package.json`, the guard recorded a verdict, and `problems` is
empty. When it is false, `not_publishable_reasons` names every reason in words,
so the flag is never false with nothing said.

A quarantined finding is not one of those reasons. The guard exists to throw out
a finding whose quote it could not match to the block it names; a guard that did
that has done its job, and the review that is left is publishable. `quarantined`
says what was thrown out, and whether that mattered is a judgement for a person.

**A review with no findings in it.** `complete-without-findings` with nothing
quarantined is publishable: the reviewers read the manuscript, raised nothing
make-or-break, and the editor wrote a package saying so, which is a review. The
same status with findings quarantined is not: what the reviewers raised is not
in the review, so there is nothing to publish, and the reason says how many were
held back.

**Damaged folders are read, not raised on.** A truncated `runtime.json`, a
half-written last line in a usage receipt, a ledger with bytes that are not
UTF-8, a status file holding a list: the normal state of a run somebody killed.
Each unreadable file or skipped line is named in `problems` and everything else
is still returned, because the reason a run stopped is exactly what you need
when it stopped badly. A reading with problems is a reading; just do not publish
it or quote a number out of it without saying so.

That is the whole supported surface. `peerreviewagents.engine` is not an
integration boundary: it holds the manuscript workspace, the investigation
tools and the report writer the plugin server calls, and
[`peerreviewagents/engine/README.md`](peerreviewagents/engine/README.md)
documents them. Neither is `peerreviewagents.plugin` — if something you need
is only reachable there, that is a gap in this list, so say so.

## Tests

```bash
just test                    # uv run --extra dev pytest tests/ -q
uv run --extra dev pytest -q # the dev extra alone runs the whole suite
pytest tests/ -q             # no subscription, no API key, no network
```

The suite makes no model calls and no network calls, and runs with `claude` off
the `PATH`. It covers Unicode anchors, ambiguous quotes, source tampering,
overlap rejection, numerical guards, table extraction, per-track tool limits,
restricted arithmetic, identity and token forgery, editor rules, basis
assignment, stream token accounting, blocked-run reporting, source-image
provenance and the two-command CLI, plus an import-isolation check that fails if
a server dependency, a LangGraph import, or any of the deleted orchestration
modules ever creeps back.

## Paper

A manuscript describing the system is in preparation, in a private companion
repository alongside the evaluation analysis. It will be linked here on
submission.

[`docs/study-arms.md`](docs/study-arms.md) is the study design: the exact
command for each workflow arm, including the manuscript-only baseline.
[`docs/EVALUATION.md`](docs/EVALUATION.md) is the frozen audit record of the
0.4.0 OpenReview comparison, which measured the LangGraph panel; its harness
was removed in 0.8.0 with the panel itself.
[`docs/INTEGRATIONS.md`](docs/INTEGRATIONS.md) covers loading the plugin in a
Claude Code session, what 0.9 removed, data handling and troubleshooting.
[`docs/paper-scaffold.md`](docs/paper-scaffold.md) is the adapter that runs the
engine over a paper-scaffold manuscript's built PDF as one more member of its
`review-all`, with the patch beside it.

## License

MIT. See [`LICENSE`](LICENSE). Contributions are accepted under the same terms.

## Citation

Cite the concept DOI, [10.5281/zenodo.21781895](https://doi.org/10.5281/zenodo.21781895), which always
resolves to the newest version. Machine-readable metadata is in
[`CITATION.cff`](CITATION.cff), and GitHub's "Cite this repository" button
reads it. Patrick Garrett, Ricard Garcia-Carbonell and
Aleix Navarro Garrido contributed equally. The CFF format has no field for
shared first authorship, so a citation generated from that file renders them as
an ordinary author list.

## Disclaimer

A research tool to assist human peer review: not a replacement for it.
Decisions and generated text should always be checked by a human editor.
