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
    invoke_structured_after_tools,
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
        summary_of_evaluation=(
            "The panel agrees the method is sound but the "
            "generalization claim reaches past the evidence shown. The "
            "scope objection stands unanswered, and closing it needs a "
            "narrowed claim, so the verdict is major."
        ),
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
        # Once the script runs out, keep returning its last line. The repair
        # loop asks up to _MAX_REPAIR_ROUNDS times, and a test that scripts two
        # failures is describing a model that keeps failing — not one that runs
        # out of opinions on the third ask. Popping past the end raised
        # IndexError, turning "this input is unrecoverable" into a crash.
        if len(self._scripted) > 1:
            return self._scripted.pop(0)
        return self._scripted[0]


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
    with pytest.raises(ValueError, match="validation failed after 3 repair attempts"):
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


def _wire_tool_call(args: dict) -> AIMessage:
    """OpenAI-compatible wire shape used when normalized calls are absent."""
    return AIMessage(
        content="",
        additional_kwargs={"tool_calls": [{
            "id": "wire-1",
            "type": "function",
            "function": {
                "name": "ReviewerOutput",
                "arguments": json.dumps(args),
            },
        }]},
    )


@pytest.mark.parametrize(
    "raw",
    [
        _tool_call(_ABSTAINED),
        _wire_tool_call(_ABSTAINED),
        # The same object written into content instead of a tool call. Models
        # pick either and the choice is not ours; looking only at tool calls is
        # what let a literature reviewer be discarded after the salvage path
        # already existed.
        AIMessage(content=json.dumps(_ABSTAINED)),
        AIMessage(content="Here you go:\n" + json.dumps(_ABSTAINED) + "\nDone."),
    ],
    ids=["tool_call", "wire_tool_call", "content_json", "content_json_with_prose"],
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
    with pytest.raises(ValueError, match="validation failed after 3 repair attempts"):
        invoke_structured(llm, ReviewerOutput, _cfg(), "sys", "user")


def test_salvage_does_not_touch_a_scored_review():
    """Rejected for some other reason; not ours to second-guess."""
    scored = dict(_ABSTAINED, score=9)
    llm = _StubLLM([_fail_with(_tool_call(scored))] * 4)
    with pytest.raises(ValueError, match="validation failed after 3 repair attempts"):
        invoke_structured(llm, ReviewerOutput, _cfg(), "sys", "user")


def test_validation_error_input_is_recoverable_when_raw_message_lost_payload():
    """LangChain may retain only Pydantic's input_value on failed parsing."""
    try:
        ReviewerOutput(**_ABSTAINED)
    except ValidationError as error:
        failed = {"raw": AIMessage(content=""), "parsed": None, "parsing_error": error}
    llm = _StubLLM([failed] * 5)
    result = invoke_structured(llm, ReviewerOutput, _cfg(), "sys", "user")
    assert result.instance.summary == _ABSTAINED["summary"]
    assert result.instance.score is None
    assert "did not say why" in result.instance.not_applicable_reason


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
        _fail_with(_tool_call(_ABSTAINED)),
        _fail_with(_tool_call(_ABSTAINED)),
        _ok(_ScoreRepair()),
    ])
    result = invoke_structured(llm, ReviewerOutput, _cfg(), "sys", "user")
    assert result.instance.score is None
    assert "did not say why" in result.instance.not_applicable_reason


# ---------- repair only a broken editor summary -----------------------------


_BAD_EDITOR = {
    "decision": "major",
    "summary_of_evaluation": "...",
    "required_revisions": [
        "Report the missing ablation and reconcile it with the central claim.",
        "Narrow the generalization claim to the evaluated datasets.",
    ],
    "minor_suggestions": ["Clarify Figure 2."],
}


def test_editor_summary_is_repaired_without_changing_verdict_or_revisions():
    from peerreviewagents.agents.utils.structured import _EditorSummaryRepair

    good_summary = (
        "The panel found the core method plausible, but the central empirical "
        "claim depends on an ablation that is not reported. That gap and the "
        "unsupported generalization outweigh the otherwise clear presentation, "
        "so a major revision is required before the claim can be assessed."
    )
    bad = _fail_with(_tool_call(_BAD_EDITOR))
    llm = _StubLLM([bad] * 4 + [
        _ok(_EditorSummaryRepair(summary_of_evaluation=good_summary)),
    ])
    result = invoke_structured(llm, EditorDecisionOutput, _cfg(), "sys", "panel evidence")
    assert result.instance.decision == "major"
    assert result.instance.required_revisions == _BAD_EDITOR["required_revisions"]
    assert result.instance.summary_of_evaluation == good_summary
    assert not result.warnings


