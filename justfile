# PeerReviewAgents — task runner
# Run `just` or `just --list` to see all commands.

set shell := ["bash", "-cu"]

# Default: show available recipes
default:
    @just --list

# --- Install ----------------------------------------------------------------

# Create the uv-managed venv and install the project (editable)
install:
    [ -d .venv ] || uv venv
    uv pip install -e .

# Install with every optional extra
install-all:
    [ -d .venv ] || uv venv
    uv pip install -e ".[research]"

# Install with a chosen extras group: `just install-extras research`
install-extras extras:
    [ -d .venv ] || uv venv
    uv pip install -e ".[{{extras}}]"

# Install dev/test tooling alongside the project
install-dev:
    [ -d .venv ] || uv venv
    uv pip install -e ".[research]"
    uv pip install pytest ruff

# Sync the venv to match pyproject.toml exactly (drops anything stale)
sync:
    uv pip install -e . --reinstall

# Wipe the local virtualenv
clean-venv:
    rm -rf .venv

# --- Run --------------------------------------------------------------------

# Launch the Textual TUI on a manuscript (pass extra peerreview flags after the path)
tui path *args:
    uv run peerreview {{path}} {{args}}

# Headless run with live progress
run path *args:
    uv run peerreview {{path}} --no-tui {{args}}

# Run the sample manuscript headless (smoke test of the CLI)
run-sample *args:
    uv run peerreview tests/sample_manuscript.md --no-tui {{args}}

# Launch the FastAPI web UI (game-like agent room + upload form)
serve *args:
    uv run peerreview serve {{args}}

# Smoke-test local PDF ingestion (pypdf, no LLM calls, no API keys).
# Writes the extracted text to reports/_ingest_test.md and prints a summary.
# Usage:  just test-ingest ~/Downloads/paper.pdf
test-ingest path:
    #!/usr/bin/env bash
    set -euo pipefail
    mkdir -p reports
    uv run python - "{{path}}" <<'PYEOF'
    import re
    import sys
    import time
    from pathlib import Path
    from peerreviewagents.ingest.loader import load_manuscript

    pdf = sys.argv[1]
    print(f"ingesting: {pdf}")
    t0 = time.time()
    title, md, sections = load_manuscript(pdf)
    elapsed = time.time() - t0

    out = Path("reports/_ingest_test.md")
    out.write_text(md)
    headings = len(re.findall(r"(?m)^#+ "  , md))

    print(f"elapsed:    {elapsed:.2f}s")
    print(f"title:      {title[:80]}")
    print(f"chars:      {len(md)}")
    print(f"headings:   {headings}")
    print(f"sections:   {sorted(sections)}")
    print(f"wrote:      {out}")
    PYEOF

# --- Quality ----------------------------------------------------------------

# Run the test suite (uses a fake LLM — no API keys needed)
test:
    uv run pytest tests/ -q

# Run tests with verbose output
test-v:
    uv run pytest tests/ -vv

# Lint with ruff
lint:
    uv run ruff check .

# Auto-fix lint issues
lint-fix:
    uv run ruff check . --fix

# Format with ruff
fmt:
    uv run ruff format .

# Check formatting without writing
fmt-check:
    uv run ruff format . --check

# Lint + format-check + tests
check: lint fmt-check test

# --- Evaluation harness -----------------------------------------------------
# Requires the eval extra:  just install-extras eval
# (openreview-py for the corpus, matplotlib for the figure).

eval_venue := "ICLR.cc/2025/Conference"
eval_dir := "data/eval"

# Inspect a venue's review/decision fields before fetching (no LLM, no cost)
eval-inspect venue=eval_venue:
    uv run python -m peerreviewagents.eval inspect --venue {{venue}}

# Fetch a balanced labeled corpus (default 10 papers) into data/eval/
eval-fetch limit="10" venue=eval_venue:
    uv run python -m peerreviewagents.eval fetch --venue {{venue}} --limit {{limit}} --out {{eval_dir}}

# Agreement phase: review every corpus paper once (resumable). Costs LLM calls.
eval-run *args:
    uv run python -m peerreviewagents.eval run --dir {{eval_dir}} --repeats 1 {{args}}

