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
import re
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
    # Both are already inside input_tokens, not additional to it. Carried
    # separately because the aggregate cost cannot show whether the shared
    # manuscript prefix is being hit or re-sent, and that is the single
    # biggest lever on what a review costs.
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0
    run_id: str = ""    # which review produced this; routes it to a consumer
    timestamp: float = field(default_factory=time.time)


# --- Per-thread "current node" tracking -------------------------------------

# LangGraph runs parallel branches on separate threads, so threading.local
# is the right granularity: each branch has exactly one "current node" at
# a time, and an LLM callback can ask which node it belongs to.
_CURRENT: threading.local = threading.local()


def current_node() -> str:
    return getattr(_CURRENT, "node", "")


def current_run() -> str:
    """The run id of whatever review this thread is working on.

    Set by :func:`node_context` inside each LangGraph worker thread, so
    anything called beneath a node — the streaming callback, the ingest
    loader — picks it up without it being threaded through every signature.
    """
    return getattr(_CURRENT, "run_id", "")


@contextlib.contextmanager
def node_context(name: str, run_id: str = "") -> Iterator[None]:
    """Mark a block of code as 'this is what node X is doing right now'.

    Emits node_start at entry and node_end at exit so the UI can move
    the row from pending -> running -> done. ``run_id`` routes those events
    to the consumer watching that particular review.
    """
    prev_node = getattr(_CURRENT, "node", "")
    prev_run = getattr(_CURRENT, "run_id", "")
    _CURRENT.node = name
    _CURRENT.run_id = run_id or prev_run
    emit(AgentEvent(kind="node_start", node=name, run_id=_CURRENT.run_id))
    started = time.time()
    try:
        yield
    finally:
        emit(AgentEvent(
            kind="node_end",
            node=name,
            text=f"{time.time() - started:.1f}s",
            run_id=_CURRENT.run_id,
        ))
        _CURRENT.node = prev_node
        _CURRENT.run_id = prev_run


# --- Global event sink ------------------------------------------------------

# One mailbox per run, keyed by run id. This was a single module global,
# which meant two concurrent reviews interleaved into the same consumer —
# the reason the web server is capped at one job.
#
# ``_DEFAULT_RUN`` is the un-keyed mailbox. A consumer that registers without
# a run id (the TUI) still receives everything, because emitters fall back to
# it when their own run has no registered consumer.
_DEFAULT_RUN = "__default__"

_QUEUES: dict[str, Queue] = {}
_QUEUES_LOCK = threading.Lock()


def register_observer(queue: Queue, run_id: str | None = None) -> None:
    """Attach a queue that receives AgentEvents for ``run_id``.

    Omit ``run_id`` to receive events from every run that doesn't have its
    own registered consumer.
    """
    with _QUEUES_LOCK:
        _QUEUES[run_id or _DEFAULT_RUN] = queue


def clear_observer(run_id: str | None = None) -> None:
    with _QUEUES_LOCK:
        _QUEUES.pop(run_id or _DEFAULT_RUN, None)


def _observer_for(run_id: str | None) -> Queue | None:
    with _QUEUES_LOCK:
        if run_id is not None:
            queue = _QUEUES.get(run_id)
            if queue is not None:
                return queue
        return _QUEUES.get(_DEFAULT_RUN)


# Running prompt-cache totals per run, so the summary can report whether the
# shared manuscript prefix was actually hit. Kept here rather than in
# ReviewState because every node would otherwise have to thread two more
# counters through its return value to answer a question about the transport.
_CACHE_TOTALS: dict[str, list[int]] = {}

# Per-node spend within a run: node -> [in, out, cache_read, cache_write, usd].
# The run total says what a review cost; this says which agent to look at, and
# the two answer different questions. Diagnosing C-09's bill without it meant
# estimating each stage's share by hand from prompt sizes, which is guessing
# with extra steps.
_NODE_USAGE: dict[str, dict[str, list[float]]] = {}


def note_cache_usage(run_id: str, read_tokens: int, write_tokens: int) -> None:
    with _QUEUES_LOCK:
        totals = _CACHE_TOTALS.setdefault(run_id or _DEFAULT_RUN, [0, 0])
        totals[0] += max(0, read_tokens)
        totals[1] += max(0, write_tokens)


def _note_node_usage(run_id: str, event: AgentEvent) -> None:
    with _QUEUES_LOCK:
        by_node = _NODE_USAGE.setdefault(run_id or _DEFAULT_RUN, {})
        row = by_node.setdefault(event.node or "(unattributed)", [0, 0, 0, 0, 0.0])
    row[0] += event.input_tokens
    row[1] += event.output_tokens
    row[2] += max(0, event.cache_read_tokens)
    row[3] += max(0, event.cache_write_tokens)
    row[4] += event.cost_usd


def cache_totals(run_id: str | None = None) -> tuple[int, int]:
    """``(read, written)`` cached input tokens across this run so far."""
    with _QUEUES_LOCK:
        totals = _CACHE_TOTALS.get(run_id or _DEFAULT_RUN)
        return (totals[0], totals[1]) if totals else (0, 0)


