"""Asynchronous MCP tools for the PeerReviewAgents pipeline."""

from __future__ import annotations

import os
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..default_config import get_config
from ..graph.review_graph import PeerReviewGraph
from ..journals import list_journals
from ..reports import write_reports
from ..runtime.subscriptions import SUBSCRIPTION_PROVIDERS, validate_subscription_cli

_PROVIDERS = {"anthropic", "openai", "openrouter", *SUBSCRIPTION_PROVIDERS}
_FINAL_DECISIONS = {"accept", "minor", "major", "reject"}
_MANUSCRIPT_SUFFIXES = {".pdf", ".md", ".markdown", ".tex", ".txt"}
_MAX_DEBATE_ROUNDS = 5
_MAX_RETAINED_JOBS = 100


def _is_within(path: Path, roots: tuple[Path, ...]) -> bool:
    return any(path == root or root in path.parents for root in roots)


@dataclass
class ReviewJob:
    """Mutable state for one background review."""

    id: str
    manuscript_path: str
    config: dict[str, Any]
    status: str = "queued"
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    current_stage: str | None = None
    decision: str | None = None
    report_dir: str | None = None
    errors: list[str] = field(default_factory=list)
    cancel_requested: bool = False
    future: Future[None] | None = field(default=None, repr=False)

    def public(self) -> dict[str, Any]:
        errors = [str(error) for error in self.errors]
        for private_path in (self.manuscript_path, self.report_dir):
            if private_path:
                errors = [
                    error.replace(private_path, Path(private_path).name)
                    for error in errors
                ]
        return {
            "job_id": self.id,
            "status": self.status,
            "manuscript_name": Path(self.manuscript_path).name,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "current_stage": self.current_stage,
            "decision": self.decision,
            "errors": errors,
            "cancel_requested": self.cancel_requested,
        }


class ReviewService:
    """Thread-safe background job service used by the MCP transport."""

    def __init__(
        self,
        max_workers: int = 2,
        *,
        input_roots: list[str | os.PathLike[str]] | None = None,
        output_root: str | os.PathLike[str] | None = None,
        max_jobs: int = _MAX_RETAINED_JOBS,
    ):
        roots = input_roots or [Path.cwd()]
        self._input_roots = tuple(Path(root).expanduser().resolve() for root in roots)
        self._output_root = Path(output_root or Path.cwd() / "reports").expanduser().resolve()
        self._max_jobs = max(1, max_jobs)
        self._lock = threading.Lock()
        self._jobs: dict[str, ReviewJob] = {}
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="peerreview-mcp",
        )

    def start_review(
        self,
        manuscript_path: str,
        *,
        provider: str = "claude-code",
        model: str = "default",
        target_journal: str = "general",
        article_type: str = "",
        strictness: int = 3,
        debate_rounds: int = 2,
        enable_debate: bool = True,
        research_enabled: bool = False,
    ) -> dict[str, Any]:
        path = Path(manuscript_path).expanduser().resolve()
        if not _is_within(path, self._input_roots):
            raise PermissionError("manuscript is outside the configured input roots")
        if not path.is_file():
            raise FileNotFoundError(f"manuscript not found: {path}")
        if path.suffix.lower() not in _MANUSCRIPT_SUFFIXES:
            raise ValueError(
                f"unsupported manuscript type {path.suffix!r}. "
                f"Available: {sorted(_MANUSCRIPT_SUFFIXES)}"
            )
        provider = provider.strip().lower()
        if provider not in _PROVIDERS:
            raise ValueError(f"unknown provider {provider!r}. Available: {sorted(_PROVIDERS)}")
        if not 1 <= strictness <= 5:
            raise ValueError("strictness must be between 1 and 5")
        if not 0 <= debate_rounds <= _MAX_DEBATE_ROUNDS:
            raise ValueError(
                f"debate_rounds must be between 0 and {_MAX_DEBATE_ROUNDS}"
            )
        if provider in SUBSCRIPTION_PROVIDERS:
            validate_subscription_cli(provider)

        overrides: dict[str, Any] = {
            "provider": provider,
            "reasoning_model": model or "default",
            "single_model": True,
            "target_journal": target_journal,
            "article_type": article_type,
            "review_strictness": strictness,
            "max_debate_rounds": debate_rounds,
            "enable_debate": enable_debate,
            "research_enabled": research_enabled,
            "output_dir": str(self._output_root),
        }
        config = get_config(**overrides)
        job = ReviewJob(
            id=uuid.uuid4().hex[:12],
            manuscript_path=str(path),
            config=config,
        )
        with self._lock:
            self._prune_finished_locked()
            if len(self._jobs) >= self._max_jobs:
                raise RuntimeError("too many active review jobs")
            self._jobs[job.id] = job
            job.future = self._executor.submit(self._run_job, job)
        return job.public()

    def get_status(self, job_id: str) -> dict[str, Any]:
        return self._get(job_id).public()

    def cancel(self, job_id: str) -> dict[str, Any]:
        job = self._get(job_id)
        with self._lock:
            job.cancel_requested = True
            if job.future and job.future.cancel():
                job.status = "cancelled"
                job.finished_at = time.time()
        return job.public()

    def list_artifacts(self, job_id: str) -> list[dict[str, Any]]:
        job = self._get(job_id)
        if not job.report_dir:
            return []
        root = Path(job.report_dir)
        return [
            {"name": path.name, "size": path.stat().st_size}
            for path in sorted(root.iterdir())
            if path.is_file()
        ]

    def read_artifact(self, job_id: str, name: str, max_chars: int = 100_000) -> str:
        job = self._get(job_id)
        if not job.report_dir:
            raise RuntimeError("review artifacts are not available yet")
        root = Path(job.report_dir).resolve()
        path = (root / name).resolve()
        if path.parent != root or not path.is_file():
            raise FileNotFoundError(f"artifact not found: {name}")
        text = path.read_text(encoding="utf-8", errors="replace")
        if len(text) > max_chars:
            return text[:max_chars] + "\n\n[artifact truncated by MCP response limit]\n"
        return text

    def journals(self) -> list[dict[str, str]]:
        return [
            {"slug": journal.slug, "name": journal.name}
            for journal in list_journals(get_config())
        ]

    def _get(self, job_id: str) -> ReviewJob:
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            raise KeyError(f"unknown review job: {job_id}")
        return job

    def _prune_finished_locked(self) -> None:
        finished = sorted(
            (job for job in self._jobs.values() if job.finished_at is not None),
            key=lambda job: job.finished_at or 0,
        )
        while len(self._jobs) >= self._max_jobs and finished:
            job = finished.pop(0)
            del self._jobs[job.id]

    def _run_job(self, job: ReviewJob) -> None:
        job.status = "running"
        job.started_at = time.time()
        final: dict[str, Any] = {}
        try:
            graph = PeerReviewGraph(job.config)
            for stage, state in graph.stream(job.manuscript_path):
                job.current_stage = stage
                final = state
                if job.cancel_requested:
                    job.status = "cancelled"
                    return
            job.errors = list(final.get("errors") or [])
            job.decision = final.get("decision")
            if job.decision not in _FINAL_DECISIONS:
                job.status = "error"
                job.errors.append("pipeline did not produce a valid decision")
                return
            job.report_dir = write_reports(final)
            job.status = "done"
        except Exception as exc:  # noqa: BLE001
            job.status = "error"
            job.errors.append(f"pipeline crashed: {type(exc).__name__}: {exc}")
        finally:
            job.finished_at = time.time()
            job.current_stage = None


