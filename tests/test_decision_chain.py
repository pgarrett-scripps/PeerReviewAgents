"""Failure honesty in the decision chain, and salvage of incomplete runs.

Two fabrication paths existed: a meta-reviewer that died returned a hardcoded
"major" as if the Area Chair had recommended it, and the editor's
malformed-output fallback adopted that value as the FINAL decision — a verdict
no model ever rendered. Separately, a run that crashed after most of the panel
finished wrote nothing at all: hours of completed reviews discarded because
the decision at the end was missing.
"""

from __future__ import annotations

import datetime as _dt
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from peerreviewagents.agents.editor import editor_in_chief
from peerreviewagents.agents.schemas import EditorDecisionOutput
from peerreviewagents.agents.synthesis import meta_reviewer
from peerreviewagents.reports import write_reports


def _panel_state(**extra):
    state = {
        "config": {"run_id": ""},
        "manuscript_md": "# Test manuscript\n\nA complete synthetic manuscript.",
        "sections": {},
        "reports": [
            {
                "reviewer": "methodology",
                "score": 3.0,
                "confidence": 4.0,
                "body": "# Methodology\n\nFine.",
            }
        ],
        "debate": [],
    }
    state.update(extra)
    return state


# --- meta-reviewer failure ---------------------------------------------------


def test_meta_failure_emits_no_recommendation(monkeypatch):
    monkeypatch.setattr(meta_reviewer, "make_llm", lambda config, **_k: object())

    def _boom(*_a, **_k):
        raise RuntimeError("rate limited")

    monkeypatch.setattr(meta_reviewer, "invoke_structured", _boom)

    out = meta_reviewer.node(_panel_state())
    assert out["draft_recommendation"] == ""
    assert "did not run" in out["meta_review"]
    assert "rate limited" in out["meta_review"]
    assert any("meta_reviewer failed" in e for e in out["errors"])


def test_editor_prompt_contains_primary_reports_without_meta_recommendation():
    state = _panel_state(draft_recommendation="", meta_review="(the meta-reviewer did not run: x)")
    user = editor_in_chief._first_round_user(state)
    assert "Specialist reports (primary panel evidence)" in user
    assert "# Methodology" in user
    assert "meta-reviewer" not in user


# --- editor refuses to repair a non-verdict ----------------------------------


def test_editor_does_not_adopt_the_draft_for_a_nonverdict(monkeypatch):
    """A malformed editor output must surface as no decision — not silently
    become whatever the draft recommendation happened to be, which can itself
    have come from a failure path."""
    bad = EditorDecisionOutput.model_construct(
        decision="revise-ish",
        summary_of_evaluation="confused",
        required_revisions=[],
        minor_suggestions=[],
    )
    monkeypatch.setattr(editor_in_chief, "make_llm", lambda config, **_k: object())
    monkeypatch.setattr(
        editor_in_chief,
        "invoke_structured",
        lambda *_a, **_k: SimpleNamespace(instance=bad, cost=0.0),
    )

    out = editor_in_chief.node(_panel_state(draft_recommendation="major"))
    assert out["decision"] == ""
    assert any("editor failed" in e for e in out["errors"])
    # The letter body is real editor prose and stays on the record.
    assert "confused" in out["decision_letter"]


def test_editor_exception_still_means_no_decision(monkeypatch):
    monkeypatch.setattr(editor_in_chief, "make_llm", lambda config, **_k: object())

    def _boom(*_a, **_k):
        raise RuntimeError("overloaded")

    monkeypatch.setattr(editor_in_chief, "invoke_structured", _boom)
    out = editor_in_chief.node(_panel_state(draft_recommendation="major"))
    assert out["decision"] == ""


# --- salvage of incomplete runs ----------------------------------------------


def _crashed_state(tmp_path):
    return {
        "config": {"output_dir": str(tmp_path / "reports"), "run_id": ""},
        "manuscript_title": "Crashed Run",
        "reports": [
            {
                "reviewer": "methodology",
                "score": 3.0,
                "confidence": 4.0,
                "body": "# Methodology\n\nFine.",
            }
        ],
        "meta_review": "# Meta review",
    }


def test_web_runner_salvages_partial_reports_on_pipeline_error(tmp_path):
    from peerreviewagents.web.jobs import JobState
    from peerreviewagents.web.runner import JobRunner

    class _Bus:
        def put_threadsafe(self, _event):
            pass

        def close(self):
            pass

    job = JobState(id="j1", manuscript_path="x", manuscript_filename="x.md")
    job.errors.append("pipeline crashed: boom")
    job.accumulated = _crashed_state(tmp_path)
    JobRunner(job, {}, _Bus())._finalize()

    assert job.status == "error"
    assert job.decision is None
    assert job.report_dir, "completed reviewer work should survive the crash"
    run_dir = Path(job.report_dir)
    assert (run_dir / "review_methodology.md").is_file()
    assert not (run_dir / "meta_review.md").exists()
    summary = (run_dir / "summary.md").read_text(encoding="utf-8")
    assert "FAILED" in summary
    assert "boom" in summary
    assert "**Decision:**" not in summary  # no fabricated verdict
    assert not (run_dir / "decision_letter.md").exists()


