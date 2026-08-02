"""Round records: the machine-readable trace one review round leaves for the next.

A revision round has to answer "did they do what we asked?", and that
question is only answerable if the previous round wrote down what it asked
for in a form something else can read. Rendered markdown is not that form —
``decision_letter.md`` flattens :class:`EditorDecisionOutput.required_revisions`
into numbered bullets, and parsing them back out is exactly the string
matching the schema layer exists to avoid.

So every run writes ``round.json`` alongside its markdown. Two properties
carry the design:

* **Stable ids.** ``R1-03`` names the third required revision of round 1 for
  the life of the manuscript. Round 2 reports on it under that id, round 3
  can say it has been open for two rounds, and metrics join on it. Reviewer
  weaknesses get ids the same way (``methodology-2``) so a reviewer can be
  asked about its *own* prior points.
* **No copy of the manuscript.** The record stores the ingest cache key
  instead. :mod:`.ingest.cache` is already keyed by file content, so the
  previous draft's parsed text is recoverable for the diff without a second
  copy on disk — and if the cache was cleared, the round degrades to a
  no-diff review rather than failing.

Records are plain dataclasses, not pydantic models: this is data we write
and read, not an LLM output that needs schema-constrained generation. That
mirrors :mod:`.eval.schema`.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from typing import Any

SCHEMA_VERSION = 1
ROUND_FILENAME = "round.json"


def revision_id(round_no: int, index: int) -> str:
    """Stable id for the ``index``-th required revision of ``round_no``."""
    return f"R{round_no}-{index + 1:02d}"


def weakness_id(reviewer: str, index: int) -> str:
    """Stable id for one reviewer's ``index``-th weakness."""
    return f"{reviewer}-{index + 1}"


@dataclass
class RequiredRevision:
    """One numbered item the editor asked the authors to do."""

    id: str
    text: str
    # Which reviewer's critique drove it, when attributable ("" if the editor
    # raised it from an audit or on its own).
    source_reviewer: str = ""
    # True when leaving it undone should block acceptance. Round 1 has no
    # structured signal for this, so it defaults False and the compliance
    # auditor decides per item in later rounds.
    blocking: bool = False


@dataclass
class PriorWeakness:
    """One weakness a reviewer raised, addressable by id in a later round."""

    id: str
    text: str


@dataclass
class PriorReviewerReport:
    """What a later round needs to know about one reviewer's prior pass."""

    reviewer: str
    score: float
    confidence: float
    weaknesses: list[PriorWeakness] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)


@dataclass
class RoundRecord:
    """The structured trace of one completed review round."""

    schema_version: int
    round: int
    job_id: str
    manuscript_title: str
    # Ingest cache key of the manuscript reviewed in THIS round, so the next
    # round can diff against it. Empty when the parse wasn't cached.
    manuscript_cache_key: str
    decision: str
    weighted_score: float | None
    required_revisions: list[RequiredRevision] = field(default_factory=list)
    minor_suggestions: list[str] = field(default_factory=list)
    reviewer_reports: list[PriorReviewerReport] = field(default_factory=list)
    # job_id of the round this one revised, forming the lineage back to round 1.
    prior_job_id: str = ""
    # True when the round ended at the desk (integrity or triage), so a later
    # round knows there was never a panel.
    desk_rejected: bool = False

    # --- serialization ---

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, ensure_ascii=False)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "RoundRecord":
        return cls(
            schema_version=int(raw.get("schema_version", SCHEMA_VERSION)),
            round=int(raw.get("round", 1)),
            job_id=str(raw.get("job_id", "")),
            manuscript_title=str(raw.get("manuscript_title", "")),
            manuscript_cache_key=str(raw.get("manuscript_cache_key", "")),
            decision=str(raw.get("decision", "")),
            weighted_score=raw.get("weighted_score"),
            required_revisions=[
                RequiredRevision(**r) for r in raw.get("required_revisions", [])
            ],
            minor_suggestions=list(raw.get("minor_suggestions", [])),
            reviewer_reports=[
                PriorReviewerReport(
                    reviewer=r.get("reviewer", ""),
                    score=float(r.get("score", 0)),
                    confidence=float(r.get("confidence", 0)),
                    weaknesses=[PriorWeakness(**w) for w in r.get("weaknesses", [])],
                    questions=list(r.get("questions", [])),
                )
                for r in raw.get("reviewer_reports", [])
            ],
            prior_job_id=str(raw.get("prior_job_id", "")),
            desk_rejected=bool(raw.get("desk_rejected", False)),
        )

    # --- lookups used by the revision agents ---

    def report_for(self, reviewer: str) -> PriorReviewerReport | None:
        return next((r for r in self.reviewer_reports if r.reviewer == reviewer), None)

    def revision_by_id(self, item_id: str) -> RequiredRevision | None:
        return next((r for r in self.required_revisions if r.id == item_id), None)

    def prior_report_block(self, reviewer: str) -> str:
        """Render one reviewer's prior pass for its own round-N prompt.

        Deliberately scoped to a single reviewer: the panel's independence
        rests on reviewers never seeing each other's reports, and a revision
        round must not quietly become the round where they do.
        """
        report = self.report_for(reviewer)
        if report is None:
            return ""
        lines = [
            f"## Your review in round {self.round}",
            "",
            f"You scored the manuscript {report.score:g}/5 "
            f"(confidence {report.confidence:g}/5).",
        ]
        if report.weaknesses:
            lines += ["", "Weaknesses you raised, by id:"]
            lines += [f"- [{w.id}] {w.text}" for w in report.weaknesses]
        if report.questions:
            lines += ["", "Questions you asked the authors:"]
            lines += [f"- {q}" for q in report.questions]
        return "\n".join(lines)

    def required_revisions_block(self) -> str:
        """Render the editor's numbered asks for the compliance auditor."""
        if not self.required_revisions:
            return "(the previous decision letter required no revisions)"
        lines = [f"## Required revisions from round {self.round}", ""]
        for item in self.required_revisions:
            source = f" (raised by {item.source_reviewer})" if item.source_reviewer else ""
            lines.append(f"- [{item.id}]{source} {item.text}")
        return "\n".join(lines)


