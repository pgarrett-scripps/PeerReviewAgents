"""Schema round-trip + structured-helper fallback tests."""

from __future__ import annotations

import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from pydantic import ValidationError

from peerreviewagents.agents.schemas import (
    AuthorRebuttalOutput,
    DebateOutput,
    EditorDecisionOutput,
    MetaReviewOutput,
    RebuttalConcession,
    RebuttalDisagreement,
    ReviewerOutput,
)
from peerreviewagents.agents.utils.agent_utils import RunResult, run_agent
from peerreviewagents.agents.utils.structured import (
    StructuredResult,
    invoke_structured,
)

# ---------- schema construction + render ------------------------------------


def test_reviewer_output_renders_all_sections():
    r = ReviewerOutput(
        score=4,
        confidence=5,
        summary="A solid contribution, clearly argued.",
        strengths=["clear writing"],
        weaknesses=["small N"],
        questions=["what about Y?"],
    )
    md = r.to_markdown(role="Methodology Reviewer")
    assert md.startswith("# Methodology Reviewer")
    assert "## Summary" in md
    assert "## Strengths" in md and "- clear writing" in md
    assert "## Weaknesses" in md and "- small N" in md
    assert "## Questions" in md and "- what about Y?" in md


def test_reviewer_output_rejects_out_of_range():
    with pytest.raises(ValidationError):
        ReviewerOutput(score=7, confidence=3, summary="The design is sound but the evaluation is thin.")
    with pytest.raises(ValidationError):
        ReviewerOutput(score=3, confidence=0, summary="The design is sound but the evaluation is thin.")


def test_debate_output_renders():
    d = DebateOutput(argument="The empirical evidence is strong.",
                     key_points=["replicates prior", "fair baselines"])
    md = d.to_markdown()
    assert "The empirical evidence is strong." in md
    assert "**Key points:**" in md
    assert "- replicates prior" in md


def test_meta_review_renders_recommendation():
    m = MetaReviewOutput(
        draft_recommendation="minor",
        synthesis="Mostly aligned panel.",
        decisive_factors="Methods are sound; presentation needs polish.",
    )
    md = m.to_markdown()
    assert "Draft recommendation:** minor" in md
    assert "## Synthesis" in md and "## Decisive Factors" in md


def test_meta_review_rejects_unknown_verdict():
    with pytest.raises(ValidationError):
        MetaReviewOutput(
            draft_recommendation="maybe",  # type: ignore[arg-type]
            synthesis="x",
            decisive_factors="y",
        )


def test_author_rebuttal_renders_empty_sections():
    r = AuthorRebuttalOutput()
    md = r.to_markdown()
    assert "## Concessions" in md and "(none)" in md
    assert "## Disagreements" in md
    assert "## Load-bearing critiques" in md
    assert "considers all critiques addressable" in md


def test_author_rebuttal_renders_populated():
    r = AuthorRebuttalOutput(
        concessions=[RebuttalConcession(
            reviewer="methodology", critique="missing power analysis",
            proposed_change="add Section 3.2 with power calculation",
        )],
        disagreements=[RebuttalDisagreement(
            reviewer="novelty", critique="duplicates Smith 2024",
            response="our method differs in three ways",
            quoted_section="Section 2.1, paragraph 3",
        )],
        load_bearing_critiques=["scope of generalization"],
    )
    md = r.to_markdown()
    assert "**methodology**" in md and "power analysis" in md
    assert "**novelty**" in md and "Section 2.1" in md
    assert "- scope of generalization" in md


def test_editor_decision_renders_numbered_revisions():
    e = EditorDecisionOutput(
        decision="major",
        summary_of_evaluation="Strong method, weak generalization.",
        required_revisions=["Narrow the claim.", "Add a second cluster."],
        minor_suggestions=["Tighten abstract."],
    )
    md = e.to_markdown()
    assert "# Decision Letter" in md
    assert "**Decision:** major" in md
    assert "1. Narrow the claim." in md
    assert "2. Add a second cluster." in md
    assert "- Tighten abstract." in md


