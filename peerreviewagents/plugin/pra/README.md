# pra — the PeerReviewAgents plugin

One peer review of one manuscript, run as a Claude Code plugin: a workflow
script fans out N core reviewers and six specialists over the same paper, an
editor pools what they found into one package, and the run is written to disk
with every finding anchored to a quotation in the manuscript.

Nothing here trusts an agent's own account of what it did. The MCP server owns
the manuscript, the identities, the ledger, the budgets and the files; an agent
can only ask it for text and hand it findings.

## The parts

| Part | Where | What it is |
| --- | --- | --- |
| `peerreview` server | `.mcp.json` → `peerreviewagents.plugin.server` | 23 tools, and every piece of state worth forging |
| Agents | `agents/*.md` | `core-reviewer`, six specialists, `editor`, `recorder`; each pins `maxTurns:` and a fallback `model:`, and may call only server tools |
| Models | `peerreviewagents/plugin/models.py` | which model each slot runs, and the refusal when one is not available |
| Workflow | `workflows/review.js` | ingest → reviewers ∥ specialists → (revision) → editor → record |
| Skills | `skills/*/SKILL.md` | `peer-review-manuscript` (a full review), `check-draft` (your own draft), `follow-up` (what changed since the last run) |
| Starter | `peerreviewagents/plugin/starter.py` | probes, launches the headless session, enforces token budgets, records models and wall time |

## Running one

```
peerreview review <manuscript.pdf> <run folder> [--reviewers N] [--arm NAME]
                  [--reviewer-models A,B,C] [--specialist-model M] [--editor-model M]
```

That is the only way to run a review in 0.9: the Python orchestration the 0.8
engine used is deleted, and `--arm` is an ordinary option that picks which
agents run. The starter probes a throwaway session first: a run only
starts once the start-up event shows the plugin loaded, `peerreview`
connected, and every tool the agents are allowed to call present. It never
uses `--bare`, which skips plugin sync and reads an API key; this runs on a
Claude Code subscription login only.

## Arms

| `--arm` | Core reviewers | Specialists | Editor | For |
| --- | --- | --- | --- | --- |
| `full-two-track` (default) | N | all six | yes | the real review |
| `reviewers-only` | N | none | yes | what the specialist track adds |
| `single` | 1 | none | no | the baseline |
| `single-revision` | 1 (then revises itself) | none | no | what a second pass adds |
| `manuscript-only` | 1, text tools only | none | no | what pages and lookups add |
| `draft-check` | none | chosen | no | `check-draft` on a draft of your own |

## Models

The core reviewers are deliberately different models. Stage A ran three of them
on one model and they caught no more than one did — three readings of the same
manuscript by the same reader — so reviewer *i* takes the *i*-th model of an
ordered list, wrapping round:

| Slot | Default | Flag |
| --- | --- | --- |
| `core-1`, `core-2`, `core-3`, … | `claude-sonnet-5-5`, `claude-opus-5-5`, `claude-haiku-5-5`, then round again | `--reviewer-models A,B,C` |
| `revision-1` | `core-1`'s model: it is `core-1` revising its own findings | `--reviewer-models` |
| `spec-*` | `claude-sonnet-5-5` | `--specialist-model` |
| `editor` | `claude-fable-5-2` | `--editor-model` |
| `recorder` | `claude-haiku-5-5` | `PEERREVIEW_RECORDER_MODEL` |

So one reviewer is Sonnet, two are Sonnet and Opus, and three are all three.
A session that drives the workflow itself passes `reviewer_models`,
`specialist_model` and `editor_model` to `ingest`; a session the starter
launched reads `PEERREVIEW_REVIEWER_MODELS`, `PEERREVIEW_SPECIALIST_MODEL`,
`PEERREVIEW_EDITOR_MODEL` and `PEERREVIEW_RECORDER_MODEL` from its environment,
and an explicit `ingest` argument wins over the environment. Nothing here is
gated on billing or usage: which models a login may run is yours to decide.

Each agent is started with its family alias as a per-spawn override, which is
what Claude Code takes, and the pinned version is what the run records —
`models` in `protocol.json`, `assigned_model` on each agent and each row of
`reviewer_provenance`, beside the `model` the agent reported running as.

