"""Tests for the reviewer's revision path and the score-consistency guard.

Two properties carry this track and are worth breaking a build over:

* a reviewer in round N sees its OWN prior report and nothing from anyone
  else — a revision round must not be where panel independence quietly ends;
* a reviewer that reports its own points resolved and then holds its score is
  asked to explain itself exactly once, and the guard never invents a number
  the reviewer did not endorse.

The fake-LLM harness comes from test_pipeline; the scripted variant below
adds per-call responses (the guard needs a first and a second answer) and
records the prompts so the independence claims can be asserted directly.
"""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage
from test_pipeline import _CANNED, FakeLLM

from peerreviewagents import rounds
from peerreviewagents.agents.reviewers import data_analysis, literature, methodology
from peerreviewagents.agents.schemas import (
    NewIssue,
    PriorPointVerdict,
    RevisionReviewerOutput,
)
from peerreviewagents.default_config import get_config
from peerreviewagents.ingest import diff as ingest_diff

# --- fake LLM --------------------------------------------------------------

_CALL_COST = 0.01


class _ScriptedChain:
    """Returns queued instances, then falls back to the canned one."""

    def __init__(self, llm: "_ScriptedLLM", schema, include_raw: bool):
        self._llm = llm
        self._schema = schema
        self._include_raw = include_raw

    def invoke(self, messages, **_kwargs):
        self._llm.prompts.append(_flatten(messages))
        if self._llm.fail:
            raise RuntimeError("provider exploded")
        queue = self._llm.queue
        instance = queue.pop(0) if queue else _CANNED[self._schema]
        if not self._include_raw:
            return instance
        # Cost rides on the raw message, which is where _call_cost reads it —
        # so the guard's second call has to be summed in for the total to be
        # right.
        raw = AIMessage(
            content="", response_metadata={"token_usage": {"cost": _CALL_COST}}
        )
        return {"raw": raw, "parsed": instance, "parsing_error": None}


class _ScriptedLLM(FakeLLM):
    """FakeLLM that can answer differently per call and remembers its prompts."""

    def __init__(self, *responses, fail: bool = False):
        self.queue = list(responses)
        self.prompts: list[str] = []
        self.fail = fail

    def with_structured_output(self, schema, **kwargs):
        return _ScriptedChain(self, schema, kwargs.get("include_raw", False))


def _flatten(messages) -> str:
    parts: list[str] = []
    for message in messages:
        content = getattr(message, "content", message)
        if isinstance(content, list):
            parts += [
                b.get("text", "") if isinstance(b, dict) else str(b) for b in content
            ]
        else:
            parts.append(str(content))
    return "\n".join(parts)


def _patch(monkeypatch, llm, module=methodology):
    monkeypatch.setattr(
        "peerreviewagents.agents.reviewers.base.make_llm",
        lambda config, **_kwargs: llm,
    )
    return module


# --- fixtures --------------------------------------------------------------


def _revision_output(**over) -> RevisionReviewerOutput:
    base = dict(
        prior_score=3,
        score=3,
        confidence=4,
        prior_points=[
            PriorPointVerdict(
                id="methodology-1",
                status="resolved",
                evidence="§3.2 now reports all three clusters separately.",
            ),
        ],
        new_issues=[],
        summary="The revision reports per-cluster results as asked.",
        score_rationale="The design concerns I raised have been addressed.",
        strengths=["Per-cluster reporting is now explicit."],
        questions=["Which cluster produced the outlier in Figure 2?"],
    )
    base.update(over)
    return RevisionReviewerOutput(**base)


@pytest.fixture(autouse=True)
def _canned_revision(monkeypatch):
    """Default answer for any call this module does not script explicitly."""
    monkeypatch.setitem(_CANNED, RevisionReviewerOutput, _revision_output(score=4))


@pytest.fixture
def prior_round():
    """A round-1 record with two reviewers, so scoping can be checked."""
    return rounds.build_from_state(
        {
            "manuscript_title": "A Lightweight Method",
            "config": {},
            "decision": "major",
            "required_revisions": ["Report per-cluster results."],
            "reports": [
                {
                    "reviewer": "methodology",
                    "score": 3,
                    "confidence": 4,
                    "weaknesses": ["Only a single production cluster is used."],
                    "questions": ["How were baselines tuned?"],
                    "body": "",
                },
                {
                    "reviewer": "rigor",
                    "score": 2,
                    "confidence": 3,
                    "weaknesses": ["No random seed is reported for training."],
                    "questions": [],
                    "body": "",
                },
            ],
        },
        job_id="20260801-round1",
    )


