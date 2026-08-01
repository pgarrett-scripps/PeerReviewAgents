"""Schema round-trip + structured-helper fallback tests."""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage
from pydantic import ValidationError

from peerreviewagents.agents.schemas import (
    AuthorRebuttalOutput,
    DebateOutput,
    EditorDecisionOutput,
    MetaReviewOutput,
    RebuttalConcession,
    RebuttalDisagreement,
    ReviewerOutput,
)
from peerreviewagents.agents.utils.structured import (
    StructuredResult,
    invoke_structured,
)

# ---------- schema construction + render ------------------------------------


def test_reviewer_output_renders_all_sections():
    r = ReviewerOutput(
        score=4,
        confidence=5,
        summary="Solid contribution.",
        strengths=["clear writing"],
        weaknesses=["small N"],
        questions=["what about Y?"],
    )
    md = r.to_markdown(role="Methodology Reviewer")
    assert md.startswith("# Methodology Reviewer")
    assert "## Summary" in md
    assert "## Strengths" in md and "- clear writing" in md
    assert "## Weaknesses" in md and "- small N" in md
    assert "## Questions" in md and "- what about Y?" in md


def test_reviewer_output_rejects_out_of_range():
    with pytest.raises(ValidationError):
        ReviewerOutput(score=7, confidence=3, summary="x")
    with pytest.raises(ValidationError):
        ReviewerOutput(score=3, confidence=0, summary="x")


def test_debate_output_renders():
    d = DebateOutput(argument="The empirical evidence is strong.",
                     key_points=["replicates prior", "fair baselines"])
    md = d.to_markdown()
    assert "The empirical evidence is strong." in md
    assert "**Key points:**" in md
    assert "- replicates prior" in md


def test_meta_review_renders_recommendation():
    m = MetaReviewOutput(
        draft_recommendation="minor",
        synthesis="Mostly aligned panel.",
        decisive_factors="Methods are sound; presentation needs polish.",
    )
    md = m.to_markdown()
    assert "Draft recommendation:** minor" in md
    assert "## Synthesis" in md and "## Decisive Factors" in md


def test_meta_review_rejects_unknown_verdict():
    with pytest.raises(ValidationError):
        MetaReviewOutput(
            draft_recommendation="maybe",  # type: ignore[arg-type]
            synthesis="x",
            decisive_factors="y",
        )


def test_author_rebuttal_renders_empty_sections():
    r = AuthorRebuttalOutput()
    md = r.to_markdown()
    assert "## Concessions" in md and "(none)" in md
    assert "## Disagreements" in md
    assert "## Load-bearing critiques" in md
    assert "considers all critiques addressable" in md


def test_author_rebuttal_renders_populated():
    r = AuthorRebuttalOutput(
        concessions=[RebuttalConcession(
            reviewer="methodology", critique="missing power analysis",
            proposed_change="add Section 3.2 with power calculation",
        )],
        disagreements=[RebuttalDisagreement(
            reviewer="novelty", critique="duplicates Smith 2024",
            response="our method differs in three ways",
            quoted_section="Section 2.1, paragraph 3",
        )],
        load_bearing_critiques=["scope of generalization"],
    )
    md = r.to_markdown()
    assert "**methodology**" in md and "power analysis" in md
    assert "**novelty**" in md and "Section 2.1" in md
    assert "- scope of generalization" in md


def test_editor_decision_renders_numbered_revisions():
    e = EditorDecisionOutput(
        decision="major",
        summary_of_evaluation="Strong method, weak generalization.",
        required_revisions=["Narrow the claim.", "Add a second cluster."],
        minor_suggestions=["Tighten abstract."],
    )
    md = e.to_markdown()
    assert "# Decision Letter" in md
    assert "**Decision:** major" in md
    assert "1. Narrow the claim." in md
    assert "2. Add a second cluster." in md
    assert "- Tighten abstract." in md


# ---------- structured-output helper paths ----------------------------------


class _StubLLM:
    """LLM stub that returns scripted parsed instances via with_structured_output."""

    def __init__(self, scripted: list):
        self._scripted = list(scripted)

    def with_structured_output(self, _schema, **_kwargs):
        return _Chain(self._scripted)


class _Chain:
    def __init__(self, scripted):
        self._scripted = scripted

    def invoke(self, _messages, **_kwargs):
        return self._scripted.pop(0)


def _ok(instance):
    return {"raw": AIMessage(content=""), "parsed": instance, "parsing_error": None}


def _fail(error: str):
    return {"raw": AIMessage(content="bad"), "parsed": None, "parsing_error": error}


def _cfg(provider: str = "openrouter") -> dict:
    return {"provider": provider, "reasoning_model": "stub"}


def test_invoke_structured_first_try():
    inst = ReviewerOutput(score=3, confidence=3, summary="ok")
    llm = _StubLLM([_ok(inst)])
    result = invoke_structured(llm, ReviewerOutput, _cfg(), "sys", "user")
    assert isinstance(result, StructuredResult)
    assert result.instance is inst


def test_invoke_structured_retry_then_success():
    inst = ReviewerOutput(score=3, confidence=3, summary="ok")
    llm = _StubLLM([_fail("bad json"), _ok(inst)])
    result = invoke_structured(llm, ReviewerOutput, _cfg(), "sys", "user")
    assert result.instance is inst


def test_invoke_structured_double_failure_raises():
    llm = _StubLLM([_fail("bad json"), _fail("still bad")])
    with pytest.raises(ValueError, match="validation failed after retry"):
        invoke_structured(llm, ReviewerOutput, _cfg(), "sys", "user")


# ---------- transient provider-error retries --------------------------------


class _FlakyChain:
    """Chain that raises ``fails`` times, then returns ``result``."""

    def __init__(self, fails: int, result):
        self.remaining = fails
        self.result = result
        self.calls = 0

    def invoke(self, _messages, **_kwargs):
        self.calls += 1
        if self.remaining > 0:
            self.remaining -= 1
            raise RuntimeError("Provider returned error")
        return self.result


class _FlakyLLM:
    def __init__(self, chain):
        self._chain = chain

    def with_structured_output(self, _schema, **_kwargs):
        return self._chain


def test_invoke_structured_retries_transient_provider_error(monkeypatch):
    import peerreviewagents.agents.utils.structured as s
    monkeypatch.setattr(s, "_RETRY_BACKOFF_S", 0)
    inst = ReviewerOutput(score=3, confidence=3, summary="ok")
    chain = _FlakyChain(fails=2, result=_ok(inst))  # fails twice, succeeds 3rd
    result = invoke_structured(_FlakyLLM(chain), ReviewerOutput, _cfg(), "sys", "user")
    assert result.instance is inst
    assert chain.calls == 3


def test_invoke_structured_provider_error_exhausts_attempts(monkeypatch):
    import peerreviewagents.agents.utils.structured as s
    monkeypatch.setattr(s, "_RETRY_BACKOFF_S", 0)
    chain = _FlakyChain(fails=99, result=_ok(None))  # always fails
    with pytest.raises(RuntimeError, match="Provider returned error"):
        invoke_structured(_FlakyLLM(chain), ReviewerOutput, _cfg(), "sys", "user")
    assert chain.calls == s._MAX_PROVIDER_ATTEMPTS  # capped at 3 tries
