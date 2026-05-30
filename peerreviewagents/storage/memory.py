"""Cross-run review memory: pending → resolved → retrieved on future runs.

Borrowed from TradingAgents' two-phase reflection pattern. The log is a
single append-only markdown file with HTML-comment record separators so
entries can be located and patched in place after the resolution phase.

Lifecycle
---------
1. **Append-pending** (end of every review): the editor's decision lands
   in the log with ``status: pending``. The entry carries the manuscript
   title + abstract + per-reviewer scores so future runs can retrieve it
   by topic similarity.
2. **Mark-resolved** (when ground-truth arrives, manual via
   ``peerreview outcome <job_id> <outcome>``): the entry's status flips
   to ``resolved``, the outcome is recorded, and the LLM is asked for a
   ≤4-sentence lesson via :class:`peerreviewagents.agents.schemas.MemoryReflection`.
3. **Retrieve** (start of every future run, used by the meta-reviewer):
   :meth:`MemoryLog.get_past_context` BM25-ranks resolved entries by
   ``query`` (typically ``title + abstract``) and returns the top-K
   formatted as a "prior calibration" prompt block.

Storage format
--------------
::

    <!-- BEGIN ENTRY job=<id> status=<pending|resolved> ts=<iso> -->
    ## <job_id> — <title>
    - timestamp: <iso>
    - decision: <verdict>
    - avg_score: <float>
    - reviewers: <comma-separated names>
    - status: <pending|resolved>
    - outcome: <set when resolved>

    ### Abstract (for retrieval)
    <abstract or first 500 chars of manuscript>

    ### Draft summary
    <decision letter excerpt>

    ### Lesson  (set when resolved)
    <reflection.lesson>

    ### Applies when  (set when resolved)
    - <reflection.applies_when[0]>
    - ...

    <!-- END ENTRY -->
"""

from __future__ import annotations

import datetime as _dt
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


_BEGIN_RE = re.compile(
    r"<!--\s*BEGIN ENTRY job=(?P<id>[^\s]+)\s+status=(?P<status>\w+)\s+ts=(?P<ts>[^\s]+)\s*-->"
)
_END_MARKER = "<!-- END ENTRY -->"

_VALID_OUTCOMES = {"accepted", "rejected", "minor", "major", "withdrawn"}


@dataclass
class MemoryEntry:
    job_id: str
    status: str            # "pending" | "resolved"
    timestamp: str
    title: str
    abstract: str
    decision: str
    avg_score: float
    reviewer_names: list[str]
    draft_summary: str
    outcome: str = ""      # set on resolve
    lesson: str = ""       # set on resolve
    applies_when: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.applies_when is None:
            self.applies_when = []

    def to_block(self) -> str:
        parts: list[str] = [
            f"<!-- BEGIN ENTRY job={self.job_id} status={self.status} ts={self.timestamp} -->",
            f"## {self.job_id} — {self.title}",
            f"- timestamp: {self.timestamp}",
            f"- decision: {self.decision}",
            f"- avg_score: {self.avg_score:.2f}",
            f"- reviewers: {', '.join(self.reviewer_names)}",
            f"- status: {self.status}",
        ]
        if self.outcome:
            parts.append(f"- outcome: {self.outcome}")
        parts += [
            "",
            "### Abstract (for retrieval)",
            (self.abstract or "(no abstract)").strip(),
        ]
        if self.draft_summary:
            parts += ["", "### Draft summary", self.draft_summary.strip()]
        if self.lesson:
            parts += ["", "### Lesson", self.lesson.strip()]
        if self.applies_when:
            parts += ["", "### Applies when"]
            parts += [f"- {x}" for x in self.applies_when]
        parts += ["", _END_MARKER, ""]
        return "\n".join(parts)