def _state(prior_round=None, **over) -> dict:
    state = {
        "manuscript_title": "A Lightweight Method",
        "manuscript_md": "# A Lightweight Method\n\nWe train on three clusters.",
        "sections": {"methods": "We train on three clusters with seed 42."},
        "config": get_config(),
        "prior_round": prior_round,
        "manuscript_diff": ingest_diff.diff_sections(
            {"methods": "We train on one cluster."},
            {"methods": "We train on three clusters with seed 42."},
        ),
        "reports": [],
        "errors": [],
        "total_cost": 0.0,
    }
    state.update(over)
    return state


# --- schema selection ------------------------------------------------------


def test_revision_path_emits_the_revision_schema(monkeypatch, prior_round):
    llm = _ScriptedLLM()
    _patch(monkeypatch, llm)
    out = methodology.node(_state(prior_round))

    report = out["reports"][0]
    assert report["reviewer"] == "methodology"
    assert report["body"].startswith("# Methodology Reviewer — Revision Review")
    # Score movement is rendered as a delta, which only the revision schema does.
    assert "3/5 ↑ 4/5" in report["body"]
    assert not out.get("errors")


def test_first_round_path_is_untouched(monkeypatch):
    llm = _ScriptedLLM()
    _patch(monkeypatch, llm)
    out = methodology.node(_state(prior_round=None))

    report = out["reports"][0]
    assert report["body"].startswith("# Methodology Reviewer")
    assert "Revision Review" not in report["body"]
    # The canned first-round reviewer output, unchanged.
    assert report["score"] == 3.0
    assert report["weaknesses"] == [
        "Single cluster limits generalization",
        "Overclaimed broad generalization",
    ]


def test_tool_using_reviewer_still_reviews_in_a_revision_round(
    monkeypatch, prior_round
):
    """The research-tool loop must survive the new path, not just the plain one."""
    llm = _ScriptedLLM()
    _patch(monkeypatch, llm, module=literature)
    out = literature.node(_state(prior_round))

    body = out["reports"][0]["body"]
    assert body.startswith("# Related-Work & Citations Reviewer — Revision Review")
    assert not out.get("errors")


# --- what reaches the prompt ----------------------------------------------


def test_reviewer_gets_its_own_prior_report_and_no_one_elses(
    monkeypatch, prior_round
):
    llm = _ScriptedLLM()
    _patch(monkeypatch, llm)
    methodology.node(_state(prior_round))

    prompt = llm.prompts[0]
    assert "[methodology-1]" in prompt
    assert "Only a single production cluster" in prompt
    # The rigor reviewer's critique must not leak into this prompt.
    assert "random seed" not in prompt.lower()
    assert "rigor-1" not in prompt


def test_prompt_carries_the_diff_and_the_task(monkeypatch, prior_round):
    llm = _ScriptedLLM()
    _patch(monkeypatch, llm)
    methodology.node(_state(prior_round))

    prompt = llm.prompts[0]
    assert "What changed since the previous draft" in prompt
    assert "three clusters" in prompt
    assert "one ruling for EVERY weakness id" in prompt
    assert "caused_by_the_revision" in prompt


def test_verified_claims_reach_the_reviewer_framed_as_pointers(
    monkeypatch, prior_round
):
    llm = _ScriptedLLM()
    _patch(monkeypatch, llm)
    methodology.node(_state(
        prior_round,
        verified_claims_block=(
            "## Passages the authors ask you to re-read\n\n"
            "- Per-cluster results were added (re: methodology-1)"
        ),
    ))

    prompt = llm.prompts[0]
    assert "Per-cluster results were added" in prompt
    assert "interested party" in prompt
    assert "The manuscript is the evidence; their letter never is." in prompt


def test_no_author_letter_means_no_pointer_section(monkeypatch, prior_round):
    llm = _ScriptedLLM()
    _patch(monkeypatch, llm)
    methodology.node(_state(prior_round))
    assert "interested party" not in llm.prompts[0]


