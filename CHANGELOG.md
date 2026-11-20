# Changelog

Notable changes per release. Earlier releases are described in their GitHub
release notes and are reachable by tag; this file starts at 0.9.0, the release
that made the plugin the only way to run a review.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
the project uses [semantic versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- **A review is run by a Python orchestrator, not by a workflow script.**
  `peerreviewagents/plugin/orchestrator.py` is what `workflows/review.js` and
  `workflows/check-fixes.js` were — stage order, fan-out, one retry then
  blocked, the token and wall-clock ceilings — and it runs inside the
  `peerreview` command rather than inside a Claude Code session driving a
  workflow. The command serves the tools itself, over loopback, for the length
  of the run: one long-lived server the orchestrator owns, its address written
  to `startup.json`, and nothing launched over stdio. The recorder is no longer
  an agent at all — writing the review, the audits and the fix check is this
  process's own work, so its usage row is `in process` with no model and no
  tokens. Each agent is one call on a runner
  (`runner.py`, with `claude_code.py` behind it): instructions, model, effort,
  turn cap, the tools that agent may call, the server's address and a
  final-answer schema in; the answer, the tokens, the model that actually ran
  and how it ended out. The headless sessions it starts get an explicit
  environment, only our tools with the built-ins off, and no user or project
  settings or `CLAUDE.md` — a reviewer cannot be handed an instruction the run
  did not write. The runner owns the model-availability check, and a model this
  login cannot run is still refused before a folder is written. The public
  surface is unchanged: `review`, `check_fixes`, `read_run`, `read_fix_check`,
  `review_arms`, the `scripted_*` helpers, the run folder and the usage files
  all read as they did.
- **The agent definitions live in `peerreviewagents/agents/`**, one `.md` per
  slot, with the instruction text unchanged and nothing in them naming Claude
  Code: a definition is what an agent is, not how it is run. The whole panel is
  testable with a scripted runner and no model call, each slot confined to its
  own tool list, which is what the tests that used to pin `review.js` do now.
- **An imported manuscript is a Markdown file, not a list of blocks.** The
  manuscript folder now holds `paper.md` — the whole paper in reading order,
  LF endings, one piece per line with a blank line between pieces, no ids in
  the text — and `map.json`, one row per piece keyed by the line range it
  occupies: kind, section, page, the box on that page, where the text came
  from, and a fingerprint of its normal form. Supplements are their own `.md`
  beside it with their own rows and their own line numbers, which is the first
  time the Word supplements the importer copies have been readable at all. The
  source files move to `original/`, and `manifest.json` gains the schema
  version, the counts per kind and per source, the fingerprint rule and the
  gaps this converter leaves — there are no figure images at the pinned
  version, so a figure is its caption line.
  Measured over seven saved runs, reviewers who could only reach the
  manuscript twelve blocks at a time had seen between half and almost all of
  the main text, and the editor as little as fifteen per cent, while spending
  three to twenty of their thirty calls: the shape of the reading surface, not
  the allowance, was what stopped them. Block ids were also a third numbering
  that three applications had to agree on, and said nothing to the person
  reading a review.
- **A displayed equation is in the manuscript.** The converter returns an
  equation with an empty `text` and its LaTeX under `math`, and the importer
  returned early on empty text, so every displayed equation in every imported
  paper was absent — eleven of eleven in the antenna manuscript — and
  reviewers argued about algebra they had never been shown. Each one is now a
  line of its own, `$$ latex \tag{n} $$`, with the converter's own confidence
  in its map row: sums and subscripts are what it mangles, so a low confidence
  means read the page rather than that the equation is missing.
- **One normal form for manuscript text**, in `engine/paper_text.py`: NFKC,
  whitespace runs collapsed, dash and quote variants folded, soft hyphens and
  zero-width characters dropped, case kept — with, for every character of the
  normal form, the offset of the character it came from, so a quote matched
  there can be reported in the coordinates of the file an author reads. It is
  what fingerprints are taken over, and what citing a quote will rest on.

- **A rebuttal quote says which document it is in.** An issue holds members
  from several documents at once, so the document a rebuttal is quoted from is
  not the first member's: the editor's `rebuttal_quote` was looked for in
  whichever document that member happened to be in, and a rebuttal quoted from
  a supplement could not be anchored at all. The pointer now carries its own
  `rebuttal_document`, read exactly as a member's `document` is — empty means
  `paper.md`, a name the base does not hold is refused with the names it does,
  and the field may not be set on an issue with nothing to rebut. `issues.json`
  rows carry it, `REVIEW.md` names the file beside the line, and the stamp is
  `review-issues-v4`.

### Added

- **Everything a run knows is readable from its folder.** A consumer opens a
  run folder days after the call that made it, so a fact that lived only in
  the return value was a fact that consumer had to infer. Five of them are on
  disk and in the readings now. `read` — whether the reader failed and why,
  the model that read, its attempts, pages and tool calls — is written to
  `runtime.json` and comes back from both `read_run` and `read_fix_check`
  (`None` for a folder that arrived with a sealed manuscript and planned no
  reader); it used to be inferable only from the manuscript's state plus a row
  in the agent table. `read_run`'s `code_check` carries `silent`, which
  `checked: 0` cannot say — a lane that reported and settled nothing and a
  lane that never reported are not the same run to come back to. A fix check's
  unchecked rows are in `progress.json` beside the written document, so a
  folder whose check was never written still answers which problems have no
  verdict and why, instead of `not_checked: 3` with `unchecked: []`. A blocked
  review's reason names each missing stage's own last ending — `core-1 (over
  the token ceiling)`, `(returned nothing)`, `(out of time)`, `(failed)`,
  `(was never started)` — and, where the panel stopped starting seats because
  the run was out of budget, says so and ends with the stop itself (`this run
  reached its token ceiling of 5,000: …`); it used to be one sentence,
  "these stages returned nothing twice", for every cause. And
  `scripted_review` builds that stopped run from a script — a seat that spends
  past a small `per_run_tokens` — so no consumer writes that folder by hand.
- **A check that is not whole says so, and says why against each problem.**
  A fix-check lane that answers nothing, or dies holding its batch, costs its
  own batch and nothing else — that is deliberate, a check with ten verdicts
  in it being worth more than no check — but it used to be invisible: the
  problems were listed as not checked with no cause, and the run read plain
  `complete`. Each unchecked problem now carries the lane that was dealt it
  (`lane`) and the cause (`reason`: past the cap, the lane never claimed its
  batch, it claimed the batch and stopped without reporting, or it reported
  without a verdict on that problem), and the check itself carries
  `incomplete` with `incomplete_reason` in one sentence — through
  `fix-check.json`, `read_fix_check`, what `check_fixes` returns, and the top
  of `FIXES.md`, which also prints the cause in its own column. The run
  statuses are unchanged on purpose: every consumer that asks whether a run
  finished reads the four it already knows, so a fifth would quietly turn
  every written-but-partial check into an unrecognised one. The status says
  the check was *written*; `incomplete` says whether it is whole. A code check
  reports the same two fields for the claims nobody reached.
- **A result says whether the reader failed and whether the code check was
  silent.** Both stages have a fallback behind them rather than a block — the
  manuscript is sealed as converted, and a silent code check leaves every
  finding as its reviewer filed it — and both used to leave the result looking
  like a run where nothing had gone wrong. `read` is now a row with
  `reader_failed` and the reason on it, and is `None` only for a folder that
  arrived already sealed; `code_check` is a row with `silent` on it, and is
  `None` only for an arm that planned no code lane. Both are in what `review`
  and `check_fixes` return.
- **A Read stage that could not seal anything says so, and counts what it
  cost.** `read['attempts']` is how many readers the run started — it was
  `None` on exactly the runs where the reader failed, because the record was
  built from a fallback with no number in it — so a stage that failed once and
  sealed on the second reader reads as two. And the one run where the fallback
  fails too, a paper with a page the converter returned nothing from, no longer
  reads as a run whose reader was fine: sealing as converted would write a
  manuscript missing that page, so it is refused and the run blocks, and the
  block it leaves behind says `reader_failed: True` with `sealed: False` and
  the reason for both rather than falling back to the stop in
  `progress.json`. A fix check stopped there keeps the whole cause once, as
  `stopped` on the check, and each unchecked problem's `reason` names its lane
  and points there — the same paragraph under twenty problems was still one
  fact.
- **A scripted seat can say what it spent, so the token ceilings work without
  a model.** `scripted_review` and `scripted_fix_check` take `tokens=`, the
  number every seat is metered at, and `slot.spend(n)` inside a script is that
  seat's own; `slot.task.token_ceiling` is what the panel handed this seat. A
  seat over its ceiling ends `over the token ceiling` with its answer dropped,
  as a real session's child does when it is cut off mid-stream, so the stage
  retries it once and then blocks the run. `per_run_tokens` and
  `per_agent_tokens` have been plan keys all along and did nothing in a
  scripted run; a consumer can now build a run stopped over a ceiling.
- **A whole run can be driven from scripts, through the orchestrator:
  `scripted_read`, `scripted_fix_check`, `scripted_review`.** Each is a real
  run with Python where the model goes — the same orchestrator, the same tool
  server, the same stage order, the same recorder and the same run folder, with
  nothing launched and no model reading anything. You hand each seat a script,
  a callable taking the slot, whose methods are that agent's own tools under
  their own names; the orchestrator drives, and the script answers in the
  schema the runner would have asked a model for. Scripts are keyed by slot
  name (`core-1`), agent name (`core-reviewer`), role (`core`, `editor`,
  `specialist`, `citation-worker`, `fix-check`, `code-check`, `reader`) or
  `'*'`, and every seat you leave out runs this package's own script for that
  role: `scripted_review('paper.pdf', folder)` is a whole written review in one
  call, and `{'editor': mine}` is that review with your editor in it.
  The point is the folder. These helpers used to claim their slots themselves
  and write the review as the recorder, which meant a scripted run was the
  engine's output shape everywhere a *consumer* looked and nothing like a real
  run anywhere else: no runtime agent rows, every `ending` missing, the recorder
  reading "not recorded", no token fields. A consumer whose rendering of a run
  was tested only against that could not see it break. Through the orchestrator
  there is one mechanism: `runtime.json` has one row per agent with its ending,
  its tool calls, its seconds and its token fields, the recorder row reads
  `in process` with no model, `summary['runner']` is `scripted`, and a seat is
  confined by its agent definition — a tool it was not granted raises
  `NotGranted` and one the server does not have raises `AttributeError`, the
  boundary a model meets. A script that returns `NO_ANSWER` is an agent that
  answered nothing, so the orchestrator retries it and then blocks the run, as
  it does for a silent model; `ScriptedSlot` and `NO_ANSWER` are public with
  the helpers. A refusal raises `ReaderRefused`, `CheckRefused` or
  `ReviewRefused` — siblings under the new `ToolRefused` — with the tool on
  `.tool` and the server's whole answer on `.answer`: caught in the script the
  stage goes on, uncaught it ends the run and is raised again out of the call.
  What the scripts must still do is the work: the reader's accounting for every
  outstanding piece, the editor's grouping, ranking and escalation rulings, a
  checker's verdict on each problem its own `fix_queue` dealt it. The server
  refuses a script exactly as it refuses a model.
  Before this, a consumer testing against a run had two bad choices: drive the
  private plugin round builders, or hand-write the JSON of a finished review,
  which copies the engine's output shape by eye and drifts from it silently.
- **A new draft can be checked against the problems a review raised:
  `peerreview check-fixes paper-v2.pdf out/ --problems problems.json`.** The
  third command, and the other half of a review rather than a second one:
  nothing in it looks for a problem, so it runs no core reviewer, no
  specialist, no editor, no citation ledger and no finding guard. The problems
  are given, and every one of them gets a verdict against the new draft —
  `fixed`, `not-fixed` or `cannot-tell`, in `FIX_VERDICTS`.
  A `fixed` or `not-fixed` verdict carries a quotation of the *new* draft,
  anchored by the machinery that anchors a finding's quotation, so a verdict
  cannot rest on words the draft does not say. Two cases have no new words to
  quote and rest on the server's own reading instead, recorded on the row as
  `basis`: a problem fixed by deletion is `old-quote-gone`, stored only after
  the server confirms every quotation the problem was raised against is gone
  from this draft, and a problem the draft still does not answer is
  `old-quote-stands`, stored only after it confirms one of those quotations is
  still there word for word. Both also carry the searches the checker ran, so
  the record says what was looked for and a reader can run it themselves. A
  deletion-only rule was the obvious design and the wrong one: it made "the
  draft changed nothing here" the one verdict with nothing behind it.
  `cannot-tell` carries a reason and is a real answer, not a hedge — a fix that
  lives in the code, the data, a repeated experiment or a reply letter cannot
  be read out of the manuscript, and this run reads the manuscript only.
  The problems come in through their own small schema,
  `peerreviewagents.problems` and `review-problems-v1`: an id, a statement, a
  severity, the quotations of the old draft the problem was raised against, and
  the authors' reply where there is one. It is deliberately not `issues.json` —
  an application that keeps its own issue list writes five fields instead of a
  whole review's shape — and a problem with no evidence is refused rather than
  checked against its statement alone, which is a reading nobody can verify.
  `problems_from_run('first-run')` writes the file from a finished run's
  issues, replies included, and a run that raised nothing writes an empty list
  the command then refuses by name. The check reads the new draft the way a
  review reads a manuscript: the same import, the same Read stage, the same
  seal, and the server tells each checker up front which old quotations still
  appear verbatim, so nobody spends a call confirming what the server already
  read. One checker per batch of ten problems, capped at 40 for the run; a
  larger file is accepted and the problems past the cap are reported as not
  checked, with `over_cap` saying how many, because a problem with no verdict
  is neither fixed nor unfixed.
  It writes `fix-check.json`, `FIXES.md` — not fixed first, then cannot tell,
  then fixed, each row saying what its verdict rests on — and the lane's own
  `fix-checks.jsonl`, and `read_fix_check(folder)` is the one call that reads
  them: status by the same rule a review's status follows, one row per answered
  problem, the counts per verdict, `not_checked` with the problems it names,
  the problems it was given, what it spent, and `file_problems` for whatever
  could not be read. It is `read_run`'s counterpart rather than `read_run`
  itself, since a check has no findings, no issues and no editor, and
  `complete-without-findings` on one means the check was written with no
  verdict in it at all.
  Its ceilings are its own — 150,000 tokens an agent, 900,000 for the run, a
  45-minute wall clock, against a review's millions — because a review's
  six-million ceiling would never fire on a run this size, which makes it no
  ceiling at all. `peerreview review --arm fix-check` refuses by name and says
  which command to run. There is no `--code`: every verdict a check can give
  rests on the words of the new draft, so a snapshot would add a second input
  and a second verification path in support of no verdict at all, and a problem
  whose fix is in the code is what `cannot-tell` is for.
  Three tools (`fix_queue`, `submit_fix_check`, `write_fix_check`) bring the
  server to 47, inside the same per-run server rather than a second one, so
  identity, the claim codes, the quotation check, the budgets and the folder
  guard are the ones a review already uses. In the plugin: the `check-fixes`
  skill, the `check-fixes.js` workflow and the `fix-check` agent, whose tool
  list is the paper tools, the queue and the submit tool and nothing else — it
  has no way to file a finding of its own.
- **`review_arms()`: what each arm runs, asked before a run exists.** One row
  per arm — how many core reviewers it fixes (or `None` where the count is the
  caller's), whether it runs the specialist tracks, an editor and a revision
  pass, whether it can keep a citation ledger, which arm is the default, and
  the line `--arm` prints for it. The row that was unaskable is `code`: the
  code review is a specialist lane, so an arm that runs no specialists refuses
  `code=` outright rather than accepting it and reviewing none of it, and an
  application offering a review against the code had to hard-code the arm
  names out of `plugin.server` to know which. Documented in
  `docs/INTEGRATIONS.md`, and the citation-ledger note there now points at
  this function instead of naming two arms that would go stale.
- **A review can read the code behind the paper: `--code <folder or
  repository URL>`, `code=` on `review()` and on `ingest`.** A URL is cloned
  shallow with hooks and submodules off; either way the *text* of that code —
  source, configuration, notebooks, documentation, skipping binaries and
  anything over the size caps, and listing the data files it ships for the
  reviewer to fetch — is snapshotted into `<run>/code/`
  beside a manifest naming the origin, the commit, every file taken with its
  digest and everything left out with the reason. The snapshot is what the
  review reads, so a finding cites code that is still there a year later.
  Three rules decide what lands there. A notebook is stored as its cells —
  code and markdown in order, each marked with its cell number, every output
  dropped — with the file cap applied to those cells and not to the base64 a
  single figure puts in the file, because otherwise the notebooks much
  analysis lives in were either skipped for their own output or served as
  JSON noise; the manifest row and `read_code` say `extracted` so a reader
  knows the cited lines are lines of the cells. Nothing that may hold secrets
  is copied: `.env` and `*.env` and `.env.*`, keys and certificates,
  `credentials*`, `secrets*`, `.netrc`, `.npmrc`, `.pypirc` are skipped by
  name with that as the reason, since the snapshot is served to a model and a
  credential copied into it is a credential handed out. And a folder the walk
  refuses or a symbolic link out of the snapshot is recorded once, by its own
  path rather than by the files under it, so the record says what was never
  looked at without a dependency tree filling it. Line endings are folded to
  LF on the way in and the copy is written and read back with the platform's
  translation off: a CRLF repository is numbered the way its author's editor
  numbers it on whatever machine the review runs on, and `bytes` and `sha256`
  are of the stored copy, so a reader can recompute them from `files/`.
  One more specialist is planned when, and only when, there is a snapshot:
  `spec-code`, which reads the whole manuscript first and then audits what the
  methods say was done against what the code does — thresholds, filters, the
  order of operations, the test actually called, the versions declared. Its
  three tools (`list_code`, `read_code`, `search_code`) list, read and search
  the snapshot and nothing else: none of them executes anything, reaches the
  network, or can name a path outside it, and the code is treated as data
  throughout, so a comment or README telling a reviewer what to report is text
  being audited rather than an instruction being followed.
  Every finding still quotes the manuscript, and a code finding carries the
  code as well — `code_path`, `code_start_line`, `code_end_line`,
  `code_quote` — which the server checks against the snapshot before it stores
  anything; a quote that is not at those lines is refused and nothing is
  stored. A finding that passes earns the new basis word `code-checked`, which
  the editor cannot bury as a `minor` suggestion without an anchored rebuttal,
  the same protection `computed` has. `REVIEW.md`, `read_run()['code']` and
  `progress.json` say which code was reviewed, at which commit, and how many
  files of the snapshot a reviewer actually opened, because "the code was
  reviewed" over a lane that opened one file of forty is a claim the run folder
  does not support. An arm that runs no specialists refuses `--code` by name
  rather than ignoring it, and an `ingest` attaching to a live run refuses a
  different `code` argument the way it already refuses a different arm.
- **The SPOT and Stage A scorers read the manuscript, so a finding has a place
  in the paper again.** Both scripts were written for the block manuscript and
  decided where a finding was by looking for the answer key's phrases in the
  reviewer's own sentences, which counted a finding that named Table 1 while
  discussing the conclusion and missed one that quoted the table and never
  named it. They now load the run's own manuscript and place each finding by
  the line it cites: the map row gives the kind of piece, the page, the
  section, and whether the converter read that text, a model repaired it from
  the page, or a model wrote it from nothing. Every finding in brief carries
  its piece and the kinds it cites, and the row carries the counts the reader
  comparison needs — findings citing a table, an equation or a figure, and
  findings resting on text the reader wrote or repaired — beside the Read
  stage's own account of what it did. `scripts/spot/score.py` takes `--slug`
  because a comparison names its arm in the folder (`cmp-antenna-on`), a run of
  a paper with no key is scored as findings and cost with no candidates, and a
  folder from before `paper-base-v2` is reported as not scored with the refusal
  rather than as a paper in which every finding is nowhere. Candidates are
  still candidates: no verdict is computed, and the cost numbers come from the
  run.
  What the reader contributed is counted per act rather than per source, because
  `ai-reconstructed` covers both a rebuilt equation and a paragraph the reader
  only cut a journal running header out of. A finding's piece now says which of
  the two it rests on, and the row splits the total three ways:
  `on_reader_rebuilt` for displayed pieces the reader rebuilt and pieces it
  added, `on_reader_cut` for prose whose every word is still the converter's in
  the converter's order, and `on_reader_rewritten` for prose the reader put a
  word of its own into. On the first antenna pair ten of the eleven findings
  resting on reader text were cut paragraphs, which the single number had
  reported as reader-written evidence.
- **The Read stage driven in code, for a consumer's tests:
  `peerreviewagents.scripted_read(paper, folder, reader, pieces=…)`.** The
  first of the scripted helpers above, and the one with the converter's own
  failures in it: `pieces` are extra converter pieces spliced into the draft,
  so a fixture holds what the stage exists for (a mangled equation, debris left
  as a heading of its own) without a PDF that has them, and
  `slot.find('a few characters')` names a piece by what the converter read
  there and raises unless exactly one matches. `base()` reads the sealed
  workspace back, and `sealed` is the reader's own answer to
  `seal_manuscript`. What the stage leaves on a manuscript — a re-read part's
  `part` and `of`, a fold's `absorbed`, the reader's `note` — existed only in
  the output of a real run, so the first consumer's fixtures for it were
  hand-written JSON copying the shape by eye, which drifts from the engine the
  moment the engine moves.
- **`ai_read['gone']` counts the pieces that left the manuscript.**
  `actions` is the pieces the sealed documents hold, counted by what was done
  to each, so a piece folded into another or withdrawn is in no document to be
  counted and a stage that folded three headings away left no number anywhere
  saying it had. The counts are now kept as the pieces go: `absorbed`
  (converter pieces folded into the piece they were debris of), `merged` (the
  other halves of an equation put back together) and `withdrawn` (pieces this
  stage added and took out again). They do not add up with `actions` — a fold
  moves a piece from the one to the other — and keys with no count are left
  out.
- **A ledger of your own: `peerreviewagents.citation_uses(base, references)`.**
  Every in-text use of a reference in a manuscript base, as the engine's own
  rows: the citing sentence, its line range and offsets, the piece it sits in,
  the reference it resolves to, and the `row_id` a round keeps its identity by.
  An application that wanted to check citations itself — or to build a ledger
  fixture for its own tests — had to re-implement the anchor rule the engine
  already applies, and a sentence that spans a line break or a marker that
  matches nothing are exactly where a second implementation goes wrong.
- **One model reads the paper's pages before any reviewer reads its text.** A
  Read stage sits between the import and the review, one slot (`reader`,
  `--reader-model`, default `claude-sonnet-5-5`), and the import no longer
  writes the manuscript: it writes a draft, `draft.json`, which has no line
  numbers and which no reviewer, specialist, citation worker or editor can
  claim a slot against. The reader works through the draft against the page
  images — for every equation, table and figure it either repairs the piece
  (`replace_equation`, `replace_table`, `describe_figure`, `merge_equations`
  for an equation the converter split, `add_piece` for one it dropped) or
  confirms it as printed, and it must open every page — and a repair is refused
  unless that page has been viewed. Then `seal_manuscript` writes `paper.md`,
  `map.json` and the manifest, once, and line numbers never move again; the
  seal is refused, naming what is left, while a single piece or page is
  unaccounted for. Its allowance is `10 + pages + 2 × pieces` rather than a
  constant, because a four-page letter and a forty-page paper with sixty
  figures are not the same job.
  The converter is the reason: on the antenna manuscript it produced eleven
  equations and about half were wrong, two of them with its own confidence at
  `1.0`, so the confidence cannot say which to trust; and it produces no figure
  image at the pinned version, so every figure reached every reviewer as a
  caption and nothing else, which is also why most reviewers in seven saved
  runs opened no page at all.
  What a reviewer now reads is marked: a repaired equation says it was read
  from the page image by that model, with the page, and a figure's description
  is its own line under the caption, marked as an AI description of the image
  rather than the authors' words. The `view_page` gate on equation findings
  stays as it is until there is a measurement that says it can go.
  Provenance is per row in `map.json`: `text` is the converter's own reading,
  `ai-reconstructed` an equation or table rewritten from the image — with the
  converter's reading kept beside it in `original`, so a repair can be audited
  — and `ai` a description or a piece the converter missed.
  `manifest['ai_read']` names the reader model and counts the pages viewed, the
  pieces settled and the calls spent, `manifest['pieces_per_source']` counts
  the rows per source, and the base schema is `paper-base-v2`:
  `paper-base-v1` is refused by name, since nothing in it says which pieces
  were the converter's own. Supplements go through the same stage under the same
  rules, as documents of their own.
  **Every PDF review runs the stage, and there is no way to turn it off**:
  the reader is planned with every other slot, and a paper with a page the
  converter returned nothing from cannot be imported at all without a reader
  to transcribe that page — measured on the exosome paper, whose page 8 the
  converter loses. Reader-on and reader-off runs over three papers caught the
  same known problems, so one path is kept rather than two manuscripts that
  cannot be pooled. A manuscript sealed from the converter's own text, with
  today's garbled-equation markers, no figure described and
  `manifest['ai_read']` as `{"read": false, "reason": …}`, is now reached only
  by a reader that fails twice, through the recorder's new
  `seal_as_converted`; one code path writes the base either way, so a run never
  holds a half-written manuscript. `write_manuscript` from an app needs no
  reader and seals at once, as it did before — an app that parses its own Typst
  source already has the equation.
- **The reader may re-read any piece on a page it has viewed, as the pieces
  that page actually prints.** The first real run found the limit of a stage
  whose tools could only reach what the converter had already labelled an
  equation, a table or a figure: the converter emitted two of that paper's
  tables as one-line paragraphs of run-together cells, a displayed equation as
  a paragraph with the next equation's symbols left in the prose after it, and
  one "table" that held a table, the next table's caption and that table too —
  and `replace_table` wrote the first of those over the lot, which left the
  rest in no file and nothing in the manifest saying so. So the kind gate is
  gone from `replace_equation` and `replace_table`, and `reread_piece`
  replaces one piece with several, in printed order, each keeping the
  converter's whole reading in `original` with `part` and `of`. **Prose the
  stage never types**: a `paragraph`, `footnote`, `heading` or `title` part of
  a re-read carries no text, only `remove` — the spans to cut, at most three
  and together at most a fifth of the piece — or `keep`, the one run of it that
  piece is, and the engine cuts them, so what comes out is a slice of the
  converter's own characters. `keep` drops everything it does not name, which
  `keep='Cytokine storms, which'` on an abstract turned into a sealed paragraph
  of three words, so it is allowed only beside the table or equation part of
  the same re-read that accounts for the rest; a re-read that is prose alone
  uses `remove`. A span is matched on the normal form a citation's
  quote is matched on and has to read there exactly once, and `view_page` now
  returns that page's pieces whole, since a span cannot be named inside a
  preview the listing cut off. The first attempt at this let a re-read retype
  prose as long as every character kept was one the converter read, in order,
  and that does not hold: almost any short sentence is a character subsequence
  of a long paragraph, so an abstract re-read as its own text plus "This result
  is highly significant." was accepted and reached the sealed line. A caption
  is never written over by a single replacement, because the
  piece it names would be left without one. `withdraw_piece` takes out a piece
  the stage added and refuses the converter's — a caption added with its text
  in the wrong argument had rendered as `Caption:`, passed a check that was on
  the rendered line rather than the content, and reached a sealed manuscript
  with nothing able to remove it; `add_piece` now refuses an empty piece by
  kind and takes a `caption` on any viewed page, since the converter loses
  those. `seal_manuscript` takes a `note`: the reader's own account of the
  document, served above the manuscript as that model's words, kept in
  `manifest['ai_read']['note']` and on `read_run`'s `manuscript` key. That
  run's reader knew a table had been dropped and two more were garbled, and
  said it in a report that reached no file.
- **A piece that is a displayed piece's debris is folded into it, and a page
  the converter returned nothing from comes with its text layer.** Two runs of
  the rewritten stage over two real papers, each sealed first try, left exactly
  two things the reader named in its own note and no tool could reach. One:
  debris the converter emitted as *pieces* rather than inside a paragraph —
  three headings reading `with {`, `qn,s,d else` and `|A|2 d=1`, each the edge
  of a displayed equation on the same page, and a bar chart's axis numbers,
  `120 100 80 … Concentration(µl/ml)`, as a paragraph above its caption. A span
  in `remove` cannot reach a piece, nothing asked for any of them to be
  accounted for, and `withdraw_piece` refuses a converter piece, so they
  reached the sealed manuscripts as headings and lines of prose. So
  `merge_equations` now takes members that are not equations, and
  `describe_figure` and `reread_piece` take `absorb`: neighbour pieces that
  leave the document, each one's whole converter reading kept under
  `original['absorbed']` on the piece that absorbed it, which is what makes it
  a fold and not a deletion. A re-read writes several pieces, and the fold goes
  on one of them — the first part that is not prose, which is the displayed
  piece the debris was printed as part of — because one fold is one fold: on
  every part, the one folded heading reads to whatever renders the map as one
  fold per part, three parts and three headings. It is refused unless the piece
  really is debris —
  the converter's own rather than one this stage added, prose by kind (an
  equation, a table or a figure is accounted for in its own right), on the page
  being viewed, at most 200 characters of reading, next to what takes it in
  with none of the paper in between, and at most three in a call — and
  `reread_piece` refuses it for a re-read that writes prose alone, where
  nothing would be left for the debris to be part of. Two pieces the converter
  labelled equations are held to neither adjacency nor the page, since one of
  them may be the tail of the equation at the top of the next page. Two: the
  same footnote letter was misread the same way in both runs — `−2.023d` on
  the page, `−2.023b` in the caption, twice — off a table printed sideways on a
  page the converter returned nothing from, where the letter is a few pixels
  and the reader's note said the letters were a best guess. The page itself is
  upright (`/Rotate 0`, so no renderer flag helps) and the render is already at
  the long edge a standard-tier vision model accepts, so the image is not where
  the fix is: `view_page` now also returns `text_layer` for those pages, the
  words Poppler read off the page's own text layer with the whitespace
  collapsed, which the import had been computing to count the page's words and
  now keeps. The answer labels them as characters to check against — a footnote
  letter, a subscript, a minus sign — in no reliable order and with no layout,
  and not something to transcribe from or to read a reading order off. They
  stay off the manifest, which records the page, its word count and its
  rotation as before.
- **A citation says where the quote is inside its line, everywhere a consumer
  reads one.** An issue's members now carry `start`, `end` and `anchor_text`
  beside the line, and a citation ledger use row carries `start`, `end` and
  `end_line` for the citing sentence with `piece_lines` keeping the passage it
  was read from. `read_run`'s findings already had them. So all three now keep
  one rule — `anchor_text == line_text(line)[start:end]`, and from `start` in
  `line` to `end` in `end_line` for a quote that crosses a line break — and
  `peerreviewagents.cited_text(base, citation)` is that read, both cases, for
  any of them. An app that had `issues.json` knew which line to open and then
  had to search the line for the words again to underline them: the search the
  engine had already done, done a second time by code that cannot refuse a
  quote that occurs twice in the line. `issues.json` is stamped
  `review-issues-v4`, and that is the only stamp this release reads: the
  reading is forward only, as the manuscript base is, and `review-issues-v2`
  and `review-issues-v3` — both written and superseded inside this release,
  never shipped in one — are refused by name like any other stamp it does not
  know. A ledger use row's
  `line` now locates the citing sentence rather
  than the paragraph around it, which is the point of the row; `row_id` is
  still built from the paragraph's first line, so a row keeps its identity
  across rounds. `docs/INTEGRATIONS.md` states the rule in one place and both
  the finding path and the members-only path a run without a ledger is read
  through are held against the characters of the file in the suite.
- **The manuscript base is on the public surface.** `write_manuscript(folder,
  pieces, ...)` writes a whole manuscript workspace — the documents, `map.json`,
  `references.json`, `statistics.json` and a `manifest.json` whose `files`
  digests the folder — from pieces a caller composed itself, and returns that
  manifest, as `import_paper` does. `read_manuscript(folder)` reads one back as
  a `PaperBase`, which is `verify_workspace` without the digest check, for an
  application showing a folder rather than reviewing it; `PaperBase`,
  `BASE_SCHEMA_VERSION` and `MAIN_DOCUMENT` are exported with them. An app that
  keeps its manuscript in its own format — a Typst source it parses itself —
  had to write `paper.md` and the map by hand against a private engine module,
  which is a second copy of the format in a place no test of this package
  reaches. A piece's `page` and `bbox` are optional, because a Typst source has
  no pages until it is built: a pageless piece gets `null` in both columns, the
  reviewer's `view_page` refuses, and the citation is the line, which is what
  it already was for a PDF.
  `docs/INTEGRATIONS.md` documents what a piece is on the way in, field by
  field, what `verify_workspace` returns, and what a scaffold workspace must
  contain. The suite's own manuscript fixtures are built through this call, so
  a test anchors against the workspace an application writes.
- **A box in `map.json` says what unit it is in, and the manifest says how big
  the page is.** `manifest['pages']` is one `{page, width, height, rotation}`
  record per page, in the same points, and `manifest['box_convention']` states
  the unit and the origin: PDF points, origin at the top-left corner, `y`
  growing down. Verified against `pdftotext -bbox` over the shared fixture,
  word for word. An application drawing a highlight on a page image had both to
  guess, and a guessed origin puts the box at the other end of the page.
  `import_paper` reads the sizes from the PDF; `write_manuscript` takes them as
  `pages=`. An empty list means the sizes were not recorded — no pages, or no
  `pdfinfo` on the importing machine — and never a default page size.
- **The tolerant readers of an issues document are documented**: `rows_of`,
  `objects_in`, `members_of`, `quotes_of`, `block_ids_of` and `audits_of` in
  `docs/INTEGRATIONS.md`, beside `field_problems`, the malformed fields
  `read_run` reports a damaged document through. A list field held as `null` —
  `members`, `audits` — is malformed rather than empty, because every one of
  them is a list in what this engine writes, empty where it does not apply.
  (The `validate_prior_issues` surface this release first added went out again
  with `--prior-issues` before the release: see **Removed**.)
- **`read_run` says what state the manuscript is in**, under a new
  `manuscript` key: `state` is one of `MANUSCRIPT_STATES` — `absent`, `draft`,
  `sealed-by-reader`, `sealed-without-read` — beside `sealed`, `reason`,
  `reader_model`, `lost_pages` and `documents`. An application could not tell a
  run whose Read stage stopped partway from one that never imported a paper
  without looking for `draft.json`, which is this package's working file and
  not part of the folder a consumer is promised. `sealed-without-read` is the
  one state that covers two roads to the same folder — a reader that failed and
  sealed as converted, and an application's own source through
  `write_manuscript` — and `reason` is the manifest's own words for which of
  them it was.

- **What an agent spent is reported in both units, under names nobody can mix
  up.** Every usage receipt, every `runtime.json` agent row and every row of
  `read_run`'s `agents` now carries `tokens_cumulative` — the session's whole
  spend, every call's usage added up, which is what the ceilings are measured
  against — its four parts (`tokens_input_uncached`, `tokens_cache_write`,
  `tokens_cache_read`, `tokens_output`), and `tokens_peak_context`, the largest
  single call. `peerreviewagents.names.TOKEN_FIELDS` is the list.
  **Totals before and after this release are different units**: up to 0.9 the
  per-agent figure came from the workflow harness, which reported the widest
  single call, and `tokens_peak_context` is the field that compares with those
  runs. One real reader spent 592,106 cumulative tokens while holding 144,072
  at its widest, so a dashboard plotting the old and new totals in one series
  will show a step change at this release that nobody's spending explains.
- **The token ceilings are a runaway guard, not a budget, and they are set
  like one.** `--per-agent-tokens` is 3,000,000 (was 500,000) and
  `--per-run-tokens` 30,000,000 (was 6,000,000); the fix check's are 900,000
  and 4,500,000 (were 150,000 and 900,000). The turn cap remains the first line
  of defence against a looping agent. The old per-agent ceiling had been
  calibrated in the old unit and enforced in the new one, which stopped a
  reader that had read all 29 pages of a paper, repaired every piece and sealed
  the manuscript. All six numbers are provisional until one full review has
  been measured end to end. No call budget was affected: the per-agent call
  ceilings are enforced on the server's own count of the tools it served, not
  on the session's.
- **`read_run`'s agent rows say how each agent ended, in the runner's own
  words.** `ending` is `answered`, `no answer`, `over the token ceiling`, `out
  of time`, `failed`, or `in process` for the recorder slot, whose tools this
  package calls itself with no model behind them — an application had to infer
  that last one from an empty `model` field. `detail` is the sentence the
  runner left with it. Both were already in `runtime.json` and were dropped on
  the way out.
- **`docs/INTEGRATIONS.md` writes down what the agent isolation does not
  cover.** A session's init event lists the owner's agent types — inert,
  because an agent is given no tool that could start one — and loads whatever
  built-in plugins the machine has, which varies between machines. There is no
  documented switch that keeps plugins out of a headless session: `--bare`
  skips plugin sync but forces API-key authentication, and these runs use a
  subscription login. A stronger boundary than this means a container.

### Fixed

- **A run with its per-run ceiling spent handed the next seat no ceiling at
  all.** Zero on a task means *no* ceiling, and the panel's own arithmetic
  handed out zero once the run's allowance was gone — the exact opposite of
  what a per-run ceiling is for, and the one case where it mattered. An
  exhausted run now hands out the smallest ceiling there is, so the next seat
  is over it before it starts and the run stops.
- **Being stopped is not the same as having nothing to say.** An agent that
  submitted its answer and was then stopped — over the token ceiling, out of
  time — is `answered`, with the stop named in its `detail`, and the answer it
  gave is the one its stage uses. Two things used to throw that work away: a
  stop that beat the harness's own result event left the answer unread, so the
  last answer in the stream is now taken as the answer; and the stage then
  retried. **No stage retries work that is already finished**: before a retry
  it asks the server whether that slot has already closed on the ledger, and
  the Read stage asks whether the manuscript is already sealed, and either way
  it carries on with what is on disk. A reader that had sealed a 29-page
  manuscript was retried, refused by the server because the manuscript was
  sealed, and the whole run blocked on work that was complete.
- **A turn that called fifteen tools is fifteen calls, not one.** A real
  reader's stream sends one event per call — the same assistant message id and
  the same usage on each, a different `tool_use` block in each — so counting a
  turn's blocks and keeping the largest count credited that whole turn as a
  single call. The reader of a 29-page paper made 46 calls and the run recorded
  nine. Each block is now credited by its own id, which is right whether the
  blocks repeat (one call) or arrive one at a time (one call each), and the
  per-message token credit is unchanged.
- **A refusal the reviewer answered is not a finding the run lost.** A quote
  that occurs twice is refused with the lines it occurs on; a reviewer that
  then cites one of those lines gets the same finding stored and grouped into
  an issue. The folder used to report both as independent facts, so a review
  that lost nothing still read `review_guard_status: quarantined` with
  `quarantined_findings_count: 1`. Every quarantined row is now stamped with
  the `finding_uid` that replaced it — same slot, same words, same title — in
  `superseded_by`, and the guard status, `quarantined_findings_count` and
  `read_run`'s `counts['quarantined']` count only the rows nothing replaced,
  with the rest in `quarantined_superseded_count`. The stamp is made when a run
  is read, so `quarantined.jsonl` stays append-only and a folder written before
  this release reads the same way as one written now; a `guard_status` recorded
  in an older folder is left as it was written, and the counts are what a
  reader should use.
- **A stage closes on `none_found` whether the word arrives quoted or not.** A
  session that submitted `none_found: "true"` was refused for the type of an
  argument the tool deliberately leaves unannotated, so the refusal reached the
  ledger and the stage did not close. The word `true` or `false`, in any case
  and with any surrounding space, now reads as the boolean it is; anything else
  — `yes`, `0`, `[]`, nothing at all — is still refused, and in the tool body
  where the refusal is logged.
- **One assistant message is counted once, however many events carry it.** A
  real stream sends one event per content block, each with the same message id
  and the same cumulative usage, so a session that wrote text and called a tool
  in one message had its tokens counted twice. Usage is now read per message
  id, and the ceiling an agent is stopped at is the one the orchestrator set.
- **A session that starts up wrong ends there instead of reviewing nothing.**
  The init event is read before the agent is believed: our tool server not
  connected, a granted tool missing from the session, or any built-in other
  than `StructuredOutput` present ends that agent as `FAILED` with what was
  wrong. A silent misconfiguration used to look like a reviewer that found
  nothing to say.
- **Auto memory is turned off by the name Claude Code actually reads.** The
  environment set `CLAUDE_CODE_DISABLE_MEMORY`, which no release of Claude Code
  reads, in the belief that it was what kept the owner's instructions out. What
  keeps them out is `--setting-sources ''`: `~/.claude/CLAUDE.md` and
  `~/.claude/rules/*.md` load only when the sources name `user`, the project
  and parent-directory files only with `project`, `CLAUDE.local.md` only with
  `local`, and this session names none of them. Auto memory is the one
  instruction file read whatever the sources say, so it is now switched off by
  `CLAUDE_CODE_DISABLE_AUTO_MEMORY`, and the agent definition's `omitClaudeMd`
  is documented as belt to those braces rather than the thing doing the work —
  Claude Code scopes it to an agent spawned as a subagent, and ours is the main
  session of its own process.
- **A step that is told to call a tool is a step that can call it.** The first
  real fix check judged all six problems correctly and then could not write the
  check: `check-fixes.js` tells the recorder to call `write_fix_check`,
  `recorder.md` never granted that tool, and the one step that writes
  `fix-check.json` and `FIXES.md` answered *No such tool available*. The
  verdicts stayed in `review/fix-checks.jsonl` and the run ended blocked over
  one missing name in a tools list. The recorder is granted it now, and so is
  the pair's other half: the reclose prompt a review sends to a stage the
  ledger never recorded tells that stage to read its own findings with
  `list_findings`, which only the core reviewer, the editor and the recorder
  could call — a reopened specialist was being asked for something it had no
  way to do. The seven specialist definitions are granted it, and a reviewer
  still sees only its own slot, so nothing about review independence changes.
  A test now reads both sides of that seam: every agent call in both workflow
  scripts, the prompt it hands that agent and the helpers that prompt is built
  from, against that agent's own `tools` header — and the reverse, an agent
  granted a tool the server does not serve. It fails on both of the above.

- **A blocked run says why, in the session's own words.** A run that ends with
  nothing written and nothing marked blocked used to report only that the
  workflow ended without writing the review, which is true of every run that
  falls through and tells nobody where to look. The reason now also carries
  what the session said as it signed off, shortened — on the fix check above,
  the refusal itself.

- **No arm a review is offered is an arm it refuses.** `review_arms()` and the
  `--arm` choices listed `fix-check`, which `review` then refused by name: it
  is a run of the same workflow, but it answers problems a review already
  raised and a review has no problems file to give it. An application that
  offered every arm it was shown therefore offered one that could not run. The
  arm is still in the engine's table, because a check is a run of it, and
  `peerreview check-fixes` is still the only command that runs it — but every
  listing a review's arm is chosen from now comes from the arms a review runs,
  and a test pushes each of them through `review` and the parser so an arm
  added to the table without a thought about `review` is caught.

- **The code reviewer can read the data the authors shipped, and never chokes
  on a big file.** On the first real run of the code lane the file-count cap
  left out 555 data-like files, and one of them —
  `study/frozen-v1/corpus-selection.json`, 196 KB — held the answer to the
  finding the lane was writing: the paper said 200 records per query, the
  script's default was 100, and the file that said which one ran was the file
  the cap had dropped, so the reviewer could only write "I could not see it".
  Data is now listed rather than copied, and fetched when the reviewer asks
  for it. Program source, build and configuration are copied up front as
  before; every tracked data file — csv, tsv, json, jsonl, ndjson, yaml, toml,
  txt, lock — is a row in `manifest["data"]` and in `list_code` with its path,
  size and kind, and those rows spend neither cap. `read_code` on a listed
  path, and `search_code` scoped to one, fetches it then and there: the engine
  copies it in, records its bytes and `sha256` at that moment, and from then
  on serves and verifies it exactly like source, so a `code-checked` finding
  on a data line checks the same way. A file up to the per-file cap is fetched
  whole; a larger text file is never read whole, and what is stored is a head
  of 20,000 bytes cut back to the last whole line, marked `partial` everywhere,
  with the file's total size, its line count and a table's column names beside
  it — a finding may quote only the lines that were stored, because the rest is
  not evidence the review has. Binary data (parquet, h5, npy, xlsx, images,
  archives) and a file stored in Git LFS stay listed with a size and a reason
  and are never opened. A run may fetch 40 files and 4 MB of them, and a spent
  budget is a refusal in words that tells the lane to report what it did not
  read. Nothing is stat-ed or hashed twice and nothing is loaded whole: a
  50 MB table costs one streamed read and a megabyte of memory. For a local
  folder the fetch reads the working file, and one that changed since ingest
  says so in the answer and in the manifest. `read_run`, `progress.json` and
  the `REVIEW.md` "Code reviewed" line carry `data_listed`, `data_fetched`,
  `data_partial`, `data_refused` and `data_bytes`, and the lane's brief tells
  it to list first, fetch the data a claim depends on, and say which files
  were partial or unreadable.
- **A code snapshot of a repository holds the files it tracks, and a cap cuts
  the least useful code first.** Pointed at a real paper repository — 1,998 tracked
  files, 113 of them Python — the snapshot filled its 600-file cap with 582
  files of a gitignored build cache and none of the analysis scripts, so the
  code reviewer would have read a build cache and reported on it as the
  authors' method. Two causes, both fixed. A git checkout is now snapshotted
  from `git ls-files`, so `.gitignore` is honoured and caches, outputs and
  environments stay out; what git does not track is one `skipped` row per
  top-level folder with a count rather than thousands of rows, and
  `manifest["tracked"]` says when git chose the files, which it does for a
  clone of a URL as well. Outside a checkout the walk leaves hidden folders
  and known cache and build folders alone. And the caps no longer cut in walk
  order, which was alphabetical and therefore arbitrary: candidates are kept
  in the order a reviewer needs them — program source, then build and
  environment files, then configuration and prose, with shallower paths first
  inside a kind, and data no longer spending a cap at all (above) — and both
  the file count and the byte
  total cut in that order. The manifest says how many files of each kind a cap
  left out (`left_out_kinds`, `counts["kinds"]`), and when program source
  itself was cut, `source_cut` and the words of `truncated` say so, in the
  manifest, in `read_run`, in the `REVIEW.md` line that says what was
  reviewed, and in what the code lane's `list_code` answers — because a
  reviewer who has not seen every script may not report that no script does a
  thing. On the repository that found this, the snapshot now holds all 113
  tracked Python files and nothing of the cache.
- **A code snapshot never copies a run folder of this package.** Reviewing a
  repository from inside it is the normal way to do it — a workspace keeps its
  runs under the repository root, and `--code .` was the documented call — and
  the snapshot walked straight into them: this run's own folder, the sealed
  manuscript it had just written there, the reports and findings of every
  earlier run, and the earlier snapshot of the same code, all served to the
  code lane as the authors' source. The run being written is left out by name,
  because the snapshot is taken while its folder is still almost empty; every
  other run folder is left out by what the engine writes in one — a
  `review/protocol.json`, `review/findings.jsonl` or another of the markers
  `runs.locate` looks for — and never by what the folder is called, since the
  name is the caller's and a directory of the authors' called `review` is
  still their code. Each is one row in `manifest["skipped"]` with the reason,
  like any other folder never walked into. A `--code` path that *is* the run
  folder, or inside it, is refused in words by both the launcher and the
  server rather than snapshotted: nothing in it would be code. A repository
  URL is unaffected, which is the case the fix had to be written around — its
  checkout lives inside the run folder by design, so the run folder is
  compared as a whole path and never as a prefix.
- **A reader slot is planned only where there is a draft to read.** A run
  opened over a folder that already holds a sealed manuscript — an
  application's own source through `write_manuscript`, or a folder a run
  already read and sealed — planned a `reader` anyway: `ingest` handed back a
  claim code and a model for a stage that could not run, and claiming the slot
  raised an `AttributeError` out of `number_pieces(None)` instead of answering.
  Such a run now plans no reader at all — no claim code, no model, `reader`
  comes back `null`, and the launcher no longer refuses the session over a
  reader model nothing would run — and asking for the slot is refused in words
  that name the seal. A reader that claims late, after the manuscript was
  sealed around it, is refused the same way rather than raising: a claim is the
  first call an agent makes, and an exception tells it nothing about the run.
- **A run refused at the import says why it was refused.** `ingest` now
  records the server's own refusal in the run folder's `progress.json`, and the
  starter reads a recorded cause ahead of the session's exit, so a manuscript
  the engine would not import — a page the converter lost, a model the login
  cannot run — reaches `blocked_reason` in those words. The summary used to say
  only that the session had ended in an error, or that the workflow had
  returned no plan, while the refusal survived nowhere but the orchestrating
  model's own prose.
- **`scripts/spot/score.py --out` appends, as its help always said it did.** It
  wrote the file, so scoring the second arm of a comparison left one line where
  two scorings had happened and the first arm's row was gone.
- **Every served line whose source is not the converter's text carries a
  marker.** The marker was written for equations, tables and figure
  descriptions — the pieces a reader repairs — so a paragraph or a caption the
  reader transcribed off a page the converter returned nothing from reached the
  reviewer looking exactly like the authors' own converted text, which is the
  one case where the text is a model's reading of an image and nothing on the
  line said so. Any row whose `source` is `ai` or `ai-reconstructed` is now
  marked, whatever its kind. In a manuscript no page of which was read — an
  application's pieces through `write_manuscript` — the marker reads `[written
  by <model>]` and the header says no stage read the pages, rather than sending
  a reviewer to a page image that was never opened.
- **`ai_read['lost_pages']` carries the lines, and is written on every seal.**
  Each entry's `added` held draft piece ids, which name nothing once the base is
  sealed: `map.json` has no piece ids, so an application reading the manifest
  for where a lost page's text ended up had no way to ask. It now holds
  `{piece, document, lines}` — the same anchor a citation uses — and
  `known_gaps['converter_pages']` names those lines in its sentence. The key is
  also written when no page was lost, as an empty list, so its absence is no
  longer ambiguous between "nothing was lost" and "nothing counted".
- **`write_manuscript` refuses a manuscript that cannot say who wrote its
  model-written pieces.** It accepted `ai` and `ai-reconstructed` pieces and
  stamped `ai_read` `{"read": false}` regardless, so a manifest said the
  manuscript needed no reading while the base held a model's sentences with
  nothing to attribute them to, and a reviewer was served them unmarked. A
  caller passing such a piece now names the model in `ai_model=`, which the
  manifest records as `ai_model` and `ai_pieces` and the served marker names on
  the line; without it the write is refused by the piece's index and kind. An
  `ai-reconstructed` piece with no `original` is refused too: that source means
  a model rewrote text the document printed, and the row is where the reading
  it replaced is kept, so one without an original promised an audit the folder
  could not give.
- **An unstamped manifest is refused by name.** `read_manuscript` and
  `verify_workspace` refused a `paper-base-v1` folder and said nothing about a
  manifest carrying no `schema_version` at all, which was then read as the
  current shape on the strength of having no opinion — and its rows trusted to
  carry fields they may never have had. Both now refuse an unstamped manifest,
  naming `BASE_SCHEMA_VERSION`, and a manuscript folder with no `manifest.json`
  beside the base.
- **`manifest['ai_read']['actions']['added']` counts `add_piece` and nothing
  else.** `describe_figure` settled the description it wrote as well as the
  figure it read, so a run that described nine figures and never called
  `add_piece` reported `described: 9, added: 9`, and a reader of the manifest
  could not tell how much of the manuscript the stage had put back.
- **`write_manuscript` refuses a piece it would write nothing of, by name.**
  A piece that renders to no line — a list whose text sits under an `items`
  key, a paragraph with an empty `text` — was dropped, so an application that
  composed twelve pieces got a manuscript of eleven and counts that agreed
  with themselves because they were taken over the rows. A field the format
  has no column for was dropped the same way, between the caller's dict and
  `map.json`, so an app's own `anchor_id` on a piece was gone with nothing to
  say so. Both are now a `ValueError` naming the document, the index of the
  piece and its kind, before anything is written, and the writer checks that
  every piece it was given left a row. `paper_base.PIECE_FIELDS` is the
  vocabulary, and `docs/INTEGRATIONS.md` documents it as the whole of it.
- **Only the workspace's own `manifest.json` is outside the digests.** The
  file list skipped every file called `manifest.json` at any depth, so a
  source tree that keeps a manifest of its own — a figure inventory, a build
  record — had that one file uncovered: it could be rewritten under a saved
  review and the workspace still verified. The skip is now the workspace's own
  manifest and nothing else.
- **An import that would lose a page of the paper is refused, by page.** The
  pinned converter returns no block at all for a page whose `/Rotate` is not
  zero, so a one-page PDF turned ninety degrees imported to a `paper.md` with
  nothing in it, and a paper with one landscape table page imported without
  that page and said nothing about it. The engine cannot repair this — the
  text never reaches it — so `import_paper` now compares the pages the
  converter returned blocks from against the pages Poppler's text layer reads
  prose off, and refuses the import naming the pages and their rotation, with
  the converter and its version to report it against. A conversion that
  returned nothing at all is refused in the same way rather than written as a
  manuscript with no lines in it. A page that carries only a running head or a
  page number, as a full-page figure does, is not a lost page.
- **A cancelled `resolve_citations` no longer ends the session.** A client that
  sent `notifications/cancelled` while a lookup was in flight took the whole MCP
  session down when the pass finished: the cancellation completed the request's
  responder, the pass ran on in a worker thread that `anyio.to_thread.run_sync`
  does not abandon, and the lowlevel server then answered a request the
  cancellation had already answered — "Request already responded to", out of the
  task group that serves every agent in the run, with the folder left at
  `running`. The pass now reaches a cancellation checkpoint after it has written
  its work, so a cancelled call keeps its ledger rows, receipts and
  `progress.json`, releases the stage, and the next `resolve_citations` or
  `write_review` is served by the same session. Every exit path of the call goes
  below that checkpoint, the rejection a failed pass answers with included:
  returning that rejection above it left one path — cancel the call, then have
  the pass raise — that still answered a cancelled request a second time and
  took the session down with the run at `running`. A pass that fails now leaves
  the stage open, `progress.json` written to what it reached, and the run still
  `running` for the next call. `resolve_citations` is the only tool that awaits;
  a synchronous tool answers inside one loop step, so none of the others could
  reach this. A cancelled pass is not a stopped pass: the worker thread runs to
  the end of its lookups, and `write_review` is refused until it does.
- **A malformed `issues.json` is reported, never raised.** `members` held as a
  string was iterated one character at a time into `str.get` and raised an
  `AttributeError` out of `read_run`, so a damaged file took the reader down
  instead of being reported by it; a row that was not an object did the same out
  of the renderer. Every row and every list field is read through one tolerant
  reader now: what cannot be read is skipped, named in `problems` — which makes
  the run not publishable with that reason — and everything around it still
  comes back. `read_run` and `write_reports` both read such a folder, and the
  neighbouring fields are covered with `members`: block ids, `audits`,
  `surviving_quotes`, and the issue rows themselves.
- **The Record stage retries the one refusal that is not final.** A
  `write_review` refused because a `resolve_citations` pass was still looking
  references up writes nothing and says so, but the workflow read it as a run
  that would not be written and blocked a run whose references were simply
  still being fetched. The stage now asks the citation table where it stands and
  tries the write a second time. This is one bounded retry, not a wait: nothing
  in the stage can block on another agent's pass — `resolve_citations` answers
  `already_resolving` immediately while one is held — so a pass still running
  when that second write lands refuses it again, and the stage reports that
  refusal by name instead of reporting a run that would not be written.
- **A method claim is read against the code before it becomes a required
  change.** A finding that rests on how something was computed, sampled,
  filtered or selected used to reach the editor on the reviewer's reading of
  the methods section alone, and the code snapshot that could settle it was
  read by one specialist for its own audit. Between the reviewers and the
  synthesis, every finding whose basis is `computed` and every one whose
  reviewer named the step it assumed in the new `assumed_method` field now goes
  to a check of its own — a slot with the snapshot's three read-only tools and
  nothing else — and comes back `confirmed`, `contradicted` or `not-shown`.
  `confirmed` and `contradicted` each carry a passage of the snapshot, verified
  at those lines before it is stored, exactly as a `code-checked` finding is;
  `not-shown` carries the searches that found nothing, because "the code does
  not show it" is a claim about what was looked for. A `contradicted` finding
  is in no issue and in no required change: the editor is refused if it groups
  one, and the finding is reported in `REVIEW.md`'s own "Checked against the
  code" section with the code that disproved it, which is more use to the
  authors than its silent absence. The record is `code_check` in `issues.json`
  and in `read_run`, one append-only row per answer in
  `review/code-checks.jsonl`, and the counts in `progress.json`. At most twelve
  findings are checked in a run; a finding that qualified past that cap is left
  exactly as its reviewer filed it, and `over_cap` says how many, so nobody
  reads an unchecked finding as a cleared one. A run given no code has no check
  and `code_check` is `None` — nothing about a review of a paper alone changes.
- **`read_run` returns the code check, so no application opens `issues.json`
  itself.** The record was published and documented but absent from the
  reading, which left an app that wanted to show what the check did reading the
  one file this package promises to read for it. `read_run(path)['code_check']`
  is now that record — `cap`, `checked`, `over_cap`, `counts` per verdict and
  `checks` — taken from `issues.json` where the review was written, and
  otherwise from `review/code-checks.jsonl` and `progress.json`, so a run that
  stopped before the review still reports the claims it answered. The counts
  are taken over the rows that come back, so a consumer's tally and its rows
  cannot disagree. `review/code-checks.jsonl` is documented in README's list of
  what a run writes, beside `quarantined.jsonl`, which was missing from it too.
  `CODE_CHECK_VERDICTS` — the three words a row can carry — moves to
  `peerreviewagents.names` with the rest of the vocabulary a consumer reads.
- **A review whose every finding the code disproved is a finished review.** It
  read as the opposite: `findings_source` came back `ledger` and the run was
  not publishable, with the reason that the findings come from the ledger and
  no issue accounts for them — over a review that is written, correct and has
  nothing wrong with it at all. A contradicted finding *is* accounted for: it
  is in the review's own thrown-out section with the passage of the authors'
  code that settled it. Such a run now reads `findings_source` `issues` and
  `publishable` `True`, and `complete-without-findings` has a third way to
  happen beside nothing raised and the guard emptying it, which
  `code_check['counts']['contradicted']` distinguishes.

### Removed

- **The recorder has no model, so nothing asks for one.** Writing the review,
  the audits and the fix check is the orchestrator's own work in process, so
  `models_requested` has no recorder slot and `--recorder-model` is gone from
  `peerreview review` and `peerreview check-fixes` — passing it is an error,
  not a flag quietly ignored. There is no shim: a caller that named the
  recorder's model names a reader's or a reviewer's instead.
- **Nothing launches a server, and no workflow script runs a review.**
  `workflows/review.js`, `workflows/check-fixes.js`, the plugin's `.mcp.json`,
  the `peerreview-plugin-server` console script, the `PEERREVIEW_MCP_COMMAND`
  variable the plugin used to launch the server with, the `recorder` agent file
  and the start-up probe that spent a throwaway session proving the plugin had
  loaded are all gone. The three skills (`peer-review-manuscript`,
  `check-draft`, `check-fixes`) shell out to `peerreview review` and
  `peerreview check-fixes`, so a session you drive yourself and a command you
  run yourself are the same run. `read_run()['startup']` keeps its `probe` key,
  `None` for every run this version writes, so a 0.9 folder still reads.
- **A review no longer carries an earlier review.** `--prior-issues` on
  `peerreview review`, `prior_issues` on `ingest` and on the workflow, the
  `prior_rulings` argument of `submit_editor`, the brief the editor was given
  about ruling on an earlier round, the `follow-up` skill, and
  `validate_prior_issues` on the public surface are gone, with no flag kept as
  a shim. Every review is a fresh panel reading the manuscript as it now
  reads: nothing is inherited, so nothing can be inherited wrongly. The
  rulings the editor used to make on a prior issue — `resolved`, `unverified`,
  "not raised again", carried forward — went with it, and with them
  `ISSUE_STATUSES`, the `status`, `status_reason`, `prior_id`, `prior_rank`,
  `text_unchanged_reason` and `surviving_quotes` fields of an `issues.json` row
  (12 fields now, from 18), the `prior_round` block of that file, REVIEW.md's closed-row and
  carried-issue sections, and `read_run()`'s `prior_round` key together with
  `counts['resolved']`, `counts['unverified']` and `counts['by_status']` (36
  keys now, from 37). A later review of a revised paper is a new out folder,
  and a consumer that wants the two compared compares two finished runs
  itself — the one thing the engine could never do honestly, since a paper's
  id is a digest of its PDF and no finding of one run shares a `finding_uid`
  with the next. The tool count is unchanged at 44: this removed arguments and
  fields, not tools.

## [0.10.0] — 2026-11-15

The report is issues now, not a body and an appendix, and an editor that cannot
finish says why. The shape a consumer reads changed, so this is a minor bump and
not a patch: `issues.json` is the source of truth, `read_run()` returns issues
rather than a body and an appendix, and a run folder written by an older engine
is refused rather than re-rendered. No model was run for this change: every
number below comes from the test suite and from the saved runs in `runs-0.9.0`
and the overlap experiment.

### Changed

- **The issue is the unit of the report.** The editor no longer decides a
  placement per finding; it groups the findings into issues — one problem, one
  issue, with its findings kept inside it as `members` — and gives each a short
  name, a severity (`major`, `moderate`, `minor`), a category from a fixed
  vocabulary, a rank and a one-paragraph statement. "Raised by N" is counted
  from the members' slots rather than asserted. `submit_issues` takes them in
  batches — at least twelve, and wider as the finding count grows, so the
  submissions always fit inside the editor's turn cap — and is keyed by name, so
  a refused batch is re-sent rather than restarted. Its `retire` argument gives
  the editor the three edits a stored issue used to be beyond: merge (retire one
  name, submit another with the combined members, one atomic call), rename
  (retire the old name, submit under the new one) and drop (retire alone).
  `submit_editor` is the finalizer: it takes the verdict and refuses while a
  stored finding belongs to no issue or the ranks are not exactly 1..N.
- **Nothing is capped and nothing is hidden.** `BODY_LIMIT` and the
  `body`/`appendix`/`duplicate` placement are gone, with no flag to bring them
  back. The major and moderate issues are the required changes, in rank order;
  the minor ones are suggestions grouped by category.
- **A checked receipt is not downgraded in silence.** Placement is gone, so the
  rule that kept a `computed` finding — one whose arithmetic the server checked
  against the manuscript — out of the appendix now holds on the severity axis
  instead: an issue called `minor` that holds a computed finding needs a
  `rebuttal_quote` (with `rebuttal_block_id` where the passage is not the
  finding's own block) that anchors exactly, and the quote is kept on the issue
  in `issues.json`. Calling arithmetic a suggestion takes the same evidence it
  always took.
- **`issues.json` is the source of truth**, one versioned document per run, and
  `REVIEW.md` is rendered from it and nothing else. Every arm writes one: where
  no editor runs, the server synthesises one issue per finding and the document
  says `issues_source: "server"`, so `draft-check` and a full review read in
  one shape. The specialist audits stay on disk as evidence and the issues
  that rest on them link to them.
- **`read_run` returns `issues`, `issues_source` and `prior_round`**, and its
  `counts` carry `issues`, `required`, `suggestions`, `resolved`, `unverified`,
  `issue_members`, `by_severity`, `by_category` and `by_status`. `body`,
  `appendix` and `findings_source: "editor-package"` are gone; `findings_source` is now
  `issues`, `ledger` or `none`, and the findings themselves always come from
  the append-only ledger, with the issues' members as the fallback when that
  file is lost. `peerreview report` prints the issue counts beside the finding
  count.
- **An arm with no editor is not publishable, and says which reason it is.**
  `publishable` was already false for `draft-check`, for the absent editor
  package; now the reason names the synthesis instead.
- **`docs/paper-scaffold-adapter.patch` is regenerated against issues.** The
  ledger row is the issue, not the finding, and the severity map keeps
  `moderate` as a required change — `major` to major, `moderate` to major, an
  escalated `major` to blocker, `minor` to minor — with the engine's own
  severity written into the row beside the issue id. The old map sent
  `moderate` to `minor`, which lost exactly the distinction an author needed.

### Added

- **An earlier round's issues can be passed in**: `--prior-issues PATH` on the
  review, `prior_issues` to `ingest` and to the workflow, refused at ingest for
  an arm that runs no editor because nothing in it could rule on a prior round.
  The editor matches this round's issues against that document by reading them,
  keeps the matched id, and marks each one `persisting` or `regressed`. Nothing
  closes by omission: every prior issue needs either a match or an explicit row
  in `submit_editor`'s `prior_rulings`, `resolved` or `unverified`, with a
  reason. Where a prior member's quote still appears verbatim in the new
  manuscript, `resolved` additionally needs a stated reason why it is fixed
  although the text reads the same, and the surviving passages are kept on the
  row as `surviving_quotes`. `unverified` is the status for "not raised again
  this round and not verified fixed" — the thing the pilots kept recording as a
  resolution. A `resolved` row is reported in the round that resolved it and
  then dropped from later rounds' carry and counts, so a tally cannot grow by
  recounting; a carried closed row holds `rank: null` and its old rank as
  `prior_rank`, so it cannot collide with a live issue's rank. One problem keeps
  one id across rounds, so a follow-up is a comparison and not a re-derivation —
  text hashing was tried first on the saved pilots and predicted merges poorly.
  `prior_round` in `issues.json` records the file's name and a digest of its
  contents, never a path from the machine that ran it.
- **`editor-log.jsonl`**, one row per editor submission call and per refusal,
  with the reason and the problems that caused it. `read_run`'s
  `stages['editor_trace']` summarises it — calls, accepted, refused, the call
  budget and the last refusals — and `reports['editor_log']` names the file.

### Fixed

- **The editor failed silently on large finding sets.** On the overlap
  experiment's fish-antibiotic paper, 42 and 46 findings, four runs out of four
  ended as `The editor never submitted an accepted pooled review` with nothing
  on disk to say why. Three things did it and all three are fixed: the turn
  budget did not fit the work (the editor's `maxTurns` is 24 and the work was
  one decision per finding, so the fix is batched issues — the editor now makes
  about ten to twenty decisions instead of forty-six — plus `maxTurns` 60 and a
  per-run server call budget that scales with the finding count); refusals said
  what was wrong without saying what to send instead; and `read_blocks` raised a
  bare `KeyError` on a `finding_uid` passed where a block id belongs. A blocked
  run now reports the editor's own numbers and the last refusal in its `cause`,
  and the trace stays in the run folder.
- **`protocol.json` recorded an allowance from before the findings existed.**
  It was written once at ingest, when the ledger was empty, so
  `editor_tool_allowance` read 12 for a run whose editor was handed 37 — the
  wrong number to debug a run that ran out of them with. The protocol is
  rewritten as each slot claims, so it states what that slot actually got.
- **The editor could still dead-end on a stored issue.** A stored issue could
  not be merged, renamed or dropped, while the refusal that fired when a finding
  was already a member of another issue told the editor to merge — so it looped
  to `maxTurns`. `retire` makes all three possible and the refusal now names
  them.
- **A re-claimed editor started from nothing.** The working set and the accepted
  package belong to the slot, not to the attempt, so a retry keeps what was
  submitted before; and `submit_issues` after an accepted close reopens the
  package and removes the stale `issues.json` instead of refusing.
- **The budget and the turn cap disagreed.** The server's call allowance could
  pass the editor's `maxTurns` near 80–100 findings. The turn cap is now the one
  source of truth: the allowance is capped eight turns below it and the batch
  width grows with the finding count so the submissions fit. The editor path
  supports up to 200 findings; above that the editor slot is refused by name and
  the run completes on the server's synthesis, not publishable, with the reason
  in `limitations`.
- **"The editor never closed" could not be read.** `write_review` answered with
  the silent stages before it reached the trace, so the one case that needed the
  numbers — an editor that never called `submit_report` — got none of them. The
  trace and the refusal count are now in that rejection too.
- **Malformed input is refused before it is stored, and logged.** `members: 5`,
  `issues=5`, `escalation_rulings: ["abc"]` and `members: "uid"` — a string
  silently split into one-character uids — now each get a typed refusal with a
  row in `editor-log.jsonl`. A null name no longer becomes an issue called
  `"None"` and a null `rebuttal_quote` is no longer stored as the string
  `"None"`: null is absent.
- **`schema_version` is checked, not just written.** `read_run`, the prior-round
  reader and the renderer each refuse an unknown version by name. A run folder
  from an engine that wrote its review as `editor-package.json` is refused by
  `peerreview report` with that reason and its `REVIEW.md` is left untouched —
  it used to be rewritten to "No review was produced".
- **`REVIEW.md` prints what the document holds**: the rebuttal quote behind a
  computed finding ruled minor, the prior round's provenance, statuses and
  tally, a closed issue's own statement and ruling, and the issue each
  escalation ruling refers to.
- **Every issue row carries every field, and so does every member.** A row's
  seventeen fields are `peerreviewagents.issues.ISSUE_FIELDS` and a member's
  eleven are `MEMBER_FIELDS`; what does not apply to a row is `null`, or an
  empty list where the field is a list — live or closed, edited or synthesised,
  in `issues.json` and out of `read_run`. Fields used to be left out where they
  did not apply, so a consumer had to test for a key before reading it. It now
  reads the field.
- **`stages['editor_trace']` is present for every run of an arm with an
  editor**, as a row of zeros where the editor made no submission call at all,
  rather than absent — the one case the trace was built for was the one case it
  was missing from. `write_review` reports the trace in that rejection on the
  call count rather than on the trace's presence.
- **`docs/INTEGRATIONS.md` is audited field by field against the code.** Every
  top-level key of `issues.json` is documented, with the five statuses and what
  each means for `rank` and `members`, the `prior_round` shape, `audits` as bare
  file names, the `members[].finding_uid` join to `read_run()['findings']` for
  the fields the ledger alone carries, what a reading is when the ledger cannot
  be read, and the `ISSUE_*` names on the supported import surface. The MCP tool
  count the doc states — 23 in one sentence and 27 in another, against a server
  of 28 — is now checked against `server.TOOLS` by a test, so it cannot drift
  again.
- **The reopen destroyed an accepted review on calls that stored nothing.**
  `submit_issues` dropped the editor's accepted package and deleted the
  `issues.json` it had written *before* validating the call, so `issues=5`
  answered "nothing was stored" over a review it had just taken apart. The
  reopen is now part of the batch landing, after validation. It is also refused
  outright once the editor has filed its `submit_report` and once the run's
  files are written: a call arriving after `write_review` used to leave a
  complete run whose `issues.json` had been removed under it.
- **An id was minted twice.** A resolved issue is dropped from the next round's
  carry, so the carry alone did not say what `i03` meant two rounds ago and the
  id was handed to a different problem. `issues.json` now publishes
  `issue_ids_used` — every id this run and every round before it has spent — the
  prior-round reader reads it back, and new ids are minted around the whole set.
  One id is one problem for the life of the manuscript.
- **A published file named the machine it ran on.** `stages['editor_trace']`
  carried the absolute path of `editor-log.jsonl` inside `issues.json`, and
  `REVIEW.md`'s header printed the run folder's absolute path. Both are now
  relative to the run folder, and the test scans the whole of `issues.json` and
  `REVIEW.md` for the machine's paths rather than checking a field at a time.
  `manifest.json` keeps the manuscript's absolute path by design — it is the
  run's own record, the pdf sits outside the run folder, and it is not in the
  published set; `docs/INTEGRATIONS.md` says so.
- **`rebuttal_quote` was accepted where there was nothing to rebut.** The field
  answers one thing — a checked receipt being called a suggestion — and it was
  stored on a `major` issue with an invented `rebuttal_block_id` and printed in
  `REVIEW.md` as a passage of the manuscript. It is now refused anywhere but the
  `minor`-over-`computed` case it exists for.
- **An `unverified` row kept nothing for the next round to test.** Ruled
  `unverified` in one round and `resolved` in the next, an issue skipped the
  surviving-quote check entirely: the closed row carried no members, so there
  was nothing to look for. The row now carries its `surviving_quotes`, the check
  reads them, and the rank it last held is in `prior_rank` whether it was closed
  once or twice.
- **`retire: ["C", "C"]` raised a `KeyError` after retiring `C`.** The first
  retirement had already happened and nothing was logged. The same name twice
  now means what it means once.
- **Wrong types never reached the server's own validation.** FastMCP's pydantic
  layer rejected `issues=5`, `retire="a name"`, `summary=5` and
  `limitations=[5, None]` before the function ran, so none of them reached
  `editor-log.jsonl` and a run whose editor died on one reported that no
  submission call had been made. The tool signatures take them, every type is
  checked in the function where the refusal is recorded, and the tests go
  through `create_server().call_tool` as a client does. `significance=None`
  was also stored as a verdict axis of `None` — `str(None)` is not empty — and
  is now refused as missing.
- **The degrade path named the wrong cause.** Four runs end with
  `issues_source: "server"` and all four printed "this arm runs no editor":
  a ledger past the 200 findings the editor path supports, an editor slot
  nobody claimed and an editor that closed nothing read as an arm without an
  editor. The true reason is written to the document as `issues_source_reason`,
  printed in `REVIEW.md`, repeated in each synthesised issue's `status_reason`
  and carried in `not_publishable_reasons`. Where findings pass the limit after
  the editor claimed the slot, its accepted review stands — `submit_editor`
  refuses while any finding has no issue, so it covers them all — with the
  ledger size stated as a limitation instead.
- **A prior-round file could crash the ingest or carry a duplicate id.** A row
  that is not an object raised an `AttributeError` naming neither the file nor
  the row, and two rows under one id matched two issues of this round to the
  same prior issue. Both are refused by name at ingest.
- **Small ones.** `uv.lock` pinned `peerreviewagents` 0.9.1 while pyproject said
  0.10.0, and a test now checks the two agree; the editor's agent file asked for
  "two sentences at least" where the server counts 40 characters, and the number
  in the file is now checked against the server's own refusal; a docstring in
  `runs.py` pointed back to "before 0.9.2", a release that never happened, and a
  test now requires every version the prose points back to be one `CHANGELOG.md`
  lists; and `editor_trace['log']`, `issue_ids_used` and `issues_source_reason`
  are documented in `docs/INTEGRATIONS.md`, with a test that every published
  top-level key of `issues.json` is named there.
- **A written review could be rewritten.** `submit_issues` had the two guards
  and `submit_editor` had neither, so after `write_review` had published a run
  the editor's own token could submit a different summary and a different
  verdict, file a second report, and a second `write_review` republished
  `issues.json` and `REVIEW.md` under it. `submit_editor`, `submit_report`,
  `write_review`, `claim_agent` and `reissue_claim_code` all refuse once a run
  is complete, and say so with its status; a re-claim no longer moves the
  editor trace of a published review. A further round of review is a new run,
  not a re-claim of a finished one.
- **The editor could close its stage before its review was accepted.** A
  `submit_report` filed first was accepted, the stage read as done over a review
  nobody had submitted, and every `submit_issues` afterwards was refused because
  the report was in — the issues in the editor's context could not be stored by
  any call. The closure is refused until `submit_editor` has accepted a review
  and names what is still open.
- **A degraded second round lost the prior round.** The no-editor path numbered
  from `i01` regardless of the ids the earlier rounds had spent, so a round that
  degraded after it opened handed `i01` to a new finding while `i01` was still
  the id of a prior issue — and wrote no row at all for the prior issues, which
  appeared in the review as neither resolved nor open. It now mints above every
  spent id and carries each prior issue as `unverified`, with a `status_reason`
  saying no editor ruled on it and why there was none.
- **A prior-round file was read on trust past the first two checks.**
  `issue_ids_used` was iterated as given — `5` raised a bare `'int' object is
  not iterable`, a string was iterated into one-character ids, a dict
  contributed its keys — and the field is now required and validated as a list
  of this engine's own ids. The duplicate-id check ran over the carried rows
  alone, so a live row and a resolved row under one id passed; ids were accepted
  as numbers (`7` and `"7"` both passed); and a `members` or `surviving_quotes`
  that was not a list was accepted here and raised a `TypeError` out of every
  `submit_editor` call afterwards. Every row is validated at ingest and the file
  is refused by name.
- **A refused closure was invisible to the editor's own trace.** Wrong-typed
  `submit_report` arguments were logged to `closures.jsonl` and nowhere else,
  and `none_found: "abc"` was refused by the schema layer before the function
  ran, logging nothing at all — so a run whose editor died closing its stage
  reported that no submission call had reached the server. `none_found` is
  checked in the function like every other argument, and every editor refusal,
  closures included, is in `editor-log.jsonl`.
- **`REVIEW.md` printed a closed issue's surviving quote unlabelled.** A bare
  blockquote under "Why it is closed" left the authors unable to tell whether
  they were being quoted their own revision or the prior round's complaint; the
  quotes are now labelled as the prior round's evidence still in the manuscript,
  and an `unverified` row says that no ruling was made and the passage standing
  is all the round can say. The header's `Run folder | review` — the name of the
  directory the renderer is handed, for every run ever written — now names the
  run folder.
- **A published path carried the separator of the machine that wrote it.** The
  editor trace in `issues.json` named `review\editor-log.jsonl` on Windows and
  `review/editor-log.jsonl` everywhere else, so nothing matching the one found
  the other; the manuscript manifest keyed its files the same way, which the
  scaffold inventory check — built with `as_posix` — read as a changed
  manuscript and refused every run over a scaffold source on Windows. One
  `storage.relative_name` makes every run-relative name POSIX on every OS, and
  the refusal that names the editor log no longer prints an absolute path.
- **A finished run folder was reopened and undone by the next ingest.** A new
  server process over a complete out folder — a restarted plugin, a second
  worktree, a workflow run again over the same path — was accepted, rewrote
  `progress.json` to `running` and took the folder over, so a review that had
  been written and handed on read back as neither complete nor publishable
  while `REVIEW.md` and `issues.json` sat beside it saying otherwise, and the
  only exit was `mark_blocked`. `ingest` into a complete folder is now refused
  by name, before anything is written, and the refusal says how a further round
  runs: a fresh out folder with `prior_issues` pointing at this one. The session
  that wrote the review is refused in the same words, so a second paper passed to
  a live server fails loudly rather than attaching to a finished folder. Ingest
  into a `blocked` folder stays allowed — that is the documented recovery.
- **Refused calls still wrote into a complete folder.** Every completion guard
  refused the call and then recorded the refusal, so `closures.jsonl` and
  `editor-log.jsonl` grew after the review was handed on; and the reviewer read
  tools and `save_note` were not guarded at all, so a reviewer still holding a
  token went on appending to `tool-log.jsonl` and `notes.jsonl` — rows no
  document in the folder counts, and reads that spent allowance in a run with
  nothing left to spend it on. `claim_citation_batch` and
  `submit_citation_verdict` are guarded too: a worker could still fill the
  ledger the published citation counts were taken from. `resolve_citations` is
  guarded too, and `write_review` refuses while a lookup pass is still open: the
  review is written from the citation table, so it is not written while the table
  is being filled, and a pass that was in flight cannot land rows under a review
  that no longer counts them. A complete run folder does not change at all now:
  the test digests every file in it, calls every one of the registered tools in
  turn, and digests again, so a newly registered tool fails the test until it is
  covered.
- **The Record stage had one attempt at the one call that writes.** A
  `write_review` that landed and an answer that was lost left the workflow with
  nothing: it returned success with the counts undefined, or called `block()`
  and threw "the run files were never written" over a folder holding a finished
  review. The refusal over an already-complete run now carries the counts of the
  review on disk — read from `issues.json`, written by nothing — and the stage
  asks the folder what it holds (`run_status`) before it blocks, treating a
  complete folder as the success it is. `run_status` carries the published
  document's counts — `issues` and `escalated` read back from `issues.json` —
  once a review is written, on every arm, because `issues` was the editor's
  working tally and was `null` on the arms that run no editor; and the stage no
  longer reports 0 for a count it could not read. A count nobody read is unknown
  and is said to be unknown, in `counts_unknown`, rather than printed as a review
  with no issues.
- **A published document named the machine the review ran on.** `REVIEW.md` and
  `REVIEW.txt` printed the ledger's absolute path in the Citations block, so the
  two documents the authors are sent carried the reviewer's home directory on
  every arm that keeps a ledger. The row is the run-relative name now, through
  the same `storage.relative_name`, and the no-machine-path scan runs over a full
  two-track run as well as a reviewers-only one.

## [0.9.1] — 2026-11-14

The five faults the SPOT re-run on 0.9.0 found, fixed, plus one the fixes
uncovered — pull requests #47 through #52. See
[`docs/SPOT_0.9.0.md`](docs/SPOT_0.9.0.md) for the run they come from; that
document is the record of the 0.9.0 run and is left as it was.

**Every number in this release is an offline replay, not a new review.** Each
one was measured by running the fixed code over the saved manuscripts and
citation ledgers of the seven SPOT 0.9.0 runs — the same 325 reference rows and
434 in-text uses — with each lookup answered live once and then from that cache.
**No model was run, no paper was re-reviewed and no verdict was re-judged.** The
numbers say what this release's reference stage gets from those PDFs, which is
not the same claim as what a 0.9.1 review will report.

### Changed

- **The pinned reader is `rustypaper==0.2.4`**, up from 0.2.3. Two of the faults
  below are the reader's and are now fixed at the source as well as guarded
  here: across the fourteen-paper pilot 0.2.4 takes reference entries whose
  `year` field disagrees with the year printed in the entry from 35 to 0, broken
  DOIs from 4 to 1, and running-header text inside entries from 2 to 0. The
  guards in the citation ledger stay — they are about what the table may assert
  against a paper on a reading it is not sure of, whatever reader produced it —
  but with this pin they should seldom have anything to do. The pin is exact
  because every run manifest records the converter version and compares it when
  resuming.

### Fixed

- **The harness stopped a healthy review ten minutes after the session's last
  turn.** Under `-p` the review is the session's background task, and the
  harness gives background tasks a fixed wait once the turn is over —
  `CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS`, ten minutes unless something sets it.
  The session's last turn comes early, while the review is still working, so
  that default was a second deadline nobody chose: the SPOT re-run lost a run
  at 10.3 minutes with its reference stage mid-flight, and the pilot's seven
  runs all finished between 6.9 and 10.3 minutes against a wall they could not
  see. The starter now sets that wait itself, from this run's own deadline
  (`--timeout`, 5400s by default) plus a two-minute margin, so the driver stays
  the only thing that ends a run and records why. A value inherited from the
  shell is overridden, because it was set for some other session and a small
  one is the fault itself; `0`, which the harness reads as waiting
  indefinitely, and any value more patient than this run's wall clock are kept.
- **A run was blocked over a stage whose findings were already in the review.**
  dark-kinome ended `blocked: Stages that never reported: core-1` while
  `findings.jsonl` held core-1's five findings, the editor had ruled all five
  into the review, and the slot had spent 72,673 tokens. Three things were
  wrong and all three are fixed.
  - The workflow decided a stage had closed from the stage's own structured
    `closed: true`, never from the server's ledger — so a stage that claimed
    closure without filing a report looked done, and `attempts: 1` is the
    record of a retry that never fired. The claims are now checked against
    `run_status`'s ledger once, at the review barrier, and a slot the ledger
    does not hold is reopened and closed properly before the editor runs.
  - That retry could not have worked anyway: it asks the recorder for a fresh
    claim code through `reissue_claim_code`, and `recorder.md` never listed
    that tool, so the recorder had no way to call it. The tool is in the
    recorder's list, its brief says when to use it, and a test now checks every
    tool the workflow asks an agent for against that agent's own tool list.
  - `write_review` refused over any required slot absent from the ledger, which
    threw out a finished review in order to report a missing summary. The
    ledger now splits `missing` in two: `silent` (stored nothing, filed
    nothing — nobody reviewed that share of the manuscript, so the run is still
    blocked) and `missing_with_findings` (findings delivered and ruled on, no
    report filed). The second writes the review as `complete` and names the
    gap — the stage's summary and limitations — in `read_run`'s `note` and in
    `REVIEW.md`, instead of losing the whole review to it.
- **A refused closure left nothing on disk.** dark-kinome could be read no
  further than "core-1 has no report": `tool-log.jsonl` records the
  investigation tools only, so whether that slot never called `submit_report`
  or called it and was refused — over a spent token, or over a `none_found`
  that contradicted its own findings — was not in the run folder in any form,
  and that part of the cause is still not recoverable from the 0.9.0 runs.
  Every `submit_report` call, taken or refused, is now a row in
  `<run>/review/closures.jsonl` with the slot, the attempt and the refusal
  text.
- **Eight real papers were reported as references that do not exist.** All
  eight of the 0.9.0 corpus's `exists: no` rows are one accident, and it is the
  reader's: the PDF text layer prints the DOI with a space inside it
  (`https://doi.org/10.4049/jimmunol. 1203102`), the reference's own `doi`
  field kept only the half before the break, and because those references print
  no title to fall back on, a 404 from the DOI handle API was the only evidence
  the row had. The papers are real and their DOIs are on their pages.
  - `no` now means *checked and not found* and requires every leg to have
    answered: a DOI the reference list gives as its own field, no handle at
    doi.org, no registration agency, and both title searches answered and
    empty. A handle 404 on its own, with nothing to search for, is no longer
    enough.
  - The resolver repairs the obvious damage before deciding anything. Using the
    row's own raw reference text it rejoins the printed DOI across the space
    the reader lost (up to three following tokens, stopping at the first word
    that is prose rather than identifier, so `Publisher's Note` is never
    absorbed) and looks each candidate up at Crossref. A record is taken only
    where the printed first author agrees with it: joining across a space can
    land on somebody else's real paper, and a wrong record here becomes the
    source text every verdict about that reference is judged from. A taken
    record resolves the row as `doi-repaired` and raises no DOI mismatch
    against the paper; a refused one is kept on the row as `repairs_refused`.
    A row with any candidate at all can never be `no`, and its note names the
    DOI the page prints so a human can check the PDF. Offline against the
    0.9.0 runs, this recovers the printed DOI for 8 of the 8 rows, and under a
    replay where every lookup answers 404 none of the eight can reach `no`.
  - `could not check` was one sentence for two different facts, and 55 rows of
    the corpus got the wrong one: "the lookups did not answer" was printed
    about rows where no lookup had been made, because this version read no DOI
    and no searchable title out of them. The row now carries `looked_up`, the
    counts split `no_lookup` out of `could_not_check`, and the report says
    which of the two it is — the reference parser falling short is not Crossref
    and Europe PMC failing.