class MemoryLog:
    """File-backed log of pending + resolved review decisions."""

    def __init__(self, path: str | os.PathLike):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.write_text(
                "# PeerReviewAgents — review memory log\n\n", encoding="utf-8"
            )

    # --- write paths --------------------------------------------------------

    def append_pending(
        self,
        *,
        job_id: str,
        title: str,
        abstract: str,
        decision: str,
        draft_summary: str,
        reports: Iterable[dict],
    ) -> MemoryEntry:
        """Append a new ``status=pending`` entry. Returns it."""
        reports = list(reports)
        scores = [float(r.get("score", 0)) for r in reports]
        avg = sum(scores) / len(scores) if scores else 0.0
        entry = MemoryEntry(
            job_id=job_id,
            status="pending",
            timestamp=_dt.datetime.now().isoformat(timespec="seconds"),
            title=title or "(untitled)",
            abstract=(abstract or "").strip(),
            decision=decision or "",
            avg_score=avg,
            reviewer_names=[str(r.get("reviewer", "?")) for r in reports],
            draft_summary=(draft_summary or "").strip(),
        )
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write("\n")
            fh.write(entry.to_block())
        return entry

    def mark_resolved(
        self,
        job_id: str,
        outcome: str,
        *,
        llm: Any | None = None,
        config: dict | None = None,
    ) -> MemoryEntry:
        """Find ``job_id``, flip status to resolved, run reflection if ``llm``.

        ``outcome`` must be one of :data:`_VALID_OUTCOMES`. Raises
        ``KeyError`` if no matching entry exists.
        """
        if outcome not in _VALID_OUTCOMES:
            raise ValueError(
                f"outcome {outcome!r} not in {sorted(_VALID_OUTCOMES)}"
            )
        entries = self._read_all()
        idx = next((i for i, e in enumerate(entries) if e.job_id == job_id), -1)
        if idx < 0:
            raise KeyError(f"no memory entry for job_id={job_id!r}")
        entry = entries[idx]
        entry.status = "resolved"
        entry.outcome = outcome
        if llm is not None:
            reflection = _reflect(llm, entry, outcome, config or {})
            entry.lesson = reflection.lesson
            entry.applies_when = list(reflection.applies_when)
        self._rewrite_all(entries)
        return entry

    # --- read paths ---------------------------------------------------------

    def get_past_context(self, query: str, k: int = 3) -> str:
        """Return the top-K resolved entries most similar to ``query``,
        formatted as a prompt-injectable block. Empty string when there
        are no resolved entries yet.
        """
        resolved = [e for e in self._read_all() if e.status == "resolved" and e.lesson]
        if not resolved or k <= 0:
            return ""
        ranked = _rank_bm25(query, resolved, k=k)
        if not ranked:
            return ""
        parts = ["### Prior calibration (lessons from similar past reviews)"]
        for entry in ranked:
            parts.append("")
            parts.append(f"**{entry.title}** — decision was {entry.decision}, outcome was {entry.outcome}")
            parts.append(f"_Lesson:_ {entry.lesson}")
            if entry.applies_when:
                parts.append(f"_Applies when:_ {', '.join(entry.applies_when)}")
        return "\n".join(parts)

    # --- internals ----------------------------------------------------------

    def _read_all(self) -> list[MemoryEntry]:
        if not self.path.exists():
            return []
        text = self.path.read_text(encoding="utf-8")
        out: list[MemoryEntry] = []
        for block in _split_blocks(text):
            entry = _parse_block(block)
            if entry is not None:
                out.append(entry)
        return out

    def _rewrite_all(self, entries: list[MemoryEntry]) -> None:
        header = "# PeerReviewAgents — review memory log\n\n"
        body = "\n".join(e.to_block() for e in entries)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(header + body, encoding="utf-8")
        tmp.replace(self.path)


# --- block (de)serialization -----------------------------------------------


def _split_blocks(text: str) -> list[str]:
    """Yield every ``<!-- BEGIN ENTRY ... --> ... <!-- END ENTRY -->`` block."""
    out: list[str] = []
    for m in _BEGIN_RE.finditer(text):
        end = text.find(_END_MARKER, m.start())
        if end < 0:
            continue
        out.append(text[m.start():end + len(_END_MARKER)])
    return out


