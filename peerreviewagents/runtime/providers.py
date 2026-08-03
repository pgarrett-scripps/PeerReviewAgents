"""LLM provider factory.

Three providers wired up: ``openrouter`` (default — single API key for any
model on the platform), ``anthropic`` (direct Anthropic API), and
``openai`` (direct OpenAI API). Each builds a streaming LangChain
``BaseChatModel`` with the observability callback attached so token /
cost events flow back to the TUI exactly like before.

A :class:`ProviderSpec` per provider declares the structured-output
mechanism the provider prefers and whether it honors
``cache_control: ephemeral`` markers on user-message content blocks.
Downstream code (structured output, prompt cache markup) reads these
flags rather than branching on the provider name directly.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any, Callable, Literal

from ..observability import StreamingCallback

_TEMPERATURE = 0.3

_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
# OpenRouter uses these for project attribution + rate-limit accounting.
# https://openrouter.ai/docs/api-reference/overview#headers
_OPENROUTER_HEADERS = {
    "HTTP-Referer": "https://github.com/pgarrett-scripps/PeerReviewAgents",
    "X-Title": "PeerReviewAgents",
}

StructuredMethod = Literal["json_schema", "tool_call", "function_calling"]


@dataclass(frozen=True)
class ProviderSpec:
    """Static description of a provider's capabilities.

    ``structured_method`` is the preferred method to pass to
    ``llm.with_structured_output(method=...)``. ``supports_cache_control``
    indicates whether the provider tolerates Anthropic-style
    ``cache_control: ephemeral`` markers on user-message content blocks
    (OpenRouter forwards them; Anthropic direct accepts natively;
    OpenAI direct doesn't accept the marker and we'll strip it).
    """

    name: str
    factory: Callable[..., Any]
    structured_method: StructuredMethod
    supports_cache_control: bool
    api_key_env: tuple[str, ...]   # checked in order, first hit wins


# --- Provider factories -----------------------------------------------------


def _make_openrouter(model: str, *, reasoning_effort: str | None = None,
                     node: str | None = None,
                     temperature: float = _TEMPERATURE) -> Any:
    from langchain_openai import ChatOpenAI

    api_key = os.environ.get("OPENROUTER_API_KEY") or os.environ.get("OPENAI_API_KEY")
    # `extra_body` is forwarded verbatim to OpenRouter: cost-inclusive
    # usage accounting + optional reasoning-effort knob for r-series /
    # extended-thinking models routed through OpenRouter.
    extra_body: dict[str, Any] = {"usage": {"include": True}}
    if reasoning_effort:
        extra_body["reasoning"] = {"effort": reasoning_effort}

    kwargs: dict[str, Any] = {
        "model": model,
        "base_url": _OPENROUTER_BASE_URL,
        "default_headers": _OPENROUTER_HEADERS,
        "extra_body": extra_body,
        "streaming": True,
        "stream_usage": True,
        "callbacks": [StreamingCallback(default_model=model, default_node=node)],
    }
    # Current Anthropic models (Opus 5/4.7/4.8, Sonnet 5, Fable/Mythos 5)
    # reject `temperature` outright. The direct-Anthropic factory already
    # omits it for them; routing the same model through OpenRouter has to
    # make the same decision, because the model is what rejects the field,
    # not the route. Whether OpenRouter would strip it for us is not
    # something to rely on — the default model is one of these.
    if not _anthropic_matches(model, _ANTHROPIC_NO_SAMPLING):
        kwargs["temperature"] = temperature
    if api_key:
        kwargs["api_key"] = api_key
    return ChatOpenAI(**kwargs)


def _make_openai(model: str, *, reasoning_effort: str | None = None,
                 node: str | None = None,
                 temperature: float = _TEMPERATURE) -> Any:
    from langchain_openai import ChatOpenAI

    api_key = os.environ.get("OPENAI_API_KEY")
    kwargs: dict[str, Any] = {
        "model": model,
        "temperature": temperature,
        "streaming": True,
        "stream_usage": True,
        "callbacks": [StreamingCallback(default_model=model, default_node=node)],
    }
    if reasoning_effort:
        # o-series + GPT-5-class reasoning models accept this top-level.
        # Older models ignore the field on the wire.
        kwargs["reasoning_effort"] = reasoning_effort
    if api_key:
        kwargs["api_key"] = api_key
    return ChatOpenAI(**kwargs)


def _make_anthropic(model: str, *, reasoning_effort: str | None = None,
                    node: str | None = None,
                    temperature: float = _TEMPERATURE) -> Any:
    try:
        from langchain_anthropic import ChatAnthropic
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "The 'anthropic' provider requires `langchain-anthropic`. "
            "Install with: pip install -e ."
        ) from exc

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    kwargs: dict[str, Any] = {
        "model": model,
        "streaming": True,
        "stream_usage": True,
        "callbacks": [StreamingCallback(default_model=model, default_node=node)],
        # Output is billed per token generated, not per token allowed, so a
        # generous cap costs nothing until it is used — but hitting it costs
        # the whole call. A reviewer truncated mid-schema produces an
        # unparseable tool call, fails its retry, and drops off the panel
        # entirely: measured on C-09, where the rigor reviewer vanished and
        # the editor decided on seven verdicts instead of eight. 8192 was too
        # close for a structured review carrying a claim ledger, and the
        # reviewer prompt now asks for the load-bearing critique at length,
        # which makes the answers longer still.
        "max_tokens": 16000,
    }

    adaptive = _anthropic_matches(model, _ANTHROPIC_ADAPTIVE_EFFORT)
    rejects_sampling = _anthropic_matches(model, _ANTHROPIC_NO_SAMPLING)

    # The newest reasoning models (Opus 4.7/4.8, Sonnet 5, Fable/Mythos 5)
    # 400 on any `temperature`; older models still accept it. Omit it there.
    if not rejects_sampling:
        kwargs["temperature"] = temperature

    if adaptive:
        # Opus 4.6+ / Sonnet 4.6+ / Fable: adaptive thinking + `effort` knob
        # (the fixed `budget_tokens` budget was removed and now 400s). We
        # leave thinking OFF unless an effort is explicitly requested, so the
        # parallel specialist reviewers don't spend extra thinking tokens —
        # only the synthesis agents (meta-reviewer, editor) pass an effort.
        if reasoning_effort:
            kwargs["thinking"] = {"type": "adaptive"}
            kwargs["effort"] = reasoning_effort  # low | medium | high
            kwargs["max_tokens"] = 16000  # room for thinking + the answer
    elif reasoning_effort:
        # Legacy models (Haiku 4.5, Sonnet 4.5, and older): fixed thinking
        # budget. Extended thinking requires temperature=1 and a max_tokens
        # cap above the budget.
        budget = _ANTHROPIC_THINKING_BUDGET.get(reasoning_effort, 4096)
        kwargs["thinking"] = {"type": "enabled", "budget_tokens": budget}
        kwargs["temperature"] = 1.0
        kwargs["max_tokens"] = budget + 4096

    if api_key:
        kwargs["api_key"] = api_key
    return ChatAnthropic(**kwargs)


_ANTHROPIC_THINKING_BUDGET = {"low": 1024, "medium": 4096, "high": 8192}

# Direct-Anthropic model families (matched as normalized substrings, so both
# "claude-opus-4-8" and "claude-opus-4.8" hit). ``_ANTHROPIC_ADAPTIVE_EFFORT``
# = models that use adaptive thinking + the `effort` knob rather than a fixed
# `budget_tokens` budget. ``_ANTHROPIC_NO_SAMPLING`` = the subset that also
# rejects `temperature`/`top_p`/`top_k` outright (400). Everything not listed
# is treated as legacy (temperature ok, thinking via `budget_tokens`).
_ANTHROPIC_ADAPTIVE_EFFORT: tuple[str, ...] = (
    "opus-4-6", "opus-4-7", "opus-4-8", "opus-5",
    "sonnet-4-6", "sonnet-5",
    "fable-5", "mythos-5",
)
_ANTHROPIC_NO_SAMPLING: tuple[str, ...] = (
    "opus-4-7", "opus-4-8", "opus-5", "sonnet-5", "fable-5", "mythos-5",
)

# Families whose whole line is adaptive, whatever version ships next.
_ANTHROPIC_ADAPTIVE_FAMILIES: tuple[str, ...] = ("claude-fable", "claude-mythos")

# First generation that dropped fixed thinking budgets and sampling params.
_ANTHROPIC_ADAPTIVE_FROM: tuple[int, int] = (4, 7)

_ANTHROPIC_VERSION_RE = re.compile(r"claude-(?:opus|sonnet|haiku)-(\d+)(?:[.-](\d+))?")


def _anthropic_version(model: str) -> tuple[int, int] | None:
    """Best-effort ``(major, minor)`` from a modern Anthropic model id.

    ``None`` for ids we can't parse, including the legacy version-first
    spelling (``claude-3-5-sonnet-...``) — those all predate the change, so
    falling through to the permissive path is correct.
    """
    match = _ANTHROPIC_VERSION_RE.search(model.lower().rsplit("/", 1)[-1])
    if not match:
        return None
    return int(match.group(1)), int(match.group(2) or 0)


def _anthropic_matches(model: str, needles: tuple[str, ...]) -> bool:
    """Whether ``model`` is in the given generation bucket.

    The explicit needle list stays authoritative, but a bare substring list
    goes stale the moment a model ships: `claude-opus-5` matched neither
    tuple, so it was treated as legacy and sent `temperature` plus
    `budget_tokens` — a 400 on both counts. The version parse is the
    backstop, so a future `claude-opus-6` is handled without an edit here.
    """
    normalized = model.lower().replace(".", "-")
    if any(n in normalized for n in needles):
        return True
    if any(normalized.startswith(f) for f in _ANTHROPIC_ADAPTIVE_FAMILIES):
        return True
    version = _anthropic_version(model)
    return version is not None and version >= _ANTHROPIC_ADAPTIVE_FROM


# --- Registry ---------------------------------------------------------------


PROVIDERS: dict[str, ProviderSpec] = {
    "openrouter": ProviderSpec(
        name="openrouter",
        factory=_make_openrouter,
        structured_method="tool_call",
        supports_cache_control=True,
        api_key_env=("OPENROUTER_API_KEY", "OPENAI_API_KEY"),
    ),
    "openai": ProviderSpec(
        name="openai",
        factory=_make_openai,
        structured_method="json_schema",
        supports_cache_control=False,
        api_key_env=("OPENAI_API_KEY",),
    ),
    "anthropic": ProviderSpec(
        name="anthropic",
        # Anthropic's native strict structured outputs enforce field types at
        # the API level — the model cannot return a bulleted string where the
        # schema wants list[str]. `function_calling` (the langchain default)
        # relies on the model formatting arrays correctly, which weaker models
        # (Haiku) fail on long manuscript prompts.
        factory=_make_anthropic,
        structured_method="json_schema",
        supports_cache_control=True,
        api_key_env=("ANTHROPIC_API_KEY",),
    ),
}


# --- Model tags / per-agent resolution --------------------------------------


@dataclass(frozen=True)
class ModelSpec:
    """A fully-resolved model choice for one agent: which provider, which
    model string, and an optional default reasoning effort."""

    provider: str
    model: str
    effort: str | None = None


def spec_for_provider(name: str | None) -> ProviderSpec:
    """Return the :class:`ProviderSpec` for a provider name."""
    key = (name or "openrouter").lower()
    spec = PROVIDERS.get(key)
    if spec is None:
        raise ValueError(
            f"unknown provider {key!r}; available: {sorted(PROVIDERS)}"
        )
    return spec


def provider_spec(config: dict) -> ProviderSpec:
    """Return the :class:`ProviderSpec` for the global ``config['provider']``.

    Kept for callers that only need the run-wide provider. Per-agent code
    paths should use :func:`spec_for_llm` so the structured-output method and
    cache-control flags match the model that agent actually built.
    """
    return spec_for_provider(config.get("provider"))


def spec_for_llm(llm: Any) -> ProviderSpec:
    """Return the :class:`ProviderSpec` for an already-built chat model.

    Inferred from the instance (class + base_url) rather than from
    ``config['provider']``, so it stays correct when different agents run on
    different providers via model tags.
    """
    cls = type(llm).__name__
    if cls == "ChatAnthropic":
        return PROVIDERS["anthropic"]
    if cls == "ChatOpenAI":
        base = str(
            getattr(llm, "openai_api_base", "")
            or getattr(llm, "base_url", "")
            or ""
        )
        return PROVIDERS["openrouter"] if "openrouter" in base.lower() else PROVIDERS["openai"]
    return PROVIDERS["openrouter"]


def resolve_model(
    config: dict,
    *,
    agent: str | None = None,
    default_tag: str = "default",
) -> ModelSpec:
    """Resolve the model an ``agent`` should use, honoring model tags.

    Resolution order:

    1. ``config['agent_models'][agent]`` — a tag name (str) or an inline
       ``{provider?, model?, effort?}`` spec — wins if present.
    2. Otherwise the agent's code-declared ``default_tag`` (e.g. every
       synthesis agent shares ``"synthesis"``), looked up in
       ``config['models']``.
    3. Any field the chosen tag/spec leaves unset falls back to the global
       ``config['provider']`` / ``config['reasoning_model']``.

    So with no ``[models]`` / ``[agent_models]`` configured, every agent
    resolves to the single global model exactly as before. Defining a group
    tag (``[models.synthesis]``) retargets that whole group; ``[agent_models]``
    overrides one agent.
    """
    agent_models: dict = config.get("agent_models") or {}
    selection = agent_models.get(agent, default_tag) if agent else default_tag

    if isinstance(selection, dict):
        raw = selection
    else:
        raw = (config.get("models") or {}).get(selection) or {}

    provider = raw.get("provider") or config.get("provider") or "openrouter"
    model = raw.get("model") or config.get("reasoning_model")
    if not model:
        raise ValueError("no model resolved: set reasoning_model or a model tag")
    return ModelSpec(provider=provider, model=model, effort=_effort(raw.get("effort")))


# Config spellings for "no thinking at all". Without one of these there is no
# way to turn thinking OFF from a config file: an absent `effort` key means
# "unset", which falls through to the agent's call-site default, so the only
# expressible choices were which level to think at. Thinking is billed at
# output rates, so "none" has to be sayable.
_EFFORT_OFF = {"off", "none", "no", "false", "0"}


def _effort(value: object) -> str | None:
    """Normalize a configured effort. ``""``/absent -> None (use the call-site
    default); an off-spelling -> ``"off"``, which suppresses thinking."""
    if value is None:
        return None
    text = str(value).strip().lower()
    if not text:
        return None
    return "off" if text in _EFFORT_OFF else text


# --- Public API -------------------------------------------------------------


def make_chat_model(
    config: dict,
    *,
    agent: str | None = None,
    default_tag: str = "default",
    reasoning_effort: str | None = None,
) -> Any:
    """Build the chat model an ``agent`` should use.

    ``agent`` + ``default_tag`` select the model via :func:`resolve_model`
    (model tags). With neither ``[models]`` nor ``[agent_models]`` configured,
    this collapses to the single global ``provider`` / ``reasoning_model``.

    ``reasoning_effort`` passed here (what synthesis agents do today) takes
    precedence over any ``effort`` declared on the resolved tag. The knob maps
    to whatever the provider supports (OpenRouter ``reasoning.effort``, OpenAI
    ``reasoning_effort``, Anthropic adaptive-thinking effort) and is ignored on
    non-reasoning models.
    """
    spec = resolve_model(config, agent=agent, default_tag=default_tag)
    prov = spec_for_provider(spec.provider)
    # `reasoning_effort` from the call site is the agent's *default*, and an
    # explicit `effort` on the resolved tag or agent override wins over it.
    # The precedence used to run the other way, which made the call-site value
    # unreachable from configuration: an `effort` key in peerreview.toml was
    # silently inert for every synthesis agent, and changing how hard the
    # editor thinks meant editing Python. Thinking tokens are billed at output
    # rates, so that is a cost knob that has to be reachable from config.
    effort = spec.effort if spec.effort is not None else reasoning_effort
    if effort == "off":
        effort = None  # configured to think at all
    temp = config.get("temperature")
    temp = _TEMPERATURE if temp is None else float(temp)
    # The agent name rides along so usage events can be attributed even
    # when LangChain dispatches the callback off the node's own thread,
    # where the thread-local `current_node()` reads empty.
    return prov.factory(
        spec.model, reasoning_effort=effort, temperature=temp, node=agent
    )