# ---------- structured-output helper paths ----------------------------------


class _StubLLM:
    """LLM stub that returns scripted parsed instances via with_structured_output."""

    def __init__(self, scripted: list):
        self._scripted = list(scripted)
        self.chain: _Chain | None = None

    def with_structured_output(self, _schema, **_kwargs):
        self.chain = _Chain(self._scripted)
        return self.chain


class _Chain:
    def __init__(self, scripted):
        self._scripted = scripted
        self.invocations: list[list] = []

    def invoke(self, messages, **_kwargs):
        self.invocations.append(list(messages))
        return self._scripted.pop(0)


def _ok(instance):
    return {"raw": AIMessage(content=""), "parsed": instance, "parsing_error": None}


def _fail(error: str):
    return {"raw": AIMessage(content="bad"), "parsed": None, "parsing_error": error}


def _cfg(provider: str = "openrouter") -> dict:
    return {"provider": provider, "reasoning_model": "stub"}


def test_invoke_structured_first_try():
    inst = ReviewerOutput(score=3, confidence=3, summary="The design is sound but the evaluation is thin.")
    llm = _StubLLM([_ok(inst)])
    result = invoke_structured(llm, ReviewerOutput, _cfg(), "sys", "user")
    assert isinstance(result, StructuredResult)
    assert result.instance is inst


def test_invoke_structured_retry_then_success():
    inst = ReviewerOutput(score=3, confidence=3, summary="The design is sound but the evaluation is thin.")
    llm = _StubLLM([_fail("bad json"), _ok(inst)])
    result = invoke_structured(llm, ReviewerOutput, _cfg(), "sys", "user")
    assert result.instance is inst


def test_invoke_structured_double_failure_raises():
    llm = _StubLLM([_fail("bad json"), _fail("still bad")])
    with pytest.raises(ValueError, match="validation failed after retry"):
        invoke_structured(llm, ReviewerOutput, _cfg(), "sys", "user")


# ---------- salvaging an unscored review ------------------------------------
#
# A reviewer that leaves `score` out and gives no not_applicable_reason is
# rejected by the abstention validator. Rejecting the *object* used to discard
# the *review* with it — summary, strengths, weaknesses and questions, already
# written and already paid for. Three of eight reviewers went that way on one
# run, and the editor was told the panel had returned nothing on those
# dimensions.


_ABSTAINED = {
    "confidence": 4,
    "summary": "A full review that happens to carry no score.",
    "strengths": ["deposited data"],
    "weaknesses": ["non-standard FDR"],
    "questions": ["which decoy set?"],
}


def _fail_with(raw: AIMessage):
    return {"raw": raw, "parsed": None, "parsing_error": "abstention rejected"}


def _tool_call(args: dict) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {"name": "ReviewerOutput", "args": args, "id": "1", "type": "tool_call"}
        ],
    )


@pytest.mark.parametrize(
    "raw",
    [
        _tool_call(_ABSTAINED),
        # The same object written into content instead of a tool call. Models
        # pick either and the choice is not ours; looking only at tool calls is
        # what let a literature reviewer be discarded after the salvage path
        # already existed.
        AIMessage(content=json.dumps(_ABSTAINED)),
        AIMessage(content="Here you go:\n" + json.dumps(_ABSTAINED) + "\nDone."),
    ],
    ids=["tool_call", "content_json", "content_json_with_prose"],
)
def test_unscored_review_is_kept_not_discarded(raw):
    llm = _StubLLM([_fail_with(raw), _fail_with(raw)])
    result = invoke_structured(llm, ReviewerOutput, _cfg(), "sys", "user")
    assert result.instance.summary == _ABSTAINED["summary"]
    assert result.instance.weaknesses == ["non-standard FDR"]
    # The abstention is published as unexplained rather than dressed up as a
    # considered "nothing here to judge".
    assert result.instance.score is None
    assert "did not say why" in result.instance.not_applicable_reason


