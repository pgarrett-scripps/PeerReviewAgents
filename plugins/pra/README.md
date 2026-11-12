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
| `peerreview` server | `.mcp.json` → `peerreviewagents.plugin.server` | 22 tools, and every piece of state worth forging |
| Agents | `agents/*.md` | `core-reviewer`, six specialists, `editor`, `recorder`; each pins `model:` and `maxTurns:` and may call only server tools |
| Workflow | `workflows/review.js` | ingest → reviewers ∥ specialists → (revision) → editor → record |
| Skills | `skills/*/SKILL.md` | `peer-review-manuscript` (a full review), `check-draft` (your own draft), `follow-up` (what changed since the last run) |
| Starter | `peerreviewagents/plugin/starter.py` | probes, launches the headless session, enforces token budgets, records models and wall time |

## Running one

```
peerreview review <manuscript.pdf> <run folder> [--reviewers N] [--arm NAME]
```

`--arm` is what tells this 0.9 route apart from the 0.8 engine's `review`
while both exist. The starter probes a throwaway session first: a run only
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

## What the server owns

- **Identity.** An agent calls `claim_agent` for a slot and gets a token; every
  other tool takes that token. A token is derived from a per-run salt, so one
  cannot be guessed, and claiming a slot twice supersedes the old token.
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

A rate limit, a breached budget or a missing stage leaves the run `blocked`.
That is an outcome recorded in `runtime.json` and `progress.json`, not an error
to paper over.