- **55 references were never looked up because the parser could not find their
  titles.** The reference list gave no title field for them and this version
  read titles out of the reference text in one style only, so a journal that
  prints `Authors: Title. Venue year;vol(issue):pages` — 29 of control-
  education's references — left the row with nothing to search for and an
  honest but useless `could not check`. The parser now reads the title off the
  reference text itself: the author block ends at the first run that stops
  looking like names, the title is the sentence that follows, and the venue is
  what follows that with the volume, issue, pages, month and the `In:` and
  `Available from:` leads stripped. It keeps the title whole where the
  reference's own punctuation would cut it — a colon inside a title, a question
  mark mid-title, a word ending in an abbreviation — and refuses the parse
  outright where the boundary lands inside the author list, because a wrong
  title searched is worse than no title. Offline over the 325 reference rows of
  the seven 0.9.0 runs, titles go from 252 to 322: 70 rows recover one, all 70
  searchable, and 52 of the 55 rows that had neither title nor DOI can now be
  asked about. The 3 that remain are institutional and legal references with no
  title to read — an FAO report, an EU regulation, a chapter cited by book.
  - A title read off the reference text is never reported as the paper printing
    a different title from the record's. It is this version's guess at where
    the title ends, and a finding about the authors cannot rest on a guess;
    `printed['extracted']['title']` says which rows those are.
  - A rejoined DOI that Crossref holds no record for is now asked of its
    registration agency and of the DOI resolver's handle API before the row is
    given up. A DOI registered with an agency neither index covers — 0.9.0's
    `10.3969/j.issn.1672-7347.2010.02.002` is one — resolves as `yes`,
    registered, with no record and a note that the printed authors could not be
    corroborated because no index this version reads holds its metadata. The
    corroboration rule for *taking* a record is unchanged.
