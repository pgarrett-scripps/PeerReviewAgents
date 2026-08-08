"""Gap finder: what the three technical reviewers missed.

The point of the stage is to surface a weakness that fell between
data_analysis, methodology and rigor — which means, unlike every other stage
that reads reports, it must be able to report something that appears in NO
report. Grounding therefore points at the manuscript, not at the panel.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from peerreviewagents.agents.schemas import GapFinding, PanelGapOutput
from peerreviewagents.agents.synthesis import gap_finder


def _gap(**over):
    base = dict(
        finding="Nobody checked whether the split preceded feature selection.",
        belongs_to="data_analysis",
        manuscript_evidence="Section 3.2, 'we select the top 200 features and then split'",
        kind="gap",
        why_it_matters="Selection before splitting leaks the test set.",
        severity="HARD",
    )
    base.update(over)
    return GapFinding(**base)


# ---------- a gap needs no report behind it ---------------------------------


def test_a_gap_is_reportable_with_no_supporting_report():
    """The whole reason this stage exists. A finding no reviewer made must
    survive validation, or the stage cannot do its job."""
    out = PanelGapOutput(findings=[_gap()])
    assert out.findings[0].drawn_from == []
    assert "[GAP]" in out.to_markdown()


def test_a_gap_must_point_at_the_manuscript():
    """The only thing standing between this stage and invention."""
    with pytest.raises(ValidationError):
        _gap(manuscript_evidence="Fig. 1")  # too short to locate anything


def test_a_gap_must_name_the_lane_that_should_have_caught_it():
    with pytest.raises(ValidationError):
        _gap(belongs_to="clarity")  # not one of the three technical lanes


# ---------- a joined finding still has to say what it joined ----------------


def test_joined_findings_need_two_sources():
    with pytest.raises(ValidationError):
        _gap(kind="joined", drawn_from=["rigor"])
    ok = _gap(kind="joined", drawn_from=["rigor", "methodology"])
    assert "JOINED from rigor, methodology" in PanelGapOutput(findings=[ok]).to_markdown()


def test_empty_findings_must_say_what_was_checked():
    with pytest.raises(ValidationError):
        PanelGapOutput(findings=[])
    ok = PanelGapOutput(findings=[], nothing_found_reason="The three covered every claim.")
    assert "left no gap worth reporting" in ok.to_markdown()


# ---------- node behaviour --------------------------------------------------


def _state(names):
    return {
        "config": {"run_id": "t"},
        "reports": [
            {"reviewer": n, "score": 3, "confidence": 4, "body": f"# {n}\nbody"}
            for n in names
        ],
        "manuscript_text": "text",
        "manuscript_title": "A paper",
    }


def test_only_the_three_technical_reports_are_audited():
    digest = gap_finder._technical_digest(
        _state(["clarity", "data_analysis", "ethics", "rigor"])
    )
    assert "data_analysis reviewer" in digest and "rigor reviewer" in digest
    assert "clarity" not in digest and "ethics" not in digest


def test_no_technical_report_means_no_call(monkeypatch):
    """Nothing to audit. Asking anyway would produce a review of the
    manuscript from an agent that is not a reviewer."""
    def _boom(*a, **k):
        raise AssertionError("should not build a model")

    monkeypatch.setattr(gap_finder, "make_llm", _boom)
    assert gap_finder._run(_state(["clarity", "ethics"])) == {"panel_gaps": ""}


def test_a_failure_is_recorded_but_never_fatal(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("provider is down")

    monkeypatch.setattr(gap_finder, "make_llm", _boom)
    out = gap_finder._run(_state(["data_analysis", "rigor"]))
    assert out["panel_gaps"] == ""
    assert out["errors"] and "provider is down" in out["errors"][0]
