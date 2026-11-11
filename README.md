# PeerReviewAgents

[![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.21781895.svg)](https://doi.org/10.5281/zenodo.21781895)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

Evidence-linked peer review of a manuscript PDF, on a coding-agent
subscription. One command imports the PDF into immutable source blocks, runs
independent reviewers against bounded manuscript and literature tools,
synthesizes their findings through an audited debate, and validates every quote
and location before a finding reaches the report. There is no API key: the
models run on your signed-in Claude Code or Codex subscription.

Every finding carries the block it is anchored to and the exact quotation it
rests on. A finding whose quote does not match the manuscript does not ship.

```bash
peerreview manuscript.pdf
```

That writes `manuscript-review/` beside the PDF, holding the imported
manuscript, the run, and `REVIEW.md` and `REVIEW.txt`.

## 0.8.0: the engine is the whole program

0.8.0 removed the LangGraph panel that shipped through 0.7.x — the five
specialist reviewers, the advocate/skeptic rounds, the journal profiles, the
strictness levels, the desk screen, the revision loop, the Textual TUI and the
API-key providers. The engine, `peerreviewagents.engine`, is now the only
review path, and every surface runs it: the command line, the web UI, the MCP
server, the Claude Code and Codex plugins, and the Pi package.

The panel is not deprecated in place, it is gone. If you need it, it is frozen
at the tag `legacy-langgraph-v0.6.1` (and in the `v0.7.0` release), where it
still works unchanged. Nothing on `main` imports `langgraph` or `langchain`.

## Install

You need a virtual environment. Current Linux distributions and Homebrew Python
refuse a bare `pip install` into the system interpreter (PEP 668), so the first
line is not optional.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .

# MCP server and Claude Code or Codex plugin support
pip install -e '.[mcp]'
```

Or with [uv](https://docs.astral.sh/uv/), which is what CI uses:

```bash
uv venv && source .venv/bin/activate
uv pip install -e '.[mcp]'
```

Dependencies are `rustypaper` (PDF → source blocks), Pillow, FastAPI and
Uvicorn for the web UI, and `python-dotenv`. No OCR, no paid service beyond the
subscription you already have, and no provider SDK.

Then sign in to the transport you want to run on. Claude Code is the default,
so the standalone `claude` CLI signed in to your Claude account is all a review
needs. Claude Desktop alone does not install that command; install it from
[the Claude Code quickstart](https://code.claude.com/docs/en/quickstart), or
set `PEERREVIEW_CLAUDE_PATH` to its absolute path if it lives somewhere
unusual. For Codex, sign in to the `codex` CLI with ChatGPT and pass
`--provider codex` (or set `PEERREVIEW_CODEX_PATH`). Nothing selects Codex
implicitly.

How much subscription usage a run may spend is your own account setting. The
engine records every call in `usage.jsonl` and does not gate on it.

## One review

```bash
# The whole thing: import, review, report.
peerreview manuscript.pdf

# Name the run folder instead of taking the default beside the PDF.
peerreview review manuscript.pdf run-output

# Bound what published work is eligible (defaults to today).
peerreview review manuscript.pdf run-output --literature-cutoff 2026-01-05
```

The three steps are still separate commands when you want them to be:

```bash
peerreview import manuscript.pdf workspace --xml manuscript.xml --supplements supplement-dir
peerreview verify workspace
peerreview review workspace run-output
peerreview report run-output        # rewrites the reports from saved JSON, no model calls
```

`peerreview --help` lists every command: `import`, `verify`, `add-review`,
`result-map`, `review`, `investigate`, `report`, `checks` and `serve`.

### What a run writes

A one-command run folder (`manuscript-review/` by default) holds:

- `manuscript/` — the immutable import: `original.pdf`, `paper.json` source
  blocks, page images, and the manifest `verify` checks against.
- `review/` — the run itself: saved finding JSON, the validated coverage
  record, tool logs, `progress.json` and `usage.jsonl`.
- `REVIEW.md` and `REVIEW.txt` — the run details, the final review, and each
  independent reviewer for an ensemble run.

`peerreview report` works on a blocked or stopped run too, and reports what was
saved rather than failing.

### Modes, policies and models

The default review is the agentic ensemble: two independent reviewers at high
effort with a thirty call tool allowance and no finding cap, then an audited
debate synthesis.

| Flag | Default | What it changes |
| --- | --- | --- |
| `--mode` | `ensemble` | `independent` is one reviewer; `panel` is one reviewer plus a debate pass |
| `--reviewers` | `2` | Independent discovery reviewers for ensemble mode, 1 to 16 |
| `--policy` | `agentic-v1` | `standard-v1` is the bounded study budget: at most five findings in about 900 words at medium effort. `workspace-agentic-v1` is one long agentic session, so it needs `--mode independent` |
| `--provider` | `claude-code` | `codex` runs on a ChatGPT login |
| `--model` | `claude-sonnet-5-5` | Or `gpt-5.6-terra` with `--provider codex`; `--help` lists the accepted IDs |
| `--preset` | — | Pins mode, reviewers, token budget and policy together, so pass it alone |
| `--token-budget` | per policy | Observed-token launch ceiling for the run, not a hard cap on a running call |
| `--literature-cutoff` | today | Latest eligible publication date |

Three further modes — `plain`, `workspace` and `self-revision` — are study
instruments and are deliberately absent from `--help`.
[`docs/study-arms.md`](docs/study-arms.md) gives the exact command for every
ablation arm, including the manuscript-only baseline.

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

Publisher JATS XML is preferred when it exists (`--xml`): it gives real text
and parsed references, where PDF-only references stay provisional.
`--supplements` preserves supplementary table cells. Source page images are
attached so a reviewer can inspect a figure, and `--source-image` attaches up to
ten specific pages.

**Convert here, not before.** Handing the engine a Markdown file you converted
yourself looks equivalent and is not: the run records which converter read the
manuscript, and a conversion done elsewhere would be recorded as though this
one did it. Give it the PDF.

[rustypaper]: https://github.com/pgarrett-scripps/rustypaper

## Web UI

```bash
peerreview serve                 # http://127.0.0.1:8000
just serve
```

Upload a manuscript PDF or point at one under an allowed input root, pick the
mode, reviewers, provider and literature cutoff, and watch the run. Reviews run
as background jobs on the same engine the command line uses, so the page polls
status and reads `REVIEW.md` and the saved artifacts when they land; closing the
browser does not stop a run. `--output-dir`, `--upload-dir` and the repeatable
`--input-root` bound what the server may read and write. It is a local tool with
no authentication: do not expose it to a network you do not control.

## Local coding agent plugins

This repository packages local integrations for Claude Code, Codex, Factory
Droid and Pi, and all of them run the same one-command engine review as a
background job, so a client can check status and read artifacts without holding
one tool call open for the full run.

For Claude Code or Claude Desktop's Code tab, install and sign in to the
standalone Claude Code CLI, then run the same installer on macOS, Linux, or
Windows with Git Bash:

```bash
bash scripts/install-claude.sh
```

The installer detects your platform, downloads Python automatically, and
configures the plugin with absolute executable paths. No preinstalled Python or
uv is required. VS Code is not required. Restart Claude Desktop after
installation and select a Local Code session. See
[Claude setup](docs/INTEGRATIONS.md#claude-code-and-claude-desktop) for
installation without a checkout, Windows without Bash, and upgrades.

For other clients, install the MCP extra so `peerreview-mcp` is available.
During local development, load the repository directly in Claude Code:

```bash
claude --plugin-dir /absolute/path/to/PeerReviewAgents
```

Add the repository marketplace and install the Codex plugin:

```bash
codex plugin marketplace add /absolute/path/to/PeerReviewAgents
codex plugin add peer-review-agents@peer-review-agents-local
```

Both clients use the same `skills/peer-review-manuscript/SKILL.md` workflow and
`.mcp.json` server. See [Local agent integrations](docs/INTEGRATIONS.md) for
clean installation, upgrade, uninstall, Factory Droid, Pi, generic MCP,
security, and the tested compatibility matrix.

## As a library

```python
from peerreviewagents import plan_review, review_options, run_review_options

paths = plan_review("manuscript.pdf")   # imports the PDF, resolves the layout
options = review_options(reviewers=2, literature_cutoff="2026-01-05")
result = run_review_options(paths, options)

print(result["progress"]["status"], result["reports"]["markdown"])
```

`review_options` validates a request and refuses a bad one in the same words
every surface uses; `run_review_options` runs it, writes the reports and returns
their paths alongside the run's progress record and `failure`.

Those are the same three functions every surface calls, so a library run and a
`peerreview` run are the same review. `peerreviewagents.capabilities()` reports
what this install can run without starting anything.
`peerreviewagents.engine.api` is the versioned engine boundary;
[`peerreviewagents/engine/README.md`](peerreviewagents/engine/README.md)
documents the engine in full, including the investigation tools, the debate
accounting, the Paper Scaffold adapter and the stated boundaries.

## Tests

```bash
just test                    # uv run pytest tests/ -q
pytest tests/ -q             # no subscription, no API key, no network
uv run --extra dev --extra web-test --extra mcp pytest -q
```

The suite makes no model calls and no network calls. It covers Unicode anchors,
ambiguous quotes, source tampering, overlap rejection, numerical guards, table
extraction, tool limits, restricted arithmetic, malformed debate dispositions,
source-image provenance, cache identity, interrupted-call recovery and usage
accounting, plus the surfaces over the engine: the bare command, the MCP
server, the web UI end to end, the Claude installer on macOS and Windows, and
an import-isolation check that fails if a server dependency or a LangGraph
import ever creeps back into the engine.

## Paper

A manuscript describing the system is in preparation, in a private companion
repository alongside the evaluation analysis. It will be linked here on
submission.

[`docs/study-arms.md`](docs/study-arms.md) is the engine's study design: the
exact command for each arm, including the manuscript-only baseline.
[`docs/EVALUATION.md`](docs/EVALUATION.md) is the frozen audit record of the
0.4.0 OpenReview comparison, which measured the LangGraph panel; its harness
was removed in 0.8.0 with the panel itself.

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
