"""Default configuration for PeerReviewAgents.

Resolution order, lowest precedence first:
    1. DEFAULT_CONFIG          — hardcoded fallbacks below.
    2. User-global TOML        — ~/.config/peerreviewagents/config.toml
    3. Project-local TOML      — ./peerreview.toml
    4. Explicit --config TOML  — passed via `get_config(config_path=...)`
    5. PEERREVIEW_* env vars   — for one-off overrides.
    6. Explicit kwargs         — `get_config(reasoning_model="...")`, CLI flags.

The pipeline is hardwired to OpenRouter. Secrets (OPENROUTER_API_KEY,
DATALAB_API_KEY) live in the environment / `.env` and are never read by
this module.
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
    # --- Models (OpenRouter) ---
    # Single text model used by every reviewer, debater, synthesizer,
    # integrity auditor, and the editor-in-chief. Use an OpenRouter slug
    # like "anthropic/claude-opus-4.1" or "openai/gpt-4o".
    "reasoning_model": "anthropic/claude-opus-4.1",
    # Multimodal model used during ingest to describe each extracted
    # figure. OpenRouter slug — needs to accept image input.
    "vision_model": "anthropic/claude-haiku-4.5",

    # --- Workflow ---
    "max_debate_rounds": 2,
    # Hard cap on manuscript chars sent to a single agent. Long papers are
    # truncated; section-aware truncation preserves the most load-bearing
    # sections (abstract, methods, results, discussion, conclusion) and drops
    # appendices/supplements first.
    "manuscript_char_budget": 60000,

    # --- Figure understanding (vision model) ---
    # Always runs during PDF ingest. Each extracted figure is described
    # by `vision_model` and the description is inlined into the manuscript
    # markdown so text-only reviewers can reason about figure content.
    # Cost scales with figure count, not with reviewer count.
    "vision_max_figures": 10,

    # --- PDF ingest (Datalab marker API) ---
    # Requires DATALAB_API_KEY in the environment. Defaults are tuned for
    # academic manuscripts; override in peerreview.toml when needed.
    "pdf_force_ocr": False,     # force OCR even on text-layer PDFs
    "pdf_use_llm": False,       # let Datalab use an LLM to clean tables/headings (costs more)
    "pdf_max_pages": None,      # cap pages sent for processing (None = full doc)
    "pdf_page_range": None,     # e.g. "0-5,10" (0-indexed) for partial ingest
    "pdf_langs": None,          # OCR language hint, e.g. "English"

    # --- Output ---
    "output_dir": os.path.join(os.getcwd(), "reports"),

    # --- Persistence ---
    "memory_path": os.path.join(
        os.path.expanduser("~"), ".peerreviewagents", "memory", "review_memory.md"
    ),

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
    "PEERREVIEW_REASONING_MODEL": "reasoning_model",
    "PEERREVIEW_VISION_MODEL": "vision_model",
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
