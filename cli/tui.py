"""Textual TUI for PeerReviewAgents: live agent progress + final decision."""

from __future__ import annotations

from textual.app import App, ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import DataTable, Footer, Header, Log, Static

from peerreviewagents.agents.utils.memory import append_memory
from peerreviewagents.graph.review_graph import PeerReviewGraph
from peerreviewagents.reports import write_reports

_VERDICT = {"accept": "ACCEPT", "minor": "MINOR REVISION", "major": "MAJOR REVISION", "reject": "REJECT"}
_VALID_DECISIONS = set(_VERDICT)


class ReviewApp(App):
    CSS = """
    #status { height: auto; padding: 1; background: $boost; }
    #table { width: 40%; }
    #log { width: 60%; border: round $primary; }
    #decision { height: auto; padding: 1; border: round $success; }
    """
    BINDINGS = [("q", "quit", "Quit")]

    def __init__(self, manuscript: str, config: dict):
        super().__init__()
        self.manuscript = manuscript
        self.config = config
        self.final: dict = {}

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static(
            f"Manuscript: {self.manuscript}\n"
            f"Provider: {self.config['provider']}  deep={self.config['deep_think_llm']}  "
            f"quick={self.config['quick_think_llm']}  research={self.config['research_enabled']}",
            id="status",
        )
        with Horizontal():
            yield DataTable(id="table")
            yield Log(id="log", highlight=True)
        yield Static("Running…", id="decision")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#table", DataTable)
        table.add_columns("Stage", "Status")
        table.add_row("ingest", "pending", key="ingest")
        for name in self.config["reviewer_set"]:
            table.add_row(f"reviewer: {name}", "pending", key=f"reviewer_{name}")
        for stage in ("advocate", "skeptic", "meta_reviewer"):
            table.add_row(stage, "pending", key=stage)
        for name in ("rigor", "reproducibility", "ethics", "citations"):
            table.add_row(f"integrity: {name}", "pending", key=f"integrity_{name}")
        table.add_row("editor", "pending", key="editor")
        self.run_review()

    def run_review(self) -> None:
        self.run_worker(self._review_worker, thread=True, exclusive=True)

    def _review_worker(self) -> None:
        graph = PeerReviewGraph(self.config)
        log = self.query_one("#log", Log)
        table = self.query_one("#table", DataTable)
        for node, partial in graph.stream(self.manuscript):
            self.final.update(partial)
            self.call_from_thread(log.write_line, f"✓ {node}")
            # Ingest fans out into two events so the row reflects progress
            # while marker is parsing (which can take minutes on a real PDF).
            if node == "_ingest_start":
                row_key, status = "ingest", "running"
            elif node == "_ingest":
                row_key, status = "ingest", "done"
            else:
                row_key, status = node, "done"
            try:
                self.call_from_thread(table.update_cell, row_key, "Status", status)
            except Exception:  # noqa: BLE001
                pass

        decision = self.final.get("decision")
        if decision not in _VALID_DECISIONS or not self.final.get("reports"):
            errors = self.final.get("errors", []) or ["(no error details collected)"]
            err_lines = "\n".join(f"  • {e}" for e in errors)
            msg = (
                "[b red]REVIEW FAILED — no report written.[/b red]\n"
                f"{err_lines}"
            )
            self.call_from_thread(self.query_one("#decision", Static).update, msg)
            return

        run_dir = write_reports(self.final)
        try:
            append_memory(self.final)
        except Exception:  # noqa: BLE001
            pass
        msg = f"[b]DECISION:[/b] {_VERDICT[decision]}\nReports written to: {run_dir}"
        self.call_from_thread(self.query_one("#decision", Static).update, msg)


def run_tui(manuscript: str, config: dict) -> None:
    ReviewApp(manuscript, config).run()
