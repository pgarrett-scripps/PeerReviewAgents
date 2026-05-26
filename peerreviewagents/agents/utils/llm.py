"""Provider-agnostic LLM factory.

Reads API keys from the environment (.env supported via python-dotenv) and
returns LangChain chat models. Mirrors TradingAgents' deep/quick split.
"""

from __future__ import annotations

from typing import Any


def _anthropic(model: str, temperature: float, base_url: str | None) -> Any:
    from langchain_anthropic import ChatAnthropic

    kwargs: dict[str, Any] = {"model": model, "temperature": temperature}
    if base_url:
        kwargs["base_url"] = base_url
    return ChatAnthropic(**kwargs)


def _openai(model: str, temperature: float, base_url: str | None) -> Any:
    from langchain_openai import ChatOpenAI

    kwargs: dict[str, Any] = {"model": model, "temperature": temperature}
    if base_url:
        kwargs["base_url"] = base_url
    return ChatOpenAI(**kwargs)


def _google(model: str, temperature: float, base_url: str | None) -> Any:
    from langchain_google_genai import ChatGoogleGenerativeAI

    return ChatGoogleGenerativeAI(model=model, temperature=temperature)


def _ollama(model: str, temperature: float, base_url: str | None) -> Any:
    from langchain_ollama import ChatOllama

    return ChatOllama(model=model, temperature=temperature, base_url=base_url or "http://localhost:11434")


_PROVIDERS = {
    "anthropic": _anthropic,
    "openai": _openai,
    "openrouter": _openai,  # OpenAI-compatible; pass base_url in config
    "google": _google,
    "ollama": _ollama,
}


def make_llm(config: dict, depth: str = "deep") -> Any:
    """Create a chat model. depth is 'deep' or 'quick'."""
    provider = config["provider"]
    if provider not in _PROVIDERS:
        raise ValueError(
            f"Unknown provider '{provider}'. Options: {sorted(_PROVIDERS)}"
        )
    model = config["deep_think_llm"] if depth == "deep" else config["quick_think_llm"]
    base_url = config.get("base_url")
    if provider == "openrouter" and not base_url:
        base_url = "https://openrouter.ai/api/v1"
    return _PROVIDERS[provider](model, config.get("temperature", 0.3), base_url)
