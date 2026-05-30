"""In-memory job state for the single-job MVP server.

A ``JobState`` keeps the streamed token buffer for every agent, the
accumulated LangGraph state (so we can serve per-agent reports on
demand), the run's status, and the path of the on-disk reports
directory once writing completes.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from threading import Lock
from typing import Any


JOB_STATUS = ("pending", "running", "done", "error")

# Display order of the canonical agents in the office. The frontend reads
# this from /jobs/<id> so it can lay sprites out deterministically.
AGENT_LAYOUT: list[dict[str, Any]] = [
    # Reviewers — 8 desks arranged in a 4x2 grid in the bullpen
    {"name": "reviewer_methodology",   "label": "Methodology",     "role": "reviewer",  "emoji": "🔬"},
    {"name": "reviewer_data_analysis", "label": "Data Analysis",   "role": "reviewer",  "emoji": "📊"},
    {"name": "reviewer_novelty",       "label": "Novelty",         "role": "reviewer",  "emoji": "💡"},
    {"name": "reviewer_clarity",       "label": "Clarity",         "role": "reviewer",  "emoji": "✍️"},
    {"name": "reviewer_literature",    "label": "Literature",      "role": "reviewer",  "emoji": "📚"},
    {"name": "reviewer_rigor",         "label": "Rigor",           "role": "reviewer",  "emoji": "🧪"},
    {"name": "reviewer_reproducibility","label": "Reproducibility","role": "reviewer",  "emoji": "🔁"},
    {"name": "reviewer_ethics",        "label": "Ethics",          "role": "reviewer",  "emoji": "⚖️"},
    # Debate stage
    {"name": "advocate",               "label": "Advocate",        "role": "debate",    "emoji": "🗣️"},
    {"name": "skeptic",                "label": "Skeptic",         "role": "debate",    "emoji": "🤨"},
    # Synthesis room
    {"name": "meta_reviewer",          "label": "Area Chair",      "role": "synthesis", "emoji": "🧑‍⚖️"},
    {"name": "author_rebuttal",        "label": "Author",          "role": "author",    "emoji": "✒️"},
    {"name": "editor",                 "label": "Editor-in-Chief", "role": "editor",    "emoji": "👔"},
    # Post-decision: venue suggestions
    {"name": "journal_recommender",    "label": "Journal Scout",   "role": "recommend", "emoji": "🗺️"},
]

AGENT_NAMES = [a["name"] for a in AGENT_LAYOUT]


@dataclass
class JobState:
    id: str
    manuscript_path: str
    manuscript_filename: str
    status: str = "pending"
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    decision: str | None = None
    report_dir: str | None = None
    total_cost: float = 0.0
    errors: list[str] = field(default_factory=list)
    # Per-agent fields populated from the event stream.
    agent_buffers: dict[str, str] = field(default_factory=dict)
    agent_status: dict[str, str] = field(default_factory=dict)
    agent_usage: dict[str, dict[str, float]] = field(default_factory=dict)
    # Accumulated ReviewState so we can serve per-agent completed bodies.
    accumulated: dict[str, Any] = field(default_factory=dict)

    def public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "manuscript_filename": self.manuscript_filename,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "decision": self.decision,
            "report_dir": self.report_dir,
            "total_cost": self.total_cost,
            "errors": list(self.errors),
            "agent_status": dict(self.agent_status),
            "agent_usage": dict(self.agent_usage),
            "agents": AGENT_LAYOUT,
        }


class JobManager:
    """Single-job manager.

    Keeping it single-job means we can pin one global observer queue to
    the active job — matching how LangGraph's thread-local plumbing
    already works ([[observability.py]]) — without inventing job-scoped
    callback contexts.
    """

    def __init__(self):
        self._lock = Lock()
        self._jobs: dict[str, JobState] = {}
        self._active_id: str | None = None

    def create(self, manuscript_path: str, manuscript_filename: str) -> JobState:
        job_id = uuid.uuid4().hex[:12]
        job = JobState(
            id=job_id,
            manuscript_path=manuscript_path,
            manuscript_filename=manuscript_filename,
        )
        with self._lock:
            self._jobs[job_id] = job
        return job

    def get(self, job_id: str) -> JobState | None:
        with self._lock:
            return self._jobs.get(job_id)

    def has_active(self) -> bool:
        with self._lock:
            if self._active_id is None:
                return False
            job = self._jobs.get(self._active_id)
            return job is not None and job.status in ("pending", "running")

    def set_active(self, job_id: str | None) -> None:
        with self._lock:
            self._active_id = job_id

    @property
    def active_id(self) -> str | None:
        with self._lock:
            return self._active_id
