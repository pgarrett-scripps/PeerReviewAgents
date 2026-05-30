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
    llm = make_chat_model(_cfg("openrouter", "anthropic/claude-opus-4.1"))
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
        _cfg("openrouter", "anthropic/claude-opus-4.1"),
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
    llm = make_chat_model(_cfg("anthropic", "claude-opus-4-7"))
    assert type(llm).__name__ == "ChatAnthropic"


def test_anthropic_extended_thinking(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "stub-key")
    llm = make_chat_model(
        _cfg("anthropic", "claude-opus-4-7"),
        reasoning_effort="high",
    )
    # Extended thinking requires temperature=1 (Anthropic constraint).
    assert llm.temperature == 1.0
    # The thinking block lives on the model as a constructor kwarg.
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
    # — the structured-output layer in Phase C will rely on these.
    for name, spec in PROVIDERS.items():
        assert spec.name == name
        assert isinstance(spec.api_key_env, tuple) and spec.api_key_env
        assert spec.structured_method in ("json_schema", "tool_call", "function_calling")
