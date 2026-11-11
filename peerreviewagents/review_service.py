"""Background engine reviews, shared by every server surface.

One job is one engine review: the PDF is imported into ``<output>/manuscript``,
reviewed in ``<output>/review`` and reported as ``REVIEW.md`` and ``REVIEW.txt``
in ``<output>``, which is exactly what ``peerreview review manuscript.pdf
<output>`` does. The layout, the refusals and the report-on-failure behaviour
all belong to :mod:`peerreviewagents.engine.bundle`; this module only confines
the paths a client may name and keeps the job state.

The MCP server and the web UI both run on this, so a review started from a
coding agent and one started from the browser cannot drift apart.
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .engine.api import capabilities
from .engine.bundle import (
    MANUSCRIPT_DIRECTORY,
    PUBLIC_MODES,
    BundleError,
    plan_review,
    review_options,
    review_outcome,
    run_progress,
    run_review_options,
)

MAX_RETAINED_JOBS = 100
MAX_LISTED_ARTIFACTS = 200
# Progress keys a client is allowed to see. The run records nothing else a
# caller needs, and an unfiltered record would leak local paths through
# ``error``.
PROGRESS_KEYS = ("status", "stage", "reviewers", "review_guard_status", "findings_delivered")


def _is_within(path: Path, roots: tuple[Path, ...]) -> bool:
    return any(path == root or root in path.parents for root in roots)


@dataclass
class ReviewJob:
    """Mutable state for one background engine review."""

    id: str
    manuscript_path: str
    options: dict[str, Any]
    status: str = "queued"
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    bundle: str | None = None
    run: str | None = None
    progress: dict[str, Any] = field(default_factory=dict)
    reports: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    cancel_requested: bool = False
    future: Future[None] | None = field(default=None, repr=False)

    def public(self) -> dict[str, Any]:
        """Job state with no local path in it, and the run's own progress."""
        private = [path for path in (self.manuscript_path, self.bundle, self.run) if path]
        live = run_progress(self.run) if self.run else {}
        progress = {key: value for key, value in (live or self.progress).items()
                    if key in PROGRESS_KEYS}
        return {
            "job_id": self.id,
            "status": self.status,
            "manuscript_name": Path(self.manuscript_path).name,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "review_mode": self.options.get("mode"),
            "review_policy": self.options.get("policy"),
            "model": self.options.get("model"),
            "literature_cutoff": self.options.get("literature_cutoff"),
            "progress": progress,
            "reports": sorted(self._report_names()),
            "errors": [_redacted(error, private) for error in self.errors],
            "cancel_requested": self.cancel_requested,
        }

    def _report_names(self) -> list[str]:
        """Report paths as the names :meth:`ReviewService.read_artifact` takes."""
        root = Path(self.bundle) if self.bundle else None
        names = []
        for report in self.reports:
            path = Path(report)
            try:
                names.append(path.relative_to(root).as_posix() if root else path.name)
            except ValueError:
                names.append(path.name)
        return names


def _redacted(text: str, private: list[str]) -> str:
    for path in sorted(private, key=len, reverse=True):
        text = str(text).replace(path, Path(path).name)
    return str(text)