def node_usage(run_id: str | None = None) -> dict[str, tuple[int, int, int, int, float]]:
    """Per-node ``(in, out, cache_read, cache_write, usd)`` for this run."""
    with _QUEUES_LOCK:
        by_node = _NODE_USAGE.get(run_id or _DEFAULT_RUN) or {}
        return {k: (int(v[0]), int(v[1]), int(v[2]), int(v[3]), v[4]) for k, v in by_node.items()}


def reset_cache_totals(run_id: str | None = None) -> None:
    with _QUEUES_LOCK:
        _CACHE_TOTALS.pop(run_id or _DEFAULT_RUN, None)
        _NODE_USAGE.pop(run_id or _DEFAULT_RUN, None)


def emit(event: AgentEvent, run_id: str | None = None) -> None:
    # Explicit argument wins, then the event's own tag, then whatever run
    # this thread is executing — so callers deep in a node need do nothing.
    target = run_id or event.run_id or current_run() or None
    if event.kind == "usage":
        # Recorded before the queue lookup: a run with no registered observer
        # still spends money, and its summary still has to be able to say
        # whether the cache was working.
        note_cache_usage(target or _DEFAULT_RUN, event.cache_read_tokens, event.cache_write_tokens)
        _note_node_usage(target or _DEFAULT_RUN, event)
    q = _observer_for(target)
    if q is None:
        return
    # Stamp the resolved run so consumers can read it off the event rather
    # than inferring it from which queue it arrived on.
    if target and not event.run_id:
        event.run_id = target
    try:
        q.put_nowait(event)
    except Exception:  # noqa: BLE001
        # A full / closed queue must not break the review.
        pass


# --- Cost estimation --------------------------------------------------------

