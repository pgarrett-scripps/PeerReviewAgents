"""Tests for the revision-round scaffold: round records, ids, graph shape.

These cover the contracts the per-node tracks build against — if one of
these breaks, a revision round is referencing the previous round wrongly,
which is the failure that matters most here.

The id tests carry more weight than they used to. With the reviewer panel
blind to the round, the editor's numbered R-list is the *only* lineage a
manuscript has through this pipeline, so an id that changes between rounds
does not degrade the review — it severs it.
"""

from __future__ import annotations

import json
import os

import pytest

from peerreviewagents import rounds
from peerreviewagents.default_config import get_config
from peerreviewagents.graph.review_graph import build_graph, is_revision


def _state(**over):
    base = {
        "manuscript_title": "A Lightweight Method",
        "manuscript_path": "",
        "config": {},
        "decision": "major",
        "readiness_score": 78,
        "readiness_breakdown": {
            "scientific_validity": 28,
            "methods_and_evidence": 20,
            "reproducibility_and_reporting": 15,
            "clarity_and_completeness": 15,
        },
        "contribution_profile": {
            "novelty": "moderate",
            "significance": "moderate",
            "usefulness": "high",
        },
        "score_decision_rationale": (
            "The manuscript is promising, but an unresolved central issue "
            "still requires major revision before publication."
        ),
        "required_revisions": [
            "Report per-cluster results rather than the pooled mean.",
            "State the random seed used for training.",
        ],
        "minor_suggestions": ["Define WidgetNet on first use."],
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
    }
    base.update(over)
    return base


# --- record construction ----------------------------------------------------


def test_build_assigns_stable_ids():
    rec = rounds.build_from_state(_state(), job_id="20260801-x")
    assert rec.round == 1
    assert [r.id for r in rec.required_revisions] == ["R1-01", "R1-02"]
    assert [w.id for w in rec.reviewer_reports[0].weaknesses] == ["scientific_validity-1"]


def test_weighted_score_matches_confidence_weighting():
    rec = rounds.build_from_state(_state(), job_id="j")
    # (3*4 + 2*3) / (4+3)
    assert rec.weighted_score == pytest.approx(18 / 7, abs=1e-3)


def test_editorial_score_survives_round_record_reload(tmp_path):
    rec = rounds.build_from_state(_state(), job_id="j")
    rounds.save(rec, str(tmp_path))
    loaded = rounds.load(str(tmp_path))
    assert loaded.readiness_score == 78
    assert loaded.readiness_breakdown["scientific_validity"] == 28
    assert loaded.contribution_profile["usefulness"] == "high"


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
    assert seed_item.source_reviewer == "data_analysis"


def test_attribution_left_empty_when_nothing_matches():
    state = _state(required_revisions=["Add a graphical abstract."])
    rec = rounds.build_from_state(state, job_id="j")
    assert rec.required_revisions[0].source_reviewer == ""


# --- id stability across rounds ---------------------------------------------
#
# The editor restates a still-open ask as "[R1-03] what is still missing".
# Re-enumerating those buried the real id inside the text and gave the item a
# new one, so an ask reported under R1-03 by round 2's auditor came back as
# R2-01 with "[R1-03]" in its sentence — two ids for one ask, and a lineage
# that no longer joins. That list is the whole of the pipeline's round-over-
# round memory now, so these are the tests that keep it connected.


def _carried(*texts: str, prior, **over):
    return _state(required_revisions=list(texts), prior_round=prior, **over)


def test_a_restated_item_keeps_its_original_id():
    first = rounds.build_from_state(_state(), job_id="j1")
    second = rounds.build_from_state(
        _carried("[R1-02] The random seed is still not stated.", prior=first),
        job_id="j2",
    )
    item = second.revision_by_id("R1-02")
    assert item is not None
    assert item.id == "R1-02"


def test_the_tag_is_stripped_from_the_stored_text():
    """It is the item's identity, not a sentence — rendering it twice is how
    round 3's compliance prompt ended up showing two ids for one ask."""
    first = rounds.build_from_state(_state(), job_id="j1")
    second = rounds.build_from_state(
        _carried("[R1-02] The random seed is still not stated.", prior=first),
        job_id="j2",
    )
    assert second.required_revisions[0].text == "The random seed is still not stated."

    block = second.required_revisions_block()
    assert block.count("R1-02") == 1
    assert "R2-01" not in block


