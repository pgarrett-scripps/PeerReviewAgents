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

What runs a review now is a Claude Code plugin — `plugins/pra` — whose workflow
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

You need a virtual environment, and it has to be `.venv` in the checkout: the
plugin launches its MCP server as `${CLAUDE_PROJECT_DIR}/.venv/bin/python`, and
`${CLAUDE_PROJECT_DIR}` is the package root the starter runs the session from.

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
<run>/review/progress.json    stages and status
<run>/review/runtime.json     tokens and models per agent, wall time, limits
<run>/review/usage/<slot>/    the session stream's own usage records
```

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

[`plugins/pra/README.md`](plugins/pra/README.md) documents the plugin itself:
the agents, the workflow, the 22 server tools and the arms.

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
claude --plugin-dir /absolute/path/to/PeerReviewAgents/plugins/pra
```

Or add the repository as a marketplace and install it by name:

```bash
claude plugin marketplace add /absolute/path/to/PeerReviewAgents
claude plugin install pra@peer-review-agents-local
```

It carries three skills: `peer-review-manuscript` for a full review,
`check-draft` for a draft of your own, and `follow-up` for what changed since
the last run. Each one runs the same server and the same guarantees as
`peerreview review`.

## As a library

```python
from peerreviewagents import review, import_paper, write_reports

run = review("manuscript.pdf", "run-output", reviewers=3)
print(run["status"], run["tokens_total"], run["report"]["markdown"])
```

Three functions, resolved lazily so `import peerreviewagents` stays cheap:
`review` is the whole review and is exactly what `peerreview review` runs,
`import_paper` imports a PDF into a manuscript workspace with no model call,
and `write_reports` renders `REVIEW.md` and `REVIEW.txt` from a run folder,
also with no model call.

`review` returns the run's summary and prints nothing: `status`,
`blocked_reason`, `stage`, the run and review folders, the report paths, the
arm, the wall time, the total and per-agent tokens, the models each slot ran
on, and the finding counts the server recorded. A `blocked` run returns the
same shape with the reason filled in, so there is nothing to catch and no
stdout to parse. `peerreviewagents.plugin.starter.exit_code(summary)` is what
the command turns it into — `0` reviewed, `2` never started, `1` otherwise.

That is the whole importable surface. `peerreviewagents.engine` is no longer an
integration boundary: it holds the manuscript workspace, the investigation
tools and the report writer the plugin server calls, and
[`peerreviewagents/engine/README.md`](peerreviewagents/engine/README.md)
documents them.

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
