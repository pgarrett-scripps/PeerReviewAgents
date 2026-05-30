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


def _make_openrouter(model: str, *, reasoning_effort: str | None = None) -> Any:
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
        "temperature": _TEMPERATURE,
        "base_url": _OPENROUTER_BASE_URL,
        "default_headers": _OPENROUTER_HEADERS,
        "extra_body": extra_body,
        "streaming": True,
        "stream_usage": True,
        "callbacks": [StreamingCallback(default_model=model)],
    }
    if api_key:
        kwargs["api_key"] = api_key
    return ChatOpenAI(**kwargs)


def _make_openai(model: str, *, reasoning_effort: str | None = None) -> Any:
    from langchain_openai import ChatOpenAI

    api_key = os.environ.get("OPENAI_API_KEY")
    kwargs: dict[str, Any] = {
        "model": model,
        "temperature": _TEMPERATURE,
        "streaming": True,
        "stream_usage": True,
        "callbacks": [StreamingCallback(default_model=model)],
    }
    if reasoning_effort:
        # o-series + GPT-5-class reasoning models accept this top-level.
        # Older models ignore the field on the wire.
        kwargs["reasoning_effort"] = reasoning_effort
    if api_key:
        kwargs["api_key"] = api_key
    return ChatOpenAI(**kwargs)


def _make_anthropic(model: str, *, reasoning_effort: str | None = None) -> Any:
    try:
        from langchain_anthropic import ChatAnthropic
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "The 'anthropic' provider requires `langchain-anthropic`. "
            "Install with: pip install -e ."
        ) from exc

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    # Extended thinking budget per effort tier. The total max_tokens
    # cap must exceed the thinking budget (Anthropic requirement).
    kwargs: dict[str, Any] = {
        "model": model,
        "temperature": _TEMPERATURE,
        "streaming": True,
        "callbacks": [StreamingCallback(default_model=model)],
        "max_tokens": 8192,
    }
    if reasoning_effort:
        budget = _ANTHROPIC_THINKING_BUDGET.get(reasoning_effort, 4096)
        kwargs["thinking"] = {"type": "enabled", "budget_tokens": budget}
        # Extended thinking requires temperature=1.
        kwargs["temperature"] = 1.0
        kwargs["max_tokens"] = budget + 4096
    if api_key:
        kwargs["api_key"] = api_key
    return ChatAnthropic(**kwargs)


_ANTHROPIC_THINKING_BUDGET = {"low": 1024, "medium": 4096, "high": 8192}


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
        factory=_make_anthropic,
        structured_method="tool_call",
        supports_cache_control=True,
        api_key_env=("ANTHROPIC_API_KEY",),
    ),
}


# --- Public API -------------------------------------------------------------


def provider_spec(config: dict) -> ProviderSpec:
    """Return the :class:`ProviderSpec` selected by ``config['provider']``."""
    name = (config.get("provider") or "openrouter").lower()
    spec = PROVIDERS.get(name)
    if spec is None:
        raise ValueError(
            f"unknown provider {name!r}; available: {sorted(PROVIDERS)}"
        )
    return spec


def make_chat_model(config: dict, *, reasoning_effort: str | None = None) -> Any:
    """Build the chat model declared by ``config`` for the active provider.

    Pass ``reasoning_effort="high"`` for synthesis/judgement agents. The
    knob maps to whatever the provider supports: OpenRouter's
    ``reasoning.effort`` field, OpenAI's ``reasoning_effort`` parameter,
    Anthropic's extended-thinking budget. On models without a reasoning
    mode the field is silently ignored.
    """
    spec = provider_spec(config)
    model = config.get("reasoning_model")
    if not model:
        raise ValueError("reasoning_model is not set in config")
    return spec.factory(model, reasoning_effort=reasoning_effort)
