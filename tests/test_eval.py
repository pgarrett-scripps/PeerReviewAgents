"""Tests for the evaluation harness: parsing, stats, and report assembly.

Everything here is network- and LLM-free — the OpenReview client is never
touched (we feed the pure extraction functions fake note objects), and the
runner isn't exercised (it needs a live pipeline)."""

from __future__ import annotations

import json
from types import SimpleNamespace

from peerreviewagents.eval import corpus as C
from peerreviewagents.eval import metrics as M
from peerreviewagents.eval.runner import weighted_score
from peerreviewagents.eval.schema import CorpusItem, RunRecord, config_digest

# ---------- corpus extraction ----------------------------------------------


def test_parse_rating_variants():
    assert C.parse_rating(8) == 8.0
    assert C.parse_rating("8") == 8.0
    assert C.parse_rating("8: accept, good paper") == 8.0
    assert C.parse_rating("6.5") == 6.5
    assert C.parse_rating("8/10") == 8.0
    assert C.parse_rating("no number") is None
    assert C.parse_rating(None) is None


def test_normalize_decision():
    assert C.normalize_decision("Accept (Poster)") == "accept"
    assert C.normalize_decision("Spotlight") == "accept"
    assert C.normalize_decision("Reject") == "reject"
    assert C.normalize_decision("") is None
    assert C.normalize_decision("Borderline") is None


def test_normalize_decision_withdrawn_is_not_a_reject():
    """A withdrawal is the authors' act, not the reviewers' verdict — it must
    come back as its own status so the paper is skipped, matching the
    submission-field path's documented exclusion."""
    assert C.normalize_decision("Withdrawn") == "withdrawn"
    assert C.normalize_decision("ICLR 2025 Conference Withdrawn Submission") == "withdrawn"
    # "Desk Rejected" contains "reject" too; the desk status must win.
    assert C.normalize_decision("Desk Rejected Submission") == "desk_reject"


def test_cval_handles_v1_and_v2():
    assert C.cval({"title": {"value": "X"}}, "title") == "X"   # API v2
    assert C.cval({"title": "X"}, "title") == "X"               # API v1
    assert C.cval({}, "title") is None


def _review(rating):
    return SimpleNamespace(
        id="r1",
        invitations=["ICLR.cc/2025/Conference/Submission1/-/Official_Review"],
        content={"rating": {"value": rating}},
    )


def _decision(label):
    return SimpleNamespace(
        id="d1",
        invitations=["ICLR.cc/2025/Conference/Submission1/-/Decision"],
        content={"decision": {"value": label}},
    )


def _meta_review(label):
    return SimpleNamespace(
        id="m1",
        invitations=["ICLR.cc/2025/Conference/Submission1/-/Meta_Review"],
        content={"recommendation": {"value": label}},
    )


def test_extract_scores_and_decision():
    replies = [_review("8: accept"), _review("6"), _decision("Accept (Oral)")]
    assert C.extract_scores(replies) == [8.0, 6.0]
    assert C.extract_decision(replies) == ("accept", "Accept (Oral)")


def test_extract_decision_prefers_decision_note_over_meta_review():
    """The AC's recommendation is input to the decision, not the decision —
    the chairs can and do overrule it, so a Decision note wins regardless of
    reply order."""
    replies = [_meta_review("Accept"), _decision("Reject")]
    assert C.extract_decision(replies) == ("reject", "Reject")


def test_extract_decision_falls_back_to_meta_review_when_no_decision_note():
    assert C.extract_decision([_meta_review("Accept")]) == ("accept", "Accept")


def test_extract_decision_returns_withdrawn_sentinel():
    """A Decision note announcing a withdrawal must positively exclude the
    paper — not fall through to some other note's recommendation, and never
    label the paper a ground-truth reject."""
    replies = [_decision("Withdrawn"), _meta_review("Accept")]
    assert C.extract_decision(replies) == ("withdrawn", "Withdrawn")


