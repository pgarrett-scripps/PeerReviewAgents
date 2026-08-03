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

import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel

from ...observability import AgentEvent, current_node, emit
from ...runtime.providers import spec_for_llm
from .agent_utils import (
    DEFAULT_CACHE_TTL,
    _build_messages,
    _cache_control_supported,
    _call_cost,
    run_agent,
)

# Transient provider/transport failures (e.g. OpenRouter "Provider returned
# error", rate limits, dropped connections) surface as exceptions from
# ``structured.invoke``. Retry the call a few times with linear backoff
# before letting it bubble up to the agent's node-level error handler, so a
# single upstream blip doesn't silently drop an agent from the run.
_MAX_PROVIDER_ATTEMPTS = 3
_RETRY_BACKOFF_S = 2.0


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
    cached_prefix: str | Sequence[str] | None = None,
) -> StructuredResult:
    """One-shot structured call. No tool loop.

    Raises ``ValueError`` if the schema can't be filled after a retry —
    agents catch this in their existing ``except`` block and report a
    node-level error.
    """
    messages = _build_messages(
        system_prompt, user_prompt, cached_prefix,
        cache_supported=_cache_control_supported(llm),
        cache_ttl=config.get("cache_ttl") or DEFAULT_CACHE_TTL,
    )
    return _try_structured(llm, schema, messages, config=config)


def invoke_structured_after_tools(
    llm,
    schema: type[BaseModel],
    config: dict,
    system_prompt: str,
    user_prompt: str,
    tools: list,
    *,
    cached_prefix: str | Sequence[str] | None = None,
) -> StructuredResult:
    """Tool-loop reviewers: run :func:`run_agent` for free text, then a
    second call extracts the schema from that text.

    Two LLM calls; cost is summed. This avoids the awkward interaction
    between tool-calling and structured-output binding (LangChain wraps
    both as tool calls, and combining them gets brittle across providers).

    If the tool loop itself fails (a provider rejecting the tool request —
    e.g. the Sonnet-only ``tools.0 function`` 400 — a tool vendor being down,
    or a rate limit), we fall back to a tools-free structured pass so this
    reviewer still contributes a verdict from the manuscript alone rather than
    dropping out of the panel. The fallback is logged so the lost web-search
    grounding is visible.

    **An empty first call takes the same route, and must.** The extraction
    prompt asks a model to convert "the assistant text below"; hand it nothing
    and a well-behaved model answers the prompt it was actually given. Observed
    on nvidia/nemotron-3-ultra, which is a reasoning model and returned its
    whole response as reasoning tokens with empty content:

        # Novelty & Contribution Reviewer
        ## Summary
        The user requested conversion of assistant text to JSON per a schema,
        but neither the source text nor the schema were included.
        ## Weaknesses
        - Missing required inputs: assistant text and schema

    That validated against ReviewerOutput, scored the paper 1/5, and flowed
    into the panel mean and the editor's verdict. Nothing caught it: no
    exception, no schema error, ``scored_count`` 8 of 8. Two of the three
    tool-using agents failed this way in one run and the review published a
    fabricated score about real authors' work.

    So a blank response is a failed call, not a response. It is the same
    failure as the tool loop raising, and it takes the same fallback.
    """
    def _without_tools(reason: str) -> StructuredResult:
        emit(AgentEvent(
            kind="log",
            node=current_node(),
            text=f"{reason}; reviewing without research tools",
        ))
        return invoke_structured(
            llm, schema, config, system_prompt, user_prompt, cached_prefix=cached_prefix
        )

    try:
        free = run_agent(
            llm, system_prompt, user_prompt, tools,
            cached_prefix=cached_prefix,
            cache_ttl=config.get("cache_ttl") or DEFAULT_CACHE_TTL,
        )
    except Exception as exc:  # noqa: BLE001
        return _without_tools(f"tool loop failed ({type(exc).__name__})")

    if not (free.text or "").strip():
        return _without_tools("tool loop returned no text")

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
    spec = spec_for_llm(llm)
    structured = _bind(llm, schema, spec.structured_method)
    result = _invoke_with_retries(structured, messages)
    parsed, cost = _unpack(result)
    if parsed is not None:
        return StructuredResult(instance=parsed, cost=cost)

    # Retry once with a sharper prompt; some models miss the schema on
    # the first try when the user prompt is long.
    retry_msg = HumanMessage(content=(
        f"Your previous response did not produce a valid {schema.__name__}. "
        "Respond again, strictly matching the schema. No prose, no extra fields."
    ))
    result2 = _invoke_with_retries(structured, messages + [retry_msg])
    parsed2, cost2 = _unpack(result2)
    if parsed2 is not None:
        return StructuredResult(instance=parsed2, cost=cost + cost2)

    err = _parsing_error(result2)
    raise ValueError(f"structured-output validation failed after retry: {err}")


def _invoke_with_retries(structured, messages: list) -> Any:
    """Invoke a structured-output chain, retrying transient provider errors.

    Retries up to ``_MAX_PROVIDER_ATTEMPTS`` times on any exception raised by
    ``invoke`` (provider/transport failures), with linear backoff between
    attempts. The final exception is re-raised if every attempt fails, so the
    agent's existing try/except still records a node-level error. Schema
    *validation* failures don't raise here (they return ``parsed=None``) and
    are handled separately by the caller's sharpened-prompt retry.
    """
    last_exc: Exception | None = None
    for attempt in range(_MAX_PROVIDER_ATTEMPTS):
        try:
            return structured.invoke(messages)
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt < _MAX_PROVIDER_ATTEMPTS - 1:
                time.sleep(_RETRY_BACKOFF_S * (attempt + 1))
    assert last_exc is not None
    raise last_exc


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
    if not isinstance(result, dict):
        return repr(result)
    # A response cut off at max_tokens arrives here as an empty or half-written
    # tool call, and the parser reports it as "Invalid json output:" with
    # nothing after the colon — which reads like the model ignored the schema
    # when in fact it ran out of room mid-answer. Naming it costs nothing and
    # is the difference between a fixable finding and a mystery.
    if _stop_reason(result.get("raw")) == "max_tokens":
        return (
            "response hit the max_tokens cap before the schema was complete "
            "(raise max_tokens for this agent, or ask it for less)"
        )
    return result.get("parsing_error") or "unknown"


def _stop_reason(raw: Any) -> str:
    meta = getattr(raw, "response_metadata", None) or {}
    return str(meta.get("stop_reason") or meta.get("finish_reason") or "")
