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
    # Advocate/skeptic debate. True (default) runs the dialectical debate
    # between the reviewer panel and the meta-reviewer; False ablates it
    # (reviewers feed the meta-reviewer directly) — used to measure the
    # debate's contribution in the eval harness.
    "enable_debate": True,
    # Optional editorial desk-screen gate. When True, a triage node runs once
    # before the reviewer fan-out and may desk-reject the manuscript (scope /
    # completeness / fatal-flaw / below-venue-bar), short-circuiting the run
    # to a reject without spending the panel. Off by default — a default run
    # is unchanged. Screens against the target journal + review strictness.
    "desk_screen": False,
    # Hard cap on manuscript chars sent to a single agent. Long papers are
    # truncated; section-aware truncation preserves the most load-bearing
    # sections (abstract, methods, results, discussion, conclusion) and drops
    # appendices/supplements first.
    "manuscript_char_budget": 60000,
    # Optional path to a supplementary-information file (pdf/md/tex/docx).
    # When set, the SI is parsed and passed IN FULL to the
    # methods_completeness auditor only (reagent/key-resources tables and
    # full protocols often live here). None = no SI; the run is unchanged.
    "supplement_path": None,

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

    # --- Target journal ---
    # Slug of the journal to review against (a file <slug>.toml under
    # journals_dir). Defaults to "general" — a stand-in profile with sound,
    # field-general standards for venues not in the database. Set to a
    # specific slug (e.g. "nature-methods") for venue-specific standards, or
    # to "" for a fully venue-agnostic review with no journal framing. The
    # selected journal's scope/standards/limits are injected into the
    # reviewer, meta-reviewer, editor, and recommender prompts.
    "target_journal": "general",
    # Directory holding journal profile .toml files. Empty/None = the
    # profiles bundled inside the peerreviewagents.journals package (resolved
    # relative to the package, so it works regardless of working directory
    # and from an installed wheel). Set to point at your own profiles dir.
    "journals_dir": None,

    # --- Article type ---
    # The kind of submission being reviewed (a venue-general taxonomy; see
    # peerreviewagents.article_types): one of "article", "letter",
    # "communication", "perspective", "review", "technical-note", "tutorial",
    # or "" for no manuscript-type framing (default). The selected type tells
    # the panel what kind of work it is judging; any per-type word caps come
    # from the target journal's profile. Renders nothing when "".
    "article_type": "",

    # --- Review strictness ---
    # How easy or harsh the panel is, as an integer 1-5 (see
    # peerreviewagents.strictness): 1=very lenient, 3=balanced (default),
    # 5=very strict. The level is rendered to a directive that is injected
    # into the reviewer, meta-reviewer, and editor prompts. Level 3 injects
    # nothing, so a default run behaves exactly as before.
    "review_strictness": 3,

    # --- Output ---
    "output_dir": os.path.join(os.getcwd(), "reports"),

    # --- Persistence ---
    "memory_path": os.path.join(
        os.path.expanduser("~"), ".peerreviewagents", "memory", "review_memory.md"
    ),
    # How many resolved past-review lessons to inject into the meta-
    # reviewer's prompt (BM25-ranked by manuscript topic).
    "memory_k": 3,
    # Master switch for the cross-run memory loop. When False, the
    # meta-reviewer retrieves no past lessons and completed runs are not
    # appended to the log — the run is fully memory-free.
    "use_memory": True,

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
    "strictness": "review_strictness",
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
    "PEERREVIEW_TARGET_JOURNAL": "target_journal",
    "PEERREVIEW_JOURNALS_DIR": "journals_dir",
    "PEERREVIEW_ARTICLE_TYPE": "article_type",
}
_ENV_INT_KEYS = {
    "PEERREVIEW_DEBATE_ROUNDS": "max_debate_rounds",
    "PEERREVIEW_STRICTNESS": "review_strictness",
}
_ENV_BOOL_KEYS = {
    "PEERREVIEW_DESK_SCREEN": "desk_screen",
    "PEERREVIEW_USE_MEMORY": "use_memory",
}
_TRUE_STRINGS = {"1", "true", "yes", "on"}
_FALSE_STRINGS = {"0", "false", "no", "off"}


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
    for env_key, cfg_key in _ENV_BOOL_KEYS.items():
        val = os.environ.get(env_key)
        if val is not None:
            low = val.strip().lower()
            if low in _TRUE_STRINGS:
                out[cfg_key] = True
            elif low in _FALSE_STRINGS:
                out[cfg_key] = False
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
