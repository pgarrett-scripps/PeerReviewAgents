# PeerReviewAgents

A multi-agent LLM **peer-review** framework — an editorial board for manuscripts,
modeled structurally on [TradingAgents](https://github.com/TauricResearch/TradingAgents).
Where TradingAgents simulates a trading firm to produce a buy/sell/hold decision on a
*ticker*, PeerReviewAgents simulates a journal editorial board to produce an
**accept / minor / major / reject** decision on a *manuscript* — through specialist
review, dialectical debate, synthesis, and a parallel citation audit, with full
reports at every stage.

## Pipeline

```
ingest → [methodology ‖ data-analysis ‖ novelty ‖ clarity ‖ literature
          ‖ rigor ‖ reproducibility ‖ ethics ‖ citations]   (parallel from START)
       → reviewers (8)  → debate (Advocate ⇄ Skeptic, up to max_debate_rounds)
                        → meta-reviewer / Area Chair (draft recommendation)
                        → author rebuttal (concedes / disputes critiques) ──┐
       → citations  ───────────────────────────────────────────────────────┤
                                                                            ↓
                                                       Editor-in-Chief (final decision + letter)
                                                                   ↓
                                                            reports + memory
```

The 8 specialist reviewers and the standalone citation-audit branch all fan out
from `START` in parallel. Reviewer outputs flow through the advocate/skeptic
debate and the meta-reviewer, then through an author-rebuttal pass that pushes
back on misreadings and concedes fixable critiques. The citation audit bypasses
both debate and meta-review and lands directly on the Editor-in-Chief, who
weighs the meta-review, the rebuttal, the citation findings, and the panel's
confidence-weighted score into the final decision letter. The manuscript is
threaded as a `cache_control: ephemeral` prefix through every stage, so every
agent grounds claims in primary text without re-paying input-token cost.

Built on **LangGraph** (orchestration), with a **Textual** TUI and a headless Rich mode.
All LLM calls go through **OpenRouter** — pick any text + vision model pair available
on the platform.

## Agent roster

| Stage | Agents |
|---|---|
| Reviewers (8, parallel from START) | Methodology · Statistics & Data-Analysis · Novelty/Contribution · Clarity/Presentation · Related-Work · Rigor & Overclaiming · Reproducibility · Ethics & Compliance |
| Citation audit (parallel from START, feeds editor) | Citation Verification Auditor |
| Debate | Advocate vs Skeptic (N rounds) |
| Synthesis | Area Chair / Meta-reviewer |
| Author rebuttal | Plays the manuscript author defending against the panel |
| Final | Editor-in-Chief |

Novelty and Literature reviewers consult a **live research layer**: arXiv and
Semantic Scholar for structured paper lookups, plus OpenRouter's server-side
web search (billed per call at `$0.005` each) for ad-hoc claim verification.
Each tool degrades gracefully if offline.

## Install

```bash
pip install -e .

# All optional extras (PDF ingest + research tools):
pip install -e '.[pdf-ingest,research]'
```

## API keys

Set them in your shell or a `.env` file at the repo root:

```bash
# Required: OpenRouter powers every text + vision model call and the
# server-side web search the reviewers use for ad-hoc claim verification.
export OPENROUTER_API_KEY=...

# Required to ingest PDFs (https://www.datalab.to/ — $5 free credits):
export DATALAB_API_KEY=...
```

## Usage

```bash
# Textual TUI
peerreview path/to/manuscript.pdf

# Headless run with live progress
peerreview path/to/manuscript.pdf --no-tui

# Browser-based "game room" — upload via the web UI and watch sprite agents work
peerreview serve                              # http://127.0.0.1:8765
peerreview serve --host 0.0.0.0 --port 8080   # bind to all interfaces

# Override the models or debate length for a single run
peerreview paper.pdf --no-tui \
  --reasoning-model anthropic/claude-opus-4.1 \
  --vision-model openai/gpt-4o-mini \
  --debate-rounds 3
```

### Web UI

`peerreview serve` boots a FastAPI app that lets you upload a manuscript through
the browser and watch the pipeline run as a 2D sprite room: one desk per
reviewer, a debate stage for Advocate vs Skeptic, and the editorial office for
synthesis and the final verdict. Sprites switch into a "working" state when
their agent's node fires; clicking one opens a side panel that streams the live
token output and, once the agent finishes, switches to the rendered markdown of
its completed report. The MVP runs **one job at a time**, in-process, with no
auth — host it behind a reverse proxy if you put it on a public network.

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

See `peerreviewagents/default_config.py`. The whole knob list:
`reasoning_model`, `vision_model`, `max_debate_rounds`, `vision_max_figures`,
`output_dir`, plus a few `pdf_*` knobs for fine-tuning ingest. The reviewer
panel and the rest of the pipeline are hardwired.

## Output

Each run writes to `reports/<timestamp>-<slug>/`: one file per reviewer and integrity
auditor, `debate_transcript.md`, `meta_review.md`, `decision_letter.md`, and a
one-page `summary.md` with the verdict badge.

## Tests

```bash
python -m pytest tests/ -q   # runs the full pipeline with a fake LLM, no keys needed
```

## Disclaimer

A research tool to assist human peer review — not a replacement for it. Decisions and
generated text should always be checked by a human editor.
