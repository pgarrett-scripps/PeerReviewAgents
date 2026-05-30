"""Background driver that runs the LangGraph pipeline for a single job.

Lives entirely on a worker thread. Registers a thread-safe ``Queue`` as
the global observer ([[observability.py]]), drives ``graph.stream``,
and translates each :class:`AgentEvent` into a wire-format dict that's
pushed through the per-job :class:`EventBus`. Also keeps the JobState's
per-agent buffers and accumulated graph state up to date so that the
``GET /jobs/<id>/agents/<name>`` endpoint can read finished outputs.
"""

from __future__ import annotations

import threading
import time
from queue import Empty, Queue
from typing import Any

from peerreviewagents.agents.utils.agent_states import ReviewState
from peerreviewagents.graph.review_graph import PeerReviewGraph
from peerreviewagents.observability import (
    AgentEvent,
    clear_observer,
    register_observer,
)
from peerreviewagents.reports import write_reports

from .bus import EventBus
from .jobs import AGENT_NAMES, JobState


_VALID_DECISIONS = {"accept", "minor", "major", "reject"}

# Maps node names to phase labels so the frontend can scroll the camera
# to the right room as work progresses.
_NODE_PHASE = {
    "ingest": "ingest",
    "advocate": "debate",
    "skeptic": "debate",
    "meta_reviewer": "synthesis",
    "author_rebuttal": "synthesis",
    "editor": "decision",
    "journal_recommender": "recommend",
}


def _phase_for(node: str) -> str | None:
    if node.startswith("reviewer_"):
        return "reviewers"
    return _NODE_PHASE.get(node)


class JobRunner:
    """Wraps the graph execution + event forwarding for one job."""

    def __init__(self, job: JobState, config: dict, bus: EventBus):
        self.job = job
        self.config = config
        self.bus = bus
        self._events: Queue[AgentEvent] = Queue()
        self._thread: threading.Thread | None = None
        # Throttle token forwarding: pile up bytes for ~50ms then flush.
        # Otherwise debate rounds saturate the WebSocket with one frame
        # per chunk.
        self._token_flush_ms = 50

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name=f"job-{self.job.id}", daemon=True
        )
        self._thread.start()

    # ------------------------------------------------------------------
    # Worker thread entry point
    # ------------------------------------------------------------------

    def _run(self) -> None:
        register_observer(self._events)
        self.job.status = "running"
        self.job.started_at = time.time()
        self._emit({
            "type": "job_status",
            "status": "running",
            "started_at": self.job.started_at,
        })

        forwarder = threading.Thread(
            target=self._forward_events, name=f"job-{self.job.id}-fwd", daemon=True
        )
        forwarder.start()

        try:
            graph = PeerReviewGraph(self.config)
            for node, accumulated in graph.stream(self.job.manuscript_path):
                self.job.accumulated = accumulated
                self._handle_node_yield(node)
        except Exception as exc:  # noqa: BLE001
            self.job.errors.append(f"pipeline crashed: {exc}")
            self._events.put(AgentEvent(kind="log", node="error", text=str(exc)))
        finally:
            # Allow the forwarder a moment to drain remaining events,
            # then signal it to stop.
            time.sleep(0.1)
            self._events.put(_STOP_SENTINEL)
            forwarder.join(timeout=2.0)
            clear_observer()

        self._finalize()

    def _handle_node_yield(self, node: str) -> None:
        """The graph yields synthetic ingest events; translate them."""
        if node == "_ingest_start":
            self._events.put(AgentEvent(kind="node_start", node="ingest"))
        elif node == "_ingest":
            self._events.put(AgentEvent(kind="node_end", node="ingest", text="done"))

    # ------------------------------------------------------------------
    # Token forwarding with light batching
    # ------------------------------------------------------------------

    def _forward_events(self) -> None:
        # Coalesce adjacent token events for the same agent so each
        # WebSocket frame is meaningful rather than 3-character spurts.
        token_buf: dict[str, list[str]] = {}
        last_flush = time.time()

        def flush_tokens() -> None:
            for node, parts in token_buf.items():
                text = "".join(parts)
                if not text:
                    continue
                self._emit({"type": "token", "agent": node, "text": text})
            token_buf.clear()

        while True:
            try:
                ev = self._events.get(timeout=0.05)
            except Empty:
                if token_buf and (time.time() - last_flush) * 1000 >= self._token_flush_ms:
                    flush_tokens()
                    last_flush = time.time()
                continue

            if ev is _STOP_SENTINEL:
                flush_tokens()
                return

            self._record(ev)
            if ev.kind == "token":
                token_buf.setdefault(ev.node, []).append(ev.text)
                if (time.time() - last_flush) * 1000 >= self._token_flush_ms:
                    flush_tokens()
                    last_flush = time.time()
            else:
                if token_buf:
                    flush_tokens()
                    last_flush = time.time()
                self._emit(self._event_to_wire(ev))

    def _record(self, ev: AgentEvent) -> None:
        node = ev.node or ""
        if ev.kind == "node_start":
            self.job.agent_status[node] = "running"
            phase = _phase_for(node)
            if phase:
                self._emit({"type": "phase", "phase": phase, "agent": node})
        elif ev.kind == "node_end":
            self.job.agent_status[node] = "done"
        elif ev.kind == "token":
            buf = self.job.agent_buffers.setdefault(node, "")
            self.job.agent_buffers[node] = buf + ev.text
        elif ev.kind == "usage":
            usage = self.job.agent_usage.setdefault(
                node, {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}
            )
            usage["input_tokens"] += ev.input_tokens
            usage["output_tokens"] += ev.output_tokens
            usage["cost_usd"] += ev.cost_usd
            self.job.total_cost += ev.cost_usd

    def _event_to_wire(self, ev: AgentEvent) -> dict[str, Any]:
        base: dict[str, Any] = {"type": ev.kind, "agent": ev.node, "ts": ev.timestamp}
        if ev.kind == "node_end":
            base["duration"] = ev.text
        elif ev.kind == "usage":
            base["input_tokens"] = ev.input_tokens
            base["output_tokens"] = ev.output_tokens
            base["cost_usd"] = ev.cost_usd
            base["total_cost"] = self.job.total_cost
        elif ev.kind == "log":
            base["text"] = ev.text
        elif ev.kind == "info":
            base["text"] = ev.text
        return base

    def _emit(self, payload: dict[str, Any]) -> None:
        self.bus.put_threadsafe(payload)

    # ------------------------------------------------------------------
    # Finalize
    # ------------------------------------------------------------------

    def _finalize(self) -> None:
        final: ReviewState = self.job.accumulated  # type: ignore[assignment]
        decision = final.get("decision") if isinstance(final, dict) else None
        if decision in _VALID_DECISIONS and final.get("reports"):
            try:
                run_dir = write_reports(final)
                self.job.report_dir = run_dir
            except Exception as exc:  # noqa: BLE001
                self.job.errors.append(f"failed to write reports: {exc}")
            self.job.decision = decision
            self.job.status = "done"
        else:
            self.job.status = "error"
            if not self.job.errors:
                errs = final.get("errors", []) if isinstance(final, dict) else []
                self.job.errors.extend(errs or ["review did not produce a valid decision"])

        self.job.finished_at = time.time()
        # Use any per-call cost the graph itself accumulated (preferred)
        # over our streaming sum, which can miss non-streamed responses.
        if isinstance(final, dict) and final.get("total_cost"):
            self.job.total_cost = float(final["total_cost"])

        self._emit({
            "type": "job_status",
            "status": self.job.status,
            "finished_at": self.job.finished_at,
        })
        self._emit({
            "type": "final",
            "status": self.job.status,
            "decision": self.job.decision,
            "report_dir": self.job.report_dir,
            "total_cost": self.job.total_cost,
            "errors": list(self.job.errors),
        })
        self.bus.close()


