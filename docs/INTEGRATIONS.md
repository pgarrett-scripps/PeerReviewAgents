# Integrations

There is one integration in 0.10: the `pra` plugin for Claude Code. It is also
the only way a review runs, whether you start it from the command line or from
inside a session, so there is nothing to wire up twice.

```
peerreview review     peerreviewagents/plugin/pra   peerreviewagents.plugin.server
  (the command)      →     (workflow, agents,     →   (42 MCP tools, the ledger,
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

### The Read stage

A PDF converter is good at prose and bad at everything else. On one twelve-page
paper it produced eleven equations and about half of them were wrong, two of
them with its own confidence at `1.0`, and it produces no figure image at all —
so a figure reached a reviewer as a caption and nothing else. The Read stage is
one model that looks at the page images before any reviewer starts.

What `import_paper` writes first is a **draft**: `draft.json`, the converted
pieces with no line numbers. Nothing can cite a draft, and the server refuses
every slot claim but the reader's while one is in the folder. The reader works
through it against the pages — for every equation, table and figure it either
repairs the piece from the page image or confirms it as printed, and it must
open every page, because an equation the converter dropped altogether is
invisible in the text and plain on the page. Then `seal_manuscript` writes
`paper.md`, `map.json` and the manifest, in one call, once. **Line numbers
never move after the seal**, which is what lets a finding cite one.

**The converter's `kind` is a guess, and the stage is allowed to disagree with
it.** Each block arrives labelled from the layout, and on the first real run it
had emitted two tables as one-line paragraphs of run-together cells, a
displayed equation as a paragraph with the next equation's symbols left in the
prose after it, and one "table" that held a table, the next table's caption and
that table too. None of those is reachable by a tool that first checks the
piece is already an equation or a table, so the rule is the honest one: **the
reader may re-read any piece on a page it has viewed, as the pieces that page
actually prints, and the converter's whole reading is kept on every piece that
comes out of it.** `reread_piece` replaces one piece with several, in printed
order, each carrying `original` = the converter's reading plus `part` and `of`;
`replace_equation` and `replace_table` take a piece of any kind and change it.
Two things hold that to transcription. **Prose the stage never types**: a
`paragraph`, `footnote`, `heading` or `title` part of a re-read carries no
text, only `remove` — the spans to cut, at most three of them and together at
most a fifth of the piece — or `keep`, the one run of it this piece is, which
drops the rest and so is allowed only beside a table or an equation part of the
same re-read, whose cells or LaTeX account for what it dropped. The engine does
the cutting. So what comes out is a slice of the converter's own
characters: a displayed piece's debris can leave a paragraph and nothing a
model writes can get into one. (A span is matched on the same normal form a
citation's quote is matched on and has to read there exactly once, and
`view_page` returns that page's pieces whole, because the listing cuts a long
paragraph off at `CONTEXT_CHARACTERS` and a span cannot be named inside what
is not shown.) And a caption is never written over by a single replacement,
because the piece it names would be left with none. `withdraw_piece` takes out
a piece the stage itself added, and refuses the converter's.

**Debris the converter left as a piece of its own is folded into the piece it
is debris of.** A span reaches debris inside a paragraph; it cannot reach a
piece. On real papers the converter emitted three headings reading `with {`,
`qn,s,d else` and `|A|2 d=1` — each the edge of a displayed equation on the
same page — and read a bar chart's axis numbers as a paragraph above its
caption. Nothing asked for any of them to be accounted for and
`withdraw_piece` refuses a converter piece, so all of them reached sealed
manuscripts as headings and lines of prose. `merge_equations` now takes
non-equation members, and `describe_figure` and `reread_piece` take `absorb`,
a list of neighbour pieces: each one leaves the document and its whole
converter reading is kept under `original['absorbed']` on the piece that
absorbed it, so it is a fold and not a deletion. A re-read writes several
pieces, and **the fold is recorded on one of them: the first part that is not
prose**, which is the displayed piece the debris was printed as part of. One
fold is one fold — on every part, the one folded heading would read to
whatever renders the map as one fold per part, three parts and three headings,
and `part` and `of` already say the parts are one piece between them. What may
be absorbed is
narrow, and each refusal is a way of being sure this is debris and not the
paper: the converter's own piece rather than one the stage added, prose by
kind (an accountable piece is repaired, confirmed or described in its own
right), on the page being viewed, at most `MOST_ABSORBED_CHARACTERS` of
reading, contiguous with what absorbs it, and at most `MOST_ABSORBED` in one
call. `reread_piece` also refuses `absorb` when the re-read writes prose
alone, because then there is no displayed piece for the debris to be part of.
Two equation pieces named in one `merge_equations` are held to neither
adjacency nor the page: the converter labelling both equations is its own
evidence, and one of them may be the tail of the equation at the top of the
next page.

The first design let a re-read *retype* prose, with a check that every
character kept was one the converter read, in order. It does not hold: almost
any short sentence is a character subsequence of a long paragraph, so
re-reading an abstract as its own text plus "This result is highly
significant." was accepted and that sentence reached the sealed line. A
deletion the engine performs cannot have that failure, which is why the reader
names spans instead of writing text.

The stage is one slot, `reader`, planned with every other: `--reader-model`
(default `claude-sonnet-5-5`, because the job is reading pictures),
`PEERREVIEW_READER_MODEL`, or `reader_model` on `ingest`. Its tool allowance is
not a constant — it is `10 + pages + 2 × pieces`, capped — since a four-page
letter and a forty-page paper with sixty figures are not the same job.

**There is no way to run a PDF review without it.** The stage is not a
setting: every review of a PDF reads the pages, because the converter's
equations and tables are its reading of a picture of them, a figure is a
caption with nothing behind it, and a page the text layer returns nothing from
cannot be imported at all without a reader to transcribe it (below). A
manuscript sealed from the converter's own text — the equations keeping
today's marker telling the reviewer to open the page, no figure described,
`manifest['ai_read']` as `{"read": false, "reason": …}` — is now reached two
ways only: the recorder's `seal_as_converted` after a reader failed twice, and
an app's own source through `write_manuscript`. One code path writes the base
however it was reached, so a run never has a half-written manuscript.

**A page the converter returned nothing from is the reader's work.** The
pinned converter returns no block at all for a page whose `/Rotate` is not
zero, and none for some upright full-page tables, so a paper can convert to a
manuscript with a page missing and nothing to say so. The import holds the
pages it got blocks from against Poppler's text layer and records every page
that reads 25 words or more and converted to nothing. With the Read stage on
those pages are outstanding items of their own: `read_draft` names them under
`unconverted_pages`, the reader views each one and transcribes what it prints
with `add_piece` — which may add a `paragraph` there and only there, a
`caption` anywhere — and `seal_manuscript` is refused while one of them has
nothing
on it. On those pages, and only those, `view_page` also returns `text_layer`:
the words Poppler read off the page's own text layer with the whitespace
collapsed, which the import already keeps because that is what it counted the
page's words from. It is labelled in the answer as characters to check against
— a footnote letter, a subscript, a minus sign in a small or rotated cell — in
no reliable order and with no layout, and explicitly not something to
transcribe or to read a reading order off. Two runs over one paper read the
same footnote letter off the same sideways table and both got it wrong, at a
rendering already at the long-edge limit a vision model accepts. The manifest then names the pages and the pieces added off them, in
`ai_read['lost_pages']` and in `known_gaps['converter_pages']`, because that
text is a model's transcription and not the converter's reading of anything.
With nothing read — `seal_as_converted` after a reader failed twice — nothing
is going to put those pages back, so the seal is **refused**, by page, naming
the converter to report them against. A paper with such a page is therefore one
only the Read stage can import at all. (`write_manuscript` never
meets this: its pieces are the app's, and no PDF was converted.)

`write_manuscript` needs no reader and seals at once: an app that parses its
own Typst or LaTeX source already has the equation, so there is nothing to read
off a page image. Its manifest records `{"read": false, "reason": …}` too. A
run opened over such a folder — or over one a run already read and sealed —
has no Read stage, and so no reader slot: `ingest` returns `reader: null`, no
claim code is minted for it, no model is planned for it, and asking for the
slot is refused in words that name the seal. The same refusal answers a reader
that claims after the manuscript was sealed around it.

#### What the manifest and the map say about it

`manifest['ai_read']` is the record of the stage:

```json
"ai_read": {"read": true, "reader_model": "claude-sonnet-5-5",
            "reader_slot": "reader", "attempt": 1,
            "pages_viewed": 12, "pages": 12,
            "actions": {"repaired": 6, "confirmed": 5, "merged": 1,
                        "described": 9, "added": 4, "reread": 3},
            "gone": {"absorbed": 4, "merged": 1, "withdrawn": 1},
            "tool_calls": 47,
            "note": "Table 4 was inside the converter's Table 3 and is now a "
                    "table of its own. Figure 2's y-axis label is unreadable "
                    "in the image.",
            "lost_pages": [{"page": 8, "words": 266, "rotation": 0,
                            "added": [{"piece": "p0211", "document": "paper.md",
                                       "lines": [120, 126]}]}]}
```

**`ai_read['actions']` counts the pieces the manuscript has** — it is the
pieces of the sealed documents, counted by what was done to each — and
**`ai_read['gone']` counts the pieces it no longer has**, kept as they went
rather than walked at the seal, because a piece folded into another or taken
back out is in no document to be counted. `absorbed` is converter pieces
folded into the piece they were debris of, `merged` the other halves of an
equation put back together, `withdrawn` pieces this stage added and took out
again. Keys with no count are left out, so `{}` is a stage that folded
nothing. **The two do not add up with each other**: a fold moves a piece out
of `actions` and into `gone`, and the piece that holds the fold is counted in
`actions` under what was done to *it* (`merged`, `described`, `reread`). Each
fold is also in the map, on the piece that holds it, under
`original['absorbed']` and `original['merged']`; `gone` is the one place a
number for them survives.

**`ai_read['note']` is the reader's own account of the document**, in its
words, capped at 2000 characters and empty when it filed none. It exists
because the first real run's reader knew two things no reviewer could work out
— that the converter had dropped a table, and that two others were garbled —
and said them in a report that reached no file. It is served above the
manuscript wherever one is read, labelled as that model's account rather than
the authors' text, and `read_run(...)['manuscript']['note']` carries it for an
application. It is not a log of the stage: `actions` and the tool log are that.

**`ai_read['lost_pages']` is written on every seal**, empty list included, so
a folder that lost no page says so rather than leaving a consumer to guess
whether the key is missing because nothing was lost or because the manifest
predates the count. Each entry's `added` carries the lines the transcription
became — document and `[first, last]`, the same anchor a citation uses — and
not only the draft piece id, which names nothing after the seal: `map.json`
has no piece ids, and a base reader asking "where did that page end up" has
only lines to ask with. `known_gaps['converter_pages']` is the same fact as a
sentence for a reviewer, with those lines in it; the data is here.

`manifest['pieces_per_source']` counts the rows by where their text came from —
`{"text": 184, "ai-reconstructed": 6, "ai": 9}` — and that is the same
`source` each row in `map.json` carries:

| `source` | What the row's text is |
| --- | --- |
| `text` | the converter's own reading, untouched |
| `ai-reconstructed` | an equation or a table a model rewrote from the page image. The converter's reading is kept on the row, in `original`, so a repair can be audited |
| `ai` | a figure description, or a piece the converter missed entirely and a model added |

A figure description is its own piece and its own row, of kind
`figure-description`, on the line under the caption. **Every served line whose
`source` is not `text` carries a marker, whatever kind it is** — the marker
used to be written for equations, tables and descriptions only, so a paragraph
or a caption transcribed off a lost page reached the reviewer looking like the
converter's own text. A repaired equation says it was read from the page image
by that model, with the page; a piece the converter missed says so too; a
description says it is an AI description of the image rather than the authors'
words. In a manuscript no model read the pages of — an app's own pieces
through `write_manuscript` — the marker reads `[written by <model>]` instead,
because there is no page image to send the reviewer to. The `view_page` gate on an equation
finding stays as it is — a reviewer who disputes an equation sees the same
rendering the reader worked from, not a second one.

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
is `Workflow`, `Skill`, `TodoWrite` and the 42 `peerreview` tools, and nothing
else. No `Read`, `Write`, `Edit`, `Bash`, `WebSearch` or `WebFetch` reaches a
reviewer, which is why a quotation cannot come from anywhere but the
manuscript the server served.

## Other MCP clients

The server is a plugin server. It takes `--plugin-root`, derives a per-run salt
for agent tokens, and expects the workflow to claim slots in order, so pointing
another client at it directly gives you the tools without the protocol that
makes them meaningful. There is no supported generic-client configuration in
0.10, and the standalone `peerreview-mcp` server that offered one was deleted
with the rest of the 0.8 surface.

If you want a review from another client, have it run `peerreview review` as a
subprocess and read the run folder. The command is the integration boundary.

## From Python: the supported surface

An application written in Python does not have to shell out. These names are
the supported surface, all importable from `peerreviewagents` itself, together
with the two modules named below it — `peerreviewagents.issues`, the shape of a
review, and `peerreviewagents.workspace`, the files the engine reads back.
Nothing else in the package is: if what you need is only reachable from
`peerreviewagents.plugin` or `peerreviewagents.engine`, treat it as a gap in
this list and say so, because those modules change without notice.

```python
from peerreviewagents import (
    review, review_main, report_main,      # run a review; the two commands
    review_arms,                           # what each arm runs, before a run exists
    read_run, write_reports,               # read and re-render a run folder
    read_citation_ledger,                  # the citation table's own rows
    validate_prior_issues,                 # check an issues file before publishing it
    locate_run, NotARunFolder,             # resolve a run folder, and the error
    import_paper, verify_workspace, export_draft,
    write_manuscript,                      # write a workspace from your own pieces
    read_manuscript, PaperBase,            # read one back, and the type both reads return
    cited_text, citation_uses,             # read an anchor back; build the use rows
    plugin_directory, claude_code_version,  # check an installation
    review_parser, report_parser,           # the real parsers behind the commands
    RUN_DIRECTORY, MANUSCRIPT_DIRECTORY,   # "review" and "manuscript"
    BASE_SCHEMA_VERSION, MAIN_DOCUMENT,    # a workspace's schema, and "paper.md"
    RUN_STATUSES, COMPLETE_STATUSES, CLI_COMMANDS, FINDING_SCHEMA_VERSION,
    ISSUES_SCHEMA_VERSION,                 # the stamp on issues.json
    ISSUE_SEVERITIES, ISSUE_CATEGORIES, ISSUE_STATUSES,  # an issue's vocabularies
)
```

Those four `ISSUE_*` names are the whole issue vocabulary on the top-level
surface, and they are the ones to validate against rather than string literals
of your own. The rest of the shape — `ISSUE_FIELDS` and `MEMBER_FIELDS` (every
field of an issue row and of a member), `MAX_ISSUE_BATCH`, `issue_batch_limit`,
`schema_problem`, and the grouping helpers `required_issues`,
`suggestion_issues`, `live`, `closed_issues`, `carried_forward`, `by_category`
and `counts` — is in `peerreviewagents.issues`, which imports nothing from the
rest of the package and is stable in the same way.

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

### What each arm runs: `review_arms()`

An arm is a setting of one workflow, and what it runs decides which arguments
it accepts. `review_arms()` answers that before a run exists — one row per
arm, in name order:

```python
from peerreviewagents import review, review_arms

arms = review_arms()
if arms[chosen]["code"]:                   # will this arm review code at all?
    review(pdf, out, arm=chosen, code="~/analysis")
else:
    review(pdf, out, arm=chosen)           # and grey out the button, don't guess
```

| Key | What it says |
| --- | --- |
| `reviewers` | how many core reviewers the arm fixes, or `None` where the count is yours to choose with `reviewers=` |
| `specialists`, `editor`, `revision` | whether it runs the specialist tracks, pools findings through an editor, gives the reviewer a revision pass |
| `code` | whether it accepts `code=` |
| `citations` | whether it can keep a citation ledger at all |
| `default` | whether this is the arm a review runs when none is named |
| `summary` | the one line `--arm` prints for it |

`code` is the one that was unaskable before. The code review is a specialist
lane, so an arm that runs no specialists refuses `code=` outright — with
`ValueError` from `review`, and a rejection from `ingest` — rather than
accepting it and reviewing none of it. That is deliberate: a run given code and
reviewing none would report a code review that never happened. Ask this instead
of keeping a list of arm names of your own, which is what every wrapper was
doing and what goes stale the next time an arm is added.

`citations` is the arm's half of whether `run["citations"]["enabled"]` can be
true; the other half is yours, since the `citations` track must also be among
the `tracks` you chose.

The rows are fresh dicts, so filter, render or keep them as you like. What they
are built from — the tools each lane is given, the call budgets — is not on
this surface.

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
for issue in run["issues"]:
    print(issue["rank"], issue["severity"], issue["name"],
          f'raised by {issue["raised_by"]}')
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
| complete | `status` `complete`, the issues from `issues.json`, the findings from the ledger |
| complete without findings | `status` `complete-without-findings`, `findings` empty, `cause` `None` |
| blocked by the server | the cause from `progress.json`, findings from the ledger |
| killed by the starter | the cause from `runtime.json` — token ceiling, timeout, rate limit, session error — even though `progress.json` still says `running` |
| start-up blocked | `stage` `startup`, `startup_problems`, and the identity the starter wrote |
| killed mid-write | everything that could be read, and each file that could not in `problems` |

Keys: `status`, `complete`, `publishable`, `not_publishable_reasons`, `blocked`,
`cause`, `note`, `stage`, `findings`, `issues`, `issues_source`, `prior_round`,
`findings_source`, `counts`, `quarantined`, `citations`, `guard_status`,
`escalations`, `escalation_rulings`, `summary`, `verdict`, `limitations`,
`identity`, `manuscript`, `code`, `stages`, `runtime`, `agents`, `usage`,
`reports`, `startup`, `startup_problems`, `problems`, `files`, `run`, `review`.
The same 36 keys come back for every folder, in every state, so a consumer
never has to test whether a key is there.

#### What state the manuscript is in

`run["manuscript"]["state"]` is one of `MANUSCRIPT_STATES`, and it is a
different question from the run's status — a run can be blocked with a sealed
manuscript, or complete with none to show.

| `state` | What the workspace holds |
| --- | --- |
| `absent` | nothing: the run stopped before the manuscript was imported |
| `draft` | the converted pieces, unsealed. No line numbers, nothing can cite it — the Read stage started and did not finish |
| `sealed-by-reader` | the base, with a model's reading of the pages behind it. `reader_model` names it |
| `sealed-without-read` | the base, with no page read at all: a reader that failed and sealed as converted, or an app's own source through `write_manuscript`. `reason` is the manifest's own words for which |

Beside it: `sealed` (that question already answered), `reason`,
`reader_model`, `lost_pages` — each page the converter returned nothing from,
with the document and lines its transcription became — `note`, the reader's own
account of the document if it left one, and `documents`. Before
this, telling a run whose read stopped from one that never started meant
looking for `draft.json` by name, which is this package's working file and not
part of the folder a consumer is promised.

#### Whether code was reviewed beside the paper

`run["code"]` answers it. `reviewed` is `False` for every run that was given no
code — which is most of them, and all of them before 0.10.0 — and the rest of
the block is empty rather than absent, so a page renders it without testing for
keys.

| Key | What it says |
| --- | --- |
| `reviewed` | whether this run read code at all |
| `kind`, `source` | `git-clone` or `folder`, and where it came from as the caller named it: the URL for a clone, the resolved path for a folder |
| `commit`, `branch`, `dirty` | the state the code was in. `dirty` is `None` for a snapshot of something that is not a repository |
| `snapshot` | the absolute path of `<run>/code/`, which holds `manifest.json` and the text under `files/` |
| `files_in_snapshot`, `files_skipped` | what was taken, and how many paths were left out — a binary, a data file, one over the size caps, one that may hold secrets, a run folder of this package, or a whole folder or symbolic link never walked into, which each count once |
| `truncated` | why the snapshot stopped early, in words; empty for one that holds the whole of what it was pointed at |
| `manifest` | the snapshot's own manifest, which names everything either way, with a digest per file taken and a reason per path skipped |
| `files_opened`, `opened`, `searches` | what a reviewer actually did with it |

The last row is the one worth printing beside the first. A lane that opened no
file read the paper and nothing else, and a page that said only "code:
reviewed" could not have told anyone so. `REVIEW.md` prints the same line from
the same record.

**Reviewing a repository from inside it is the normal case, and the snapshot
holds none of this tool's own output.** A workspace keeps its run folders under
the repository it reviews, so the folder passed as `code` usually contains
them. The run being written is left out of the snapshot, and so is every other
run folder under that path — recognised by what the engine writes in one
(`review/protocol.json`, `review/startup.json`, `review/findings.jsonl` and the
rest of `RUN_MARKERS`) rather than by what it is called, since the name is the
caller's. Each is one row in `manifest["skipped"]`, with the reason, like any
other folder never walked into. A `code` path that *is* a run folder, or is
inside the out folder, is refused instead: there would be no code in that
snapshot, only the review.

A finding from that lane carries `code_evidence` — `path`, `start_line`,
`end_line` and `text` — and its `basis` is `code-checked`: the server found
that text at those lines in the snapshot before it stored the finding, and
refused it outright otherwise. The paper quote is there as on every other
finding, anchored the same way, so highlighting one is unchanged.

`path` names a file of the snapshot, and the lines are that file's lines as the
snapshot holds them. For a notebook they are lines of the extracted cells, not
of the JSON: the manifest row and `read_code` both carry `extracted`
(`"notebook cells"`, or empty for a file stored as it is), and a `# %% notebook
cell N (code)` marker in the text is where each cell begins. An application
showing code evidence reads it out of `<run>/code/files/<path>`, which is the
text the lines were counted in: line endings are folded to LF on the way into
the snapshot, so a CRLF repository is numbered the way its author's editor
numbers it, and the manifest row's `bytes` and `sha256` are of that stored
copy rather than of the original file.

#### Highlighting a citation

Everything in a run that cites the manuscript says where it is the same way:
a finding in `run["findings"]`, a member of an issue, a row of the citation
ledger. Each carries `document`, `line`, `end_line`, `start` and `end`, and the
rule they all keep is

> `anchor_text == line_text(line)[start:end]`

for the ordinary citation, which is inside one line. A quote that crosses a
line break — a span of a table, two lines of an equation — has `end_line`
greater than `line`; then `start` is where it begins in `line` and `end` where
it ends in `end_line`, and it covers each line between them whole.
`peerreviewagents.cited_text` is that read, both cases, against a workspace you
loaded with `read_manuscript` or `verify_workspace`:

```python
from peerreviewagents import cited_text, read_manuscript, read_run

base = read_manuscript("run-output/manuscript")
for finding in read_run("run-output")["findings"]:
    print(finding["line"], finding["start"], finding["end"],
          cited_text(base, finding))     # == finding["anchor_text"]
```

Use it rather than searching the line for the words again: the engine already
did that search, against the normal form, and a second implementation in the
app gets the duplicate quote and the line break wrong. `anchor_text` is the
manuscript's own characters, so it is what a viewer highlights; `quote` is what
the reviewer typed, and may differ from it in a dash, a quotation mark or a
letter's case. All of `line`, `end_line`, `start`, `end` and `anchor_text` are
`null` on a citation that was never placed — a quarantined finding has words
and no position — and `cited_text` returns `""` for one, rather than the top of
the paper.

### The issues

`issues` is the review: one row per problem with the paper, in the order
`issues.json` holds them, and it is the only list a consumer should render. A
row is

```python
{"id": "i03", "name": "...", "severity": "major", "category": "statistics-and-inference",
 "rank": 1, "statement": "the paragraph the authors read", "raised_by": 2,
 "members": [{"finding_uid": "...", "slot": "core-2", "track": "core", "quote": "...",
              "document": "paper.md", "line": 118, "end_line": 118,
              "start": 34, "end": 61, "anchor_text": "the words as the file holds them",
              "evidence_lines": [118, 121], "fingerprint": "9f2c…",
              "severity": "major", "kind": "issue", "basis": "two-quotes",
              "escalate": False, "title": "..."}],
 "audits": ["audit_stats.md"],
 "rebuttal_document": None, "rebuttal_line": None, "rebuttal_quote": None,
 "prior_id": None, "prior_rank": None,
 "status": "new", "status_reason": None,
 "text_unchanged_reason": None, "surviving_quotes": []}
```

**Every one of those eighteen fields is on every row**, live or closed, edited
or synthesised, and a field that does not apply to a row is `None` — or `[]`
where the field is a list. `peerreviewagents.issues.ISSUE_FIELDS` is that list
and `MEMBER_FIELDS` is a member's seventeen, so a consumer reads a field by name
and never tests whether a key is there. Earlier releases left the inapplicable
ones out; 0.10 does not.

- `severity` is `major`, `moderate` or `minor`. Major and moderate are the
  authors' **required changes**, in `rank` order; minor are **suggestions**,
  which a renderer groups by `category`. `rank` is 1..N over the live issues of
  the run, with no gaps and no ties, and it is `None` on a closed row — a row
  holding the prior round's rank would collide with this round's order, so the
  rank it had then is in `prior_rank` instead.
- `members` are the findings the issue was raised from, kept as its evidence:
  the slot that filed each one, the quote it anchored, the document and line
  the engine placed that quote on, **where in that line the quote sits**
  (`start`, `end`, `anchor_text` — see [Highlighting a
  citation](#highlighting-a-citation)) and the lines it cited as evidence.
  `raised_by` is counted from them, so two reviewers raising one problem is one
  issue that says it was raised twice — never two rows. A closed row has no
  members and `raised_by` of zero.
- The rest of a finding — `comment`, `page`, `bbox`,
  `basis_reason`, `counterevidence`, `source_urls` — is **not** copied onto the
  member. Join `members[*]["finding_uid"]` to `read_run()["findings"]` (or to
  `findings.jsonl`) for those, keyed on the same uid. The member carries what a
  reader of `issues.json` alone needs to go to the manuscript, and the ledger
  carries the finding.
- `id` is stable across rounds: an issue the editor matched to a prior one keeps
  that round's id. `finding_uid` is not — it is a digest of the paper's id and
  the slot, both of which move between runs.
- `audits` are the specialist audits a member's track wrote, as **bare file
  names** (`audit_stats.md`) inside the run's `review/` folder — not paths. Join
  them to `reports['audits']`, which maps each track to that file's full path,
  or to the review folder in `read_run()['review']`. The audits are evidence and
  stay where they are; the issue names them.
- `rebuttal_quote` is set on a `minor` issue that holds a `computed` finding —
  one whose arithmetic the server checked against the manuscript. Calling such
  a finding a suggestion takes a passage of the manuscript that answers the
  receipt, quoted exactly as the manuscript reads; `rebuttal_line` is the line
  the engine placed it on, and is `None` where the quote was not placed.
  `rebuttal_document` is **which document that line is in**, read exactly as a
  member's `document` is: `None` for `paper.md`, the supplement's file name
  otherwise. A rebuttal quoted from a supplement used to be looked for in
  `paper.md` and refused, and a line number with no document beside it is a
  line of the wrong file. The server refuses the issue without a quote. All
  three are `None` on every other issue, and a renderer that shows a suggestion
  over a receipt should show the rebuttal with it.

#### The five statuses

`status` is one of `new`, `persisting`, `regressed`, `resolved`, `unverified` —
`peerreviewagents.ISSUE_STATUSES`, in that order. Only a run given a prior round
(`peerreview review --prior-issues`) has anything but `new`, and **nothing
becomes a status by omission**: the editor rules on every issue of the prior
round, by matching it or by naming it, or the review is refused. `--prior-issues`
on an arm that runs no editor is refused by the command itself, before the run
starts — there would be nobody to rule on the prior round.

| `status` | What it says | `rank` | `members` |
| --- | --- | --- | --- |
| `new` | This round is the first to raise it | 1..N | the findings behind it |
| `persisting` | The prior round raised it and it is still wrong | 1..N | this round's findings |
| `regressed` | The prior round raised it and it is worse | 1..N | this round's findings |
| `resolved` | The editor read the revision and it is fixed | `None` | empty |
| `unverified` | Nobody raised it again and nothing shows it was fixed | `None` | empty |

`status_reason` is the editor's one line for any status but `new`: why it still
stands, or what changed. The last two are the closed statuses
(`issues.CLOSED_STATUSES`) and they are not carried forward alike. `resolved` is
reported once — in the round that resolved it — and then dropped, so nothing is
recounted round after round (`issues.carried_forward`). `unverified` is carried,
because that status exists precisely because it is *not* a fix: a later round
rules on it again like any other open issue. Its row holds no `members` — those
findings were the earlier round's — so what it carries instead is
`surviving_quotes`, and the round that does rule it `resolved` tests the ruling
against those. An id that was `resolved` stays in `issue_ids_used` for good.

`resolved` is the stronger claim and the server checks it. Where a prior
member's passage is still in the new manuscript, `resolved` takes a
`text_unchanged_reason` as well, and the passages the server found are published
in `surviving_quotes` so a reader can see what the ruling was made against.
Without that reason the status is `unverified`: not raised again, not shown
fixed. `surviving_quotes` is `[]` and `text_unchanged_reason` is `None` on every
other row.

A prior round's line numbers mean nothing in a manuscript imported afresh, so
the passage is looked for twice over: first the quote itself, **verbatim**, and
then the member's `fingerprint`, the digest of the whole passage it was found
in, which still matches where the converter has since broken the lines
somewhere else. Each row of `surviving_quotes` is `{"finding_uid", "quote",
"document", "line", "fingerprint", "matched_by"}`, and `matched_by` is `"quote"`
or `"fingerprint"` — a fingerprint match says the passage is unchanged without
claiming the quoted words were found at that line.

#### The rest of the document

`issues_source` says where the issues came from: `editor` where an editor closed
a pooled review, `server` where nothing did — `draft-check` and `single` write
one issue per finding, so every arm reads in the same shape.

`issues_source_reason` is why, in one line, and `None` wherever
`issues_source` is `editor`. Four different runs end with `server` and they are
not the same run: an arm that runs no editor; a ledger past the 200 findings the
editor path supports, which is refused the editor slot by name; an arm with an
editor nobody claimed; and an editor that claimed the slot and never got a
review accepted, which carries the number of submission calls it made. The
reason is printed in `REVIEW.md` under the issues, repeated in each
server-written issue's `status_reason`, and carried in `not_publishable_reasons`
— so an application never has to guess which of the four it is holding. Before
0.10.0 all four printed the first.

`issue_ids_used` is every issue id this run and every round before it has
spent, sorted — live, resolved and unverified alike. **An id is never reused.**
A resolved issue is dropped from the next round's carry, so the carry alone does
not say what `i03` meant two rounds ago; this list does, and the next round
reads it back and mints around it. A consumer that stores issues across rounds
can treat an id as permanent: one id is one problem, for the life of the
manuscript. A reader that writes its own `issues.json` for a later round to
match against must carry the list forward, or that round will mint an id an
earlier one already reported.

A prior-round file handed to `--prior-issues` is validated before the run opens
and refused by name, with the path, if it cannot be matched against: the
schema stamp must be a known one, every row must be an object with a **string**
`id` (`7` and `"7"` are not the same id), no id may appear twice **across all
rows** — open and closed alike — every `members` and `surviving_quotes` must be
a list, and `issue_ids_used` must be present and a list of ids in this engine's
own form (`i01` upwards). Every `issues.json` this version writes satisfies
that; a hand-written or edited file that drops `issue_ids_used` is refused
rather than read with the ids unreserved.
`peerreviewagents.validate_prior_issues(document)` is that same check, callable
on its own — see [Checking a file before you publish
it](#checking-a-file-before-you-publish-it) — so a file can be held against it
before it is published rather than at the start of the next review.

A run that degrades **after** it opened — a ledger that passed the editor's
maximum, an editor slot nobody claimed, an editor that never closed — still
accounts for the prior round in the file it writes: its own issues are minted
above every id in `issue_ids_used`, and each prior issue is carried as
`unverified` with a `status_reason` saying no editor ruled on it and why there
was no editor. Nothing is reported as `resolved` by a round that had no editor
to rule it so.

`prior_round` is `None` for a first round, and for a later one it is
`{'file': 'issues.json', 'digest': '<16 hex>', 'issues': N, 'tally': {...}}`:
the prior file's **name**, a digest of its contents, how many of its issues were
carried into this round, and this round's issues by status. It holds no machine
path — a run folder is published, and where the previous round sat on disk is
not the authors' business. Two readings of the same prior round have the same
digest; two different files never do.

`findings` stays beside it, and it is always the whole ledger: nothing is hidden
from a reader, so the ledger *is* the finding set rather than a longer list the
review draws from. `counts` carries both tallies — `findings`, `issues`,
`required`, `suggestions`, `resolved`, `unverified`, `issue_members`,
`by_severity`, `by_category`, `by_status` and `quarantined`, plus
`findings_delivered` and `findings_submitted`. `issue_members` is the number of
distinct findings the issues hold between them, and it is called that
everywhere it appears: in `issues.json`, in the `issues` tally `progress.json`
carries while the run is still going, and here.
`by_status` counts all five statuses, always with all five keys.

`reports` names the files on disk: `markdown` and `text` are `REVIEW.md` and
`REVIEW.txt`, `issues` is `issues.json` itself, `editor_log` is
`editor-log.jsonl`, and `audits` maps each track to its audit. Each is `None`
where the file is not there. `REVIEW.md` is rendered from `issues.json` and from
nothing else, so an application that renders the issues itself and one that
shows `REVIEW.md` are showing the same review.

#### `issues.json` itself

`read_run` is the supported way in, and these are the document's own top-level
keys for anything that reads the file directly. `schema_version` is the one to
check first: `peerreviewagents.ISSUES_SCHEMA_VERSION` is what this release
writes (`review-issues-v4`, which added an issue's `rebuttal_document`), and it
is the only one it reads: every earlier stamp — `review-issues-v1`, which held
evidence as block ids, and `review-issues-v2` and `review-issues-v3`, both
written and superseded inside the unreleased one and never shipped, the latter
carrying a `rebuttal_line` with no document beside it — is refused by name
rather than read as far as it happens to parse. A file you published from an
earlier release is not loadable as a prior round: re-render it from that run's
folder, or raise its issues again.
`issues.schema_problem(document)` is the rule the reader, the prior-round loader
and the renderer all go through, so a consumer can use the same one.

| Key | What it holds |
| --- | --- |
| `schema_version` | the stamp above; refuse what you do not know |
| `issues_source` | `editor` or `server` |
| `issues_source_reason` | why it is `server`, in one line; `None` where an editor closed the review |
| `issue_ids_used` | every issue id this run and the rounds before it have spent; an id is never reused |
| `summary` | the editor's paragraph above the issues |
| `issues` | the rows described above, live ones first |
| `counts` | the tally described above, less `findings` and `quarantined` — those are ledger-wide, and the reader adds them |
| `verdict` | `significance`, `strength_of_evidence`, `reasoning`, `basis`, `limitation` — words, never a score; `None` where no editor closed the review |
| `limitations` | what the editor says the review could not do |
| `escalation_rulings` | `{finding_uid, ruling}` per escalated finding, one each |
| `escalated_count` | how many findings were escalated |
| `prior_round` | the shape above, or `None` on a first round |
| `stages` | the ledger: which slots were required, reported, silent |
| `audits` | every specialist audit in the run, as bare file names |
| `findings_by_track` | the findings that reached the editor, counted per track |
| `findings_delivered`, `findings_submitted` | what reached the editor, and what was filed |
| `quarantined_findings`, `quarantined_findings_count` | the rows the guard held back, and how many |
| `review_guard_status` | `clean` or `quarantined` |
| `reviewer_count`, `reviewer_provenance` | how many core reviewers, and how they were drawn |
| `arm`, `synthesis_policy` | the arm that ran, and whether an editor pooled it |
| `finding_schema_version`, `workflow_version` | the ledger's stamp and the workflow's |

Every one of those keys is written on every run, and so is every field of every
issue row: a run that had nothing of the kind writes the key with `None` —
`prior_round` on a first round — or with an empty list or object.

**No file meant to be published holds an absolute machine path.** `issues.json`
and `REVIEW.md` are the two an integration hands on — to the authors, to a
journal, into a repository — so every path in them is relative to the run folder
(`review/editor-log.jsonl`, `audit_stats.md`) or is a bare file name
(`prior_round['file']`, `reports`, `audits`), and the run folder itself appears
as its own name and nothing more. A test scans both files whole for the
machine's paths, rather than field by field, because the two that leaked got in
through fields nobody thought of as paths: the editor's trace inside `stages`,
and the run folder printed in `REVIEW.md`'s header.

`manifest.json` is the one exception and it is deliberate: it keeps the
manuscript's **absolute** path, and `read_run()['identity']['pdf']` hands it on
as it stands. The manifest is the run's own record of what it read — the pdf
lives outside the run folder, so a relative path would name nothing — and it is
not in the published set. Strip it yourself if you publish the folder whole.

`REVIEW.md` is rendered from this file alone, and `peerreview report <run>`
re-renders it from the file on disk. A folder with no `issues.json` renders "No
review was produced", which is what a run whose editor never closed looks like.
Two cases are refused instead, with `REVIEW.md` left exactly as it is: a
`schema_version` this release does not know, and a folder from an engine that
wrote its review as `editor-package.json` with no `issues.json` beside it. Both
refuse before anything is written, because re-rendering either one would print
"No review was produced" over a review that exists.

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
  `finding_uid`, in the order the findings were first seen. The ledger is the
  finding set — nothing is hidden from a reader, so there is no shorter list to
  prefer — and the issues are how that set is presented. `findings_source` says
  which of the two the reading stands on: `issues` when `issues.json` could be
  read, so what came back is the review (this includes the empty review of a
  manuscript nobody raised anything about); `ledger` when it could not be read,
  or when it holds no issue over findings that *are* in the ledger — then what
  came back is the raw findings and not the review, and `publishable` is
  `False` for that reason; and `none` for a folder that holds neither.
  **When no row of the ledger can be read and `issues.json` can**, the members
  of the issues are the fallback: `findings` is rebuilt from
  `issues[*]["members"]`, so every finding an issue holds is still there with
  its slot, quote and lines, and `problems` names the file or the lines that
  could not be read. A ledger that is only torn in places keeps the rows that
  read, and the torn ones are counted and named in the same way. The fields the
  ledger alone carries — `comment`, `start`, `end`, `page`, `bbox`,
  `category` — are absent from those rebuilt rows, and
  `findings_source` is still `issues`: the review is intact, the ledger behind
  it is not.
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
  directory itself, a usage receipt, a specialist audit, `issues.json`: all the
  same way, with a `ValueError` naming the path. A symlink in `issues.json`'s
  place is some other file's contents presented as this run's result.

### Checking a file before you publish it

A consumer that writes an `issues.json` for a later round to read — a revision
tool, a round-keeping service, anything that carries a review forward — used to
have one way to find out whether that round could read it: start the round. The
rules lived inside the loader behind `--prior-issues`. They are callable now:

```python
from peerreviewagents import validate_prior_issues

problems = validate_prior_issues(document)   # the parsed JSON, not a path
if problems:
    raise SystemExit("\n".join(problems))    # do not publish it
```

`validate_prior_issues(document, label='issues.json')` returns **every** problem
that would stop the document being read as a prior round, in words, worst first;
an empty list means `peerreview review --prior-issues` will load it. `label` is
the name the problems are written against, so a tool that checks several files
can say which one each problem is about. An unknown `schema_version` is the only
problem returned on its own: nothing else in a document this version cannot read
is worth reporting.

It is the check the loader runs, not a copy of it. The loader refuses with this
list's first problem and asks nothing outside it, so a file this says nothing
about is a file the next round loads, and a rule added to the loader shows up
here in the same release rather than drifting out of step.

What it requires of a prior file, in the order it says it: a `schema_version`
this release reads; every field the type a reader expects; an `issues` list,
present — a prior round is the rows it holds; a non-empty text `id` on every
row, because the id is the only thing a later round matches on; no id twice; an
`issue_ids_used` that is present and holds ids of this engine's own form, so the
next round mints above every id already spent; and at least one row left once
the resolved ones are dropped. A list field held as `null` — `members`,
`surviving_quotes`, `audits` — is malformed rather than empty: every one of them
is a list in what this engine writes, `[]` where it does not apply, so `null`
says something this version does not read.

`peerreviewagents.issues` holds the readers behind it, for a consumer that has
to display a document it did not write:

| Name | What it gives you |
| --- | --- |
| `rows_of(document)` | the issue rows: the objects in `issues`, in order, with anything else dropped |
| `objects_in(rows)` | the same filter over a list you already hold |
| `members_of(issue)` | the member findings of one issue; a member that is not an object is dropped |
| `quotes_of(issue)` | its `surviving_quotes`, the same way |
| `lines_of(row)` | the lines a row cites: its own line, the lines of a range, and its `evidence_lines` |
| `audits_of(issue)` | the specialist audits named on it |
| `field_problems(document)` | the malformed fields, in words — the lines `read_run` reports a damaged document through |

A damaged file is reported, never raised: `read_run` puts `field_problems` into
`problems`, which makes the run not publishable with that reason, and reads
everything it can. `members` held as a string, a row that is not an object, an
`audits` that is a string: each is named and skipped, and the issues around it
still come back.

### The citation ledger

`run['citations']` is what the run's own citation table says. It is the same
shape for every folder:

```python
cites = read_run("run-output")["citations"]
if cites["enabled"]:
    print(cites["summary"])                 # one sentence, for a brief — see below
    print(cites["counts"].get("references", 0), "references,",
          cites["counts"].get("not_found", 0), "with an unregistered DOI,",
          cites["counts"].get("not_located", 0), "in neither index,",
          cites["counts"].get("could_not_check", 0), "not checked")
    for line in cites["problems"]:          # one per stage that did not finish
        print("unfinished:", line)
```

- `enabled` is `False` — and `ledger` `None`, `counts` `{}` — for an arm that
  keeps no ledger. Only an arm running the `citations` specialist with network
  tools does — `review_arms()[arm]["citations"]`, and then only when
  `citations` is one of the tracks you chose. **Absent is not zero.** A run with `enabled: False` checked no
  citations; a run with `references: 0` had no reference list to check.
  The third case reads differently on purpose: an arm that planned a citations
  lane and left no table behind has `enabled: True`, `ledger: None`, `counts:
  {}` and one `problems` line saying the run planned a citations lane and wrote
  no citation ledger, so there are no ledger counts. Do not fold that into "no
  lane" — the lane was paid for and produced nothing, which is a failure worth
  showing. The line stops there on purpose: it does not say that not one
  reference was checked, because a run whose ledger stage never ran holds no
  record of what the specialist did or did not look at.
- A ledger file that is **there and cannot be read** is reported the same way:
  `enabled: True`, `counts: {}`, and one `problems` line naming the file from
  the run folder (`review/citations/ledger.jsonl`) and why it could not be
  read. It is not in `not_publishable_reasons`. It is the strongest form of an
  unfinished table rather than a different kind of damage, and a citation check
  never withholds a review.
- `ledger` is the absolute path of
  `<run>/review/citations/ledger.jsonl`, an append-only table with one row per
  reference and one per in-text use, last record per `row_id` winning. Use
  `read_citation_ledger` for the rows (below); `counts` is there so you do not
  have to read them at all.
- `counts` holds twenty-six integers: `references`, `resolved`, `not_found`,
  `not_located`, `could_not_check`, `no_lookup`, `damaged_dois`,
  `repaired_dois`, `candidates`, `errors`,
  `unresolved`, `mismatched`, `retracted`, `with_source_text`,
  `with_abstract`, `with_full_text`, `uses`,
  `uses_unmatched`, `uses_judged`, `supported`,
  `partly_supported`, `not_supported`, `cannot_tell`, `uses_unjudged`,
  `lookups` and `unreadable_rows`.
- **`with_abstract` and `with_full_text` are not the same evidence**, and
  `with_source_text` is the references that have either. An abstract answers
  what a paper claims; only the full text answers whether a number, a method or
  a limit the citing sentence attributes to it is in it. A page reporting one
  number for both reads as if every reference had been read. Most reference
  lists are mostly abstracts: open-access full text exists for a minority of
  what a paper cites, and nothing here reads a paywalled page or a PDF.
- **Four entries under `counts` are not numbers**, and an integration that maps
  the dict to an integer schema will trip over them: `ledger` is a string, the
  path `citations/ledger.jsonl` relative to the review folder; `stages` is a
  mapping — what fill, resolve and judge each did, with their own per-stage
  lookup and verdict counts; `complete` and `stages_done` are booleans.
  `complete` is repeated one level up as `citations['complete']`, which is the
  copy to read. Iterate `counts` by name rather than over its values, or pull
  the numbers you print by key.
- A reference lands in exactly one of **five** states, and only one of them is
  an existence claim. Four are answers to a lookup — `resolved`, `not_found`,
  `not_located`, `could_not_check` — and the fifth, `unresolved`, is the rows
  the run never looked up at all. It is a state of its own, not the gap between
  the other four, and `summary_line` prints it beside them ("… N never looked
  up …") so the printed states add up to `references`. A reference nobody
  looked up is not one that was checked and came back empty, and an integration
  that subtracts the four from the total and calls the remainder resolved is
  reporting the opposite of what happened. `not_found` counts the rows where
  every leg answered and all of them came back empty: the reference list gave
  the DOI as its own field, the DOI resolver has no handle for it — doi.org's
  handle API answering `responseCode` 100 with a 404, the one service whose
  whole job is to say whether a DOI was ever registered — no registration
  agency holds it, and both title searches answered and listed nothing. A DOI
  read out of the reference *text* instead never lands here, whatever the
  resolver says, since a DOI broken across a line in a two-column PDF is
  indistinguishable from one printed wrongly; neither does a DOI the reader cut
  short at a space inside it, which `damaged_dois` counts and which the
  resolver rejoins and looks up again (`repaired_dois` counts the rows where
  the rejoined DOI resolved, and those rows are `resolved` — the reference was
  fine and the text layer was not; a rejoined DOI that is registered while no
  index holds its metadata is `resolved` too, with no `record`, since the
  identifier does exist, and its note says the printed authors could not be
  corroborated against anything). Those rows are `not_located` and carry the
  caveat in their note. `not_found`, and only it, is the table saying a work
  does not exist; a metadata index that merely does not hold the record leaves
  the row in `could_not_check`. `not_located` counts the rows where an index
  answered and listed nothing: weak evidence, because books, chapters, theses,
  standards, software and preprints outside Crossref and Europe PMC land here
  while being perfectly real. `could_not_check` counts the rows where no lookup
  answered — or where no lookup was made at all, which is `no_lookup`, a subset
  of it: this version read no DOI and no searchable title out of those rows —
  neither from a title field nor out of the reference text, which it also
  parses for the title in the styles journals print — so no index was ever
  asked, and `looked_up` on the row says which of the two it is. Either way it says nothing whatever about the work.
  **Do not add them.** An integration that prints "N references do not exist"
  from the sum of the three is misreporting the run; `not_located` and
  `could_not_check` belong in the same sentence as how much of the list could
  not be confirmed, not in a count of bad references.
- `complete` is `True` only when fill and resolve finished, no matched in-text
  use is still unjudged, and at least one reference was actually checked: a
  table in which every row came back `could_not_check` is not complete, because
  the stages running is not the same as the references being checked. When it
  is `False`, `problems` names each unfinished stage in words — and those lines are in `run['problems']` too but
  never in `run['not_publishable_reasons']`. **An unfinished ledger does not
  make a run unpublishable.** Show the lines beside the findings; do not gate
  on them.
- A verdict in the table is not a finding. Findings are in `run['findings']`
  as always, written by the citations specialist after it re-checked the row
  itself. Nothing a citation worker wrote reaches `findings` on its own.

#### What in `citations` is written to be printed

`problems` is. Every line in it is a sentence for a person to read as it stands
— see [Problem lines are printed verbatim](#problem-lines-are-printed-verbatim)
below for the three rules that hold for all of them.

**`summary` is not, on a public page.** It is one sentence for two readers who
already have the context: the reviewer's brief, and `REVIEW.md`. It puts the
reference existence states and the in-text use verdicts together — "N
references: … M not located …. K in-text uses, J judged (… N not supported
…)" — and that sentence, printed under the title of a paper with named authors,
reads as an accusation about those authors rather than as a report on how far
an automated lookup got. The states behind it mean much less than they sound
like: `not_located` is an index not listing a title, which is ordinary for
books, theses and standards, and `not_supported` is one worker's reading of one
abstract.

For a page anyone can see, build your own lines from `counts` and say what the
numbers are: how much of the reference list the run was able to check, and how
much it could not. Keep the existence states and the use verdicts in separate
sentences, and do not sum `not_found`, `not_located` and `could_not_check` into
one number — only `not_found` is the table saying a work does not exist.

#### Reading the rows: `read_citation_ledger`

`counts` answers how much of the reference list was checked. When you need the
table itself — which reference resolved to which record, which in-text use was
judged how — `read_citation_ledger` is the supported reading. Do not open
`ledger.jsonl` yourself and do not import
`peerreviewagents.engine.citation_ledger`: that module is private and changes
without notice, and the "last record per `row_id` wins" rule is the one thing
every consumer that went there ended up re-implementing.

```python
from peerreviewagents import read_citation_ledger

cites = read_citation_ledger("run-output")        # run folder or its review/ folder
for row in cites["references"]:
    resolution = row.get("resolution") or {}
    print(row["label"], resolution.get("exists"), resolution.get("has_source_text"))
for row in cites["uses"]:
    print(row["sentence"], "->", row.get("verdict"))
```

- `run`, `review`, `ledger`, `exists` — the folders, the absolute path of the
  file, and whether it is there. A run from an arm without the lane reads as
  `exists: False` with empty lists, and raises nothing.
- `references`, `uses` — the rows, the last one written for each `row_id`,
  ordered by `row_id` so two readings of a folder agree. **The order is
  natural**: a run of digits in the id is compared as the number it is, so
  `ref:r2` comes before `ref:r10` and `use:paper.md:11:2:r1` before
  `use:paper.md:11:10:r1`,
  which is the reference order and the sentence order a reader counts in. You
  do not need to re-sort the rows to print them.
- `counts`, `complete`, `summary`, `problems` — identical to
  `read_run(path)["citations"]`, from the same functions and the same reading
  of what the arm planned, so a page showing rows and a page showing counts
  cannot disagree about one run. That holds for a `protocol.json` that records
  the citation slots and no `plan` as well: both readers call it a planned
  lane.
- **Source text is left out by default.** A resolved reference carries the
  abstract and, where an open-access archive gave one, the article body the
  passages shown to a worker were cut from, which is most of the bytes in the
  file and is someone else's copyrighted prose. Those rows
  come back without `resolution['abstract']` and `resolution['full_text']` and
  with `resolution['has_source_text']` instead, so you can tell a reference
  nobody fetched text for from one whose text was dropped here. Beside it,
  `resolution['has_abstract']` and `resolution['has_full_text']` say *which*
  of the two was dropped, because a page that only knows text existed cannot
  tell its reader whether a verdict rested on an abstract or on the article.
  Pass `source_text=True` for the rows exactly as stored.
- The guards are `read_run`'s. A symlinked `review/`, `citations/` or
  `ledger.jsonl` is refused by name — `ValueError`, naming the file with its
  folder — rather than followed, and damaged content never raises: an
  unreadable line is skipped, counted in `counts['unreadable_rows']` and named
  in `problems`. A line that is readable JSON but that the table cannot hold is
  damage of the same kind and is counted the same way. A row is readable when
  `row_id` is a non-empty string, `kind` is `"reference"` or `"use"`, and
  `resolution` and `printed` are each absent, null or an object — the fields
  the reading dereferences, nothing else. Every other field may be any shape
  and the row is still a row: a field that is only compared or printed cannot
  crash a reader, and dropping a row over one would throw away a citation.
  `NotARunFolder` for a folder that is not a run is the only other raise.
- **Below both is a last guard.** If anything at all goes wrong while the
  counts are taken or the rows are built — an exception neither this library
  nor your file's author foresaw — both readers give the "present and could
  not be read" answer above rather than raise, and `publishable` is unaffected.
  A citation check never withholds a review, and it never takes down the page
  printing it either.
- **A ledger this process may not open is not a raise either.** It comes back
  as `references: []`, `uses: []`, `counts: {}`, `exists: True`, `ledger` set to
  the file, and the one `problems` line `read_run` gives it. It is the same
  answer from both readings, because the state where nothing in the table can
  be counted is the state a page that prints the table is most likely to meet,
  and it must not be the one that crashes it.
- **The files beside the ledger are read the same guarded way.** Reaching the
  table means opening `protocol.json` for what the arm planned and
  `runtime.json` for which agents ran, and the stage record in
  `citations/stages.json`. Each of the four — torn, not a JSON object,
  unopenable, or a directory where the file should be — is read as absent,
  never raised, by `read_citation_ledger` as well as `read_run`. Damage to a
  file the citation table does not come from, `runtime.json` or
  `progress.json`, leaves the rows and the counts whole: it is a fact about the
  run, so it is named in `read_run`'s own `problems` beside the lost tokens and
  wall time, and not in `citations['problems']`, which both readers return
  identically.
- **And a last guard under the agents reading, for what is inside the file.**
  `runtime.json` can be a sound JSON object and still carry a field in a shape
  nothing writes: `attempt` as a list, or as `Infinity`, which Python's own
  `json` writes and reads. Whatever the reading of that file raises, neither
  reader raises it. `read_run` gives no agent rows and one `problems` line —
  `runtime.json could not be read as this run's record of its agents (…)` — in
  the same family as the line for a file that could not be read at all, and
  with the same effect on `publishable` as that line, which is unchanged.
  `read_citation_ledger` drops the line, for the reason above, and decides
  whether the lane was planned from `protocol.json` alone.

**Building a ledger fixture.** There is no fixture writer in the public
surface, and the supported way to build one is to write the file: the ledger is
JSON lines at `<run>/review/citations/ledger.jsonl`, append-only, one JSON
object per line, and the last line written for a `row_id` is that row. A
reference row is `{"kind": "reference", "row_id": "ref:<reference_id>",
"reference_id": …, "label": …, "raw": …, "printed": {…}}` plus a `resolution`
object once it was looked up; a use row is `{"kind": "use", "row_id":
"use:<document>:<piece line>:<sentence>:<reference_id>", "reference_id": …,
"document": …, "line": …, "end_line": …, "start": …, "end": …,
"piece_lines": […, …], "sentence": …, "marker": …}` plus a
`verdict` once a worker judged it — `document` is `paper.md` for the manuscript
and the supplement's file name otherwise, and `line`, `end_line`, `start` and
`end` locate the **citing sentence** by the rule in [Highlighting a
citation](#highlighting-a-citation), so a viewer underlines the sentence it is
showing a verdict about. `piece_lines` is the first and last line of the
passage the sentence was read out of — the paragraph a reader wants around it —
and it is the piece's first line, not the sentence's, that `row_id` is built
from, so a row keeps its identity. The offsets are into the file, markup and
all, while `sentence` is the same words as the page prints them: the two differ
by the reader's citation links, which is why both are kept. The stage record beside
it, `citations/stages.json`, is what `complete` and `lookups` are read from:
`{"fill": {"done": true}, "resolve": {"done": true, "lookups": 2}}`.
`tests/test_ledger_consumer_surface.py` builds its fixtures exactly this way
and is the worked example to copy.

**A ledger of your own: `citation_uses`.** Writing the rows by hand is the
rule above applied by eye, and the part that is easy to get wrong is not the
JSON — it is the anchor. `citation_uses(base, references)` is the engine's own
rule, exported:

```python
from peerreviewagents import citation_uses, cited_text, read_manuscript

base = read_manuscript('out/manuscript')
rows = citation_uses(base, json.loads(Path('out/manuscript/references.json').read_text()))
for row in rows:
    print(row['row_id'], row['reference_id'], cited_text(base, row))
```

`base` is what `read_manuscript` (or `verify_workspace`) answers with, and
`references` is the reference list as `references.json` holds it: one object
per reference with an `id` and a `label`, or the author and year fields the
markers in the text would name. Pass `[]` and you get the unmatched uses alone.
The answer is a list of `use` rows in document order, with exactly the fields
listed above plus `section`, `sentence_index`, `derivation` and `stage` — the
same rows the engine's own ledger is built from, by the same call. They are
rows and not a ledger: writing them to a file of your own, answering them, and
rendering the verdicts stay yours.

It is here because nothing else in this surface produces an anchor. `cited_text`
reads one back and the row's shape is documented, but where one sentence ends
and the next begins, how a sentence that crosses a line break is spanned, and
that `row_id` carries the **piece's** first line rather than the sentence's are
the rule itself. A second implementation of it differs by a character or a line
somewhere, and then your rows and the engine's are about different sentences
with no sign that they are.

#### Problem lines are printed verbatim

Every line in `run['problems']`, in `run['citations']['problems']` and in
`run['not_publishable_reasons']` is written to be shown to a person as it
stands. They are for showing, not for gating — a publishable run can carry a
non-empty `problems`, see [Gating on `publishable`](#gating-on-publishable).
Two rules hold for all of them:

- **No semicolons.** Several reasons about one row are joined with commas and a
  final "and".
- **No absolute paths.** A line names a file the way the reader can find it in
  the run folder they already have — `review/citations/ledger.jsonl`, not
  wherever the machine that read it keeps its runs.

A third rule is the ledger's own: no line says a reference **does not exist**
unless the DOI resolver has no handle for the DOI the reference list printed.
Rows that two indexes do not list, and rows whose lookups never answered, say
nothing either way about whether the work is real.

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

#### A stage missing from the ledger

`stages` holds the stage ledger, and a required slot absent from
`stages['reported']` is absent in one of two ways. `stages['silent']` is the
slots that stored nothing and filed nothing: nobody reviewed that share of the
manuscript, so `write_review` refuses and the run is `blocked` with those slots
named. `stages['missing_with_findings']` is the slots that stored findings and
never filed a report. Those findings were pooled and ruled on like any other,
so the review is written and the status is `complete`; what is missing is that
stage's own summary and limitations, and `note` says so in both `read_run` and
`REVIEW.md`. `stages['missing']` is still the union of the two, unchanged.

#### An editor that never submitted a review

`stages['editor_trace']` is the editor's own record of what it tried, present
for every run of an arm that has an editor — a row of zeros where the editor
made no submission call at all, rather than nothing: `calls`, `accepted`,
`refused`, `calls_spent` against `call_budget`, and `last_refusals` — the last
three refusals with the reason and the first problems of each (`calls_spent`
and `call_budget` are `None` until the first call, because the budget is the
server's answer to a call). Every call and every
refusal is also appended to `editor-log.jsonl` in the run folder, which
`reports['editor_log']` names.

`editor_trace['log']` is where that file sits, **relative to the run folder** —
`review/editor-log.jsonl` — and not the path it was read from. The trace is
carried into `stages`, `stages` is published inside `issues.json`, and an
absolute path there names the home directory of whoever ran the review. Join it
onto the run folder you read to open the file.

A run blocked because the editor never produced an accepted review says so with
those numbers in `cause`, and the trace stays on disk. The silent version of
this failure — `blocked` with nothing to read — was what 0.9.0 did on a
forty-finding manuscript, four runs out of four, and it is what the log exists
to answer. `calls` of zero with the run blocked means the editor made no
submission call at all, which is a different fault from a refusal it could not
satisfy.

### Gating on `publishable`

`complete` answers whether the run finished. `publishable` answers whether the
whole review is here to show, which is the question a consumer actually has
before it puts a review in front of someone. It is `True` only when:

- `status` is `complete`, or `complete-without-findings` with nothing
  quarantined,
- `findings_source` is `issues` — the review was read from `issues.json`, not
  fallen back to the raw ledger,
- `guard_status` is set, so the guard recorded a verdict over these findings,
  and
- `problems` is empty.

When it is `False`, `not_publishable_reasons` holds one sentence per reason and
is never empty, so the flag is never `False` with nothing named. A complete run
whose `issues.json` was truncated is the usual case: the ledger findings are
real, but they are not the review, and the reason says so.

**`problems` is not a gate.** `publishable` and `not_publishable_reasons` are
the gate; `problems` is for showing. The fourth condition above is `problems`
*as it stands when the flag is decided*, and three kinds of line are appended to
it afterwards on purpose: the citation stage lines, the line for a ledger that
could not be read, and the line for a citation worker that ran a substituted
model. None of them is a reason against publishing, and each of them is
something a reader should see. So **a publishable run can come back with a
non-empty `problems`**, and that is the normal case for any run with an
unfinished citation table. An application that treats a non-empty `problems` as
a refusal will withhold reviews that are whole — read `publishable`, print
`problems`.

**A quarantined finding does not make a run unpublishable.** The guard throws
out a finding whose quote it could not place in the manuscript. A
guard that did that has done its job, and what is left is a whole review minus
one unanchored quote. `guard_status` is `clean` or `quarantined` — there is no
error state — and `quarantined` holds the rows themselves, so an application
that wants to weigh what was held can.

**A review with no findings in it is still a review.**
`complete-without-findings` with `counts['quarantined']` at zero means the
reviewers read the manuscript and raised nothing make-or-break, and the editor
closed the review saying so — `issues` empty, `findings_source` `issues`,
`counts['issues']` zero: publishable. The same status with findings
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

### Writing a manuscript from your own pieces

`import_paper(pdf, folder)` is for a PDF. An app that already holds the
structure — a Typst or LaTeX source it parses itself, its own editor's
document — writes the same workspace from the pieces it has, with
`write_manuscript`. It is one call, and it is the only supported way to produce
a workspace: the base is a Markdown file plus a `map.json` of line ranges, and
a second implementation of that format in an app is a workspace the engine
either refuses or, worse, accepts as something it is not.

```python
from peerreviewagents import write_manuscript, read_manuscript, verify_workspace

manifest = write_manuscript(
    folder,                 # the workspace folder; it must not already hold a manuscript
    pieces,                 # the main document's pieces in order, or {name: pieces}
    title='Replay during quiet rest',
    parser='paper-workbench 1.0',
    source={'file': 'scaffold/main.typ'},
    references=[{'id': 'r1', 'raw': 'Wilson MA, McNaughton BL. Science. 1994.'}],
    pages=[{'page': 1, 'width': 595.276, 'height': 841.89}],   # only if your pieces have boxes
)
```

It writes the documents, `map.json`, `references.json`, `statistics.json` and
`manifest.json`, and returns the manifest it wrote. `MAIN_DOCUMENT` is the name
of the main document — `paper.md` — and every other name must end in `.md`;
supplements are conventionally `supplement-<name>.md`. `BASE_SCHEMA_VERSION` is
what lands in `manifest['schema_version']`, and a reader checks it before
trusting the rows.

**A workspace is three files, and it says which shape it is.**
`read_manuscript` and `verify_workspace` refuse, by name, a folder with no
`manifest.json` beside the base and a manifest carrying no `schema_version` —
not only one stamped with an older version. An unstamped folder was being read
as the current shape on the strength of having no opinion, and its rows were
then trusted to carry fields they may never have had. If you write a workspace
with `write_manuscript` this is already true of it.

A workspace is written **once**. A second `write_manuscript` over the same
folder is a `ValueError`, because rewriting the base renumbers every line a
saved review cites. A redraft is a new folder — and a new `paper_id`, which is
what makes `export_draft` refuse a review of the version before it.

**Pieces a model wrote need `ai_model=`.** If any piece carries `source` `ai`
or `ai-reconstructed`, name the model that wrote it:

```python
manifest = write_manuscript(folder, pieces, title='Replay during quiet rest',
                            ai_model='claude-sonnet-5-5')
```

Without it the write is refused by the piece's index and kind. The reviewer is
served a marker saying who wrote each such line, and a manifest that said the
manuscript needed no reading while holding a model's sentences would be saying
something untrue about the paper under review. What lands in the manifest is
authorship, not a read:

```json
"ai_read": {"read": false,
            "reason": "written from an application's own source, which needs no reading: no stage here read the pages. 3 of its 184 pieces were written by claude-sonnet-5-5, which the caller named, and carry source \"ai\" or \"ai-reconstructed\"",
            "ai_model": "claude-sonnet-5-5", "ai_pieces": 3, "lost_pages": []}
```

`read` stays `false` because no page image was opened here, and the served
marker on those lines says `[written by claude-sonnet-5-5]` rather than
claiming a page was read. An `ai-reconstructed` piece must also carry the
`original` it replaced — that source means a model rewrote text the document
printed, and the map row is where the reading it replaced is kept, so without
one it would promise an audit the folder cannot give. Use `ai` for a piece
nothing had before.

#### What a piece is on the way in

A piece is a dict. Only `kind` and the text that kind renders from are needed;
everything else is optional, and what is absent is `null` in the map rather
than a guess.

| Field | Required | What it is |
| --- | --- | --- |
| `kind` | no, defaults to `paragraph` | `title`, `heading`, `paragraph`, `equation`, `figure`, `caption`, `table`, `reference`, `supplement` — or any other word, which renders as a paragraph and is reported under that name in the counts |
| `text` | for every kind but `table` | the piece as a reader reads it. Newlines are folded to spaces: one piece is one paragraph. A piece that would render to nothing is refused by its index and kind rather than dropped |
| `section` | no | the section the piece sits in, as printed (`'Results'`). It is what a finding reports as its location, so an app that knows its sections should pass them |
| `page` | no | the printed page, numbered from **1**. Absent for a manuscript that has no pages |
| `bbox` | no | where the piece is printed on that page, as `{'x0', 'y0', 'x1', 'y1'}` or `[x0, y0, x1, y1]`. See the next section for the unit |
| `source` | no, defaults to `text` | `text` for what the document says, `ai` for something a model produced, `ai-reconstructed` for text a model rebuilt from a damaged original. A reviewer is told which it is reading |
| `level` | for `heading` | 1 for a section under the title, 2 for a subsection, and so on |
| `latex` | for `equation` | the equation's LaTeX. `number` is its printed number, if it has one |
| `image`, `label` | no, for `figure` | a path to the figure image relative to the workspace, and its label |
| `table` | for `table` | the rows as a list of lists of cells, header row first |
| `confidence` | no | the converter's own confidence in this piece, `0.0`–`1.0`. It travels into the map row, which is what tells a reviewer whether to open the page |
| `citations` | no | the reference ids this piece cites, for citations that are markup rather than text (a JATS `<xref>`). The citation ledger reads them |

That table is the whole vocabulary — with `original` and `page_mapping`, which
a repair and a page-numbering app pass through. **A field outside it is
refused by name**, because the format has no column for it: it would be
dropped between your dict and `map.json` with nothing to say so, and an app
that kept its own `anchor_id` on a piece lost it that way. Keep your own ids
beside the pieces, not on them, and anchor against the line range the map
gives back. A piece that renders to no line — a list whose text is under an
`items` key, an empty paragraph — is refused the same way, by its index and
its kind: the count in the manifest is then over the pieces you passed, not
over the ones that happened to survive rendering.

A manuscript with no PDF — a Typst source before it is built — carries no
`page` and no `bbox`:

```python
pieces = [
    {'kind': 'title', 'text': 'Replay during quiet rest', 'section': 'Title'},
    {'kind': 'heading', 'level': 1, 'text': 'Results', 'section': 'Results'},
    {'kind': 'paragraph', 'section': 'Results',
     'text': 'Sequences recurred in 12 of 14 sessions (86%).'},
    {'kind': 'equation', 'section': 'Results', 'latex': 'r = \\frac{a}{b}', 'number': 1},
    {'kind': 'table', 'section': 'Results',
     'table': [['Session', 'Events'], ['1', '12'], ['2', '14']]},
]
manifest = write_manuscript(folder, pieces, title='Replay during quiet rest',
                            parser='paper-workbench 1.0')
```

That is a complete manuscript. What it costs is the page: `map.json` holds
`"page": null` and `"bbox": null` for every row, `manifest['pages']` is `[]`
and `statistics.pdf_pages` is `0`. In consequence

* the reviewer's `view_page` tool refuses with `Unknown page` rather than
  showing anything, and nothing in the review depends on it;
* a finding's `page` and `bbox` are `null` too, so there is no region to draw
  on a page image;
* **the citation is the line.** Every finding carries `document`, `line`,
  `end_line` and a `start`/`end` offset pair into it, and `anchor_text` is
  exactly `line_text(line)[start:end]` — see [Highlighting a
  citation](#highlighting-a-citation) for the rule and for the quote that
  crosses a line break. A viewer highlights that span in the text it is
  showing. This is the same anchoring a PDF-imported manuscript gets; the box
  is the extra a PDF has, not the mechanism.

#### Where a box is, and how big the page is

The four numbers of a `bbox`, in `map.json` and in a finding, are

> PDF points — 1/72 inch — in the coordinates of the page as the converter
> read it. The origin is the **top-left** corner of the page, `x` grows right
> and `y` grows **down**, so `x0, y0` is the top-left corner of the box and
> `x1, y1` the bottom-right.

That is checked rather than assumed: the converter's boxes line up word for
word with `pdftotext -bbox` over the same file, which prints the same
coordinates from the same corner. The manifest says it too, in
`manifest['box_convention']`, so the folder does not need this page to be read
correctly.

A box is only placeable against the page it is on, so `manifest['pages']` is
one record per page, in the same points:

```json
"pages": [{"page": 1, "width": 612.0, "height": 792.0, "rotation": 0},
          {"page": 2, "width": 612.0, "height": 792.0, "rotation": 0}]
```

`page` is numbered from 1, as a piece's own `page` is, so a row indexes this
list by its number. To draw a box on an image you rendered of that page at
`image_width` pixels, scale by `image_width / width` — there is nothing else to
know, and nothing to guess. `rotation` is the degrees a viewer applies to the
printed page; boxes are **not** rotated, so a page whose `rotation` is not `0`
is one an app rotates the box for itself.

`import_paper` fills `pages` from the PDF. `write_manuscript` fills it from the
`pages=` argument, and an app whose pieces carry boxes should pass it; the page
count in `manifest['source']['pages']` follows from it when you do not set one.
`"pages": []` means the sizes were not recorded — either the manuscript has no
pages, or `pdfinfo` was missing from the machine that imported it. It never
means the page is a default size.

#### What `verify_workspace` returns

`read_manuscript(folder)` and `verify_workspace(folder)` both return a
`PaperBase`: the documents' text (`text`, `documents`, `line_text(line)`,
`line_count()`), the map rows (`rows`, `piece_at(line)`, `section_at(line)`),
and the manifest (`manifest`, `paper_id`, `title`). The difference is the
check, not the result:

* `read_manuscript` reads the folder as it stands. That is what a viewer wants,
  since it is showing the folder rather than reviewing it.
* `verify_workspace` first re-digests every file `manifest['files']` lists and
  walks the source tree beside the base, then returns that same `PaperBase`. An
  edited document is a `ValueError` naming the file — `Manuscript source
  changed: paper.md` — because a line number is only worth having while the
  line is still there. A path in the manifest that points outside the folder,
  and a symlink in the source tree, are refused the same way.

#### What a scaffold workspace must contain

A scaffold workspace is a manuscript whose source — a Typst tree, a LaTeX
project — is kept beside the base, under `scaffold/`. The inventory is part of
the manuscript: `verify_workspace` walks `scaffold/` and requires the file
names it finds to be exactly the `scaffold/`-prefixed keys in
`manifest['files']`, so a source file *added* after the write is refused
(`Manuscript source inventory changed`) even though no listed file was touched.

So a scaffold workspace holds, at minimum:

1. `paper.md` and `map.json` — the base, written by `write_manuscript`;
2. `manifest.json`, whose `files` digests every file in the folder, the
   `scaffold/` tree included;
3. the source tree itself, **written before** `write_manuscript` is called,
   since that call digests the folder as it stands. A file added afterwards
   needs the workspace rewritten, not patched;
4. `references.json` and `statistics.json`, which the same call writes.

`source={'file': 'scaffold/main.typ'}` is how the manifest names the root of
that tree. Nothing in the engine builds the source or reads it: the reviewer
reads `paper.md`, and the tree is there so the workspace is the whole
manuscript and a later build can be checked against what was reviewed.

### Building a read manuscript for your tests: `scripted_read`

**For tests and fixtures.** A review's Read stage is a model stage, launched by
`review`; this is that stage driven in code, with nothing launched and no model
reading anything.

What the stage leaves on a sealed manuscript — a re-read part's `part` and
`of`, the `absorbed` reading of folded debris, the reader's `note` in the
manifest — used to exist only in the output of a real run, so a consumer's
fixtures for it were hand-written JSON copying the shape by eye, which drifts
from the engine the moment the engine moves. `scripted_read` opens a real run
on a PDF, claims the reader's slot and hands back a handle whose methods are
the reader's own tools under their own names, through the same server the stage
drives:

```python
from peerreviewagents import ReaderRefused, scripted_read

CONVERTER_LEFT = [
    {'kind': 'equation', 'text': 'E = m c^{2', 'latex': 'E = m c^{2',
     'number': '(1)', 'section': '3 Results'},
    {'kind': 'caption', 'text': 'Figure 1: The bench and the instrument.',
     'section': '3 Results'},
    {'kind': 'paragraph', 'text': '0 10 20 30 40 mm', 'section': '3 Results'},
]

with scripted_read('paper.pdf', folder, CONVERTER_LEFT) as read:
    read.view_page(1)
    read.replace_equation(read.find('E = m c^{2'), 1, 'E = m c^{2}', number='(1)')
    read.describe_figure(read.find('Figure 1:'), 1, 'A photograph of the bench …',
                         absorb=[read.find('0 10 20 30 40 mm')])
    read.seal(note="The figure's axis numbers were a paragraph and are folded in.")

base = read.base()          # the sealed workspace: `text`, `rows`, `manifest`
```

- `pieces` — the third argument — are extra converter pieces spliced into the
  draft, each in the shape the converter writes and merged over
  `peerreviewagents.PIECE`, so `{'kind': 'equation', 'latex': 'E = m c^{2'}` is
  a piece. This is how a fixture holds the failures the stage exists for
  without a PDF that has them. One naming a `document` other than `paper.md`
  goes to that document, which is how a fixture gets a supplement.
- `find(text)` is how a script names a piece: by a few characters of what the
  converter read there. It raises `LookupError` unless exactly one piece
  matches, because a fixture quietly working on a different piece than it
  means to is worse than one that stops.
- A refusal raises `ReaderRefused`, with the tool on `.tool` and the server's
  whole answer on `.answer`. The tools *answer* a refusal to a model, which
  reads it and tries something else; a script has no turn to take again.
- `view_page` returns that page's JSON payload alone. The image beside it is
  for a model.
- `base()` is `read_manuscript` on the sealed workspace, so a test does not
  have to know where in the run folder the workspace is.
- One run is open per process, so use it as a context manager (or call
  `close()`): leaving the block puts back whatever run was attached before.
  The folder stays as it is, sealed, for the test to read.

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

### The out folder already holds a review

A folder whose `progress.json` says `complete` or `complete-without-findings`
is the review, not a place to start one. `ingest` refuses it by name and
changes nothing in it, so a re-run over a finished folder cannot undo the
review it holds. A new server process and the session that wrote the review are
refused alike, in the same words: one paper per session, and a second one passed
to a live server fails by name rather than attaching to a finished folder. Read
what is there with `peerreview report <run>` or straight
from `review/REVIEW.md`; neither takes a run. A further round of review is a
fresh out folder with this one as its prior round — pass
`prior_issues=<old run>/review/issues.json` to that run.

A `running` or `blocked` folder is reopened as before: re-ingesting into a
blocked run is how a run that hit a rate limit or lost a stage is resumed.

Nothing writes in a complete folder either. Every tool a slot could still hold
a token for — the reviewer read tools, `save_note`, `submit_finding`,
`submit_issues`, `submit_editor`, `submit_report`, `write_review`,
`claim_agent`, `reissue_claim_code`, `resolve_citations`,
`claim_citation_batch` and `submit_citation_verdict` — is refused with the run's
status, and the refusal is not logged: the logs are the record of the review
that was written. `run_status` and `list_findings` still answer, and write
nothing. The test for this calls every registered tool over a finished
two-track run and digests every file in the folder between calls.

A lookup pass that is already in flight is the one writer a guard on the call
cannot stop, so the order is fixed from the other side: `write_review` refuses
while a `resolve_citations` pass is open, with `resolving: true`. The review is
written from the citation table, so it is not written while the table is being
filled — wait for that call to answer, and then write. A pass that will never
answer is still released by `mark_blocked`.

Cancelling the `resolve_citations` request does not stop the pass. A client that
sends `notifications/cancelled` is answered nothing and stops waiting, but the
lookups are in a worker thread that runs to the end of the slice it was given:
the ledger and `progress.json` keep growing after the client has given up, and
`write_review` is refused with `resolving: true` until that pass ends. The
session itself survives the cancellation — every later call on it is served — so
a cancelled call is a lost answer, never a lost run: ask `run_status`, or call
`resolve_citations` again once the pass has ended, to see where the table got to.

A second `write_review` answers with the counts of the review already on disk
(`issue_counts`, `required_changes`, `suggestions`, `escalated`), so a recorder
whose first answer was lost reports what was written rather than calling again.
`run_status` carries the same counts once a review is written: its `issues` and
`escalated` are read back from the published `issues.json`, on every arm,
including the arms that run no editor and have no working tally. Before a review
is written `issues` is the editor's tally and is absent where no editor runs —
absent means the answer does not say, never zero.
