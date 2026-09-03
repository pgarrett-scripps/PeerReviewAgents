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

import pytest

from peerreviewagents.agents.editor import editor_in_chief
from peerreviewagents.agents.schemas import EditorDecisionOutput
from peerreviewagents.agents.synthesis import debate_synthesizer
from peerreviewagents.agents.utils.agent_utils import RunResult
from peerreviewagents.agents.utils.structured import MarkdownResult
from peerreviewagents.reports import write_reports

_SCORE_BLOCK = """
**Publication readiness:** 78/100

## Readiness Breakdown
- Scientific validity: 28/35
- Methods and evidence: 20/25
- Reproducibility and reporting: 15/20
- Clarity and completeness: 15/20

## Contribution Profile
- Novelty: moderate
- Significance: moderate
- Usefulness: high

## Score and Decision
The score reflects a sound foundation with a material unresolved issue. The
decision follows the work required to resolve that issue, not a score range.
"""


def _editor_score_fields() -> dict:
    return {
        "readiness_score": 78,
        "readiness_breakdown": {
            "scientific_validity": 28,
            "methods_and_evidence": 20,
            "reproducibility_and_reporting": 15,
            "clarity_and_completeness": 15,
        },
        "contribution_profile": {
            "novelty": "moderate",
            "significance": "moderate",
            "usefulness": "high",
        },
        "score_decision_rationale": (
            "The score reflects a sound foundation with a material unresolved "
            "issue. The decision follows the required work, not a score range."
        ),
    }


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


# --- debate-synthesizer failure ----------------------------------------------


def test_synthesizer_failure_emits_marker_not_content(monkeypatch):
    monkeypatch.setattr(debate_synthesizer, "make_llm", lambda config, **_k: object())

    def _boom(*_a, **_k):
        raise RuntimeError("rate limited")

    monkeypatch.setattr(debate_synthesizer, "invoke_markdown", _boom)

    out = debate_synthesizer.node(_panel_state())
    assert "did not run" in out["debate_synthesis"]
    assert "rate limited" in out["debate_synthesis"]
    assert any("debate_synthesizer failed" in e for e in out["errors"])


def test_editor_prompt_contains_primary_reports():
    user = editor_in_chief._first_round_user(_panel_state())
    assert "Specialist reports (primary panel evidence)" in user
    assert "# Methodology" in user
    assert "no debate was run" in user


def test_editor_receives_synthesis_not_the_raw_transcript():
    state = _panel_state(
        debate=[{"role": "advocate", "round": 1, "content": "RAW TURN TEXT."}],
        debate_synthesis="# Debate Synthesis\n\nThe theorem objection stands unresolved.",
    )
    user = editor_in_chief._first_round_user(state)
    assert "Debate synthesis" in user
    assert "theorem objection stands unresolved" in user
    assert "RAW TURN TEXT" not in user
    assert "no panel average is used" in user


def test_editor_falls_back_to_the_transcript_when_synthesis_failed():
    state = _panel_state(
        debate=[{"role": "advocate", "round": 1, "content": "RAW TURN TEXT."}],
        debate_synthesis="(the debate synthesizer did not run: rate limited)",
    )
    user = editor_in_chief._first_round_user(state)
    assert "raw transcript" in user
    assert "RAW TURN TEXT" in user


def test_editor_forbids_turning_ingest_limitations_into_author_revisions():
    user = editor_in_chief._first_round_user(_panel_state())
    assert "INGEST LIMITATION" in user
    assert "not an author-facing criticism" in user


