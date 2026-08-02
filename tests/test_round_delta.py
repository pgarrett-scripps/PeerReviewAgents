"""Tests for the round-over-round delta and the editor's revision path.

Two properties matter here. First, the delta must be *derived* — it is the
only thing standing between the editor and a verdict argued from the tone of a
response letter. Second, it must never be the reason a run dies: its inputs
come from sibling agents that are each allowed to produce less than the full
picture, so a missing field costs a line, not the round.
"""

from __future__ import annotations

import pytest
from test_pipeline import _CANNED, _patch_llms

from peerreviewagents import rounds
from peerreviewagents.agents.editor import editor_in_chief
from peerreviewagents.agents.schemas import (
    ComplianceFinding,
    EditorDecisionOutput,
    NewIssue,
    PriorPointVerdict,
    RevisionComplianceOutput,
    RevisionReviewerOutput,
)
from peerreviewagents.agents.utils.round_delta import round_delta
from peerreviewagents.default_config import get_config


def _reports(methodology: float = 3, rigor: float = 2, **extra):
    reports = [
        {
            "reviewer": "methodology",
            "score": methodology,
            "confidence": 4,
            "weaknesses": ["Only a single production cluster is used."],
            "questions": [],
            "body": "",
        },
        {
            "reviewer": "rigor",
            "score": rigor,
            "confidence": 3,
            "weaknesses": ["No random seed is reported for training."],
            "questions": [],
            "body": "",
        },
    ]
    for report in reports:
        report.update(extra)
    return reports


def _prior(**over):
    """A finished round-1 record, built the way the pipeline builds it."""
    base = {
        "manuscript_title": "A Lightweight Method",
        "config": {},
        "decision": "major",
        "required_revisions": [
            "Report per-cluster results rather than the pooled mean.",
            "State the random seed used for training.",
        ],
        "minor_suggestions": ["Define WidgetNet on first use."],
        "reports": _reports(),
    }
    base.update(over)
    return rounds.build_from_state(base, job_id="20260801-round1")


def _state(prior=None, reports=None, audits=None, **config):
    return {
        "config": get_config(**config),
        "prior_round": prior,
        "reports": reports if reports is not None else _reports(),
        "audits": audits or [],
    }


def _compliance_audit(output: RevisionComplianceOutput, *, promote: bool = False) -> dict:
    """An audits entry shaped like the compliance auditor's.

    ``promote`` mirrors the auditor optionally lifting structured findings onto
    the entry; without it only the rendered body is available, which is the
    contract AuditReport actually guarantees.
    """
    entry = {
        "auditor": "revision_compliance",
        "title": "Revision Compliance",
        "hard_gaps": 0,
        "soft_gaps": 0,
        "body": output.to_markdown(),
    }
    if promote:
        entry["findings"] = [f.model_dump() for f in output.findings]
    return entry


# --- first round ------------------------------------------------------------


def test_first_round_has_no_delta():
    assert round_delta(_state()) == ""


def test_first_round_delta_is_empty_even_with_audits_present():
    audits = [{"auditor": "methods_completeness", "title": "Methods", "body": "x"}]
    assert round_delta(_state(audits=audits)) == ""


# --- score movement ---------------------------------------------------------


def test_reports_score_movement_upward():
    block = round_delta(_state(prior=_prior(), reports=_reports(methodology=5, rigor=4)))
    # Round 1 weighted (3*4 + 2*3)/7 = 2.57; round 2 (5*4 + 4*3)/7 = 4.57.
    assert "2.57/5 -> 4.57/5 (+2.00)" in block
    assert "methodology 3 -> 5 (+2)" in block
    assert "rigor 2 -> 4 (+2)" in block


def test_reports_score_movement_downward():
    block = round_delta(_state(prior=_prior(), reports=_reports(methodology=1, rigor=1)))
    assert "-> 1.00/5 (-1.57)" in block
    assert "methodology 3 -> 1 (-2)" in block


def test_flat_score_is_reported_as_zero_delta():
    block = round_delta(_state(prior=_prior()))
    assert "2.57/5 -> 2.57/5 (+0.00)" in block


def test_round_and_budget_are_named():
    block = round_delta(_state(prior=_prior(), max_rounds=3))
    assert "This is round 2 of at most 3" in block
    assert "1 further revision round remains after this one" in block
    assert "The round-1 decision was 'major'." in block


def test_final_round_says_no_further_round_is_available():
    block = round_delta(_state(prior=_prior(), max_rounds=2))
    assert "no further revision round is available" in block
    assert "last one this review can make" in block


# --- compliance -------------------------------------------------------------


def _compliance_output() -> RevisionComplianceOutput:
    return RevisionComplianceOutput(
        summary="One of two items was carried out.",
        findings=[
            ComplianceFinding(
                id="R1-01",
                status="addressed",
                manuscript_evidence="Table 2 now reports per-cluster results.",
            ),
            ComplianceFinding(
                id="R1-02",
                status="not_addressed",
                manuscript_evidence="",
                blocking=True,
            ),
        ],
    )


