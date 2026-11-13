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

### Models

The core reviewers run on different models by default, because Stage A ran
three of them on one model and they caught no more than one did. Reviewer *i*
takes the *i*-th model of an ordered list, wrapping round, so one reviewer is
Sonnet, two are Sonnet and Opus, and three are Sonnet, Opus and Haiku:

```bash
peerreview review manuscript.pdf run-output --reviewers 3 \
    --reviewer-models sonnet,opus,haiku \
    --specialist-model sonnet --editor-model fable
```

Those three are the defaults — `--reviewer-models sonnet,opus,haiku`,
`--specialist-model claude-sonnet-5-5`, `--editor-model claude-fable-5-2`, with
the recorder on `claude-haiku-5-5`. Each takes one of the four models this
version knows — `claude-sonnet-5-5`, `claude-opus-5-5`, `claude-haiku-5-5` and
`claude-fable-5-2` — by alias or by id, and anything else is refused by name as
a model this version does not know rather than as one your login may not run.
`revision-1` always runs on `core-1`'s model because it is `core-1` revising
its own findings. Pinning every reviewer to one model is
`--reviewer-models sonnet`, which is what a run being compared against a
recorded Stage A result wants. **None of this is gated on billing or usage:**
which models your login may run is yours to decide.

A model this login cannot run is refused at start-up, before the probe spends
anything, with one line naming the slot, the model and the flag that changes
it — Claude Code answers a model it may not run by quietly running another, so
a run that asked for Opus could otherwise be recorded as an arm it was not.
Availability is the `availableModels` allowlist in the settings files Claude
Code reads, managed settings first.

The citation workers are the one slot kind with its own count as well as its
own model: `--citation-workers N` (0 to 12, default 4) and
`--citation-worker-model` (default `claude-haiku-5-5`). They exist only in an
arm that runs the `citations` specialist, and a model that login cannot run is
refused at start-up like any other.

A session that drives the workflow itself passes `reviewer_models`,
`specialist_model` and `editor_model` to the `ingest` tool instead; its answer
gains `models`, one `{slot, model, spawn}` row per agent, where `spawn` is the
family alias the workflow hands Claude Code and `model` is the version the run
records; `citation_workers` and `citation_worker_model` go the same way.
`PEERREVIEW_REVIEWER_MODELS`, `PEERREVIEW_SPECIALIST_MODEL`,
`PEERREVIEW_EDITOR_MODEL`, `PEERREVIEW_RECORDER_MODEL` and
`PEERREVIEW_CITATION_WORKER_MODEL` do the same for a session the starter
launched, and an explicit `ingest` argument wins over the environment.

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
is `Workflow`, `Skill`, `TodoWrite` and the 27 `peerreview` tools, and nothing
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
    locate_run, NotARunFolder,             # resolve a run folder, and the error
    import_paper, verify_workspace, export_draft,
    plugin_directory, claude_code_version,  # check an installation
    review_parser, report_parser,           # the real parsers behind the commands
    RUN_DIRECTORY, MANUSCRIPT_DIRECTORY,   # "review" and "manuscript"
    RUN_STATUSES, COMPLETE_STATUSES, CLI_COMMANDS, FINDING_SCHEMA_VERSION,
)
```

The callables resolve lazily, so `import peerreviewagents` costs nothing and an
app that only reads finished runs never imports the launcher or the MCP SDK.
The constants are plain values from `peerreviewagents.names`, which imports
nothing at all.

### Wrapping the two commands

`review_main(argv, prog=...)` and `report_main(argv, prog=...)` are
`peerreview review` and `peerreview report` as functions: they take the argument
list, print the JSON the command prints, and **return** the exit code instead of
raising it. `prog` is the name in the wrapper's own `--help` and in its argument
errors, so each of your commands can call itself what it is:

```python
import sys
from peerreviewagents import report_main, review_main

command, argv = sys.argv[1], sys.argv[2:]
if command == "review":
    raise SystemExit(review_main(argv, prog="workbench review"))
