# Study arms: the exact command for each one

`peerreview review` has one default, and that default is what a person should
run: the agentic ensemble plus an audited debate, at high effort, with no
finding cap. Everything below is an **ablation of that default**, used to show
what each part of the engine contributes. Each arm is one command. Nothing here
is a different build of the engine; every arm is the same code under a
different named policy, preset or mode.

Two rules hold for every arm:

- The arm goes in the command, never in an edited source file. If an arm cannot
  be expressed as a command, it is not a study arm yet.
- Each arm writes its own run directory, and every run directory records the
  policy, preset, mode and literature cutoff it actually ran under in
  `protocol.json` and `core-artifact.json`. That record, not this document, is
  the evidence of what a run was.

Set `CUTOFF` once so every arm in a comparison is eligible for the same
published work:

```bash
CUTOFF=2026-11-10
```

## Reference arm: the default review

```bash
peerreview review manuscript.pdf arms/reference --literature-cutoff "$CUTOFF"
```

Two independent discovery reviewers, then one audited debate synthesis, all
three stages under `agentic-v1`: high effort, thirty tool calls, no finding
cap, a two hour call limit and one bounded retry. The PDF is imported into
`arms/reference/manuscript`, the run lands in `arms/reference/review`, and
`REVIEW.md` and `REVIEW.txt` are written into `arms/reference`.

Every other arm below takes the same first two positional arguments and the
same cutoff, and differs by exactly one thing.

## Arm: debate off (ordinary synthesis)

```bash
peerreview review manuscript.pdf arms/no-debate --literature-cutoff "$CUTOFF" \
  --preset workspace-ensemble-ordinary-v1
```

The same two independent reviewers, but their findings are merged by an
ordinary synthesis instead of the audited advocate-and-skeptic debate. This is
the arm that isolates the debate's contribution. The preset pins the mode, the
reviewer count, the token budget, the synthesis policy and `standard-v1`, so
pass it on its own; to run this arm on the agentic allowance instead, drop the
preset and name the parts:

```bash
peerreview review manuscript.pdf arms/no-debate-agentic --literature-cutoff "$CUTOFF" \
  --preset workspace-ensemble-ordinary-v1 --policy agentic-v1   # refused: the preset pins its policy
```

That command is refused by design. An arm that mixes a preset's protocol with a
different policy has to say so explicitly through the API:

```python
from peerreviewagents.engine.api import review_request_from_preset, run_review

run_review(review_request_from_preset(
    'workspace-ensemble-ordinary-v1', workspace='arms/reference/manuscript',
    output='arms/no-debate-agentic', literature_cutoff='2026-11-10', policy='agentic-v1'))
```

## Arm: tools off (the manuscript-only baseline)

The command line refuses the manuscript-only workflow on purpose: it is a
baseline, not a review a person should ask for by accident. Run it through the
headless API.

```python
from peerreviewagents.engine.api import review_request_from_preset, run_review

run_review(review_request_from_preset(
    'manuscript-only-single-v1', workspace='arms/reference/manuscript',
    output='arms/no-tools', literature_cutoff='2026-11-10'))
```

One pass over the complete extracted manuscript and its attached figure pixels,
with no workspace tools, no literature lookups and no external sources
(`evidence_profile: manuscript-only-no-tools`, `tool_access: none` in the
protocol). It is the floor the evidence-linked arms are measured against.
Add `policy='agentic-v1'` to run the baseline at the agentic allowance.

## Arm: single reviewer

```bash
peerreview review manuscript.pdf arms/single --literature-cutoff "$CUTOFF" \
  --preset workspace-independent-v1
```

One independent discovery reviewer with the full workspace and evidence tools,
and no synthesis stage at all: the editor package is that reviewer's own
output. This arm separates "more than one reviewer" from "a synthesis stage".

For one reviewer still followed by a synthesis stage, keep the ensemble and set
the count:

```bash
peerreview review manuscript.pdf arms/single-with-synthesis --literature-cutoff "$CUTOFF" \
  --reviewers 1
```

For one long investigative session rather than bounded passes:

```bash
peerreview review manuscript.pdf arms/one-session --literature-cutoff "$CUTOFF" \
  --preset workspace-agentic-v1
```

`workspace-agentic-v1` names a single session, so it is refused on the
ensemble workflow; the preset carries `--mode independent` with it.

## Arm: the study budget

```bash
peerreview review manuscript.pdf arms/study-budget --literature-cutoff "$CUTOFF" \
  --policy standard-v1
```

`standard-v1` holds exactly the limits the engine shipped with and the bounded
study ran on: medium effort, at most five findings in about 900 words, four to
six tool calls, a fifteen minute call limit, no retry. It is also the library
default, so a run made by an embedded caller that names no policy is this arm.
Use it to show what the agentic allowance buys over the original budget.

## The modes `--help` does not list

`--mode` accepts three more values. They are study instruments rather than
choices a reviewer should make, so they are kept out of `--help`; they are
fully supported and are not going away.

| Mode | What it runs | What it is for |
| --- | --- | --- |
| `plain` | One review call, workspace tools available, no candidate inventory and no synthesis: "give your strongest review". | The unstructured-prompt arm: it isolates the structured discovery instruction from the tools. |
| `workspace` | One structured discovery call with a coverage record, and nothing after it. | The discovery-only arm: the inventory without any synthesis or revision. |
| `self-revision` | A `plain` call, then one revision call over its own draft. | The self-revision arm: one model revising itself in place of a second reviewer. |

```bash
peerreview review manuscript.pdf arms/plain        --literature-cutoff "$CUTOFF" --mode plain
peerreview review manuscript.pdf arms/workspace    --literature-cutoff "$CUTOFF" --mode workspace
peerreview review manuscript.pdf arms/self-revised --literature-cutoff "$CUTOFF" --mode self-revision
```

`plain` and `workspace` write no editor package; `peerreview report` reads
their `responses/<mode>-guarded.json` instead.

## Transport

Every arm above runs on a Claude Code subscription, which is the default
transport: nothing selects Codex implicitly. To run an arm on a Codex
subscription instead, add `--provider codex` (or name a Codex model with
`--model`). No preset pins a provider, so the same arm is directly comparable
across the two transports.

```bash
peerreview review manuscript.pdf arms/reference-codex --literature-cutoff "$CUTOFF" \
  --provider codex
```
