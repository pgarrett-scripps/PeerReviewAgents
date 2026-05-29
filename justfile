# PeerReviewAgents — task runner
# Run `just` or `just --list` to see all commands.

set shell := ["bash", "-cu"]

# Default: show available recipes
default:
    @just --list

# --- Install ----------------------------------------------------------------

# Create the uv-managed venv and install the project (editable)
install:
    uv venv
    uv pip install -e .

# Install with every optional extra
install-all:
    uv venv
    uv pip install -e ".[research,pdf-ingest]"

# Install with a chosen extras group: `just install-extras research,pdf`
install-extras extras:
    uv venv
    uv pip install -e ".[{{extras}}]"

# Install dev/test tooling alongside the project
install-dev:
    uv venv
    uv pip install -e ".[research,pdf-ingest]"
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

# Smoke-test PDF ingestion via the Datalab API on one PDF (no LLM calls, no vision).
# Writes the extracted markdown to reports/_ingest_test.md and prints a summary.
# Requires DATALAB_API_KEY in the environment.
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
    img_refs = len(re.findall(r"!\[\]\([^)]+\)", md))
    headings = len(re.findall(r"(?m)^#+ "  , md))
    table_rows = len(re.findall(r"(?m)^\|.+\|$", md))

    print(f"elapsed:    {elapsed:.1f}s")
    print(f"title:      {title[:80]}")
    print(f"chars:      {len(md)}")
    print(f"image refs: {img_refs}")
    print(f"headings:   {headings}")
    print(f"table rows: {table_rows}")
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