def test_extract_ignores_non_review_notes():
    comment = SimpleNamespace(id="c1", invitations=["…/-/Comment"], content={"comment": {"value": "hi"}})
    assert C.extract_scores([comment]) == []
    assert C.extract_decision([comment]) == (None, "")


def test_extract_scores_respects_pinned_field():
    # A venue whose rating lives in a non-default field name.
    note = SimpleNamespace(
        id="r1",
        invitations=["X/-/Official_Review"],
        content={"score": {"value": "7"}, "summary": {"value": "prose"}},
    )
    assert C.extract_scores([note]) == []                       # default fields miss
    assert C.extract_scores([note], ("score",)) == [7.0]        # pinned field hits


def _sub(venue="", venueid=""):
    return SimpleNamespace(content={"venue": {"value": venue}, "venueid": {"value": venueid}})


def test_submission_status_classification():
    assert C.submission_status(_sub(venue="ICLR 2025 Poster"))[0] == "accept"
    assert C.submission_status(_sub(venue="ICLR 2025 Oral"))[0] == "accept"
    assert C.submission_status(_sub(venue="Submitted to ICLR 2025"))[0] == "reject"
    assert C.submission_status(_sub(venueid=".../Rejected_Submission"))[0] == "reject"
    assert C.submission_status(_sub(venue="ICLR 2025 Conference Withdrawn Submission"))[0] == "withdrawn"
    assert C.submission_status(_sub(venueid=".../Desk_Rejected_Submission"))[0] == "desk_reject"
    assert C.submission_status(_sub())[0] == "unknown"


def test_decision_from_submission_excludes_withdrawn():
    # accept/reject map through; withdrawn/desk/unknown become None (excluded)
    assert C.decision_from_submission(_sub(venue="ICLR 2025 Poster"))[0] == "accept"
    assert C.decision_from_submission(_sub(venue="Submitted to ICLR 2025"))[0] == "reject"
    assert C.decision_from_submission(_sub(venue="ICLR 2025 Withdrawn Submission"))[0] is None
    assert C.decision_from_submission(_sub())[0] is None


def test_summarize_fields_lists_what_exists():
    replies = [
        SimpleNamespace(id="r1", invitations=["X/-/Official_Review"],
                        content={"rating": {"value": "8: accept"}, "confidence": {"value": "4"}}),
        SimpleNamespace(id="d1", invitations=["X/-/Decision"],
                        content={"decision": {"value": "Reject"}}),
    ]
    s = C.summarize_fields(replies)
    assert s["n_reviews"] == 1 and s["n_decisions"] == 1
    assert set(s["review_fields"]) == {"rating", "confidence"}
    assert s["review_fields"]["rating"] == "8: accept"
    assert s["decision_fields"]["decision"] == "Reject"


# ---------- pure statistics --------------------------------------------------


def test_pearson_perfect_positive():
    assert M.pearson([1, 2, 3], [2, 4, 6]) == 1.0


def test_spearman_monotonic_is_one():
    assert M.spearman([1, 2, 3, 4], [10, 20, 25, 40]) == 1.0


def test_spearman_handles_ties():
    # ranks of [1,1,2] are [1.5,1.5,3]; against strictly increasing y → ~1
    rho = M.spearman([1, 1, 2], [5, 6, 9])
    assert rho is not None and rho > 0.8


def test_cohen_kappa_perfect_and_inverse():
    assert M.cohen_kappa(["accept", "reject"], ["accept", "reject"]) == 1.0
    assert M.cohen_kappa(["accept", "reject"], ["reject", "accept"]) == -1.0


def test_cohen_kappa_single_class_is_undefined_not_perfect():
    """pe == 1 makes kappa 0/0. Reporting 1.0 scored a rubber stamp on an
    all-accept corpus as flawless agreement; the honest answer is 'the data
    cannot say'."""
    assert M.cohen_kappa(["accept"] * 10, ["accept"] * 10) is None
    assert M.cohen_kappa([], []) is None


def test_confusion_counts():
    cf = M.confusion(["accept", "accept", "reject"], ["accept", "reject", "reject"])
    assert cf["accept__accept"] == 1
    assert cf["accept__reject"] == 1
    assert cf["reject__reject"] == 1
    assert cf["reject__accept"] == 0