**A model this login cannot run is a refusal, not a substitution.** Claude Code
answers a model it may not run by quietly running another, so a run that asked
for Opus could be recorded as an arm it was not. Both entry points check first
— `peerreview review` before the probe spends anything, `ingest` before the
manuscript is imported — and fail with one line naming the slot, the model and
the flag that changes it. Availability is the `availableModels` allowlist in
the settings files Claude Code reads, managed settings first.

## What the server owns

- **Identity.** `ingest` mints one claim code per slot and returns them to the
  recorder, which gives each agent its own code in its own prompt. An agent
  calls `claim_agent` with that code and gets a token; every other tool takes
  the token. A code works once, a slot someone else holds is not re-claimed,
  and a retry asks the recorder for a fresh code through `reissue_claim_code`,
  which drops the stuck holder's token. A slot name is not an identity: without
  the codes, a reviewer that guessed `recorder` read every other reviewer's
  findings.
- **Tools per track.** Each slot has an allow list. A citations specialist
  asking for figure pages is refused, and `manuscript-only` has the page and
  lookup tools removed outright.
- **Budgets.** Per-agent and per-run tool-call ceilings, plus a separate
  external-request ceiling, all enforced in the server. The starter enforces
  token ceilings from the stream on top.
- **Findings.** `review-finding-v3`: `finding_uid` (stable across re-reviews of
  the same paper), `track`, `escalate` (specialists only), and a `basis` the
  server assigns — `computed` when a `calculate` receipt covers the numbers
  quoted, `two-quotes` when a second anchored quotation corroborates,
  `judgement` otherwise. An unanchored quotation is quarantined, not stored.
- **Isolation.** Core reviewers never see each other's findings and are not
  told specialists exist. The editor sees the pool in an order seeded by the
  run salt, so slot order carries no weight.
- **The ledger.** Stages, provenance hashes of the plugin and engine files, and
  the run's own files; `write_review` refuses while a stage is missing or no
  editor package has been accepted.

## Editor rules

One decision per finding, a reason for each, duplicates merged by `finding_uid`,
at most eight findings in the body, and a ruling by name on every escalation.
Leaving a `computed` finding out of the body needs an anchored rebuttal
quotation. The verdict is two axes in words, never a number.

## Run folder

```
<run>/manuscript/        the imported paper
<run>/review/REVIEW.md   the review
<run>/review/editor-package.json
<run>/review/reviewer-<n>/    each reviewer's own package view
<run>/review/audit_<track>.md six specialist audits
<run>/review/protocol.json    arm, slots, versions, provenance
<run>/review/progress.json    stages and status
<run>/review/runtime.json     tokens per agent, models, wall time, rate limits
<run>/review/usage/<slot>/    the stream's own usage record
```

## The first real run

One `full-two-track` run with 3 reviewers, on the same FTLD-TDP manuscript the
0.8.0 live run used: complete, 4 minutes 7 seconds, 636,299 tokens over
11 agents, 89 tool calls. 53 findings stored and none quarantined — core 17,
field 7, methods-reporting 6, figures-tables 6, citations 6, consistency 6,
stats 5 — of which the editor put 8 in the body, 45 in the appendix, and ruled
by name on all 4 escalations. For comparison the 0.8.0 run of the same paper
delivered 11 findings for 1,462,572 tokens.

Of the 53, 7 earned `two-quotes` and, as the run was stored, none earned
`computed` despite 17 `calculate` calls: the rule then counted a receipt only
when its numbers appeared in the quoted sentence, and a reviewer quoting one
sentence rarely quotes every number it combined. 0.9 widened the rule to the
blocks a finding cites, which earns that run one `computed` finding: the stats
specialist's confidence-interval asymmetry check, whose one unquoted input (the
estimate, 0.1044) sits in the same block `b0048` the finding is anchored to.

A rate limit, a breached budget or a missing stage leaves the run `blocked`.
That is an outcome recorded in `runtime.json` and `progress.json`, not an error
to paper over.