def test_reviewer_without_a_prior_report_is_told_so(monkeypatch, prior_round):
    """A specialist absent from round 1 still re-reviews, with nothing to rule on."""
    llm = _ScriptedLLM()
    _patch(monkeypatch, llm, module=data_analysis)
    data_analysis.node(_state(prior_round))

    prompt = llm.prompts[0]
    assert "no report on record for the previous round" in prompt
    assert "leave prior_points empty" in prompt


# --- prior_score is pinned, never trusted -----------------------------------


def test_prior_score_is_pinned_from_the_record(monkeypatch, prior_round):
    """A miscopied prior_score must not reach the rendered arrow."""
    llm = _ScriptedLLM(_revision_output(prior_score=5, score=4))
    _patch(monkeypatch, llm)
    out = methodology.node(_state(prior_round))
    # The record says 3, whatever the model copied.
    assert "3/5 ↑ 4/5" in out["reports"][0]["body"]


def test_miscopied_prior_score_cannot_disarm_the_guard(monkeypatch, prior_round):
    """Inventing a lower prior makes a stuck score look like a raise."""
    disguised = _revision_output(prior_score=2, score=3)   # true prior is 3
    earned = _revision_output(
        score=4, score_rationale="On reflection the fixes earn a 4.",
    )
    llm = _ScriptedLLM(disguised, earned)
    _patch(monkeypatch, llm)
    out = methodology.node(_state(prior_round))

    assert len(llm.prompts) == 2, "the guard must fire on the true prior score"
    assert out["reports"][0]["score"] == 4.0


# --- omitted rulings ---------------------------------------------------------


def test_omitted_rulings_are_filled_as_outstanding(monkeypatch, prior_round):
    """Silence on a prior point must not erase it from the record."""
    llm = _ScriptedLLM(_revision_output(score=3, prior_points=[]))
    _patch(monkeypatch, llm)
    out = methodology.node(_state(prior_round))

    report = out["reports"][0]
    assert len(llm.prompts) == 1, "an outstanding fill is not a stuck score"
    assert report["weaknesses"], "an unruled point must carry forward"
    assert report["weaknesses"][0].startswith("Only a single production cluster")
    assert "did not rule" in report["weaknesses"][0]
    # The fill is visible in the rendered report, not just in state.
    assert "[methodology-1] outstanding" in report["body"]
    assert "did not rule" in report["body"]


def test_partial_omission_fills_only_the_skipped_ids(monkeypatch):
    prior = rounds.build_from_state(
        {
            "manuscript_title": "A Lightweight Method",
            "config": {},
            "decision": "major",
            "required_revisions": [],
            "reports": [
                {
                    "reviewer": "methodology",
                    "score": 3,
                    "confidence": 4,
                    "weaknesses": [
                        "Only a single production cluster is used.",
                        "The ablation omits the largest workload.",
                    ],
                    "questions": [],
                    "body": "",
                },
            ],
        },
        job_id="j",
    )
    llm = _ScriptedLLM(_revision_output(score=4))   # rules on methodology-1 only
    _patch(monkeypatch, llm)
    report = methodology.node(_state(prior))["reports"][0]

    assert len(report["weaknesses"]) == 1
    assert report["weaknesses"][0].startswith("The ablation omits")
    assert "did not rule" in report["weaknesses"][0]
    assert "[methodology-2] outstanding" in report["body"]


# --- repeated abstention -----------------------------------------------------


def test_a_prior_abstainer_may_return_null_again(monkeypatch):
    """The prompt's 'return null again' must be an answer the schema accepts."""
    prior = rounds.build_from_state(
        {
            "manuscript_title": "A Lightweight Method",
            "config": {},
            "decision": "major",
            "required_revisions": [],
            "reports": [
                {
                    "reviewer": "methodology",
                    "score": None,
                    "not_applicable_reason": "Nothing methodological to judge.",
                    "confidence": 5,
                    "weaknesses": [],
                    "questions": [],
                    "body": "",
                },
            ],
        },
        job_id="j",
    )
    still_na = RevisionReviewerOutput(
        prior_score=None,
        score=None,
        not_applicable_reason="The revision still contains nothing methodological.",
        confidence=5,
        prior_points=[],
        new_issues=[],
        summary="Still nothing in my dimension to judge.",
        score_rationale="No score last round and none now.",
    )
    llm = _ScriptedLLM(still_na)
    _patch(monkeypatch, llm)
    out = methodology.node(_state(prior))

    assert "return null again" in llm.prompts[0]
    report = out["reports"][0]
    assert report["score"] is None
    assert "n/a → n/a" in report["body"]
    assert "Not applicable to this manuscript" in report["body"]
    assert not out.get("errors")


