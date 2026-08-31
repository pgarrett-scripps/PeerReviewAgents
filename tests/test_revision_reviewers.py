"""Tests for what a specialist reviewer sees in a revision round: nothing.

The panel is blind to the round. A reviewer reads the manuscript in front of
it and returns a ``ReviewerOutput``, in round 3 exactly as in round 1 — no
prior report, no "what changed" block, no knowledge that a previous round
exists at all.

That is not a simplification, it is the fix for a specific failure. Reviewers
used to be shown their own prior report and a section diff and asked to rule
on a revision, which on a byte-identical resubmission produced a novelty
reviewer raising 3 → 5 "because the revision successfully addresses the
concerns" against a manuscript nobody had touched. Telling a panel it is
looking at a revision creates the incentive to find progress; every guard
that path carried existed to police a psychology the framing itself created.

The one thing that still reaches a reviewer from outside the manuscript is
the response verifier's pointer block, and it survives precisely because it
says nothing about rounds. The tests below hold it to that.

The fake-LLM harness comes from test_pipeline; the recording variant here
keeps the prompts so the blinding can be asserted over the real rendered
text rather than by reading the source.
"""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage
from test_pipeline import _CANNED, FakeLLM

from peerreviewagents import rounds
from peerreviewagents.agents.reviewers import contribution_context, scientific_validity
from peerreviewagents.default_config import get_config

# --- fake LLM --------------------------------------------------------------

_CALL_COST = 0.01


class _RecordingChain:
    def __init__(self, llm: "_RecordingLLM", schema, include_raw: bool):
        self._llm = llm
        self._schema = schema
        self._include_raw = include_raw

    def invoke(self, messages, **_kwargs):
        self._llm.prompts.append(_flatten(messages))
        self._llm.schemas.append(self._schema)
        if self._llm.fail:
            raise RuntimeError("provider exploded")
        instance = _CANNED[self._schema]
        if not self._include_raw:
            return instance
        raw = AIMessage(
            content="", response_metadata={"token_usage": {"cost": _CALL_COST}}
        )
        return {"raw": raw, "parsed": instance, "parsing_error": None}


class _RecordingLLM(FakeLLM):
    """FakeLLM that remembers every prompt and schema it was asked for."""

    def __init__(self, *, fail: bool = False):
        self.prompts: list[str] = []
        self.schemas: list[type] = []
        self.fail = fail

    def with_structured_output(self, schema, **kwargs):
        return _RecordingChain(self, schema, kwargs.get("include_raw", False))

    def invoke(self, messages, **kwargs):
        self.prompts.append(_flatten(messages))
        if self.fail:
            raise RuntimeError("provider exploded")
        return super().invoke(messages, **kwargs)


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


def _patch(monkeypatch, llm, module=scientific_validity):
    monkeypatch.setattr(
        "peerreviewagents.agents.reviewers.base.make_llm",
        lambda config, **_kwargs: llm,
    )
    return module


# --- fixtures --------------------------------------------------------------


