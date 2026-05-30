"""Structured-output helper for agents.

Wraps ``llm.with_structured_output(schema)`` to:

  * pick the provider's preferred extraction method
    (``ProviderSpec.structured_method`` — ``json_schema`` for OpenAI
    direct, ``function_calling`` everywhere else);
  * capture cost via ``include_raw=True`` so the per-call cost line in
    the TUI / summary still works;
  * retry once with a sharpened prompt on schema-validation failure
    before letting the exception bubble up to the agent's existing
    try/except.

Two entry points:

  * :func:`invoke_structured` — one shot, no tool loop. Used by agents
    that don't call external tools (every agent today except novelty +
    literature reviewers).
  * :func:`invoke_structured_after_tools` — run the existing tool loop
    to free text, then a second structured call extracts the schema.
    Two LLM calls; cost is summed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel

from ...runtime.providers import provider_spec
from .agent_utils import (
    _cache_control_supported,
    _call_cost,
    _user_message,
    run_agent,
)


@dataclass(frozen=True)
class StructuredResult:
    """What every structured call returns: parsed instance + summed cost."""

    instance: BaseModel
    cost: float


def invoke_structured(
    llm,
    schema: type[BaseModel],
    config: dict,
    system_prompt: str,
    user_prompt: str,
    *,
    cached_prefix: str | None = None,
) -> StructuredResult:
    """One-shot structured call. No tool loop.

    Raises ``ValueError`` if the schema can't be filled after a retry —
    agents catch this in their existing ``except`` block and report a
    node-level error.
    """
    use_cache = _cache_control_supported(llm)
    messages = [
        SystemMessage(content=system_prompt),
        _user_message(user_prompt, cached_prefix, with_cache_marker=use_cache),
    ]
    return _try_structured(llm, schema, messages, config=config)


def invoke_structured_after_tools(
    llm,
    schema: type[BaseModel],
    config: dict,
    system_prompt: str,
    user_prompt: str,
    tools: list,
    *,
    cached_prefix: str | None = None,
) -> StructuredResult:
    """Tool-loop reviewers: run :func:`run_agent` for free text, then a
    second call extracts the schema from that text.

    Two LLM calls; cost is summed. This avoids the awkward interaction
    between tool-calling and structured-output binding (LangChain wraps
    both as tool calls, and combining them gets brittle across providers).
    """
    free = run_agent(llm, system_prompt, user_prompt, tools, cached_prefix=cached_prefix)
    extraction_sys = (
        "Convert the assistant text below into a structured JSON object "
        "matching the given schema. Preserve every concrete claim verbatim; "
        "do not invent new content."
    )
    messages = [
        SystemMessage(content=extraction_sys),
        HumanMessage(content=free.text),
    ]
    extracted = _try_structured(llm, schema, messages, config=config)
    return StructuredResult(
        instance=extracted.instance,
        cost=free.cost + extracted.cost,
    )


def _try_structured(
    llm,
    schema: type[BaseModel],
    messages: list,
    *,
    config: dict,
) -> StructuredResult:
    spec = provider_spec(config)
    structured = _bind(llm, schema, spec.structured_method)
    result = structured.invoke(messages)
    parsed, cost = _unpack(result)
    if parsed is not None:
        return StructuredResult(instance=parsed, cost=cost)

    # Retry once with a sharper prompt; some models miss the schema on
    # the first try when the user prompt is long.
    retry_msg = HumanMessage(content=(
        f"Your previous response did not produce a valid {schema.__name__}. "
        "Respond again, strictly matching the schema. No prose, no extra fields."
    ))
    result2 = structured.invoke(messages + [retry_msg])
    parsed2, cost2 = _unpack(result2)
    if parsed2 is not None:
        return StructuredResult(instance=parsed2, cost=cost + cost2)

    err = _parsing_error(result2)
    raise ValueError(f"structured-output validation failed after retry: {err}")


def _bind(llm, schema: type[BaseModel], method: str):
    """Wrap llm in a structured-output chain. Falls back gracefully if
    the provider doesn't support the requested method or ``include_raw``
    (older LangChain versions / non-standard providers)."""
    try:
        return llm.with_structured_output(schema, method=method, include_raw=True)
    except (TypeError, ValueError):
        pass
    try:
        return llm.with_structured_output(schema, include_raw=True)
    except (TypeError, ValueError):
        pass
    return llm.with_structured_output(schema)


def _unpack(result: Any) -> tuple[BaseModel | None, float]:
    """Extract (parsed_instance, cost_usd) from either include_raw dict
    or a bare parsed instance."""
    if isinstance(result, dict):
        parsed = result.get("parsed")
        raw = result.get("raw")
        cost = _call_cost(raw) if raw is not None else 0.0
        return parsed, cost
    if isinstance(result, BaseModel):
        return result, 0.0
    return None, 0.0


def _parsing_error(result: Any) -> Any:
    if isinstance(result, dict):
        return result.get("parsing_error") or "unknown"
    return repr(result)
