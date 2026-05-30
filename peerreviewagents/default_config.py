"""Default configuration for PeerReviewAgents.

Resolution order, lowest precedence first:
    1. DEFAULT_CONFIG          — hardcoded fallbacks below.
    2. User-global TOML        — ~/.config/peerreviewagents/config.toml
    3. Project-local TOML      — ./peerreview.toml
    4. Explicit --config TOML  — passed via `get_config(config_path=...)`
    5. PEERREVIEW_* env vars   — for one-off overrides.
    6. Explicit kwargs         — `get_config(reasoning_model="...")`, CLI flags.

Three LLM providers are wired up: ``openrouter`` (default), ``anthropic``
(direct), ``openai`` (direct). Pick one with ``provider = "anthropic"``
in TOML or ``--provider anthropic`` on the CLI. API keys live in the
environment / ``.env`` (OPENROUTER_API_KEY, ANTHROPIC_API_KEY,
OPENAI_API_KEY) and are never read by this module.

PDF ingest is fully local (pypdf) — no external API key required.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore[no-redef]


DEFAULT_CONFIG: dict[str, Any] = {
    # --- Model ---
    # Which provider to call. One of: "openrouter" (default), "anthropic",
    # "openai". See peerreviewagents.runtime.providers.PROVIDERS.
    "provider": "openrouter",
    # Single text model used by every reviewer, debater, synthesizer,
    # author rebuttal, editor, and journal recommender. The model string
    # is interpreted by the active provider:
    #   openrouter -> a slug like "anthropic/claude-opus-4.1"
    #   anthropic  -> a model id like "claude-opus-4-7" or "claude-sonnet-4-6"
    #   openai     -> a model id like "gpt-4.1" or "o3"
    "reasoning_model": "anthropic/claude-opus-4.1",

    # --- Workflow ---
    "max_debate_rounds": 2,
    # Hard cap on manuscript chars sent to a single agent. Long papers are
    # truncated; section-aware truncation preserves the most load-bearing
    # sections (abstract, methods, results, discussion, conclusion) and drops
    # appendices/supplements first.
    "manuscript_char_budget": 60000,

    # --- Research vendors ---
    # Per-category default vendor list (comma-separated, primary first).
    # Used by peerreviewagents.research.interface.route to pick which
    # vendor serves each logical operation; on rate-limit the router
    # falls through to the next vendor in the list. ``tool_vendors``
    # is a per-method override map (e.g. {"find_related_work": "arxiv"}).
    "data_vendors": {
        "paper_search": "semantic_scholar,arxiv",
        "biomedical":   "pubmed,biorxiv",
        "preprints":    "biorxiv,arxiv",
    },
    "tool_vendors": {},

    # --- Output ---
    "output_dir": os.path.join(os.getcwd(), "reports"),

    # --- Persistence ---
    "memory_path": os.path.join(
        os.path.expanduser("~"), ".peerreviewagents", "memory", "review_memory.md"
    ),
    # How many resolved past-review lessons to inject into the meta-
    # reviewer's prompt (BM25-ranked by manuscript topic).
    "memory_k": 3,

    # --- Manuscript cache ---
    # Parsed (title, markdown, sections) triples are always cached on disk,
    # keyed by file content + the ingest config slice. Default location is
    # ~/.cache/peerreviewagents/manuscripts/; set cache_dir to override.
    # Wipe with `just cache-clear`.
    "cache_dir": None,

    # --- Runtime ---
    "debug": False,
}


# Friendly TOML names -> internal config keys. Any TOML key not in this map
# is passed through unchanged (and validated against DEFAULT_CONFIG keys).
_TOML_KEY_RENAMES = {
    "debate_rounds": "max_debate_rounds",
}


def _user_global_config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
        os.path.expanduser("~"), ".config"
    )
    return Path(base) / "peerreviewagents" / "config.toml"


def _project_config_path() -> Path:
    return Path.cwd() / "peerreview.toml"


def _read_toml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    with path.open("rb") as fh:
        return tomllib.load(fh)


def _normalize_toml(raw: dict[str, Any]) -> dict[str, Any]:
    """Map TOML keys to internal config keys; ignore unknown keys silently
    rather than crashing, but they have no effect."""
    out: dict[str, Any] = {}
    valid_keys = set(DEFAULT_CONFIG.keys())
    for k, v in raw.items():
        internal = _TOML_KEY_RENAMES.get(k, k)
        if internal in valid_keys:
            out[internal] = v
    return out


# --- env var fallback (kept for one-off overrides / CI / scripts) -----------

_ENV_STR_KEYS = {
    "PEERREVIEW_PROVIDER": "provider",
    "PEERREVIEW_REASONING_MODEL": "reasoning_model",
    "PEERREVIEW_OUTPUT_DIR": "output_dir",
    "PEERREVIEW_CACHE_DIR": "cache_dir",
}
_ENV_INT_KEYS = {
    "PEERREVIEW_DEBATE_ROUNDS": "max_debate_rounds",
}


def _env_overrides() -> dict[str, Any]:
    out: dict[str, Any] = {}
    for env_key, cfg_key in _ENV_STR_KEYS.items():
        val = os.environ.get(env_key)
        if val:
            out[cfg_key] = val
    for env_key, cfg_key in _ENV_INT_KEYS.items():
        val = os.environ.get(env_key)
        if val:
            try:
                out[cfg_key] = int(val)
            except ValueError:
                pass
    return out


# --- public API -------------------------------------------------------------


def get_config(config_path: str | os.PathLike | None = None, **overrides: Any) -> dict:
    """Build a fully-resolved config dict.

    Args:
        config_path: optional explicit TOML path (CLI --config). Overrides
            both user-global and project-local config files.
        **overrides: explicit Python-level overrides (CLI flags, library
            callers). Win over everything else.
    """
    cfg: dict[str, Any] = DEFAULT_CONFIG.copy()

    # Layer TOML files in increasing-precedence order.
    toml_paths: list[Path] = [_user_global_config_path(), _project_config_path()]
    if config_path is not None:
        toml_paths.append(Path(config_path))
    for path in toml_paths:
        cfg.update(_normalize_toml(_read_toml(path)))

    cfg.update(_env_overrides())
    cfg.update(overrides)
    return cfg