def test_system_binary_mapping():
    assert M.system_binary("accept") == "accept"
    assert M.system_binary("minor") == "accept"
    assert M.system_binary("major") == "reject"
    assert M.system_binary("reject") == "reject"
    assert M.system_binary(None) is None


# ---------- weighted score ---------------------------------------------------


def test_weighted_score_matches_confidence_weighting():
    reports = [
        {"reviewer": "a", "score": 4, "confidence": 5},
        {"reviewer": "b", "score": 2, "confidence": 1},
    ]
    # (4*5 + 2*1) / (5+1) = 22/6 = 3.6667
    assert weighted_score(reports) == 3.6667
    assert weighted_score([]) is None


def test_weighted_score_skips_null_score_reviewers():
    """Scores are nullable by design (a dimension with nothing to judge
    abstains). The abstention drops out of the average — mirroring
    score_summary — instead of crashing the batch on None * confidence."""
    reports = [
        {"reviewer": "a", "score": 4, "confidence": 5},
        {"reviewer": "ethics", "score": None, "confidence": 3,
         "not_applicable_reason": "no human subjects"},
        {"reviewer": "b", "score": 2, "confidence": 1},
    ]
    assert weighted_score(reports) == 3.6667  # same as without the abstainer


def test_weighted_score_all_abstained_is_none():
    reports = [{"reviewer": "ethics", "score": None, "confidence": 3}]
    assert weighted_score(reports) is None


# ---------- record round-trips ----------------------------------------------


def test_run_record_roundtrip():
    rec = RunRecord(
        paper_id="p1", repeat=0, ok=True,
        system_decision="minor", system_weighted_score=3.5,
        per_reviewer=[{"name": "rigor", "score": 4, "confidence": 3}],
        n_reviewers=1, cost_usd=0.12, latency_s=42.0, errors=[],
        manifest={"model": "x"},
    )
    back = RunRecord.from_dict(json.loads(rec.to_json()))
    assert back.key == ("p1", 0)
    assert back.system_decision == "minor"
    assert back.manifest["model"] == "x"


def test_config_digest_stable_and_selective():
    a = {"provider": "openrouter", "reasoning_model": "m", "unrelated": 1}
    b = {"provider": "openrouter", "reasoning_model": "m", "unrelated": 999}
    c = {"provider": "openai", "reasoning_model": "m"}
    assert config_digest(a) == config_digest(b)   # ignores non-result keys
    assert config_digest(a) != config_digest(c)   # provider matters


# ---------- report assembly (synthetic corpus + runs) ------------------------


def _write_corpus(path, items):
    with open(path, "w", encoding="utf-8") as fh:
        for it in items:
            fh.write(it.to_json() + "\n")


def _write_runs(path, recs):
    with open(path, "w", encoding="utf-8") as fh:
        for r in recs:
            fh.write(r.to_json() + "\n")


def test_agreement_and_consistency_end_to_end(tmp_path):
    corpus_path = tmp_path / "corpus.jsonl"
    runs_path = tmp_path / "runs.jsonl"
    _write_corpus(corpus_path, [
        CorpusItem(id="p1", title="A", pdf_path="x", human_mean=8.0, human_decision="accept"),
        CorpusItem(id="p2", title="B", pdf_path="x", human_mean=6.0, human_decision="accept"),
        CorpusItem(id="p3", title="C", pdf_path="x", human_mean=3.0, human_decision="reject"),
    ])

    def rec(pid, rep, dec, score, ok=True):
        return RunRecord(paper_id=pid, repeat=rep, ok=ok, system_decision=dec,
                         system_weighted_score=score, manifest={"model": "m"})

    _write_runs(runs_path, [
        rec("p1", 0, "accept", 4.5),
        rec("p2", 0, "minor", 4.0),
        rec("p3", 0, "reject", 2.0),
        # p1 re-run twice for consistency
        rec("p1", 1, "accept", 4.4),
        rec("p1", 2, "minor", 4.6),
    ])

    report = M.build_report(str(corpus_path), str(runs_path))
    agr, con = report["agreement"], report["consistency"]

    # monotone scores + correct binary verdicts → perfect agreement
    assert agr["n_scored_papers"] == 3
    assert agr["score_spearman"] == 1.0
    assert agr["decision_accuracy"] == 1.0
    assert agr["decision_cohen_kappa"] == 1.0

    # only p1 has multiple runs; not unanimous (accept,accept,minor)
    assert con["n_papers_multi_run"] == 1
    assert con["per_paper"][0]["paper_id"] == "p1"
    assert con["per_paper"][0]["unanimous"] is False
    assert con["per_paper"][0]["majority_frac"] == round(2 / 3, 3)

    md = M.render_markdown(report)
    assert "# Evaluation Report" in md and "Agreement with human reviewers" in md


