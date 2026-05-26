"""End-to-end pipeline test using a fake LLM (no API keys / network required)."""

import os

from langchain_core.messages import AIMessage

from peerreviewagents.default_config import get_config
from peerreviewagents.graph.review_graph import PeerReviewGraph
from peerreviewagents.reports import write_reports

SAMPLE = os.path.join(os.path.dirname(__file__), "sample_manuscript.md")

_CANNED = """## Summary
The paper proposes a method and reports improvements.

## Strengths
- Clear motivation
- Simple approach

## Weaknesses
- Single cluster limits generalization
- Overclaimed broad generalization

## Questions
- How were baselines tuned?

## Assessment
Score: 3
Confidence: 4

## Draft Recommendation
major

DECISION: major
## Decision Letter
Thank you for your submission.
## Required Revisions
1. Add more datasets.
"""


class FakeLLM:
    def bind_tools(self, tools):
        return self

    def invoke(self, messages):
        return AIMessage(content=_CANNED)


def _patch_llms(monkeypatch):
    targets = [
        "peerreviewagents.agents.reviewers.base",
        "peerreviewagents.agents.debate.base",
        "peerreviewagents.agents.synthesis.meta_reviewer",
        "peerreviewagents.agents.integrity.base",
        "peerreviewagents.agents.integrity.citations",
        "peerreviewagents.agents.editor.editor_in_chief",
    ]
    for mod in targets:
        monkeypatch.setattr(f"{mod}.make_llm", lambda config, depth="deep": FakeLLM())


def test_full_pipeline(monkeypatch, tmp_path):
    _patch_llms(monkeypatch)
    cfg = get_config(research_enabled=False, max_debate_rounds=2, output_dir=str(tmp_path))
    graph = PeerReviewGraph(cfg)
    state = graph.review(SAMPLE)

    # Every configured reviewer produced a report.
    assert len(state["reports"]) == len(cfg["reviewer_set"])
    # Debate ran the configured number of rounds (advocate+skeptic per round).
    assert state["debate_round"] == cfg["max_debate_rounds"]
    assert len(state["debate"]) == cfg["max_debate_rounds"] * 2
    # Integrity panel and editor produced output.
    assert len(state["integrity_findings"]) == 4
    assert state["decision"] == "major"
    assert not state.get("errors")

    run_dir = write_reports(state)
    assert os.path.exists(os.path.join(run_dir, "summary.md"))
    assert os.path.exists(os.path.join(run_dir, "decision_letter.md"))
    assert os.path.exists(os.path.join(run_dir, "debate_transcript.md"))


def test_ingest_sections():
    from peerreviewagents.ingest.loader import load_manuscript

    title, md, sections = load_manuscript(SAMPLE)
    assert "Widget" in title
    assert "methods" in sections
    assert "results" in sections