- **269 of 434 citation uses were `cannot tell` because no source text was ever
  fetched.** 0.9.0 asked Europe PMC for an abstract and nothing else, so 153 of
  325 references carried an abstract, **none** carried full text, and the
  largest single category of verdict was "no evidence to judge against" — a
  review that reads as if it checked a reference list while two thirds of it
  was never read. The resolver now tries four sources in order and stops at the
  first article body it gets: Europe PMC's `fullTextXML` where the record is
  open access, PubMed Central for a PMCID Europe PMC named, arXiv by DOI and
  then by printed title, and the abstract from whichever of them answered.
  - **Full text means the article's `<body>`.** Both archive legs keep the
    `<body>` of the JATS answer and drop the front matter, because an archive
    that holds only metadata still answers `fullTextXML` with a document, and
    storing that would record "rests on full text" about a reference whose text
    nobody has. It also keeps the title, authors and affiliations out of the
    stored text, where they were scoring as the passage a citing sentence
    pointed at.
  - **No publisher pages and no PDFs.** An open-access link on a Crossref
    record usually leads to an HTML landing page or a PDF behind a paywall, and
    a link followed is not a text extracted, so such a reference is counted
    honestly as having no full text.
  - **The worker is shown passages, not the article.** A body can run past a
    megabyte, far more than a verdict agent should read, so the judge is given
    the abstract plus up to three 1,200-character windows of the body chosen by
    the content words of the citing sentence itself — snapped to sentence
    boundaries, joined by a visible gap so a quote cannot be reassembled across
    one, and falling back to the body's opening only when the sentence shares
    no content word with it. The use row records the windows' offsets and a
    `passage_rule` naming which of the two rules chose them, so "rests on full
    text" can be checked against the passage it rested on.
  - Each reference gets at most four source fetches and 2 MB, both recorded on
    the row, and every request is a public identifier or a printed title sent
    to a free keyless API under a generic client name. The ledger counts
    `with_abstract` and `with_full_text` separately, because one number for
    both reads as if every reference had been read.
  - Replayed live over the same 325 reference rows, with no model and no run
    re-done: abstracts go from 153 to 201, open-access article bodies from
    **0 to 75**, and references with some source text from 153 to 203. Of the
    269 uses that could not be judged, 87 now have evidence that did not exist
    — 62 that had nothing now have an abstract or a body, and 25 more that had
    only an abstract now have the body as well — while 182 have nothing new:
    151 of those have no source text at all and 31 have the abstract they
    already had. Engineering reference lists stay the hard case:
    beamforming's 25 IEEE references yield one abstract and no body.
