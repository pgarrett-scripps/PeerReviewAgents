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
    # A ChatOpenAI *subclass*: the override that keeps OpenRouter's reported
    # usage.cost on the streamed message (see _chat_openrouter_class).
    assert "ChatOpenAI" in {c.__name__ for c in type(llm).__mro__}
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


def test_openrouter_disables_implicit_reasoning_for_ordinary_agents(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "stub-key")
    llm = make_chat_model(_cfg("openrouter", "deepseek/deepseek-v4-flash-0731"))
    reasoning = (getattr(llm, "extra_body", None) or {}).get("reasoning", {})
    assert reasoning == {"effort": "none", "exclude": True}


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
        # Dotted spelling still normalizes; the OpenRouter-slug spelling now
        # fails resolve_model's shape check on the direct provider by design,
        # and its matching is covered by the openrouter temperature test.
        ("claude-opus-4.8", True, True),
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


# --- provider/model-id shape ------------------------------------------------
#
# An OpenRouter id is vendor/model[:tag]; Anthropic and OpenAI direct ids
# never contain "/". A mismatch used to surface as a mid-run 404 after the
# desk screen and half the panel had billed; resolve_model now refuses it
# before any request exists, including for per-tag providers.


def test_openrouter_rejects_a_bare_model_id(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "stub-key")
    with pytest.raises(ValueError, match="not an OpenRouter id"):
        make_chat_model(_cfg("openrouter", "claude-haiku-4-5"))


def test_anthropic_rejects_an_openrouter_slug(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "stub-key")
    with pytest.raises(ValueError, match="no vendor prefix"):
        make_chat_model(_cfg("anthropic", "anthropic/claude-opus-5"))


def test_openai_rejects_an_org_model_id_without_a_gateway(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "stub-key")
    with pytest.raises(ValueError, match="openai_base_url"):
        make_chat_model(_cfg("openai", "meta-llama/llama-3-70b"))


