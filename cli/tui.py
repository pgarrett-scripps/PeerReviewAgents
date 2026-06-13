"""Textual TUI for PeerReviewAgents.

Shows per-agent progress, live streaming output, and running token /
cost totals while the LangGraph pipeline executes. All live data flows
through the observability event queue: agent nodes emit
``node_start`` / ``node_end`` events from a context manager, and the
LLM callback streams tokens and per-call usage from every model
invocation. The UI thread drains the queue on a Textual timer and
mutates widgets via :meth:`call_from_thread` semantics.
"""

from __future__ import annotations

import os
import time
from queue import Empty, Queue

from rich.markup import escape
from textual.app import App, ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import DataTable, Footer, Header, Log, Static

from peerreviewagents.agents.auditors import AUDITOR_NAMES
from peerreviewagents.agents.reviewers import REVIEWER_NAMES
from peerreviewagents.graph.review_graph import PeerReviewGraph
from peerreviewagents.observability import (
    AgentEvent,
    clear_observer,
    register_observer,
)
from peerreviewagents.reports import write_reports
from peerreviewagents.storage.memory import MemoryLog

_VERDICT = {
    "accept": "ACCEPT",
    "minor": "MINOR REVISION",
    "major": "MAJOR REVISION",
    "reject": "REJECT",
}
_VALID_DECISIONS = set(_VERDICT)

_STATUS_GLYPH = {
    "pending": "·",
    "running": "▶",
    "done": "✓",
    "error": "✗",
    "skipped": "—",
}

# (column_key, header_label). Column keys must be stable so update_cell()
# can address them; the headers are just display strings.
_COLUMNS: tuple[tuple[str, str], ...] = (
    ("stage", "Stage"),
    ("status", "Status"),
    ("time", "Time"),
    ("in", "Tok In"),
    ("out", "Tok Out"),
    ("cost", "Cost"),
)
_STATUS_STYLE = {
    "pending": "dim",
    "running": "yellow",
    "done": "green",
    "error": "red",
    "skipped": "dim",
}


def _fmt_elapsed(seconds: float) -> str:
    seconds = max(0.0, seconds)
    if seconds < 60:
        return f"{seconds:4.1f}s"
    m, s = divmod(int(seconds), 60)
    if m < 60:
        return f"{m:02d}:{s:02d}"
    h, m = divmod(m, 60)
    return f"{h:d}:{m:02d}:{s:02d}"


def _status_cell(status: str) -> str:
    glyph = _STATUS_GLYPH.get(status, "?")
    style = _STATUS_STYLE.get(status, "white")
    return f"[{style}]{glyph} {status}[/]"


