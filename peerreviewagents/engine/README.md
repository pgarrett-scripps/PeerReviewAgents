# The manuscript workspace

`peerreviewagents.engine` is what the review works *on*, not what runs it. In
0.9 the review itself is the `pra` Claude Code plugin and its MCP server
(`peerreviewagents.plugin`); this subpackage holds the manuscript, the tools the
server exposes to the agents, and the report writer.

Everything that used to orchestrate a review from Python — `ensemble`,
`reviewer`, `model_runtime`, the debate modules, `api.run_review`,
`ReviewRequest`, the six modes and three policies, `python -m
peerreviewagents.engine review` and the Codex provider — was deleted in 0.9, not
deprecated. The 0.8 engine is frozen at the tag `v0.8.0`; the LangGraph panel
that preceded it is at `legacy-langgraph-v0.6.1`. There is no shim.

## What is here

| Module | What it does |
| --- | --- |
| `ingest.py` | imports a PDF (and optional JATS XML and supplements) into a manuscript workspace of numbered source blocks |
| `storage.py`, `file_lock.py` | reads and writes the workspace and run folders, one writer at a time |
| `source_adapters.py`, `source_adapters_extra.py`, `source_payload.py` | turn stored blocks into what an agent is allowed to see |
| `supplement_view.py`, `targeted_sources.py` | supplementary tables and page/figure images, fetched by handle |
| `calculator.py` | the restricted arithmetic behind the `calculate` tool, which records a receipt for every evaluation |
| `external_evidence.py` | bounded Crossref and Europe PMC lookups under a publication cutoff |
| `annotations.py`, `result_map.py` | anchoring a finding to a block, and the quote/anchor guards |
| `domain_registry.py`, `domains/` | 21 field checklists the `field` specialist is given |
| `investigation.py` | the tool implementations the plugin server wraps |
| `usage.py` | usage and runtime accounting (`observed_usage`, `runtime_usage`, `agent_rows`) |
| `run_report.py` | renders `REVIEW.md` and `REVIEW.txt` from a finished or blocked run |

No module here starts a model, reads a key, or launches a session. Importing
the package imports no transport.

## Import and report, without a model

```bash
peerreview review manuscript.pdf run-output   # the review; spends subscription usage
peerreview report run-output                  # re-render the reports; no model calls
```

The two commands are the whole CLI. `review` imports the PDF into
`run-output/manuscript` itself, so there is no separate import step; `report`
reads the saved JSON and works on a blocked or stopped run.

From Python:

```python
from peerreviewagents import import_paper, write_reports

workspace = import_paper("manuscript.pdf", "workspace")
paths = write_reports("run-output")        # {'markdown': ..., 'text': ...}
```

Sources stay local. A review sends manuscript text and the page images an agent
asks for to the authenticated Claude Code subscription, and sends focused
literature queries to Crossref or Europe PMC. There is no API key path.

## What the server adds on top

The guarantees are enforced by the server, not by this package and not by a
prompt:

- An agent reaches these tools only through the plugin server, which assigns
  each slot a per-run token, a tool allow list and a call budget. A
  `manuscript-only` reviewer has the image and network tools removed from its
  allow list, so asking for a page image is refused rather than discouraged.
- A finding is anchored to a block with an exact quote or it is quarantined.
- `basis` is assigned by the server from what the agent actually did —
  `computed` from a `calculate` receipt whose inputs appear in the blocks the
  finding cites, `two-quotes` from two anchored quotations, `judgement`
  otherwise. An agent cannot assert it.
- Every tool call, receipt and external lookup is written to the run's ledger
  with the identity that made it. An agent's own account of what it did is
  never taken on trust.

`<run>/review/protocol.json` records what a run was allowed to do; the ledger
records what it did.

## Boundaries

- PDF layout and reading order can be imperfect. Page links support inspection,
  and unresolved anchors stay explicit.
- Publisher XML is preferred when available. PDF-only references are
  provisional.
- The workspace preserves text and references; it does not reconstruct an
  editable paper, and there is no Word or PDF writeback.
- Literature search is focused and bounded. It does not establish novelty or
  citation coverage, and an abstract is not full-text support.
- Anchoring prevents a misplaced edit. It cannot prove a scientific allegation
  correct.
- The pilots behind this work were development demonstrations on published
  papers, not held-out benchmarks. [`docs/study-arms.md`](../../docs/study-arms.md)
  gives the exact command for each ablation arm.

## Verification

```bash
pytest tests/engine -q      # this subpackage
pytest -q                   # everything, including the plugin server and starter
```

The suite makes no model calls and needs no network: the starter and server
tests drive a stubbed `claude` binary. Coverage includes Unicode anchors,
ambiguous quotes, source tampering, numerical guards, table extraction,
restricted arithmetic, tool allow lists per track, basis assignment, token
ceilings, rate-limit handling and the report header.
