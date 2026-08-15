# OpenReview comparison protocol

This protocol characterizes PeerReviewAgents against human OpenReview outcomes
and a one-call practical baseline. It is a software validation study, not a
claim that generated critiques are correct or human-equivalent. The baseline is
not compute-matched: report performance, cost, and latency together.

## Design

- Use one fully decided OpenReview conference and pin its actual rating field.
- Fetch 30 papers, balanced 15 accepted and 15 rejected.
- Run the full workflow and single-LLM baseline once on every paper.
- Run the full workflow three times total on six papers selected before results.
- Primary endpoint: Spearman correlation with mean human rating.
- Secondary endpoints: balanced decision accuracy, Cohen's kappa, completion,
  cost, latency, and repeat-run stability.
- Use one provider/model for every role and disable research tools to reduce
  leakage. Record the remaining training-data leakage risk in the manifest.
- Do not tune strictness, thresholds, prompts, or sampling after inspecting the
  comparison results.

The decision endpoint collapses `accept`/`minor` to accept-leaning and
`major`/`reject` to reject-leaning. This is declared before running and is not a
claim that journal revision verdicts equal conference accept/reject decisions.

## Install and inspect the venue

```bash
pip install -e '.[eval,dev]'
python -m peerreviewagents.eval inspect --venue ICLR.cc/2026/Conference
```

Confirm the venue and rating field from `inspect`; do not rely on a guessed
schema. Then fetch a balanced corpus. Choose a model whose documented training
snapshot predates the venue decisions when possible.

```bash
python -m peerreviewagents.eval fetch \
  --venue ICLR.cc/2026/Conference \
  --rating-field rating \
  --limit 30 \
  --out data/eval/iclr-2026 \
  --leakage-note 'Model snapshot and conference-decision timing assessed before running.'
```

`fetch` writes `corpus.jsonl`, downloaded PDFs, and `corpus_manifest.json` with
SHA-256 hashes. For an older corpus, create the manifest explicitly:

```bash
python -m peerreviewagents.eval freeze --dir data/eval/iclr-2026 \
  --leakage-note 'Retrospective corpus; training-data leakage remains possible.'
```

Freeze endpoints and deterministically select the repeatability subset:

```bash
python -m peerreviewagents.eval plan --dir data/eval/iclr-2026 \
  --repeat-papers 6 --repeats 3 --seed 20260815
```

Commit `corpus.jsonl`, `corpus_manifest.json`, and `protocol.json` before model
runs. PDF redistribution must follow the source venue's terms; hashes and source
URLs can be archived even when PDFs cannot be redistributed.

## Run both conditions

Use exactly the same flags for both conditions. `--single-model` clears role
model-routing tables, `--offline` disables literature search, and evaluation
runs automatically omit the post-decision journal recommender because it cannot
affect an endpoint.

```bash
python -m peerreviewagents.eval run --dir data/eval/iclr-2026 \
  --mode system --runs-out data/eval/iclr-2026/runs_system.jsonl \
  --provider openrouter --model VENDOR/MODEL --single-model --offline \
  --journal ml-conference --article-type conference-paper --strictness 3

python -m peerreviewagents.eval run --dir data/eval/iclr-2026 \
  --mode single-llm --runs-out data/eval/iclr-2026/runs_single_llm.jsonl \
  --provider openrouter --model VENDOR/MODEL --single-model --offline \
  --journal ml-conference --article-type conference-paper --strictness 3
```

For repeatability, rerun only the six IDs printed by `plan`, increasing the full
workflow file to three runs per selected paper:

```bash
python -m peerreviewagents.eval run --dir data/eval/iclr-2026 \
  --mode system --runs-out data/eval/iclr-2026/runs_system.jsonl \
  --repeats 3 --only ID1,ID2,ID3,ID4,ID5,ID6 \
  --provider openrouter --model VENDOR/MODEL --single-model --offline \
  --journal ml-conference --article-type conference-paper --strictness 3
```

The runner is resumable. It verifies corpus hashes before every batch and skips
successful `(paper_id, repeat)` pairs already on disk.

## Analyze

```bash
python -m peerreviewagents.eval metrics --dir data/eval/iclr-2026 \
  --runs data/eval/iclr-2026/runs_system.jsonl \
  --out data/eval/iclr-2026/report_system

python -m peerreviewagents.eval metrics --dir data/eval/iclr-2026 \
  --runs data/eval/iclr-2026/runs_single_llm.jsonl \
  --out data/eval/iclr-2026/report_single_llm

python -m peerreviewagents.eval compare --dir data/eval/iclr-2026 \
  --system-runs data/eval/iclr-2026/runs_system.jsonl \
  --baseline-runs data/eval/iclr-2026/runs_single_llm.jsonl \
  --out data/eval/iclr-2026/comparison

python -m peerreviewagents.eval figure --dir data/eval/iclr-2026 \
  --runs data/eval/iclr-2026/runs_system.jsonl \
  --out data/eval/iclr-2026/evaluation
```

Reports include deterministic paper-level bootstrap intervals. Preserve the raw
JSONL, manifests, protocol, reports, and figure source as the publication
artifact. A significant or favorable difference does not isolate the effect of
agentic structure because the conditions use different amounts of computation.