# Consistency phase: 3x runs on a subset. Usage: just eval-consistency ID1,ID2,ID3
eval-consistency ids:
    uv run python -m peerreviewagents.eval run --dir {{eval_dir}} --repeats 3 --only {{ids}}

# Compute the agreement + consistency report (prints + writes data/eval/report.*)
eval-metrics:
    uv run python -m peerreviewagents.eval metrics --dir {{eval_dir}}

# Render the paper figure (SVG+PNG) into paper/figures/eval_results.*
eval-figure:
    uv run python -m peerreviewagents.eval figure --dir {{eval_dir}} --out paper/figures/eval_results

# Full agreement pilot: fetch -> run all once -> metrics -> figure.
# (Consistency is a separate, cheaper step — pick ids, then `just eval-consistency`.)
eval-pilot limit="10":
    just eval-fetch {{limit}}
    just eval-run
    just eval-metrics
    just eval-figure

# --- Ablation pipelines (reproducible; rerun any time) ----------------------
# Shared conditioning for the ablations, matched to the headline run
# (model owl-alpha + conference profile + conference-paper type). Override on
# the command line if you change the comparison model, e.g.:
#   just eval-model="openrouter/some-model" eval-ablate-debate
eval_model := "openrouter/owl-alpha"
eval_cond := "--provider openrouter --model " + eval_model + " --journal ml-conference --article-type conference-paper"

# Debate ablation: review all corpus papers with the advocate/skeptic debate
# OFF, into its own runs file, then score it. Compare report_nodebate.md
# against the debate-ON report.md.
eval-ablate-debate:
    uv run python -m peerreviewagents.eval run --dir {{eval_dir}} {{eval_cond}} \
        --no-debate --runs-out {{eval_dir}}/runs_nodebate.jsonl \
        --leakage-note "debate-OFF ablation; conference profile"
    uv run python -m peerreviewagents.eval metrics --dir {{eval_dir}} \
        --runs {{eval_dir}}/runs_nodebate.jsonl --out {{eval_dir}}/report_nodebate
    just eval-ablation-figure

# Regenerate the ablation-ladder figure (single-LLM -> no-debate -> full).
# Needs the three runs files present. Override `baseline` if the comparison
# model changed (the baseline file is model-namespaced).
eval-ablation-figure baseline="data/eval/runs_baseline_openrouter-owl-alpha.jsonl":
    uv run python -m peerreviewagents.eval ablation-figure --dir {{eval_dir}} \
        --runs {{baseline}},{{eval_dir}}/runs_nodebate.jsonl,{{eval_dir}}/runs.jsonl \
        --labels "Single-LLM,No debate,Full pipeline" \
        --out paper/figures/eval_ablation --title "Structure ablation (n=30)"

# Strictness sweep: run a subset of papers at every strictness level (1..5),
# each to its own runs file, then tabulate how the weighted score moves.
# Usage: just eval-strictness-sweep "ID1,ID2,ID3"
eval-strictness-sweep ids:
    #!/usr/bin/env bash
    set -euo pipefail
    for L in 1 2 3 4 5; do
      uv run python -m peerreviewagents.eval run --dir {{eval_dir}} {{eval_cond}} \
        --strictness "$L" --only "{{ids}}" \
        --runs-out "{{eval_dir}}/runs_strict$L.jsonl" \
        --leakage-note "strictness sweep L=$L; conference profile"
    done
    uv run python -m peerreviewagents.eval sweep --dir {{eval_dir}} \
      --runs {{eval_dir}}/runs_strict1.jsonl,{{eval_dir}}/runs_strict2.jsonl,{{eval_dir}}/runs_strict3.jsonl,{{eval_dir}}/runs_strict4.jsonl,{{eval_dir}}/runs_strict5.jsonl \
      --labels 1,2,3,4,5 --out {{eval_dir}}/sweep_strictness
    just eval-strictness-figure

