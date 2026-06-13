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

Reviews can be **venue-specific**: point the run at a target journal and its
scope, standards, and submission limits are threaded into the reviewer,
meta-reviewer, editor, and Journal Scout prompts. See
[Target journal](#target-journal).

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

# Review against a specific journal (see --list-journals for slugs)
peerreview paper.pdf --no-tui --journal nature-methods
peerreview --list-journals

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
behind a reverse proxy if you put it on a public network. The upload form
includes a **target-journal** dropdown (populated from `GET /journals`) so you
can pick the venue per submission.

### Target journal

Profiles in [`peerreviewagents/journals/`](peerreviewagents/journals/) — one `.toml` per venue — describe a
journal's scope, audience, impact factor, submission limits, and author/reviewer
guidelines. Selecting one injects that context into the reviewers,
meta-reviewer, editor, and Journal Scout, so the panel judges the manuscript
against the standards of the venue it's actually headed for, and records the
chosen venue in `summary.md`.

```bash
peerreview --list-journals                       # available slugs
peerreview paper.pdf --journal bioinformatics    # review against a venue
peerreview paper.pdf --journal ""                # fully venue-agnostic, no framing
```

Select a venue with `--journal <slug>`, the `target_journal` TOML key,
`PEERREVIEW_TARGET_JOURNAL`, or the web dropdown. The default is **`general`** —
a stand-in profile with sound, field-general standards, ideal when the intended
journal isn't one of the bundled profiles. ~30 journals ship across the natural
sciences, bioinformatics, chemistry, ML, and medicine; add your own by copying
[`_template.toml`](peerreviewagents/journals/_template.toml) into a directory of
your own and pointing `journals_dir` / `PEERREVIEW_JOURNALS_DIR` at it. A
`--journal` slug that doesn't resolve is rejected at startup with the list of
valid slugs. See [`peerreviewagents/journals/README.md`](peerreviewagents/journals/README.md)
for the schema and details.

### Review strictness

A 1–5 dial controls how easy or harsh the panel is. The level renders to a
directive that's injected into the **reviewer**, **meta-reviewer**, and
**editor** prompts (sharing the same provider-side cache entry as the
journal/manuscript context), so it changes how the manuscript is *judged*
without touching the author rebuttal or the venue recommendations.

| Level | Meaning |
|---|---|
| 1 | Very lenient — reward the contribution; only fundamental flaws block |
| 2 | Lenient |
| **3** | **Balanced (default)** — no directive injected; behaves as before |
| 4 | Strict — top-venue bar; unaddressed weaknesses are blocking |
| 5 | Very strict — exacting bar; default to rejection on doubt |

```bash
peerreview paper.pdf --no-tui --strictness 5     # harsh review
peerreview paper.pdf --no-tui --strictness 1     # gentle review
```

Set it with `--strictness <1-5>`, the `review_strictness` (or `strictness`)
TOML key, `PEERREVIEW_STRICTNESS`, or the web form's slider. The chosen level
is recorded in `summary.md`.

### Article type

Tell the panel what *kind* of submission it's reviewing. The taxonomy is
venue-general — `article`, `letter`, `communication`, `perspective`, `review`,
`technical-note`, `tutorial` — and naming it injects a manuscript-type block
into the reviewer/meta-reviewer/editor prompts so the work is judged
appropriately (a Letter or Review isn't held to a research Article's bar for
novel data). Any per-type word limits come from the **target journal's**
profile, which may declare them per type (e.g. Journal of Proteome Research).

```bash
peerreview paper.pdf --no-tui --journal journal-of-proteome-research --article-type review
peerreview --list-article-types                  # available type keys
```

Set it with `--article-type <key>`, the `article_type` TOML key,
`PEERREVIEW_ARTICLE_TYPE`, or the web form. Default is unset (no manuscript-type
framing); the chosen type is recorded in `summary.md`.

### Desk screen (optional triage gate)

Real editorial flows screen submissions *before* assigning reviewers. Enabling
the desk screen adds a triage node that runs once, ahead of the panel, and can
**desk-reject** a manuscript (out of scope, incomplete, fatal flaw, or clearly
below the venue's bar) — short-circuiting the run to a reject without spending
the 8-reviewer panel, the debate, or the editor. It screens against the target
journal and the current strictness, and is **fail-open** (any error proceeds to
the full review). Off by default, so a normal run is unchanged.

```bash
peerreview paper.pdf --no-tui --desk-screen --journal nature --strictness 5
```

Enable it with `--desk-screen`, the `desk_screen` TOML key,
`PEERREVIEW_DESK_SCREEN`, or the web form's checkbox. A desk reject writes
`desk_screen.md` + a `decision_letter.md`, and `summary.md` records the outcome.

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
`manuscript_char_budget`, `target_journal`, `journals_dir`, `article_type`,
`review_strictness`, `desk_screen`, `output_dir`, `cache_dir`, `memory_path`, `memory_k`,
`data_vendors`, `tool_vendors`. See
`peerreview.toml.example` for an annotated template.

## Output

Each run writes to `reports/<timestamp>-<slug>/`:

- `review_<reviewer>.md` × 8 — per-specialist reports
- `debate_transcript.md` — full advocate/skeptic transcript
- `meta_review.md` — Area Chair synthesis
- `author_rebuttal.md` — author's defense
- `decision_letter.md` — Editor-in-Chief verdict + required revisions
- `journal_recommendations.md` — tiered venue suggestions (as-is / after-revision / alternative)
- `summary.md` — one-page roll-up with the verdict badge + target venue + per-reviewer scores + cost

## Tests

```bash
just test                    # uv run pytest tests/ -q
pytest tests/ -q             # runs the full pipeline with a fake LLM, no API keys needed
```

The test suite covers ingest, structured-output round-trip + retry fallback,
provider factories, research-vendor routing with rate-limit fallback, journal
profile loading + context-block injection, the memory log lifecycle, and
end-to-end web pipeline (uploading → running → reading finished bodies via the
REST endpoints).

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

## Paper

LaTeX sources for the arXiv preprint live in [`paper/`](paper/). See
[`paper/README.md`](paper/README.md) for the build instructions.

## Disclaimer

A research tool to assist human peer review — not a replacement for it.
Decisions and generated text should always be checked by a human editor.