class ReviewApp(App):
    CSS = """
    Screen { layout: vertical; }
    #status { height: 3; padding: 0 1; background: $boost; color: $text; }
    #main { height: 1fr; min-height: 12; }
    #table { width: 62; min-width: 50; }
    #stream-scroll { width: 1fr; border: round $primary; padding: 0 1; }
    #stream { width: 100%; }
    #log { height: 8; border: round $secondary; padding: 0 1; }
    #decision { height: auto; padding: 1; border: round $success; }
    """
    BINDINGS = [
        ("q", "quit", "Quit"),
        ("tab", "cycle_focus", "Next agent"),
    ]

    # Order matters: this is the order rows appear in the progress table.
    # Debate roles are listed once even though they run multiple rounds;
    # counters accumulate across rounds.
    _STAGES: tuple[tuple[str, str], ...] = (
        ("ingest", "Ingest"),
        *tuple(
            (f"reviewer_{n}", f"Reviewer · {n.replace('_', ' ').title()}")
            for n in REVIEWER_NAMES
        ),
        *tuple(
            (f"audit_{n}", f"Audit · {n.replace('_', ' ').title()}")
            for n in AUDITOR_NAMES
        ),
        ("advocate", "Debate · Advocate"),
        ("skeptic", "Debate · Skeptic"),
        ("meta_reviewer", "Meta-reviewer"),
        ("author_rebuttal", "Author rebuttal"),
        ("editor", "Editor-in-Chief"),
        ("journal_recommender", "Journal Scout"),
    )

    def __init__(self, manuscript: str, config: dict):
        super().__init__()
        self.manuscript = manuscript
        self.config = config
        self.final: dict = {}

        self._events: Queue[AgentEvent] = Queue()
        self._start_time = 0.0
        self._stage_state: dict[str, dict] = {}
        self._labels: dict[str, str] = {k: v for k, v in self._STAGES}
        self._known_keys: set[str] = set(self._labels)
        self._total_in = 0
        self._total_out = 0
        self._total_cost = 0.0
        self._agent_buffers: dict[str, str] = {}
        self._focused_node: str | None = None
        self._ingest_started: float | None = None
        # Drives the throttled stream-pane re-render in _flush_stream.
        self._stream_dirty = False
        # Hard cap on visible stream chars so a runaway agent doesn't
        # melt the renderer; the full buffer is still kept for the final
        # report and Tab-cycling.
        self._stream_window = 12000

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static("", id="status")
        with Horizontal(id="main"):
            yield DataTable(id="table", cursor_type="row", zebra_stripes=True)
            with VerticalScroll(id="stream-scroll"):
                # Live token output is rendered into a single Static and
                # re-flowed on a throttled tick (see _flush_stream). A
                # RichLog would treat every streamed token chunk as its
                # own line, which made the panel unreadable.
                yield Static("", id="stream", markup=True)
        yield Log(id="log", highlight=True)
        yield Static("Running…", id="decision")
        yield Footer()

    def on_mount(self) -> None:
        self._start_time = time.time()

        table = self.query_one("#table", DataTable)
        # Explicit column keys are required so update_cell() can address
        # them later — DataTable.add_columns(*labels) auto-generates
        # anonymous keys, and looking them up by header text silently
        # raises (caught and swallowed below), leaving every row at
        # "pending".
        for col_key, label in _COLUMNS:
            table.add_column(label, key=col_key)
        for key, label in self._STAGES:
            self._stage_state[key] = {
                "label": label,
                "status": "pending",
                "started": None,
                "ended": None,
                "time_text": "",
                "in": 0,
                "out": 0,
                "cost": 0.0,
            }
            table.add_row(
                label, _status_cell("pending"), "—", "—", "—", "—",
                key=key,
            )

        self._refresh_status()
        self.query_one("#stream", Static).update(
            "[dim]Waiting for the first agent to start…[/dim]"
        )

        register_observer(self._events)
        self.set_interval(0.05, self._drain_events)
        # Re-render the stream pane at ~10Hz; this is cheap because we
        # only paint when _stream_dirty is set by a token event.
        self.set_interval(0.1, self._flush_stream)
        self.set_interval(1.0, self._tick_clock)
        self.run_worker(self._review_worker, thread=True, exclusive=True)

    def on_unmount(self) -> None:
        clear_observer()

    # ------------------------------------------------------------------
    # Background worker
    # ------------------------------------------------------------------

    def _review_worker(self) -> None:
        """Runs in a worker thread; just drives the graph + final write."""
        graph = PeerReviewGraph(self.config)
        try:
            for node, accumulated in graph.stream(self.manuscript):
                self.final = accumulated
                # The ingest stage isn't a graph node, so the graph
                # surfaces synthetic events instead of going through
                # the observability node_context wrapper.
                if node == "_ingest_start":
                    self._events.put(AgentEvent(kind="node_start", node="ingest"))
                elif node == "_ingest":
                    elapsed = time.time() - (self._ingest_started or self._start_time)
                    self._events.put(AgentEvent(
                        kind="node_end",
                        node="ingest",
                        text=_fmt_elapsed(elapsed),
                    ))
        except Exception as exc:  # noqa: BLE001
            self._events.put(AgentEvent(kind="log", node="error", text=f"pipeline crashed: {exc}"))
            self.call_from_thread(self._finish_with_error, [str(exc)])
            return

        self.call_from_thread(self._finalize)

    def _finalize(self) -> None:
        decision = self.final.get("decision")
        if decision not in _VALID_DECISIONS or not self.final.get("reports"):
            errors = self.final.get("errors", []) or ["(no error details collected)"]
            self._finish_with_error(errors)
            return

        try:
            run_dir = write_reports(self.final)
        except Exception as exc:  # noqa: BLE001
            self._finish_with_error([f"failed to write reports: {exc}"])
            return
        job_id = os.path.basename(run_dir.rstrip(os.sep))
        try:
            sections = self.final.get("sections") or {}
            abstract = sections.get("abstract") or self.final.get("manuscript_md", "")[:500]
            MemoryLog(self.config["memory_path"]).append_pending(
                job_id=job_id,
                title=self.final.get("manuscript_title", ""),
                abstract=abstract,
                decision=self.final.get("decision", ""),
                draft_summary=self.final.get("decision_letter", ""),
                reports=self.final.get("reports", []),
            )
        except Exception:  # noqa: BLE001
            pass

        cost = self.final.get("total_cost") or self._total_cost
        msg = (
            f"[b green]DECISION:[/b green] {_VERDICT[decision]}\n"
            f"[b]Reports:[/b] {run_dir}\n"
            f"[b]Job ID:[/b] {job_id}\n"
            f"[b]OpenRouter cost:[/b] ${cost:.4f}"
        )
        self.query_one("#decision", Static).update(msg)

    def _finish_with_error(self, errors: list[str]) -> None:
        err_lines = "\n".join(f"  • {escape(e)}" for e in errors)
        msg = (
            "[b red]REVIEW FAILED — no report written.[/b red]\n"
            f"{err_lines}"
        )
        self.query_one("#decision", Static).update(msg)

    # ------------------------------------------------------------------
    # Event drain
    # ------------------------------------------------------------------

    def _drain_events(self) -> None:
        # Bulk-drain to amortize Textual layout cost; tokens come fast.
        try:
            while True:
                ev = self._events.get_nowait()
                self._handle_event(ev)
        except Empty:
            return

    def _handle_event(self, ev: AgentEvent) -> None:
        if ev.kind == "node_start":
            self._on_node_start(ev.node, ev.timestamp)
        elif ev.kind == "node_end":
            self._on_node_end(ev.node, ev.text)
        elif ev.kind == "token":
            self._on_token(ev.node, ev.text)
        elif ev.kind == "usage":
            self._on_usage(ev.node, ev.input_tokens, ev.output_tokens, ev.cost_usd)
        elif ev.kind == "log":
            self._append_log(f"[{ev.node}] {ev.text}")
        elif ev.kind == "info":
            self._append_log(ev.text)

    def _on_node_start(self, node: str, started: float) -> None:
        if node == "ingest":
            self._ingest_started = started or time.time()
        if node not in self._stage_state:
            self._register_dynamic_stage(node)
        st = self._stage_state[node]
        # Don't overwrite a finished/restarted node's prior time. For
        # debate roles that fire multiple rounds, keep "started" pointing
        # at the first start so total elapsed is meaningful.
        if st["status"] != "running":
            st["status"] = "running"
            st["started"] = st["started"] or started or time.time()
        self._set_table_status(node)
        self._focused_node = node
        self._stream_dirty = True
        self._append_log(f"▶ {st['label']}")

    def _on_node_end(self, node: str, time_text: str) -> None:
        if node not in self._stage_state:
            return
        st = self._stage_state[node]
        st["status"] = "done"
        st["ended"] = time.time()
        if time_text:
            st["time_text"] = time_text
        elif st["started"] is not None:
            st["time_text"] = _fmt_elapsed(time.time() - st["started"])
        self._set_table_status(node)
        self._append_log(f"✓ {st['label']} ({st['time_text']})")
        # Auto-advance the focus to another still-running agent so
        # parallel branches don't strand the viewer on a finished one.
        if self._focused_node == node:
            self._focused_node = self._next_running(exclude=node)
            self._stream_dirty = True

    def _on_token(self, node: str, text: str) -> None:
        if not text:
            return
        self._agent_buffers.setdefault(node, "")
        self._agent_buffers[node] += text
        # Other agents' tokens still accumulate in their per-agent
        # buffer (revealable via Tab cycling); we just mark the panel
        # dirty when the focused agent has new content.
        if self._focused_node is None:
            self._focused_node = node
        if self._focused_node == node:
            self._stream_dirty = True

    def _on_usage(self, node: str, in_tok: int, out_tok: int, cost: float) -> None:
        self._total_in += in_tok
        self._total_out += out_tok
        self._total_cost += cost
        if node in self._stage_state:
            st = self._stage_state[node]
            st["in"] += in_tok
            st["out"] += out_tok
            st["cost"] += cost
            self._set_table_status(node)
        self._refresh_status()

    def _append_log(self, msg: str) -> None:
        log = self.query_one("#log", Log)
        log.write_line(f"{time.strftime('%H:%M:%S')}  {msg}")

    def _flush_stream(self) -> None:
        """Throttled re-render of the live-output pane.

        Called ~10x/sec but only repaints when a token event has marked
        the buffer dirty. Renders the full focused-agent buffer (tail-
        trimmed) into a single Static so text wraps naturally instead
        of appearing one chunk per line as it did with RichLog.
        """
        if not self._stream_dirty:
            return
        self._stream_dirty = False
        static = self.query_one("#stream", Static)
        node = self._focused_node
        if node is None or not self._agent_buffers.get(node):
            static.update("[dim]Waiting for the next agent to start…[/dim]")
            return
        label = self._labels.get(node, node)
        buf = self._agent_buffers[node]
        if len(buf) > self._stream_window:
            buf = "…" + buf[-self._stream_window:]
        body = escape(buf)
        static.update(f"[bold cyan]── {escape(label)} ──[/]\n{body}")
        # Pin the scroll view to the bottom so the latest tokens stay
        # visible without the user having to scroll manually.
        try:
            self.query_one("#stream-scroll", VerticalScroll).scroll_end(animate=False)
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------
    # UI helpers
    # ------------------------------------------------------------------

    def _set_table_status(self, node: str) -> None:
        st = self._stage_state.get(node)
        if st is None:
            return
        table = self.query_one("#table", DataTable)
        time_text = st["time_text"]
        if not time_text and st["status"] == "running" and st["started"] is not None:
            time_text = _fmt_elapsed(time.time() - st["started"])
        try:
            table.update_cell(node, "status", _status_cell(st["status"]))
            table.update_cell(node, "time", time_text or "—")
            table.update_cell(node, "in", _fmt_int(st["in"]))
            table.update_cell(node, "out", _fmt_int(st["out"]))
            table.update_cell(node, "cost", _fmt_cost(st["cost"]))
        except Exception:  # noqa: BLE001
            # Row may not exist for unknown nodes; we add them via
            # _register_dynamic_stage when that happens.
            pass

    def _register_dynamic_stage(self, node: str) -> None:
        """Add a row for a node we didn't pre-list (defensive)."""
        label = node.replace("_", " ").title()
        self._labels[node] = label
        self._stage_state[node] = {
            "label": label,
            "status": "pending",
            "started": None,
            "ended": None,
            "time_text": "",
            "in": 0,
            "out": 0,
            "cost": 0.0,
        }
        table = self.query_one("#table", DataTable)
        try:
            table.add_row(
                label, _status_cell("pending"), "—", "—", "—", "—",
                key=node,
            )
        except Exception:  # noqa: BLE001
            pass

    def _next_running(self, *, exclude: str | None = None) -> str | None:
        for key, st in self._stage_state.items():
            if key == exclude:
                continue
            if st["status"] == "running":
                return key
        return None

    def _refresh_status(self) -> None:
        elapsed = _fmt_elapsed(time.time() - self._start_time)
        provider = self.config.get("provider", "openrouter")
        bar = (
            f"[b]Manuscript:[/b] {escape(self.manuscript)}   "
            f"[b]Provider:[/b] {escape(provider)}   "
            f"[b]Model:[/b] {escape(self.config['reasoning_model'])}\n"
            f"[b]Elapsed:[/b] {elapsed}   "
            f"[b]Tokens:[/b] {_fmt_int(self._total_in)} in / {_fmt_int(self._total_out)} out   "
            f"[b]Cost:[/b] [green]${self._total_cost:.4f}[/green]"
        )
        self.query_one("#status", Static).update(bar)

    def _tick_clock(self) -> None:
        self._refresh_status()
        # Repaint running rows so their elapsed time advances live.
        for key, st in self._stage_state.items():
            if st["status"] == "running":
                self._set_table_status(key)

    # ------------------------------------------------------------------
    # Keybindings
    # ------------------------------------------------------------------

    def action_cycle_focus(self) -> None:
        """Tab through agents whose buffers have content."""
        keys = [k for k in self._stage_state if self._agent_buffers.get(k)]
        if not keys:
            return
        try:
            idx = keys.index(self._focused_node or "")
        except ValueError:
            idx = -1
        self._focused_node = keys[(idx + 1) % len(keys)]
        self._stream_dirty = True
        self._flush_stream()


def _fmt_int(value: int) -> str:
    if not value:
        return "—"
    return f"{value:,}"


def _fmt_cost(value: float) -> str:
    if value <= 0:
        return "—"
    if value < 0.01:
        return f"${value:.4f}"
    return f"${value:.3f}"


def run_tui(manuscript: str, config: dict) -> None:
    ReviewApp(manuscript, config).run()