def test_revision_abstention_requires_a_reason():
    """Same contract as ReviewerOutput: a bare null is rejected, not recorded."""
    with pytest.raises(Exception, match="not_applicable_reason"):
        RevisionReviewerOutput(
            prior_score=None,
            score=None,
            confidence=5,
            prior_points=[],
            new_issues=[],
            summary="s",
            score_rationale="r",
        )


# --- score-consistency guard ----------------------------------------------


def test_guard_fires_on_a_stuck_score_and_reasks_exactly_once(
    monkeypatch, prior_round
):
    stuck = _revision_output(score=3)          # everything resolved, score held
    earned = _revision_output(
        score=4,
        score_rationale="On reflection the per-cluster results earn a 4.",
    )
    llm = _ScriptedLLM(stuck, earned)
    _patch(monkeypatch, llm)
    out = methodology.node(_state(prior_round))

    assert len(llm.prompts) == 2, "the guard re-asks once, never twice"
    report = out["reports"][0]
    assert report["score"] == 4.0, "the reviewer's own corrected score is used"
    assert "has not been adjusted" not in report["body"]
    # Both calls are paid for.
    assert out["total_cost"] == pytest.approx(2 * _CALL_COST)


def test_challenge_quotes_the_contradiction_back(monkeypatch, prior_round):
    stuck = _revision_output(score=3)
    llm = _ScriptedLLM(stuck, _revision_output(score=4))
    _patch(monkeypatch, llm)
    methodology.node(_state(prior_round))

    challenge = llm.prompts[1]
    assert "[methodology-1] resolved" in challenge
    assert "scored the revised manuscript 3/5 against your previous 3/5" in challenge
    assert stuck.score_rationale in challenge
    # It must demand one of two specific answers, not just re-ask the question.
    assert "The score should move" in challenge
    assert "Something genuinely does still hold it down" in challenge


def test_guard_is_silent_when_a_revision_caused_issue_explains_the_score(
    monkeypatch, prior_round
):
    explained = _revision_output(
        score=3,
        new_issues=[
            NewIssue(
                issue="The new per-cluster table contradicts the pooled mean in Table 1.",
                caused_by_the_revision=True,
            ),
        ],
        score_rationale="Points resolved, but the new table introduces a conflict.",
    )
    llm = _ScriptedLLM(explained)
    _patch(monkeypatch, llm)
    out = methodology.node(_state(prior_round))

    assert len(llm.prompts) == 1, "an explained hold needs no challenge"
    assert out["reports"][0]["score"] == 3.0
    assert out["total_cost"] == pytest.approx(_CALL_COST)


def test_guard_is_silent_when_a_point_is_still_open(monkeypatch, prior_round):
    llm = _ScriptedLLM(_revision_output(
        score=3,
        prior_points=[
            PriorPointVerdict(
                id="methodology-1",
                status="partial",
                evidence="Two of three clusters are reported; the third is not.",
            ),
        ],
    ))
    _patch(monkeypatch, llm)
    methodology.node(_state(prior_round))
    assert len(llm.prompts) == 1


def test_guard_keeps_the_first_verdict_when_the_answer_moves_the_goalposts(
    monkeypatch, prior_round
):
    """A late objection the reviewer admits was always visible is not an answer."""
    stuck = _revision_output(score=3)
    drifted = _revision_output(
        score=3,
        new_issues=[
            NewIssue(
                issue="The abstract has always overstated the scope.",
                caused_by_the_revision=False,
            ),
        ],
    )
    llm = _ScriptedLLM(stuck, drifted)
    _patch(monkeypatch, llm)
    out = methodology.node(_state(prior_round))

    report = out["reports"][0]
    assert len(llm.prompts) == 2
    # The reviewer's original verdict stands — nothing is fabricated for it.
    assert report["score"] == 3.0
    assert "has not been adjusted" in report["body"]
    # The note blames the reviewer, because this time the reviewer earned it.
    assert "did neither" in report["body"]
    assert "The abstract has always overstated" not in report["body"]
    assert out["total_cost"] == pytest.approx(2 * _CALL_COST)


