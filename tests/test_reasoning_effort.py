"""Which agents think, and who gets to decide that.

Adaptive thinking is billed at OUTPUT rates and then discarded — it never
reaches a file. On C-01, 74% of the 96,403 output tokens billed produced no
text in any report, and four agents were running with thinking on.

Two questions this pins: which agents need it, and whether the answer is
reachable from configuration.
"""

from __future__ import annotations

import inspect

from peerreviewagents.agents.author import rebuttal
from peerreviewagents.agents.editor import editor_in_chief
from peerreviewagents.agents.journal_recommender import recommender
from peerreviewagents.agents.synthesis import meta_reviewer
from peerreviewagents.runtime.providers import make_chat_model


def calls_with_effort(module) -> bool:
    return 'reasoning_effort="' in inspect.getsource(module)


def test_only_the_editor_thinks_by_default():
    """One agent issues the verdict, and it is the one that sees everything —
    the panel, the debate, the meta-review, the audits and the rebuttal. That
    is the call worth deliberating over.

    The area chair used to think too. It synthesises rather than decides, and
    the editor re-reads its output downstream, so the deliberation was being
    paid for twice at the most expensive rate in the run."""
    assert calls_with_effort(editor_in_chief)
    assert not calls_with_effort(meta_reviewer)


def test_the_agents_that_decide_nothing_do_not():
    """The rebuttal argues the authors' side for the editor to weigh — it
    informs the verdict without setting it. The scout suggests venues, and
    nothing downstream reads it. Both were paying for discarded thinking
    tokens at the synthesis tier's output rate."""
    assert not calls_with_effort(rebuttal)
    assert not calls_with_effort(recommender)


def test_config_can_switch_thinking_off_entirely():
    """An absent `effort` means "unset" and falls through to the call-site
    default, so without an explicit off-spelling there was no way to say "do
    not think" from a config file — only which level to think at."""
    cfg = {
        "provider": "anthropic",
        "reasoning_model": "claude-opus-5",
        "agent_models": {"editor": {"model": "claude-opus-5", "effort": "off"}},
    }
    llm = make_chat_model(cfg, agent="editor", default_tag="synthesis",
                          reasoning_effort="medium")
    assert not getattr(llm, "thinking", None)


def test_an_empty_effort_string_is_unset_not_off():
    """`effort = ""` is a key someone left blank, not a decision to disable
    deliberation on the agent that issues the verdict."""
    cfg = {
        "provider": "anthropic",
        "reasoning_model": "claude-opus-5",
        "agent_models": {"editor": {"model": "claude-opus-5", "effort": ""}},
    }
    llm = make_chat_model(cfg, agent="editor", default_tag="synthesis",
                          reasoning_effort="medium")
    assert llm.effort == "medium"


def test_config_can_override_the_call_sites_effort():
    """It could not before: the call-site value won, so an `effort` key in
    peerreview.toml was silently inert and tuning the editor meant editing
    Python. Thinking is a cost knob and has to be reachable from config."""
    cfg = {
        "provider": "anthropic",
        "reasoning_model": "claude-opus-5",
        "models": {"synthesis": {"model": "claude-opus-5", "effort": "low"}},
    }
    llm = make_chat_model(cfg, agent="editor", default_tag="synthesis",
                          reasoning_effort="medium")
    assert llm.effort == "low"


def test_the_call_site_default_still_applies_when_config_is_silent():
    cfg = {"provider": "anthropic", "reasoning_model": "claude-opus-5"}
    llm = make_chat_model(cfg, agent="editor", default_tag="synthesis",
                          reasoning_effort="medium")
    assert llm.effort == "medium"


def test_an_agent_with_no_effort_anywhere_does_not_think():
    """Thinking off means no `thinking` kwarg at all — the reviewers' path."""
    cfg = {"provider": "anthropic", "reasoning_model": "claude-sonnet-5"}
    llm = make_chat_model(cfg, agent="reviewer_rigor", default_tag="reviewer")
    assert not getattr(llm, "thinking", None)


def test_thinking_respects_the_configured_output_budget():
    """Thinking must not silently override the explicit output-token budget."""
    cfg = {"provider": "anthropic", "reasoning_model": "claude-opus-5"}
    thinking = make_chat_model(cfg, agent="editor", default_tag="synthesis",
                               reasoning_effort="high")
    plain = make_chat_model(cfg, agent="reviewer_rigor", default_tag="reviewer")
    assert thinking.max_tokens == 12000
    assert thinking.max_tokens == plain.max_tokens
