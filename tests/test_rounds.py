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


# --- null scores ------------------------------------------------------------


def _null_score_state():
    """A finished round whose panel includes one abstaining reviewer."""
    state = _state()
    state["reports"] = state["reports"] + [
        {
            "reviewer": "data_analysis",
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
    assert rec.report_for("data_analysis").score is None
    # The abstention leaves the weighted mean entirely — numerator and
    # denominator both.
    assert rec.weighted_score == pytest.approx(18 / 7, abs=1e-3)

    rounds.save(rec, str(tmp_path))
    loaded = rounds.load(str(tmp_path))
    assert loaded.report_for("data_analysis").score is None
    assert loaded.report_for("methodology").score == 3.0


def test_prior_report_block_handles_an_abstention():
    """The null-prior branch must be reachable, not decorative."""
    rec = rounds.build_from_state(_null_score_state(), job_id="j")
    block = rec.prior_report_block("data_analysis")
    assert "return null again" in block
    assert "You scored the manuscript" not in block
    # A scored reviewer still gets the numeric framing.
    assert "You scored the manuscript 3/5" in rec.prior_report_block("methodology")


def test_write_reports_survives_a_null_score_panel(tmp_path):
    """One abstaining reviewer must still leave a loadable round.json behind."""
    from peerreviewagents.reports import write_reports

    state = _null_score_state()
    state["config"] = {"output_dir": str(tmp_path), "run_id": ""}
    state["errors"] = []
    run_dir = write_reports(state)

    loaded = rounds.load(run_dir)
    assert loaded.report_for("data_analysis").score is None
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
    assert loaded.reviewer_reports[0].weaknesses[0].id == "methodology-1"


def test_carried_reports_compare_membership_not_size():
    """A prior round short one reviewer (it errored) can match a subset by count.

    Seven chosen names against seven prior reports used to read as "the whole
    panel is re-running" whenever the sizes coincided, dropping exactly the
    report that needed carrying.
    """
    from peerreviewagents.graph.review_graph import PeerReviewGraph

    prior = rounds.build_from_state(_state(), job_id="j1")  # methodology + rigor
    graph = PeerReviewGraph(get_config(
        revision_of="j1", only_reviewers=["methodology", "clarity"],
    ))
    carried = graph._carried_reports(prior)
    assert [r["reviewer"] for r in carried] == ["rigor"]


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


def _prior_record_with_key(
    key: str, text_sha256: str = "", file_sha256: str = ""
) -> rounds.RoundRecord:
    return rounds.RoundRecord(
        schema_version=rounds.SCHEMA_VERSION,
        round=1,
        job_id="round-1",
        manuscript_title="A paper",
        manuscript_cache_key=key,
        decision="major",
        weighted_score=3.0,
        manuscript_text_sha256=text_sha256,
        manuscript_file_sha256=file_sha256,
    )


def _cached_prior_draft(tmp_path, cfg, *, caveman):
    """Plant a prior-round draft in the ingest cache, as a revision finds it."""
    from peerreviewagents.ingest import cache as ingest_cache
    from peerreviewagents.ingest.loader import Manuscript

    src = tmp_path / "prior.pdf"
    src.write_bytes(b"%PDF-1.7\n")
    ingest_cache.put(
        "priorkey",
        Manuscript(
            title="A paper",
            text="Methods text here.",
            sections={"methods": "Methods text here."},
            ingest={
                "format": "markdown",
                "tool": "rustypaper 9.9.9",
                "caveman": None if caveman == "off" else caveman,
                "chars": 18,
            },
        ),
        source_path=str(src),
        config=cfg,
    )


def test_a_caveman_flip_between_rounds_degrades_the_diff(tmp_path):
    """Compressed vs uncompressed text differs everywhere even when the
    authors changed nothing; diffing across the flip told the panel the
    paper was rewritten wholesale."""
    from peerreviewagents.graph.review_graph import PeerReviewGraph

    cfg = get_config(
        caveman="off", cache_dir=str(tmp_path / "cache"), output_dir=str(tmp_path)
    )
    _cached_prior_draft(tmp_path, cfg, caveman="hard")

    diff = PeerReviewGraph(cfg)._manuscript_diff(
        _prior_record_with_key("priorkey"), {"methods": "Methods text here."}
    )
    assert not diff.available
    assert "compression setting changed" in diff.note


def test_a_stable_caveman_level_still_diffs(tmp_path):
    from peerreviewagents.graph.review_graph import PeerReviewGraph

    cfg = get_config(
        caveman="off", cache_dir=str(tmp_path / "cache"), output_dir=str(tmp_path)
    )
    _cached_prior_draft(tmp_path, cfg, caveman="off")

    diff = PeerReviewGraph(cfg)._manuscript_diff(
        _prior_record_with_key("priorkey"), {"methods": "Methods text here."}
    )
    assert diff.available
    assert all(d.status == "unchanged" for d in diff.deltas)


# --- the caller-supplied baseline (revision_baseline_path) -------------------
#
# A round record stores a cache key, and a cache key is an address on one
# machine. On an ephemeral CI runner the cache dies with the job, and the v8
# cache-key change (converter version hashed into PDF keys) re-addressed every
# round record already published — the overlay-journal consumer could re-fetch
# and re-parse the exact draft a round reviewed and still never land on the
# recorded key. `revision_baseline_path` lets such a caller hand the prior
# draft in as a file and prove it with the recorded text hash instead.


# The body deliberately does not open with a section keyword: a line
# starting "Methods ..." reads as a heading to the flat-text splitter and
# would be swallowed as one.
def _baseline_file(tmp_path, text: str = "We train on one production cluster.") -> str:
    path = tmp_path / "prior-draft.md"
    path.write_text(f"# A paper\n\n## Methods\n\n{text}\n", encoding="utf-8")
    return str(path)


# The section map load_manuscript produces for _baseline_file's content.
_BASELINE_SECTIONS = {
    "_preamble": "# A paper",
    "methods": "We train on one production cluster.",
}


def test_a_baseline_file_diffs_with_no_cache_entry_at_all(tmp_path):
    from peerreviewagents.graph.review_graph import PeerReviewGraph

    cfg = get_config(
        revision_baseline_path=_baseline_file(tmp_path),
        cache_dir=str(tmp_path / "cache"),  # empty: nothing to recover from
        output_dir=str(tmp_path),
    )
    # The prior record does not even carry a cache key — the case every
    # pre-v8 record is in after the key derivation changed under it. A record
    # this old predates the text hash too, so the diff proceeds unverified
    # rather than being lost to a check it cannot pass.
    diff = PeerReviewGraph(cfg)._manuscript_diff(
        _prior_record_with_key(""),
        {"_preamble": "# A paper",
         "methods": "We train on three clusters with seed 42."},
    )
    assert diff.available
    by_name = {d.name: d for d in diff.deltas}
    assert by_name["methods"].status == "changed"
    assert by_name["_preamble"].status == "unchanged"


def test_an_unreadable_baseline_costs_the_diff_not_the_round(tmp_path):
    from peerreviewagents.graph.review_graph import PeerReviewGraph

    cfg = get_config(
        revision_baseline_path=str(tmp_path / "vanished.md"),
        cache_dir=str(tmp_path / "cache"),
        output_dir=str(tmp_path),
    )
    diff = PeerReviewGraph(cfg)._manuscript_diff(
        _prior_record_with_key(""), dict(_BASELINE_SECTIONS)
    )
    assert not diff.available
    assert "could not be read" in diff.note


def test_a_wrong_baseline_is_refused_by_the_recorded_text_hash(tmp_path):
    """A diff over the wrong baseline reports author edits that never
    happened, confidently — worse than no diff, so it must not survive a
    hash the prior round recorded."""
    from peerreviewagents.graph.review_graph import PeerReviewGraph
    from peerreviewagents.ingest.loader import load_manuscript_record

    baseline = _baseline_file(tmp_path, text="A different paper entirely.")
    cfg = get_config(
        revision_baseline_path=baseline,
        cache_dir=str(tmp_path / "cache"),
        output_dir=str(tmp_path),
    )
    recorded = "0" * 64  # what round 1 wrote; not what this file parses to
    diff = PeerReviewGraph(cfg)._manuscript_diff(
        _prior_record_with_key("", text_sha256=recorded),
        dict(_BASELINE_SECTIONS),
    )
    assert not diff.available
    assert "could not be verified as the draft" in diff.note
    # Both fingerprints are named, so the operator can see which side moved.
    assert recorded[:12] in diff.note
    parsed = load_manuscript_record(baseline, cfg).ingest["text_sha256"]
    assert parsed[:12] in diff.note


def test_a_verified_baseline_diffs(tmp_path):
    from peerreviewagents.graph.review_graph import PeerReviewGraph
    from peerreviewagents.ingest.loader import load_manuscript_record

    baseline = _baseline_file(tmp_path)
    cfg = get_config(
        revision_baseline_path=baseline,
        cache_dir=str(tmp_path / "cache"),
        output_dir=str(tmp_path),
    )
    # The hash round 1 recorded is of the parsed text, so derive it the same
    # way the pipeline did.
    recorded = load_manuscript_record(baseline, cfg).ingest["text_sha256"]
    diff = PeerReviewGraph(cfg)._manuscript_diff(
        _prior_record_with_key("", text_sha256=recorded),
        dict(_BASELINE_SECTIONS),
    )
    assert diff.available
    assert all(d.status == "unchanged" for d in diff.deltas)


def test_a_converter_upgrade_baseline_is_verified_by_the_file_hash(tmp_path):
    """The text hash breaks across converter upgrades — the same bytes read
    into different text — and verifying by text alone is how an unchanged
    resubmission reviewed across the 0.1.1 → 0.2.0 boundary lost its diff.
    The file hash is of the bytes, and bytes survive the upgrade."""
    import hashlib

    from peerreviewagents.graph.review_graph import PeerReviewGraph

    baseline = _baseline_file(tmp_path)
    cfg = get_config(
        revision_baseline_path=baseline,
        cache_dir=str(tmp_path / "cache"),
        output_dir=str(tmp_path),
    )
    with open(baseline, "rb") as fh:
        file_hash = hashlib.sha256(fh.read()).hexdigest()
    # The recorded TEXT hash mismatches — round 1's converter read the same
    # file into different text — but the recorded FILE hash matches, which is
    # proof enough that this is the reviewed draft.
    diff = PeerReviewGraph(cfg)._manuscript_diff(
        _prior_record_with_key("", text_sha256="0" * 64, file_sha256=file_hash),
        dict(_BASELINE_SECTIONS),
    )
    assert diff.available
    assert all(d.status == "unchanged" for d in diff.deltas)


def test_an_unchanged_resubmission_is_its_own_proof(tmp_path):
    """An old record whose text hash cannot match (converter upgraded, no
    file hash recorded) still gets its diff when the baseline is byte-equal
    to this round's manuscript: the resubmission IS the draft it is compared
    against, so 'nothing changed' holds with no recorded hash at all — and
    that is the exact fact the adversarial invariant needs said out loud."""
    from peerreviewagents.graph.review_graph import PeerReviewGraph

    baseline = _baseline_file(tmp_path)
    cfg = get_config(
        revision_baseline_path=baseline,
        cache_dir=str(tmp_path / "cache"),
        output_dir=str(tmp_path),
    )
    diff = PeerReviewGraph(cfg)._manuscript_diff(
        _prior_record_with_key("", text_sha256="0" * 64),
        dict(_BASELINE_SECTIONS),
        manuscript_path=baseline,  # resubmitted unchanged: same file
    )
    assert diff.available
    assert all(d.status == "unchanged" for d in diff.deltas)


def test_the_loader_fingerprints_the_file_even_on_a_cache_hit(tmp_path):
    """round.json records the file hash off the ingest record, so the record
    has to carry it on every path — including a manuscript served from the
    cache, where the stored entry predates the field."""
    import hashlib

    from peerreviewagents.ingest.loader import load_manuscript_record

    path = _baseline_file(tmp_path)
    cfg = {"cache_dir": str(tmp_path / "cache")}
    with open(path, "rb") as fh:
        expected = hashlib.sha256(fh.read()).hexdigest()
    first = load_manuscript_record(path, cfg)
    assert first.ingest["file_sha256"] == expected
    served = load_manuscript_record(path, cfg)  # cache hit this time
    assert served.ingest["file_sha256"] == expected


def test_round_record_roundtrips_the_file_hash():
    from dataclasses import asdict

    record = _prior_record_with_key("k", file_sha256="f" * 64)
    again = rounds.RoundRecord.from_dict(json.loads(json.dumps(asdict(record))))
    assert again.manuscript_file_sha256 == "f" * 64


def test_a_correction_ignores_the_baseline(tmp_path):
    """A correction has nothing to diff by definition — the complaint is
    about the review. A supplied baseline must not resurrect the comparison."""
    from peerreviewagents.graph.review_graph import PeerReviewGraph

    cfg = get_config(
        revision_of="round-1",
        revision_mode="correction",
        revision_baseline_path=_baseline_file(tmp_path),
        cache_dir=str(tmp_path / "cache"),
        output_dir=str(tmp_path),
    )
    diff = PeerReviewGraph(cfg)._manuscript_diff(
        _prior_record_with_key(""), dict(_BASELINE_SECTIONS)
    )
    assert not diff.available
    assert "correction" in diff.note


def test_no_baseline_still_means_the_cache_path(tmp_path):
    """With the key unset the cache recovery must behave exactly as before —
    the baseline is an addition for callers without a cache, not a change
    for the callers with one."""
    from peerreviewagents.graph.review_graph import PeerReviewGraph

    cfg = get_config(
        cache_dir=str(tmp_path / "cache"), output_dir=str(tmp_path)
    )
    assert cfg["revision_baseline_path"] is None
    _cached_prior_draft(tmp_path, cfg, caveman="off")

    diff = PeerReviewGraph(cfg)._manuscript_diff(
        _prior_record_with_key("priorkey"), {"methods": "Methods text here."}
    )
    assert diff.available
    assert all(d.status == "unchanged" for d in diff.deltas)


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
    assert config["revision_baseline_path"] is None
    assert config["max_rounds"] == 3


def test_author_statement_requires_a_prior_round():
    from peerreviewagents.cli.main import build_parser, config_from_args

    args = build_parser().parse_args(["m.pdf", "--author-statement", "letter.md"])
    with pytest.raises(SystemExit, match="requires --revision-of"):
        config_from_args(args)


def test_revision_baseline_requires_a_prior_round():
    """The baseline is 'the draft the previous round reviewed'; without a
    previous round the phrase names nothing."""
    from peerreviewagents.cli.main import build_parser, config_from_args

    args = build_parser().parse_args(["m.pdf", "--revision-baseline", "prior.pdf"])
    with pytest.raises(SystemExit, match="requires --revision-of"):
        config_from_args(args)


def test_missing_baseline_file_fails_before_any_spend(tmp_path):
    """A typo'd path would otherwise surface as a silently diff-less review,
    discovered after the whole panel has been paid."""
    from peerreviewagents.cli.main import _validate_revision_inputs

    cfg = get_config(revision_baseline_path=str(tmp_path / "gone.pdf"))
    with pytest.raises(SystemExit):
        _validate_revision_inputs(cfg)


def test_baseline_env_var_reaches_the_config(monkeypatch):
    """CI callers set config through PEERREVIEW_* rather than flags."""
    monkeypatch.setenv("PEERREVIEW_REVISION_BASELINE_PATH", "/ci/prior-draft.pdf")
    assert get_config()["revision_baseline_path"] == "/ci/prior-draft.pdf"


def test_unloadable_prior_round_fails_loudly(tmp_path):
    """Silently downgrading to a fresh review would misinform the authors."""
    from peerreviewagents.graph.review_graph import PeerReviewGraph

    graph = PeerReviewGraph(get_config(revision_of="nonexistent", output_dir=str(tmp_path)))
    with pytest.raises(FileNotFoundError):
        graph.initial_state("tests/sample_manuscript.md")