def test_single_class_kappa_renders_as_na_not_perfect(tmp_path):
    """An all-accept corpus must render κ as n/a, not as agreement."""
    corpus_path = tmp_path / "corpus.jsonl"
    runs_path = tmp_path / "runs.jsonl"
    _write_corpus(corpus_path, [
        CorpusItem(id="p1", title="A", pdf_path="x", human_mean=8.0, human_decision="accept"),
        CorpusItem(id="p2", title="B", pdf_path="x", human_mean=6.0, human_decision="accept"),
    ])
    _write_runs(runs_path, [
        RunRecord(paper_id="p1", repeat=0, ok=True, system_decision="accept",
                  system_weighted_score=4.5, manifest={"model": "m"}),
        RunRecord(paper_id="p2", repeat=0, ok=True, system_decision="accept",
                  system_weighted_score=4.0, manifest={"model": "m"}),
    ])
    report = M.build_report(str(corpus_path), str(runs_path))
    assert report["agreement"]["decision_cohen_kappa"] is None
    md = M.render_markdown(report)
    assert "Cohen's κ = n/a" in md
    assert "Cohen's κ = None" not in md


def test_mixed_config_pooling_gets_a_loud_warning(tmp_path):
    """Pooling two configs into one runs file blends their numbers under one
    manifest line; the report must say so instead of passing the blend off
    as a single-configuration result."""
    corpus_path = tmp_path / "corpus.jsonl"
    runs_path = tmp_path / "runs.jsonl"
    _write_corpus(corpus_path, [
        CorpusItem(id="p1", title="A", pdf_path="x", human_mean=8.0, human_decision="accept"),
        CorpusItem(id="p2", title="B", pdf_path="x", human_mean=3.0, human_decision="reject"),
    ])
    _write_runs(runs_path, [
        RunRecord(paper_id="p1", repeat=0, ok=True, system_decision="accept",
                  system_weighted_score=4.5, manifest={"model": "m1", "config_digest": "aaa"}),
        RunRecord(paper_id="p2", repeat=0, ok=True, system_decision="reject",
                  system_weighted_score=2.0, manifest={"model": "m2", "config_digest": "bbb"}),
    ])
    report = M.build_report(str(corpus_path), str(runs_path))
    assert report["distinct_configs"] == 2
    md = M.render_markdown(report)
    assert "MIXED CONFIGS" in md and "2 distinct" in md

    # And a homogeneous file stays clean of the warning.
    _write_runs(runs_path, [
        RunRecord(paper_id="p1", repeat=0, ok=True, system_decision="accept",
                  system_weighted_score=4.5, manifest={"model": "m1"}),
        RunRecord(paper_id="p2", repeat=0, ok=True, system_decision="reject",
                  system_weighted_score=2.0, manifest={"model": "m1"}),
    ])
    md = M.render_markdown(M.build_report(str(corpus_path), str(runs_path)))
    assert "MIXED CONFIGS" not in md


# ---------- runner bookkeeping (fake graph, no LLM) ---------------------------