def test_the_shape_check_covers_per_tag_providers(monkeypatch):
    """The whole point of putting it in resolve_model: a [models.reviewer]
    block with its own provider gets the same preflight as the global one."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "stub-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "stub-key")
    cfg = {
        "provider": "openrouter",
        "reasoning_model": "anthropic/claude-opus-5",
        "models": {"reviewer": {"provider": "anthropic",
                                "model": "anthropic/claude-haiku-4.5"}},
    }
    with pytest.raises(ValueError, match="reviewer_rigor"):
        make_chat_model(cfg, agent="reviewer_rigor", default_tag="reviewer")


# --- openai_base_url gateways -----------------------------------------------


def test_openai_base_url_reaches_the_client_and_relaxes_the_shape_check(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "stub-key")
    cfg = _cfg("openai", "meta-llama/llama-3-70b")
    cfg["openai_base_url"] = "http://localhost:11434/v1"
    llm = make_chat_model(cfg)  # org/model is what gateways serve — no raise
    base = str(getattr(llm, "openai_api_base", "") or getattr(llm, "base_url", ""))
    assert base == "http://localhost:11434/v1"


def test_openai_direct_keeps_the_default_endpoint(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "stub-key")
    llm = make_chat_model(_cfg("openai", "gpt-4.1"))
    base = str(getattr(llm, "openai_api_base", "") or getattr(llm, "base_url", "") or "")
    assert "localhost" not in base


def test_openai_o_series_omits_temperature_but_gateway_names_keep_it(monkeypatch):
    """The sampling gate is an OpenAI-name heuristic and must stay one: an
    o-series id 400s on `temperature`, but a gateway model that merely fails
    to match the pattern is not an o-series model and keeps sampling."""
    monkeypatch.setenv("OPENAI_API_KEY", "stub-key")

    o3 = make_chat_model(_cfg("openai", "o3"))
    assert o3.temperature is None

    gpt5 = make_chat_model(_cfg("openai", "gpt-5"))
    assert gpt5.temperature is None

    gpt4 = make_chat_model(_cfg("openai", "gpt-4o"))
    assert gpt4.temperature is not None

    gateway = _cfg("openai", "org/oasis-7b")  # starts with "o" + not o-series
    gateway["openai_base_url"] = "http://localhost:8000/v1"
    assert make_chat_model(gateway).temperature is not None


# --- OpenRouter reported cost survives streaming ------------------------------
#
# The factory requests cost-inclusive usage accounting, and OpenRouter answers
# with the authoritative spend in the usage object's nonstandard `cost` key.
# langchain-openai's streaming path reduces that dict to token counts
# (_create_usage_metadata) and dropped the cost on the floor — a live DeepSeek
# run recorded total_cost_usd: 0.0. The subclass keeps the raw dict on
# response_metadata["token_usage"], the same place the non-streaming path
# already puts it, which is where _call_cost and _extract_usage look.


def _openrouter_llm(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "stub-key")
    return make_chat_model(_cfg("openrouter", "deepseek/deepseek-chat"))


def test_streamed_openrouter_cost_lands_on_response_metadata(monkeypatch):
    from langchain_core.messages import AIMessageChunk

    llm = _openrouter_llm(monkeypatch)
    # The final streamed chunk, as OpenRouter sends it: empty choices, usage
    # carrying the nonstandard `cost` in USD.
    final_chunk = {
        "choices": [],
        "usage": {
            "prompt_tokens": 51_000,
            "completion_tokens": 800,
            "total_tokens": 51_800,
            "cost": 0.0123,
        },
    }
    gen = llm._convert_chunk_to_generation_chunk(final_chunk, AIMessageChunk, None)
    assert gen.message.response_metadata["token_usage"]["cost"] == 0.0123
    # LangChain's normalized counts are untouched.
    assert gen.message.usage_metadata["input_tokens"] == 51_000


def test_a_costless_chunk_is_left_alone(monkeypatch):
    """Ordinary content chunks (and providers that report no cost) must not
    grow a token_usage block that could clash when the chunks merge."""
    from langchain_core.messages import AIMessageChunk

    llm = _openrouter_llm(monkeypatch)
    chunk = {"choices": [{"delta": {"content": "hi"}, "finish_reason": None, "index": 0}]}
    gen = llm._convert_chunk_to_generation_chunk(chunk, AIMessageChunk, None)
    assert "token_usage" not in gen.message.response_metadata


def test_openrouter_reasoning_delta_survives_for_empty_response_diagnostics(monkeypatch):
    from langchain_core.messages import AIMessageChunk

    llm = _openrouter_llm(monkeypatch)
    chunk = {
        "choices": [{
            "delta": {
                "content": "",
                "reasoning": "private chain that must not become the review",
                "reasoning_details": [{"type": "reasoning.text", "text": "private"}],
            },
            "finish_reason": None,
            "index": 0,
        }]
    }
    gen = llm._convert_chunk_to_generation_chunk(chunk, AIMessageChunk, None)
    assert gen.message.content == ""
    assert gen.message.additional_kwargs["openrouter_reasoning"].startswith("private")
    assert len(gen.message.additional_kwargs["openrouter_reasoning_details"]) == 1


def test_openrouter_stream_error_is_not_silently_converted_to_empty_text(monkeypatch):
    from langchain_core.messages import AIMessageChunk

    from peerreviewagents.runtime.providers import OpenRouterStreamError

    llm = _openrouter_llm(monkeypatch)
    chunk = {
        "choices": [{"delta": {"content": ""}, "finish_reason": "error", "index": 0}],
        "error": {
            "code": 502,
            "message": "upstream provider disconnected",
            "metadata": {"provider_name": "ExampleProvider"},
        },
    }
    with pytest.raises(OpenRouterStreamError, match="ExampleProvider.*disconnected"):
        llm._convert_chunk_to_generation_chunk(chunk, AIMessageChunk, None)


def test_the_subclass_still_reads_as_an_openrouter_chat_model(monkeypatch):
    """spec_for_llm and the cache-control probe matched on the literal class
    name "ChatOpenAI"; the subclass must not fall out of either."""
    from peerreviewagents.agents.utils.agent_utils import _cache_control_supported
    from peerreviewagents.runtime.providers import spec_for_llm

    llm = _openrouter_llm(monkeypatch)
    assert spec_for_llm(llm).name == "openrouter"
    assert _cache_control_supported(llm) is True


# ---------------------------------------------------------------------------
# Every call must carry a deadline.
#
# Without one, a provider that stops sending mid-stream hangs the whole run:
# LangGraph's executor waits on pending futures with no timeout on teardown,
# and a future that has started cannot be cancelled. Two consecutive runs were
# lost that way with the panel's work already finished and paid for. These
# assert the deadline exists on every route, because the failure it prevents
# is silent, expensive, and looks exactly like a slow model.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("provider", "model", "env"),
    [
        ("openrouter", "deepseek/deepseek-v4-flash-0731", "OPENROUTER_API_KEY"),
        ("openai", "gpt-4o-mini", "OPENAI_API_KEY"),
        ("anthropic", "claude-haiku-4-5", "ANTHROPIC_API_KEY"),
    ],
)
def test_every_provider_sets_a_request_deadline(monkeypatch, provider, model, env):
    monkeypatch.setenv(env, "stub-key")
    llm = make_chat_model(_cfg(provider, model))

    timeout = getattr(llm, "request_timeout", None)
    if timeout is None:
        timeout = getattr(llm, "default_request_timeout", None)
    assert timeout is not None, f"{provider} builds a client with no timeout"

    # httpx.Timeout on the OpenAI-shaped clients, a bare float on Anthropic,
    # which rejects the structured form. Either way it must be finite.
    read = getattr(timeout, "read", timeout)
    assert read is not None and 0 < float(read) < 600

    # Retries happen in structured._invoke_with_retries, where transport
    # failures can be distinguished from schema failures. Enabling the client
    # retry loop too multiplies attempts and can outlive the enclosing job.
    assert getattr(llm, "max_retries", 0) == 0
