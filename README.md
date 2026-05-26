# PeerReviewAgents

A multi-agent LLM **peer-review** framework — an editorial board for manuscripts,
modeled structurally on [TradingAgents](https://github.com/TauricResearch/TradingAgents).
Where TradingAgents simulates a trading firm to produce a buy/sell/hold decision on a
*ticker*, PeerReviewAgents simulates a journal editorial board to produce an
**accept / minor / major / reject** decision on a *manuscript* — through specialist
review, dialectical debate, synthesis, and integrity checks, with full reports at
every stage.

## Pipeline

```
ingest → [methodology ‖ data-analysis ‖ novelty ‖ clarity ‖ literature]   (parallel reviewers)
       → debate loop (Advocate ⇄ Skeptic, up to max_debate_rounds)
       → meta-reviewer / Area Chair (draft recommendation)
       → integrity panel (rigor ‖ reproducibility ‖ ethics)
       → Editor-in-Chief (final decision + author-facing letter)
       → reports + memory
```

Built on **LangGraph** (orchestration), with a **Textual** TUI and a headless Rich mode.

## Agent roster

| Stage | Agents |
|---|---|
| Reviewers (parallel) | Methodology · Statistics & Data-Analysis · Novelty/Contribution · Clarity/Presentation · Related-Work/Citations |
| Debate | Advocate vs Skeptic (N rounds) |
| Synthesis | Area Chair / Meta-reviewer |
| Integrity panel (parallel) | Rigor & Overclaiming · Reproducibility · Ethics & Compliance |
| Final | Editor-in-Chief |

Novelty and Literature reviewers can use a **live research layer** (arXiv, Semantic
Scholar, web) to check prior art and verify citations. Each tool degrades gracefully
if offline.

## Install

```bash
pip install -e .
# optional extras:  pip install -e ".[research,google,ollama,pdf]"
```

## API keys

Set the key for your provider (a `.env` file is supported):

```bash
export ANTHROPIC_API_KEY=...     # or OPENAI_API_KEY, GOOGLE_API_KEY, ...
```

## Usage

```bash
# Textual TUI
peerreview path/to/manuscript.pdf

# Headless run with live progress
peerreview path/to/manuscript.pdf --no-tui

# Choose provider / models / reviewers
peerreview paper.md --no-tui \
  --provider anthropic \
  --deep-model claude-opus-4-7 \
  --quick-model claude-haiku-4-5-20251001 \
  --reviewers methodology,data_analysis,novelty \
  --debate-rounds 3 --pdf
```

As a library:

```python
from peerreviewagents.graph.review_graph import PeerReviewGraph
from peerreviewagents.default_config import get_config
from peerreviewagents.reports import write_reports

graph = PeerReviewGraph(get_config(max_debate_rounds=3))
state = graph.review("paper.pdf")
print(state["decision"])
write_reports(state)
```

## Configuration

See `peerreviewagents/default_config.py`. Key knobs: `provider`, `deep_think_llm` /
`quick_think_llm` (a deep model for synthesis/judgement, a quick model for the
parallel reviewer pass), `reviewer_set`, `max_debate_rounds`, `research_enabled`,
`emit_pdf`, `emit_verdict`, and `checkpoint` (SQLite crash-resume).

## Output

Each run writes to `reports/<timestamp>-<slug>/`: one file per reviewer and integrity
auditor, `debate_transcript.md`, `meta_review.md`, `decision_letter.md`, and a
one-page `summary.md` with the verdict badge (optionally a combined PDF).

## Tests

```bash
python -m pytest tests/ -q   # runs the full pipeline with a fake LLM, no keys needed
```

## Disclaimer

A research tool to assist human peer review — not a replacement for it. Decisions and
generated text should always be checked by a human editor.
