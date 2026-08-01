"""Tests for the optional desk-screen triage gate.

Reuses the fake-LLM machinery from test_pipeline so the canned desk-screen
output flows through the real graph wiring.
"""

from __future__ import annotations

import os

from test_pipeline import _CANNED, SAMPLE, _patch_llms

from cli.main import _run_failed
from peerreviewagents.agents.reviewers import REVIEWER_NAMES
from peerreviewagents.agents.schemas import DeskScreenOutput
from peerreviewagents.default_config import get_config
from peerreviewagents.graph.review_graph import PeerReviewGraph
from peerreviewagents.reports import write_reports

# --- config wiring ---------------------------------------------------------


def test_desk_screen_defaults_off():
    assert get_config().get("desk_screen") is False


def test_desk_screen_env_and_kwarg_precedence(monkeypatch):
    monkeypatch.setenv("PEERREVIEW_DESK_SCREEN", "true")
    assert get_config()["desk_screen"] is True
    monkeypatch.setenv("PEERREVIEW_DESK_SCREEN", "off")
    assert get_config()["desk_screen"] is False
    # Explicit kwarg (CLI flag) wins.
    assert get_config(desk_screen=True)["desk_screen"] is True


# --- schema ----------------------------------------------------------------


def test_desk_screen_output_renders_markdown():
    md = DeskScreenOutput(
        desk_reject=True, rationale="Out of scope.", reasons=["Wrong venue"]
    ).to_markdown()
    assert md.startswith("# Editorial Desk Screen")
    assert "Desk Reject" in md
    assert "Wrong venue" in md


# --- graph behavior --------------------------------------------------------


def test_disabled_graph_runs_full_pipeline(monkeypatch, tmp_path):
    _patch_llms(monkeypatch)
    graph = PeerReviewGraph(get_config(max_debate_rounds=1, output_dir=str(tmp_path)))
    state = graph.review(SAMPLE)
    assert len(state["reports"]) == len(REVIEWER_NAMES)
    assert not state.get("desk_rejected")
    assert not state.get("desk_screen")  # node never ran


def test_enabled_pass_runs_full_pipeline(monkeypatch, tmp_path):
    # Canned desk screen defaults to desk_reject=False -> proceed to panel.
    _patch_llms(monkeypatch)
    graph = PeerReviewGraph(
        get_config(desk_screen=True, max_debate_rounds=1, output_dir=str(tmp_path))
    )
    state = graph.review(SAMPLE)
    assert state.get("desk_rejected") is False
    assert state.get("desk_screen")  # the screen ran and recorded a note
    assert len(state["reports"]) == len(REVIEWER_NAMES)
    assert state["decision"] == "major"  # normal editor verdict


def test_enabled_reject_short_circuits(monkeypatch, tmp_path):
    monkeypatch.setitem(
        _CANNED,
        DeskScreenOutput,
        DeskScreenOutput(
            desk_reject=True,
            rationale="Clearly out of scope for the venue.",
            reasons=["Out of scope", "Below the venue bar"],
        ),
    )
    _patch_llms(monkeypatch)
    graph = PeerReviewGraph(
        get_config(desk_screen=True, max_debate_rounds=1, output_dir=str(tmp_path))
    )
    state = graph.review(SAMPLE)

    # Short-circuited: reject with no panel, no debate, no meta-review.
    assert state.get("desk_rejected") is True
    assert state["decision"] == "reject"
    assert not state.get("reports")
    assert not state.get("debate")
    assert not state.get("meta_review")
    assert state["decision_letter"].startswith("# Editorial Desk Screen")
    # A desk reject is a valid terminal outcome, not a failed run.
    assert _run_failed(state) is None

    # Reports: summary records the desk reject; no reviewer-scores section.
    run_dir = write_reports(state)
    assert os.path.exists(os.path.join(run_dir, "desk_screen.md"))
    assert os.path.exists(os.path.join(run_dir, "decision_letter.md"))
    summary = open(os.path.join(run_dir, "summary.md"), encoding="utf-8").read()
    assert "Desk reject" in summary
    assert "Reviewer Scores" not in summary
