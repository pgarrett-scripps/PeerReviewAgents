"""Per-job event bus.

Bridges the LangGraph pipeline (which runs in a worker thread and posts
:class:`AgentEvent` to a thread-safe queue) to an arbitrary number of
asyncio consumers (WebSocket clients) on the FastAPI event loop. The
loop reference is captured at construction so the worker thread can
schedule a put_nowait via ``call_soon_threadsafe`` without owning a
reference to the loop itself.

Events are also persisted in a per-job ring of dicts so that a client
which connects mid-job (or reconnects after a refresh) can replay the
full history and pick up where it left off.
"""

from __future__ import annotations

import asyncio
from collections import deque
from typing import Any


class EventBus:
    """Asyncio fan-out queue with a bounded replay log.

    ``put_threadsafe`` is callable from any thread; ``subscribe`` returns
    an async iterator of every event seen so far plus everything that
    arrives until the bus is closed.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop, history_cap: int = 10_000):
        self._loop = loop
        self._history: deque[dict[str, Any]] = deque(maxlen=history_cap)
        self._subscribers: set[asyncio.Queue[dict[str, Any] | None]] = set()
        self._closed = False

    # --- producer side -----------------------------------------------------

    def put_threadsafe(self, event: dict[str, Any]) -> None:
        """Schedule a publish from a non-loop thread."""
        if self._closed:
            return
        self._loop.call_soon_threadsafe(self._publish, event)

    def _publish(self, event: dict[str, Any]) -> None:
        self._history.append(event)
        dead: list[asyncio.Queue[dict[str, Any] | None]] = []
        for q in self._subscribers:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                # Slow consumer; drop them rather than blocking publishers.
                dead.append(q)
        for q in dead:
            self._subscribers.discard(q)

    def close(self) -> None:
        """Signal end-of-stream to all current subscribers."""
        if self._closed:
            return
        self._closed = True
        # Schedule the sentinel so anyone awaiting q.get() wakes up.
        self._loop.call_soon_threadsafe(self._broadcast_close)

    def _broadcast_close(self) -> None:
        for q in list(self._subscribers):
            try:
                q.put_nowait(None)
            except asyncio.QueueFull:
                pass

    # --- consumer side -----------------------------------------------------

    async def subscribe(self) -> "EventBus._Subscription":
        """Return an async iterator over history + future events."""
        q: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(maxsize=4096)
        # Replay history before registering so we don't get a torn view.
        for ev in list(self._history):
            await q.put(ev)
        if self._closed:
            await q.put(None)
        self._subscribers.add(q)
        return EventBus._Subscription(self, q)

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def history(self) -> list[dict[str, Any]]:
        return list(self._history)

    class _Subscription:
        def __init__(self, bus: "EventBus", q: asyncio.Queue):
            self._bus = bus
            self._q = q

        def __aiter__(self) -> "EventBus._Subscription":
            return self

        async def __anext__(self) -> dict[str, Any]:
            ev = await self._q.get()
            if ev is None:
                raise StopAsyncIteration
            return ev

        def close(self) -> None:
            self._bus._subscribers.discard(self._q)
