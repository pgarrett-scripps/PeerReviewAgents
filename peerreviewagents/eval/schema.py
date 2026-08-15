"""Data records for the evaluation harness, with JSONL (de)serialization.

Three records, written to disk as one JSON object per line:

  * :class:`CorpusItem`  — one labeled manuscript (PDF path + human ground
    truth from OpenReview). Lives in ``corpus.jsonl``.
  * :class:`Manifest`    — provenance stamped on every run so a result is
    reproducible: model, provider, git SHA, a digest of the run config, the
    source venue, and a leakage note.
  * :class:`RunRecord`   — the structured outcome of one pipeline run over one
    paper. Lives in ``runs.jsonl``, keyed by ``(paper_id, repeat)``.

Everything is a plain dataclass (not a pydantic model): these are data we
write and read, not LLM outputs that need schema-constrained generation.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import time
from dataclasses import asdict, dataclass, field
from typing import Any

# Config keys that actually change a result; the manifest digests exactly
# these so two runs with the same digest are comparable.
_DIGEST_KEYS = (
    "provider",
    "reasoning_model",
    "fast_model",
    "model",
    "models",
    "agent_models",
    "single_model",
    "review_strictness",
    "article_type",
    "target_journal",
    "max_debate_rounds",
    "enable_debate",
    "enable_journal_recommender",
    "desk_screen",
    "research_enabled",
    "temperature",
    "manuscript_char_budget",
)


@dataclass
class CorpusItem:
    """One labeled manuscript: the PDF plus its human ground truth."""

    id: str
    title: str
    pdf_path: str
    human_scores: list[float] = field(default_factory=list)
    human_mean: float | None = None
    human_decision: str | None = None          # normalized: "accept" | "reject"
    human_decision_raw: str = ""                # the venue's own label
    venue: str = ""
    year: str = ""
    source_url: str = ""

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> CorpusItem:
        known = {k: d[k] for k in cls.__dataclass_fields__ if k in d}
        return cls(**known)


@dataclass
class Manifest:
    """Provenance for one run — enough to reproduce and to spot leakage."""

    model: str
    provider: str
    git_sha: str
    config_digest: str
    created_at: float
    venue: str = ""
    leakage_note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RunRecord:
    """Outcome of one pipeline run over one paper."""

    paper_id: str
    repeat: int
    ok: bool
    system_decision: str | None
    system_weighted_score: float | None
    per_reviewer: list[dict[str, Any]] = field(default_factory=list)
    n_reviewers: int = 0
    cost_usd: float = 0.0
    latency_s: float = 0.0
    errors: list[str] = field(default_factory=list)
    manifest: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> RunRecord:
        known = {k: d[k] for k in cls.__dataclass_fields__ if k in d}
        return cls(**known)

    @property
    def key(self) -> tuple[str, int]:
        return (self.paper_id, self.repeat)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def git_sha() -> str:
    """Short HEAD SHA, with a ``+dirty`` suffix if the tree has changes.

    Returns ``"unknown"`` if we're not in a git checkout.
    """
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        if not sha:
            return "unknown"
        dirty = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        return f"{sha}+dirty" if dirty else sha
    except Exception:  # noqa: BLE001
        return "unknown"


def config_digest(config: dict[str, Any]) -> str:
    """Stable 12-char digest of the result-affecting config keys."""
    subset = {k: config.get(k) for k in _DIGEST_KEYS}
    blob = json.dumps(subset, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


def build_manifest(config: dict[str, Any], *, venue: str = "", leakage_note: str = "") -> Manifest:
    model = (
        config.get("reasoning_model")
        or config.get("model")
        or config.get("fast_model")
        or "unknown"
    )
    return Manifest(
        model=str(model),
        provider=str(config.get("provider", "unknown")),
        git_sha=git_sha(),
        config_digest=config_digest(config),
        created_at=time.time(),
        venue=venue,
        leakage_note=leakage_note,
    )


def read_jsonl(path: str) -> list[dict[str, Any]]:
    """Read a JSONL file into a list of dicts; missing file -> empty list."""
    import os

    if not os.path.exists(path):
        return []
    out: list[dict[str, Any]] = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def append_jsonl(path: str, line_obj: str) -> None:
    """Append one already-serialized JSON line to ``path`` (created if absent)."""
    import os

    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line_obj + "\n")
