"""The typed bibliography, and the two agents that read it.

The converter types bibliography entries and parses what it can out of each,
so the citation auditor and the literature reviewer no longer have to recover
the reference list by pattern-matching the prose. What has to hold:

* the entries reach exactly those two agents, and nobody else — a block added
  to the shared prefix would cost every other agent its provider-side cache
  entry;
* it is appended *after* the shared blocks, so the prefix the other agents
  send stays byte-identical and these two write only the list on top of it;
* an absent bibliography changes nothing, because a Markdown submission and an
  older converter both produce none.

No API key and no network: the fake-LLM harness from test_pipeline stands in
for the model.
"""

from __future__ import annotations

from test_pipeline import _patch_llms

from peerreviewagents.agents.auditors import citation_integrity
from peerreviewagents.agents.reviewers import literature
from peerreviewagents.agents.utils.agent_utils import (
    context_block,
    manuscript_block,
    references_block,
)

ENTRIES = [
    {
        "label": "1",
        "raw": "Jimmy Lei Ba, Jamie Ryan Kiros, and Geoffrey E Hinton. Layer normalization. 2016.",
        "authors": ["Jimmy Lei Ba", "Jamie Ryan Kiros", "Geoffrey E Hinton"],
        "title": "Layer normalization",
        "year": 2016,
        "arxiv": "1607.06450",
    },
    {"label": "2", "raw": "Denny Britz and others. Massive exploration. 2017."},
]


def _state(references=None, **over):
    state = {
        "manuscript_path": "paper.pdf",
        "manuscript_title": "A Lightweight Method",
        "manuscript_md": "# A Lightweight Method\n\nWe cite [1] and [2].",
        "sections": {},
        "references": ENTRIES if references is None else references,
        "ingest": {},
        "config": {"run_id": "test-run", "research_enabled": False},
        "journal_block": "",
        "article_type_block": "",
        "strictness_block": "",
    }
    state.update(over)
    return state


# --- what the block says ------------------------------------------------------


def test_the_block_is_the_list_with_the_manuscripts_own_labels():
    block = references_block(_state())
    assert "=== REFERENCE LIST (2 entries) ===" in block
    assert "[1] Jimmy Lei Ba" in block
    assert "[2] Denny Britz" in block


def test_no_bibliography_means_no_block():
    """A Markdown submission has no document model, and an older converter
    types no reference blocks. Both must cost nothing."""
    assert references_block(_state(references=[])) == ""
    assert references_block({"manuscript_md": "Body."}) == ""


def test_a_pathological_bibliography_is_capped_and_says_so():
    many = [{"label": str(i), "raw": f"Entry {i}."} for i in range(1200)]
    block = references_block(_state(references=many))
    assert "=== REFERENCE LIST (1200 entries) ===" in block
    assert "200 further entries omitted" in block


# --- who receives it ----------------------------------------------------------


def _captured_prefix(monkeypatch, node_module, node, state):
    """Run one agent node against the fake LLM, returning its cached prefix."""
    seen: dict = {}
    real = node_module.invoke_structured

    def fake_invoke(llm, schema, config, system, user, *, cached_prefix=None, **kw):
        seen["prefix"] = cached_prefix
        seen["user"] = user
        return real(llm, schema, config, system, user, cached_prefix=cached_prefix, **kw)

    _patch_llms(monkeypatch)
    monkeypatch.setattr(node_module, "invoke_structured", fake_invoke)
    result = node(state)
    assert not result.get("errors"), result.get("errors")
    return seen


def test_the_citation_auditor_is_given_the_entries(monkeypatch):
    from peerreviewagents.agents.auditors import base

    state = _state()
    seen = _captured_prefix(monkeypatch, base, citation_integrity.node, state)
    assert seen["prefix"][: len(context_block(state))] == context_block(state)
    assert seen["prefix"][-1] == references_block(state)
    # And it is told what the list is and what it is not, because "no matching
    # entry" has two causes and only one of them is the manuscript's.
    assert "A REFERENCE LIST block is included above" in seen["user"]
    assert "never on its own evidence" in seen["user"]


def test_the_literature_reviewer_is_given_the_entries(monkeypatch):
    from peerreviewagents.agents.reviewers import base

    state = _state()
    seen = _captured_prefix(monkeypatch, base, literature.node, state)
    assert seen["prefix"][: len(context_block(state))] == context_block(state)
    assert seen["prefix"][-1] == references_block(state)


def test_the_shared_prefix_the_other_agents_send_is_untouched(monkeypatch):
    """The manuscript block is one provider-side cache entry across the whole
    fan-out. Mixing the reference list into it would fragment that entry for
    the benefit of two agents."""
    from peerreviewagents.agents.reviewers import base, rigor

    state = _state()
    seen = _captured_prefix(monkeypatch, base, rigor.node, state)
    assert seen["prefix"] == context_block(state)
    assert seen["prefix"][0] == manuscript_block(state)
    assert "REFERENCE LIST" not in "".join(seen["prefix"])


def test_without_a_bibliography_the_two_agents_send_what_everyone_else_does(monkeypatch):
    """The prompt must not announce a block that is not there."""
    from peerreviewagents.agents.auditors import base

    state = _state(references=[])
    seen = _captured_prefix(monkeypatch, base, citation_integrity.node, state)
    assert seen["prefix"] == context_block(state)
    assert "REFERENCE LIST" not in seen["user"]


def test_the_graph_puts_the_entries_on_the_state(monkeypatch, tmp_path):
    """The loader parses them; the state channel is how they get to an agent."""
    from peerreviewagents.default_config import get_config
    from peerreviewagents.graph.review_graph import PeerReviewGraph
    from peerreviewagents.ingest import loader

    md = tmp_path / "paper.md"
    md.write_text("# A Paper\n\n" + "We measured the thing. " * 40, encoding="utf-8")
    real = loader.load_manuscript_record

    def with_references(path, config=None, *, kind="manuscript"):
        parsed = real(path, config, kind=kind)
        return type(parsed)(
            title=parsed.title,
            text=parsed.text,
            sections=parsed.sections,
            references=ENTRIES,
            ingest=parsed.ingest,
        )

    monkeypatch.setattr(
        "peerreviewagents.graph.review_graph.load_manuscript_record", with_references
    )
    state = PeerReviewGraph(get_config(cache_dir=str(tmp_path / "cache"))).initial_state(
        str(md)
    )
    assert state["references"] == ENTRIES
    assert "[1] Jimmy Lei Ba" in references_block(state)
