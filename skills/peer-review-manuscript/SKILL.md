---
name: peer-review-manuscript
description: Run and inspect a rigorous multi-agent scholarly peer review through PeerReviewAgents. Use when a user asks to review, referee, critique, evaluate, or produce an editorial decision for a local PDF, Markdown, LaTeX, or text manuscript.
---

# Peer Review Manuscript

Use the Peer Review Agents MCP tools to start a review, monitor it, and read its artifacts.

## Start a review

1. Resolve the manuscript to an absolute local path.
2. Choose the provider for the active client. Use `claude-code` in Claude Code, `codex` in Codex, `droid` in Factory Droid, and `pi` in Pi.
3. Ask for a target journal only when venue-specific review matters. Use `general` otherwise.
4. Call `start_peer_review`. Keep `research_enabled` false unless the user explicitly wants live literature searches.
5. Tell the user the returned job ID. Do not wait in one long tool call.

Use `model: default` to honor the coding client's configured model. API providers remain available when the matching API key is configured.

## Monitor and deliver

1. Call `get_peer_review_status` using the job ID.
2. If the job is queued or running, report its current stage and check again when the user asks.
3. When the job is done, call `list_peer_review_artifacts`.
4. Read `summary.md` and `decision_letter.md` first. Read specialist reports when the user asks for detail or when the decision needs supporting evidence.
5. Link or name the report directory so the user can inspect every artifact.

If a job fails, report the exact errors from its status. Do not present a partial run as a completed editorial decision.

## Cancellation

Call `cancel_peer_review` when the user asks to stop a run. Cancellation takes effect after an active model call returns.
