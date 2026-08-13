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
  weaknesses get ids the same way (``methodology-2``) so the editor's asks
  can be attributed to the reviewer who raised them.

  This is load-bearing now rather than convenient. The reviewer panel is
  blind to the round — it is never told a previous one exists — so the R-list
  is the *entire* lineage of a manuscript through this pipeline. An id that
  changes between rounds severs it. See :func:`build_from_state` for how a
  carried item keeps the id it was born with.
* **No copy of the manuscript.** The record stores the ingest cache key and
  two hashes of what the round read, not the draft itself. The file hash is
  what a later round compares against to know whether a resubmission is
  byte-identical to the draft the previous panel saw — the one "what changed"
  fact that needs no re-parse and no converter agreement.

Records are plain dataclasses, not pydantic models: this is data we write
and read, not an LLM output that needs schema-constrained generation. That
mirrors :mod:`.eval.schema`.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from dataclasses import fields as dataclass_fields
from typing import Any

SCHEMA_VERSION = 1
ROUND_FILENAME = "round.json"

# An ask the editor restated from an earlier round, which it is instructed to
# write as "[R1-03] what specifically is still missing". The tag is the item's
# identity, not part of its text.
_CARRIED_TAG = re.compile(r"^\s*\[\s*R(?P<round>\d+)\s*-\s*(?P<index>\d+)\s*\]\s*")


def revision_id(round_no: int, index: int) -> str:
    """Stable id for the ``index``-th required revision of ``round_no``."""
    return f"R{round_no}-{index + 1:02d}"


def weakness_id(reviewer: str, index: int) -> str:
    """Stable id for one reviewer's ``index``-th weakness."""
    return f"{reviewer}-{index + 1}"


def _known_fields(cls, raw: dict) -> dict:
    """Keep only the keys ``cls`` declares, so newer records load under older code.

    A round.json written by a future schema version may carry fields this
    version has never heard of; passing them straight to the dataclass
    constructor turns a forward-compatible record into a TypeError, and a
    manuscript's whole revision lineage becomes unreadable over one added
    field.
    """
    names = {f.name for f in dataclass_fields(cls)}
    return {k: v for k, v in raw.items() if k in names}