class ReviewService:
    """Thread-safe background job service for engine reviews."""

    def __init__(
        self,
        max_workers: int = 2,
        *,
        input_roots: list[str | os.PathLike[str]] | None = None,
        output_root: str | os.PathLike[str] | None = None,
        max_jobs: int = MAX_RETAINED_JOBS,
    ):
        roots = input_roots or [Path.cwd()]
        self._input_roots = tuple(Path(root).expanduser().resolve() for root in roots)
        self._output_root = Path(output_root or Path.cwd() / "reviews").expanduser().resolve()
        self._max_jobs = max(1, max_jobs)
        self._lock = threading.Lock()
        self._jobs: dict[str, ReviewJob] = {}
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="peerreview-review",
        )

    @property
    def output_root(self) -> Path:
        """Where every review this service runs writes its run folder."""
        return self._output_root

    def start_review(
        self,
        manuscript_path: str,
        *,
        output_name: str | None = None,
        mode: str = "ensemble",
        reviewers: int = 2,
        provider: str | None = None,
        model: str | None = None,
        policy: str | None = None,
        token_budget: int | None = None,
        literature_cutoff: str | None = None,
    ) -> dict[str, Any]:
        """Import, review and report one manuscript in the background."""
        path = Path(manuscript_path).expanduser().resolve()
        if not _is_within(path, self._input_roots):
            raise PermissionError("manuscript is outside the configured input roots")
        if not path.exists():
            raise FileNotFoundError(f"manuscript not found: {path}")
        try:
            options = review_options(
                mode=mode,
                reviewers=reviewers,
                provider=provider,
                model=model,
                policy=policy,
                token_budget=token_budget,
                literature_cutoff=literature_cutoff,
            )
        except BundleError as error:
            raise ValueError(str(error)) from error
        job = ReviewJob(id=uuid.uuid4().hex[:12], manuscript_path=str(path), options=options)
        job.bundle = str(self._bundle_for(path, output_name, job.id))
        with self._lock:
            self._prune_finished_locked()
            if len(self._jobs) >= self._max_jobs:
                raise RuntimeError("too many active review jobs")
            self._jobs[job.id] = job
            job.future = self._executor.submit(self._run_job, job)
        return job.public()

    def get_status(self, job_id: str) -> dict[str, Any]:
        return self._get(job_id).public()

    def list_jobs(self) -> list[dict[str, Any]]:
        """Every retained job, newest first."""
        with self._lock:
            jobs = list(self._jobs.values())
        jobs.sort(key=lambda job: job.created_at, reverse=True)
        return [job.public() for job in jobs]

    def cancel(self, job_id: str) -> dict[str, Any]:
        """Cancel a queued review. A running one cannot be interrupted.

        An engine stage is one long model call, so there is nothing to stop
        between stages: a running job is marked and keeps going. The returned
        state says which of the two happened rather than implying the call was
        stopped.
        """
        job = self._get(job_id)
        with self._lock:
            job.cancel_requested = True
            if job.future and job.future.cancel():
                job.status = "cancelled"
                job.finished_at = time.time()
        state = job.public()
        state["cancelled"] = job.status == "cancelled"
        if not state["cancelled"]:
            state["note"] = (
                "the review is already running and a model call cannot be interrupted; "
                "it will finish and report"
            )
        return state

    def list_artifacts(self, job_id: str) -> list[dict[str, Any]]:
        """The reports and saved run files, named relative to the run folder."""
        job = self._get(job_id)
        if not job.bundle:
            return []
        root = Path(job.bundle)
        if not root.is_dir():
            return []
        found = []
        for path in sorted(root.rglob("*")):
            if len(found) >= MAX_LISTED_ARTIFACTS:
                break
            relative = path.relative_to(root)
            # The imported manuscript is the input, not a review artifact, and
            # its source images would fill the listing on their own.
            if relative.parts[0] == MANUSCRIPT_DIRECTORY or not path.is_file():
                continue
            found.append({"name": relative.as_posix(), "size": path.stat().st_size})
        return found

    def read_artifact(self, job_id: str, name: str, max_chars: int = 100_000) -> str:
        job = self._get(job_id)
        if not job.bundle:
            raise RuntimeError("review artifacts are not available yet")
        root = Path(job.bundle).resolve()
        path = (root / name).resolve()
        if not _is_within(path, (root,)) or not path.is_file():
            raise FileNotFoundError(f"artifact not found: {name}")
        if path.relative_to(root).parts[0] == MANUSCRIPT_DIRECTORY:
            raise FileNotFoundError(f"artifact not found: {name}")
        text = path.read_text(encoding="utf-8", errors="replace")
        if len(text) > max_chars:
            return text[:max_chars] + "\n\n[artifact truncated by the response limit]\n"
        return text

    def capabilities(self) -> dict[str, Any]:
        """What this service can be asked for: the engine's own record, plus limits."""
        record = dict(capabilities())
        record["review_modes"] = list(PUBLIC_MODES)
        record["study_arms"] = "named presets and study modes are command line only: docs/study-arms.md"
        record["input_roots"] = [root.name for root in self._input_roots]
        record["max_jobs"] = self._max_jobs
        return record

    def _bundle_for(self, manuscript: Path, output_name: str | None, job_id: str) -> Path:
        """The run folder this review owns, always inside the output root."""
        if output_name is None:
            return self._output_root / f"{manuscript.stem}-{job_id}"
        bundle = (self._output_root / output_name).resolve()
        if not _is_within(bundle, (self._output_root,)) or bundle == self._output_root:
            raise PermissionError("output_name must be a folder inside the configured output root")
        return bundle

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
        try:
            paths = plan_review(job.manuscript_path, job.bundle)
            job.run = str(paths.run)
            result = run_review_options(paths, job.options)
            job.progress = result["progress"] if isinstance(result["progress"], dict) else {}
            job.reports = list(result["reports"].values())
            if result["failure"] is not None:
                # The engine reports a stopped run rather than losing it, so the
                # reports above are real and the cause belongs with them.
                job.status = "error"
                job.errors.append("review did not finish: " + result["failure"])
                return
            outcome = review_outcome(result["progress"])
            job.status = "done-without-findings" if outcome["without_findings"] else "done"
            if outcome["note"]:
                job.errors.append(outcome["note"])
        except BundleError as error:
            job.status = "error"
            job.errors.append(str(error))
        except Exception as exc:  # noqa: BLE001
            job.status = "error"
            job.errors.append(f"review crashed: {type(exc).__name__}: {exc}")
        finally:
            job.finished_at = time.time()