def test_only_new_items_draw_a_fresh_id():
    first = rounds.build_from_state(_state(), job_id="j1")
    second = rounds.build_from_state(
        _carried(
            "[R1-02] The random seed is still not stated.",
            "Report the variance across runs.",
            prior=first,
        ),
        job_id="j2",
    )
    assert [r.id for r in second.required_revisions] == ["R1-02", "R2-01"]


def test_new_items_are_numbered_among_themselves():
    """A carried item above a new one must not consume R2-01."""
    first = rounds.build_from_state(_state(), job_id="j1")
    second = rounds.build_from_state(
        _carried(
            "[R1-01] Per-cluster results are still pooled.",
            "Report the variance across runs.",
            "[R1-02] The random seed is still not stated.",
            "Name the hardware used.",
            prior=first,
        ),
        job_id="j2",
    )
    assert [r.id for r in second.required_revisions] == [
        "R1-01", "R2-01", "R1-02", "R2-02",
    ]


def test_a_short_form_tag_normalizes_to_the_canonical_id():
    """`[R1-2]` and `[R1-02]` name the same ask; lookups use the padded form."""
    first = rounds.build_from_state(_state(), job_id="j1")
    second = rounds.build_from_state(
        _carried("[R1-2] Still no seed.", prior=first), job_id="j2"
    )
    assert second.revision_by_id("R1-02") is not None


def test_a_duplicated_tag_does_not_collapse_two_asks():
    """Two items under one id would make `revision_by_id` answer for one of
    them and silently lose the other, which is worse than an odd id."""
    first = rounds.build_from_state(_state(), job_id="j1")
    second = rounds.build_from_state(
        _carried("[R1-01] Still pooled.", "[R1-01] Also still pooled.", prior=first),
        job_id="j2",
    )
    ids = [r.id for r in second.required_revisions]
    assert len(set(ids)) == 2
    assert ids[0] == "R1-01"


def test_an_item_open_since_round_one_is_still_R1_03_in_round_three():
    """The chaining test: three rounds, one ask nobody ever fixed.

    If this breaks, the round-3 compliance auditor is reporting on an id that
    round 1 never issued, the authors are being asked to follow an ask whose
    name changed under them, and nothing joins the three rounds together.
    """
    round1 = rounds.build_from_state(
        _state(required_revisions=[
            "Report per-cluster results rather than the pooled mean.",
            "State the random seed used for training.",
            "Justify the single-cluster generalization claim.",
        ]),
        job_id="j1",
    )
    assert round1.revision_by_id("R1-03").text.startswith("Justify the single-cluster")

    round2 = rounds.build_from_state(
        _carried(
            "[R1-03] The generalization claim is still unqualified.",
            "Report the variance across runs.",
            prior=round1,
            config={"revision_of": "j1"},
        ),
        job_id="j2",
    )
    assert round2.round == 2
    assert [r.id for r in round2.required_revisions] == ["R1-03", "R2-01"]

    round3 = rounds.build_from_state(
        _carried(
            "[R1-03] The generalization claim is still unqualified.",
            "[R2-01] Variance is reported for one run only.",
            "Cite the benchmark's original paper.",
            prior=round2,
            config={"revision_of": "j2"},
        ),
        job_id="j3",
    )
    assert round3.round == 3
    assert [r.id for r in round3.required_revisions] == ["R1-03", "R2-01", "R3-01"]

    carried = round3.revision_by_id("R1-03")
    assert carried is not None
    assert carried.text == "The generalization claim is still unqualified."
    assert "[R1-03]" not in carried.text

    # And it survives the disk round-trip the next round would read it from.
    assert rounds.RoundRecord.from_dict(
        json.loads(round3.to_json())
    ).revision_by_id("R1-03") is not None


# --- null scores ------------------------------------------------------------


