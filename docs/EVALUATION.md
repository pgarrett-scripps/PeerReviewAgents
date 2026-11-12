# OpenReview comparison protocol

> **Historical protocol.** This document describes the frozen retrospective
> evaluation run on PeerReviewAgents 0.4.0, whose full workflow used the
> original eight-role reviewer panel and a sequential single-round debate.
> The 0.5.0 pipeline is different — five referees, parallel debate rounds, a
> post-debate synthesizer — and is not what these numbers measured. The
> protocol is preserved unchanged so the published results stay auditable.
>
> **The harness is gone.** `peerreviewagents.eval`, the `just eval-*` recipes
> and the corpus builder ran this protocol against the LangGraph panel, and
> were deleted with the panel in 0.8.0. To re-run it, check out the tag
> `legacy-langgraph-v0.6.1` (or the `v0.7.0` release). The current study design
> is [`study-arms.md`](study-arms.md), which gives the exact command for each
> arm; since 0.9 an arm is a workflow — which agents run — and the review
> itself runs as the `pra` Claude Code plugin, so neither the harness nor the
> conditions below can be reproduced on a current checkout.

This protocol characterizes PeerReviewAgents against human OpenReview outcomes
and a one-call practical baseline. It is a software validation study, not a
claim that generated critiques are correct or human-equivalent. The baseline is
not compute-matched: report performance, cost, and latency together.

## Design

- Use one fully decided OpenReview conference and pin its actual rating field.
- Fetch 20 papers, balanced 10 accepted and 10 rejected.
- Run the full workflow and single-LLM baseline once on every paper.
- Run the full workflow three times total on four papers selected before results.
- Primary endpoint: Spearman correlation with mean human rating.
- Secondary endpoints: balanced decision accuracy, Cohen's kappa, completion,
  saved-artifact integrity, recovery warnings, cost, latency, and repeat-run
  stability. A run fails if any of the eight reviews or two audits is missing,
  blank/truncated, duplicated, or contains an internal prompt boundary.
- Use one provider/model for every role and disable research tools to reduce
  leakage. Record the remaining training-data leakage risk in the manifest.
- Do not tune strictness, thresholds, prompts, or sampling after inspecting the
  comparison results.

The decision endpoint collapses `accept`/`minor` to accept-leaning and
`major`/`reject` to reject-leaning. This is declared before running and is not a
claim that journal revision verdicts equal conference accept/reject decisions.

## How it was run

The steps below are the protocol as executed, not commands to type: the
`peerreviewagents.eval` module they were typed into no longer exists.

1. **Inspect the venue.** The venue and its actual rating field were read from
   OpenReview rather than guessed from a schema.
2. **Fetch a balanced corpus** of 20 papers, 10 accepted and 10 rejected, with
   the PDFs, `corpus.jsonl` and a `corpus_manifest.json` of SHA-256 hashes. The
   model was chosen so its documented training snapshot predated the venue
   decisions where possible, and the residual leakage risk was recorded in the
   manifest.
3. **Freeze the plan** before any model run: the endpoints, and the four papers
   deterministically selected for the three-run repeatability subset, were
   committed as `protocol.json`. The runner refused any configuration whose
   digest differed from it.
4. **Run both conditions** on identical flags — one model for every role,
   literature search disabled, the post-decision journal recommender omitted
   because it could not affect an endpoint. The runner verified the corpus
   hashes before every batch and was resumable per `(paper_id, repeat)`.
5. **Analyze** each condition's runs, then compare them, with deterministic
   paper-level bootstrap intervals.

PDF redistribution follows the source venue's terms; hashes and source URLs can
be archived even where the PDFs cannot. The raw JSONL, manifests, protocol,
reports and figure source are the publication artifact.

A significant or favorable difference does not isolate the effect of agentic
structure, because the conditions use different amounts of computation.