def test_truncated_response_still_raises():
    """Half a review is not a review — the fix for that one is the token cap."""
    cut = AIMessage(content='{"confidence": 4, "summary": "The manuscri')
    llm = _StubLLM([_fail_with(cut), _fail_with(cut)])
    with pytest.raises(ValueError, match="validation failed after retry"):
        invoke_structured(llm, ReviewerOutput, _cfg(), "sys", "user")


def test_salvage_does_not_touch_a_scored_review():
    """Rejected for some other reason; not ours to second-guess."""
    scored = dict(_ABSTAINED, score=9)
    llm = _StubLLM([_fail_with(_tool_call(scored)), _fail_with(_tool_call(scored))])
    with pytest.raises(ValueError, match="validation failed after retry"):
        invoke_structured(llm, ReviewerOutput, _cfg(), "sys", "user")


# ---------- the score-repair ask before an unexplained abstention -----------
#
# Observed live: on a long manuscript, half a panel wrote complete, sharp
# reviews and returned them with a null score and no reason. The salvage kept
# the reviews, but the panel mean was then computed over whoever happened to
# comply — and the abstaining bodies argued for LOW scores, so the failure
# moved the mean up. Probed directly, the same model scores its own review
# reliably when the two fields are the whole question, so one targeted repair
# ask runs before the abstention is published as unexplained.


def test_repair_recovers_the_score_the_review_implies():
    from peerreviewagents.agents.utils.structured import _ScoreRepair

    llm = _StubLLM([
        _fail_with(_tool_call(_ABSTAINED)),
        _fail_with(_tool_call(_ABSTAINED)),
        _ok(_ScoreRepair(score=2)),
    ])
    result = invoke_structured(llm, ReviewerOutput, _cfg(), "sys", "user")
    assert result.instance.score == 2
    assert result.instance.summary == _ABSTAINED["summary"]
    assert result.instance.weaknesses == ["non-standard FDR"]
    assert result.instance.not_applicable_reason == ""
    # The repair quotes the review back rather than re-running it: the model
    # is scoring what it already wrote, not writing again.
    ask = llm.chain.invocations[-1][-1].content
    assert _ABSTAINED["summary"] in ask
    assert "non-standard FDR" in ask


def test_repair_accepts_a_reason_instead_of_a_score():
    from peerreviewagents.agents.utils.structured import _ScoreRepair

    reason = "Qualitative study; no statistical claims to judge."
    llm = _StubLLM([
        _fail_with(_tool_call(_ABSTAINED)),
        _fail_with(_tool_call(_ABSTAINED)),
        _ok(_ScoreRepair(not_applicable_reason=reason)),
    ])
    result = invoke_structured(llm, ReviewerOutput, _cfg(), "sys", "user")
    assert result.instance.score is None
    assert result.instance.not_applicable_reason == reason


def test_failed_repair_falls_back_to_the_unexplained_abstention():
    from peerreviewagents.agents.utils.structured import _ScoreRepair

    # The repair abstaining AGAIN with neither field changes nothing: the
    # number is the model's to give, never this layer's to invent.
    llm = _StubLLM([
        _fail_with(_tool_call(_ABSTAINED)),
        _fail_with(_tool_call(_ABSTAINED)),
        _ok(_ScoreRepair()),
    ])
    result = invoke_structured(llm, ReviewerOutput, _cfg(), "sys", "user")
    assert result.instance.score is None
    assert "did not say why" in result.instance.not_applicable_reason


def test_repair_call_error_falls_back_to_the_unexplained_abstention():
    # Queue holds only the two failed reviews; the repair's own invoke hits an
    # empty script and raises. A repair that fails must cost the run nothing.
    llm = _StubLLM([
        _fail_with(_tool_call(_ABSTAINED)),
        _fail_with(_tool_call(_ABSTAINED)),
    ])
    result = invoke_structured(llm, ReviewerOutput, _cfg(), "sys", "user")
    assert result.instance.score is None
    assert "did not say why" in result.instance.not_applicable_reason