def _run_one_with_state(monkeypatch, state):
    from peerreviewagents.eval import runner as R

    class FakeGraph:
        def __init__(self, config):
            pass

        def review(self, pdf_path):
            return state

    monkeypatch.setattr(R, "PeerReviewGraph", FakeGraph)
    item = SimpleNamespace(id="p1", title="T", venue="V", pdf_path="x.pdf")
    return R._run_one(item, 0, {"provider": "test"}, "")


def test_run_one_survives_an_abstaining_reviewer(monkeypatch):
    """One null-score reviewer must not kill the batch: the run stays ok and
    the abstention is preserved in the record for later analysis."""
    rec = _run_one_with_state(monkeypatch, {
        "reports": [
            {"reviewer": "rigor", "score": 4.0, "confidence": 5.0,
             "weaknesses": ["w1", "w2"], "not_applicable_reason": ""},
            {"reviewer": "ethics", "score": None, "confidence": 3.0,
             "weaknesses": [], "not_applicable_reason": "no human subjects"},
        ],
        "decision": "minor",
        "total_cost": 1.25,
        "errors": [],
    })
    assert rec.ok
    assert rec.system_weighted_score == 4.0
    ethics = next(r for r in rec.per_reviewer if r["name"] == "ethics")
    assert ethics["score"] is None
    assert ethics["not_applicable_reason"] == "no human subjects"


def test_run_record_keeps_reviewer_weaknesses(monkeypatch):
    """Weakness-level overlap with human reviews is the project's endpoint;
    a record holding only name/score/confidence forecloses that analysis."""
    rec = _run_one_with_state(monkeypatch, {
        "reports": [{"reviewer": "rigor", "score": 3.0, "confidence": 4.0,
                     "weaknesses": ["unblinded raters", "n too small"],
                     "not_applicable_reason": ""}],
        "decision": "major",
    })
    back = RunRecord.from_dict(json.loads(rec.to_json()))
    assert back.per_reviewer[0]["weaknesses"] == ["unblinded raters", "n too small"]


def test_run_one_records_bookkeeping_failure_instead_of_crashing(monkeypatch):
    """A malformed state after a successful graph run is recorded like a graph
    failure — the batch moves on and the slot retries next invocation."""
    rec = _run_one_with_state(monkeypatch, {
        # confidence None: summing it raises inside the bookkeeping.
        "reports": [{"reviewer": "rigor", "score": 3.0, "confidence": None}],
        "decision": "minor",
    })
    assert not rec.ok
    assert any("bookkeeping failed" in e for e in rec.errors)


def test_old_run_records_without_weakness_fields_still_load():
    """Pre-existing runs.jsonl lines predate the added per_reviewer fields."""
    old = {"paper_id": "p", "repeat": 0, "ok": True, "system_decision": "accept",
           "system_weighted_score": 4.0,
           "per_reviewer": [{"name": "rigor", "score": 4, "confidence": 3}]}
    back = RunRecord.from_dict(old)
    assert back.per_reviewer[0]["name"] == "rigor"


def test_figure_renders_svg_and_png(tmp_path):
    import pytest
    pytest.importorskip("matplotlib")
    from peerreviewagents.eval.figure import make_figure

    corpus_path = tmp_path / "corpus.jsonl"
    runs_path = tmp_path / "runs.jsonl"
    _write_corpus(corpus_path, [
        CorpusItem(id="p1", title="A", pdf_path="x", human_mean=8.0, human_decision="accept"),
        CorpusItem(id="p2", title="B", pdf_path="x", human_mean=3.0, human_decision="reject"),
    ])

    def rec(pid, rep, dec, score):
        return RunRecord(paper_id=pid, repeat=rep, ok=True, system_decision=dec,
                         system_weighted_score=score, manifest={"model": "m"})

    _write_runs(runs_path, [
        rec("p1", 0, "accept", 4.5), rec("p2", 0, "reject", 2.0),
        rec("p1", 1, "accept", 4.4), rec("p1", 2, "minor", 4.6),
    ])

    svg, png = make_figure(str(corpus_path), str(runs_path), str(tmp_path / "fig"))
    assert svg.endswith(".svg") and png.endswith(".png")
    import os
    assert os.path.getsize(svg) > 0 and os.path.getsize(png) > 0
