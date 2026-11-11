# Study arms: the exact command for each one

`peerreview review` has one default, and that default is what a person should
run: the agentic ensemble plus an audited debate, at high effort, with no
finding cap. Everything below is an **ablation**, used to show what each part
of the engine contributes, measured against one of the two reference arms in
the next section. Each arm is one command. Nothing here is a different build of
the engine; every arm is the same code under a different named policy, preset
or mode.

Three rules hold for every arm:

- The arm goes in the command, never in an edited source file. If an arm cannot
  be expressed as a command, it is not a study arm yet.
- Each arm writes its own run directory, and `protocol.json` in that directory
  records what the run was actually allowed to do: the mode, the model and
  provider, the literature cutoff, the effort, the per-stage tool and
  external-request limits, the observed-token ceiling the run started under,
  and — for every policy except `standard-v1` — the policy name, its version
  and its full record. That record, not this document, is the evidence of what
  a run was.
- A preset name is not in `protocol.json`. The command line expands a preset
  into the mode, reviewer count, token budget and policy it pins, and records
  those, so name the arm in its output directory. `core-artifact.json`, which
  does carry the preset name, is written only by runs started through
  `peerreviewagents.engine.api.run_review`, never by `peerreview review`.

Set `CUTOFF` once so every arm in a comparison is eligible for the same
published work:

```bash
CUTOFF=2026-11-10
```

## The two reference arms

An ablation is readable only against a reference that differs from it in one
place, and the presets below pin `standard-v1` along with the mechanism they
ablate. A preset arm is therefore not comparable with the agentic default:
it differs from it in two things at once. So there are two reference arms, and
every arm below names the one it belongs to.

### Study reference arm: ensemble and audited debate under `standard-v1`

```bash
peerreview review manuscript.pdf arms/reference-study --literature-cutoff "$CUTOFF" \
  --preset workspace-ensemble-v1
```

**This is the reference arm for the paper.** Two independent discovery
reviewers, then one audited debate synthesis, all three stages under
`standard-v1`: medium effort, at most five findings in about 900 words, four
tool calls and six external requests per reviewer, two of each in the debate, a
fifteen minute call limit, no retry, and one 750,000 observed-token ceiling for
the whole run. Every preset arm below keeps exactly those limits and changes
one mechanism, so the difference measured is the mechanism.

`--policy standard-v1` without the preset runs the same three stages under the
same limits; the preset only also labels the configuration record. Either
command is this arm.

### Default reference arm: the review a person should run

```bash
peerreview review manuscript.pdf arms/reference-default --literature-cutoff "$CUTOFF"
```

The same three stages under `agentic-v1`: high effort, thirty tool calls and
twenty-four external requests per stage, no finding cap, a two hour call limit,
one bounded retry, and a 1,500,000 ceiling per stage — 4,500,000 for two
reviewers plus the debate, scaled by reviewer count. This is the engine default
on every surface, and it is the reference for the allowance arms. It differs
from the study reference arm in the policy alone.

In both forms the PDF is imported into `<output>/manuscript`, the run lands in
`<output>/review`, and `REVIEW.md` and `REVIEW.txt` are written into
`<output>`. Every arm below takes the same first two positional arguments and
the same cutoff.

### What each arm changes

| Arm | Reference | Exactly what differs |
| --- | --- | --- |
| `arms/study-budget` (`--policy standard-v1`) | default | the policy, and nothing else: medium effort, a five-finding cap, four tool calls, one 750,000 whole-run ceiling. It is the study reference arm, reached without the preset. |
| `arms/no-debate` (`--preset workspace-ensemble-ordinary-v1`) | study | the synthesis stage: an ordinary synthesis in place of the audited debate |
| `arms/single` (`--preset workspace-independent-v1`) | study | the stages after discovery: one reviewer and no synthesis at all |
| `arms/no-tools` (`manuscript-only-single-v1`, API only) | study | the evidence: one pass over the manuscript and its figures, no workspace tools and no literature |
| `arms/single-with-synthesis` (`--reviewers 1`) | default | the reviewer count: one discovery reviewer, the debate kept |
| `arms/one-session` (`--preset workspace-agentic-v1`) | default | the session shape: one long agentic session in place of three agentic stages, which also means one reviewer and no synthesis |
| `arms/plain`, `arms/workspace`, `arms/self-revised` (`--mode`) | default | the prompt and stage structure of a single reviewer, as the mode table below states |