def test_synthesizer_reads_the_panel_and_transcript_under_the_word_budget(monkeypatch):
    seen = {}
    state = _panel_state(
        debate=[
            {"role": "skeptic", "round": 1, "content": "The proof objection."},
            {"role": "advocate", "round": 1, "content": "The theorem holds."},
        ],
    )
    monkeypatch.setattr(
        debate_synthesizer, "make_llm", lambda *_a, **_k: object(),
    )

    def capture(_llm, _config, _system, user, **_kwargs):
        seen["user"] = user
        return MarkdownResult("## Issue 1\n\nUnresolved.", 0.0)

    monkeypatch.setattr(debate_synthesizer, "invoke_markdown", capture)
    out = debate_synthesizer.node(state)

    assert "# Methodology" in seen["user"]
    assert "The theorem holds." in seen["user"]
    assert "The proof objection." in seen["user"]
    assert "1200 words or fewer" in seen["user"]
    assert "Unresolved." in out["debate_synthesis"]


def test_minor_verdict_cannot_require_new_experiments_or_proofs():
    issue = editor_in_chief._decision_semantic_issue(
        "minor",
        ["Report mean and standard deviation over at least 20 independent seeds."],
    )
    assert "called the decision minor" in issue
    assert editor_in_chief._decision_semantic_issue(
        "minor", ["Clarify the notation in Theorem 3."],
    ) == ""
    assert editor_in_chief._decision_semantic_issue(
        "minor", ["Provide a detailed derivation of Equation 24."],
    )


def test_minor_reporting_fix_that_mentions_a_study_is_not_major_work():
    assert editor_in_chief._decision_semantic_issue(
        "minor",
        ["Add an explicit ethics statement for the human study."],
    ) == ""
    assert editor_in_chief._decision_semantic_issue(
        "minor",
        ["Add an additional experiment comparing the two baselines."],
    )


def test_editor_recovers_explicit_verdict_after_provider_boolean_prefix():
    letter = (
        "false VERDICT: minor\n\n## Summary of Evaluation\n\n"
        "The evidence supports the central claims after reporting corrections.\n\n"
        "## Required Revisions\n\n1. Clarify the evaluation protocol.\n\n"
        + _SCORE_BLOCK
    )
    parsed = editor_in_chief._editor_from_markdown(letter)
    assert parsed is not None
    assert parsed[0] == "minor"
    assert parsed[1] == ["Clarify the evaluation protocol."]
    assert parsed[5] == 78
    assert parsed[6]["scientific_validity"] == 28
    assert parsed[7]["usefulness"] == "high"


def test_editor_rejects_missing_or_inconsistent_readiness_data():
    body = (
        "VERDICT: accept\n\n## Summary of Evaluation\n\n"
        "The manuscript is publishable as written and has no blocking issues.\n\n"
    )
    assert editor_in_chief._editor_from_markdown(body) is None
    inconsistent = body + _SCORE_BLOCK.replace("78/100", "79/100")
    assert editor_in_chief._editor_from_markdown(inconsistent) is None


# --- editor refuses to repair a non-verdict ----------------------------------


def test_editor_does_not_adopt_the_draft_for_a_nonverdict(monkeypatch):
    """A malformed editor output must surface as no decision — not silently
    become whatever the draft recommendation happened to be, which can itself
    have come from a failure path."""
    monkeypatch.setattr(editor_in_chief, "make_llm", lambda config, **_k: object())
    prose = (
        "The panel identified several concerns, and the manuscript should be "
        "changed before publication. This letter deliberately refuses to say "
        "whether that means accept, minor, major, or reject, even after being "
        "asked a second time. It is long enough to be substantive but contains "
        "no decision that the pipeline may safely invent from another agent."
    )
    monkeypatch.setattr(
        editor_in_chief,
        "run_agent",
        lambda *_a, **_k: RunResult(text=prose, cost=0.0),
    )

    out = editor_in_chief.node(_panel_state())
    assert out["decision"] == ""
    assert any("editor failed" in e for e in out["errors"])
    # The letter body is real editor prose and stays on the record.
    assert prose in out["decision_letter"]


def test_editor_exception_still_means_no_decision(monkeypatch):
    monkeypatch.setattr(editor_in_chief, "make_llm", lambda config, **_k: object())

    def _boom(*_a, **_k):
        raise RuntimeError("overloaded")

    monkeypatch.setattr(editor_in_chief, "run_agent", _boom)
    out = editor_in_chief.node(_panel_state())
    assert out["decision"] == ""