raise SystemExit(report_main(argv, prog="workbench report"))
```

That is the whole reason they exist. Copying `starter.main`, or reaching into
`plugin.starter` for the parser, means your wrapper breaks when an option is
added; these two do not.

### Checking the installation and your own commands

Four names answer the questions a wrapper asks about the engine rather than
about a run, so none of them needs a private module:

```python
from peerreviewagents import (CLI_COMMANDS, claude_code_version,
                              plugin_directory, report_parser, review_parser)

if claude_code_version() is None:          # None, not an exception
    raise SystemExit("Claude Code is not installed on this PATH")
plugin = plugin_directory()                # package data, not a checkout

documented = {"review", "report", "doctor"}
assert documented >= set(CLI_COMMANDS), "a peerreview command this wrapper hides"
options = {action.dest for action in review_parser()._actions}
```

`plugin_directory()` is the `pra` plugin that ships inside the package — what
`claude --plugin-dir` is given, and what to assert the existence of before
offering a review. `claude_code_version()` is what `claude --version` prints,
or `None` when Claude Code is not on this PATH: the one precondition a review
cannot do without. `CLI_COMMANDS` is `("review", "report")` and there is no
third; `review_parser(prog=...)` and `report_parser(prog=...)` are the real
parsers those commands run on, so a wrapper that documents its own options can
diff them against these instead of against a copy that silently falls behind.
Use `review_main` and `report_main` to *run* a command; these are for checking
one.

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

`locate_run(path)` is the step `read_run` does first, on its own: it returns
`(run folder, review folder)` for either spelling, raises `NotARunFolder` for a
folder that is neither and refuses a symlinked `review/` by name. An app that
lists a directory of runs before reading any of them wants that, not a reading
of each.

It works on every kind of folder a review can leave behind, which is the point:

| Folder | What `read_run` gives you |
| --- | --- |
| complete | `status` `complete`, findings from `editor-package.json`, body and appendix |
| complete without findings | `status` `complete-without-findings`, `findings` empty, `cause` `None` |
| blocked by the server | the cause from `progress.json`, findings from the ledger |
| killed by the starter | the cause from `runtime.json` — token ceiling, timeout, rate limit, session error — even though `progress.json` still says `running` |
| start-up blocked | `stage` `startup`, `startup_problems`, and the identity the starter wrote |
| killed mid-write | everything that could be read, and each file that could not in `problems` |

Keys: `status`, `complete`, `publishable`, `not_publishable_reasons`, `blocked`,
`cause`, `note`, `stage`, `findings`, `body`, `appendix`, `findings_source`,
`counts`, `quarantined`, `citations`, `guard_status`, `escalations`,
`escalation_rulings`, `summary`, `verdict`, `limitations`, `identity`, `stages`,
`runtime`, `agents`, `usage`, `reports`, `startup`, `startup_problems`,
`problems`, `files`, `run`, `review`. The same 33 keys come back for every
folder, in every state, so a consumer never has to test whether a key is there.

Three of them hold what an integration used to read out of the run folder
itself:

- `identity['code_hashes']` is `manifest.json`'s code map — one digest per
  engine and plugin file that served the run, so two runs of the same arm can
  be told apart when the plugin changed between them. `{}` for a run that
  stopped before `ingest` wrote one, or for a map that is damaged.
- `identity['provider']` is what ran the review — `claude-code` for every run
  this version writes, from `protocol.json` rather than inferred. `None` when
  the protocol does not say.
- `startup` is `{'probe': ..., 'problems': [...]}` from `startup.json`: the
  harness's own start-up event and what the probe refused to launch over.
  `probe` is `None` when no probe was written or it holds the wrong shape, and
  `problems` is the probe's own list. The long-standing `startup_problems` key
  is the same list, kept as it was.

Damage in any of the three is named in `problems` and read as absent or empty,
like every other damaged file.

Three more say which model wrote which report, so a published review can name
them. `identity['models']` maps each slot to the model the run was started
with and `identity['reviewer_models']` is the ordered list the core reviewers
were drawn from; each row of `agents` carries `assigned_model`, the plan, beside
the `model` the agent reported running as. The two are never merged and neither
stands in for the other: `model` is `None` when nothing observed that slot —
no model in the session stream and no self-report — and it never falls back to
`assigned_model`. `identity['models']` and `identity['reviewer_models']` are
empty for a run written before 0.9.0, which recorded no models at all — empty
means *not recorded*, never *the default*.

`identity['models_unverified']` is the third: the slots that were assigned a
model and never reported running one. Those runs are still publishable —
nothing was seen to disagree with the plan — but for those slots the plan is
all there is, and `REVIEW.md` and `REVIEW.txt` carry the same sentence under
**Models unverified**. Show it beside `identity['models']` rather than showing
the assignment alone.

A slot that ran a *different model family* from the one it was assigned is a
different matter. That run did not run the models it is filed under, and no
later reading of the folder repairs it: `read_run` names the slot, the assigned
model and the observed one in `problems` and again in
`not_publishable_reasons`, so `publishable` is `False`. A different *version* of
the same family is not a mismatch — which version a family alias resolves to is
the login's business and the observed id is simply what gets reported — so it
is neither a problem nor a reason. Compare families, not ids, if you do this
yourself: `peerreviewagents.plugin.models.substituted(assigned, observed)` is
the rule both the reader and the report use.

One start-up refusal is worth knowing about if you launch runs yourself.
Before Claude Code 2.1.251, `CLAUDE_CODE_SUBAGENT_MODEL` outranked both the
per-spawn model override the plugin sends and the agent files, so with it set
every agent in the run would be the one model it names while `protocol.json`
recorded the per-slot plan. `peerreview review` refuses to start in that
situation — one `ValueError` before the probe, naming the variable, the version
and what to do — and refuses the same way when `claude --version` cannot be
read at all, because "new enough" is not something an unreadable version says.
The variable is never unset: it is the user's, and a process that quietly
removed it would be overriding a deliberate choice. Unset it in the environment
you launch from, or launch from Claude Code 2.1.251 or newer, where it ranks
below both and the plan holds.

Four rules it enforces so you do not have to:

- **One row per finding.** `findings.jsonl` is append-only, so a finding
  restated during the run is several rows. `read_run` keeps the latest row per
  `finding_uid`, in the order the findings were first seen, from the editor
  package when there is one and from the ledger when there is not —
  `findings_source` says which. A uid the editor placed in both its lists is in
  `body` only, so it is one finding and one escalation, not two.
- **A folder that is not a run is an error, not an empty answer.** It raises
  `peerreviewagents.NotARunFolder` (a `ValueError`, also reachable as
  `peerreviewagents.runs.NotARunFolder`) naming the files it
  looked for.
- **A damaged file inside a real run is named, not raised.** A truncated
  `runtime.json`, a half-written last line in a usage receipt, a status file
  holding a list, a ledger with bytes that are not UTF-8 — the normal state of
  a run somebody killed. The unreadable file or line goes into `problems` and
  everything else still comes back, because a run that stopped badly is exactly
  when you need to know why. When a receipt could not be read in full,
  `usage['unknown']` is `True` and `usage['observed_tokens']` is a lower bound.
- **A symlink anywhere under `review/` is refused, by name.** The review
  directory itself, a usage receipt, a specialist audit, `editor-package.json`:
  all the same way, with a `ValueError` naming the path. A symlink in
  `editor-package.json`'s place is some other file's contents presented as this
  run's result.

### The citation ledger

`run['citations']` is what the run's own citation table says. It is the same
shape for every folder:

```python
cites = read_run("run-output")["citations"]
if cites["enabled"]:
    print(cites["summary"])                 # one sentence, already written
    print(cites["counts"].get("references", 0), "references,",
          cites["counts"].get("not_found", 0), "with an unregistered DOI,",
          cites["counts"].get("not_located", 0), "in neither index,",
          cites["counts"].get("could_not_check", 0), "not checked")
    for line in cites["problems"]:          # one per stage that did not finish
        print("unfinished:", line)
