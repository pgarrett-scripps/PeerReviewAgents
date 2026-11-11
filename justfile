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

# Install with the MCP extra (the MCP server and the agent plugins)
install-mcp:
    [ -d .venv ] || uv venv
    uv pip install -e ".[mcp]"

# Install with a chosen extras group: `just install-extras mcp`
install-extras extras:
    [ -d .venv ] || uv venv
    uv pip install -e ".[{{extras}}]"

# Install dev/test tooling alongside the project
install-dev:
    [ -d .venv ] || uv venv
    uv pip install -e ".[dev]"

# Sync the venv to match pyproject.toml exactly (drops anything stale)
sync:
    uv pip install -e . --reinstall

# Wipe the local virtualenv
clean-venv:
    rm -rf .venv

# --- Run --------------------------------------------------------------------
# These recipes spend subscription usage. `just review paper.pdf` is the whole
# review: it imports the PDF, runs it and writes REVIEW.md beside it.

# Review a manuscript PDF end to end (pass extra peerreview flags after the path)
review path *args:
    uv run peerreview {{path}} {{args}}

# Import a PDF into a manuscript workspace, with no model calls
import path workspace:
    uv run peerreview import {{path}} {{workspace}}

# Verify an imported workspace against its manifest (no model calls)
verify workspace:
    uv run peerreview verify {{workspace}}

# Rewrite REVIEW.md and REVIEW.txt from a finished or stopped run (no model calls)
report run:
    uv run peerreview report {{run}}

# Launch the FastAPI web UI (upload a PDF, watch the run, read the report)
serve *args:
    uv run peerreview serve {{args}}

# --- Quality ----------------------------------------------------------------

# Run the test suite (no model calls, no network, no API keys)
test:
    uv run --extra dev pytest tests/ -q

# Run tests with verbose output
test-v:
    uv run --extra dev pytest tests/ -vv

# Run the engine suite alone
test-engine:
    uv run --extra dev pytest tests/engine -q

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

# --- Housekeeping -----------------------------------------------------------

# Remove what `just serve` wrote (review runs and uploaded manuscripts)
clean-reviews:
    rm -rf reviews/ .peerreview-uploads/

# Remove caches and build artifacts
clean:
    rm -rf .pytest_cache .ruff_cache build dist *.egg-info
    find . -type d -name __pycache__ -prune -exec rm -rf {} +

# Full wipe: caches, review runs, and venv
clean-all: clean clean-reviews clean-venv
