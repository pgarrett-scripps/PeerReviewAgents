"""Provider factory smoke tests.

Builds each provider's chat model with a stub API key and asserts the
correct LangChain class + provider-specific configuration without making
any real API call. The OpenAI direct path needs OPENAI_API_KEY *not* to
be confused with OPENROUTER_API_KEY, so the openrouter check explicitly
exercises the OpenRouter base URL.
"""

from __future__ import annotations

import pytest

from peerreviewagents.runtime.providers import (
    PROVIDERS,
    make_chat_model,
    provider_spec,
)


def _cfg(provider: str, model: str = "test-model") -> dict:
    return {"provider": provider, "reasoning_model": model}


def test_default_provider_is_openrouter():
    spec = provider_spec({"reasoning_model": "x"})
    assert spec.name == "openrouter"


def test_unknown_provider_raises():
    with pytest.raises(ValueError, match="unknown provider"):
        provider_spec({"provider": "bogus", "reasoning_model": "x"})


def test_openrouter_factory(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "stub-key")
    llm = make_chat_model(_cfg("openrouter", "anthropic/claude-opus-5"))
    assert type(llm).__name__ == "ChatOpenAI"
    # Base URL is what makes this OpenRouter rather than OpenAI direct.
    base = (
        getattr(llm, "openai_api_base", None)
        or getattr(llm, "base_url", None)
        or ""
    )
    assert "openrouter" in str(base).lower()


def test_openrouter_passes_reasoning_effort(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "stub-key")
    llm = make_chat_model(
        _cfg("openrouter", "anthropic/claude-opus-5"),
        reasoning_effort="high",
    )
    extra = getattr(llm, "extra_body", None) or {}
    assert extra.get("reasoning", {}).get("effort") == "high"
    # OpenRouter cost field must be requested or _call_cost stays 0.
    assert extra.get("usage", {}).get("include") is True


def test_openai_direct_factory(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "stub-key")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    llm = make_chat_model(_cfg("openai", "gpt-4.1"))
    assert type(llm).__name__ == "ChatOpenAI"
    # OpenAI direct: no custom base_url override (LangChain uses the
    # default api.openai.com endpoint).
    base = (
        getattr(llm, "openai_api_base", None)
        or getattr(llm, "base_url", None)
        or ""
    )
    assert "openrouter" not in str(base).lower()


def test_anthropic_direct_factory(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "stub-key")
    llm = make_chat_model(_cfg("anthropic", "claude-opus-5"))
    assert type(llm).__name__ == "ChatAnthropic"


def test_anthropic_adaptive_thinking(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "stub-key")
    # Current models (Opus 5/Sonnet 5/Fable 5): adaptive thinking + the effort
    # knob, and NO sampling temperature (the API 400s on it).
    llm = make_chat_model(
        _cfg("anthropic", "claude-opus-5"),
        reasoning_effort="high",
    )
    assert llm.temperature is None
    assert getattr(llm, "thinking", None) == {"type": "adaptive"}
    assert getattr(llm, "effort", None) == "high"


def test_anthropic_legacy_thinking(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "stub-key")
    # Older models (Haiku 4.5, Sonnet 4.5, and earlier) keep the fixed-budget
    # extended-thinking path, which requires temperature=1.
    llm = make_chat_model(
        _cfg("anthropic", "claude-haiku-4-5"),
        reasoning_effort="high",
    )
    assert llm.temperature == 1.0
    thinking = getattr(llm, "thinking", None)
    assert isinstance(thinking, dict)
    assert thinking.get("type") == "enabled"
    assert thinking.get("budget_tokens", 0) > 0


def test_missing_model_raises(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "stub-key")
    with pytest.raises(ValueError, match="reasoning_model"):
        make_chat_model({"provider": "openrouter"})


def test_spec_table_consistency():
    # Every spec must declare an api_key_env tuple and a structured method
    # — the structured-output layer in Phase C will rely on these. Only the
    # two names LangChain's with_structured_output accepts count: the
    # openrouter spec said "tool_call" for months, every bind raised
    # ValueError, and the declared preference was silently dead code.
    for name, spec in PROVIDERS.items():
        assert spec.name == name
        assert isinstance(spec.api_key_env, tuple) and spec.api_key_env
        assert spec.structured_method in ("json_schema", "function_calling")


