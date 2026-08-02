"""Tests for the revision-round scaffold: round records, diff, graph shape.

These cover the contracts the per-node tracks build against — if one of
these breaks, a revision round is referencing the previous round wrongly,
which is the failure that matters most here.
"""

from __future__ import annotations

import json
import os

import pytest

from peerreviewagents import rounds
from peerreviewagents.default_config import get_config
from peerreviewagents.graph.review_graph import build_graph, is_revision
from peerreviewagents.ingest import diff as ingest_diff


def _state(**over):
    base = {
        "manuscript_title": "A Lightweight Method",
        "manuscript_path": "",
        "config": {},
        "decision": "major",
        "required_revisions": [
            "Report per-cluster results rather than the pooled mean.",
            "State the random seed used for training.",
        ],
        "minor_suggestions": ["Define WidgetNet on first use."],
        "reports": [
            {
                "reviewer": "methodology",
                "score": 3,
                "confidence": 4,
                "weaknesses": ["Only a single production cluster is used."],
                "questions": ["How were baselines tuned?"],
                "body": "",
            },
            {
                "reviewer": "rigor",
                "score": 2,
                "confidence": 3,
                "weaknesses": ["No random seed is reported for training."],
                "questions": [],
                "body": "",
            },
        ],
    }
    base.update(over)
    return base


# --- record construction ----------------------------------------------------


def test_build_assigns_stable_ids():
    rec = rounds.build_from_state(_state(), job_id="20260801-x")
    assert rec.round == 1
    assert [r.id for r in rec.required_revisions] == ["R1-01", "R1-02"]
    assert [w.id for w in rec.reviewer_reports[0].weaknesses] == ["methodology-1"]


def test_weighted_score_matches_confidence_weighting():
    rec = rounds.build_from_state(_state(), job_id="j")
    # (3*4 + 2*3) / (4+3)
    assert rec.weighted_score == pytest.approx(18 / 7, abs=1e-3)


def test_round_increments_from_prior():
    first = rounds.build_from_state(_state(), job_id="j1")
    second = rounds.build_from_state(
        _state(prior_round=first, config={"revision_of": "j1"}), job_id="j2"
    )
    assert second.round == 2
    assert second.prior_job_id == "j1"
    assert [r.id for r in second.required_revisions] == ["R2-01", "R2-02"]


def test_required_revision_attributed_to_its_reviewer():
    rec = rounds.build_from_state(_state(), job_id="j")
    seed_item = next(r for r in rec.required_revisions if "seed" in r.text)
    assert seed_item.source_reviewer == "rigor"


def test_attribution_left_empty_when_nothing_matches():
    state = _state(required_revisions=["Add a graphical abstract."])
    rec = rounds.build_from_state(state, job_id="j")
    assert rec.required_revisions[0].source_reviewer == ""


# --- persistence ------------------------------------------------------------


def test_round_trips_through_disk(tmp_path):
    rec = rounds.build_from_state(_state(), job_id="j", cache_key="abc123")
    rounds.save(rec, str(tmp_path))
    loaded = rounds.load(str(tmp_path))
    assert loaded.job_id == rec.job_id
    assert loaded.manuscript_cache_key == "abc123"
    assert [r.id for r in loaded.required_revisions] == ["R1-01", "R1-02"]
    assert loaded.reviewer_reports[0].weaknesses[0].text.startswith("Only a single")


def test_missing_record_names_the_problem(tmp_path):
    with pytest.raises(FileNotFoundError, match="predates round records"):
        rounds.load(str(tmp_path))


def test_resolve_run_dir_accepts_job_id_or_path(tmp_path):
    run = tmp_path / "20260801-slug"
    run.mkdir()
    config = {"output_dir": str(tmp_path)}
    assert rounds.resolve_run_dir("20260801-slug", config) == str(run)
    assert rounds.resolve_run_dir(str(run), config) == str(run)
    with pytest.raises(FileNotFoundError, match="No review run found"):
        rounds.resolve_run_dir("nope", config)


def test_written_by_write_reports(tmp_path, monkeypatch):
    from test_pipeline import SAMPLE, _patch_llms

    from peerreviewagents.graph.review_graph import PeerReviewGraph
    from peerreviewagents.reports import write_reports

    _patch_llms(monkeypatch)
    graph = PeerReviewGraph(get_config(max_debate_rounds=1, output_dir=str(tmp_path)))
    state = graph.review(SAMPLE)
    run_dir = write_reports(state)

    raw = json.loads(open(os.path.join(run_dir, "round.json"), encoding="utf-8").read())
    assert raw["round"] == 1
    assert raw["decision"] == "major"
    assert raw["manuscript_cache_key"]          # sample is a real file
    assert raw["required_revisions"], "editor's asks must survive as structured data"
    assert raw["reviewer_reports"][0]["weaknesses"]


# --- prompt blocks ----------------------------------------------------------


def test_prior_report_block_is_scoped_to_one_reviewer():
    rec = rounds.build_from_state(_state(), job_id="j")
    block = rec.prior_report_block("methodology")
    assert "methodology-1" in block
    assert "single production cluster" in block
    # The panel's independence must survive the revision round.
    assert "seed" not in block.lower()