@pytest.fixture
def prior_round():
    """A round-1 record with two reviewers and a full set of asks."""
    return rounds.build_from_state(
        {
            "manuscript_title": "A Lightweight Method",
            "config": {},
            "decision": "major",
            "required_revisions": ["Report per-cluster results."],
            "reports": [
                {
                    "reviewer": "scientific_validity",
                    "score": 3,
                    "confidence": 4,
                    "weaknesses": ["Only a single production cluster is used."],
                    "questions": ["How were baselines tuned?"],
                    "body": "",
                },
                {
                    "reviewer": "data_analysis",
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
        "reports": [],
        "errors": [],
        "total_cost": 0.0,
    }
    state.update(over)
    return state


def _prompt(monkeypatch, state, module=scientific_validity) -> str:
    llm = _RecordingLLM()
    _patch(monkeypatch, llm, module=module)
    module.node(state)
    return llm.prompts[0]


# --- one schema, one path --------------------------------------------------


def test_a_revision_round_still_emits_the_same_markdown_contract(monkeypatch, prior_round):
    llm = _RecordingLLM()
    _patch(monkeypatch, llm)
    out = scientific_validity.node(_state(prior_round))

    assert llm.schemas == []
    report = out["reports"][0]
    assert report["reviewer"] == "scientific_validity"
    assert report["body"].startswith("# Scientific Validity")
    assert "Revision Review" not in report["body"]
    assert not out.get("errors")


def test_the_report_shape_is_identical_in_both_rounds(monkeypatch, prior_round):
    """Downstream reads reports the same way whatever round produced them."""
    llm = _RecordingLLM()
    _patch(monkeypatch, llm)
    first = scientific_validity.node(_state(prior_round=None))["reports"][0]
    second = scientific_validity.node(_state(prior_round))["reports"][0]

    assert set(first) == set(second)
    assert first == second


def test_tool_using_reviewer_is_blind_too(monkeypatch, prior_round):
    """The research-tool loop takes the same single path."""
    llm = _RecordingLLM()
    _patch(monkeypatch, llm, module=contribution_context)
    out = contribution_context.node(_state(prior_round))

    assert llm.schemas == []
    assert "Revision Review" not in out["reports"][0]["body"]
    assert not out.get("errors")


def test_a_failed_call_is_an_error_entry_not_an_exception(monkeypatch, prior_round):
    llm = _RecordingLLM(fail=True)
    _patch(monkeypatch, llm)
    out = scientific_validity.node(_state(prior_round))

    assert not out.get("reports")
    assert "Markdown generation failed after 3 prose attempts" in out["errors"][0]


# --- the blinding ----------------------------------------------------------
#
# The load-bearing property of the redesign, asserted over the actual rendered
# prompt rather than by reading make_reviewer_node.


def test_the_revision_prompt_is_identical_to_the_first_round_prompt(
    monkeypatch, prior_round
):
    """The strongest form of the invariant: the presence of a prior round
    changes not one byte of what a reviewer is asked."""
    blind = _prompt(monkeypatch, _state(prior_round))
    fresh = _prompt(monkeypatch, _state(prior_round=None))
    assert blind == fresh


def test_no_prior_report_reaches_the_reviewer(monkeypatch, prior_round):
    prompt = _prompt(monkeypatch, _state(prior_round))

    assert "scientific_validity-1" not in prompt
    assert "Only a single production cluster" not in prompt
    assert "How were baselines tuned?" not in prompt
    # And still nothing from any other reviewer, which was true before and
    # must not become false now that there is one code path.
    assert "data_analysis-1" not in prompt
    assert "random seed" not in prompt.lower()


def test_no_round_framing_reaches_the_reviewer(monkeypatch, prior_round):
    prompt = _prompt(monkeypatch, _state(prior_round)).lower()

    for phrase in (
        "previous round",
        "prior round",
        "revised draft",
        "revised manuscript",
        "re-review",
        "you reviewed",
        "round 1",
        "resubmi",
        "what changed since",
        "prior_score",
        "prior_points",
    ):
        assert phrase not in prompt, phrase


def test_the_score_scale_is_the_only_place_revision_appears(monkeypatch, prior_round):
    """"3=major revision, 4=minor revision" is the round-1 scoring scale and
    says nothing about which round this is — it is worded identically on a
    first submission. Pinned so a future edit cannot smuggle round framing in
    under a word the blanket check has to allow."""
    prompt = _prompt(monkeypatch, _state(prior_round))
    occurrences = [
        line for line in prompt.splitlines() if "revision" in line.lower()
    ]
    assert occurrences
    assert all(
        "major revision" in line or "minor revision" in line for line in occurrences
    ), occurrences


def test_no_diff_block_reaches_the_reviewer(monkeypatch, prior_round):
    prompt = _prompt(monkeypatch, _state(prior_round))
    assert "What changed since the previous draft" not in prompt
    assert "Unchanged sections" not in prompt
    assert "substantially rewritten" not in prompt


# --- the one channel that survives -----------------------------------------


def test_verified_claims_reach_the_reviewer_framed_as_pointers(
    monkeypatch, prior_round
):
    prompt = _prompt(monkeypatch, _state(
        prior_round,
        verified_claims_block=(
            "## Passages the authors have asked the panel to read\n\n"
            "- The results cover all three datasets"
        ),
    ))

    assert "The results cover all three datasets" in prompt
    assert "interested party" in prompt
    assert "The manuscript is the evidence; their account of it never is." in prompt


def test_the_pointer_block_does_not_reintroduce_round_framing(
    monkeypatch, prior_round
):
    """This block is the only thing a blind reviewer sees that the authors
    touched. Its handling note must not undo the blinding it sits inside."""
    marker = "## Passages"
    prompt = _prompt(monkeypatch, _state(
        prior_round,
        verified_claims_block=f"{marker}\n\n- The seed is stated in §2.1",
    ))
    pointers = prompt[prompt.index(marker):].lower()

    for phrase in ("previous round", "last round", "revision", "resubmi", "revised"):
        assert phrase not in pointers, phrase


def test_no_letter_means_no_pointer_block(monkeypatch, prior_round):
    """A first round has no letter by construction, and a revision round
    without one must render the same prompt as one that never had a letter."""
    with_empty = _prompt(monkeypatch, _state(prior_round, verified_claims_block=""))
    without = _prompt(monkeypatch, _state(prior_round))
    assert with_empty == without
    assert "pointer, not a finding" not in with_empty
