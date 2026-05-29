"""OpenRouter chat-model factory.

Every text agent in the pipeline (reviewers, debaters, meta-reviewer,
integrity panel, editor-in-chief) calls the same model — the
`reasoning_model` slug from config — via OpenRouter's OpenAI-compatible
endpoint. Temperature is fixed at a low, deterministic value because
peer review is judgement, not brainstorming.

The synthesis-heavy agents (meta-reviewer and editor) opt into
`reasoning_effort="high"` so reasoning-capable models spend more
deliberation tokens on the calls that actually need it; the parallel
specialist reviewers and integrity panel run at the default effort.

Streaming is on by default so the TUI can show tokens as they arrive;
`stream_usage` plus OpenRouter's `usage.include` extension surface
token counts and per-call cost via the langchain callback handler in
:mod:`peerreviewagents.observability`.
"""

from __future__ import annotations

import os
from typing import Any

from ...observability import StreamingCallback

_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
_TEMPERATURE = 0.3

# App attribution — OpenRouter uses these to identify the project on its
# leaderboard and (for some providers) for rate-limit accounting.
# https://openrouter.ai/docs/api-reference/overview#headers
_APP_HEADERS = {
    "HTTP-Referer": "https://github.com/pgarrett-scripps/PeerReviewAgents",
    "X-Title": "PeerReviewAgents",
}


def _chat_openrouter(model: str, *, reasoning_effort: str | None = None) -> Any:
    from langchain_openai import ChatOpenAI

    api_key = os.environ.get("OPENROUTER_API_KEY") or os.environ.get("OPENAI_API_KEY")
    # `extra_body` is forwarded verbatim to the OpenRouter request, which is
    # how we pass through OpenRouter-specific fields the OpenAI SDK doesn't
    # know about (reasoning effort + cost-inclusive usage accounting).
    extra_body: dict[str, Any] = {"usage": {"include": True}}
    if reasoning_effort:
        extra_body["reasoning"] = {"effort": reasoning_effort}

    kwargs: dict[str, Any] = {
        "model": model,
        "temperature": _TEMPERATURE,
        "base_url": _OPENROUTER_BASE_URL,
        "default_headers": _APP_HEADERS,
        "extra_body": extra_body,
        # Streaming powers the TUI's live-output panel; the callback below
        # forwards each token to the observability queue.
        "streaming": True,
        "stream_usage": True,
        "callbacks": [StreamingCallback(default_model=model)],
    }
    if api_key:
        kwargs["api_key"] = api_key
    return ChatOpenAI(**kwargs)


def make_llm(config: dict, *, reasoning_effort: str | None = None) -> Any:
    """Return the reasoning model used by every text agent.

    Pass ``reasoning_effort="high"`` for synthesis/judgement agents (the
    meta-reviewer and editor) — it's a no-op on models without a reasoning
    mode and a meaningful quality bump on those that do (o-series,
    Claude extended thinking, DeepSeek-R1, etc.).
    """
    model = config.get("reasoning_model")
    if not model:
        raise ValueError("reasoning_model is not set in config")
    return _chat_openrouter(model, reasoning_effort=reasoning_effort)


def make_vision_llm(config: dict) -> Any:
    """Return the multimodal model used during ingest to describe figures."""
    model = config.get("vision_model")
    if not model:
        raise ValueError("vision_model is not set in config")
    return _chat_openrouter(model)