# $/million tokens, (input, output).
#
# Keyed by *normalized* model name — see :func:`_normalize_model_key`. One
# model reaches this table under several spellings depending on the route:
# "anthropic/claude-opus-5" via OpenRouter, "claude-opus-5" direct,
# "claude-haiku-4-5-20251001" as a dated snapshot. Maintaining a separate
# entry per spelling meant a route with no entry silently fell through to
# the family heuristic; normalizing collapses them onto one key.
_PRICING_USD_PER_M: dict[str, tuple[float, float]] = {
    # Anthropic — current generation
    "claude-fable-5": (10.0, 50.0),
    "claude-mythos-5": (10.0, 50.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-opus-4-6": (5.0, 25.0),
    "claude-opus-4-5": (5.0, 25.0),
    "claude-sonnet-5": (3.0, 15.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-sonnet-4-5": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
    # Anthropic — legacy, priced at the old Opus/Sonnet tiers
    "claude-opus-4": (15.0, 75.0),
    "claude-sonnet-4": (3.0, 15.0),
    "claude-haiku-4": (1.0, 5.0),
    "claude-3-5-sonnet": (3.0, 15.0),
    "claude-3-5-haiku": (0.8, 4.0),
    "claude-3-opus": (15.0, 75.0),
    # OpenAI
    "gpt-4o": (2.5, 10.0),
    "gpt-4o-mini": (0.15, 0.6),
    "gpt-4-1": (2.0, 8.0),
    "gpt-4-1-mini": (0.4, 1.6),
    "o3": (10.0, 40.0),
    "o4-mini": (1.1, 4.4),
    # Google
    "gemini-2-5-pro": (1.25, 10.0),
    "gemini-2-5-flash": (0.3, 2.5),
    "gemini-2-0-flash": (0.1, 0.4),
}


def _normalize_model_key(model: str) -> str:
    """Collapse the provider-specific spellings of a model id into one key.

    ``anthropic/claude-opus-5`` and ``claude-opus-5`` are the same model;
    so are ``claude-haiku-4-5`` and ``claude-haiku-4-5-20251001``.
    """
    key = model.lower().rsplit("/", 1)[-1]   # drop any vendor prefix
    key = re.sub(r"-\d{8}$", "", key)        # drop a dated snapshot suffix
    return key.replace(".", "-")             # unify 4.8 / 4-8 spellings


# Prompt-cache multipliers on the input rate (Anthropic's published pricing).
# Writing a cache entry costs a quarter more than sending the tokens plain;
# reading one costs a tenth. The whole point of threading the manuscript
# through as a shared prefix is to pay the first once and the second after.
_CACHE_WRITE_MULTIPLIER = 1.25
_CACHE_READ_MULTIPLIER = 0.10


def estimate_cost(
    model: str | None,
    input_tokens: int,
    output_tokens: int,
    *,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> float:
    """Best-effort cost estimate from a static pricing table.

    ``input_tokens`` is the provider's total input count, which **includes**
    the cached tokens — that is how LangChain reports it for Anthropic, which
    sums the plain, cache-read and cache-creation counts into one figure and
    puts the breakdown in ``input_token_details``. The two cache arguments are
    therefore subtracted back out here and re-priced at their own rates, not
    added on top; adding them would count those tokens twice.

    That detail is the whole reason this signature grew. Pricing every input
    token at the full rate makes a review with a perfectly working prompt
    cache cost exactly what one with no cache at all costs — the reported
    figure could not tell them apart, so a cache that was working looked
    identical to a cache that wasn't, and there was no way to see which you
    had.

    The multipliers are Anthropic's. OpenAI reports no cache-creation tokens
    (so the write term falls out) and discounts reads less steeply, which
    makes this an approximation on that route rather than a quote.
    """
    if not model:
        return 0.0
    rates = _PRICING_USD_PER_M.get(_normalize_model_key(model))
    if rates is None:
        # Fall back to a per-family heuristic so the cost field isn't always 0.
        rates = _family_rate(model)
    if rates is None:
        return 0.0
    in_rate, out_rate = rates

    cache_read = max(0, cache_read_tokens)
    cache_write = max(0, cache_write_tokens)
    # Clamped: a provider that reports cached tokens *outside* its input total
    # would otherwise drive this negative and credit the run.
    plain = max(0, input_tokens - cache_read - cache_write)

    billed = (
        plain
        + cache_write * _CACHE_WRITE_MULTIPLIER
        + cache_read * _CACHE_READ_MULTIPLIER
    )
    return (billed / 1_000_000) * in_rate + (output_tokens / 1_000_000) * out_rate


def _family_rate(model: str) -> tuple[float, float] | None:
    """Last-resort guess for a model not in the table.

    Only fires for something unreleased; every legacy tier is listed
    explicitly above, so current-generation rates are the better default.
    """
    m = model.lower()
    if "opus" in m:
        return (5.0, 25.0)
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

    def __init__(
        self,
        default_model: str | None = None,
        default_node: str | None = None,
        default_run: str | None = None,
    ):
        super().__init__()
        self._default_model = default_model
        # Which agent this model belongs to, captured when it was built.
        # `current_node()` is a thread-local set by the node, and LangChain
        # does not guarantee callbacks run on the node's thread — when they
        # don't, it reads empty and every usage event in the run collapses
        # into one unattributed bucket. The factory knows the agent, so it is
        # recorded here rather than inferred later.
        self._default_node = default_node or ""
        # And which run, for exactly the same reason. Capturing the node but
        # not the run left usage correctly *named* and filed under the
        # un-keyed mailbox instead of this review: `_usage_table` asks for one
        # run's rows, so an agent whose callback landed off-thread was absent
        # from the table entirely rather than merely mislabelled. The live TUI
        # registers without a run id and so kept showing those agents, which
        # is why the written report was the only place the loss was visible.
        self._default_run = default_run or ""

    def _node(self) -> str:
        return current_node() or self._default_node

    def _run(self) -> str:
        return current_run() or self._default_run

    # langchain calls this for every streamed chunk.
    def on_llm_new_token(self, token: str, **kwargs: Any) -> None:
        if not token:
            return
        emit(AgentEvent(kind="token", node=self._node(), text=token, run_id=self._run()))

    def on_llm_end(self, response: Any, **kwargs: Any) -> None:
        usage = _extract_usage(response)
        if not usage:
            return
        in_tok, out_tok, cost, model, cache_read, cache_write = usage
        if cost == 0.0:
            cost = estimate_cost(
                model or self._default_model, in_tok, out_tok,
                cache_read_tokens=cache_read, cache_write_tokens=cache_write,
            )
        emit(AgentEvent(
            kind="usage",
            node=self._node(),
            input_tokens=in_tok,
            output_tokens=out_tok,
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
            cost_usd=cost,
            run_id=self._run(),
        ))

    def on_llm_error(self, error: BaseException, **kwargs: Any) -> None:
        emit(AgentEvent(kind="log", node=self._node(), text=f"LLM error: {error}", run_id=self._run()))


def _extract_usage(
    response: Any,
) -> tuple[int, int, float, str | None, int, int] | None:
    """Pull (input, output, cost, model, cache_read, cache_write) out of an LLMResult.

    Tries usage_metadata on the message first (always present when
    stream_usage=True), then falls back to response_metadata's
    ``token_usage`` block; cost comes from OpenRouter's optional
    ``usage.cost`` field when ``extra_body={"usage":{"include":true}}``.

    The two cache counts are components of ``input_tokens``, not additions to
    it — see :func:`estimate_cost`.
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

            # LangChain normalizes these into input_token_details; a message
            # that came off the SDK without that adapter carries Anthropic's
            # raw spelling instead, and would otherwise read as uncached.
            details = um.get("input_token_details") or {}
            tu = rm.get("usage") or {}
            cache_read = int(
                details.get("cache_read") or tu.get("cache_read_input_tokens") or 0
            )
            cache_write = int(
                details.get("cache_creation") or tu.get("cache_creation_input_tokens") or 0
            )

            model = rm.get("model_name") or rm.get("model")
            if in_tok or out_tok or cost:
                return in_tok, out_tok, cost, model, cache_read, cache_write
    return None
