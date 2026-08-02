"""End-to-end pipeline test using a fake LLM (no API keys / network required)."""

import os

from langchain_core.messages import AIMessage

from peerreviewagents.agents.reviewers import REVIEWER_NAMES
from peerreviewagents.agents.schemas import (
    AuditFinding,
    AuditOutput,
    AuthorRebuttalOutput,
    ComplianceFinding,
    DebateOutput,
    DeskScreenOutput,
    EditorDecisionOutput,
    JournalRecommendationsOutput,
    JournalSuggestion,
    MetaReviewOutput,
    PriorPointVerdict,
    ResponseVerificationOutput,
    ReviewerOutput,
    RevisionComplianceOutput,
    RevisionReviewerOutput,
    VerifiedClaim,
)
from peerreviewagents.default_config import get_config
from peerreviewagents.graph.review_graph import PeerReviewGraph
from peerreviewagents.reports import write_reports

SAMPLE = os.path.join(os.path.dirname(__file__), "sample_manuscript.md")


# One canned instance per schema. The pipeline test never makes a real
# LLM call — it patches every agent's make_llm to a FakeLLM that returns
# the matching instance from .with_structured_output(Schema).invoke(...).
_CANNED: dict[type, object] = {
    # Desk screen defaults to "pass" so enabling the gate doesn't change a
    # canned run unless a test overrides this entry.
    DeskScreenOutput: DeskScreenOutput(
        desk_reject=False,
        rationale="Within scope and complete enough for full review.",
        reasons=[],
    ),
    AuditOutput: AuditOutput(
        summary="Methods are mostly documented; a few HARD identifiers are missing.",
        categories_detected=["Cross-cutting", "Computational/ML"],
        findings=[
            AuditFinding(
                category="Computational/ML",
                item="Random seed",
                severity="HARD",
                status="missing",
                evidence="Training described without any seed or seed-averaging statement.",
            ),
            AuditFinding(
                category="Cross-cutting",
                item="Code availability",
                severity="SOFT",
                status="missing",
                evidence="No code-availability statement for the custom pipeline.",
            ),
        ],
    ),
    ReviewerOutput: ReviewerOutput(
        score=3,
        confidence=4,
        summary="The paper proposes a method and reports improvements.",
        strengths=["Clear motivation", "Simple approach"],
        weaknesses=[
            "Single cluster limits generalization",
            "Overclaimed broad generalization",
        ],
        questions=["How were baselines tuned?"],
    ),
    DebateOutput: DebateOutput(
        argument="The contribution is incremental but the empirical signal is clean.",
        key_points=["Methodology is reproducible", "Comparisons are fair"],
    ),
    MetaReviewOutput: MetaReviewOutput(
        draft_recommendation="major",
        synthesis="Panel split between minor and major; the weakness consensus dominates.",
        decisive_factors="Generalization claim outruns the experimental scope.",
    ),
    AuthorRebuttalOutput: AuthorRebuttalOutput(
        concessions=[],
        disagreements=[],
        load_bearing_critiques=["scope of generalization claim"],
    ),
    EditorDecisionOutput: EditorDecisionOutput(
        decision="major",
        summary_of_evaluation="Strong methodology, weak generalization claim.",
        required_revisions=["Narrow the generalization claim or add a second cluster."],
        minor_suggestions=["Tighten the abstract."],
    ),
    JournalRecommendationsOutput: JournalRecommendationsOutput(
        as_is=[],
        after_revision=[
            JournalSuggestion(
                name="Specialty Journal X",
                fit_reasoning="Direct topic match for widget-throughput estimation.",
                acceptance_realism="Plausible after the generalization claim is narrowed.",
            ),
        ],
        alternative=[
            JournalSuggestion(
                name="arXiv cs.LG",
                fit_reasoning="Preprint server appropriate while authors revise.",
                acceptance_realism="Self-publication; immediate.",
            ),
        ],
        notes="Avoid headline venues until the second cluster is added.",
    ),
    # --- revision round -----------------------------------------------------
    # Defaults describe an honest, successful revision: the reviewer's point
    # was fixed and the score moved because of it. Tests that need a stuck
    # score, a stonewalling author, or an unchanged draft override these with
    # monkeypatch.setitem rather than editing this module.
    RevisionReviewerOutput: RevisionReviewerOutput(
        prior_score=3,
        score=4,
        confidence=4,
        prior_points=[
            PriorPointVerdict(
                id="methodology-1",
                status="resolved",
                evidence="Methods now report results for all three clusters.",
            ),
        ],
        new_issues=[],
        summary="The revision addresses the single-cluster limitation I raised.",
        score_rationale="The one weakness I raised is resolved, so the score rises.",
        strengths=["Clear reporting of the added clusters."],
        questions=[],
    ),
    RevisionComplianceOutput: RevisionComplianceOutput(
        summary="One of two required revisions is carried out; the other is not.",
        findings=[
            ComplianceFinding(
                id="R1-01",
                status="addressed",
                manuscript_evidence="Results now report per-cluster error.",
                author_claim="We added per-cluster results.",
                claim_accuracy="corroborated",
                blocking=False,
            ),
            ComplianceFinding(
                id="R1-02",
                status="not_addressed",
                manuscript_evidence="",
                author_claim="",
                claim_accuracy="no_claim",
                blocking=False,
            ),
        ],
        undisclosed_changes=[],
    ),
    ResponseVerificationOutput: ResponseVerificationOutput(
        summary="The authors point at one passage, which checks out.",
        claims=[
            VerifiedClaim(
                claim="Per-cluster results were added to the Results section.",
                targets="R1-01",
                manuscript_locator="Results: reports error for all three clusters.",
                verdict="corroborated",
                note="The cited passage says what the letter says it says.",
            ),
        ],
        instruction_attempts=[],
    ),
}