# Regenerate the strictness-sweep figure from the per-level runs files.
eval-strictness-figure:
    uv run python -m peerreviewagents.eval strictness-figure --dir {{eval_dir}} \
        --runs {{eval_dir}}/runs_strict1.jsonl,{{eval_dir}}/runs_strict2.jsonl,{{eval_dir}}/runs_strict3.jsonl,{{eval_dir}}/runs_strict4.jsonl,{{eval_dir}}/runs_strict5.jsonl \
        --labels "1,2,3,4,5" --out paper/figures/eval_strictness

# --- Human-vs-AI benchmark (30 published papers) ----------------------------
# Corpus of published papers with BOTH the preprint (what PRA reviews) and the
# real human peer reviews, for a human-vs-AI comparison figure in the paper.
# Batches: A=Nature-portfolio pairs, B=PLOS, C=Nature flagship (10 each).

# Assemble the corpus (idempotent): copy Batch A, download B/C bioRxiv
# preprints, scrape PLOS peer reviews. Pass e.g. `--only B_plos` or
# `--skip-network`.  Sources default to the user's Downloads folders.
benchmark-build *args:
    uv run python benchmark/build.py {{args}}

# Corpus + AI-run status at a glance
benchmark-status:
    uv run python benchmark/status.py

# Contamination probe (run BEFORE the main run): ask the review model, OFFLINE
# and with no manuscript, whether it already recalls each paper's abstract and
# its human reviews. Writes benchmark/contamination/REPORT.md + per-paper
# transcripts so you can drop/caveat high-recall papers.
benchmark-probe *args:
    uv run python benchmark/contamination_probe.py {{args}}

# Run the PRA pipeline over the ready corpus (resumable; skips finished papers).
# Leakage-free by DEFAULT: web research tools OFF (--offline) and every review
# runs under a network tripwire that allows only the LLM API and logs any other
# connection attempt (benchmark/ai_reviews/.../_netguard.json). temperature=0.
# Each paper is conditioned on its real venue + article type. Examples:
#   just benchmark-run --dry-run          (print commands, no LLM calls)
#   just benchmark-run --only C_nature
#   just benchmark-run --ids A-01,B-03
#   just benchmark-run --jobs 2           (2 papers at once — mind rate limits)
#   just benchmark-run --online           (ESCAPE HATCH: re-enable web + no guard)
benchmark-run *args:
    uv run python benchmark/run.py {{args}}

# Smoke-test one paper end to end (cheap: one full run) before the whole corpus
benchmark-smoke:
    uv run python benchmark/run.py --ids A-08 --limit 1

# Build/refresh the per-paper comparison SHEETS + INDEX: an AI-drafted overlap
# (Shared / Human-only / AI-only + bottom line) followed by a blank scoring
# table for your team. Runs offline. Sheets with a `<!-- edited-by-human -->`
# marker are preserved; --force overwrites anyway.
benchmark-compare *args:
    uv run python benchmark/precompare.py {{args}}

# Adversarially verify the comparison sheets against the source reviews:
# re-checks every Shared/Human-only/AI-only bullet and flags false-shared,
# missed-shared, unsupported, or missing-major errors. Offline.
# Writes benchmark/verification/REPORT.md + per-paper detail.
benchmark-verify *args:
    uv run python benchmark/verify_sheets.py {{args}}

# End-to-end once the corpus is built: run everything, then scaffold worksheets.
benchmark-all:
    just benchmark-run
    just benchmark-compare

# --- Manuscript cache -------------------------------------------------------

# Show what's in the manuscript parsing cache
cache-info:
    uv run python -c "from peerreviewagents.ingest.cache import stats; s = stats(); print(f\"root:    {s['root']}\"); print(f\"entries: {s['entries']}\"); print(f\"size:    {s['bytes'] / 1024:.1f} KiB\")"

# Wipe the manuscript parsing cache
cache-clear:
    uv run python -c "from peerreviewagents.ingest.cache import clear; print(f'removed {clear()} entries')"

# --- Housekeeping -----------------------------------------------------------

# Remove generated reports
clean-reports:
    rm -rf reports/

# Remove caches and build artifacts
clean:
    rm -rf .pytest_cache .ruff_cache build dist *.egg-info
    find . -type d -name __pycache__ -prune -exec rm -rf {} +

# Full wipe: caches, reports, and venv
clean-all: clean clean-reports clean-venv
