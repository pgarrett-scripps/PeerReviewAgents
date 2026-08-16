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
    GapFinding,
    JournalRecommendationsOutput,
    JournalSuggestion,
    MetaReviewOutput,
    PanelGapOutput,
    ResponseVerificationOutput,
    ReviewerOutput,
    RevisionComplianceOutput,
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
    PanelGapOutput: PanelGapOutput(
        findings=[
            GapFinding(
                finding=(
                    "No reviewer examined whether the held-out split was "
                    "drawn before or after feature selection."
                ),
                belongs_to="data_analysis",
                manuscript_evidence="Section 3.2, 'we select the top 200 features and then split 80/20'",
                kind="gap",
                why_it_matters=(
                    "Selection before splitting leaks the test set into the "
                    "model, so the reported accuracy is not held out."
                ),
                severity="HARD",
            )
        ],
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
        summary_of_evaluation=(
            "The panel agrees the methodology is sound and the measurements "
            "carefully made, but the generalization claim reaches past the "
            "single cluster the experiments cover. The skeptic's objection on "
            "scope stands unanswered, and closing it needs either a narrowed "
            "claim or a second cluster, so the verdict is major."
        ),
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
    # There is no revision-specific reviewer schema: the panel is blind to the
    # round and returns ReviewerOutput above, in round 3 exactly as in round 1.
    #
    # The compliance default describes an honest revision: one ask carried out,
    # one not. Its `addressed` evidence quotes sample_manuscript.md verbatim
    # because the auditor's quotes are verified against the manuscript in code
    # — a plausible-sounding paraphrase here would be demoted to
    # `unsubstantiated`, which is exactly what that check is for. Tests that
    # want the demotion override this entry with monkeypatch.setitem.
    RevisionComplianceOutput: RevisionComplianceOutput(
        summary="One of two required revisions is carried out; the other is not.",
        findings=[
            ComplianceFinding(
                id="R1-01",
                status="addressed",
                manuscript_evidence=(
                    'The results now cover every dataset: "WidgetNet achieves '
                    'lower error on all three datasets (p < 0.05)."'
                ),
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

    def invoke(self, messages, **_kwargs):
        prompt = "\n".join(str(getattr(m, "content", "")) for m in messages)
        if "verification officer" in prompt:
            return AIMessage(content=_CANNED[ResponseVerificationOutput].to_markdown())
        if "revision-compliance auditor" in prompt:
            return AIMessage(content=_CANNED[RevisionComplianceOutput].to_markdown())
        if "DESK DECISION" in prompt:
            canned = _CANNED[DeskScreenOutput]
            decision = "reject" if canned.desk_reject else "proceed"
            return AIMessage(content=(
                f"DESK DECISION: {decision}\n\n{canned.rationale} "
                + " ".join(canned.reasons)
            ))
        if "specialist reviewer" in prompt or "SCORE: <1-5" in prompt:
            return AIMessage(content=(
                "SCORE: 3\nCONFIDENCE: 4\n\n## Assessment\nThe paper proposes "
                "a useful method, but the evidence does not yet establish the broad "
                "generalization claim. The implementation is described clearly and "
                "the main comparison is relevant, although it covers only one cluster.\n\n"
                "## Strengths\n- Clear motivation.\n- Simple approach.\n\n## Weaknesses\n"
                "- Single cluster limits generalization.\n- The broad claim outruns "
                "the evidence shown.\n\n## Questions\n- How were the baselines tuned?\n\n"
                "A broader evaluation or a narrower claim would address the central "
                "concern without requiring a different method. The remaining reporting "
                "issues are straightforward to correct in revision."
            ))
        if "compliance auditor" in prompt:
            return AIMessage(content=(
                "The manuscript documents the main workflow but omits several details "
                "needed for exact repetition. [HARD, missing] Random seed: training is "
                "described without a seed or seed-averaging statement. [SOFT, missing] "
                "Code availability: no availability statement is supplied. The dataset "
                "and principal analysis category were checked; no accept or reject "
                "recommendation is made by this factual audit."
            ))
        if "Editor-in-Chief" in prompt:
            return AIMessage(content=_CANNED[EditorDecisionOutput].to_markdown())
        if "editorial debate" in prompt or "Make your argument" in prompt:
            return AIMessage(content=(
                "The contribution is incremental but the empirical signal is clean. "
                "The strongest concern is the unsupported breadth of the generalization "
                "claim; the implementation and comparisons otherwise provide a useful "
                "basis for revision. The other side should distinguish a fixable scope "
                "problem from a fundamental defect in the method."
            ))
        if "which venues to submit" in prompt:
            return AIMessage(content=(
                "# Journal Recommendations\n\n## After revision\n- **Specialty "
                "Journal X** — direct topic fit and realistic after the claim is "
                "narrowed.\n\n## Alternatives\n- **arXiv cs.LG** — immediate preprint "
                "dissemination while the additional comparison is prepared."
            ))
        if "what they missed" in prompt:
            return AIMessage(content=_CANNED[PanelGapOutput].to_markdown())
        if "Area Chair" in prompt:
            return AIMessage(content=_CANNED[MetaReviewOutput].to_markdown())
        if "author of the manuscript responding" in prompt:
            return AIMessage(content=_CANNED[AuthorRebuttalOutput].to_markdown())
        return AIMessage(content="canned free-text")

    def with_structured_output(self, schema, **kwargs):
        return _FakeStructuredChain(schema, kwargs.get("include_raw", False))


def _patch_llms(monkeypatch):
    targets = [
        "peerreviewagents.agents.reviewers.base",
        "peerreviewagents.agents.auditors.base",
        "peerreviewagents.agents.debate.base",
        "peerreviewagents.agents.synthesis.gap_finder",
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
    # There is no lossy intermediate synthesis or simulated author voice.
    assert not state.get("author_rebuttal")
    assert not state.get("meta_review")
    # Editor's final decision came straight off the schema.
    assert state["decision"] == "major"
    # Decision letter is markdown rendered from the schema.
    assert state["decision_letter"].startswith("# Decision Letter")
    # Journal recommender ran after the editor and produced a tiered list.
    assert state["journal_recommendations"].startswith("# Journal Recommendations")
    assert "Specialty Journal X" in state["journal_recommendations"]
    assert state["publication_ready"] is True
    assert state["run_status"] == "publishable"
    assert not state.get("errors")

    run_dir = write_reports(state)
    assert os.path.exists(os.path.join(run_dir, "summary.md"))
    assert os.path.exists(os.path.join(run_dir, "decision_letter.md"))
    assert os.path.exists(os.path.join(run_dir, "debate_transcript.md"))
    assert os.path.exists(os.path.join(run_dir, "journal_recommendations.md"))
    # No citations file should be produced — that pipeline was removed.
    assert not any(
        f.startswith("citations_") for f in os.listdir(run_dir)
    )


def test_one_missing_specialist_proceeds_only_with_opted_in_quorum(monkeypatch, tmp_path):
    """A partial verdict remains available, but is not the default contract."""
    _patch_llms(monkeypatch)
    import peerreviewagents.graph.review_graph as review_graph_mod

    real_nodes = review_graph_mod.get_reviewer_nodes()

    def failed(_state):
        return {"errors": ["methodology reviewer failed: provider down"]}

    monkeypatch.setattr(
        review_graph_mod,
        "get_reviewer_nodes",
        lambda: [
            (name, failed if name == "methodology" else node)
            for name, node in real_nodes
        ],
    )

    state = PeerReviewGraph(get_config(
        output_dir=str(tmp_path), panel_quorum_fraction=0.75,
    )).review(SAMPLE)

    assert state["panel_complete"] is True
    assert state["panel_degraded"] is True
    assert len(state["reports"]) == len(REVIEWER_NAMES) - 1
    assert state["decision"] == "major"
    assert state.get("debate")
    assert state["publication_ready"] is False
    assert state["run_status"] == "degraded"
    assert any("degraded panel" in error for error in state["errors"])


def test_one_missing_specialist_fails_closed_by_default(monkeypatch, tmp_path):
    _patch_llms(monkeypatch)
    import peerreviewagents.graph.review_graph as review_graph_mod

    real_nodes = review_graph_mod.get_reviewer_nodes()

    def failed(_state):
        return {"errors": ["methodology reviewer failed: provider down"]}

    monkeypatch.setattr(
        review_graph_mod,
        "get_reviewer_nodes",
        lambda: [
            (name, failed if name == "methodology" else node)
            for name, node in real_nodes
        ],
    )

    state = PeerReviewGraph(get_config(output_dir=str(tmp_path))).review(SAMPLE)

    assert state["panel_complete"] is False
    assert state["panel_degraded"] is False
    assert "decision" not in state
    assert not state.get("debate")
    assert state["publication_ready"] is False
    assert state["run_status"] == "panel_incomplete"
    assert any("incomplete panel" in error for error in state["errors"])


def test_below_quorum_still_stops_before_synthesis(monkeypatch, tmp_path):
    """The quorum is a bounded degradation policy, not permission for a stub panel."""
    _patch_llms(monkeypatch)
    import peerreviewagents.graph.review_graph as review_graph_mod

    real_nodes = review_graph_mod.get_reviewer_nodes()
    failed_names = {"methodology", "rigor", "data_analysis"}

    def failed(_state):
        return {"errors": ["reviewer failed: provider down"]}

    monkeypatch.setattr(
        review_graph_mod,
        "get_reviewer_nodes",
        lambda: [
            (name, failed if name in failed_names else node)
            for name, node in real_nodes
        ],
    )

    state = PeerReviewGraph(get_config(output_dir=str(tmp_path))).review(SAMPLE)
    assert state["panel_complete"] is False
    assert len(state["reports"]) == len(REVIEWER_NAMES) - len(failed_names)
    assert "decision" not in state
    assert any("incomplete panel" in error for error in state["errors"])


def test_ingest_sections():
    from peerreviewagents.ingest.loader import load_manuscript

    title, md, sections = load_manuscript(SAMPLE)
    assert "Widget" in title
    assert "methods" in sections
    assert "results" in sections