def test_compliance_counts_from_the_rendered_body():
    state = _state(prior=_prior(), audits=[_compliance_audit(_compliance_output())])
    block = round_delta(state)
    assert "Required revisions from round 1 (2 items): 1 addressed, 1 not addressed." in block
    assert "1 still-open item is marked blocking" in block


def test_compliance_counts_from_promoted_findings():
    state = _state(
        prior=_prior(),
        audits=[_compliance_audit(_compliance_output(), promote=True)],
    )
    assert "1 addressed, 1 not addressed" in round_delta(state)


def test_rebutted_items_are_not_counted_as_blocking():
    output = RevisionComplianceOutput(
        summary="The authors declined one item and argued why.",
        findings=[
            ComplianceFinding(id="R1-01", status="rebutted", blocking=True),
        ],
    )
    block = round_delta(_state(prior=_prior(), audits=[_compliance_audit(output)]))
    assert "1 rebutted" in block
    assert "No still-open item is marked blocking." in block


def test_compliance_line_omitted_when_the_audit_did_not_run():
    """Reporting '0 addressed' for a missing audit would read as a finding."""
    block = round_delta(_state(prior=_prior()))
    assert "Required revisions from round" not in block


def test_other_auditors_are_not_mistaken_for_the_compliance_audit():
    audits = [{
        "auditor": "methods_completeness",
        "title": "Methods Completeness",
        "hard_gaps": 1,
        "soft_gaps": 0,
        "body": "- **[R1-01] addressed**",
    }]
    assert "Required revisions from round" not in round_delta(_state(prior=_prior(), audits=audits))


# --- reviewer drift ---------------------------------------------------------


def _drifting_body() -> str:
    return RevisionReviewerOutput(
        prior_score=3,
        score=3,
        confidence=4,
        prior_points=[
            PriorPointVerdict(id="methodology-1", status="resolved", evidence="Table 2."),
        ],
        new_issues=[
            NewIssue(issue="The loss function is never stated.", caused_by_the_revision=False),
            NewIssue(issue="New Figure 4 has no error bars.", caused_by_the_revision=True),
        ],
        summary="Concerns addressed.",
        score_rationale="Held flat for a newly noticed gap.",
    ).to_markdown(role="Methodology")


def test_drift_is_recovered_from_the_rendered_reviewer_body():
    reports = _reports()
    reports[0]["body"] = _drifting_body()
    block = round_delta(_state(prior=_prior(), reports=reports))
    assert "Reviewer drift: 1 new issue raised this round" in block
    assert "moves the goalposts" in block


def test_drift_line_omitted_when_no_reviewer_drifted():
    """Silence is not a claim of no drift — the signal may simply be absent."""
    assert "Reviewer drift" not in round_delta(_state(prior=_prior()))


def test_drift_prefers_promoted_structure_over_the_body():
    reports = _reports(drifted_issues=[{"issue": "a"}, {"issue": "b"}])
    reports[0]["body"] = _drifting_body()
    assert "Reviewer drift: 4 new issues" in round_delta(_state(prior=_prior(), reports=reports))


# --- degradation ------------------------------------------------------------


def test_missing_prior_score_does_not_crash():
    prior = _prior(reports=[])
    block = round_delta(_state(prior=prior))
    assert "the previous round recorded none" in block


def test_missing_current_reports_does_not_crash():
    block = round_delta(_state(prior=_prior(), reports=[]))
    assert "no reviewer scores were produced this round" in block
    # A panel that vanished entirely is itself the finding, so every prior
    # reviewer is still named rather than the line being dropped.
    assert "methodology (no report this round); rigor (no report this round)" in block


def test_new_and_departed_reviewers_are_both_named():
    reports = _reports()[:1] + [{
        "reviewer": "ethics", "score": 4, "confidence": 5, "weaknesses": [], "body": "",
    }]
    block = round_delta(_state(prior=_prior(), reports=reports))
    assert "ethics 4 (new this round)" in block
    assert "rigor (no report this round)" in block


def test_absent_max_rounds_still_renders():
    state = _state(prior=_prior())
    state["config"]["max_rounds"] = None
    assert "This is round 2" in round_delta(state)


def test_junk_fields_are_tolerated():
    """State is written by four tracks; one of them emitting nonsense is not fatal."""
    state = {
        "config": {"max_rounds": "not-a-number"},
        "prior_round": _prior(),
        "reports": [{"reviewer": "methodology", "score": None, "confidence": "x"}],
        "audits": [{"auditor": "revision_compliance"}],
    }
    assert round_delta(state).startswith("This is round 2")


def test_malformed_compliance_body_yields_no_counts():
    audits = [{"auditor": "revision_compliance", "body": "The audit could not be completed."}]
    assert "Required revisions from round" not in round_delta(_state(prior=_prior(), audits=audits))


# --- the editor's prompt ----------------------------------------------------