def _null_score_state():
    """A finished round whose panel includes one abstaining reviewer."""
    state = _state()
    state["reports"] = state["reports"] + [
        {
            "reviewer": "ethics",
            "score": None,
            "not_applicable_reason": "No quantitative analysis in this paper.",
            "confidence": 5,
            "weaknesses": [],
            "questions": [],
            "body": "",
        },
    ]
    return state


def test_null_score_survives_build_and_reload(tmp_path):
    rec = rounds.build_from_state(_null_score_state(), job_id="j")
    assert rec.report_for("ethics").score is None
    # The abstention leaves the weighted mean entirely — numerator and
    # denominator both.
    assert rec.weighted_score == pytest.approx(18 / 7, abs=1e-3)

    rounds.save(rec, str(tmp_path))
    loaded = rounds.load(str(tmp_path))
    assert loaded.report_for("ethics").score is None
    assert loaded.report_for("scientific_validity").score == 3.0


def test_write_reports_survives_a_null_score_panel(tmp_path):
    """One abstaining reviewer must still leave a loadable round.json behind."""
    from peerreviewagents.reports import write_reports

    state = _null_score_state()
    state["config"] = {"output_dir": str(tmp_path), "run_id": ""}
    state["errors"] = []
    run_dir = write_reports(state)

    loaded = rounds.load(run_dir)
    assert loaded.report_for("ethics").score is None
    assert not state["errors"]


def test_round_record_failure_is_surfaced_not_swallowed(tmp_path, monkeypatch, capsys):
    """A run that cannot be revised must say so, not look healthy on disk."""
    from peerreviewagents.reports import write_reports

    def boom(*_args, **_kwargs):
        raise RuntimeError("record build exploded")

    monkeypatch.setattr("peerreviewagents.rounds.build_from_state", boom)
    state = _state()
    state["config"] = {"output_dir": str(tmp_path), "run_id": ""}
    state["errors"] = []
    run_dir = write_reports(state)  # the run itself must survive

    assert not os.path.isfile(os.path.join(run_dir, rounds.ROUND_FILENAME))
    assert state["errors"] and "round.json" in state["errors"][0]
    assert "cannot be revised" in state["errors"][0]
    assert "round.json" in capsys.readouterr().out


# --- persistence ------------------------------------------------------------


def test_round_trips_through_disk(tmp_path):
    rec = rounds.build_from_state(_state(), job_id="j", cache_key="abc123")
    rounds.save(rec, str(tmp_path))
    loaded = rounds.load(str(tmp_path))
    assert loaded.job_id == rec.job_id
    assert loaded.manuscript_cache_key == "abc123"
    assert [r.id for r in loaded.required_revisions] == ["R1-01", "R1-02"]
    assert loaded.reviewer_reports[0].weaknesses[0].text.startswith("Only a single")


def test_text_hash_is_recorded_and_round_trips(tmp_path):
    """The hash is what lets a later round verify a caller-supplied baseline
    file — the cache key alone proved unverifiable the moment its derivation
    changed (cache v8) under every record already published."""
    state = _state()
    state["ingest"] = {"text_sha256": "a" * 64, "chars": 18}
    rec = rounds.build_from_state(state, job_id="j", cache_key="abc123")
    assert rec.manuscript_text_sha256 == "a" * 64

    rounds.save(rec, str(tmp_path))
    assert rounds.load(str(tmp_path)).manuscript_text_sha256 == "a" * 64


def test_records_without_the_text_hash_still_load():
    """Every round.json published before the field existed lacks it, and
    those manuscripts' revision lineages must stay readable."""
    rec = rounds.build_from_state(_state(), job_id="j")
    raw = json.loads(rec.to_json())
    del raw["manuscript_text_sha256"]

    loaded = rounds.RoundRecord.from_dict(raw)
    assert loaded.manuscript_text_sha256 == ""
    # A state with no ingest record (the _state() fixture) records "" too,
    # rather than crashing the record build.
    assert rec.manuscript_text_sha256 == ""