- **A year the reference reader got wrong was reported as a year the paper
  printed wrong.** The ingest's `year` field is not always the year the page
  prints: a reader that cut `240:151882` out of an entry printing `(2022)`
  filed 1882, and one that swept a page footer into the entry filed the
  footer's 2024 over a reference printing `Science. 201 7;356` — and both rows
  came out of the table as the paper printing a year its own indexed record
  disagrees with. The ledger now reads the year the reference text itself
  prints, in the two shapes that say plainly that the number is a year — in
  brackets, and in front of the volume's punctuation, a space inside it or not
  — with URLs, DOIs and article numbers taken out first and the leftmost year
  winning, so a footer behind the entry cannot outrank it. Where that year and
  the field disagree, the page's year is the one compared, the field's is kept
  on the row as `reader_year`, and a row flagged for any other reason says
  which of the two it used. The one-year tolerance is unchanged: a record year
  *earlier* than the printed one is what a work cited by its issue year and
  indexed by its online-first year looks like, so a mismatch that stands now
  names that as the cause to rule out. Offline over the same 325 reference
  rows, year mismatches go from 5 to 3: cgrp r16 (1882 against a record of
  2021) and dark-kinome r27 (2024 against 2017) were the reader and are gone,
  while photocatalytic r40 and r54 — whose reference text prints a DOI
  belonging to another paper — and oxytetracycline r72, printed 2018 against a
  record of 2015, stay findings.