def test_editor_accepts_markdown_without_a_structured_response(monkeypatch):
    letter = """\
I recommend major revision because the central performance claim lacks its
necessary control, although the software and workflow are otherwise sound.

## Summary of Evaluation
The panel converges on one load-bearing evidentiary gap. The missing control
prevents the comparative result from supporting the headline claim, while the
remaining comments concern reporting and presentation.

## Required Revisions
1. Run the existing benchmark with the necessary control and report the same metrics.
2. Requalify the headline claim if that comparison does not support it.

## Minor Suggestions
- State the software versions and random seed used for the example.
""" + _SCORE_BLOCK
    monkeypatch.setattr(editor_in_chief, "make_llm", lambda config, **_k: object())
    monkeypatch.setattr(
        editor_in_chief,
        "run_agent",
        lambda *_a, **_k: RunResult(text=letter, cost=0.25),
    )

    out = editor_in_chief.node(_panel_state())

    assert out["decision"] == "major"
    assert len(out["required_revisions"]) == 2
    assert "I recommend major revision" in out["decision_letter"]
    assert out["total_cost"] == pytest.approx(0.25)


def test_editor_preserves_verdict_when_revision_list_format_is_unparseable(monkeypatch):
    letter = (
        "VERDICT: minor\n\nThe evidence supports the claims, but the authors "
        "should state the dependency versions and clarify the caption before "
        "publication. Those are text-only corrections and do not require new "
        "analysis. This deliberately ordinary paragraph has no special section "
        "headings or numbered list, and that formatting choice must not erase "
        "the editor's actual decision.\n\n"
        + _SCORE_BLOCK
    )
    monkeypatch.setattr(editor_in_chief, "make_llm", lambda config, **_k: object())
    monkeypatch.setattr(
        editor_in_chief,
        "run_agent",
        lambda *_a, **_k: RunResult(text=letter, cost=0.0),
    )
    out = editor_in_chief.node(_panel_state())

    assert out["decision"] == "minor"
    assert out["required_revisions"] == []
    assert letter.strip() in out["decision_letter"]
    assert any("editor degraded" in error for error in out["errors"])


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
        "debate_synthesis": "# Debate synthesis",
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
    # Completed synthesis prose is real work and survives the crash, like
    # the reviewer reports; only the verdict may not be fabricated.
    assert (run_dir / "debate_synthesis.md").is_file()
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
            **_editor_score_fields(),
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
            **_editor_score_fields(),
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
                **_editor_score_fields(),
                summary_of_evaluation=junk,
                required_revisions=["Do the thing."],
            )