def test_declared_structured_methods_actually_bind(monkeypatch):
    """The regression the spec table alone can't catch: the declared method
    has to be one the built model's with_structured_output accepts, or the
    silent fallback in structured._bind swallows the ValueError and the
    preference never applies."""
    from peerreviewagents.agents.schemas import ReviewerOutput

    monkeypatch.setenv("OPENROUTER_API_KEY", "stub-key")
    monkeypatch.setenv("OPENAI_API_KEY", "stub-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "stub-key")
    models = {
        "openrouter": "anthropic/claude-opus-5",
        "openai": "gpt-4.1",
        "anthropic": "claude-opus-5",
    }
    for name, spec in PROVIDERS.items():
        llm = make_chat_model(_cfg(name, models[name]))
        # Raises ValueError on an unrecognized method name.
        llm.with_structured_output(
            ReviewerOutput, method=spec.structured_method, include_raw=True
        )


# --- generation matching ----------------------------------------------------
#
# Two independent properties, and they changed in different releases:
#   adaptive thinking  — Opus/Sonnet 4.6 and newer
#   rejects sampling   — 4.7 and newer (4.6 still accepts `temperature`)
#
# The needle lists are substring matches and go stale the moment a model
# ships: `claude-opus-5` matched neither, so it was treated as legacy and sent
# both `temperature` and `budget_tokens` — a 400 on each. A version parse
# backstops the lists so the next release is handled without an edit.


@pytest.mark.parametrize(
    "model,adaptive,rejects_sampling",
    [
        ("claude-opus-5", True, True),
        ("claude-opus-4-8", True, True),
        ("claude-opus-4-7", True, True),
        ("claude-sonnet-5", True, True),
        ("claude-fable-5", True, True),
        ("anthropic/claude-opus-4.8", True, True),
        ("claude-opus-6", True, True),            # unreleased — must not regress
        ("claude-opus-4-6", True, False),         # adaptive, but sampling still ok
        ("claude-sonnet-4-6", True, False),
        ("claude-haiku-4-5", False, False),
        ("claude-haiku-4-5-20251001", False, False),  # dated snapshot
        ("claude-3-5-sonnet-20241022", False, False),   # legacy version-first id
    ],
)
def test_anthropic_generation_matching(monkeypatch, model, adaptive, rejects_sampling):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "stub-key")
    llm = make_chat_model(_cfg("anthropic", model), reasoning_effort="high")
    thinking = getattr(llm, "thinking", None) or {}

    if adaptive:
        assert thinking.get("type") == "adaptive", f"{model} should think adaptively"
        assert "budget_tokens" not in thinking
    else:
        assert thinking.get("budget_tokens", 0) > 0, f"{model} should use a budget"

    if rejects_sampling:
        assert llm.temperature is None, f"{model} must not send temperature"
    else:
        assert llm.temperature is not None, f"{model} still accepts temperature"


def test_opus_5_payload_omits_rejected_params(monkeypatch):
    """The specific regression: Opus 5 fell through to the legacy path."""
    from langchain_core.messages import HumanMessage

    monkeypatch.setenv("ANTHROPIC_API_KEY", "stub-key")
    llm = make_chat_model(_cfg("anthropic", "claude-opus-5"), reasoning_effort="xhigh")
    payload = llm._get_request_payload([HumanMessage("hi")])
    assert "temperature" not in payload
    assert payload["thinking"] == {"type": "adaptive"}


def test_openrouter_omits_temperature_for_current_anthropic(monkeypatch):
    """The default route: OpenRouter + Opus 5, which 400s on `temperature`.

    The rejecting party is the model, not the provider — so the OpenRouter
    factory has to make the same call the direct-Anthropic one does.
    """
    monkeypatch.setenv("OPENROUTER_API_KEY", "stub-key")
    llm = make_chat_model(_cfg("openrouter", "anthropic/claude-opus-5"))
    assert llm.temperature is None

    # A model that still accepts sampling must keep it.
    legacy = make_chat_model(_cfg("openrouter", "anthropic/claude-haiku-4.5"))
    assert legacy.temperature is not None

    # Non-Anthropic slugs that accept sampling are unaffected.
    other = make_chat_model(_cfg("openrouter", "openai/gpt-4o"))
    assert other.temperature is not None


# The same field, rejected by a different vendor's models. OpenAI's o-series
# and GPT-5 reasoning line 400 on `temperature` rather than ignoring it, and
# the direct-OpenAI factory used to set it unconditionally — the one route
# that had not learned what the other two already gated per model.


@pytest.mark.parametrize(
    "model,rejects",
    [
        ("o1", True),
        ("o3", True),
        ("o3-mini", True),
        ("o4-mini", True),
        ("gpt-5", True),
        ("gpt-5-mini", True),
        ("gpt-4.1", False),
        ("gpt-4o", False),
    ],
)
def test_openai_direct_temperature_gating(monkeypatch, model, rejects):
    monkeypatch.setenv("OPENAI_API_KEY", "stub-key")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    llm = make_chat_model(_cfg("openai", model))
    if rejects:
        # LangChain pins o1 to 1.0 (the one value the API tolerates) on its
        # own; what must never arrive is the run's configured sampling value.
        assert llm.temperature in (None, 1.0), f"{model} must not send temperature"
    else:
        assert llm.temperature == 0.3, f"{model} still accepts temperature"


def test_openrouter_omits_temperature_for_openai_reasoning_models(monkeypatch):
    """The model rejects the field whichever route carries it, so an
    OpenRouter slug has to reach the same verdict as the direct route."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "stub-key")
    llm = make_chat_model(_cfg("openrouter", "openai/o3-mini"))
    assert llm.temperature is None
