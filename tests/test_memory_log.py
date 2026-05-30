"""Tests for the cross-run review memory log."""

from __future__ import annotations

from pathlib import Path

import pytest

from peerreviewagents.agents.schemas import MemoryReflection
from peerreviewagents.storage.memory import MemoryEntry, MemoryLog


SAMPLE_REPORTS = [
    {"reviewer": "methodology", "score": 4.0, "confidence": 4.0, "body": "..."},
    {"reviewer": "novelty",     "score": 3.0, "confidence": 5.0, "body": "..."},
    {"reviewer": "ethics",      "score": 5.0, "confidence": 3.0, "body": "..."},
]


def _log(tmp_path: Path) -> MemoryLog:
    return MemoryLog(tmp_path / "review_memory.md")


def test_append_pending_writes_entry(tmp_path):
    log = _log(tmp_path)
    entry = log.append_pending(
        job_id="abc123",
        title="A study of widgets",
        abstract="We propose a new method...",
        decision="minor",
        draft_summary="Editor approved minor revision.",
        reports=SAMPLE_REPORTS,
    )
    assert entry.status == "pending"
    assert entry.avg_score == pytest.approx(4.0)
    assert entry.reviewer_names == ["methodology", "novelty", "ethics"]

    # File contains the entry block.
    raw = log.path.read_text()
    assert "BEGIN ENTRY job=abc123 status=pending" in raw
    assert "A study of widgets" in raw


def test_round_trip_parses_back(tmp_path):
    log = _log(tmp_path)
    log.append_pending(
        job_id="job-1", title="Paper One",
        abstract="abstract one text",
        decision="major", draft_summary="draft one",
        reports=SAMPLE_REPORTS,
    )
    log.append_pending(
        job_id="job-2", title="Paper Two",
        abstract="abstract two text",
        decision="accept", draft_summary="draft two",
        reports=SAMPLE_REPORTS[:1],
    )
    entries = log._read_all()
    assert [e.job_id for e in entries] == ["job-1", "job-2"]
    assert entries[0].title == "Paper One"
    assert entries[1].decision == "accept"


def test_mark_resolved_without_llm_records_outcome(tmp_path):
    log = _log(tmp_path)
    log.append_pending(
        job_id="job-x", title="A paper", abstract="a",
        decision="major", draft_summary="d", reports=SAMPLE_REPORTS,
    )
    entry = log.mark_resolved("job-x", "accepted")
    assert entry.status == "resolved"
    assert entry.outcome == "accepted"
    # File reflects the new status.
    raw = log.path.read_text()
    assert "status=resolved" in raw
    assert "- outcome: accepted" in raw


def test_mark_resolved_with_stub_llm_records_lesson(tmp_path):
    log = _log(tmp_path)
    log.append_pending(
        job_id="job-y", title="Another paper", abstract="b",
        decision="reject", draft_summary="d", reports=SAMPLE_REPORTS,
    )
    stub_llm = _StubLLM(MemoryReflection(
        lesson="The panel under-weighted methodology when novelty was high.",
        applies_when=["high novelty score", "weak methodology score"],
    ))
    config = {"provider": "openrouter", "reasoning_model": "stub"}
    entry = log.mark_resolved("job-y", "accepted", llm=stub_llm, config=config)
    assert "under-weighted methodology" in entry.lesson
    assert "high novelty score" in entry.applies_when

    # Round-trip preserves lesson + applies_when.
    again = log._read_all()
    assert "under-weighted methodology" in again[0].lesson
    assert "high novelty score" in again[0].applies_when


def test_mark_resolved_unknown_job_raises(tmp_path):
    log = _log(tmp_path)
    with pytest.raises(KeyError, match="missing"):
        log.mark_resolved("missing", "accepted")


def test_mark_resolved_invalid_outcome_raises(tmp_path):
    log = _log(tmp_path)
    log.append_pending(
        job_id="z", title="t", abstract="a",
        decision="major", draft_summary="d", reports=SAMPLE_REPORTS,
    )
    with pytest.raises(ValueError, match="outcome"):
        log.mark_resolved("z", "kinda-accepted")  # type: ignore[arg-type]


def test_get_past_context_only_returns_resolved(tmp_path):
    log = _log(tmp_path)
    log.append_pending(
        job_id="pending-1", title="Unresolved widget paper",
        abstract="we present widgets and gadgets",
        decision="major", draft_summary="d", reports=SAMPLE_REPORTS,
    )
    # Pending entries are excluded from retrieval, even on direct match.
    assert log.get_past_context("widgets gadgets", k=3) == ""


def test_get_past_context_ranks_similar_higher(tmp_path):
    log = _log(tmp_path)
    log.append_pending(
        job_id="bio", title="Pyruvate kinase in arthritis",
        abstract="rheumatoid arthritis synovial macrophages glycolysis",
        decision="minor", draft_summary="d", reports=SAMPLE_REPORTS,
    )
    log.append_pending(
        job_id="ml", title="A new optimizer for transformers",
        abstract="adam variant for deep learning",
        decision="reject", draft_summary="d", reports=SAMPLE_REPORTS,
    )
    stub = _StubLLM(MemoryReflection(lesson="lesson", applies_when=[]))
    cfg = {"provider": "openrouter", "reasoning_model": "stub"}
    log.mark_resolved("bio", "accepted", llm=stub, config=cfg)
    log.mark_resolved("ml", "accepted", llm=stub, config=cfg)

    bio_query = "synovial macrophages glycolysis arthritis"
    out = log.get_past_context(bio_query, k=1)
    assert "Pyruvate kinase in arthritis" in out
    assert "optimizer for transformers" not in out


def test_memory_entry_block_round_trip(tmp_path):
    entry = MemoryEntry(
        job_id="x1", status="resolved",
        timestamp="2026-05-29T20:00:00",
        title="Title",
        abstract="line1\nline2",
        decision="major",
        avg_score=3.7,
        reviewer_names=["methodology", "novelty"],
        draft_summary="some draft",
        outcome="accepted",
        lesson="be skeptical of small N",
        applies_when=["small N", "single cluster"],
    )
    log = _log(tmp_path)
    log.path.write_text("# header\n\n" + entry.to_block(), encoding="utf-8")
    parsed = log._read_all()
    assert len(parsed) == 1
    p = parsed[0]
    assert p.job_id == "x1"
    assert p.status == "resolved"
    assert p.title == "Title"
    assert p.decision == "major"
    assert p.outcome == "accepted"
    assert p.lesson == "be skeptical of small N"
    assert p.applies_when == ["small N", "single cluster"]


# --- helpers ---------------------------------------------------------------


class _StubLLM:
    """LLM stub that returns a fixed MemoryReflection via with_structured_output."""

    def __init__(self, reflection: MemoryReflection):
        self._reflection = reflection

    def with_structured_output(self, _schema, **kwargs):
        return _StubChain(self._reflection, kwargs.get("include_raw", False))


class _StubChain:
    def __init__(self, instance, include_raw: bool):
        self._inst = instance
        self._include_raw = include_raw

    def invoke(self, _messages, **_kwargs):
        if self._include_raw:
            from langchain_core.messages import AIMessage
            return {"raw": AIMessage(content=""), "parsed": self._inst, "parsing_error": None}
        return self._inst