## [0.9.0] — 2026-11-14

The review is a Claude Code plugin. The Python orchestration that ran one
through 0.8.x is deleted, not deprecated: 90 files and about 14,800 lines went,
including 32 test modules, and nothing was left behind as a shim.

### Added

- A citation ledger: the `citations` track no longer samples the reference
  list, it checks all of it from a table the server owns.
  `<run>/review/citations/ledger.jsonl` is append-only, one row per reference
  and one per in-text use, last record per `row_id` winning, so a run that
  stopped halfway resumes from it instead of being redone. Four stages fill it,
  and the first two spend no model tokens at all. **Fill** happens inside
  `ingest`: a row for every extracted reference, with or without a DOI, and a
  row for every sentence that cites one. **Resolve** is the server's own
  lookups, driven by the recorder through `resolve_citations` and bounded by
  `limit`: a DOI goes to Crossref, a reference without one is searched by
  bibliographic title against Crossref and then Europe PMC, and each row
  records `exists` in four states, not three: `yes`, `no` only when the DOI
  resolver itself has no handle for a DOI the reference list gives as its own
  field — doi.org's handle API answering `responseCode` 100 with the 404 it
  uses for a handle it does not hold, the one service whose whole job is to say
  whether a DOI was ever registered — **`not located`** for a title two indexes
  answered about and neither listed, and **`could not check`** for a lookup that
  did not answer. A metadata index that does not hold the record is not that
  claim: Crossref and the registration-agency route both answering 404 leaves
  the row `could not check`, because a DataCite or JaLC DOI is absent from
  Crossref while being perfectly real. A lookup that failed is never read as a
  reference that does not exist, and a work outside Crossref and Europe PMC — a
  book, a thesis, a standard, a preprint — is `not located` rather than accused
  of anything. A reference whose lookups did not answer is owed its retries
  rather than written off: it stays counted in `resolve_citations`'s
  `remaining`, and `retry_after` says how many seconds until the earliest owed
  row is due, because the attempts are five minutes apart and a host that just
  rate-limited the run has not recovered in the same turn. A gap that ends
  inside the run's own deadline — which the driver now hands the session — is
  waited out by `resolve_citations` itself rather than handed back, on a worker
  thread so every other tool call in the session is still answered and the
  run's progress file keeps moving while it waits, interruptibly, and for no
  more than fifteen minutes in any one call; a gap that does not fit comes
  straight back, and the stage record says which rows were left and why rather
  than letting the lane report them as asked and failed. One resolve call runs
  at a time: a second one made while the first is still looking rows up or
  waiting does nothing and says so, because two passes over the same owed rows
  would fetch each row twice and spend two of its three attempts on one
  answer. Each row also
  carries a per-field comparison of title, first author, year and venue,
  any retraction or correction notice — matched on the record's own type and
  update-pointing fields as well as on a leading "Retraction of", "Correction
  to" or "Comment on" label, so a notice is never mistaken for the work it is
  about — and the abstract or open-access full text beside the URL it came from.
  A title search is only read as the cited work when the first author's surname
  agrees, because the query is already filtered to the printed year ±1 and a
  matching year corroborates nothing. Every response
  is cached under `citations/lookups/` with a receipt, and the requests carry a
  generic `User-Agent` with no address and nothing identifying whoever ran the
  review. **Judge** is `--citation-workers` Haiku workers, `cite-1`, `cite-2`,
  …, each claiming a batch with `claim_citation_batch` and writing one verdict
  per use with `submit_citation_verdict` — supports, partly supports, does not
  support, cannot tell — quoting the exact source passage it relied on, where
  `cannot tell` is the only verdict allowed no passage. **Confirm** is the
  citations specialist, which now reads the whole table with `citation_rows`,
  re-checks every problem row itself, and keeps the one-query
  missing-literature search and the cutoff rule it already had. It reads the
  clean half of the table as well: `citation_rows(kind='supported')` returns
  every use a worker read as *supports* with the passage it quoted, so a worker
  that answered supports for everything is spot-checked rather than taken at
  its word, and the specialist's report says how many of those rows it checked
  out of how many there were. Every citation read — a worker's batch and each
  of the specialist's four kinds — carries an `untrusted_text` line saying that
  the citing sentence and the fetched source text are quoted material to judge
  and not instructions: a sentence in either that tells a model what to answer
  is part of what is being checked, and both briefs say so where the model
  reads them. A worker's
  verdict is a row and never a finding: a `cite-N` slot cannot call
  `submit_finding` at all, and only what the specialist confirmed is stored.
  The workers are planned, claimable and modelled like every other slot and
  required like none of them, so one that never reports leaves rows unjudged
  instead of holding `write_review` open. `--citation-workers N` is 0 to 12,
  default 4, `--citation-worker-model` defaults to `claude-haiku-5-5`, both are
  `ingest` arguments too, and `PEERREVIEW_CITATION_WORKER_MODEL` does the same
  in a session the starter launched. The ledger exists only where the citations
  specialist does, so an arm without that track writes no `citations/` folder
  and reports no counts — absent is not zero, and two arms are never compared
  on numbers one of them cannot have. An arm that *planned* a citations lane
  and left no table behind is the other case, and it now says so: `enabled` is
  true, the counts are empty and `problems` carries one line reading that this
  run planned a citations lane and wrote no citation ledger, so not one
  reference or in-text use was checked. A lane that failed before its first row
  no longer reads like an arm that never had one.