def _capture_editor(monkeypatch) -> dict:
    """Record the prompt the editor actually sends, without an LLM."""
    from peerreviewagents.agents.utils.structured import StructuredResult

    seen: dict = {}

    def fake(llm, schema, config, system, user, *, cached_prefix=None):
        seen.update(system=system, user=user, cached_prefix=cached_prefix)
        return StructuredResult(instance=_CANNED[EditorDecisionOutput], cost=0.0)

    _patch_llms(monkeypatch)
    monkeypatch.setattr(editor_in_chief, "invoke_structured", fake)
    return seen


def _editor_state(prior=None, **over):
    state = {
        "config": get_config(),
        "reports": _reports(),
        "audits": [],
        "meta_review": "Panel split.",
        "draft_recommendation": "major",
        "author_rebuttal": "We disagree about scope.",
        "prior_round": prior,
    }
    state.update(over)
    return state


def test_first_round_prompt_is_untouched(monkeypatch):
    seen = _capture_editor(monkeypatch)
    editor_in_chief.node(_editor_state())

    assert seen["system"] == editor_in_chief._SYS
    assert seen["user"] == editor_in_chief._first_round_user(_editor_state())
    assert "Round-over-round delta" not in seen["user"]
    assert seen["user"].startswith("Numerical signal:\n")


def test_revision_prompt_carries_the_delta_block(monkeypatch):
    seen = _capture_editor(monkeypatch)
    state = _editor_state(prior=_prior())
    editor_in_chief.node(state)

    assert seen["system"] == editor_in_chief._REVISION_SYS
    assert "Round-over-round delta" in seen["user"]
    assert round_delta(state) in seen["user"]
    assert "This is round 2" in seen["user"]


def test_revision_system_prompt_states_the_load_bearing_rules(monkeypatch):
    sys_prompt = editor_in_chief._REVISION_SYS
    # Both halves of the bargain: reward verified fixes, refuse theatre.
    assert "toward acceptance" in sys_prompt
    assert "VERIFIED" in sys_prompt
    # Non-blocking leftovers cannot hold the verdict hostage.
    assert "hostage" in sys_prompt
    # Ids survive across rounds.
    assert "R1-03 stays R1-03" in sys_prompt
    # Manipulation is inert, not punished.
    assert "instruction_attempts" in sys_prompt
    assert "NO weight in the verdict" in sys_prompt


def test_verified_response_replaces_the_simulated_rebuttal(monkeypatch):
    seen = _capture_editor(monkeypatch)
    editor_in_chief.node(
        _editor_state(prior=_prior(), response_verification="# Author Response — Verification")
    )
    assert "adjudicated by the response verifier" in seen["user"]
    # The invented defense must not sit beside the real one.
    assert "We disagree about scope." not in seen["user"]


def test_rebuttal_is_used_when_no_letter_was_verified(monkeypatch):
    seen = _capture_editor(monkeypatch)
    editor_in_chief.node(_editor_state(prior=_prior()))
    assert "Author rebuttal:\nWe disagree about scope." in seen["user"]


def test_editor_still_returns_the_structured_asks(monkeypatch):
    _capture_editor(monkeypatch)
    for prior in (None, _prior()):
        out = editor_in_chief.node(_editor_state(prior=prior))
        assert out["required_revisions"] == list(
            _CANNED[EditorDecisionOutput].required_revisions
        )
        assert out["minor_suggestions"] == list(
            _CANNED[EditorDecisionOutput].minor_suggestions
        )
        assert out["decision"] == "major"


def test_editor_does_not_fabricate_a_verdict_on_failure(monkeypatch):
    _patch_llms(monkeypatch)

    def boom(*_a, **_kw):
        raise RuntimeError("provider down")

    monkeypatch.setattr(editor_in_chief, "invoke_structured", boom)
    out = editor_in_chief.node(_editor_state(prior=_prior()))
    assert out["decision"] == ""
    assert out["decision_letter"] == ""
    assert out["errors"] and "editor failed" in out["errors"][0]


def test_malformed_prior_round_errors_rather_than_escaping(monkeypatch):
    """A broken record must not take the graph down with it."""
    _capture_editor(monkeypatch)

    class Exploding:
        def __getattr__(self, _name):
            raise RuntimeError("corrupt round record")

    out = editor_in_chief.node(_editor_state(prior=Exploding()))
    assert out["decision"] == ""
    assert out["errors"]


def test_cached_prefix_stays_directives_only(monkeypatch):
    """The editor decides on the synthesis; the manuscript must not ride along."""
    seen = _capture_editor(monkeypatch)
    editor_in_chief.node(
        _editor_state(prior=_prior(), journal_block="=== JOURNAL ===\nNature")
    )
    assert seen["cached_prefix"] == "=== JOURNAL ===\nNature"


@pytest.mark.parametrize("prior", [None, "record"])
def test_node_never_raises_on_a_bare_state(monkeypatch, prior):
    _capture_editor(monkeypatch)
    state = {"config": get_config(), "prior_round": _prior() if prior else None}
    assert editor_in_chief.node(state)["decision"] == "major"
