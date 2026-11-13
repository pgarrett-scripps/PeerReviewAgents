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
  rate-limited the run has not recovered in the same turn. Each row also
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

### Changed

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
