"""Event routing between concurrent runs.

The observer used to be a single module-level queue, which is why the web
server was capped at one job: a second review would post its events into the
first one's consumer. These tests pin the per-run routing that replaced it,
including the un-keyed fallback that keeps single-run consumers (the TUI)
working without changes.
"""

from __future__ import annotations

import threading
from queue import Empty, Queue

import pytest

from peerreviewagents.observability import (
    AgentEvent,
    clear_observer,
    current_run,
    emit,
    node_context,
    register_observer,
)


@pytest.fixture(autouse=True)
def _clean_registry():
    """Leave no observers behind — the registry is module state."""
    yield
    for run in ("run-a", "run-b", None):
        clear_observer(run)


def drain(q: Queue) -> list[AgentEvent]:
    out = []
    while True:
        try:
            out.append(q.get_nowait())
        except Empty:
            return out


def test_events_route_to_their_own_run():
    a, b = Queue(), Queue()
    register_observer(a, "run-a")
    register_observer(b, "run-b")

    emit(AgentEvent(kind="log", text="for a", run_id="run-a"))
    emit(AgentEvent(kind="log", text="for b", run_id="run-b"))

    assert [e.text for e in drain(a)] == ["for a"]
    assert [e.text for e in drain(b)] == ["for b"]


def test_unkeyed_consumer_still_receives_everything():
    """The TUI registers without a run id and must keep working."""
    q = Queue()
    register_observer(q)
    emit(AgentEvent(kind="log", text="untagged"))
    emit(AgentEvent(kind="log", text="tagged", run_id="run-a"))
    assert [e.text for e in drain(q)] == ["untagged", "tagged"]


def test_registered_run_is_not_stolen_by_the_default_queue():
    default, scoped = Queue(), Queue()
    register_observer(default)
    register_observer(scoped, "run-a")

    emit(AgentEvent(kind="log", text="mine", run_id="run-a"))

    assert [e.text for e in drain(scoped)] == ["mine"]
    assert drain(default) == []


def test_node_context_tags_events_and_sets_thread_run():
    q = Queue()
    register_observer(q, "run-a")

    with node_context("reviewer_clarity", run_id="run-a"):
        assert current_run() == "run-a"
        # An emit from deeper code, with no run_id of its own, inherits it.
        emit(AgentEvent(kind="token", text="hello"))

    kinds = [(e.kind, e.run_id) for e in drain(q)]
    assert kinds[0] == ("node_start", "run-a")
    assert ("token", "run-a") in kinds
    assert kinds[-1][0] == "node_end"
    # Thread-local is restored on exit.
    assert current_run() == ""


def test_concurrent_runs_do_not_interleave():
    """The property the single-job limit existed to avoid."""
    a, b = Queue(), Queue()
    register_observer(a, "run-a")
    register_observer(b, "run-b")

    def work(run_id: str, label: str) -> None:
        with node_context(f"node-{label}", run_id=run_id):
            for i in range(25):
                emit(AgentEvent(kind="token", text=f"{label}{i}"))

    threads = [
        threading.Thread(target=work, args=("run-a", "a")),
        threading.Thread(target=work, args=("run-b", "b")),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    a_tokens = [e.text for e in drain(a) if e.kind == "token"]
    b_tokens = [e.text for e in drain(b) if e.kind == "token"]
    assert len(a_tokens) == 25 and all(t.startswith("a") for t in a_tokens)
    assert len(b_tokens) == 25 and all(t.startswith("b") for t in b_tokens)


def test_emit_with_no_observer_is_a_noop():
    emit(AgentEvent(kind="log", text="nobody listening", run_id="run-a"))


def test_full_queue_does_not_break_the_review():
    q = Queue(maxsize=1)
    register_observer(q, "run-a")
    for _ in range(5):
        emit(AgentEvent(kind="log", text="x", run_id="run-a"))  # must not raise
    assert q.qsize() == 1