def test_reporting_failure_renders_apart_from_a_reasoned_abstention():
    from peerreviewagents.agents.schemas import NO_SCORE_NO_REASON

    salvaged = ReviewerOutput(
        score=None, confidence=3, summary="The design is sound but the evaluation is thin.",
        not_applicable_reason=NO_SCORE_NO_REASON,
    )
    md = salvaged.to_markdown()
    assert "reporting failure" in md.lower()
    assert "Not applicable to this manuscript" not in md

    reasoned = ReviewerOutput(
        score=None, confidence=3, summary="The design is sound but the evaluation is thin.",
        not_applicable_reason="No statistical claims to judge.",
    )
    md2 = reasoned.to_markdown()
    assert "Not applicable to this manuscript" in md2
    assert "reporting failure" not in md2.lower()


# ---------- transient provider-error retries --------------------------------


class _FlakyChain:
    """Chain that raises ``fails`` times, then returns ``result``."""

    def __init__(self, fails: int, result):
        self.remaining = fails
        self.result = result
        self.calls = 0

    def invoke(self, _messages, **_kwargs):
        self.calls += 1
        if self.remaining > 0:
            self.remaining -= 1
            raise RuntimeError("Provider returned error")
        return self.result


class _FlakyLLM:
    def __init__(self, chain):
        self._chain = chain

    def with_structured_output(self, _schema, **_kwargs):
        return self._chain


def test_invoke_structured_retries_transient_provider_error(monkeypatch):
    import peerreviewagents.agents.utils.structured as s
    monkeypatch.setattr(s, "_RETRY_BACKOFF_S", 0)
    inst = ReviewerOutput(score=3, confidence=3, summary="The design is sound but the evaluation is thin.")
    chain = _FlakyChain(fails=2, result=_ok(inst))  # fails twice, succeeds 3rd
    result = invoke_structured(_FlakyLLM(chain), ReviewerOutput, _cfg(), "sys", "user")
    assert result.instance is inst
    assert chain.calls == 3


def test_invoke_structured_provider_error_exhausts_attempts(monkeypatch):
    import peerreviewagents.agents.utils.structured as s
    monkeypatch.setattr(s, "_RETRY_BACKOFF_S", 0)
    chain = _FlakyChain(fails=99, result=_ok(None))  # always fails
    with pytest.raises(RuntimeError, match="Provider returned error"):
        invoke_structured(_FlakyLLM(chain), ReviewerOutput, _cfg(), "sys", "user")
    assert chain.calls == s._MAX_PROVIDER_ATTEMPTS  # capped at 3 tries


# ---------- the retry sees what it is retrying -------------------------------
#
# The correction says "keep the rest of your answer as it was" — satisfiable
# only when the rejected turn is in the retry conversation. It was not: the
# retry used to get the original messages plus the correction alone, so "your
# previous response" pointed at nothing and every retry was a blind full
# regeneration of an answer already written and paid for.


def test_validation_retry_replays_the_rejected_answer():
    inst = ReviewerOutput(score=3, confidence=3, summary="The design is sound but the evaluation is thin.")
    bad = AIMessage(content="a bulleted list where the JSON should be")
    llm = _StubLLM([_fail_with(bad), _ok(inst)])
    result = invoke_structured(llm, ReviewerOutput, _cfg(), "sys", "user")
    assert result.instance is inst
    retry_conversation = llm.chain.invocations[1]
    # The rejected turn sits right before the correction that points at it.
    assert retry_conversation[-2] is bad
    assert "did not produce a valid" in retry_conversation[-1].content


def test_a_rejected_tool_call_is_answered_before_the_correction():
    """Replaying a tool call unanswered is itself an invalid transcript —
    both APIs 400 on a tool_use with no tool_result — so a stub result has to
    sit between the rejected turn and the human correction."""
    inst = ReviewerOutput(score=3, confidence=3, summary="The design is sound but the evaluation is thin.")
    bad = _tool_call({"summary": "no score"})
    llm = _StubLLM([_fail_with(bad), _ok(inst)])
    invoke_structured(llm, ReviewerOutput, _cfg(), "sys", "user")
    retry_conversation = llm.chain.invocations[1]
    answer = retry_conversation[retry_conversation.index(bad) + 1]
    assert isinstance(answer, ToolMessage)
    assert answer.tool_call_id == "1"
    assert isinstance(retry_conversation[-1], HumanMessage)


