---
name: peer-review-manuscript
description: Run and inspect an evidence-linked scholarly peer review of a manuscript PDF through PeerReviewAgents. Use when a user asks to review, referee, critique or evaluate a local PDF manuscript and wants source-linked findings rather than general impressions.
---

# Peer Review Manuscript

Use the Peer Review Agents MCP tools to start an engine review, monitor it, and read its reports. One review imports the PDF, has independent reviewers investigate it with bounded source and literature tools, then audits their findings in a debate synthesis. Every finding is anchored to an exact quotation in the manuscript.

## Start a review

1. Resolve the manuscript to an absolute local path. It must be a PDF: the engine imports the PDF's own text, figures and tables, and no other format is supported.
2. Call `start_peer_review` with that path. Everything else is optional and the defaults are the review to ask for: the ensemble of two reviewers plus an audited debate, on a Claude Code subscription, under the `agentic-v1` allowance.
3. Pass `provider: "codex"` only when the user wants the review run on a ChatGPT subscription instead. Claude Code is the default and nothing selects Codex implicitly.
4. Pass `mode: "independent"` for one reviewer with no synthesis, or `mode: "panel"` for one reviewer with a debate pass. `reviewers` sets the ensemble's reviewer count.
5. Tell the user the returned job ID and that the run spends their own subscription usage. Do not wait in one long tool call.

`literature_cutoff` (YYYY-MM-DD) bounds which published work the reviewers may cite, and defaults to today. `policy: "standard-v1"` runs the bounded study budget instead of the default allowance: medium effort, at most five findings in about 900 words. `output_name` names the folder the review writes inside the server's output root.

## Monitor and deliver

1. Call `get_peer_review_status` with the job ID. While it runs, `progress.stage` names the stage the engine is in.
2. When the status is `done`, call `list_peer_review_artifacts` and read `REVIEW.md` with `read_peer_review_artifact`. It carries the run's details, the final review and each independent reviewer's own findings.
3. Quote findings with the manuscript quotation each one is anchored to. Say that a finding is model-reported: source anchoring prevents misplaced edits, but it cannot prove a scientific allegation correct.
4. Name the review folder so the user can inspect the saved run themselves.

A status of `done-without-findings` means the run finished and endorsed nothing, either because the reviewers found nothing or because every finding failed the source guards. Report that, with the note in `errors`, rather than presenting it as a delivered review.

If the status is `error`, report its exact errors. The engine still writes `REVIEW.md` for a stopped run, so read it and say what was saved and what the run did not reach. Do not present a stopped run as a completed review.

## Cancellation

Call `cancel_peer_review` when the user asks to stop a run. A queued review is cancelled. A running one is not: a model call cannot be interrupted, and the response says so. Report whichever happened.

## What this review is not

It does not rerun the authors' analyses, execute their code, or decide a venue. There is no editorial decision, target journal, article type or strictness level: the engine reports source-linked findings and proposed wording changes, and the editorial judgement stays with the user.