def test_guard_refuses_an_answer_that_lowers_the_score(monkeypatch, prior_round):
    """Being asked to justify a score must not become a reason to cut it."""
    stuck = _revision_output(score=3)
    retaliation = _revision_output(
        score=2,
        prior_points=[
            PriorPointVerdict(
                id="methodology-1",
                status="outstanding",
                evidence="Reconsidered: the clusters are not independent.",
            ),
        ],
    )
    llm = _ScriptedLLM(stuck, retaliation)
    _patch(monkeypatch, llm)
    out = methodology.node(_state(prior_round))

    assert out["reports"][0]["score"] == 3.0
    assert "has not been adjusted" in out["reports"][0]["body"]
    # The note names what happened — a refused cut, not a reviewer that went silent.
    assert "scoring lower instead" in out["reports"][0]["body"]
    assert "did neither" not in out["reports"][0]["body"]


def test_failed_challenge_keeps_the_review(monkeypatch, prior_round):
    """A provider error during the re-ask must not drop the reviewer's verdict."""
    stuck = _revision_output(score=3)

    class _FailOnSecond(_ScriptedLLM):
        def with_structured_output(self, schema, **kwargs):
            if self.prompts:
                self.fail = True
            return super().with_structured_output(schema, **kwargs)

    llm = _FailOnSecond(stuck)
    _patch(monkeypatch, llm)
    out = methodology.node(_state(prior_round))

    report = out["reports"][0]
    assert report["score"] == 3.0
    assert "has not been adjusted" in report["body"]
    # The failure was the call's, and the note must not pin it on the reviewer.
    assert "infrastructure failure" in report["body"]
    assert "did neither" not in report["body"]
    assert not out.get("errors")


# --- what the next round inherits ------------------------------------------


def test_open_points_and_new_issues_carry_forward(monkeypatch, prior_round):
    llm = _ScriptedLLM(_revision_output(
        score=3,
        prior_points=[
            PriorPointVerdict(
                id="methodology-1",
                status="partial",
                evidence="Two clusters are reported; the third is still pooled.",
            ),
        ],
        new_issues=[
            NewIssue(
                issue="The new Table 3 has no error bars.",
                caused_by_the_revision=True,
            ),
        ],
    ))
    _patch(monkeypatch, llm)
    carried = methodology.node(_state(prior_round))["reports"][0]["weaknesses"]

    assert len(carried) == 2
    # The point keeps its original wording so round 3 hands back the same ask.
    assert carried[0].startswith("Only a single production cluster is used.")
    assert "only partly addressed in the revision" in carried[0]
    assert carried[1] == "The new Table 3 has no error bars. (introduced by the revision)"


def test_resolved_points_are_not_handed_back(monkeypatch, prior_round):
    llm = _ScriptedLLM(_revision_output(score=4))
    _patch(monkeypatch, llm)
    report = methodology.node(_state(prior_round))["reports"][0]

    assert report["weaknesses"] == []
    assert report["questions"] == ["Which cluster produced the outlier in Figure 2?"]


def test_carried_weaknesses_become_round_three_ids(monkeypatch, prior_round):
    """The carried text has to survive into the next round record, or it is lost."""
    llm = _ScriptedLLM(_revision_output(
        score=3,
        prior_points=[
            PriorPointVerdict(
                id="methodology-1",
                status="outstanding",
                evidence="No per-cluster results appear anywhere.",
            ),
        ],
    ))
    _patch(monkeypatch, llm)
    out = methodology.node(_state(prior_round))

    record = rounds.build_from_state(
        {
            "manuscript_title": "A Lightweight Method",
            "config": {"revision_of": prior_round.job_id},
            "prior_round": prior_round,
            "decision": "major",
            "required_revisions": [],
            "reports": out["reports"],
        },
        job_id="20260802-round2",
    )
    assert record.round == 2
    weakness = record.report_for("methodology").weaknesses[0]
    assert weakness.id == "methodology-1"
    assert "still unaddressed in the revision" in weakness.text


# --- failure handling ------------------------------------------------------


def test_a_failing_call_returns_an_error_not_an_exception(monkeypatch, prior_round):
    llm = _ScriptedLLM(fail=True)
    _patch(monkeypatch, llm)
    out = methodology.node(_state(prior_round))

    assert out["errors"] and "methodology reviewer failed" in out["errors"][0]
    assert "reports" not in out
