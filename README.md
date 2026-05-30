# PeerReviewAgents

A multi-agent LLM **peer-review** framework — an editorial board for manuscripts,
modeled structurally on [TradingAgents](https://github.com/TauricResearch/TradingAgents).
Where TradingAgents simulates a trading firm to produce a buy/sell/hold decision on a
*ticker*, PeerReviewAgents simulates a journal editorial board to produce an
**accept / minor / major / reject** decision on a *manuscript* — through specialist
review, dialectical debate, synthesis, an author rebuttal, an editorial verdict,
and tiered venue suggestions, with full reports at every stage.

## Pipeline

```
                                ┌─ methodology
                                ├─ data analysis
                                ├─ novelty
   ingest                       ├─ clarity        (8 specialists, parallel from START)
   (pypdf,  ─→ reviewer fan-out ┤  literature
   local)                       ├─ rigor
                                ├─ reproducibility
                                └─ ethics
                                       │
                                       ▼
      advocate ⇄ skeptic  ◀──── debate loop  (up to max_debate_rounds)
                                       │
                                       ▼
                                meta-reviewer   (Area Chair: draft recommendation)
                                       │
                                       ▼
                              author rebuttal   (concessions / disagreements / load-bearing)
                                       │
                                       ▼
                              Editor-in-Chief   (final decision + letter)
                                       │
                                       ▼
                          Journal Scout         (tiered venue suggestions)
                                       │
                                       ▼
                              reports + memory log
```

Built on **LangGraph** (orchestration), with a **Textual** TUI, a headless Rich CLI,
and a browser-based 2D "room" UI. Every agent emits a typed
[pydantic schema](peerreviewagents/agents/schemas.py) via the provider's
structured-output mode — no YAML-frontmatter parsing, no string-matching for
verdicts. The manuscript is threaded as a `cache_control: ephemeral` prefix
through every stage that supports it, so all 13 LLM-calling nodes share one
provider-side cache entry.

PDF ingest is fully local via `pypdf` — no external API key needed.

## Providers

Three are wired up. Pick one with `--provider` or the `provider` TOML key.

| Provider | Default | API key | Model id format |
|---|---|---|---|
| `openrouter` | ✅ | `OPENROUTER_API_KEY` | slug, e.g. `anthropic/claude-opus-4.1` |
| `anthropic`  |    | `ANTHROPIC_API_KEY`  | model id, e.g. `claude-opus-4-1-20250805` |
| `openai`     |    | `OPENAI_API_KEY`     | model id, e.g. `gpt-4.1`, `o3` |

Provider abstraction lives in [`peerreviewagents/runtime/providers.py`](peerreviewagents/runtime/providers.py).
Each provider declares its preferred structured-output method and whether it
honors `cache_control: ephemeral` markers; the agent layer reads these flags
rather than branching on the provider name directly.

## Agent roster

14 agents, all emitting structured outputs:

| Stage | Agent | Schema |
|---|---|---|
| Reviewers (×8, parallel from START) | Methodology · Data Analysis · Novelty · Clarity · Literature · Rigor · Reproducibility · Ethics | `ReviewerOutput` |
| Debate | Advocate, Skeptic (N rounds) | `DebateOutput` |
| Synthesis | Area Chair / Meta-reviewer | `MetaReviewOutput` |
| Author rebuttal | Plays the manuscript author | `AuthorRebuttalOutput` |
| Final | Editor-in-Chief | `EditorDecisionOutput` |
| Post-decision | Journal Scout (venue suggestions) | `JournalRecommendationsOutput` |

The **Novelty** and **Literature** reviewers can call into a live research layer
([`peerreviewagents/research/`](peerreviewagents/research/)) backed by **arXiv**,
**Semantic Scholar**, **PubMed** (NCBI E-utilities), and **bioRxiv/medRxiv**
(via EuropePMC). Each reviewer declares the logical operations it wants
(`find_related_work`, `search_biomedical_literature`, `search_preprints`); a
vendor-routing dispatcher picks the configured vendor per category and falls
through to the next on rate-limit. The routing pattern mirrors TradingAgents'
`dataflows/interface.py`.

## Install

```bash
pip install -e .

# Optional extra (live arXiv lookups for the Novelty / Literature reviewers):
pip install -e '.[research]'
```

Base deps include `pypdf` (PDF ingest), `langchain-openai`, `langchain-anthropic`,
`rank-bm25` (memory retrieval). No system dependencies; no `Pillow`; no OCR; no
external paid services beyond your chosen LLM provider.

## API keys

Set one of the following in your shell or a `.env` file at the repo root,
matching your `--provider` choice:

```bash
# Default — OpenRouter, single key for any model on the platform
export OPENROUTER_API_KEY=...

# --provider anthropic
export ANTHROPIC_API_KEY=...

# --provider openai
export OPENAI_API_KEY=...
```

PDF ingest needs no API key. Image-only / scanned PDFs aren't supported — convert
them to text or Markdown first.

## Usage

```bash
# Textual TUI
peerreview path/to/manuscript.pdf

# Headless run with live progress
peerreview path/to/manuscript.pdf --no-tui

# Override the provider / model / debate length for a single run
peerreview paper.pdf --no-tui \
  --provider anthropic \
  --reasoning-model claude-opus-4-1-20250805 \
  --debate-rounds 3

# Browser-based "room" UI — upload + watch agents work
peerreview serve                              # http://127.0.0.1:8765
peerreview serve --host 0.0.0.0 --port 8080   # bind to all interfaces

# Record the real-world outcome of a past review for cross-run reflection
peerreview outcome <job-id> {accepted|rejected|minor|major|withdrawn}
```

### Web UI

`peerreview serve` boots a FastAPI app that lets you upload a manuscript through
the browser and watch the pipeline run as a 2D sprite room: one desk per
reviewer, a debate stage for Advocate vs Skeptic, the editorial office for
synthesis, and a Journal Scout desk for the venue recommendations. Sprites
switch into a "working" state when their node fires; clicking one opens a side
panel that shows a live progress card (token + cost counters + heartbeat) while
the agent is running, then renders the agent's report when it finishes. When
the pipeline completes, the topbar shows a **View summary** button — click it
to open a completion card with the decision badge, stats, and report-file
links. The MVP runs **one job at a time**, in-process, with no auth — host it
behind a reverse proxy if you put it on a public network.

### As a library

```python
from peerreviewagents.graph.review_graph import PeerReviewGraph
from peerreviewagents.default_config import get_config
from peerreviewagents.reports import write_reports

graph = PeerReviewGraph(get_config(max_debate_rounds=3))
state = graph.review("paper.pdf")
print(state["decision"])
print(state["journal_recommendations"])
write_reports(state)
```

## Cross-run memory

Each completed review writes a `status: pending` entry to a markdown memory log
([`storage/memory.py`](peerreviewagents/storage/memory.py)) containing the
manuscript title + abstract + per-reviewer scores + the editor's decision.

When you know the real-world outcome:

```bash
peerreview outcome <job-id> accepted
```

This flips the entry to `status: resolved`, calls the LLM with a
[`MemoryReflection`](peerreviewagents/agents/schemas.py) schema for a
2–4-sentence lesson, and patches the entry in place. On subsequent runs, the
meta-reviewer fetches the top-K most topically-similar resolved lessons (BM25
ranked over title + abstract) and injects them into its prompt as **prior
calibration**.

Log lives at `~/.peerreviewagents/memory/review_memory.md` by default; override
with `memory_path` in TOML or `PEERREVIEW_MEMORY_PATH` in the environment.

## Configuration

See [`peerreviewagents/default_config.py`](peerreviewagents/default_config.py).
TOML, environment vars, and CLI flags all layer on top of the built-in defaults
(precedence: defaults → user TOML → project TOML → `--config` → env → flags).

The full knob list: `provider`, `reasoning_model`, `max_debate_rounds`,
`manuscript_char_budget`, `output_dir`, `cache_dir`, `memory_path`, `memory_k`,
`data_vendors`, `tool_vendors`. See `peerreview.toml.example` for an annotated
template.

## Output

Each run writes to `reports/<timestamp>-<slug>/`:

- `review_<reviewer>.md` × 8 — per-specialist reports
- `debate_transcript.md` — full advocate/skeptic transcript
- `meta_review.md` — Area Chair synthesis
- `author_rebuttal.md` — author's defense
- `decision_letter.md` — Editor-in-Chief verdict + required revisions
- `journal_recommendations.md` — tiered venue suggestions (as-is / after-revision / alternative)
- `summary.md` — one-page roll-up with the verdict badge + per-reviewer scores + cost

## Tests

```bash
just test                    # uv run pytest tests/ -q
pytest tests/ -q             # runs the full pipeline with a fake LLM, no API keys needed
```

The test suite covers ingest, structured-output round-trip + retry fallback,
provider factories, research-vendor routing with rate-limit fallback, the memory
log lifecycle, and end-to-end web pipeline (uploading → running → reading
finished bodies via the REST endpoints).

## Architecture notes

- **`runtime/providers.py`** — provider factory + capabilities table; each
  provider declares its `structured_method` and `supports_cache_control`.
- **`agents/schemas.py`** — every agent's typed output, with a `to_markdown()`
  renderer so structured fields stay the source of truth.
- **`agents/utils/structured.py`** — `invoke_structured` (one-shot) and
  `invoke_structured_after_tools` (free-text stream → structured extract) wrap
  `llm.with_structured_output` with a single retry on validation failure.
- **`research/interface.py`** — category-level `data_vendors` map + per-method
  `tool_vendors` override; rate-limit triggers fall-through, other errors
  propagate.
- **`storage/memory.py`** — append-only markdown log with HTML-comment record
  separators; pending entries are patched in place when resolved.

## Disclaimer

A research tool to assist human peer review — not a replacement for it.
Decisions and generated text should always be checked by a human editor.