```

- `enabled` is `False` — and `ledger` `None`, `counts` `{}` — for an arm that
  keeps no ledger. Only an arm running the `citations` specialist with network
  tools does: `full-two-track`, and `draft-check` when `citations` is one of the
  chosen tracks. **Absent is not zero.** A run with `enabled: False` checked no
  citations; a run with `references: 0` had no reference list to check.
  The third case reads differently on purpose: an arm that planned a citations
  lane and left no table behind has `enabled: True`, `ledger: None`, `counts:
  {}` and one `problems` line saying the run planned a citations lane and wrote
  no citation ledger. Do not fold that into "no lane" — the lane was paid for
  and produced nothing, which is a failure worth showing.
- `ledger` is the absolute path of
  `<run>/review/citations/ledger.jsonl`, an append-only table with one row per
  reference and one per in-text use, last record per `row_id` winning. Read it
  if you want the rows; `counts` is there so you do not have to.
- `counts` holds `references`, `resolved`, `not_found`, `not_located`,
  `could_not_check`,
  `unresolved`, `mismatched`, `retracted`, `with_source_text`, `uses`,
  `uses_unmatched`, `uses_judged`, `supported`,
  `partly_supported`, `not_supported`, `cannot_tell`, `uses_unjudged`,
  `lookups`, `unreadable_rows`, `ledger` (the relative path), `stages` (what
  fill, resolve and judge each did, with the per-stage lookup and verdict
  counts) and `complete`.
- A reference lands in exactly one of four buckets, and only one of them is an
  existence claim. `not_found` counts the rows whose printed DOI no
  registration agency holds — that, and only that, is the table saying a work
  does not exist. `not_located` counts the rows where Crossref and Europe PMC
  both answered and neither listed the title: weak evidence, because books,
  chapters, theses, standards, software and preprints outside those two indexes
  land here while being perfectly real. `could_not_check` counts the rows where
  a lookup did not answer at all, which says nothing whatever.
  **Do not add them.** An integration that prints "N references do not exist"
  from the sum of the three is misreporting the run; `not_located` and
  `could_not_check` belong in the same sentence as how much of the list could
  not be confirmed, not in a count of bad references.
- `complete` is `True` only when fill and resolve finished and no matched
  in-text use is still unjudged. When it is `False`, `problems` names each
  unfinished stage in words — and those lines are in `run['problems']` too but
  never in `run['not_publishable_reasons']`. **An unfinished ledger does not
  make a run unpublishable.** Show the lines beside the findings; do not gate
  on them.
- A verdict in the table is not a finding. Findings are in `run['findings']`
  as always, written by the citations specialist after it re-checked the row
  itself. Nothing a Haiku worker wrote reaches `findings` on its own.

Two options on the run side feed this: `--citation-workers N` (0 to 12,
default 4) and `--citation-worker-model` (default `claude-haiku-5-5`). Those
worker slots are named `cite-1`, `cite-2`, … and appear in `run['agents']` and
in `protocol.json` with `assigned_model` and the observed `model` like every
other slot, with `role` `citation-worker`. They are deliberately not required
slots: a worker that never reported does not block `write_review` and is not a
reason a run is unpublishable. A session driving the workflow itself passes
`citation_workers` and `citation_worker_model` to `ingest`.

### The status rule

`peerreviewagents.runs.run_status` decides the status, and `REVIEW.md` is
rendered from the same answer: a folder cannot read one way through `read_run`
and another way in its own report. The rule, in order:

1. A status in `progress.json` that means finished wins, even if
   `runtime.json` says `blocked`. The starter records a rate limit or a token
   ceiling that fired *after* `write_review`, and a review that was written is
   written; that late limit becomes `note`, not `cause`.
2. Otherwise `runtime.json`'s status, which is how a killed run reports itself
   while `progress.json` still says `running`.
3. Otherwise `progress.json`'s status.
4. Otherwise `blocked`, if there is a cause or the start-up probe refused.
5. Otherwise `unknown` — and a status word this version does not know is
   `unknown` with a line in `problems`, never passed through.

`complete-without-findings` means the review was written with no findings in
it, and it happens two ways: the reviewers raised nothing, or the guard
quarantined everything they raised. `counts['quarantined']` is what tells them
apart, and `publishable` follows it — see below.

### Gating on `publishable`

`complete` answers whether the run finished. `publishable` answers whether the
whole review is here to show, which is the question a consumer actually has
before it puts a review in front of someone. It is `True` only when:

- `status` is `complete`, or `complete-without-findings` with nothing
  quarantined,
- `findings_source` is `editor-package` — the editor's own placement, not the
  ledger fallback,
- `guard_status` is set, so the guard recorded a verdict over these findings,
  and
- `problems` is empty.

When it is `False`, `not_publishable_reasons` holds one sentence per reason and
is never empty, so the flag is never `False` with nothing named. A complete run
whose `editor-package.json` was truncated is the usual case: the ledger findings
are real, but they are not the editor's review, and the reason says so.

**A quarantined finding does not make a run unpublishable.** The guard throws
out a finding whose quote it could not match to the block the finding names. A
guard that did that has done its job, and what is left is a whole review minus
one unanchored quote. `guard_status` is `clean` or `quarantined` — there is no
error state — and `quarantined` holds the rows themselves, so an application
that wants to weigh what was held can.

**A review with no findings in it is still a review.**
`complete-without-findings` with `counts['quarantined']` at zero means the
reviewers read the manuscript and raised nothing make-or-break, and the editor
wrote a package saying so: publishable. The same status with findings
quarantined is not publishable, because what the reviewers raised is not in the
review; the reason names how many were held back. The status alone does not
tell you which case you have — the count does.

### Exit codes

`review_main` and `report_main` return a code and never raise one. `review`
raises `ValueError` for an argument that cannot be used at all (an unknown arm,
a missing manuscript, a plugin directory with no plugin) before writing
anything, and `review_main` turns that into the message on stderr and the code
`2`.

| Code | `peerreview review` | `peerreview report` |
| --- | --- | --- |
| `0` | the review ran to a status in `COMPLETE_STATUSES` | the reports were written |
| `1` | the run was blocked | the path is not a run folder, or a run file is a symlink |
| `2` | the run never started, or an argument was unusable | argparse refused the arguments |

A blocked run is the code `1` and a full run folder to read, not an exception.

`write_reports(run)` re-renders `REVIEW.md` and `REVIEW.txt` from a run folder
with no model call. The workspace path is optional: a run folder holds its own
manuscript, so pass one only when the manuscript lives somewhere else.

### Writing files the engine reads: `peerreviewagents.workspace`

An app that keeps its own files beside a manuscript workspace — a revision, a
saved review, a scaffold import, an export — writes files the engine reads back
and verifies against a manifest. So it cannot carry its own digest, its own
JSON writer or its own review normalisation: a second implementation of any of
them is a file the engine refuses, or worse, one it accepts as something it is
not. The seven tools are in one public module rather than seven more top-level
names, and each is the same object the engine itself uses — the import is an
alias, not a copy:

```python
from peerreviewagents.workspace import (digest, read_json, write_json,
                                        normalize_review, save_review,
                                        words, text_of)
```

| Name | What it is |
| --- | --- |
| `digest(value)` | the SHA-256 every manifest entry in a workspace is checked against; takes `bytes` or `str` |
| `read_json(path)` | the one reader of a JSON file in a run or a workspace, UTF-8 |
| `write_json(path, value)` | the one writer: parents created, written to a temp file in the same directory and `os.replace`d, UTF-8 and `indent=2`, so a half-written file is impossible rather than unlikely |
| `normalize_review(paper, response, review_id)` | the anchoring step: ids, quote positions, `placement`, withheld edits, and the counts — for an app that wants the normalised findings without writing them |
| `save_review(root, response, review_id)` | that, verified against the workspace and stored at `reviews/<review_id>.json`; the same review twice is the same file, a different body under the same id is a `ValueError`, and a review of another manuscript version is refused |
| `words(text)` | the word split the import counts by |
| `text_of(element)` | an XML element's text as the import reads it, whitespace collapsed; `''` for `None` |

The whole-workspace operations stay on the top level: `import_paper`,
`verify_workspace` and `export_draft`. Use `write_json` for every file you add
to a workspace and `digest` for every manifest entry, and
`verify_workspace(root)` will accept what you wrote; writing JSON yourself is
the one way to produce a workspace the engine rejects.

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