def test_prior_report_block_empty_for_unknown_reviewer():
    rec = rounds.build_from_state(_state(), job_id="j")
    assert rec.prior_report_block("ethics") == ""


def test_required_revisions_block_lists_ids():
    rec = rounds.build_from_state(_state(), job_id="j")
    block = rec.required_revisions_block()
    assert "[R1-01]" in block and "[R1-02]" in block
    assert "(raised by rigor)" in block


# --- manuscript diff --------------------------------------------------------


def test_diff_detects_changed_added_and_unchanged():
    old = {"abstract": "We propose WidgetNet.", "methods": "We train on one cluster."}
    new = {
        "abstract": "We propose WidgetNet.",
        "methods": "We train on three clusters with seed 42.",
        "limitations": "Single-domain evaluation.",
    }
    d = ingest_diff.diff_sections(old, new)
    by_name = {x.name: x for x in d.deltas}
    assert by_name["abstract"].status == "unchanged"
    assert by_name["methods"].status == "changed"
    assert by_name["limitations"].status == "added"
    assert "limitations" in d.changed_section_names()


def test_whitespace_reflow_is_not_a_change():
    old = {"methods": "We train\non one cluster."}
    new = {"methods": "We train on one   cluster."}
    assert ingest_diff.diff_sections(old, new).deltas[0].status == "unchanged"


def test_identical_draft_says_so_loudly():
    sections = {"abstract": "Unchanged text here.", "methods": "Also unchanged."}
    block = ingest_diff.render_diff_block(
        ingest_diff.diff_sections(sections, dict(sections))
    )
    assert "**Nothing.**" in block
    assert "still outstanding" in block


def test_reference_churn_is_not_substantive():
    old = {"methods": "Same.", "references": "[1] Smith 2019."}
    new = {"methods": "Same.", "references": "[1] Smith 2019. [2] Jones 2020."}
    d = ingest_diff.diff_sections(old, new)
    assert d.changed and not d.substantive


def test_unavailable_diff_tells_the_agent_not_to_assume():
    block = ingest_diff.render_diff_block(ingest_diff.unavailable("cache cleared"))
    assert "Not available" in block
    assert "do not assume" in block


# --- graph shape ------------------------------------------------------------


def _nodes(**kw):
    return set(build_graph(get_config(**kw)).get_graph().nodes)


def test_first_round_graph_is_unchanged():
    nodes = _nodes()
    assert "audit_revision_compliance" not in nodes
    assert "response_verifier" not in nodes
    assert "author_rebuttal" in nodes
    assert not is_revision(get_config())


def test_revision_adds_the_compliance_auditor():
    assert "audit_revision_compliance" in _nodes(revision_of="j1")


def test_author_statement_swaps_rebuttal_for_the_verifier():
    nodes = _nodes(revision_of="j1", author_statement_path="letter.md")
    assert "response_verifier" in nodes
    # The simulated rebuttal gives way to the real letter.
    assert "author_rebuttal" not in nodes


def test_verifier_precedes_the_panel():
    """The letter must be adjudicated before any reviewer could read it."""
    graph = build_graph(get_config(revision_of="j1", author_statement_path="l.md"))
    edges = graph.get_graph().edges
    targets = {e.target for e in edges if e.source == "response_verifier"}
    assert "reviewer_methodology" in targets
    sources = {e.source for e in edges if e.target == "reviewer_methodology"}
    assert sources == {"response_verifier"}


def test_verifier_still_gated_behind_the_desk_screen():
    """An injected letter must hit the integrity screen before verification."""
    graph = build_graph(get_config(revision_of="j1", author_statement_path="l.md"))
    edges = graph.get_graph().edges
    sources = {e.source for e in edges if e.target == "response_verifier"}
    assert sources == {"desk_screen"}


def test_verifier_reachable_from_start_without_the_desk_screen():
    graph = build_graph(get_config(
        revision_of="j1", author_statement_path="l.md", injection_screen=False,
    ))
    edges = graph.get_graph().edges
    sources = {e.source for e in edges if e.target == "response_verifier"}
    assert sources == {"__start__"}


# --- config wiring ----------------------------------------------------------


def test_revision_defaults_are_off():
    config = get_config()
    assert config["revision_of"] is None
    assert config["author_statement_path"] is None
    assert config["max_rounds"] == 3


def test_author_statement_requires_a_prior_round():
    from cli.main import build_parser, config_from_args

    args = build_parser().parse_args(["m.pdf", "--author-statement", "letter.md"])
    with pytest.raises(SystemExit, match="requires --revision-of"):
        config_from_args(args)


def test_unloadable_prior_round_fails_loudly(tmp_path):
    """Silently downgrading to a fresh review would misinform the authors."""
    from peerreviewagents.graph.review_graph import PeerReviewGraph

    graph = PeerReviewGraph(get_config(revision_of="nonexistent", output_dir=str(tmp_path)))
    with pytest.raises(FileNotFoundError):
        graph.initial_state("tests/sample_manuscript.md")