def _score_or_none(value: Any) -> float | None:
    """A reviewer score from a raw record: a number, or None for an abstention."""
    return float(value) if isinstance(value, (int, float)) else None


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
    """What a later round needs to know about one reviewer's prior pass.

    ``score`` is None when the reviewer found nothing in its dimension to
    judge — the same nullable contract as
    :class:`~.agents.schemas.ReviewerOutput`. Coercing it to a number here
    would hand round N a score round N-1 never gave, and the editor's
    round-over-round delta would then show a movement that never happened.
    """

    reviewer: str
    score: float | None
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
    # Ingest cache key of the manuscript reviewed in THIS round. Empty when
    # the parse wasn't cached.
    manuscript_cache_key: str
    decision: str
    weighted_score: float | None
    required_revisions: list[RequiredRevision] = field(default_factory=list)
    minor_suggestions: list[str] = field(default_factory=list)
    reviewer_reports: list[PriorReviewerReport] = field(default_factory=list)
    # sha256 of the manuscript TEXT this round reviewed (the loader's
    # ingest["text_sha256"]). The cache key above names where the parse lived;
    # this names what the panel actually read, and it survives both a wiped
    # cache and a cache-key schema change — the key derivation has changed
    # under existing records before (cache v8). "" on records written before
    # the field existed.
    manuscript_text_sha256: str = ""
    # SHA-256 of the manuscript FILE's bytes, not its parsed text. This is
    # what a later round compares its own file hash against to establish that
    # a resubmission is byte-identical to the draft the previous panel read —
    # the one statement about "what changed" that costs nothing, needs no
    # re-parse, and cannot be wrong. The text hash cannot do that job: the
    # same PDF reads into different text under rustypaper 0.1.1 and 0.2.0, so
    # an unchanged file looked changed at exactly the upgrade boundary. Bytes
    # survive the upgrade. "" on records written before the field existed.
    manuscript_file_sha256: str = ""
    # job_id of the round this one revised, forming the lineage back to round 1.
    prior_job_id: str = ""
    # True when the round ended at the desk (triage), so a later
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
                RequiredRevision(**_known_fields(RequiredRevision, r))
                for r in raw.get("required_revisions", [])
            ],
            minor_suggestions=list(raw.get("minor_suggestions", [])),
            reviewer_reports=[
                PriorReviewerReport(
                    reviewer=r.get("reviewer", ""),
                    # A null score is a recorded abstention, not a zero — see
                    # PriorReviewerReport. float(None) here used to crash the
                    # load of any round whose panel had one.
                    score=_score_or_none(r.get("score")),
                    confidence=float(r.get("confidence") or 0),
                    weaknesses=[
                        PriorWeakness(**_known_fields(PriorWeakness, w))
                        for w in r.get("weaknesses", [])
                    ],
                    questions=list(r.get("questions", [])),
                )
                for r in raw.get("reviewer_reports", [])
            ],
            manuscript_text_sha256=str(raw.get("manuscript_text_sha256", "")),
        manuscript_file_sha256=str(raw.get("manuscript_file_sha256", "")),
            prior_job_id=str(raw.get("prior_job_id", "")),
            desk_rejected=bool(raw.get("desk_rejected", False)),
        )

    # --- lookups used by the revision agents ---

    def report_for(self, reviewer: str) -> PriorReviewerReport | None:
        return next((r for r in self.reviewer_reports if r.reviewer == reviewer), None)

    def revision_by_id(self, item_id: str) -> RequiredRevision | None:
        return next((r for r in self.required_revisions if r.id == item_id), None)

    def required_revisions_block(self) -> str:
        """Render the editor's numbered asks for the compliance auditor.

        One id per item, and the id is the item's own — a carried ask is
        stored under the id it was raised with (see :func:`_assign_ids`), so
        round 3 renders ``[R1-03]`` exactly once for an item that has been
        open since round 1 rather than a fresh id with the real one inside
        the sentence.
        """
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

    revisions = _assign_ids(state.get("required_revisions") or [], round_no, state)

    reports = [
        PriorReviewerReport(
            reviewer=r["reviewer"],
            # None survives as None: a reviewer that abstained must reach the
            # next round as an abstention, not as a zero that reads like the
            # panel's harshest verdict. float(None) here used to crash the
            # record build instead — and the bare `except` around the writer
            # meant no round.json and no trace of why.
            score=_score_or_none(r.get("score")),
            confidence=float(r.get("confidence") or 0),
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
        # Read off the state's ingest record, like the title: the loader
        # fingerprints every text it parses, so this is free — and it is what
        # lets a later round verify a caller-supplied baseline file without
        # the cache entry the key above names.
        manuscript_text_sha256=str(
            (state.get("ingest") or {}).get("text_sha256") or ""
        ),
        manuscript_file_sha256=str(
            (state.get("ingest") or {}).get("file_sha256") or ""
        ),
        prior_job_id=str(config.get("revision_of") or ""),
        desk_rejected=bool(state.get("desk_rejected")),
    )


def _assign_ids(
    texts: list[str], round_no: int, state: dict
) -> list[RequiredRevision]:
    """Give every ask an id, keeping the one a carried item already has.

    The editor is told to restate a still-open ask as ``[R1-03] <what is
    still missing>``, and it does. Re-enumerating those as ``R2-01`` used to
    store the original id buried inside the *text*, so the item rendered as
    "[R2-01] [R1-03] …" in round 3's compliance prompt — two ids for one ask,
    neither of which was reliably the one round 2's auditor had reported
    under. With the panel blinded, this list is the whole lineage of the
    manuscript, and an ask that changes identity between rounds breaks it.

    So a tagged item keeps its tag as its id and loses it from its text, and
    only genuinely new asks draw a fresh ``R{this round}-nn``. New asks are
    numbered among themselves, so round 3's first new item is ``R3-01``
    however many carried items sit above it.

    The id is re-rendered through :func:`revision_id` rather than copied
    verbatim, so ``[R1-3]`` and ``[R1-03]`` become the same id and
    ``revision_by_id("R1-03")`` resolves either way.
    """
    out: list[RequiredRevision] = []
    taken: set[str] = set()
    fresh = 0
    for raw in texts:
        text = str(raw)
        match = _CARRIED_TAG.match(text)
        if match:
            item_id = revision_id(
                int(match.group("round")), int(match.group("index")) - 1
            )
            text = text[match.end():].strip()
        else:
            item_id = ""
        # Also covers the pathological case of the editor restating two asks
        # under one id: the second gets a fresh id rather than silently
        # overwriting the first for every consumer that looks items up by id.
        while not item_id or item_id in taken:
            item_id = revision_id(round_no, fresh)
            fresh += 1
        taken.add(item_id)
        out.append(RequiredRevision(
            id=item_id,
            text=text,
            # Attributed on the stripped text: an id tag is not evidence about
            # which reviewer raised the point.
            source_reviewer=_attribute(text, state),
        ))
    return out


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
    """Confidence-weighted panel score, over the reviewers that actually scored.

    A null score means the dimension did not apply to this manuscript, so it
    leaves the average entirely — numerator *and* denominator. Counting it as
    a zero would punish a paper for not being the kind of paper that has a
    statistics section; leaving it in the denominator alone does the same
    thing more quietly. Returns None when nobody could score, which is a
    finding worth surfacing rather than a number worth inventing.
    """
    reports = [
        r for r in (state.get("reports") or [])
        if isinstance(r.get("score"), (int, float))
    ]
    if not reports:
        return None
    total_w = sum(r.get("confidence", 0) for r in reports) or 1.0
    return round(
        sum(r["score"] * r.get("confidence", 0) for r in reports) / total_w, 4
    )
