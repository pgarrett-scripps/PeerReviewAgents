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
    assert C.normalize_decision("Withdrawn") == "reject"
    assert C.normalize_decision("") is None
    assert C.normalize_decision("Borderline") is None


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


def test_extract_scores_and_decision():
    replies = [_review("8: accept"), _review("6"), _decision("Accept (Oral)")]
    assert C.extract_scores(replies) == [8.0, 6.0]
    assert C.extract_decision(replies) == ("accept", "Accept (Oral)")


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
