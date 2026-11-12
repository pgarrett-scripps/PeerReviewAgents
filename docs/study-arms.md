# Study arms: the exact command for each one

An arm is a **workflow** now: which agents run, in what order, and what they
can see. It is not a policy, a preset, a mode or a model — 0.9 deleted all four.
Effort, turn limits and the model each slot runs on are pinned in the plugin's
own agent files (`peerreviewagents/plugin/pra/agents/*.md`), identically for
every arm, so what
an arm changes is the shape of the review and nothing else.

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

## The reference arm: the review a person should run

```bash
peerreview review manuscript.pdf arms/reference --literature-cutoff "$CUTOFF"
```

Three core reviewers and all six specialist tracks over the same paper, then
the editor. The core reviewers never see each other's findings and are not told
the specialists exist; the specialists audit one dimension each (`stats`,
`figures-tables`, `methods-reporting`, `citations`, `consistency`, `field`) and
may escalate, and the editor must rule by name on every escalation. This is the
default on every surface, so it needs no `--arm`.

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
  --track stats --track consistency --track methods-reporting
```

The specialists you name, no core reviewers and no editor, which is what the
plugin's `check-draft` skill runs on a draft of your own. `--track` is
repeatable and defaults to `stats`, `consistency` and `methods-reporting`. It
is listed here because it is an arm the command accepts, not because it belongs
in a comparison.

## What an arm cannot change

- **The model.** Each slot's model is pinned in its agent file. To vary it you
  edit `peerreviewagents/plugin/pra/agents/<agent>.md`, which changes the plugin's provenance
  hash in `protocol.json` — a different plugin, honestly recorded, not a flag.
- **The transport.** Every arm runs in a headless Claude Code session on a
  subscription login. There is no provider flag and no API-key path: 0.9
  deleted the Codex provider outright.
- **The budgets.** `--per-agent-tokens`, `--per-run-tokens` and `--timeout`
  exist, but they are ceilings that block a run, not an allowance an arm
  varies. A run that hits one is `blocked` and says so.
