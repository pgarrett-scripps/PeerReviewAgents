"""Corrections: challenging the review rather than the manuscript.

A correction is anchored to a prior round like a revision, but nothing about
the draft has changed — the claim is that a reviewer got something wrong. Two
things follow, and both are load-bearing:

* The compliance auditor must not run. Against an identical draft it would
  correctly report every required revision as undone, which would push the
  verdict *down* for an author who was right.
* Re-running one reviewer must still yield a whole-panel score. The reviewers
  left alone are carried forward, so the aggregate is over eight assessments
  and not over the one agent that happened to re-run.
"""

from __future__ import annotations

import json
import os

import pytest

from peerreviewagents import rounds
from peerreviewagents.agents.reviewers import REVIEWER_NAMES
from peerreviewagents.default_config import get_config
from peerreviewagents.graph.review_graph import (
    PeerReviewGraph,
    is_correction,
    is_revision,
    selected_reviewers,
)

PANEL = [
    ("methodology", 2.0, 4.0),
    ("data_analysis", 3.0, 4.0),
    ("novelty", 4.0, 3.0),
    ("clarity", 4.0, 5.0),
    ("literature", 3.0, 4.0),
    ("rigor", 2.0, 4.0),
    ("reproducibility", 3.0, 3.0),
    ("ethics", 5.0, 5.0),
]


def _prior_record() -> rounds.RoundRecord:
    return rounds.RoundRecord(
        schema_version=rounds.SCHEMA_VERSION,
        round=1,
        job_id="round-1",
        manuscript_title="A paper",
        manuscript_cache_key="deadbeef",
        decision="major",
        weighted_score=3.1,
        required_revisions=[
            rounds.RequiredRevision(id="R1-01", text="Report effect sizes."),
        ],
        reviewer_reports=[
            rounds.PriorReviewerReport(
                reviewer=name,
                score=score,
                confidence=conf,
                weaknesses=[rounds.PriorWeakness(id=f"{name}-1", text="a weakness")],
                questions=[],
            )
            for name, score, conf in PANEL
        ],
    )


def _prior_dir(tmp_path) -> str:
    run_dir = tmp_path / "round-1"
    run_dir.mkdir(exist_ok=True)
    rounds.save(_prior_record(), str(run_dir))
    for name, _s, _c in PANEL:
        (run_dir / f"review_{name}.md").write_text(
            f"# {name}\n\nThe original {name} report.\n", encoding="utf-8"
        )
    return str(run_dir)


def _cfg(tmp_path, **over):
    return get_config(revision_of=_prior_dir(tmp_path), **over)


# --- mode detection ---------------------------------------------------------


def test_correction_requires_a_prior_round():
    assert not is_correction(get_config(revision_mode="correction"))


def test_revision_is_the_default_mode(tmp_path):
    cfg = _cfg(tmp_path)
    assert is_revision(cfg) and not is_correction(cfg)


def test_correction_mode_is_recognised(tmp_path):
    assert is_correction(_cfg(tmp_path, revision_mode="correction"))


# --- reviewer selection -----------------------------------------------------


def test_full_panel_by_default():
    assert selected_reviewers(get_config()) == list(REVIEWER_NAMES)


def test_subset_is_honoured(tmp_path):
    cfg = _cfg(tmp_path, only_reviewers=["methodology"])
    assert selected_reviewers(cfg) == ["methodology"]


def test_unknown_reviewer_is_an_error_not_a_silent_drop(tmp_path):
    """A typo must not quietly shrink the panel."""
    cfg = _cfg(tmp_path, only_reviewers=["methodolgy"])
    with pytest.raises(ValueError, match="no such reviewer"):
        selected_reviewers(cfg)


def test_subset_without_a_prior_round_is_refused():
    """Without a prior round there is nothing to carry the others forward from."""
    cfg = get_config(only_reviewers=["methodology"])
    with pytest.raises(ValueError, match="requires revision_of"):
        selected_reviewers(cfg)


