# PeerReviewAgents

[![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.21781895.svg)](https://doi.org/10.5281/zenodo.21781895)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

Evidence-linked peer review of a manuscript PDF, on a Claude Code
subscription. One command imports the PDF into an immutable Markdown manuscript
and runs the `pra` plugin over it: several core reviewers and six specialist
tracks — seven where you pass the code behind the paper —
work the paper in parallel and in isolation, then an editor pools what they
found into one package with a decision and a reason for every finding. There is
no API key — the models run on your signed-in Claude Code login.

Every finding carries the exact quotation it rests on and the line of the
manuscript that quotation is on. A finding whose quote does not match the
manuscript does not ship.

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

What runs a review now is one command: a Python orchestrator fans out the
agents as Claude Code sessions, and an MCP server it serves itself owns the
manuscript, the agent
identities, the tool allowances, the budgets, the stage ledger and the run
files. No agent's account of its own work is taken on trust: it can only ask
the server for text and hand it findings, and the server decides what a finding
rests on.

This is a break, not a deprecation. There is no shim and no legacy path. If you
need the 0.8 engine it is frozen at the `v0.8.0` release, where it still works
unchanged; the LangGraph panel that preceded it is frozen at
`legacy-langgraph-v0.6.1`.

## Install

Install it anywhere a Python environment runs. `peerreview review` serves the
tools itself, from the very interpreter running the command, so neither the
venv's name nor its place matters, and a plain `pip install peerreviewagents`
can run a review with no checkout at all.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .
```

Or with [uv](https://docs.astral.sh/uv/), which is what CI uses:

```bash
uv venv && source .venv/bin/activate
uv pip install -e .
```

Dependencies are `rustypaper` (PDF → manuscript pieces), Pillow, and the MCP SDK.
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
at its own ceilings (3,000,000 tokens per agent, 30,000,000 per run, 90 minutes
wall clock, all overridable). They are a guard against an agent that has run
away rather than a budget this tool manages for you — the turn cap stops a
looping agent long before they do — and they are measured against an agent's
*cumulative* spend, every call added up. They are provisional until one full
review has been measured end to end.

## One review

```bash
# The whole thing: import, review, report.
peerreview review manuscript.pdf run-output

# A bare path means the same review.
peerreview manuscript.pdf run-output

# Five core reviewers instead of three.
peerreview review manuscript.pdf run-output --reviewers 5

# One model per reviewer is the default; pin them all to one to compare arms.
peerreview review manuscript.pdf run-output --reviewer-models sonnet

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
| `--reviewer-models` | `sonnet,opus,haiku` | Ordered; reviewer *i* takes the *i*-th, wrapping round |
| `--specialist-model` | `claude-sonnet-5-5` | Every specialist |
| `--editor-model` | `claude-fable-5-2` | The editor in chief |
| `--arm` | `full-two-track` | Which agents run and in what order; see [`docs/study-arms.md`](docs/study-arms.md) |
| `--track` | all six | Repeatable; limits the specialist tracks (used by `draft-check`) |
| `--citation-workers` | `4` | Workers judging in-text citations against the resolved sources, 0 to 12; only where the `citations` track runs |
| `--citation-worker-model` | `claude-haiku-5-5` | Every citation worker |
| `--code` | none | A folder or repository URL holding the code behind the paper: its text is snapshotted into the run folder and one more specialist reads the paper against it |
| `--literature-cutoff` | today | Latest eligible publication date |
| `--per-agent-tokens` | `3000000` | Per-agent ceiling on cumulative tokens, enforced from the session stream |
| `--per-run-tokens` | `30000000` | Per-run ceiling on cumulative tokens |
| `--timeout` | `5400` | Wall-clock limit in seconds |
| `--plugin-dir` | the checkout's | Override the folder the plugin is loaded from |

The core reviewers are deliberately different models: Stage A ran three of them
on one model and they caught no more than one did, which is three readings by
the same reader. So one reviewer is Sonnet, two are Sonnet and Opus, three are
Sonnet, Opus and Haiku, and a fourth starts the list again; `revision-1` runs
on `core-1`'s model because it is `core-1` revising its own findings. A model
your login cannot run is refused at start-up, naming the slot, the model and
the flag — never quietly swapped for another. Nothing about models is gated on
billing or usage. The recorder has no model flag: that slot is in the plan, but
its work happens in the `peerreview` process, so there is no session to choose
a model for and nothing to refuse a login over.

Set `PEERREVIEW_CLAUDE_VERSION` to have the run record which Claude Code
version produced it when `claude --version` cannot be asked;
`PEERREVIEW_REVIEWER_MODELS`, `PEERREVIEW_SPECIALIST_MODEL`,
`PEERREVIEW_EDITOR_MODEL`, `PEERREVIEW_READER_MODEL` and
`PEERREVIEW_CITATION_WORKER_MODEL` are those model flags for a caller that
reaches `ingest` directly instead. Nothing else in the run depends on them.

### What a run writes

```
<run>/manuscript/             the immutable import: original.pdf, paper.md and
                              map.json, page images, the manifest
<run>/review/REVIEW.md        the review, and REVIEW.txt beside it
<run>/review/issues.json      the review itself: the issues the editor grouped
                              the findings into, ranked, with their members
<run>/review/editor-log.jsonl every editor submission and every refusal
<run>/review/findings.jsonl   every stored finding, with its basis
<run>/review/quarantined.jsonl  every finding the guard threw out, and why
<run>/review/code-checks.jsonl  one row per method claim read against the
                                code: the verdict, the passage of the code
                                behind it, or the searches that found nothing
<run>/review/reviewer-<n>/    each core reviewer's own package view
<run>/review/audit_<track>.md the specialist audits
<run>/review/protocol.json    arm, slots, models, versions, provenance hashes
<run>/review/manifest.json    the paper's identity and its source hash
<run>/review/progress.json    stages and status
<run>/review/runtime.json     tokens and models per agent, wall time, limits
<run>/review/usage/<slot>/    the session stream's own usage records
<run>/review/citations/ledger.jsonl   one row per reference and per in-text use
<run>/review/citations/stages.json    what each citation stage did
<run>/review/citations/lookups/       the cached lookups and their receipts
```

`protocol.json` and `manifest.json` are written when the manuscript is
ingested, before any agent reports, so a run that stopped halfway still says
which paper it was, which arm it ran and what the source hashed to. A run that
never got as far as ingest — nothing to run the agents with, a tool server that
would not open — gets a thin pair written by the starter instead, which never
overwrites the server's own, beside a `startup.json` saying what stopped it.

`peerreview report` works on a blocked or stopped run too, and reports what was
saved rather than failing. A rate limit, a breached budget or a missing stage
leaves the run `blocked`: that is an outcome recorded in `runtime.json` and
`progress.json`, not an error to paper over.

### What the review guarantees

- **Anchored or quarantined.** A finding's quotation must match the manuscript
  exactly. One that does not is quarantined by the server, not reported.
- **The text was read against the pages before anyone reviewed it.** A PDF
  converter mangles equations and tables and returns no figure image, so one
  model reads the page images first: it repairs or confirms every equation,
  table and figure, writes a description of each figure, and seals the
  manuscript, after which no line moves. Every row says which it is — the
  converter's own text, a reconstruction from the image with the converter's
  reading kept beside it, or the model's own words — and a reviewer reading
  one is told. A run without that stage says so in its manifest; it is a
  setting, never a silent fallback.
- **A basis the agent cannot assert.** The server labels each finding
  `computed` when a `calculate` receipt covers the numbers the cited lines
  print, `code-checked` when it found the quoted code at the lines the finding
  names in the code snapshot, `two-quotes` when a second anchored quotation
  corroborates it, and `judgement` otherwise.
- **Isolation.** Core reviewers never see each other's findings and are not
  told the specialists exist. The editor sees the pool in an order seeded by
  the run salt, so slot order carries no weight.
- **Tools per track.** Every slot has an allow list. A citations specialist
  asking for figure pages is refused, and the numbers specialist has no page
  image tool at all, because a quantity you had to read off a plot belongs to
  the figures-and-tables lane.
- **One lane each.** The six specialists audit disjoint dimensions —
  `numbers` every printed quantity and the arithmetic between them, `stats` the
  inference drawn from them, `figures-tables` what a panel shows,
  `methods-reporting` what is absent from the methods, `citations` the
  reference list and every use of it, `field` what this kind of study is
  required to do. Each brief names the other five lanes as not its own, so a
  defect is filed once rather than by whoever noticed it first.
- **The issue is the unit.** The editor groups the findings into issues — one
  problem, one issue, its findings kept inside it as evidence — gives each a
  severity, a category, a rank and a one-paragraph statement, and must rule by
  name on every specialist escalation. Nothing is capped and nothing is hidden:
  the major and moderate issues are the required changes in rank order, the
  minor ones are suggestions by category, and every finding belongs to an
  issue. `issues.json` is the source of truth and `REVIEW.md` is rendered from
  it alone.

### The code behind the paper

Pass `--code` and one more specialist joins the run: it reads the whole
manuscript, then reads the paper's methods against the code the authors
released.

```bash
peerreview review manuscript.pdf run-output --code ./analysis
peerreview review manuscript.pdf run-output --code https://github.com/lab/analysis
```

A URL is cloned shallow, with hooks and submodules off. Either way the **text**
of the code is snapshotted into `<run>/code/` — source, configuration, notebooks
and documentation, skipping binaries and anything over the size caps — beside a
manifest naming the origin, the commit, every file taken with its digest, every
data file the code ships, and everything left out with the reason. The snapshot
is what the review reads, so the finding you get back cites code that is still
there a year later.

Four rules about what lands there, because a snapshot nobody can describe is
not evidence:

- **The data is listed, and fetched when the reviewer asks for it.** A paper's
  repository ships anything from a 2 KB configuration to a 40 GB table, so
  none of it is copied up front and none of it spends the size caps: each data
  file is a row with its path, size and kind, and reading one brings it into
  the snapshot, where it is quoted and checked exactly like source. A file
  over the per-file cap comes back as a bounded head with the file's size, its
  line count and its column names, marked partial, and a finding may quote
  only the lines that were stored. Binary data and anything held in Git LFS
  stay listed and are never opened.

- **A notebook is stored as its cells**, code and markdown in order, each marked
  with its cell number, every output dropped — and the size cap is applied to
  those cells, not to the megabytes of base64 a single figure puts in the file.
  Code evidence cites lines of that stored form.
- **Nothing that may hold secrets is copied.** `.env` and `*.env`, keys and
  certificates, `credentials*`, `secrets*`, `.netrc`, `.npmrc`, `.pypirc` are
  skipped by name, with that as the reason. The snapshot is served to a model,
  so a credential copied into it is a credential handed out.
- **What was never looked at is named once.** A folder the walk refuses —
  `node_modules`, `.git`, a build cache — and a symbolic link out of the
  snapshot are each one row of the manifest rather than none, so "the code
  never does that" is never read off a tree nobody opened.

The lane's three tools list, read and search that snapshot and nothing else.
None of them executes anything, reaches the network, or can name a path outside
it: a review reads code, it does not run it. The code is treated as data
throughout — a comment or README telling a reviewer what to report is text being
audited, not an instruction being followed.

Every finding still quotes the manuscript, as every finding in this review does,
and a code finding carries the code as well: the file, the line range and the
exact text, which the server checks against the snapshot before it stores
anything. A quote that is not at those lines is refused and nothing is stored.
A finding that passes is recorded as `code-checked` — what the code says has
been checked by the server; that it disagrees with the paper is still the
reviewer's argument, and the editor cannot bury it as a suggestion without
quoting the manuscript back.

`REVIEW.md` and `read_run` say which code was reviewed, at which commit, and how
many files of the snapshot a reviewer actually opened, because "the code was
reviewed" over a lane that opened one file of forty is a claim the run folder
does not support.

### Citations, every one of them

The `citations` track does not sample the reference list. The server keeps a
table — `<run>/review/citations/ledger.jsonl`, one row per reference and one
per in-text use — and fills it in four stages, the first two of which call no
model at all:

1. **Fill.** `ingest` writes a row for every reference in the extracted list,
   whether or not it has a DOI, and a row for every sentence that cites one.
2. **Resolve.** The server looks up *every* reference: a DOI against Crossref,
   a reference without one by title against Crossref and then Europe PMC —
   and where the reference list gives no title field, the title is read out of
   the reference text itself, in the styles journals print, so the row is
   asked about rather than filed as unchecked. Each
   row records `exists` in four states — yes, **no** (checked and not found),
   **not located** (an index answered and listed nothing) and **could not
   check** (no lookup was made, or none answered) — so a lookup that failed is
   never read as a reference that does not exist. A DOI the reader broke across
   a space is rejoined and looked up again before any of this is decided, and a
   row whose printed DOI is cut short can never be `no`. A rejoined DOI no
   index holds a record for is still asked of the DOI resolver and of its
   registration agency, so a reference registered elsewhere is `yes`,
   registered, with a note that its authors could not be corroborated because
   no index this version reads holds its metadata. Each row also carries
   a per-field comparison of title, first author, year and venue, any retraction notice,
   and the abstract or open-access full text with the URL it came from. Source
   text is looked for in four places, in order and stopping at the first that
   answers: the record the index already returned, Europe PMC for the article
   and its open-access full text, PubMed Central for the same article where
   Europe PMC's copy did not answer, and arXiv for a preprint neither index
   holds an abstract for. A full text is the article's `<body>` and nothing
   else, so an answer carrying only front matter is recorded as no full text.
   Every response is cached in the run folder, and the requests carry a generic
   `User-Agent` with no address and nothing identifying the person running it.
3. **Judge.** `--citation-workers` Haiku workers (`cite-1`, `cite-2`, …) claim
   batches and write one verdict per in-text use — supports, partly supports,
   does not support, cannot tell — each quoting the exact source passage it
   judged from. `cannot tell` is the only verdict allowed no source text. The
   source text on a row is the abstract plus, for an open-access full text, the
   few windows of it whose sentences share the most subject-matter words with
   the citing sentence being checked — a paper is far longer than any window a
   worker is shown, and the opening of it is a title and an introduction about
   other people's work. The row records which passages were shown and how they
   were chosen, so "this verdict rests on full text" names the passage it
   rested on. A worker files no findings and cannot hold `write_review` open.
4. **Confirm.** The citations specialist reads the whole ledger instead of
   sampling it, re-checks every problem row itself, and only what it confirms
   becomes a finding. A worker's verdict is a row in a table, never a finding.

`read_run()['citations']` and the review's own **Citations** section print the
counts, the ledger path and one line per stage that did not finish. An
unfinished ledger is reported, not published over — it is never a
`not_publishable_reason`; see [Reading a run folder](#reading-a-run-folder).

[`peerreviewagents/plugin/pra/README.md`](peerreviewagents/plugin/pra/README.md)
documents the parts themselves: the agents, the orchestrator, the runner, the
server tools and the arms.

## Manuscript ingest

PDFs are converted to manuscript pieces by [rustypaper][rustypaper], which keeps
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
piece, line and page counts, and the manifest hash the review is anchored to. Publish
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

It carries three skills: `peer-review-manuscript` for a full review,
`check-draft` for a draft of your own, and `check-fixes` for a new draft
against problems already raised. Each one shells out to `peerreview`, so the
session you are in asks for the review and the command runs it — same panel,
same server, same guarantees as running it yourself. Nothing in the plugin
launches a server: a run serves its own tools, in its own process, for its own
lifetime. The package has to be installed in the environment that shell uses.

## As a library

```python
from peerreviewagents import review, read_run

run = review("manuscript.pdf", "run-output", reviewers=3)
print(run["status"], run["tokens_total"], run["report"]["markdown"])

summary = read_run("run-output")          # any run folder, finished or not
print(summary["status"], summary["cause"], len(summary["issues"]))
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
| `verify_workspace(workspace)` | re-check an import against its own manifest, returns its manuscript and the map over it |
| `export_draft(paper, review, decisions)` | apply the accepted anchored edits to a copy of `paper.md` |
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
arm, the wall time, the total and per-agent tokens, the models each slot was
asked for in `models_requested` beside the models that were observed running,
and the finding counts the server recorded. A `blocked` run returns the
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
error, and stopped at start-up before any agent ran — nothing to run them with,
or a tool server that would not open — which leaves only `startup.json` and
`runtime.json`. It takes the run folder or the
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
| `findings` | every stored finding, de-duplicated per `finding_uid` |
| `issues` | the review: one row per issue, with `id`, `name`, `severity`, `category`, `rank`, `statement`, `raised_by`, its `members` (the findings that evidence it), and the specialist `audits` it cites |
| `issues_source` | `editor` where an editor grouped the findings, `server` where the arm runs no editor and the server synthesised one issue per finding |
| `findings_source` | `issues`, `ledger` or `none` |
| `counts` | `findings`, `issues`, `required`, `suggestions`, `issue_members`, `by_severity`, `by_category`, `quarantined`, and the server's own `findings_delivered`/`findings_submitted` |
| `quarantined`, `guard_status` | what the guard threw out, and `clean` or `quarantined` |
| `citations` | the citation ledger: `enabled`, the absolute `ledger` path, `counts` (references resolved, not found, could not be checked, mismatched, retracted; in-text uses judged and how), `complete`, a one-sentence `summary`, and `problems` — one line per stage that did not finish |
| `escalations`, `escalation_rulings` | the specialist findings that asked for a ruling, and the rulings given |
| `summary`, `verdict`, `limitations` | the editor's own three, or `None`/`[]` |
| `identity` | paper id, source hash, PDF path, arm, reviewers, specialist tracks, versions, `provider`, `code_hashes` — the manifest's digest of each engine and plugin file that served the run — and `models`/`reviewer_models`, the model each slot was started with (both empty for a run from before 0.9.0), with `models_unverified` naming the slots that never reported running one |
| `manuscript` | what state the workspace is in, which is not what state the run is in: `state` is one of `MANUSCRIPT_STATES` — `absent` for a run that stopped before the import, `draft` for a converted paper the Read stage never sealed (no line numbers, nothing can cite it), `sealed-by-reader` for a base a model read the pages of, `sealed-without-read` for one written with no page read at all — plus `sealed`, `reason` (why nothing read the pages, in the manifest's own words), `reader_model`, `lost_pages` (each page the converter returned nothing from, with the document and lines its transcription became) and `documents` |
| `code` | whether code was reviewed beside the paper: `reviewed` is false for every run given none, and otherwise `kind` (`git-clone` or `folder`), `source`, `commit`, `branch`, `dirty`, the `snapshot` folder, `files_in_snapshot`, `files_skipped`, `truncated`, the snapshot's own `manifest`, what the data it lists came to — `data_listed`, `data_fetched`, `data_partial`, `data_refused`, `data_bytes` — and what a reviewer did with it: `files_opened`, `opened`, `searches` |
| `code_check` | what the code lane's second pass answered, and `None` for a run given no code: `cap` (the most findings it is handed), `checked`, `over_cap` (how many qualified past the cap and were left exactly as filed), `counts` per verdict, and `checks` — one row per answer, with the finding, the verdict (`confirmed`, `contradicted` or `not-shown`), the `code_evidence` verified against the snapshot or the `searches` behind a `not-shown`, and the `note` for the authors. A `contradicted` finding is in no issue and in no required change: it is here and in the review's thrown-out section |
| `stages` | the stage ledger the run recorded, or `None`: `required`, `reported`, `missing`, and `missing` split in two — `silent` (stored nothing, filed nothing, so the run is blocked) and `missing_with_findings` (findings delivered and ruled on, no report filed, so the review is written and `note` names the gap) — plus `editor_trace`, what the editor's submission calls did and the last refusals it could not satisfy |
| `runtime` | wall time, total tokens, rate limits, both token ceilings, the Claude Code version |
| `agents` | one row per agent slot: role, track, `assigned_model` and the `model` it reported running as, tool calls, tokens, state |
| `usage` | observed subscription tokens from the usage receipts, with `unknown` set if any receipt was unreadable |
| `reports` | absolute paths to what exists — `REVIEW.md`, `REVIEW.txt`, `issues.json`, `editor-log.jsonl`, and `audits` keyed by track |
| `startup` | what start-up recorded: `problems` — the lines a run that could not begin was stopped over — and `probe`, the harness's own init event from a 0.9 folder that probed one (`None` otherwise, or where it holds the wrong shape) |
| `startup_problems` | that same list at the top level, for callers that already read it |
| `problems` | every file that could not be read in full, named, every slot that ran a different model family than it was assigned, and one line per citation stage that did not finish |
| `files` | the run files that exist, by name |

Findings always come from the `findings.jsonl` ledger, de-duplicated to the
latest row per `finding_uid`: the ledger is append-only, so a finding restated
during the run is one finding, not three. Where that file is gone, the issues'
own `members` carry the same rows and are read instead. The issues come from
`issues.json`, which is the review — nothing is capped and nothing is placed
out of sight, so every finding reaches the review through the issue that holds
it, and the two readings never disagree about how much was found.

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
status is `complete` or `complete-without-findings`, the issues came from a
readable `issues.json` an editor wrote, the guard recorded a verdict, and no
file was unreadable. When it is false, `not_publishable_reasons` names every
reason in words, so the flag is never false with nothing said. An arm that runs
no editor is never publishable and that is not a fault: its `issues_source` is
`server`, one issue per finding, and a reason says so.

A quarantined finding is not one of those reasons. The guard exists to throw out
a finding whose quote it could not match to the line it names; a guard that did
that has done its job, and the review that is left is publishable. `quarantined`
says what was thrown out, and whether that mattered is a judgement for a person.

**An unfinished citation ledger is not one either.** A reference the lookups
could not reach and an in-text use nobody judged are lines in `problems` and in
`citations['problems']`, and they are deliberately not reasons: a finding the
citations specialist confirmed is a finding whether or not the rest of the table
was finished, and `citations['counts']` says in numbers how much of the
reference list this run actually checked. `citations['complete']` is the flag
for "the whole table was filled, resolved and judged".

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
command for each arm, including the manuscript-only baseline.
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
