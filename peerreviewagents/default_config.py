"""Default configuration for PeerReviewAgents.

Resolution order, lowest precedence first:
    1. DEFAULT_CONFIG          — hardcoded fallbacks below.
    2. User-global TOML        — ~/.config/peerreviewagents/config.toml
    3. Project-local TOML      — ./peerreview.toml
    4. Explicit --config TOML  — passed via `get_config(config_path=...)`
    5. PEERREVIEW_* env vars   — for one-off overrides.
    6. Explicit kwargs         — `get_config(provider="...")`, CLI flags.

Secrets (ANTHROPIC_API_KEY, OPENAI_API_KEY, OPENROUTER_API_KEY, GOOGLE_API_KEY)
are never read by this module — they stay in the environment (loaded from
`.env` by the CLI) and are picked up by the LLM SDKs directly.
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
    # --- LLM provider / models ---
    # provider: one of "anthropic", "openai", "google", "openrouter", "ollama"
    "provider": "anthropic",
    "deep_think_llm": "claude-opus-4-7",
    "quick_think_llm": "claude-haiku-4-5-20251001",
    "temperature": 0.3,
    # Base URL override (used by openrouter / ollama / custom gateways)
    "base_url": None,

    # --- Workflow controls ---
    # Which specialist reviewers to run in the parallel pass.
    "reviewer_set": ["methodology", "data_analysis", "novelty", "clarity", "literature"],
    "max_debate_rounds": 2,
    # Hard cap on manuscript chars sent to a single agent. Long papers are
    # truncated; section-aware truncation preserves the most load-bearing
    # sections (abstract, methods, results, discussion, conclusion) and drops
    # appendices/supplements first.
    "manuscript_char_budget": 60000,

    # --- Research / grounding layer ---
    "research_enabled": True,
    # Tools the research layer may use when research_enabled is True.
    # "tavily" enables web search + URL extract (needs TAVILY_API_KEY).
    # The legacy name "web" is accepted as an alias for "tavily".
    "research_tools": ["tavily", "arxiv", "scholar"],
    # Auto-detect scientific manuscripts and enable domain MCP grounding.
    "domain_grounding": False,

    # --- Tavily web search (used when "tavily" is in research_tools) ---
    # Requires TAVILY_API_KEY in the environment. Without it, Tavily tools
    # are silently omitted and the agents fall back to the scientific APIs.
    "tavily_search_depth": "advanced",     # "basic" (1 credit) | "advanced" (2)
    "tavily_max_results": 5,
    "tavily_topic": "general",             # "general" | "news"
    "tavily_include_domains": [],          # e.g. ["nature.com","nih.gov","arxiv.org"]
    "tavily_exclude_domains": [],
    "tavily_timeout": 30.0,                # seconds per HTTP call
    "tavily_max_retries": 3,               # attempts on transient failure
    "tavily_backoff_base": 0.75,           # seconds; doubles each retry
    "tavily_backoff_cap": 8.0,             # max retry sleep
    "tavily_cache_ttl": 3600.0,            # seconds; 0 disables cache
    "tavily_cache_maxsize": 256,

    # --- Figure understanding (vision model) ---
    # Runs once at ingest time: each extracted figure is sent to a vision
    # model whose prose description is inlined into manuscript_md so the
    # text-only reviewer LLMs can reason about figure content.
    "vision_enabled": False,
    "vision_provider": None,   # falls back to `provider` if None
    "vision_model": None,      # required when vision_enabled
    "vision_base_url": None,   # falls back to `base_url`
    "vision_temperature": 0.2,
    "vision_max_figures": 10,  # cap per manuscript (cost control)
    "vision_prompt": None,     # optional override of the default prompt

    # --- Output ---
    "output_dir": os.path.join(os.getcwd(), "reports"),
    "emit_pdf": False,
    # Include an accept/minor/major/reject verdict in the editor decision.
    "emit_verdict": True,

    # --- Persistence ---
    "memory_path": os.path.join(
        os.path.expanduser("~"), ".peerreviewagents", "memory", "review_memory.md"
    ),
    "checkpoint": False,
    "checkpoint_path": os.path.join(
        os.path.expanduser("~"), ".peerreviewagents", "checkpoints", "review.sqlite"
    ),

    # --- Manuscript cache ---
    # Cache the parsed (title, markdown, sections) triple keyed by file
    # content + vision config. Default location: ~/.cache/peerreviewagents/.
    "cache_enabled": True,
    "cache_dir": None,  # None -> XDG default; set to a path to override.

    # --- Runtime ---
    "debug": False,
}


# Friendly TOML names -> internal config keys. Any TOML key not in this map
# is passed through unchanged (and validated against DEFAULT_CONFIG keys).
_TOML_KEY_RENAMES = {
    "deep_model": "deep_think_llm",
    "quick_model": "quick_think_llm",
    "reviewers": "reviewer_set",
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
    "PEERREVIEW_DEEP_MODEL": "deep_think_llm",
    "PEERREVIEW_QUICK_MODEL": "quick_think_llm",
    "PEERREVIEW_BASE_URL": "base_url",
    "PEERREVIEW_OUTPUT_DIR": "output_dir",
    "PEERREVIEW_CACHE_DIR": "cache_dir",
    "PEERREVIEW_VISION_PROVIDER": "vision_provider",
    "PEERREVIEW_VISION_MODEL": "vision_model",
}
_ENV_FLOAT_KEYS = {"PEERREVIEW_TEMPERATURE": "temperature"}
_ENV_INT_KEYS = {
    "PEERREVIEW_DEBATE_ROUNDS": "max_debate_rounds",
}
_ENV_BOOL_KEYS = {
    "PEERREVIEW_RESEARCH_ENABLED": "research_enabled",
    "PEERREVIEW_EMIT_PDF": "emit_pdf",
    "PEERREVIEW_VISION_ENABLED": "vision_enabled",
    "PEERREVIEW_CACHE_ENABLED": "cache_enabled",
}


def _truthy(s: str) -> bool:
    return s.strip().lower() in {"1", "true", "yes", "on"}


def _env_overrides() -> dict[str, Any]:
    out: dict[str, Any] = {}
    for env_key, cfg_key in _ENV_STR_KEYS.items():
        val = os.environ.get(env_key)
        if val:
            out[cfg_key] = val
    for env_key, cfg_key in _ENV_FLOAT_KEYS.items():
        val = os.environ.get(env_key)
        if val:
            try:
                out[cfg_key] = float(val)
            except ValueError:
                pass
    for env_key, cfg_key in _ENV_INT_KEYS.items():
        val = os.environ.get(env_key)
        if val:
            try:
                out[cfg_key] = int(val)
            except ValueError:
                pass
    for env_key, cfg_key in _ENV_BOOL_KEYS.items():
        val = os.environ.get(env_key)
        if val:
            out[cfg_key] = _truthy(val)
    reviewers = os.environ.get("PEERREVIEW_REVIEWERS")
    if reviewers:
        out["reviewer_set"] = [r.strip() for r in reviewers.split(",") if r.strip()]
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
