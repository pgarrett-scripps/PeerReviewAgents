# Evidence-linked review engine

`peerreviewagents.engine` reviews a manuscript in context. It imports a PDF, optionally uses publisher JATS XML for text and references, preserves supplementary table cells, and produces source-linked findings and proposed wording changes. It does not rerun author analyses.

This subpackage is the headless engine that landed in PeerReviewAgents 0.7.0 and became the whole program in 0.8.0: the LangGraph panel that used to make up the rest of the package was deleted, and every surface — the command line, the web UI, the MCP server and the local agent plugins — now runs this engine. The panel is frozen at the tag `legacy-langgraph-v0.6.1`. The app layer that used to sit on top of this engine (the local viewer, the revision loop, native Scaffold export, Word review copies) is not part of it.

## Use

PDF import requires `rustypaper`, Pillow and Poppler. Every command below is available both as `peerreview <command>` and as `python -m peerreviewagents.engine <command>`.

```bash
peerreview import manuscript.pdf workspace --xml manuscript.xml --supplements supplement-directory
peerreview verify workspace
```

Sources stay local unless you explicitly run a model review. A review sends manuscript text and selected source images to the authenticated Claude Code or Codex subscription for the selected model, and sends focused literature queries to Crossref or Europe PMC.

Import structured findings with `peerreview add-review workspace response.json --id review-name`. Findings contain a block ID, exact quote, kind, comment, evidence block IDs, and optional replacement. The schemas in `review_schema.py` are the current reference. Ambiguous anchors remain unplaced comments. Replacements introducing numerical literals are withheld. Overlapping accepted edits and stale source hashes are rejected.

## Investigation tools

`investigation.py` exposes a bounded MCP server for source reads and searches, PDF page and supplementary figure inspection, saved evidence notes, arithmetic, DOI metadata checks, and focused literature searches. Tool logs and retrieved metadata retain provenance. Search defaults to a publication cutoff configured per paper. Abstracts and DOI metadata are not treated as full-text support.

The engine defaults to Claude Code and `claude-sonnet-5-5`. Codex Terra and Sol remain fully supported, but only when `--provider codex` or a Codex model is named: nothing selects Codex implicitly. A review defaults to the agentic ensemble: independent reviewers under the `agentic-v1` allowance, then an audited debate synthesis. `agentic-v1` is high effort, thirty tool calls, no finding cap, a two hour call limit and one bounded retry, and it is the default on every surface: the command line, `ReviewRequest`, `run_review`, `review`, `ensemble_review` and `get_review_policy(None)`. Medium effort and the five-finding cap belong to `standard-v1`, the bounded study budget, which now has to be named. `--mode independent` is one reviewer, `--mode panel` is the single-reviewer run with a debate pass, and the remaining study modes are not listed in `--help`; [`docs/study-arms.md`](../../docs/study-arms.md) gives the exact command for each ablation arm. No API key, fallback model, shell tool or author-code execution is used. Inputs and code are fingerprinted before inference. Cache identity includes tool configuration, the manuscript manifest, and attached image paths and hashes. Completed calls can be recovered without another inference. Unknown usage and unresolved interrupted calls block further inference in that attempt directory. The observed-token stop is checked before each call. It is not a hard within-call cap.

```bash
# A PDF is imported, reviewed and reported in one command.
peerreview review manuscript.pdf run-output --literature-cutoff 2026-01-05
# An already imported workspace is reviewed in place, exactly as before.
peerreview review workspace run-output --mode panel --literature-cutoff 2026-01-05
```

This command performs model inference. It requires a subscription login for the transport it runs on: Claude Code by default, or an active ChatGPT login when a Codex model is named. `--literature-cutoff` is optional and defaults to today. How much subscription usage a run may spend is the operator's own account setting. The engine records every call in `usage.jsonl` for reporting and does not gate on it.

The one-command form above writes the reports itself, into the folder that holds `manuscript/` and `review/`. After any run, `peerreview report run-output` writes `REVIEW.md` and `REVIEW.txt` to the run folder. Both contain the run details, the final review and, for ensemble runs, each independent reviewer. The command reads the saved JSON and makes no model calls. It also works on a blocked or stopped run and reports what was saved.

