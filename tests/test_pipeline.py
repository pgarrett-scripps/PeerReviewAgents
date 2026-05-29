"""End-to-end pipeline test using a fake LLM (no API keys / network required)."""

import os

from langchain_core.messages import AIMessage

from peerreviewagents.agents.reviewers import REVIEWER_NAMES
from peerreviewagents.default_config import get_config
from peerreviewagents.graph.review_graph import PeerReviewGraph
from peerreviewagents.reports import write_reports

SAMPLE = os.path.join(os.path.dirname(__file__), "sample_manuscript.md")


# Single markdown response that satisfies every agent in the pipeline.
# Every agent now reads YAML frontmatter for its scalars; the body is
# whatever the model writes. Having all the scalar keys present (score,
# confidence, draft_recommendation, decision) means one canned reply
# serves reviewers, meta-reviewer, and editor alike.
_CANNED = (
    "---\n"
    "score: 3\n"
    "confidence: 4\n"
    "draft_recommendation: major\n"
    "decision: major\n"
    "---\n"
    "# Review\n\n"
    "## Summary\n"
    "The paper proposes a method and reports improvements.\n\n"
    "## Strengths\n"
    "- Clear motivation\n"
    "- Simple approach\n\n"
    "## Weaknesses\n"
    "- Single cluster limits generalization\n"
    "- Overclaimed broad generalization\n\n"
    "## Questions\n"
    "- How were baselines tuned?\n"
)


class FakeLLM:
    """Stand-in chat model used by the pipeline test."""

    def bind(self, **_kwargs):
        return self

    def invoke(self, messages, **_kwargs):
        return AIMessage(content=_CANNED)


def _patch_llms(monkeypatch):
    targets = [
        "peerreviewagents.agents.reviewers.base",
        "peerreviewagents.agents.debate.base",
        "peerreviewagents.agents.synthesis.meta_reviewer",
        "peerreviewagents.agents.author.rebuttal",
        "peerreviewagents.agents.editor.editor_in_chief",
    ]
    # Accept arbitrary kwargs so the meta-reviewer / editor can pass
    # reasoning_effort= without blowing up the lambda signature.
    for mod in targets:
        monkeypatch.setattr(f"{mod}.make_llm", lambda config, **_kwargs: FakeLLM())


def test_full_pipeline(monkeypatch, tmp_path):
    _patch_llms(monkeypatch)
    cfg = get_config(max_debate_rounds=2, output_dir=str(tmp_path))
    graph = PeerReviewGraph(cfg)
    state = graph.review(SAMPLE)

    # Every reviewer in the hardcoded panel produced a report.
    assert len(state["reports"]) == len(REVIEWER_NAMES)
    # Each report carries a markdown body that starts with frontmatter
    # and the scalars are parsed out of it.
    for r in state["reports"]:
        assert r["body"].startswith("---")
        assert r["score"] == 3.0
        assert r["confidence"] == 4.0
    # Debate ran the configured number of rounds (advocate+skeptic per round).
    assert state["debate_round"] == cfg["max_debate_rounds"]
    assert len(state["debate"]) == cfg["max_debate_rounds"] * 2
    # Author rebuttal ran between meta-review and editor.
    assert state.get("author_rebuttal")
    # Meta-reviewer parsed its draft recommendation from frontmatter.
    assert state["draft_recommendation"] == "major"
    # Editor parsed the final decision from frontmatter.
    assert state["decision"] == "major"
    assert state["decision_letter"].startswith("---")
    assert not state.get("errors")

    run_dir = write_reports(state)
    assert os.path.exists(os.path.join(run_dir, "summary.md"))
    assert os.path.exists(os.path.join(run_dir, "decision_letter.md"))
    assert os.path.exists(os.path.join(run_dir, "author_rebuttal.md"))
    assert os.path.exists(os.path.join(run_dir, "debate_transcript.md"))
    # No citations file should be produced — that pipeline was removed.
    assert not any(
        f.startswith("citations_") for f in os.listdir(run_dir)
    )


def test_ingest_sections():
    from peerreviewagents.ingest.loader import load_manuscript

    title, md, sections = load_manuscript(SAMPLE)
    assert "Widget" in title
    assert "methods" in sections
    assert "results" in sections