- Both citation stages are sized from the paper rather than from a constant.
  `ingest` reports `citations.references`, `citations.uses` and
  `citation_worker_allowance` — the server's own per-worker call budget — and
  the workflow plans the lookup calls from the reference count instead of a
  fixed twelve: a 300-reference list gets the calls it needs, and the
  recorder's agent file allows every call the prompt asks for rather than
  cutting the stage off part-way through the list. Where the lane still cannot
  cover the paper, the workflow says which part before it starts and the ledger
  reports it afterwards: references nobody reached are reported as never looked
  up and uses nobody could reach as never judged, with the number of workers
  that would cover them. **No silent cap.** Judging is the stage that does not
  scale: a worker's call budget and the batch size put the default four workers
  at about 144 in-text uses and the maximum twelve at about 430, so a paper
  citing more than that leaves uses unjudged however the run is configured.
  That shortfall is reported rather than hidden — the counts say how many uses
  nobody judged and how many workers would have covered them — and resolve, the
  stage that decides whether a reference exists at all, covers the whole list
  regardless.
- `read_run()['citations']` and the review's own **Citations** section report
  what the ledger says: `enabled`, the absolute `ledger` path, `counts` (one
  per state of a reference and per verdict, plus the lookups spent and what
  each stage did), `complete`, a one-sentence `summary`, and one `problems`
  line per stage that did not finish. `manifest.json` names the ledger among
  the run's files. Those lines go in `problems` and never in
  `not_publishable_reasons`: **an unfinished ledger is reported, not published
  over.** A finding the specialist confirmed stands whether or not the rest of
  the table was finished, and the counts say in numbers how much of the
  reference list this run actually checked. `complete` is about what was
  checked and not about which stages ran: a table whose every reference came
  back `could not check` is not complete, however tidily the stages finished,
  and a ledger file that cannot be read at all is reported as unreadable rather
  than read as a clean reference list.
