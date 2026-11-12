# PeerReviewAgents — task runner
# Run `just` or `just --list` to see all commands.

set shell := ["bash", "-cu"]

# Default: show available recipes
default:
    @just --list

# --- Install ----------------------------------------------------------------
# The venv has to be `.venv` here: the plugin launches its MCP server as
# `${CLAUDE_PROJECT_DIR}/.venv/bin/python`.

# Create the uv-managed venv and install the project (editable)
install:
    [ -d .venv ] || uv venv
    uv pip install -e .

# Install with a chosen extras group: `just install-extras dev`
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
# `just review paper.pdf run/` spends subscription usage: it imports the PDF,
# runs the pra plugin over it in a headless Claude Code session, and writes
# REVIEW.md into the run folder.

# Review a manuscript PDF end to end (pass extra review flags after the folder)
review path out *args:
    uv run peerreview review {{path}} {{out}} {{args}}

# Rewrite REVIEW.md and REVIEW.txt from a finished or blocked run (no model calls)
report run:
    uv run peerreview report {{run}}

# --- Quality ----------------------------------------------------------------

# Run the test suite (no model calls, no network, no subscription)
test:
    uv run --extra dev pytest tests/ -q

# Run tests with verbose output
test-v:
    uv run --extra dev pytest tests/ -vv

# Run the manuscript-workspace suite alone
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

# Remove caches and build artifacts
clean:
    rm -rf .pytest_cache .ruff_cache build dist *.egg-info
    find . -type d -name __pycache__ -prune -exec rm -rf {} +

# Full wipe: caches and venv
clean-all: clean clean-venv
