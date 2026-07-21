"""Live-progress observability for the peer-review pipeline.

The pipeline is mostly opaque from the outside — agents run in parallel
inside LangGraph and only return when finished. This module exposes a
small event stream so the TUI (or any other consumer) can show what each
agent is doing in real time: when nodes start/finish, what they're
streaming, and how many tokens / how much money they've burned.

Wiring is opt-in. Call :func:`register_observer` once with a thread-safe
queue before running the graph. The agent nodes call :func:`node_context`
around their work; the LLM factory attaches :class:`StreamingCallback` to
every chat model so token streaming and usage metadata flow back through
the same queue.
"""

from __future__ import annotations

import contextlib
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from queue import Queue
from typing import Any

from langchain_core.callbacks.base import BaseCallbackHandler


# --- Event types ------------------------------------------------------------

@dataclass
class AgentEvent:
    """One observable thing that happened in the pipeline."""

    kind: str           # node_start | node_end | token | usage | log | info
    node: str = ""      # logical node name (e.g. "reviewer_methodology")
    text: str = ""      # streamed text for kind=token, free-form for log/info
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    timestamp: float = field(default_factory=time.time)


# --- Per-thread "current node" tracking -------------------------------------

# LangGraph runs parallel branches on separate threads, so threading.local
# is the right granularity: each branch has exactly one "current node" at
# a time, and an LLM callback can ask which node it belongs to.
_CURRENT: threading.local = threading.local()


def current_node() -> str:
    return getattr(_CURRENT, "node", "")


@contextlib.contextmanager
def node_context(name: str) -> Iterator[None]:
    """Mark a block of code as 'this is what node X is doing right now'.

    Emits node_start at entry and node_end at exit so the UI can move
    the row from pending → running → done.
    """
    prev = getattr(_CURRENT, "node", "")
    _CURRENT.node = name
    emit(AgentEvent(kind="node_start", node=name))
    started = time.time()
    try:
        yield
    finally:
        _CURRENT.node = prev
        emit(AgentEvent(
            kind="node_end",
            node=name,
            text=f"{time.time() - started:.1f}s",
        ))


# --- Global event sink ------------------------------------------------------

_QUEUE: Queue | None = None
_QUEUE_LOCK = threading.Lock()


def register_observer(queue: Queue) -> None:
    """Attach a queue that will receive every AgentEvent emitted from now on."""
    global _QUEUE
    with _QUEUE_LOCK:
        _QUEUE = queue


def clear_observer() -> None:
    global _QUEUE
    with _QUEUE_LOCK:
        _QUEUE = None


def emit(event: AgentEvent) -> None:
    q = _QUEUE
    if q is None:
        return
    try:
        q.put_nowait(event)
    except Exception:  # noqa: BLE001
        # A full / closed queue must not break the review.
        pass


# --- Cost estimation --------------------------------------------------------