- A different model per core reviewer. Stage A ran three core reviewers on one
  model and they caught no more than one of them did — three readings of the
  same manuscript by the same reader — so the reviewers are now drawn in order
  from a list, reviewer *i* running `list[(i - 1) % len(list)]`. The default
  list is `claude-sonnet-5-5, claude-opus-5-5, claude-haiku-5-5`: one reviewer
  is Sonnet, two are Sonnet and Opus, three are all three, and a fourth starts
  the list again. `revision-1` is `core-1` revising its own findings, so it
  takes `core-1`'s model. The specialists stay on `claude-sonnet-5-5`, the
  editor is `claude-fable-5-2` and the recorder `claude-haiku-5-5`. Every one
  of them is configurable — `--reviewer-models A,B,C`, `--specialist-model`,
  `--editor-model` and `--recorder-model` on `peerreview review`, the same four
  as arguments to `ingest` for a session that drives the workflow itself, and
  `PEERREVIEW_REVIEWER_MODELS`, `PEERREVIEW_SPECIALIST_MODEL`,
  `PEERREVIEW_EDITOR_MODEL`, `PEERREVIEW_RECORDER_MODEL` in a session the
  starter launched. There is no billing or usage gate anywhere in it: which
  models a login may run is the user's business.
  `peerreviewagents.plugin.models` is the only place that decides, and
  `scripts/stage_a/matrix.py --reviewer-models sonnet` pins every reviewer to
  one model so a rerun stays comparable with the recorded results.
- A model this login cannot run is refused at startup, in one line naming the
  slot, the model and the flag that changes it, in both places a run can begin:
  `peerreview review` before the probe spends anything, and `ingest` before the
  manuscript is imported. Claude Code's own answer to a model it may not run is
  to substitute one — a blocked family alias runs the newest permitted version
  of that family — so a run that asked for Opus and got Sonnet would be
  recorded as an arm it was not. Availability is the `availableModels` allowlist
  in the settings files Claude Code reads, concatenated across them the way
  Claude Code concatenates it rather than the highest-precedence file replacing
  the others — reading only the first file would refuse a model that is in fact
  available, which is a refusal with nothing wrong behind it. Where no allowlist
  is set anywhere, the models this version knows are available and any other
  name is refused as a name this version does not know. A list with an empty
  item in it — `sonnet,,opus`, a stray comma — is refused rather than quietly
  dropped, because a three-reviewer run drawing from a two-model list is a
  different arm from the one that was typed. A folder already holding a run
  under different models is refused too, the way a folder holding a different
  arm already was, and that check covers every slot rather than the reviewers
  alone.
- `CLAUDE_CODE_SUBAGENT_MODEL` stops a run before it starts on Claude Code
  older than 2.1.251, where that variable outranked both the per-spawn model
  override the workflow sends and the agent files: every slot would have run
  the one model it names while `protocol.json` recorded the per-slot plan. The
  same refusal covers a `claude --version` that cannot be read, because "new
  enough" is not something an unreadable version says. The variable is refused,
  never unset — it is the user's, and quietly removing it would override a
  deliberate choice.
- The model each slot was asked to run, written into the run folder, so a
  published review can say which model wrote which report: `models` and
  `reviewer_models` in `protocol.json` and in `plan` in both `protocol.json`
  and `manifest.json`, `assigned_model` beside the self-reported `model` on
  every row of `protocol.json`'s `agents` and of `editor-package.json`'s
  `reviewer_provenance`, `identity['models']`, `identity['reviewer_models']`
  and `agents[]['assigned_model']` from `read_run`, and `models_requested`
  beside the observed `models` in the `review` summary. The two are different
  facts and both are kept: what the run asked for, and what the agent reported
  having run as. Neither is ever shown as the other — a slot nothing observed
  reads as `None` from `read_run` and `not recorded` in the report, and never
  as the model it was asked to run. They are empty for a run written before
  0.9.0, which pinned one model per slot in the plugin agent files.
- A slot that ran a different model *family* from the one it was assigned is a
  problem and a reason the run is not publishable. `read_run` names the slot,
  the assigned model and the observed one in `problems` and again in
  `not_publishable_reasons`: that run did not run the models it is filed under,
  and no later reading of the folder repairs it. A different *version* of the
  same family is neither — which version a family alias resolves to is the
  login's business, and the observed id is simply what gets reported.
- `identity['models_unverified']` from `read_run`, and a **Models unverified**
  line in `REVIEW.md` and `REVIEW.txt`: the slots that were assigned a model
  and never reported running one. The review is still publishable, because
  nothing was seen to disagree with the plan, but for those slots the plan is
  all there is and a page showing the assignment alone would read as agreement.
- `Model asked` and `Model ran` as two columns of the report's agent table,
  where there was one `Model` column, and `Models asked for` beside `Models
  that ran` in its header. The slots do not all run one model, so there is no
  single model for a run; the table is the only honest answer, and it is
  allowed past the report's prose width rather than shortening a model id.
- Stage A puts the pin in the job id for every stage, not only `rerun`: a
  `--reviewer-models` value becomes a suffix such as `--sonnet-opus`, and an
  explicit `--tag` wins over it. A pinned `main` run otherwise lands in the
  recorded run's folder, where `already_done` reads it as finished and the
  pinned arm never runs at all. With neither given the ids are byte-identical
  to the ones already in `results.jsonl` and on disk.
- `peerreviewagents/plugin/pra`, the plugin that now runs every review: a
  workflow that fans out N core reviewers and six specialist tracks and then an
  editor, agent files that pin a turn limit per slot and a fallback model the
  workflow's per-spawn override replaces, and three
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
- **An unregistered DOI is never by itself proof of anything.** A DOI is a
  claim about an identifier only when the reference list hands it over as its
  own field. A DOI read out of the reference *text* cannot make a row
  `exists: no` and cannot raise a `doi` mismatch, because a two-column PDF
  breaks a DOI across a line constantly and the fragment that comes out of
  that is indistinguishable from a DOI printed wrongly: such a row is `not
  located` with a note saying the DOI as read is unregistered, the extraction
  may be cut short, and the PDF is where to check. `doi_in` refuses a DOI whose
  trimmed form does not end on a letter or a digit — the four ways a line break
  lands, after a hyphen, a prefix slash, a dot or an underscore — while
  accepting the two shapes it used to throw away with them: a DOI at the end of
  a sentence (`10.1016/j.cell.2020.01.001.`) and the bracketed suffix every
  Lancet DOI has (`10.1016/S0140-6736(20)30183-5`). `no` on the resolver leg
  now also requires the 404 the handle API answers for a handle it does not
  hold, so a proxy returning 429 or 503 with `responseCode` 100 in the body is
  `could not check`. And `citations.md` says that an `exists: no` row is a lead
  the specialist confirms against the PDF text before it becomes a finding.
- **A printed acronym is not a different venue.** *PNAS*, *NEJM*, *JAMA*,
  *BMJ*, *JACS* and *NeurIPS* against the words they stand for each raised
  "printed venue differs from the indexed record", which is the citation style
  most journals ask for reported as an error. When either side is a single
  token it is read as an initialism of the other side's significant words; a
  parenthetical edition is dropped before the comparison, so `Science (80-. )`
  is *Science* and `Bioinformatics (Oxford, England)` is *Bioinformatics*; and
  a one-token name that is neither an initialism nor an abbreviation is **not
  compared** rather than called a mismatch. *Nature* is still not *Nature
  Methods*.
