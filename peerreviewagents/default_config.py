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

PDF ingest is fully local (rustypaper) — no external API key required.
"""

from __future__ import annotations

import os
import warnings
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
    #   openrouter -> a slug like "anthropic/claude-opus-5"
    #   anthropic  -> a model id like "claude-opus-5" or "claude-sonnet-5"
    #   openai     -> a model id like "gpt-4.1" or "o3"
    "reasoning_model": "anthropic/claude-opus-5",

    # Sampling temperature for every text agent. None = the provider default
    # (0.3). Set 0.0 for the most reproducible/defensible single run. NOTE:
    # the current Anthropic models (Opus 5, Sonnet 5, Opus 4.7/4.8, Fable 5)
    # reject the `temperature` parameter outright, so this has no effect on
    # the default model — it only takes effect on models that still accept
    # sampling (e.g. the Haiku reading panel).
    "temperature": None,

    # Base URL for the "openai" provider. None (default) = api.openai.com.
    # Point it at any OpenAI-compatible gateway (Ollama, vLLM, Groq, ...) to
    # use local or third-party models without wiring a fourth provider —
    # TradingAgents' `backend_url` idea. Setting it also relaxes the
    # provider/model-id shape check for this provider: gateways serve
    # HuggingFace-style "org/model" ids, which the check would otherwise
    # reject as impossible OpenAI ids. (Env: PEERREVIEW_OPENAI_BASE_URL.)
    "openai_base_url": None,

    # --- Per-agent models (optional) ---
    # Named model "tags". Each maps a tag to {provider?, model?, effort?};
    # any field left out falls back to the global provider / reasoning_model
    # above. Example (TOML):
    #     [models.synthesis]           # editor, meta-reviewer, rebuttal, recommender
    #     provider = "anthropic"
    #     model = "claude-opus-5"
    #     effort = "high"
    #     [models.reviewer]            # the specialist panel + desk screen
    #     provider = "anthropic"
    #     model = "claude-sonnet-5"
    # Agents resolve their model through a code-declared default tag:
    #   reviewers -> "reviewer", debate -> "debate", auditors -> "audit",
    #   and editor / meta-reviewer / author-rebuttal / journal-recommender ->
    #   "synthesis". There is deliberately NO "screen" tag: the desk screen
    #   (and the response verifier) share "reviewer", so the prompt cache
    #   they warm is the one the panel then reads — caches are per-model.
    #   This file used to advertise [models.screen] as an example, and the
    #   In Silico journal shipped one; it was silently inert. get_config now
    #   warns on any tag no agent resolves through (see panel.KNOWN_TAGS).
    #   Defining a tag retargets that whole group; leaving "models" empty
    #   means every agent uses the global model.
    "models": {},
    # Per-agent override map: agent key -> tag name or an inline
    # {provider?, model?, effort?} spec. Wins over the agent's default tag.
    # Agent keys: reviewer_<name> (e.g. reviewer_methodology), debate_advocate,
    # debate_skeptic, audit_<name>, desk_screen, response_verifier,
    # meta_reviewer, editor, author_rebuttal, journal_recommender (the full
    # roster lives in peerreviewagents.panel). Example (TOML):
    #     [agent_models]
    #     reviewer_novelty = "synthesis"          # give one reviewer the big model
    #     editor = { model = "claude-opus-5", effort = "max" }
    "agent_models": {},
    # Run the named reasoning_model for EVERY agent by clearing `models` and
    # `agent_models` after all config layers are applied. This exists because
    # the tag tables win over an explicit --reasoning-model for every tagged
    # agent: In Silico's per-agent split meant a dev's `--model
    # minimax/minimax-m3` reviewed nothing and the run billed the lab for
    # Claude, and the only workaround was manually writing `models = {}` on
    # the command line's config layer. `--single-model <flag>` / this key is
    # that dance, upstreamed. (Env: PEERREVIEW_SINGLE_MODEL.)
    "single_model": False,

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
    # Desk-screen mode: "off", "warm", or "gate". Default None -> fall back to
    # the legacy `desk_screen` bool (True -> "gate", False -> "off"). Set this
    # to override.
    #   gate — run triage and desk-reject weak manuscripts (short-circuit).
    #   warm — run triage ONLY to prime the shared manuscript prompt cache
    #          before the parallel reviewer fan-out, then always proceed.
    #   off  — skip the node entirely.
    # (Present here as None so TOML `desk_screen_mode = "..."` isn't dropped.)
    "desk_screen_mode": None,
    # What a failed PDF conversion does. Measured deterministically at ingest
    # (peerreviewagents.ingest.prose) and checked before any agent is paid.
    #
    #   "broken"   (default) stop the run when the text arrives as
    #              `well-definedsitecanbeengaged` — words fused, spaces gone.
    #              Raises ManuscriptUnreadable rather than desk-rejecting: a
    #              converter failure is a fact about a file, and recording it
    #              as a verdict would follow the paper around as a rejection
    #              of work no model ever read.
    #   "degraded" also stop on lesser damage. For callers who would rather
    #              fix the conversion than have a panel read around it.
    #   "off"      review whatever arrives. The prior behaviour.
    #
    # On the calibration corpus there is no middle ground to worry about:
    # healthy conversions score 0.0 fused tokens per 1000 words and the broken
    # ones score ~23, so the default costs nothing on a readable paper. Damage
    # short of the gate is not silently absorbed either — it is named to every
    # reviewer inside the manuscript block, so nobody writes up the authors
    # for the converter's spacing.
    "conversion_gate": "broken",
    # Optional cap on manuscript chars sent to a single agent. None (default)
    # sends the FULL manuscript — no truncation. Set an int to cap it: long
    # papers are then truncated section-aware, preserving the most load-bearing
    # sections (abstract, methods, results, discussion, conclusion) and dropping
    # appendices/supplements first. Raise a cap only to bound cost on very long
    # papers; cost scales with manuscript length × the ~14 agents that read it.
    "manuscript_char_budget": None,
    # --- Revision rounds ---
    # Job ID (or report directory) of the previous round for this manuscript.
    # Setting it turns the run into a revision round: each reviewer receives
    # its OWN prior report and rules on every point it raised, a compliance
    # auditor checks the previous decision letter's numbered required
    # revisions against the new draft, and the editor decides on the delta.
    # None (default) = an ordinary first-round review. See
    # peerreviewagents.rounds for the record format.
    "revision_of": None,
    # Optional path to the REAL authors' response letter (pdf/md/tex/txt) —
    # the human scientists' reply, not the simulated author-rebuttal agent.
    # Treated as untrusted, interested-party input: never shown to the panel
    # as prose, and reaches reviewers only as verified pointers to passages they
    # must re-read and judge for themselves. It can redirect attention; it
    # cannot move a score on its own. None = no statement supplied.
    "author_statement_path": None,
    # Hard cap on revision rounds. The editor is told which round it is and
    # how many remain, so an endless revise-and-resubmit loop reads as the
    # failure it is rather than continuing indefinitely.
    "max_rounds": 3,
    # What kind of follow-up run this is. Both need `revision_of`; they differ
    # in what changed, and therefore in what is allowed to run.
    #
    #   "revision"   (default) the AUTHORS changed the manuscript. Diff the
    #                drafts, run the compliance auditor over the previous
    #                letter's required revisions, decide on the delta.
    #
    #   "correction" the manuscript is UNCHANGED and the review itself is
    #                challenged. The compliance auditor must not run: against
    #                an identical draft it would correctly report every
    #                required revision as undone and push the verdict down —
    #                punishing an author who was right that a reviewer misread
    #                the paper. The diff is skipped for the same reason: there
    #                is nothing to compare. The response verifier still runs,
    #                and it is what separates a checkable factual claim from a
    #                disagreement about judgment.
    "revision_mode": "revision",
    # Restrict the specialist fan-out to these reviewers by name, e.g.
    # ["methodology", "data_analysis"]. Empty (default) = the full panel.
    #
    # Built for corrections. If one reviewer misread a table, only that
    # reviewer's assessment should move; re-running all eight lets the other
    # seven drift on resampling noise and overstates what the correction
    # actually changed. Isolating it is the accurate thing, not just the cheap
    # one.
    #
    # Reviewers left out are NOT dropped: their prior reports are carried
    # forward from `revision_of`, so the meta-reviewer and editor still see a
    # full panel rather than an aggregate over the one agent that re-ran.
    # Requires `revision_of` for that reason.
    "only_reviewers": [],

    # Optional path to a supplementary-information file (pdf/md/tex/txt).
    # When set, the SI is parsed and passed IN FULL to the
    # methods_completeness auditor only (reagent/key-resources tables and
    # full protocols often live here). None = no SI; the run is unchanged.
    "supplement_path": None,

    # --- Research vendors ---
    # Master switch for the web research tools (find_related_work,
    # search_biomedical_literature, search_preprints — used by the novelty and
    # literature reviewers and the citation-integrity auditor). True = tools
    # are bound and may hit PubMed / Semantic Scholar / bioRxiv / arXiv.
    # False (offline mode, via --offline) = those agents get NO tools and the
    # research router refuses, so the pipeline makes no outbound calls except
    # to the LLM inference API. Use False for leakage-free benchmark runs.
    "research_enabled": True,

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

    # --- Manuscript ingest ---
    # PDFs are converted to Markdown by rustypaper, which keeps headings,
    # tables and equations. There is no second backend and no fallback: the
    # alternative was pypdf's flat text layer, which on a two-column paper
    # fuses words across the column boundary and loses structure entirely,
    # and a panel reading that reviews a document the authors did not write.
    # A missing converter is an error. See peerreviewagents.ingest.loader.
    #
    # Telegraphic compression of the manuscript, for models billed by the
    # token: "off" (the default), "light" (drops articles and copulas) or
    # "hard" (also prepositions and connectives). Mathematics, tables and
    # bibliography entries are exempt at every level.
    #
    # Off by default because it was measured, not assumed. Under "light" the
    # clarity reviewer reported "grammatical errors that obscure the main
    # claims" three times on a paper where the uncompressed run reported
    # none — it read the compressor's work as the authors' writing. Against
    # that, the manuscript is a cached prefix read by the cheapest model
    # tier, so compressing it saves well under a cent a review.
    #
    # When it IS on, manuscript_block() tells every agent the text was
    # machine-compressed, which is the least a published review owes an
    # author whose prose it is about to criticise. Even so, prefer it on
    # paths that never publish a referee's words.
    "caveman": "off",

    # How long a provider-side prompt-cache entry should live: "1h" (default)
    # or "5m" (the provider's own default). The manuscript is sent to every
    # agent as a shared cached prefix and read ~20 times per review, but a
    # review takes 10-20 minutes and the stages after the panel run
    # sequentially — so under "5m" the entry expires mid-run and the next
    # agent rewrites the whole manuscript. A 1h write costs 2x base where a
    # 5m write costs 1.25x, which is the trade: 0.75 of one manuscript when
    # nothing would have expired, against a rewrite per stage when it does.
    "cache_ttl": "1h",

    # --- Manuscript cache ---
    # Parsed manuscripts are always cached on disk, keyed by file content +
    # the caveman level above. Default location is
    # ~/.cache/peerreviewagents/manuscripts/; set cache_dir to override.
    # Wipe with `just cache-clear`.
    "cache_dir": None,

    # --- Runtime ---
    # Identifies one review run. Generated per PeerReviewGraph unless set;
    # tags emitted events so concurrent runs don't share an observer queue.
    "run_id": None,
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


def _normalize_toml(raw: dict[str, Any], source: Path | None = None) -> dict[str, Any]:
    """Map TOML keys to internal config keys.

    Unknown keys are ignored rather than fatal — a stray key shouldn't stop a
    review — but they warn. Dropping them silently meant a typo like
    ``reasoning_modl`` ran the default model with no indication anything was
    wrong, which is miserable to debug from the outside.
    """
    out: dict[str, Any] = {}
    valid_keys = set(DEFAULT_CONFIG.keys())
    unknown: list[str] = []
    for k, v in raw.items():
        internal = _TOML_KEY_RENAMES.get(k, k)
        if internal in valid_keys:
            out[internal] = v
        else:
            unknown.append(k)

    if unknown:
        where = f" in {source}" if source else ""
        known = ", ".join(sorted(valid_keys | set(_TOML_KEY_RENAMES)))
        warnings.warn(
            f"Ignoring unrecognized config key(s){where}: "
            f"{', '.join(sorted(unknown))}. Recognized keys: {known}.",
            UserWarning,
            stacklevel=3,
        )
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
    "PEERREVIEW_CAVEMAN": "caveman",
    "PEERREVIEW_CONVERSION_GATE": "conversion_gate",
    "PEERREVIEW_OPENAI_BASE_URL": "openai_base_url",
}
_ENV_INT_KEYS = {
    "PEERREVIEW_DEBATE_ROUNDS": "max_debate_rounds",
    "PEERREVIEW_STRICTNESS": "review_strictness",
}
_ENV_BOOL_KEYS = {
    "PEERREVIEW_DESK_SCREEN": "desk_screen",
    "PEERREVIEW_RESEARCH_ENABLED": "research_enabled",
    "PEERREVIEW_SINGLE_MODEL": "single_model",
}
_ENV_FLOAT_KEYS = {
    "PEERREVIEW_TEMPERATURE": "temperature",
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
    for env_key, cfg_key in _ENV_FLOAT_KEYS.items():
        val = os.environ.get(env_key)
        if val:
            try:
                out[cfg_key] = float(val)
            except ValueError:
                pass
    return out


# --- model-table validation ---------------------------------------------------

# The only fields a {provider?, model?, effort?} spec can carry. Anything else
# is a typo (`modle = ...`) that TOML happily parses and resolve_model happily
# ignores, running the default model with no indication anything was wrong.
_SPEC_KEYS = {"provider", "model", "effort"}


def _warn(message: str) -> None:
    # stacklevel points at the get_config caller, matching _normalize_toml.
    warnings.warn(message, UserWarning, stacklevel=4)


def _validate_model_tables(cfg: dict[str, Any], *, explicit_model: bool) -> None:
    """Warn (never crash — same philosophy as _normalize_toml) on model-table
    entries the pipeline can never read.

    Every case here is a real way someone silently paid for the wrong model:
    a ``[models.screen]`` block for a tag no agent resolves through, an
    ``[agent_models]`` key spelled ``data_analysis`` where the agent is
    ``reviewer_data_analysis``, a ``modle = ...`` typo inside a spec, and an
    explicit ``--reasoning-model`` that the tag tables out-rank for every
    tagged agent (In Silico: "minimax reviews nothing, and the run bills the
    lab for Claude").
    """
    # Lazy: the roster imports the agents package, which this module must not
    # pull in at import time (get_config is used by lightweight callers).
    from .panel import KNOWN_AGENTS, KNOWN_TAGS

    models = cfg.get("models") or {}
    agent_models = cfg.get("agent_models") or {}

    for tag, spec in models.items():
        if tag not in KNOWN_TAGS:
            _warn(
                f"[models.{tag}] matches no agent's model tag and is silently "
                f"inert. Known tags: {', '.join(sorted(KNOWN_TAGS))}. (The "
                "desk screen shares the 'reviewer' tag — there is no 'screen' "
                "tag.)"
            )
        _check_spec_keys(f"[models.{tag}]", spec)

    for name, selection in agent_models.items():
        if name not in KNOWN_AGENTS:
            hints = [
                known for known in sorted(KNOWN_AGENTS)
                if known.endswith(f"_{name}")
            ]
            hint = f" Did you mean {' or '.join(repr(h) for h in hints)}?" if hints else ""
            _warn(
                f"[agent_models] entry {name!r} matches no agent and is "
                f"silently inert.{hint} Known agents: "
                f"{', '.join(sorted(KNOWN_AGENTS))}."
            )
        if isinstance(selection, str):
            # A tag name that neither [models] defines nor any agent declares
            # resolves to the empty spec — the override does nothing.
            if selection not in KNOWN_TAGS and selection not in models:
                _warn(
                    f"[agent_models] {name} = {selection!r} names a tag that "
                    "is neither defined under [models] nor a known default "
                    f"tag; the override is silently inert. Known tags: "
                    f"{', '.join(sorted(KNOWN_TAGS))}."
                )
        else:
            _check_spec_keys(f"[agent_models.{name}]", selection)

    if explicit_model and (models or agent_models):
        _warn(
            f"an explicit reasoning_model ({cfg.get('reasoning_model')!r} — "
            "CLI --reasoning-model or a kwargs override) is out-ranked by the "
            "[models]/[agent_models] tables for every tagged agent, so it may "
            "run few agents or none while the tables' models are billed. Pass "
            "--single-model (config: single_model = true) to clear the tables "
            "and run it everywhere."
        )


def _check_spec_keys(where: str, spec: Any) -> None:
    if not isinstance(spec, dict):
        return
    unknown = sorted(set(spec) - _SPEC_KEYS)
    if unknown:
        _warn(
            f"{where} has unrecognized field(s): {', '.join(unknown)}. "
            f"A spec takes only: {', '.join(sorted(_SPEC_KEYS))}. Unknown "
            "fields are ignored, so a typo like 'modle' runs the default "
            "model without complaint."
        )


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
        cfg.update(_normalize_toml(_read_toml(path), source=path))

    cfg.update(_env_overrides())
    cfg.update(overrides)

    # single_model wins over the tables no matter which layer defined them:
    # the whole point of the key is "run the named model everywhere", and it
    # replaces In Silico's manual trick of appending `models = {}` /
    # `agent_models = {}` as a final config layer to flatten its per-agent
    # split for cheap local iteration.
    if cfg.get("single_model"):
        cfg["models"] = {}
        cfg["agent_models"] = {}

    # `overrides` is the only layer whose reasoning_model is knowably
    # explicit (CLI --reasoning-model / --model, library kwargs); a TOML or
    # env model is a default the tables are *meant* to refine.
    _validate_model_tables(
        cfg, explicit_model="reasoning_model" in overrides
    )
    return cfg
