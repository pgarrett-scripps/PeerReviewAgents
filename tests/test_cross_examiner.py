"""Cross-examiner: findings that need more than one report to see.

The stage exists because the specialists never read each other, so an argument
split across three reports never gets assembled. Its whole value is that a
finding is *joined*, which is why the tests below are mostly about refusing to
report things that are not.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from peerreviewagents.agents.schemas import CrossExamOutput, CrossFinding
from peerreviewagents.agents.synthesis import cross_examiner


def _finding(**over):
    base = dict(
        finding="The claim needs both limitations to hold at once.",
        drawn_from=["methodology", "data_analysis"],
        evidence=["one dataset stands in for the general case",
                  "the preprocessing is not specified"],
        adds="Either alone is survivable; together they are not.",
        severity="HARD",
    )
    base.update(over)
    return CrossFinding(**base)


# ---------- the schema is what stops it becoming a ninth reviewer -----------


def test_a_finding_must_name_two_reports():
    with pytest.raises(ValidationError):
        _finding(drawn_from=["methodology"])


def test_a_finding_must_quote_each_report():
    with pytest.raises(ValidationError):
        _finding(evidence=["only one quote"])


def test_empty_findings_must_say_what_was_looked_for():
    """Silence is a legitimate answer; unexplained silence is indistinguishable
    from a stage that failed."""
    with pytest.raises(ValidationError):
        CrossExamOutput(findings=[])
    ok = CrossExamOutput(
        findings=[],
        nothing_found_reason="No two reports bore on the same claim.",
    )
    assert "No finding required more than one report" in ok.to_markdown()


def test_markdown_shows_the_provenance_of_each_finding():
    md = CrossExamOutput(findings=[_finding()]).to_markdown()
    assert "[HARD]" in md
    assert "**Drawn from:** methodology, data_analysis" in md
    assert "> one dataset stands in for the general case" in md
    assert "**What this adds:**" in md


# ---------- node behaviour --------------------------------------------------


def _state(reports):
    return {
        "config": {"run_id": "t"},
        "reports": reports,
        "manuscript_text": "text",
        "manuscript_title": "A paper",
    }


def _report(name):
    return {"reviewer": name, "score": 3, "confidence": 4, "body": f"# {name}\nbody"}


def test_fewer_than_two_reports_does_not_call_a_model(monkeypatch):
    """Nothing to cross-examine, so the call would be spent asking a model to
    find connections inside a single document."""
    def _boom(*a, **k):
        raise AssertionError("should not build a model")

    monkeypatch.setattr(cross_examiner, "make_llm", _boom)
    assert cross_examiner._run(_state([_report("rigor")])) == {"cross_exam": ""}


def test_a_failure_is_recorded_but_never_fatal(monkeypatch):
    """A run without this stage is the run the pipeline produced before it
    existed, so it must not be able to take a review down."""
    def _boom(*a, **k):
        raise RuntimeError("provider is down")

    monkeypatch.setattr(cross_examiner, "make_llm", _boom)
    out = cross_examiner._run(_state([_report("rigor"), _report("methodology")]))
    assert out["cross_exam"] == ""
    assert out["errors"] and "provider is down" in out["errors"][0]