- Known and accepted in 0.9.0, documented rather than fixed: nothing in the
  workflow returns after the 300-second retry wait, so rows the hosts
  rate-limited stay `could not check` and are reported as such; a surname
  spelt two ways (*Müller* / *Mueller*) or hyphenated falls to `candidate`
  rather than agreeing; any Crossref `update-to` marks a record as a notice,
  including one that points at a dataset version; and a `<title> (Reply)`
  suffix in parentheses is not caught by the notice matching.
- `peerreviewagents.read_citation_ledger(path, source_text=False)` reads the
  citation table's own rows from a run folder: `references` and `uses`, the
  last record written for each `row_id`, in natural `row_id` order — a run of
  digits compared as the number it is, so `ref:r2` comes before `ref:r10` and
  `use:b2:2:r1` before `use:b2:10:r1`, which is the reference order and the
  sentence order a reader counts in — plus the same `counts`, `complete`,
  `summary` and `problems` `read_run(path)['citations']` holds, from the same
  functions and the same reading of what the arm planned. `read_run` reports the ledger in numbers and
  there was no public way to read the rows, so every consumer that wanted them
  imported `peerreviewagents.engine.citation_ledger` — a private module — and
  re-implemented the one rule the format has. It takes `read_run`'s guards: a
  symlinked `review/`, `citations/` or `ledger.jsonl` is refused by name rather
  than followed, and a damaged line is skipped, counted in
  `counts['unreadable_rows']` and named in `problems` rather than raised. A
  ledger file the process may not open at all comes back as empty `references`
  and `uses` with `exists` true and the same one `problems` line `read_run`
  gives it, rather than raising the `PermissionError` out of a consumer's page:
  the state where nothing in the table can be counted is the one such a page is
  most likely to meet. The
  fetched abstract and full text are left out by default — most of the bytes in
  the file, and someone else's prose — with `resolution['has_source_text']` in
  their place, and `source_text=True` returns the rows exactly as stored.
  `docs/INTEGRATIONS.md` documents it and the supported way to build a ledger
  fixture, which is to write the JSON lines yourself.

### Changed

- **The fill stage takes the reader's marker-to-reference links instead of
  working the matching out again.** `rustypaper` matches each in-text citation
  to the bibliography entry it cites and writes the marker as a link to that
  entry; the ledger now reads those links and records the reference each one
  names. It had been looking for bracketed markers in the flattened text, which
  is everything a journal that prints `blocked the receptor³` does not give:
  with the superscript gone the digits are ordinary text, and the matching has
  to happen where the superscript still exists. On the seven pilot PDFs, read
  by the `rustypaper` 0.2.3 this release pins, that took the use rows from 143
  to 434 and the matched ones from 38 to 434 — Nature-style papers that had
  none at all now have them, and every use those seven papers make now names
  the entry it cites — with the reference rows going 210 to 325 as the same
  reader release splits bibliographies it used to leave whole. The paper that
  moves furthest is the one the earlier readers reached least:
  `control-education-pmc7183109` yields 31 reference rows where it yielded 5,
  so its 47 uses all match instead of 5 of them. Across nine papers — the seven
  plus two the ranges below are thickest in — it is 467 reference rows, 566 uses
  and 566 matched, with no unmatched use left in any of them. There is one way
  to find a use and no flag for the old one. A citation range is the one place the links are not the whole citation:
  the reader links the two ends of `respiration³⁻⁵` and the dash between them
  is the paper's own, so the entries the range covers are read off the
  numbering and recorded with a `reader-link-range` derivation saying the row
  was inferred — capped at the same 60 entries the bracketed reading already
  allowed, with a backwards or wider span (`5–3`, `3–300`) left as the two ends
  the reader did link. On the seven pilots that is 18 more uses and on the nine
  36, all of them matched.
  What the reader does not link is still read here: a bracketed run it
  left whole because one part of it is not a known label — `[1, 42]` — is read
  as one matched use and one unmatched, and author–year citations are matched
  against the reference list as before, because resolving `Devlin et al., 2019`
  means matching names and the reader does not do it. A row's `derivation` says
  which of those found it (`reader-link`, `marker-numeric`, `marker-author-year`
  and the unmatched forms of each), the marker is quoted as the paper prints it
  — bare digits for a superscript citation, `[12]` inside a bracketed run — and
  the sentence the row carries is the printed sentence, with the link markup
  taken back off, so a specialist judging the claim reads what the page says.
  A superscript marker the reader could not link is the one citation this does
  not see, and that gap belongs to the reader, which still has the spans.
- The six specialists audit six disjoint dimensions, and the `consistency`
  track is now `numbers`. Each brief carries a **Not yours** table naming the
  other five lanes and what each of them owns, because two specialists filing
  the same defect spends two allowances and lands on the editor to merge.
  `numbers` owns every printed quantity and the arithmetic between them — the
  same value in two places, one n followed through, parts against the whole;
  `stats` owns the inference drawn from those numbers and drops the arithmetic
  item it shared with `numbers`, gaining what the result licenses the paper to
  say; `figures-tables` owns what a panel shows, how a table is built and the
  panel inventory, and drops the value-against-value comparisons; both the
  methods lane and the field lane now say where the line between them runs. A
  specialist that sees something outside its lane writes one line under
  **Seen, not mine** in its audit and files no finding — it is never told which
  other tracks are running, so that line is the right answer in every arm. The
  boundary between `numbers` and `figures-tables` is a plot, not a page:
  `spec-numbers` keeps `view_page`, because the text extraction garbles tables
  — escaped tabs, a two-column page linearised into interleaved rows — and a
  lane that could only read the extracted text would either miss a real cell
  disagreement or file one it could have cleared by looking. Its brief scopes
  the tool to confirming a printed table cell or a number in running text, and
  requires that check before any finding that involves a table cell; reading a
  value off a plot, axis, error bar or colour scale stays with
  `figures-tables`, and `view_supplement` is still not in the slot.
- The core reviewers are told what a review is **not** — a catalogue of surface
  defects — without being told that specialists exist. The blinding is the
  property `reviewers-only`, `single` and `manuscript-only` are measured
  against, so naming the auditors in the reviewer brief would change every one
  of those arms rather than only the reference arm.
- `--track consistency` is gone; it is `--track numbers`, the `draft-check`
  default is `stats`, `numbers`, `methods-reporting`, and a run writes
  `audit_numbers.md` where it wrote `audit_consistency.md`. Any saved command,
  prompt or adapter naming the old track is refused by the parser rather than
  silently dropped, and `docs/stage-a-results.md` keeps the name the recorded
  runs ran under.
- The server has 27 tools rather than 23: `resolve_citations` for the recorder,
  `citation_rows` for the citations specialist, and `claim_citation_batch` and
  `submit_citation_verdict` for a citation worker, each on the allow list of
  that slot alone. The per-run tool-call ceiling rises from 600 to 900 with
  them, because a 60-reference paper has roughly 90 in-text uses and one
  verdict is one call; a worker gets 40 calls and no external requests, since
  everything it may see is already in the row it claimed. No reviewer's own
  allowance changed.
- Every reviewer brief now says to copy the `quote` out of the block text
  rather than retype it, and names the two ways it goes wrong: numbers or terms
  reordered, and a clause dropped from the middle. A quote written from memory
  is quarantined even when the finding is right, so the finding is lost — the
  Stage A matrix lost three correct catches of planted defects that way. On a
  rerun of both affected papers the targeted failure stopped happening and one
  paper's withheld correct catches went to zero, with no catch lost;
  `docs/stage-a-results.md` has the before-and-after counts and the two
  remaining causes, which are the reader's and the guard's, not the briefs'.
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

- **Reading a run created a file in it.** The ledger's writers serialise on
  sentinel files beside the rows, and for part of this release's development
  `Ledger.load` took the rows sentinel too — so `read_run` and
  `read_citation_ledger` created `review/citations/rows.lock`, and changed the
  folder's mtime, in every run anybody read. On a run folder nobody may write
  to — an archive, a copy taken for a report, anything handed over read-only —
  the sentinel could not be created, and both readers reported the ledger as
  "could not be read (Permission denied)" with no rows at all, on a file whose
  every byte was readable. A read now creates nothing: `load` takes a shared
  lock on the rows file it already has open, which `append` locks exclusively
  inside its own sentinel lock, so a row landing mid-replay is still never a
  torn line and no reader needs a sentinel to get that. Where the lock cannot
  be granted at all, the rows are read unlocked rather than not read. Writers
  are unchanged and still create the folder and both sentinels on the first
  append.
- **A torn file beside the ledger raised out of `read_citation_ledger`.**
  Reaching the citation table means reading `protocol.json` for what the arm
  planned and `runtime.json` for which agents ran. `read_run` read both through
  its guarded reader; the public ledger reader read `runtime.json` through
  `agent_rows` with no guard and outside its own last guard, so a run killed
  partway through writing that file raised `ValueError` out of a function
  documented to raise two things, neither of them that. Both readers now open
  every file on that path the same guarded way — torn, not a JSON object,
  unopenable, or a directory in its place is read as absent — which also makes
  the `agents` the two readers see identical, and with it the decision about
  whether the lane was planned. Damage to `runtime.json` or `progress.json`
  leaves the citation rows and counts whole and is named in `read_run`'s own
  `problems`, beside the lost tokens, rather than in `citations['problems']`,
  which both readers return identically. An `attempt` that is a list, a word,
  `Infinity` or `NaN` — all of which the same file can carry while being sound
  JSON, and the last two of which Python's own `json` writes — is read as the
  first attempt instead of raising `TypeError`, `ValueError` or `OverflowError`
  out of the merge. Under the fields is a guard under the whole of that
  reading, so that no content of `runtime.json` can raise out of either reader:
  `read_run` gives no agent rows and one line in its own `problems`, in the
  family of the line for a file that could not be read at all and with the same
  effect on `publishable`, and `read_citation_ledger` decides whether the lane
  was planned from `protocol.json` alone.
- **A ledger row of the wrong shape raised out of both readers.** A line
  carrying a list under `row_id` raised `TypeError` on an unhashable key out of
  `read_run` and `read_citation_ledger` alike; one carrying a number there
  raised later, wherever the rows were sorted; one carrying a list under `kind`
  raised where a reader filtered on it; and one carrying a string under
  `resolution` raised `AttributeError` inside the counting, which is the
  reading `read_run` does on every page. None is among the raises a consumer is
  told to expect, and all are what a half-written line or a hand-edited file
  produces. A line is now a row only when `row_id` is a non-empty string,
  `kind` is `reference` or `use`, and `resolution` and `printed` are each
  absent, null or an object — the fields the module dereferences, and no more
  than those. Anything else is an unreadable row like a torn line: skipped,
  counted in `counts['unreadable_rows']` and named in `problems`. Nothing the
  server writes is affected. `reference_id` and `sentence_index` are guarded
  the same way where the server assembles a worker's batch, so one damaged row
  no longer takes the batch with it, and a run of digits in a `row_id` too long
  for `int` — Python's own 4,300-digit limit — sorts as text instead of raising.
- **A last guard under both readers.** Whatever a file does to the counting or
  to the building of the rows, including an exception no version of this
  library foresaw, `read_run` and `read_citation_ledger` now give the same
  "`review/citations/ledger.jsonl` is present and could not be read (…)"
  problem line with empty counts and no rows, rather than raising into the page
  that called them. `publishable` is unaffected, as it is for every other
  citation state: a citation check never withholds a review, and it must not
  take down the page printing one either.
- **The two readers disagreed about a run that planned the lane.**
  `read_citation_ledger` decided whether the arm planned citations from the
  protocol's `plan` alone, while `read_run` falls back to the citation slots
  when there is no plan. A `protocol.json` recording `spec-citations` and
  `cite-1` with no plan therefore got the planned-lane problem line from one
  reader and an empty `problems` from the other, against the reader's own
  promise that the two cannot disagree. Both now read it through one function.
- **An unreadable citation ledger blocked publishing.** `read_run` put that one
  citation failure in `problems` before `publishable` was decided, so a run
  whose `ledger.jsonl` was present and could not be opened came back
  `publishable: False` while every other unfinished-ledger state came back
  publishable. It is now a `problems` line like the rest and never a reason
  against publishing: a confirmed finding is a finding whether or not the table
  behind it could be read. The line also carried the absolute path of the file
  on the machine that read it, through the formatted `OSError`; it now names
  the file from the run folder (`review/citations/ledger.jsonl`) and gives the
  reason without the path.
- A run that planned the citations lane and wrote no ledger was reported as one
  where "not one reference or in-text use was checked". That is a claim about
  what the specialist did, and a run whose ledger stage never ran holds no
  record of what was looked at. The line now says only what the folder shows:
  the run planned a citations lane and wrote no citation ledger, so there are
  no ledger counts.
- `unresolved` — a reference the run never looked up — is a fifth reference
  state and was missing from `summary_line`, so the printed states did not add
  up to `references` and a reader could take the difference for resolved rows.
  The sentence now carries all five, `docs/INTEGRATIONS.md` documents five
  rather than four, and the `REVIEW.md` Citations section prints the
  `not_located` count beside the others instead of leaving it to be inferred.
- Wording a consumer prints verbatim. No problem line contains a semicolon any
  more — insilico and paper-workbench ban them in authored text and print these
  lines as they stand — and no line says a reference does not exist unless the
  DOI resolver has no handle for the DOI the reference list printed. The
  citation worker's instruction said "one Haiku worker reading one abstract"
  and now names the role rather than a model family, since `assigned_model` is
  the plan and `model` is what was observed and a hardcoded family is neither.
  A `cannot tell` verdict with nothing to read stores `source_kind: null`
  rather than the string `"none"`, which a page printed as a kind of source.
  The symlink refusal names the file with its folder — `citations/ledger.jsonl`
  — because a run holds more than one file called `usage.jsonl`.
- The Stage A corpus fetch asked a web page for a file, so 20 of the 41
  candidates on disk were HTML saved under a `.pdf` name: PMC's "Preparing to
  download" interstitial and a bot challenge, both served with status 200, and
  both unplantable. `corpus.py` now fetches an article from the PMC Open Access
  Subset on AWS Open Data, which serves the file itself: it lists the article's
  prefix, takes its own PDF rather than a supplement, and prefers the newest
  version. Every download is checked for `%PDF` before anything is written,
  verified bytes are renamed into place from a temporary name, a page or a
  refused request is retried once and then the candidate is dropped by name,
  and a file already on disk counts only if it is a PDF — so a saved
  interstitial is refetched instead of trusted. The publisher URL and OpenAlex
  remain as later routes for a paper outside the subset, and OpenAlex is
  resolved only if it is reached. `corpus.py refill` tops each field up to a
  quota without fetching a paper another field already holds, and
  `corpus.py audit` counts the usable PDFs per field and names the rest, and
  `audit --repair` asks the bucket again for each file that is not a PDF, by
  the PMCID in its own name — which recovered all 20 of the saved pages. A
  truncated search response no longer ends a refill either: JSON is asked for
  up to three times and a field whose search cannot be read is given up on by
  itself.
  `tests/stage_a/test_corpus.py` covers all of it offline against a fake
  network.
- The `follow-up` skill compared two runs by `finding_uid`, which can never
  match across them: a paper's id is a digest of its PDF and is part of the
  uid, so a revised file renames every finding, and the interchangeable core
  slots are in it too. The skill would have reported the whole first review
  resolved and the whole second one new for a revision that changed nothing.
  It now matches a finding on the passage it rests on and the claim it makes,
  uses the uid only to name findings and to follow `duplicate_uids` within one
  run, and records the quote each pairing was made on so a reader can check
  it. `tests/test_plugin_server.py` pins the uid's behaviour across two files.
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