def test_future_record_fields_do_not_break_the_load(tmp_path):
    """A round.json from a newer schema must load under this code.

    A manuscript's revision lineage spans months; the version that reads a
    record is routinely older than the one that wrote it, and one added field
    must not make the whole lineage unreadable.
    """
    rec = rounds.build_from_state(_state(), job_id="j")
    raw = json.loads(rec.to_json())
    raw["required_revisions"][0]["deadline"] = "2027-01-01"
    raw["reviewer_reports"][0]["weaknesses"][0]["severity"] = "HARD"
    raw["a_future_top_level_field"] = {"x": 1}

    loaded = rounds.RoundRecord.from_dict(raw)
    assert loaded.required_revisions[0].id == "R1-01"
    assert loaded.reviewer_reports[0].weaknesses[0].id == "scientific_validity-1"


def test_carried_reports_compare_membership_not_size():
    """A prior round short one reviewer (it errored) can match a subset by count.

    Seven chosen names against seven prior reports used to read as "the whole
    panel is re-running" whenever the sizes coincided, dropping exactly the
    report that needed carrying.
    """
    from peerreviewagents.graph.review_graph import PeerReviewGraph

    prior = rounds.build_from_state(_state(), job_id="j1")  # sci-validity + data-analysis
    graph = PeerReviewGraph(get_config(
        revision_of="j1",
        only_reviewers=["scientific_validity", "ethics"],
    ))
    carried = graph._carried_reports(prior)
    assert [r["reviewer"] for r in carried] == ["data_analysis"]


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
    # The parsed text's own fingerprint travels with the record: it is what a
    # later round on another machine verifies a baseline file against.
    assert raw["manuscript_text_sha256"]
    assert raw["required_revisions"], "editor's asks must survive as structured data"
    assert raw["reviewer_reports"][0]["weaknesses"]


# --- prompt blocks ----------------------------------------------------------



def test_required_revisions_block_lists_ids():
    rec = rounds.build_from_state(_state(), job_id="j")
    block = rec.required_revisions_block()
    assert "[R1-01]" in block and "[R1-02]" in block
    assert "(raised by data_analysis)" in block


# --- the optional documents around the manuscript ----------------------------


def test_an_unreadable_supplement_is_surfaced_not_silently_dropped(tmp_path):
    """`except Exception: return "", {}` swallowed everything — including
    "rustypaper not installed" — and the methods-completeness auditor then
    reported the SI missing when the operator had supplied it."""
    from peerreviewagents.agents.utils.agent_utils import supplement_block
    from peerreviewagents.graph.review_graph import PeerReviewGraph

    cfg = get_config(
        supplement_path=str(tmp_path / "missing_si.pdf"), output_dir=str(tmp_path)
    )
    sup_md, sup_sections = PeerReviewGraph(cfg)._load_supplement()
    assert sup_sections == {}
    assert "missing_si.pdf" in sup_md
    assert "could not be read" in sup_md
    assert "do not report it as missing" in sup_md
    # The placeholder rides where the SI text would have, so the auditor's
    # context block carries the explanation instead of nothing.
    assert "could not be read" in supplement_block({"supplement_md": sup_md})


def test_no_supplement_path_still_means_no_supplement(tmp_path):
    from peerreviewagents.graph.review_graph import PeerReviewGraph

    cfg = get_config(output_dir=str(tmp_path))
    assert PeerReviewGraph(cfg)._load_supplement() == ("", {})


def test_author_letter_failure_names_the_letter_not_the_manuscript(tmp_path):
    from peerreviewagents.graph.review_graph import PeerReviewGraph

    cfg = get_config(
        revision_of="j1",
        author_statement_path=str(tmp_path / "gone.md"),
        output_dir=str(tmp_path),
    )
    with pytest.raises(ValueError, match="author response letter"):
        PeerReviewGraph(cfg)._load_author_statement()


# --- graph shape ------------------------------------------------------------


def _nodes(**kw):
    return set(build_graph(get_config(**kw)).get_graph().nodes)


def test_first_round_graph_has_one_synthesis_layer():
    nodes = _nodes()
    assert "audit_revision_compliance" not in nodes
    assert "response_verifier" not in nodes
    assert "author_rebuttal" not in nodes
    assert "gap_finder" not in nodes
    assert "debate_synthesizer" in nodes
    assert "editor" in nodes
    assert not is_revision(get_config())


