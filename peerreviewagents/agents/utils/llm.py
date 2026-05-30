"""LLM factory used by every text agent in the pipeline.

Thin wrapper over :mod:`peerreviewagents.runtime.providers` — the actual
provider dispatch (OpenRouter / OpenAI / Anthropic), cost callbacks, and
reasoning-effort mapping live there. This module exists so call sites
don't reach into runtime/ directly and so the historic ``make_llm``
signature stays stable.

The synthesis-heavy agents (meta-reviewer and editor) pass
``reasoning_effort="high"`` to spend more deliberation tokens on the
calls that actually need it; the parallel specialists run at the
default effort.
"""

from __future__ import annotations

from typing import Any

from ...runtime.providers import make_chat_model


def make_llm(config: dict, *, reasoning_effort: str | None = None) -> Any:
    """Return the reasoning model used by every text agent.

    ``reasoning_effort`` is a no-op on models without a reasoning mode
    and a meaningful quality bump on those that do (o-series, Claude
    extended thinking, DeepSeek-R1, etc.).
    """
    return make_chat_model(config, reasoning_effort=reasoning_effort)
