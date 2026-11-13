# Study arms: the exact command for each one

An arm is a **workflow** now: which agents run, in what order, and what they
can see. It is not a policy, a preset, a mode or a model — 0.9 deleted all four.
Effort and turn limits are pinned in the plugin's own agent files
(`peerreviewagents/plugin/pra/agents/*.md`), identically for every arm, so what
an arm changes is the shape of the review and nothing else. The model each slot
runs on is a flag, and the same for every arm only if you keep it so — see
[Models](#models) below.

`peerreview review` has one default, and that default is what a person should
run: `full-two-track`. Everything else is an **ablation**, measured against it
or against `single`.

Three rules hold for every arm:

- The arm goes in the command, never in an edited source file. If an arm cannot
  be expressed as a command, it is not a study arm yet.
- Each arm writes its own run folder, and `<run>/review/protocol.json` records
  what the run was actually allowed to do: the arm, every agent slot with its
  role, track, model, tool allow list and call budget, the literature cutoff,
  the plugin and engine provenance hashes, and the manuscript manifest. That
  record, not this document, is the evidence of what a run was.
- The server enforces the arm. A `manuscript-only` reviewer asking for a page
  image is refused by the server, not discouraged by its prompt.

Set `CUTOFF` once so every arm in a comparison is eligible for the same
published work:

```bash
CUTOFF=2026-11-10
```

## Models

The core reviewers run on different models by default — reviewer *i* takes the
*i*-th of `sonnet,opus,haiku`, wrapping round — so **changing the reviewer
count changes which models read the paper.** `single` is one Sonnet reviewer
and the reference arm is Sonnet, Opus and Haiku, which is two differences, not
one. An arm comparison therefore says which of the two questions it is asking,
and pins the models accordingly:

```bash
# What the shape of the review is worth, models held still.
peerreview review manuscript.pdf arms/single --arm single --reviewer-models sonnet
peerreview review manuscript.pdf arms/reference --reviewers 3 --reviewer-models sonnet

# What three readers are worth when they are three different models.
peerreview review manuscript.pdf arms/mixed --reviewers 3
```

`--reviewer-models`, `--specialist-model`, `--editor-model`,
`--recorder-model` and `--citation-worker-model` take one of the four models
this version knows —
`claude-sonnet-5-5`, `claude-opus-5-5`, `claude-haiku-5-5`,
`claude-fable-5-2` — by alias or by id, each run records what it was given in
`protocol.json`
(`models`, `reviewer_models`, and `assigned_model` per slot), and a folder that
already holds a run under other models is refused rather than reused. Every
Stage A result recorded before 0.9.0 ran one model for every reviewer, so a run
being compared against one pins `--reviewer-models` to that model.

The citation workers are the one slot kind an arm can also change the *number*
of: `--citation-workers N`, 0 to 12, default 4, each on
`--citation-worker-model` (`claude-haiku-5-5` by default). More workers judge
the same in-text citations in less wall time and cost the same tokens; `0`
leaves the ledger resolved but unjudged, which is a legitimate arm — it asks
what the judging stage is worth — and the run reports it as unfinished rather
than clean. The workers exist only where the `citations` specialist runs, so
`--citation-workers` is silently nothing in an arm without that track.

## The reference arm: the review a person should run

```bash
peerreview review manuscript.pdf arms/reference --literature-cutoff "$CUTOFF"
```

Three core reviewers and all six specialist tracks over the same paper, then
the editor. The core reviewers never see each other's findings and are not told
the specialists exist; the specialists audit one dimension each (`stats`,
`figures-tables`, `methods-reporting`, `citations`, `numbers`, `field`) and
may escalate, and the editor must rule by name on every escalation. This is the
default on every surface, so it needs no `--arm`.

The six audit disjoint dimensions, one each, and every brief names the other
five as not its own: `numbers` owns every printed quantity and the arithmetic
between them, `stats` the inference drawn from them, `figures-tables` what a
panel shows, `methods-reporting` what is absent from the methods, `citations`
the reference list and every use of it, `field` what this kind of study is
required to do. The split is why a per-track finding count means something: a
defect is filed by the lane that owns it rather than by however many
specialists happened to see it.

**One known deviation from the blinding, and it is in the run folder rather
than in any prompt.** A specialist's audit is written to
`<output>/review/audit_<track>.md`, so the set of file names in a finished run
says which lanes ran: a run of this version holding `audit_numbers.md` ran the
`numbers` lane, and one that does not, did not. Nothing an agent reads
exposes that: a core reviewer is never told the specialists exist, each
specialist is told the other five lanes by name but never which of them is
running in this arm, and no slot can list the run folder. It matters only to
whoever reads the arms afterwards, and it is written down here because a
blinded comparison scored by a person who can see the file names is not
blinded for that person.

Because the `citations` track is in it, this arm also keeps a citation ledger:
the server writes one row per reference and one per in-text use, resolves every
reference against Crossref and Europe PMC without a model call, and fans out
`--citation-workers` Haiku workers (`cite-1`, `cite-2`, …) to judge each use
against the resolved source before the citations specialist reads the table.
Those workers are slots in `protocol.json` like any other, with their assigned
and observed models, and they are required like none of them: a worker that
never reports leaves rows unjudged instead of holding the review open.

The PDF is imported into `<output>/manuscript`, the run lands in
`<output>/review`, and `REVIEW.md` and `REVIEW.txt` are written there. Every
arm below takes the same two positional arguments and the same cutoff.

## What each arm changes

| `--arm` | Core | Specialists | Editor | Reference | Exactly what differs |
| --- | --- | --- | --- | --- | --- |
| `full-two-track` (default) | N | all six | yes | — | the reference arm |
| `reviewers-only` | N | none | yes | reference | the specialist track is gone, the editor stays |
| `single` | 1 | none | no | `reviewers-only` | the reviewer count and the editor: one reviewer's own findings are the review |
| `single-revision` | 1, then revising itself | none | no | `single` | one revision pass over its own findings |
| `manuscript-only` | 1, text tools only | none | no | `single` | the evidence: no page images, no supplements, no literature or identifier lookups |
| `draft-check` | none | chosen | no | — | not a study arm: what `draft-check` runs on a draft of your own |

Only the arms that run the `citations` specialist keep a citation ledger —
`full-two-track`, and `draft-check` when `citations` is one of the chosen
tracks. The rest write no `citations/` folder and report no citation counts, so
an ablation is never measured against numbers its reference arm does not have.
`read_run()['citations']['enabled']` is how an analysis tells the two apart:
absent is not zero.

`single` differs from `reviewers-only` in two things by construction — a
one-reviewer run has no second reviewer to pool and nothing for an editor to
decide between — so it is reported as the compound arm it is. Read the chain
downward: each row's reference is the row above it.

## Arm: the specialist track off

```bash
peerreview review manuscript.pdf arms/reviewers-only --literature-cutoff "$CUTOFF" \
  --arm reviewers-only
```

The same N core reviewers with the same tools and the same editor, and no
specialist tracks at all. This is the arm that isolates what the six audits
contribute: the editor still pools, merges by `finding_uid`, caps the body at
eight and gives a reason per finding, but there is nothing to escalate.

Keep `--reviewers` the same as the reference run, or the comparison also
changes the reviewer count:

```bash
peerreview review manuscript.pdf arms/reference      --literature-cutoff "$CUTOFF" --reviewers 3
peerreview review manuscript.pdf arms/reviewers-only --literature-cutoff "$CUTOFF" --reviewers 3 \
  --arm reviewers-only
```

## Arm: one reviewer, no editor

```bash
peerreview review manuscript.pdf arms/single --literature-cutoff "$CUTOFF" --arm single
```

One core reviewer with the full tool set, one pass, and no editor stage: the
review is that reviewer's own findings, written up as they stand. It is the
baseline the pooled arms are measured against, and the cheapest arm by a long
way.

## Arm: the reviewer revising itself

```bash
peerreview review manuscript.pdf arms/single-revision --literature-cutoff "$CUTOFF" \
  --arm single-revision
```

The `single` arm, then the same brief handed back to the reviewer over its own
findings. Against `single` the revision pass is the only thing that differs, so
this is the arm that answers whether a second look by the same agent is worth
what a second reviewer costs.

## Arm: tools off (the manuscript-only baseline)

```bash
peerreview review manuscript.pdf arms/manuscript-only --literature-cutoff "$CUTOFF" \
  --arm manuscript-only
```

One core reviewer with the manuscript text only. The server removes the page
and supplement image tools and every network tool from that slot's allow list,
so the reviewer keeps `read_blocks`, `search`, `calculate`, `save_note`,
`inspect_results` and `record_comparison` and has no way to look at a figure or
check a citation. It is the floor the evidence-linked arms are measured
against: against `single`, the evidence is what differs.

A finding here can still be `computed` or `two-quotes` — both rest on the
manuscript — but nothing in this arm can be checked against the literature.

## Not a study arm: `draft-check`

```bash
peerreview review draft.pdf arms/draft-check --arm draft-check \
  --track stats --track numbers --track methods-reporting
```

The specialists you name, no core reviewers and no editor, which is what the
plugin's `check-draft` skill runs on a draft of your own. `--track` is
repeatable and defaults to `stats`, `numbers` and `methods-reporting`. It
is listed here because it is an arm the command accepts, not because it belongs
in a comparison.

Adding `--track citations` to this arm turns the citation ledger on with it:
every reference is looked up and every in-text use judged, which is the slowest
and most network-bound track and the one an author usually wants before
submission. Without that track, `draft-check` writes no `citations/` folder and
`read_run()['citations']['enabled']` is `False`.

## What an arm cannot change

- **The model.** The arm does not choose it: the model flags above do, the same
  way for every arm, and `protocol.json` records the plan beside what each slot
  was observed running. An arm comparison that also changes a model is two
  differences, which is why [Models](#models) says to pin them.
- **The transport.** Every arm runs in a headless Claude Code session on a
  subscription login. There is no provider flag and no API-key path: 0.9
  deleted the Codex provider outright.
- **The budgets.** `--per-agent-tokens`, `--per-run-tokens` and `--timeout`
  exist, but they are ceilings that block a run, not an allowance an arm
  varies. A run that hits one is `blocked` and says so.