def test_debate_rounds_are_parallel_with_a_join():
    graph = build_graph(get_config(max_debate_rounds=2))
    view = graph.get_graph()
    nodes = set(view.nodes)
    assert {"advocate", "skeptic", "debate_join", "debate_synthesizer"} <= nodes

    edges = {(edge.source, edge.target) for edge in view.edges}
    # Both debaters are entered from the panel gate in parallel, meet at the
    # join, and the join either fans the next round out or hands off to the
    # synthesizer — there is no advocate -> skeptic edge anywhere.
    assert ("panel_gate", "advocate") in edges
    assert ("panel_gate", "skeptic") in edges
    assert ("advocate", "debate_join") in edges
    assert ("skeptic", "debate_join") in edges
    assert ("advocate", "skeptic") not in edges
    assert ("skeptic", "advocate") not in edges
    assert ("debate_join", "advocate") in edges
    assert ("debate_join", "skeptic") in edges
    assert ("debate_join", "debate_synthesizer") in edges
    assert ("debate_synthesizer", "editor") in edges


def test_the_panel_is_the_five_condensed_specialists():
    graph = build_graph(get_config())
    nodes = set(graph.get_graph().nodes)

    assert {
        "reviewer_scientific_validity",
        "reviewer_data_analysis",
        "reviewer_contribution_context",
        "reviewer_reporting_reproducibility",
        "reviewer_ethics",
    } <= nodes
    assert "reviewer_methodology" not in nodes
    assert "reviewer_rigor" not in nodes
    assert "reviewer_novelty" not in nodes
    assert "reviewer_literature" not in nodes
    assert "reviewer_clarity" not in nodes
    assert "reviewer_reproducibility" not in nodes
    assert "debate_synthesizer" in nodes
    assert "advocate" in nodes
    assert "skeptic" in nodes
    assert "audit_methods_completeness" in nodes
    assert "audit_citation_integrity" in nodes


def test_revision_adds_the_compliance_auditor():
    assert "audit_revision_compliance" in _nodes(revision_of="j1")


def test_author_statement_adds_the_verifier_without_a_simulated_rebuttal():
    nodes = _nodes(revision_of="j1", author_statement_path="letter.md")
    assert "response_verifier" in nodes
    assert "author_rebuttal" not in nodes


def test_verifier_precedes_the_panel():
    """The letter must be adjudicated before any reviewer could read it."""
    graph = build_graph(get_config(revision_of="j1", author_statement_path="l.md"))
    edges = graph.get_graph().edges
    targets = {e.target for e in edges if e.source == "response_verifier"}
    assert "reviewer_scientific_validity" in targets
    sources = {e.source for e in edges if e.target == "reviewer_scientific_validity"}
    assert sources == {"response_verifier"}


def test_verifier_still_gated_behind_the_desk_screen():
    """The letter must clear the desk before anything verifies it."""
    graph = build_graph(get_config(revision_of="j1", author_statement_path="l.md"))
    edges = graph.get_graph().edges
    sources = {e.source for e in edges if e.target == "response_verifier"}
    assert sources == {"desk_screen"}


def test_verifier_reachable_from_start_without_the_desk_screen():
    graph = build_graph(get_config(
        revision_of="j1", author_statement_path="l.md",
        # The conversion gate would otherwise keep the desk node wired in.
        conversion_gate="off",
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
    from peerreviewagents.cli.main import build_parser, config_from_args

    args = build_parser().parse_args(["m.pdf", "--author-statement", "letter.md"])
    with pytest.raises(SystemExit, match="requires --revision-of"):
        config_from_args(args)


def test_unloadable_prior_round_fails_loudly(tmp_path):
    """Silently downgrading to a fresh review would misinform the authors."""
    from peerreviewagents.graph.review_graph import PeerReviewGraph

    graph = PeerReviewGraph(get_config(revision_of="nonexistent", output_dir=str(tmp_path)))
    with pytest.raises(FileNotFoundError):
        graph.initial_state("tests/sample_manuscript.md")