The three arms whose cell says "which also means" or "and no synthesis" change
more than one thing by construction: a single session cannot have a second
reviewer, and a run with no synthesis stage has no debate to vary. They are
reported as the compound arms they are, not as one-factor ablations.

## Arm: debate off (ordinary synthesis)

```bash
peerreview review manuscript.pdf arms/no-debate --literature-cutoff "$CUTOFF" \
  --preset workspace-ensemble-ordinary-v1
```

Compared with the study reference arm, one thing differs: the synthesis stage.
The same two independent reviewers under the same `standard-v1` limits, but
their findings are merged by an ordinary synthesis instead of the audited
advocate-and-skeptic debate. This is the arm that isolates the debate's
contribution. The preset pins the mode, the
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
    'workspace-ensemble-ordinary-v1', workspace='arms/reference-study/manuscript',
    output='arms/no-debate-agentic', literature_cutoff='2026-11-10', policy='agentic-v1'))
```

## Arm: tools off (the manuscript-only baseline)

The command line refuses the manuscript-only workflow on purpose: it is a
baseline, not a review a person should ask for by accident. Run it through the
headless API.

```python
from peerreviewagents.engine.api import review_request_from_preset, run_review

run_review(review_request_from_preset(
    'manuscript-only-single-v1', workspace='arms/reference-study/manuscript',
    output='arms/no-tools', literature_cutoff='2026-11-10'))
```

One pass over the complete extracted manuscript and its attached figure pixels,
with no workspace tools, no literature lookups and no external sources
(`evidence_profile: manuscript-only-no-tools`, `tool_access: none` in the
protocol). It is the floor the evidence-linked arms are measured against.
Against the study reference arm two things differ, and neither can be separated
from the other: the evidence tools are gone, and with one pass there is no
second reviewer and no synthesis. Add `policy='agentic-v1'` to run the same
baseline at the agentic allowance, which is the floor for the default
reference arm instead.

## Arm: single reviewer

```bash
peerreview review manuscript.pdf arms/single --literature-cutoff "$CUTOFF" \
  --preset workspace-independent-v1
```

One independent discovery reviewer with the full workspace and evidence tools,
under the same `standard-v1` limits as the study reference arm, and no
synthesis stage at all: the editor package is that reviewer's own output.
Against that reference, the second reviewer and the synthesis stage both go,
which is what the next arm separates.

For one reviewer still followed by a synthesis stage — the default reference
arm with its reviewer count changed and nothing else — keep the ensemble and
set the count:

```bash
peerreview review manuscript.pdf arms/single-with-synthesis --literature-cutoff "$CUTOFF" \
  --reviewers 1
```

For one long investigative session rather than three agentic stages. It keeps
the agentic allowance of the default reference arm, so the session shape is
what differs — and with it, by construction, the reviewer count and the
synthesis stage:

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
six tool calls, a fifteen minute call limit, no retry, and one 750,000
observed-token ceiling for the whole run rather than a ceiling per stage.

This is the same three stages as the study reference arm above, reached without
a preset, and against the default reference arm the policy is the only thing
that differs. Use it to show what the agentic allowance buys over the original
budget.

`standard-v1` is no longer any surface's default: the command line,
`ReviewRequest`, `run_review`, `review`, `ensemble_review` and
`get_review_policy(None)` all mean `agentic-v1`. A run under the study budget
has to name it, by `--policy standard-v1`, by `policy='standard-v1'`, or
through a preset that pins it.

## The modes `--help` does not list

`--mode` accepts three more values. They are study instruments rather than
choices a reviewer should make, so they are kept out of `--help`; they are
fully supported and are not going away. Each names no policy, so each runs
under `agentic-v1` and belongs to the default reference arm: what differs is
the single reviewer's prompt and the stages after it. Add `--policy
standard-v1` to run the same three mode arms against the study reference arm.

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