def test_editor_schema_accepts_a_real_synthesis():
    out = EditorDecisionOutput(
        decision="major",
        **_editor_score_fields(),
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


def test_editor_score_does_not_control_the_decision():
    fields = _editor_score_fields()
    fields["readiness_score"] = 90
    fields["readiness_breakdown"] = {
        "scientific_validity": 33,
        "methods_and_evidence": 23,
        "reproducibility_and_reporting": 17,
        "clarity_and_completeness": 17,
    }
    fields["contribution_profile"] = {
        "novelty": "low",
        "significance": "moderate",
        "usefulness": "high",
    }
    summary = (
        "The evidence is valid, complete, and clearly reported. The work is "
        "incremental but useful to its intended community, and no revision is "
        "needed before publication. The recommendation therefore follows the "
        "absence of blocking work rather than novelty or a numerical cutoff."
    )
    accepted = EditorDecisionOutput(
        decision="accept",
        summary_of_evaluation=summary,
        **fields,
    )
    major = EditorDecisionOutput(
        decision="major",
        summary_of_evaluation=(
            "The manuscript is otherwise strong and nearly complete, but one "
            "central claim still depends on an outcome-changing analysis. That "
            "single blocking task makes the recommendation major even though "
            "the broader publication-readiness assessment remains high."
        ),
        required_revisions=["Run the missing outcome-changing analysis."],
        **fields,
    )
    assert accepted.decision == "accept"
    assert accepted.contribution_profile.novelty == "low"
    assert major.decision == "major"
    assert major.readiness_score == 90


def test_editor_score_must_equal_its_components():
    fields = _editor_score_fields()
    fields["readiness_score"] = 79
    with pytest.raises(ValueError, match="must equal the sum"):
        EditorDecisionOutput(
            decision="accept",
            summary_of_evaluation=(
                "The manuscript is scientifically sound, complete, and ready "
                "for publication. The panel found no blocking concerns, and "
                "the editor therefore recommends acceptance as it stands."
            ),
            **fields,
        )


def test_editor_schema_rejects_a_summary_that_is_a_whole_review():
    """One letter published a 37,275-character summary that had swallowed a
    reviewer report and its numbered questions, while required_revisions held
    its own copy of the asks. Field confusion, not verbosity."""
    with pytest.raises(ValueError, match="review rather than a synthesis"):
        EditorDecisionOutput(
            decision="major",
            **_editor_score_fields(),
            summary_of_evaluation="The panel found problems. " * 2000,
            required_revisions=["Do the thing."],
        )


def test_editor_prose_path_rejects_a_letter_that_is_a_transcript(monkeypatch):
    """The prose path preserves what the editor writes, which is right until
    the editor stops writing a letter: one run emitted 55,670 characters
    carrying the panel's own headings back verbatim. Over the cap it must ask
    again rather than publish the transcript."""
    dump = "# Decision Letter\n\n**Decision:** major\n\n## Merged Review\n" + ("x " * 30000)
    good = (
        "# Decision Letter\n\n**Decision:** major\n\n"
        "## Summary of Evaluation\n\nThe panel's reports converge on an "
        "unsupported central claim, and the debate did not resolve the "
        "statistical objection. Fixing it needs a reanalysis whose outcome "
        "could change a conclusion, so the verdict is major.\n\n"
        "## Required Revisions\n\n1. Rerun the analysis with the correction.\n"
        + _SCORE_BLOCK
    )
    answers = iter([dump, good])
    monkeypatch.setattr(editor_in_chief, "make_llm", lambda config, **_k: object())
    monkeypatch.setattr(
        editor_in_chief, "run_agent",
        lambda *_a, **_k: RunResult(text=next(answers), cost=0.0),
    )

    out = editor_in_chief.node(_panel_state())
    assert out["decision"] == "major"
    assert "Merged Review" not in out["decision_letter"]
    assert out["required_revisions"] == ["Rerun the analysis with the correction."]


def test_editor_retry_does_not_echo_a_contaminated_attempt(monkeypatch):
    """A sub-cap transcript must be discarded, not quoted into the retry."""
    contaminated = (
        "Accept\n\nA purported letter.\n\n"
        "=== Summary of reviewer scores ===\n" + ("panel material " * 300)
    )
    good = (
        "# Decision Letter\n\nVERDICT: major\n\n"
        "## Summary of Evaluation\n\nThe evidence does not yet support the "
        "central claim, and the missing control could change the conclusion.\n\n"
        "## Required Revisions\n\n1. Add the missing control.\n"
        + _SCORE_BLOCK
    )
    prompts = []
    answers = iter([contaminated, good])

    def fake_run_agent(_llm, _system, user, *_args, **_kwargs):
        prompts.append(user)
        return RunResult(text=next(answers), cost=0.0)

    monkeypatch.setattr(editor_in_chief, "make_llm", lambda config, **_k: object())
    monkeypatch.setattr(editor_in_chief, "run_agent", fake_run_agent)

    out = editor_in_chief.node(_panel_state())
    assert out["decision"] == "major"
    assert contaminated not in prompts[1]
    assert "Summary of reviewer scores" not in out["decision_letter"]