# --- the carry-forward ------------------------------------------------------


def test_untouched_reviewers_are_carried_forward(tmp_path):
    cfg = _cfg(tmp_path, revision_mode="correction", only_reviewers=["methodology"])
    carried = PeerReviewGraph(cfg)._carried_reports(_prior_record())

    names = sorted(r["reviewer"] for r in carried)
    assert "methodology" not in names, "the re-running reviewer must not be carried"
    assert len(carried) == len(PANEL) - 1, "every other reviewer must be carried"

    # The panel the editor sees is the carried set plus the one that re-runs:
    # eight assessments, not one.
    assert len(carried) + 1 == len(PANEL)


def test_carried_reports_keep_their_scores(tmp_path):
    cfg = _cfg(tmp_path, revision_mode="correction", only_reviewers=["methodology"])
    carried = PeerReviewGraph(cfg)._carried_reports(_prior_record())
    by_name = {r["reviewer"]: r for r in carried}
    assert by_name["ethics"]["score"] == 5.0
    assert by_name["ethics"]["confidence"] == 5.0
    assert by_name["rigor"]["weaknesses"] == ["a weakness"]


def test_carried_reports_carry_their_prose(tmp_path):
    """The digest concatenates bodies; a carried report needs a real one."""
    cfg = _cfg(tmp_path, revision_mode="correction", only_reviewers=["methodology"])
    carried = PeerReviewGraph(cfg)._carried_reports(_prior_record())
    body = next(r["body"] for r in carried if r["reviewer"] == "ethics")
    assert "The original ethics report." in body
    assert "Carried forward unchanged" in body, "a reader must see it was not re-run"


def test_missing_prior_body_degrades_rather_than_failing(tmp_path):
    run_dir = _prior_dir(tmp_path)
    os.remove(os.path.join(run_dir, "review_ethics.md"))
    cfg = get_config(
        revision_of=run_dir, revision_mode="correction", only_reviewers=["methodology"]
    )
    carried = PeerReviewGraph(cfg)._carried_reports(_prior_record())
    body = next(r["body"] for r in carried if r["reviewer"] == "ethics")
    assert "a weakness" in body, "should fall back to the record's own summary"


def test_full_panel_carries_nothing(tmp_path):
    """When every reviewer re-runs there is nothing to carry."""
    cfg = _cfg(tmp_path)
    assert PeerReviewGraph(cfg)._carried_reports(_prior_record()) == []


def test_first_round_carries_nothing():
    assert PeerReviewGraph(get_config())._carried_reports(None) == []


# --- what a correction must not do ------------------------------------------


def test_correction_omits_the_compliance_auditor(tmp_path):
    """The headline property. An unchanged draft must not read as non-compliance."""
    from peerreviewagents.graph.review_graph import build_graph

    revision = build_graph(_cfg(tmp_path)).nodes
    correction = build_graph(_cfg(tmp_path, revision_mode="correction")).nodes

    assert "audit_revision_compliance" in revision, "revisions still audit compliance"
    assert "audit_revision_compliance" not in correction, (
        "a correction that ran the compliance auditor would report every "
        "required revision as undone and push the verdict down"
    )


def test_correction_does_not_diff_the_manuscript(tmp_path):
    cfg = _cfg(tmp_path, revision_mode="correction")
    diff = PeerReviewGraph(cfg)._manuscript_diff(_prior_record(), {"Methods": "text"})
    assert not diff.available
    assert "unchanged" in diff.note, diff.note


def test_correction_still_builds_a_graph_with_the_chosen_reviewer(tmp_path):
    from peerreviewagents.graph.review_graph import build_graph

    nodes = build_graph(
        _cfg(tmp_path, revision_mode="correction", only_reviewers=["methodology"])
    ).nodes
    assert "reviewer_methodology" in nodes
    assert "reviewer_ethics" not in nodes, "unselected reviewers must not run"