The first reviewer receives the full extracted text and at most three tool calls. Workspace and panel modes ask it to consider claims, comparators, measurement, interventions, replication, and internal consistency before selecting up to three uncertainties for investigation. The final response includes a short coverage record with exact source quotations. Areas can remain unresolved or be inapplicable. The record replaces the mandatory note-saving tool call. It is model-reported coverage, not proof of a complete review.

Coverage validation checks that every area appears once and that supplied quotations match the manuscript. Invalid entries remain unresolved. The validated record is saved to the run directory and included in workspace and panel JSON exports. Raw responses are retained separately.

The debate receives guarded review suggestions, the coverage record, cited source blocks, and up to three source images inspected by the first reviewer. It has two further evidence lookups. Withheld numerical replacements are passed as author questions. Debate output receives the same guards before entering the editor package. Every input finding has a stable ID. The debate must explicitly retain, merge, reject with reasons, or request verification for each one. Missing, duplicate, or invalid dispositions remain in an unendorsed verification queue. A shared source passage does not establish that two concerns are equivalent. Original suggestions and raw responses remain available, with guarded findings, dispositions, and validation records preserved in the editor package.

`debate_revision.py` provides a versioned component-preserving debate schema, prompt, validator, and editor package. Every original component receives a treatment, and scientific status is separate from editorial priority. Exact quotations and complete accounting are checked without asserting scientific correctness.

## Paper Scaffold checks

The optional adapter genuinely reuses `readability.metrics` and `prose_check.check` from an explicitly selected Paper Scaffold checkout. It reports prose word counts, readability, and source-linked advisory findings. It records the checkout revision, tool hashes, and optional dependency versions. The imported manuscript stays unchanged.

```bash
peerreview checks workspace --scaffold-root /path/to/paper-scaffold --output scaffold-report.json
```

The checks are a separate command. A review run never calls them.

The checks operate on extracted plain text and skip headings, references, and tables. They include captions and supplementary prose. Their word convention differs from the importer totals. Readability and prose warnings are editorial cues, not evidence of scientific defects. Scaffold's Typst authoring rules and journal limits are not imposed on imported manuscripts.

Scaffold's bibliography checker requires BibTeX entries and citation keys, so it is not currently reused for extracted references. The adapter checks conservative citation-ID linkage for XML imports. PDF-only linkage is explicitly unavailable. The investigation tools still support focused Crossref identity checks, which cannot establish whether a citation supports a scientific claim.

## Headless API

`peerreviewagents.engine.api` is the versioned integration boundary, re-exported from `peerreviewagents.engine` itself. It carries `ReviewRequest`, `capabilities`, `load_manuscript`, `evidence_registry`, `validate_review`, `run_review`, `review_request_from_preset`, `SavedDraftSynthesisRequest` and `run_saved_draft_synthesis`, with the presets and resolved configuration in `config.py`. Importing it starts no model provider and reads no manuscript.

Run manifests record a content hash of this package directory under `engine_source`. Runs started under the previous layout cannot be resumed.

## Boundaries

- PDF layout and reading order can be imperfect. Source-page links support inspection, and unresolved anchors are explicit.
- Publisher XML is preferred when available. PDF-only references remain provisional.
- The engine preserves text and references rather than reconstructing a complete editable Typst paper.
- JSON export is implemented. Native Word comments, tracked changes, and PDF annotation writeback are not part of this package.
- Literature search is focused and bounded. It does not establish exhaustive novelty or citation coverage.
- Model output remains provisional. Source anchoring prevents misplaced edits, but it cannot prove a scientific allegation correct.
- The completed pilots behind this engine were development demonstrations on published final papers, not held-out benchmarks or claims of superiority over single agents. The two-paper pilot and the later four-paper panel both found no measured overlap improvement from debate.

## Verification

```bash
pytest tests/engine -q
```

Tests cover Unicode anchors, ambiguous quotes, source tampering, overlap rejection, numerical guards, retained unplaced comments, table extraction, tool limits, restricted arithmetic, distinct concerns on shared passages, malformed debate dispositions, source-image provenance, export history, cache identity, interrupted-call recovery, and usage accounting. They make no model calls.