def test_an_empty_rejected_response_is_not_replayed():
    """Nothing in it for the model to keep, and Anthropic rejects an
    assistant turn with empty content."""
    inst = ReviewerOutput(score=3, confidence=3, summary="The design is sound but the evaluation is thin.")
    llm = _StubLLM([_fail_with(AIMessage(content="")), _ok(inst)])
    invoke_structured(llm, ReviewerOutput, _cfg(), "sys", "user")
    first, retry = llm.chain.invocations
    assert len(retry) == len(first) + 1  # the correction alone was added


# ---------- the discarded tool loop is still billed ---------------------------


def test_short_text_fallback_still_counts_the_tool_loop_cost(monkeypatch):
    """A tool loop whose answer is too short to keep was still run — every
    lookup invoiced — and the fallback used to report only its own cost,
    undercounting the agent by exactly its most expensive calls."""
    import peerreviewagents.agents.utils.structured as s
    monkeypatch.setattr(
        s, "run_agent",
        lambda *a, **k: RunResult(text="Let me verify a few more citations.", cost=0.42),
    )
    inst = ReviewerOutput(score=3, confidence=3, summary="The design is sound but the evaluation is thin.")
    llm = _StubLLM([_ok(inst)])
    result = s.invoke_structured_after_tools(
        llm, ReviewerOutput, _cfg(), "sys", "user", [],
    )
    assert result.instance is inst
    assert result.cost == pytest.approx(0.42)


# ---------- the tool loop's call budget ---------------------------------------


class _Lookup:
    name = "lookup"

    def invoke(self, args):
        return f"- one result for {args.get('query', '')}"


class _ToolHungryModel:
    """Asks for another batch of lookups every turn until the loop refuses."""

    def __init__(self, per_round: int):
        self.per_round = per_round
        self.invocations: list[list] = []
        self._round = 0

    def bind_tools(self, _tools):
        return self

    def invoke(self, messages, **_kwargs):
        self.invocations.append(list(messages))
        last = messages[-1]
        if isinstance(last, HumanMessage) and "research budget" in str(last.content):
            return AIMessage(content="the final audit, from what was gathered")
        self._round += 1
        return AIMessage(content="", tool_calls=[
            {
                "name": "lookup",
                "args": {"query": f"q{self._round}-{j}"},
                "id": f"call-{self._round}-{j}",
                "type": "tool_call",
            }
            for j in range(self.per_round)
        ])


def test_call_budget_cap_answers_every_pending_tool_call():
    """When the cap trips mid-round, the assistant turn asking for that round
    is already in the history. Leaving its calls unanswered made the
    forced-final request a guaranteed 400 ("tool_use without tool_result"),
    and the caller's fallback then reran the agent with no tools — every
    lookup already gathered thrown away. Each id must have a matching
    ToolMessage."""
    model = _ToolHungryModel(per_round=9)
    result = run_agent(model, "sys", "user", [_Lookup()])
    assert result.text == "the final audit, from what was gathered"

    final_history = model.invocations[-1]
    asked = {
        c["id"]
        for m in final_history if isinstance(m, AIMessage)
        for c in (getattr(m, "tool_calls", None) or [])
    }
    answered = {m.tool_call_id for m in final_history if isinstance(m, ToolMessage)}
    assert asked and asked == answered

    # The capped round's calls were answered with the not-executed stub —
    # the budget refuses them, it does not quietly run them.
    stubs = [
        m for m in final_history
        if isinstance(m, ToolMessage) and "not executed" in str(m.content)
    ]
    assert len(stubs) == model.per_round
