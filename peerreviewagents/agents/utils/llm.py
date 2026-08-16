"""LLM factory used by every text agent in the pipeline.

Thin wrapper over :mod:`peerreviewagents.runtime.providers` — the actual
provider dispatch (OpenRouter / OpenAI / Anthropic), cost callbacks, and
reasoning-effort mapping live there. This module exists so call sites
don't reach into runtime/ directly and so the historic ``make_llm``
signature stays stable.

Hidden reasoning is opt-in through model-tag configuration. Call sites do not
force it: a provider can otherwise consume the entire completion budget as
hidden reasoning and return no review at all.
"""

from __future__ import annotations

from typing import Any

from ...runtime.providers import make_chat_model


def make_llm(
    config: dict,
    *,
    agent: str | None = None,
    default_tag: str = "default",
    reasoning_effort: str | None = None,
) -> Any:
    """Return the reasoning model used by a text agent.

    ``agent`` (a stable key like ``"editor"`` or ``"reviewer_methodology"``)
    plus ``default_tag`` select the model via config model tags — see
    :func:`peerreviewagents.runtime.providers.resolve_model`. With no
    ``[models]`` / ``[agent_models]`` configured, every agent falls back to
    the single global ``provider`` / ``reasoning_model``, so existing configs
    are unchanged.

    ``reasoning_effort`` is a no-op on models without a reasoning mode and a
    meaningful quality bump on those that do (o-series, Claude adaptive
    thinking, DeepSeek-R1, etc.); when set it overrides any ``effort`` on the
    resolved tag.
    """
    return make_chat_model(
        config,
        agent=agent,
        default_tag=default_tag,
        reasoning_effort=reasoning_effort,
    )
