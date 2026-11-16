# Running the engine from a paper-scaffold manuscript

[paper-scaffold](https://github.com/pgarrett-scripps/paper-scaffold) is a Typst
manuscript repository with its own review skills: `claim-audit`,
`methods-vs-code`, `figure-review`, `prose-review`, `peer-review` and
`literature-check`, which `/paper:review-all` runs in parallel and whose
findings it merges into one action ledger, `reviews/ACTIONS.md`. Those reviews
are the same session reading the paper it is writing. This engine is a
different thing: separate headless sessions, specialist agents that cannot see
each other, and a guard that throws out any finding whose quote is not in the
PDF it was given.

The adapter makes the second kind available as a member of the first. It is
one skill, `/paper:draft-check`, which runs

```
peerreview review paper.pdf reviews/engine/<YYYY-MM-DD> \
  --arm draft-check --track stats --track numbers --track methods-reporting
```

and writes `reviews/<YYYY-MM-DD>-draft-check.md` in the shape
`/paper:review-all`'s merge step already takes. `review-all` gains it as an
opt-in seventh agent, on the words "with the engine", for the same reason the
literature check is opt-in: it spends its own tokens.

## The patch

[`paper-scaffold-adapter.patch`](paper-scaffold-adapter.patch) in this folder.
It applies to paper-scaffold at `bc4ef42` (release 3.24.2):

```
cd ../paper-scaffold
git apply --check ../PeerReviewAgents/docs/paper-scaffold-adapter.patch
git apply ../PeerReviewAgents/docs/paper-scaffold-adapter.patch
```

Four files: the new `plugins/paper/skills/draft-check/SKILL.md`, and the
`which` parameter, the launch clause and the merge rule in
`plugins/paper/skills/review-all/SKILL.md`, plus the skill lists in `CLAUDE.md`
and `docs/working-with-ai.md`. It is a patch rather than a commit because
paper-scaffold is not this repository's to change: the manuscript side owns
when its plugin gains a skill, and the version number in
`plugins/paper/.claude-plugin/plugin.json` is theirs to bump.

## The four decisions in it

**The PDF, not the sources.** Every other `review-all` member reads
`paper.review.txt`, the reviewers' copy with numbers resolved. The engine
cannot: it anchors each finding to a quotation in a PDF it ingested itself, and
it refuses a finding whose quote it cannot place. So `draft-check` is handed
`paper.pdf`, builds it with `just paper` if `just check-build` says it is
stale, and gives each row's location as a section heading plus a quoted
sentence from the issue's members. A `block_id` is a position in that PDF and
does not survive a rebuild, so it goes in the `source` column and never in
`location`.

**The issue is the row.** The run's own unit of work is the issue, not the
finding: `review/issues.json` holds one issue per problem, with a severity, a
category, a rank, a statement and the findings that evidence it as `members`.
One ledger row is one issue, which is what the author has to act on, and the
member quotes are the evidence inside it. Two reviewers who raised the same
problem are one row that says "raised by 2" rather than two rows to deduplicate
by hand.

**A severity map that keeps `moderate`.** The engine's issue severities are
`major`, `moderate` and `minor`; the ledger's columns are `blocker`, `major`
and `minor`. The adapter maps a `major` whose evidence was escalated to a
blocker, `major` to major, `moderate` to major, and `minor` to minor. Nothing
becomes a blocker that the engine did not escalate, because a blocker in that
ledger is what decides "not ready" and an outside program should not be able to
declare one on its own. What changed is the `moderate` row: it is a required
change in the engine's own report, so mapping it to `minor` lost exactly the
distinction the author needed. It now maps up, and the row carries the engine's
own severity verbatim beside the issue id, so a reader can tell a major row
that was major from one that was moderate.

**`publishable` is not the gate here.** `read_run` reports `publishable:
false` for every `draft-check` run, and that is correct rather than a fault:
the arm runs no editor, so its issues are the server's one-per-finding
synthesis rather than an editor's grouping, which `not_publishable_reasons`
says in those words and `issues_source` says as `server` where a full run says
`editor`. `publishable` asks whether a run is a finished
peer review; an author's check is not one and does not need to be. The adapter
gates on `status` being `complete` and on `problems` being empty, and the skill
says so, because a skill that read `publishable` as a health check would report
every successful author pass as a failure.

## What the engine's side already provides

Nothing in this repository changes for the adapter. It uses:

* `peerreview review ... --arm draft-check --track ...` — the specialist-only
  arm, no core reviewer and no editor, which is what the `draft-check` skill
  runs inside a session.
* `peerreview report <run>` — the offline reader. It prints `status`, `cause`,
  `findings`, `issues`, `required`, `suggestions`, `publishable`,
  `not_publishable_reasons`, `problems` and the report paths, and makes no
  model call, so the adapter never has to parse a run folder to find out what
  happened to it.
* `review/issues.json` — the review itself: one issue per problem, with its
  severity, category, rank, statement and the findings that evidence it as
  `members`. It is what the ledger rows are built from, and every arm writes
  it, so the adapter reads one shape whether an editor grouped the findings or
  the server synthesised them one apiece.
* `review/findings.jsonl`, `review/audit_<track>.md`,
  `review/quarantined.jsonl` and `review/runtime.json` — the raw findings
  behind the issues, each track's own write-up, what the guard rejected and the
  run's cost.

That list is the whole interface, and all of it is public: the patch imports no
Python module from this package at all. The two commands it runs are the two in
`peerreviewagents.CLI_COMMANDS`, and the five files it reads are the run-folder
files [`INTEGRATIONS.md`](INTEGRATIONS.md) documents. An adapter that reached
into `peerreviewagents.engine.*` would be a second copy of engine behaviour
living in a repository that cannot see when the engine changes it.

A wrapping application that wants the same thing in Python rather than in a
skill should read [`INTEGRATIONS.md`](INTEGRATIONS.md) instead, and it has the
same constraint: `review_main`, `report_main`, `read_run` and `locate_run` are
the supported surface, both `_main` functions take a `prog` so the wrapper's own
name appears in its help, and an application that writes files the engine reads
back — a revision, a saved review, a scaffold import — takes the digest, the
JSON reader and writer and the review normalisation from
`peerreviewagents.workspace` rather than implementing its own.