# Sentinel used to wake the forwarder thread on shutdown.
_STOP_SENTINEL = object()


def render_agent_payload(job: JobState, agent: str) -> dict[str, Any]:
    """Build the response for ``GET /jobs/<id>/agents/<name>``.

    Every agent emits a typed pydantic schema (see
    :mod:`peerreviewagents.agents.schemas`); the ``body`` field below is
    the rendered markdown produced from that schema via ``to_markdown()``.
    The ``streamed`` field is the live token buffer the WebSocket has
    accumulated so far — useful for live introspection while the agent
    is still running.
    """
    if agent not in AGENT_NAMES and agent != "ingest":
        return {"agent": agent, "known": False}

    status = job.agent_status.get(agent, "pending")
    usage = job.agent_usage.get(agent, {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0})
    streamed = job.agent_buffers.get(agent, "")

    body, meta = _finished_body(job, agent)

    return {
        "agent": agent,
        "known": True,
        "status": status,
        "usage": usage,
        "streamed": streamed,
        "body": body,
        "meta": meta,
    }


def _finished_body(job: JobState, agent: str) -> tuple[str | None, dict[str, Any] | None]:
    state = job.accumulated or {}
    if agent.startswith("reviewer_"):
        name = agent[len("reviewer_"):]
        for r in state.get("reports", []) or []:
            if r.get("reviewer") == name:
                return r.get("body"), {
                    "score": r.get("score"),
                    "confidence": r.get("confidence"),
                }
        return None, None
    if agent in ("advocate", "skeptic"):
        turns = [t for t in state.get("debate", []) or [] if t.get("role") == agent]
        if not turns:
            return None, None
        body = "\n\n---\n\n".join(
            f"**Round {t.get('round')}**\n\n{t.get('content', '')}" for t in turns
        )
        return body, {"rounds": len(turns)}
    if agent == "meta_reviewer":
        body = state.get("meta_review")
        return body, {"draft_recommendation": state.get("draft_recommendation")}
    if agent == "author_rebuttal":
        return state.get("author_rebuttal"), None
    if agent == "editor":
        return state.get("decision_letter"), {"decision": state.get("decision")}
    if agent == "journal_recommender":
        return state.get("journal_recommendations"), None
    return None, None