def test_web_runner_salvage_skips_runs_with_nothing_to_keep(tmp_path):
    from peerreviewagents.web.runner import salvage_partial_reports

    assert salvage_partial_reports({}, ["boom"]) is None
    assert (
        salvage_partial_reports(
            {"config": {"output_dir": str(tmp_path)}, "reports": []}, ["boom"]
        )
        is None
    )


def test_headless_cli_salvages_partial_reports(tmp_path, monkeypatch):
    from peerreviewagents.cli import main as cli_main

    out_dir = tmp_path / "reports"
    partial = _crashed_state(tmp_path)

    class _FakeGraph:
        def __init__(self, config):
            pass

        def stream(self, _manuscript):
            yield "reviewer_methodology", partial
            raise RuntimeError("boom")

    monkeypatch.setattr(cli_main, "PeerReviewGraph", _FakeGraph)
    with pytest.raises(SystemExit) as exc:
        cli_main.run_headless("paper.md", {"output_dir": str(out_dir)})
    assert exc.value.code == 2

    runs = list(out_dir.iterdir())
    assert len(runs) == 1
    summary = (runs[0] / "summary.md").read_text(encoding="utf-8")
    assert "FAILED" in summary
    assert "boom" in summary
    assert (runs[0] / "review_methodology.md").is_file()


# --- run-dir collisions --------------------------------------------------------


def test_same_second_same_title_runs_get_distinct_dirs(tmp_path, monkeypatch):
    from peerreviewagents import reports as reports_mod
    from peerreviewagents import rounds

    real_datetime = _dt.datetime

    class _Frozen:
        @staticmethod
        def now():
            return real_datetime(2026, 1, 2, 3, 4, 5)

    monkeypatch.setattr(reports_mod._dt, "datetime", _Frozen)

    out_dir = tmp_path / "reports"
    state = {
        "config": {"output_dir": str(out_dir), "run_id": ""},
        "manuscript_title": "Same Title",
        "decision": "accept",
        "reports": [
            {"reviewer": "methodology", "score": 4.0, "confidence": 4.0, "body": "# R"}
        ],
    }
    first = write_reports(dict(state))
    second = write_reports(dict(state))
    third = write_reports(dict(state))

    assert first != second != third
    assert second == f"{first}-2"
    assert third == f"{first}-3"
    for d in (first, second, third):
        assert os.path.isfile(os.path.join(d, "summary.md"))
        # The suffixed name still resolves as a job id for revision rounds.
        assert rounds.resolve_run_dir(
            os.path.basename(d), {"output_dir": str(out_dir)}
        ) == d


# --- a revision verdict must carry its demands --------------------------------


def test_editor_schema_rejects_a_major_with_no_required_revisions():
    """Observed on a V4 Flash batch: four of ten majors folded every demand
    into summary prose and left required_revisions empty. The letter published
    as a stub and round.json carried no asks for a later compliance audit.
    The schema now rejects it so the structured-output layer asks again."""
    with pytest.raises(ValueError, match="required_revisions is empty"):
        EditorDecisionOutput(
            decision="major",
            summary_of_evaluation=(
                "The panel found the analysis sound but the headline claim "
                "unsupported by the reported statistics, and the debate did "
                "not resolve it. Every demand is described here in prose "
                "rather than listed, which is the defect under test."
            ),
            required_revisions=[],
        )


def test_editor_schema_allows_accept_and_reject_without_revisions():
    for verdict in ("accept", "reject"):
        out = EditorDecisionOutput(
            decision=verdict,
            summary_of_evaluation=(
                "The panel is unanimous and the debate surfaced nothing "
                "unresolved, so the verdict follows directly from the "
                "reports and needs no revision list to explain it. Settled "
                "either way, with no outstanding asks for the authors."
            ),
        )
        assert out.required_revisions == []


def test_editor_prompt_defines_the_minor_major_boundary():
    """The boundary was deliberately unwritten for a while; the model's prior
    drifted to counting items (a 20-item letter of caption fixes and missing
    statements decided 'major' while itself noting no new experiments were
    needed). The line is now stated: the verdict tracks what the revision
    requires, not how many items the letter lists."""
    sys = editor_in_chief._SYS
    assert "what the revision REQUIRES" in sys
    assert "new experiments, new data, or a reanalysis" in sys
    assert "however numerous" in sys


def test_editor_schema_rejects_a_placeholder_summary():
    """Four letters published "..." as their entire Summary of Evaluation and
    a fifth "overwritten from prior — write visible text". Each carried a real
    verdict and revision list, so nothing else in the run looked wrong."""
    for junk in ("...", "-", "overwritten from prior — write visible text."):
        with pytest.raises(ValueError, match="says nothing"):
            EditorDecisionOutput(
                decision="major",
                summary_of_evaluation=junk,
                required_revisions=["Do the thing."],
            )


def test_editor_schema_accepts_a_real_synthesis():
    out = EditorDecisionOutput(
        decision="major",
        summary_of_evaluation=(
            "The panel converges on a technically sound dataset whose headline "
            "mechanistic claim outruns the evidence. Two reviewers flagged the "
            "absent multiple-comparisons correction as load-bearing, and the "
            "skeptic's reading of the confound is decisive. Because fixing it "
            "requires a reanalysis whose outcome could change a conclusion, the "
            "verdict is major rather than minor."
        ),
        required_revisions=["Apply an FDR correction and report what survives."],
    )
    assert out.decision == "major"