def _parse_block(block: str) -> MemoryEntry | None:
    m = _BEGIN_RE.search(block)
    if not m:
        return None
    job_id = m.group("id")
    status = m.group("status")
    ts = m.group("ts")

    # Body line-by-line; tolerant of missing optional fields.
    body = block[m.end():block.rfind(_END_MARKER)].strip()
    lines = body.splitlines()

    title = ""
    decision = ""
    avg = 0.0
    reviewers: list[str] = []
    outcome = ""
    abstract_parts: list[str] = []
    draft_parts: list[str] = []
    lesson_parts: list[str] = []
    applies: list[str] = []

    section: str | None = None
    for line in lines:
        s = line.rstrip()
        if s.startswith("## ") and " — " in s:
            # Title heading line
            try:
                title = s.split(" — ", 1)[1].strip()
            except IndexError:
                pass
            continue
        if s.startswith("### "):
            section = s[4:].strip().lower()
            continue
        if section is None:
            if s.startswith("- decision:"):
                decision = s.split(":", 1)[1].strip()
            elif s.startswith("- avg_score:"):
                try:
                    avg = float(s.split(":", 1)[1].strip())
                except ValueError:
                    pass
            elif s.startswith("- reviewers:"):
                reviewers = [
                    p.strip() for p in s.split(":", 1)[1].split(",") if p.strip()
                ]
            elif s.startswith("- outcome:"):
                outcome = s.split(":", 1)[1].strip()
            continue
        # Inside a section
        if section.startswith("abstract"):
            abstract_parts.append(s)
        elif section.startswith("draft"):
            draft_parts.append(s)
        elif section.startswith("lesson"):
            lesson_parts.append(s)
        elif section.startswith("applies"):
            stripped = s.lstrip("- ").strip()
            if stripped:
                applies.append(stripped)

    return MemoryEntry(
        job_id=job_id,
        status=status,
        timestamp=ts,
        title=title,
        abstract="\n".join(abstract_parts).strip(),
        decision=decision,
        avg_score=avg,
        reviewer_names=reviewers,
        draft_summary="\n".join(draft_parts).strip(),
        outcome=outcome,
        lesson="\n".join(lesson_parts).strip(),
        applies_when=applies,
    )


# --- reflection + retrieval -------------------------------------------------


def _reflect(llm, entry: MemoryEntry, outcome: str, config: dict):
    """Ask the LLM for a ≤4-sentence lesson comparing decision to outcome."""
    # Local import keeps this module free of an unconditional schemas
    # dependency for callers that only need read / append.
    from ..agents.schemas import MemoryReflection
    from ..agents.utils.structured import invoke_structured

    system = (
        "You are reviewing the editorial board's prior decision on a "
        "manuscript now that the real outcome is known. Return a structured "
        "MemoryReflection with one short lesson the panel should carry into "
        "future similar manuscripts."
    )
    user = (
        f"Manuscript title: {entry.title}\n"
        f"Abstract: {entry.abstract}\n\n"
        f"Panel decision (then): {entry.decision} (avg score {entry.avg_score:.2f})\n"
        f"Ground-truth outcome (now): {outcome}\n\n"
        f"Draft summary the editor wrote at the time:\n{entry.draft_summary}\n\n"
        "Write the lesson. Keep it concrete and falsifiable; avoid "
        "platitudes. Name the specific manuscript pattern this lesson "
        "applies to in applies_when."
    )
    result = invoke_structured(
        llm, MemoryReflection, config, system, user,
    )
    return result.instance


def _rank_bm25(query: str, entries: list[MemoryEntry], *, k: int) -> list[MemoryEntry]:
    """Rank entries by relevance to ``query`` over (title + abstract).

    Uses BM25 when the log is large enough that IDF is meaningful
    (4+ resolved entries), falls back to a simple TF-overlap ranker
    for tiny corpora (BM25's IDF collapses to 0 when a term appears
    in nearly every doc out of a 2-3 doc set). Also falls back to
    TF-overlap when ``rank_bm25`` isn't installed.
    """
    corpus = [_tokenize(f"{e.title} {e.abstract}") for e in entries]
    if not any(corpus):
        return entries[-k:][::-1]
    q_tokens = _tokenize(query)

    if len(entries) >= 4:
        try:
            from rank_bm25 import BM25Okapi
        except ImportError:
            pass
        else:
            scores = BM25Okapi(corpus).get_scores(q_tokens)
            ranked = sorted(zip(scores, entries), key=lambda x: x[0], reverse=True)
            picked = [e for s, e in ranked[:k] if s > 0]
            if picked:
                return picked

    # TF-overlap fallback (well-defined for any corpus size).
    q_set = set(q_tokens)
    scored: list[tuple[float, MemoryEntry]] = []
    for doc, entry in zip(corpus, entries):
        overlap = sum(1 for t in doc if t in q_set)
        if overlap == 0:
            continue
        # Light length normalization so a long doc that mentions a few
        # query terms doesn't beat a short doc full of them.
        score = overlap / (len(doc) ** 0.5 + 1)
        scored.append((score, entry))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [e for _, e in scored[:k]] or entries[-k:][::-1]


def _tokenize(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", (text or "").lower())