# $/million tokens, (input, output). Updated against OpenRouter's public
# pricing page; unknown models fall through to None and are billed at $0
# in the UI rather than a fabricated number. Extend as needed.
_PRICING_USD_PER_M: dict[str, tuple[float, float]] = {
    # Direct Anthropic model ids (provider = "anthropic"): no vendor prefix.
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-opus-4-6": (5.0, 25.0),
    "claude-sonnet-5": (3.0, 15.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-fable-5": (10.0, 50.0),
    # OpenRouter slugs (provider = "openrouter").
    "anthropic/claude-opus-4.1": (15.0, 75.0),
    "anthropic/claude-opus-4": (15.0, 75.0),
    "anthropic/claude-sonnet-4.6": (3.0, 15.0),
    "anthropic/claude-sonnet-4.5": (3.0, 15.0),
    "anthropic/claude-sonnet-4": (3.0, 15.0),
    "anthropic/claude-haiku-4.5": (1.0, 5.0),
    "anthropic/claude-haiku-4": (1.0, 5.0),
    "anthropic/claude-3.5-sonnet": (3.0, 15.0),
    "anthropic/claude-3.5-haiku": (0.8, 4.0),
    "anthropic/claude-3-opus": (15.0, 75.0),
    "openai/gpt-4o": (2.5, 10.0),
    "openai/gpt-4o-mini": (0.15, 0.6),
    "openai/gpt-4.1": (2.0, 8.0),
    "openai/gpt-4.1-mini": (0.4, 1.6),
    "openai/o3": (10.0, 40.0),
    "openai/o4-mini": (1.1, 4.4),
    "google/gemini-2.5-pro": (1.25, 10.0),
    "google/gemini-2.5-flash": (0.3, 2.5),
    "google/gemini-2.0-flash": (0.1, 0.4),
}


def estimate_cost(model: str | None, input_tokens: int, output_tokens: int) -> float:
    """Best-effort cost estimate from a static pricing table."""
    if not model:
        return 0.0
    rates = _PRICING_USD_PER_M.get(model)
    if rates is None:
        # Fall back to a per-family heuristic so the cost field isn't always 0.
        rates = _family_rate(model)
    if rates is None:
        return 0.0
    in_rate, out_rate = rates
    return (input_tokens / 1_000_000) * in_rate + (output_tokens / 1_000_000) * out_rate


def _family_rate(model: str) -> tuple[float, float] | None:
    m = model.lower()
    if "opus" in m:
        return (15.0, 75.0)
    if "sonnet" in m:
        return (3.0, 15.0)
    if "haiku" in m:
        return (1.0, 5.0)
    if "gpt-4o-mini" in m or "4.1-mini" in m:
        return (0.4, 1.6)
    if "gpt-4o" in m or "gpt-4.1" in m:
        return (2.5, 10.0)
    if "gemini" in m and "flash" in m:
        return (0.3, 2.5)
    if "gemini" in m and "pro" in m:
        return (1.25, 10.0)
    return None


# --- LangChain callback -----------------------------------------------------

class StreamingCallback(BaseCallbackHandler):
    """Bridge between langchain LLM events and the AgentEvent queue.

    - Streams each generated token to the queue (kind="token") so the TUI
      can render the active agent's output live.
    - On LLM completion, walks the response for ``usage_metadata`` and
      OpenRouter's cost field and emits a kind="usage" event.

    Lookup of the "current node" goes through the thread-local set by
    :func:`node_context`, which is the only place that knows which agent
    we belong to.
    """

    def __init__(self, default_model: str | None = None):
        super().__init__()
        self._default_model = default_model

    # langchain calls this for every streamed chunk.
    def on_llm_new_token(self, token: str, **kwargs: Any) -> None:
        if not token:
            return
        emit(AgentEvent(kind="token", node=current_node(), text=token))

    def on_llm_end(self, response: Any, **kwargs: Any) -> None:
        usage = _extract_usage(response)
        if not usage:
            return
        in_tok, out_tok, cost, model = usage
        if cost == 0.0:
            cost = estimate_cost(model or self._default_model, in_tok, out_tok)
        emit(AgentEvent(
            kind="usage",
            node=current_node(),
            input_tokens=in_tok,
            output_tokens=out_tok,
            cost_usd=cost,
        ))

    def on_llm_error(self, error: BaseException, **kwargs: Any) -> None:
        emit(AgentEvent(kind="log", node=current_node(), text=f"LLM error: {error}"))


def _extract_usage(response: Any) -> tuple[int, int, float, str | None] | None:
    """Pull (input, output, cost, model) out of a LangChain LLMResult.

    Tries usage_metadata on the message first (always present when
    stream_usage=True), then falls back to response_metadata's
    ``token_usage`` block; cost comes from OpenRouter's optional
    ``usage.cost`` field when ``extra_body={"usage":{"include":true}}``.
    """
    generations = getattr(response, "generations", None) or []
    for gen_list in generations:
        for gen in gen_list:
            msg = getattr(gen, "message", None)
            if msg is None:
                continue
            um = getattr(msg, "usage_metadata", None) or {}
            rm = getattr(msg, "response_metadata", None) or {}

            in_tok = int(um.get("input_tokens") or 0)
            out_tok = int(um.get("output_tokens") or 0)
            if not in_tok and not out_tok:
                tu = rm.get("token_usage") or rm.get("usage") or {}
                in_tok = int(tu.get("prompt_tokens") or tu.get("input_tokens") or 0)
                out_tok = int(tu.get("completion_tokens") or tu.get("output_tokens") or 0)

            cost = 0.0
            # OpenRouter "usage.include=true" surfaces cost; langchain pipes
            # this through response_metadata under various keys depending
            # on version, so check a couple.
            for path in (("usage", "cost"), ("token_usage", "cost"), ("cost",)):
                cur: Any = rm
                for key in path:
                    if not isinstance(cur, dict):
                        cur = None
                        break
                    cur = cur.get(key)
                if isinstance(cur, (int, float)):
                    cost = float(cur)
                    break

            model = rm.get("model_name") or rm.get("model")
            if in_tok or out_tok or cost:
                return in_tok, out_tok, cost, model
    return None