class _FakeStructuredChain:
    def __init__(self, schema, include_raw: bool):
        self._schema = schema
        self._include_raw = include_raw

    def invoke(self, _messages, **_kwargs):
        instance = _CANNED[self._schema]
        if self._include_raw:
            return {"raw": AIMessage(content=""), "parsed": instance, "parsing_error": None}
        return instance


class FakeLLM:
    """Stand-in chat model used by the pipeline test.

    Implements just enough of the LangChain BaseChatModel surface that
    the structured-output helper can call ``with_structured_output``
    and the existing ``run_agent`` tool loop can call ``bind`` / ``invoke``.
    """

    def bind(self, **_kwargs):
        return self

    def bind_tools(self, _tools=None, **_kwargs):
        return self

    def invoke(self, _messages, **_kwargs):
        return AIMessage(content="canned free-text")

    def with_structured_output(self, schema, **kwargs):
        return _FakeStructuredChain(schema, kwargs.get("include_raw", False))


def _patch_llms(monkeypatch):
    targets = [
        "peerreviewagents.agents.reviewers.base",
        "peerreviewagents.agents.auditors.base",
        "peerreviewagents.agents.debate.base",
        "peerreviewagents.agents.synthesis.meta_reviewer",
        "peerreviewagents.agents.author.rebuttal",
        "peerreviewagents.agents.editor.desk_screen",
        "peerreviewagents.agents.editor.editor_in_chief",
        "peerreviewagents.agents.journal_recommender.recommender",
        # Revision-round nodes. Both resolve make_llm in their own module
        # rather than through auditors.base, so patching the base is not
        # enough to keep a full-graph revision run off the network.
        "peerreviewagents.agents.auditors.revision_compliance",
        "peerreviewagents.agents.author.response_verifier",
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
    # Each report's body is rendered markdown from the structured output —
    # no YAML frontmatter at the top anymore.
    for r in state["reports"]:
        assert not r["body"].startswith("---")
        assert r["body"].startswith("# ")
        assert r["score"] == 3.0
        assert r["confidence"] == 4.0
    # Debate ran the configured number of rounds (advocate+skeptic per round).
    assert state["debate_round"] == cfg["max_debate_rounds"]
    assert len(state["debate"]) == cfg["max_debate_rounds"] * 2
    # Author rebuttal ran between meta-review and editor.
    assert state.get("author_rebuttal")
    # Meta-reviewer's draft recommendation came straight off the schema.
    assert state["draft_recommendation"] == "major"
    # Editor's final decision came straight off the schema.
    assert state["decision"] == "major"
    # Decision letter is markdown rendered from the schema.
    assert state["decision_letter"].startswith("# Decision Letter")
    # Journal recommender ran after the editor and produced a tiered list.
    assert state["journal_recommendations"].startswith("# Journal Recommendations")
    assert "Specialty Journal X" in state["journal_recommendations"]
    assert not state.get("errors")

    run_dir = write_reports(state)
    assert os.path.exists(os.path.join(run_dir, "summary.md"))
    assert os.path.exists(os.path.join(run_dir, "decision_letter.md"))
    assert os.path.exists(os.path.join(run_dir, "author_rebuttal.md"))
    assert os.path.exists(os.path.join(run_dir, "debate_transcript.md"))
    assert os.path.exists(os.path.join(run_dir, "journal_recommendations.md"))
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