def test_editor_verdict_survives_when_summary_micro_repair_also_fails():
    bad = _fail_with(_tool_call(_BAD_EDITOR))
    llm = _StubLLM([bad])
    result = invoke_structured(llm, EditorDecisionOutput, _cfg(), "sys", "panel evidence")
    assert result.instance.decision == "major"
    assert result.instance.required_revisions == _BAD_EDITOR["required_revisions"]
    assert "original narrative synthesis was unusable" in result.instance.summary_of_evaluation
    assert result.warnings


def test_repair_call_error_falls_back_to_the_unexplained_abstention():
    # Queue holds only the two failed reviews; the repair's own invoke hits an
    # empty script and raises. A repair that fails must cost the run nothing.
    llm = _StubLLM([
        _fail_with(_tool_call(_ABSTAINED)),
        _fail_with(_tool_call(_ABSTAINED)),
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


def test_short_text_retry_still_counts_the_first_prose_cost(monkeypatch):
    """A tool loop whose answer is too short to keep was still run — every
    lookup invoiced — and the fallback used to report only its own cost,
    undercounting the agent by exactly its most expensive calls."""
    import peerreviewagents.agents.utils.structured as s
    calls = 0

    def _prose(*_a, **_k):
        nonlocal calls
        calls += 1
        if calls == 1:
            return RunResult(text="Let me verify a few more citations.", cost=0.42)
        return RunResult(text=_MARKDOWN_REVIEW, cost=0.21)

    monkeypatch.setattr(s, "run_agent", _prose)
    llm = _StubLLM([_fail("a schema must never be requested")])
    result = s.invoke_structured_after_tools(
        llm, ReviewerOutput, _cfg(), "sys", "user", [],
    )
    assert result.instance.score == 4
    assert result.raw_text == _MARKDOWN_REVIEW.strip()
    assert result.cost == pytest.approx(0.63)
    assert llm.chain is None


# ---------- the tool loop's call budget ---------------------------------------


class _Lookup:
    name = "lookup"

    def invoke(self, args):
        return f"- one result for {args.get('query', '')}"


class _EmptyResponseModel:
    def invoke(self, _messages):
        return AIMessage(
            content="",
            additional_kwargs={"openrouter_reasoning": "hidden work"},
            response_metadata={"finish_reason": "length", "model_name": "deepseek/test"},
            usage_metadata={
                "input_tokens": 100,
                "output_tokens": 500,
                "total_tokens": 600,
                "output_token_details": {"reasoning": 500},
            },
        )


def test_run_agent_raises_diagnostic_error_instead_of_returning_empty_review():
    from peerreviewagents.agents.utils.agent_utils import EmptyModelResponse

    with pytest.raises(EmptyModelResponse) as caught:
        run_agent(_EmptyResponseModel(), "sys", "user")
    message = str(caught.value)
    assert "finish_reason=length" in message
    assert "reasoning_tokens=500" in message
    assert "reasoning_chars=11" in message
    assert "hidden work" not in message


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


# ---------------------------------------------------------------------------
# The repair loop asks more than once.
#
# The failure it exists for is stochastic, not deterministic: the same
# manuscript on the same model lost data_analysis and methodology on one run
# and kept all eight reviewers on the next. Meeting a coin flip with a single
# re-ask leaves a quarter of the bad flips unrecovered, and each unrecovered
# one is a verdict decided by seven reviewers with no note of which is absent.
# ---------------------------------------------------------------------------


def test_a_reviewer_recovered_on_the_third_ask_is_not_lost():
    inst = ReviewerOutput(
        score=3, confidence=3,
        summary="The evaluation is thin but the design is sound.",
    )
    llm = _StubLLM([
        _fail("bad json"),
        _fail("still bad"),
        _fail("bad again"),
        _ok(inst),
    ])
    result = invoke_structured(llm, ReviewerOutput, _cfg(), "sys", "user")
    assert result.instance is inst


def test_each_repair_round_quotes_the_latest_error():
    """The ask names what was wrong *this* time. A round that replays the
    first rejection asks the model to fix something it already changed."""
    inst = ReviewerOutput(
        score=3, confidence=3,
        summary="The evaluation is thin but the design is sound.",
    )
    llm = _StubLLM([_fail("first problem"), _fail("second problem"), _ok(inst)])
    invoke_structured(llm, ReviewerOutput, _cfg(), "sys", "user")

    asks = [
        m[-1].content for m in llm.chain.invocations[1:]
        if "did not produce a valid" in str(m[-1].content)
    ]
    assert "first problem" in asks[0]
    assert "second problem" in asks[1]


def test_the_repair_budget_is_bounded():
    """A model that cannot satisfy the schema must not be asked forever."""
    llm = _StubLLM([_fail("hopeless")])
    with pytest.raises(ValueError, match="after 3 repair attempts"):
        invoke_structured(llm, ReviewerOutput, _cfg(), "sys", "user")
    # initial call + 3 repair rounds, and nothing more
    assert len(llm.chain.invocations) == 4


# ---------------------------------------------------------------------------
# Last resort: drop the schema, ask for the review, extract it.
#
# Everything before this asks the same failing question again. This asks a
# different one, because the difficulty is the formatting contract and not the
# manuscript. The alternative is what it replaces: the reviewer is dropped and
# the paper is decided by seven.
# ---------------------------------------------------------------------------

# Long enough to clear MIN_AGENT_TEXT_CHARS, which is the point of that floor:
# a short answer is a failed call, not a terse review.
_PROSE = (
    "The quantification rests on a ratio of two measured intensities with no "
    "stated error model. The headline claim that 378 proteins have the "
    "substituted form as their dominant product is not established: a ratio of "
    "two noisy intensities exceeding one is not evidence the underlying ratio "
    "exceeds one. Several p-values are reported without naming the test, the n, "
    "or the correction applied, which makes them uninterpretable as written. "
    "The tissue-specificity comparison does not state its unit of replication, "
    "so it is unclear whether n counts patients or peptides. The degradation "
    "correlation is computed from intensities that also enter the ratio, so the "
    "two quantities are not independent measurements of the same thing."
)


class _ProseLLM(_StubLLM):
    """Fails the schema every time; answers in prose when asked for prose."""

    def __init__(self, scripted, prose=_PROSE):
        super().__init__(scripted)
        self._prose = prose

    def invoke(self, messages, **_kwargs):
        return AIMessage(content=self._prose)


def test_a_reviewer_that_cannot_fill_the_schema_is_recovered_from_prose():
    inst = ReviewerOutput(
        score=2, confidence=4,
        summary="The quantification rests on a ratio with no stated error model.",
    )
    llm = _ProseLLM([_fail("no score")] * 4 + [_ok(inst)])
    result = invoke_structured(llm, ReviewerOutput, _cfg(), "sys", "user")
    assert result.instance is inst


def test_prose_too_thin_to_be_a_review_is_refused():
    """The floor exists so an empty answer cannot become a fabricated verdict —
    the failure that once put a made-up 1/5 score into a published panel."""
    llm = _ProseLLM([_fail("no score")] * 5, prose="ok")
    with pytest.raises(ValueError):
        invoke_structured(llm, ReviewerOutput, _cfg(), "sys", "user")


def test_the_extraction_sees_only_the_prose():
    """It must not be handed the manuscript again: the job is to convert what
    the reviewer wrote, not to write a review of its own."""
    inst = ReviewerOutput(
        score=2, confidence=4,
        summary="The quantification rests on a ratio with no stated error model.",
    )
    llm = _ProseLLM([_fail("no score")] * 4 + [_ok(inst)])
    invoke_structured(llm, ReviewerOutput, _cfg(), "sys", "MANUSCRIPT-BODY")
    extraction = llm.chain.invocations[-1]
    assert len(extraction) == 2
    assert _PROSE in str(extraction[-1].content)
    assert "MANUSCRIPT-BODY" not in str(extraction)


# ---------------------------------------------------------------------------
# Markdown-first reviewer output.
# ---------------------------------------------------------------------------


class _MarkdownOnlyLLM:
    """Produces a complete review and refuses every structured-output call."""

    def __init__(self, text: str):
        self.text = text
        self.structured_calls = 0

    def invoke(self, _messages, **_kwargs):
        return AIMessage(content=self.text)

    def with_structured_output(self, *_args, **_kwargs):
        self.structured_calls += 1
        raise AssertionError("explicit Markdown metadata must not need a schema")


_MARKDOWN_REVIEW = """\
**Score:** 4/5
**Confidence:** 3/5

## Overall Assessment
The method is useful and the main conclusion follows from the reported results,
although one robustness check should be added before publication. The scope is
well defined and the manuscript is otherwise clear enough to reproduce.

## Strengths
- The workflow is described with concrete parameters and released code.
- The example exercises the complete analysis rather than a toy fragment.

## Weaknesses
- The sensitivity of the result to the threshold is not reported.
- Runtime is discussed qualitatively but not measured on the example input.

## Questions for the Authors
- Does the conclusion change under the neighboring threshold values?

The requested robustness check is small and does not change the underlying
method, so a minor revision is proportionate. This final paragraph makes the
review intentionally long enough to represent an actual model response.
"""


def test_reviewer_markdown_is_the_durable_output_without_schema_extraction():
    llm = _MarkdownOnlyLLM(_MARKDOWN_REVIEW)
    result = invoke_structured_after_tools(
        llm, ReviewerOutput, _cfg(), "sys", "user", [],
    )

    assert result.raw_text == _MARKDOWN_REVIEW.strip()
    assert result.instance.score == 4
    assert result.instance.confidence == 3
    assert result.instance.weaknesses[0].startswith("The sensitivity")
    assert result.score_source == "explicit"
    assert llm.structured_calls == 0


def test_placeholder_question_does_not_discard_complete_markdown_review():
    review = _MARKDOWN_REVIEW.replace(
        "- Does the conclusion change under the neighboring threshold values?",
        "- None.",
    )
    llm = _MarkdownOnlyLLM(review)
    result = invoke_structured_after_tools(
        llm, ReviewerOutput, _cfg(), "sys", "user", [],
    )

    assert result.raw_text == review.strip()
    assert result.instance.score == 4
    assert result.instance.questions == []
    assert llm.structured_calls == 0


def test_reviewer_score_can_be_natural_prose_instead_of_formatted_fields():
    prose = (
        "I recommend major revision, because the central comparison omits a "
        "necessary control. My confidence is 4 out of 5 because the relevant "
        "methods and results are explicit. The implementation itself appears "
        "sound, and the released workflow is useful, but the omitted control "
        "means the central performance claim is not established. Adding that "
        "single comparison would directly resolve the concern. The manuscript "
        "should also identify the software versions and random seed used for "
        "the reported example. These comments are based on the methods and "
        "results as supplied, rather than on assumptions about undocumented "
        "experiments. No special headings or machine-readable wrapper are "
        "needed for this review to remain usable downstream."
    )
    llm = _MarkdownOnlyLLM(prose)
    result = invoke_structured_after_tools(
        llm, ReviewerOutput, _cfg(), "sys", "user", [],
    )

    assert result.instance.score == 3
    assert result.instance.confidence == 4
    assert result.raw_text == prose
    assert result.score_source == "explicit"
    assert llm.structured_calls == 0


def test_reviewer_prompt_echo_is_rejected_and_retried_as_plain_markdown(monkeypatch):
    """A long prompt dump is not a substantive review merely because it is long."""
    import peerreviewagents.agents.utils.structured as s

    prompt = "Instructions\n=== MANUSCRIPT ===\nA manuscript body\n=== END MANUSCRIPT ==="
    echoed = (
        "I will reproduce the request.\n=== MANUSCRIPT ===\nA manuscript body\n"
        "This copied material continues for long enough to pass the old length floor. " * 10
    )
    calls = 0

    def _prose(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return RunResult(text=echoed if calls == 1 else _MARKDOWN_REVIEW, cost=0.1)

    monkeypatch.setattr(s, "run_agent", _prose)
    result = s.invoke_structured_after_tools(
        llm=None,
        schema=ReviewerOutput,
        config={"markdown_attempts": 2},
        system_prompt="sys",
        user_prompt=prompt,
        tools=[],
    )

    assert calls == 2
    assert result.raw_text == _MARKDOWN_REVIEW.strip()
    assert result.instance.score == 4
    assert any("echoed internal prompt boundary" in warning for warning in result.warnings)


def test_prompt_echo_from_cached_prefix_is_also_rejected(monkeypatch):
    """Cached manuscript boundaries are input even when absent from the role prompt."""
    import peerreviewagents.agents.utils.structured as s

    calls = 0

    def _prose(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return RunResult(
                text="=== MANUSCRIPT ===\n" + "copied manuscript material " * 20,
                cost=0.1,
            )
        return RunResult(text=_MARKDOWN_REVIEW, cost=0.1)

    monkeypatch.setattr(s, "run_agent", _prose)
    result = s.invoke_markdown(
        llm=None,
        config={"markdown_attempts": 2},
        system_prompt="sys",
        user_prompt="Write the citation audit.",
        cached_prefix="=== MANUSCRIPT ===\nprivate paper",
    )

    assert calls == 2
    assert result.text == _MARKDOWN_REVIEW.strip()
    assert any("echoed internal prompt boundary" in warning for warning in result.warnings)