# --- disk I/O ---------------------------------------------------------------


def save(record: RoundRecord, run_dir: str) -> str:
    """Write ``round.json`` into a finished run's report directory."""
    path = os.path.join(run_dir, ROUND_FILENAME)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(record.to_json())
    return path


def load(run_dir: str) -> RoundRecord:
    """Read the round record from a report directory.

    Raises ``FileNotFoundError`` with an actionable message when the
    directory predates round records — the CLI turns that into a clear
    "that run can't be revised" rather than a stack trace.
    """
    path = os.path.join(run_dir, ROUND_FILENAME)
    if not os.path.isfile(path):
        raise FileNotFoundError(
            f"No {ROUND_FILENAME} in {run_dir}. That review predates round "
            "records, so there is nothing structured to revise against; "
            "re-run the first-round review to produce one."
        )
    with open(path, "r", encoding="utf-8") as fh:
        return RoundRecord.from_dict(json.load(fh))


def resolve_run_dir(job_id: str, config: dict) -> str:
    """Turn a job id (or an explicit path) into a report directory."""
    if os.path.isdir(job_id):
        return job_id
    candidate = os.path.join(config.get("output_dir") or "", job_id)
    if os.path.isdir(candidate):
        return candidate
    raise FileNotFoundError(
        f"No review run found for '{job_id}'. Expected a directory at "
        f"{candidate} (the Job ID printed at the end of the first-round run)."
    )


def load_prior(job_id: str, config: dict) -> RoundRecord:
    """Resolve ``job_id`` and load its round record."""
    return load(resolve_run_dir(job_id, config))


# --- building a record from a finished run ----------------------------------


def build_from_state(state: dict, job_id: str, cache_key: str = "") -> RoundRecord:
    """Derive this run's record from the final graph state.

    Ids are assigned here, at the one point where the round is complete, so
    every consumer sees the same numbering.
    """
    prior = state.get("prior_round")
    round_no = (prior.round + 1) if prior is not None else 1
    config = state.get("config") or {}

    revisions = [
        RequiredRevision(
            id=revision_id(round_no, i),
            text=text,
            source_reviewer=_attribute(text, state),
        )
        for i, text in enumerate(state.get("required_revisions") or [])
    ]

    reports = [
        PriorReviewerReport(
            reviewer=r["reviewer"],
            score=float(r.get("score", 0)),
            confidence=float(r.get("confidence", 0)),
            weaknesses=[
                PriorWeakness(id=weakness_id(r["reviewer"], i), text=w)
                for i, w in enumerate(r.get("weaknesses") or [])
            ],
            questions=list(r.get("questions") or []),
        )
        for r in state.get("reports") or []
    ]

    return RoundRecord(
        schema_version=SCHEMA_VERSION,
        round=round_no,
        job_id=job_id,
        manuscript_title=state.get("manuscript_title", ""),
        manuscript_cache_key=cache_key,
        decision=state.get("decision", ""),
        weighted_score=_weighted_score(state),
        required_revisions=revisions,
        minor_suggestions=list(state.get("minor_suggestions") or []),
        reviewer_reports=reports,
        prior_job_id=str(config.get("revision_of") or ""),
        desk_rejected=bool(state.get("desk_rejected")),
    )


def _attribute(text: str, state: dict) -> str:
    """Best-effort: which reviewer's weakness most resembles this ask.

    Attribution is a convenience for the revision prompts ("your point R2-04
    is still open"), never load-bearing — an unattributed item is handled the
    same way, just without the reviewer's name attached.
    """
    words = {w for w in _normalize(text).split() if len(w) > 4}
    if not words:
        return ""
    best, best_overlap = "", 0.0
    for report in state.get("reports") or []:
        for weakness in report.get("weaknesses") or []:
            other = {w for w in _normalize(weakness).split() if len(w) > 4}
            if not other:
                continue
            overlap = len(words & other) / len(words)
            if overlap > best_overlap:
                best, best_overlap = report["reviewer"], overlap
    return best if best_overlap >= 0.35 else ""


def _normalize(text: str) -> str:
    return "".join(ch.lower() if ch.isalnum() else " " for ch in text)


def _weighted_score(state: dict) -> float | None:
    reports = state.get("reports") or []
    if not reports:
        return None
    total_w = sum(r.get("confidence", 0) for r in reports) or 1.0
    return round(
        sum(r.get("score", 0) * r.get("confidence", 0) for r in reports) / total_w, 4
    )
