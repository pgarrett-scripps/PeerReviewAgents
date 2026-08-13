"""A reviewer that fails must not vanish from the aggregate silently.

On C-09 the rigor reviewer was truncated at max_tokens, failed its structured
retry, and dropped out. The panel line read n=7 and named no absence, so the
meta-reviewer and editor weighed seven verdicts believing they had eight —
and rigor is one of the specialties most likely to be carrying the objection
that decides a paper.

This is distinct from a reviewer reporting "nothing in my dimension to judge":
that one is present with a null score and already named.
"""

from __future__ import annotations

from peerreviewagents.agents.reviewers import REVIEWER_NAMES
from peerreviewagents.agents.utils.agent_utils import score_summary


def report(name, score=3.0, confidence=4.0):
    return {"reviewer": name, "score": score, "confidence": confidence, "body": ""}


def state_with(reports, **config):
    return {"reports": reports, "config": config}


def test_a_full_panel_says_nothing_about_completeness():
    s = score_summary(state_with([report(n) for n in REVIEWER_NAMES]))
    assert "INCOMPLETE PANEL" not in s


def test_a_missing_reviewer_is_named():
    present = [n for n in REVIEWER_NAMES if n != "rigor"]
    s = score_summary(state_with([report(n) for n in present]))
    assert "INCOMPLETE PANEL" in s
    assert "rigor" in s


def test_a_not_applicable_reviewer_is_not_reported_as_missing():
    """It reported; it just had nothing to score. Already handled, and calling
    it a failure would misrepresent a working reviewer."""
    reports = [report(n) for n in REVIEWER_NAMES if n != "data_analysis"]
    reports.append({
        "reviewer": "data_analysis", "score": None, "confidence": 3.0,
        "not_applicable_reason": "no quantitative analysis", "body": "",
    })
    s = score_summary(state_with(reports))
    assert "INCOMPLETE PANEL" not in s
    assert "not applicable" in s


def test_a_deliberate_subset_is_not_treated_as_missing():
    """A correction re-runs named reviewers only; the rest are carried
    forward by design and are not failures."""
    s = score_summary(state_with(
        [report("methodology")], only_reviewers=["methodology"]
    ))
    assert "INCOMPLETE PANEL" not in s


def test_the_absence_warns_against_reading_it_as_no_concern():
    """The failure mode is the editor treating a missing specialty as a clean
    bill of health on that dimension."""
    present = [n for n in REVIEWER_NAMES if n != "methodology"]
    s = score_summary(state_with([report(n) for n in present]))
    assert "do not read a missing specialty as no concern" in s


def test_reporting_failure_labelled_apart_from_reasoned_abstention():
    """The editor must not read a schema failure as 'nothing to judge'.

    Both events carry a null score, but one is a judgment and the other is a
    reviewer that wrote its assessment and never scored it. On the live run
    that motivated this, the unlabelled version invited the editor to treat
    four sharp, low-scoring review bodies as neutral abstentions.
    """
    from peerreviewagents.agents.schemas import NO_SCORE_NO_REASON
    from peerreviewagents.agents.utils.agent_utils import score_summary

    state = {"config": {}, "reports": [
        {"reviewer": "rigor", "score": None, "confidence": 3,
         "not_applicable_reason": NO_SCORE_NO_REASON},
        {"reviewer": "data_analysis", "score": None, "confidence": 3,
         "not_applicable_reason": "No statistical claims to judge."},
        {"reviewer": "clarity", "score": 4.0, "confidence": 4.0},
    ]}
    line = score_summary(state)
    assert "rigor no score returned (reporting failure" in line
    assert "data_analysis n/a" in line