def create_server(service: ReviewService | None = None):
    """Create the FastMCP server, importing the optional SDK only when needed."""
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:
        raise RuntimeError(
            "The MCP server requires the optional dependency. "
            "Install with: pip install 'peerreviewagents[mcp]'"
        ) from exc

    reviews = service or ReviewService()
    server = FastMCP("Peer Review Agents")

    @server.tool()
    def start_peer_review(
        manuscript_path: str,
        provider: str = "claude-code",
        model: str = "default",
        target_journal: str = "general",
        article_type: str = "",
        strictness: int = 3,
        debate_rounds: int = 2,
        enable_debate: bool = True,
        research_enabled: bool = False,
    ) -> dict[str, Any]:
        """Start a background multi-agent review and return its job ID."""
        return reviews.start_review(
            manuscript_path,
            provider=provider,
            model=model,
            target_journal=target_journal,
            article_type=article_type,
            strictness=strictness,
            debate_rounds=debate_rounds,
            enable_debate=enable_debate,
            research_enabled=research_enabled,
        )

    @server.tool()
    def get_peer_review_status(job_id: str) -> dict[str, Any]:
        """Get the current stage, result, and errors for a review job."""
        return reviews.get_status(job_id)

    @server.tool()
    def cancel_peer_review(job_id: str) -> dict[str, Any]:
        """Request cancellation after the current model call finishes."""
        return reviews.cancel(job_id)

    @server.tool()
    def list_peer_review_artifacts(job_id: str) -> list[dict[str, Any]]:
        """List report files produced by a completed review."""
        return reviews.list_artifacts(job_id)

    @server.tool()
    def read_peer_review_artifact(job_id: str, name: str) -> str:
        """Read one report artifact by its filename."""
        return reviews.read_artifact(job_id, name)

    @server.tool()
    def list_peer_review_journals() -> dict[str, Any]:
        """List bundled target journal profiles."""
        journals = reviews.journals()
        return {"count": len(journals), "journals": journals}

    return server


def run() -> None:
    """Run the MCP server over standard input and output."""
    configured_roots = os.environ.get("PEERREVIEW_MCP_INPUT_ROOTS", "")
    input_roots = [root for root in configured_roots.split(os.pathsep) if root]
    output_root = os.environ.get("PEERREVIEW_MCP_OUTPUT_ROOT") or None
    service = ReviewService(
        input_roots=input_roots or None,
        output_root=output_root,
    )
    create_server(service).run(transport="stdio")
