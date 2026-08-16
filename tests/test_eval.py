"""Tests for the evaluation harness: parsing, stats, and report assembly.

Everything here is network- and LLM-free — the OpenReview client is never
touched (we feed the pure extraction functions fake note objects), and the
runner isn't exercised (it needs a live pipeline)."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from peerreviewagents.eval import baseline as B
from peerreviewagents.eval import corpus as C
from peerreviewagents.eval import metrics as M
from peerreviewagents.eval.comparison import build_comparison
from peerreviewagents.eval.comparison import render_markdown as render_comparison
from peerreviewagents.eval.integrity import (
    EXPECTED_AUDITORS,
    EXPECTED_REVIEWERS,
    inspect_run_artifacts,
)
from peerreviewagents.eval.runner import existing_keys, weighted_score
from peerreviewagents.eval.schema import (
    CorpusItem,
    RunRecord,
    config_digest,
    source_fingerprint,
    verify_protocol,
)

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
        artifact_integrity_ok=True,
        manifest={"model": "x"},
    )
    back = RunRecord.from_dict(json.loads(rec.to_json()))
    assert back.key == ("p1", 0)
    assert back.system_decision == "minor"
    assert back.manifest["model"] == "x"
    assert back.artifact_integrity_ok is True


def _complete_system_record() -> RunRecord:
    prose = "A specific, substantive evaluation of the manuscript. " * 3
    return RunRecord(
        paper_id="p1",
        repeat=0,
        ok=True,
        system_decision="major",
        system_weighted_score=3.0,
        per_reviewer=[
            {"name": name, "score": 3, "confidence": 4, "markdown": prose}
            for name in sorted(EXPECTED_REVIEWERS)
        ],
        decision_letter=prose,
        audit_markdown=[
            {"auditor": name, "markdown": prose}
            for name in sorted(EXPECTED_AUDITORS)
        ],
        n_reviewers=8,
        manifest={"mode": "system"},
    )


def test_artifact_integrity_accepts_complete_prose_panel():
    assert inspect_run_artifacts(_complete_system_record()) == []


def test_artifact_integrity_rejects_prompt_echo_and_missing_reviewer():
    rec = _complete_system_record()
    rec.per_reviewer.pop()
    rec.n_reviewers = 7
    rec.decision_letter += "\n=== MANUSCRIPT ===\n"
    problems = inspect_run_artifacts(rec)
    assert any("missing reviewers" in problem for problem in problems)
    assert any("expected 8" in problem for problem in problems)
    assert any("internal prompt marker" in problem for problem in problems)


def test_artifact_integrity_accepts_schema_free_single_llm_markdown():
    rec = RunRecord(
        paper_id="p1", repeat=0, ok=True,
        system_decision="minor", system_weighted_score=4.0,
        per_reviewer=[{
            "name": "single_llm", "score": 4, "confidence": 5,
            "markdown": "SCORE: 4\nVERDICT: minor\n\n" + "Substantive assessment. " * 5,
        }],
        n_reviewers=1,
        manifest={"mode": "single-llm"},
    )
    assert inspect_run_artifacts(rec) == []


def test_baseline_score_deterministically_defines_decision():
    score, decision, cost, warnings = B._parse_metadata(
        None, {}, "SCORE: 4\nVERDICT: accept\n\n" + "Substantive review. " * 20,
    )
    assert (score, decision, cost) == (4, "minor", 0.0)
    assert any("conflicted" in warning for warning in warnings)


def test_config_digest_stable_and_selective():
    a = {"provider": "openrouter", "reasoning_model": "m", "unrelated": 1}
    b = {"provider": "openrouter", "reasoning_model": "m", "unrelated": 999}
    c = {"provider": "openai", "reasoning_model": "m"}
    assert config_digest(a) == config_digest(b)   # ignores non-result keys
    assert config_digest(a) != config_digest(c)   # provider matters


def test_config_digest_includes_evaluation_controls():
    base = {"provider": "openrouter", "reasoning_model": "m"}
    assert config_digest(base) != config_digest({**base, "single_model": True})
    assert config_digest(base) != config_digest({**base, "research_enabled": False})
    assert config_digest(base) != config_digest({**base, "enable_journal_recommender": False})


def test_frozen_protocol_rejects_changed_run_config(tmp_path):
    corpus_path = tmp_path / "corpus.jsonl"
    corpus_path.write_text("", encoding="utf-8")
    config = {"provider": "openrouter", "reasoning_model": "model-a"}
    (tmp_path / "protocol.json").write_text(
        json.dumps({"config_digest": config_digest(config)}), encoding="utf-8"
    )
    assert verify_protocol(str(corpus_path), config) is not None
    with pytest.raises(ValueError, match="differs from frozen protocol"):
        verify_protocol(str(corpus_path), {**config, "reasoning_model": "model-b"})


def test_source_fingerprint_is_stable_and_present():
    first = source_fingerprint()
    assert len(first) == 16
    assert first == source_fingerprint()


def test_resume_refuses_changed_config_or_mode(tmp_path):
    runs = tmp_path / "runs.jsonl"
    config = {"provider": "p", "reasoning_model": "m", "research_enabled": False}
    _write_runs(runs, [
        RunRecord("p1", 0, True, "accept", 4, manifest={
            "config_digest": config_digest(config), "mode": "system",
            "source_fingerprint": source_fingerprint(),
        }),
    ])
    assert existing_keys(str(runs), config=config, mode="system") == {("p1", 0)}
    with pytest.raises(ValueError, match="requested config"):
        existing_keys(str(runs), config={**config, "research_enabled": True}, mode="system")
    with pytest.raises(ValueError, match="not single-llm"):
        existing_keys(str(runs), config=config, mode="single-llm")

    # Failed attempts remain in the report, so they must not come from another
    # configuration either.
    _write_runs(runs, [
        RunRecord("p1", 0, False, None, None, manifest={
            "config_digest": "other", "mode": "system",
            "source_fingerprint": source_fingerprint(),
        }),
    ])
    with pytest.raises(ValueError, match="requested config"):
        existing_keys(str(runs), config=config, mode="system")


def test_resume_refuses_changed_frozen_corpus(tmp_path):
    runs = tmp_path / "runs.jsonl"
    config = {"provider": "p", "reasoning_model": "m"}
    _write_runs(runs, [
        RunRecord("p1", 0, True, "accept", 4, manifest={
            "config_digest": config_digest(config),
            "mode": "system",
            "corpus_sha256": "old-corpus",
            "source_fingerprint": source_fingerprint(),
        }),
    ])
    with pytest.raises(ValueError, match="currently frozen corpus"):
        existing_keys(
            str(runs), config=config, mode="system", corpus_sha256="new-corpus",
        )


def test_corpus_manifest_detects_pdf_and_label_drift(tmp_path):
    pdf_dir = tmp_path / "pdfs"
    pdf_dir.mkdir()
    pdf = pdf_dir / "p1.pdf"
    pdf.write_bytes(b"fake-pdf-v1")
    corpus_path = tmp_path / "corpus.jsonl"
    _write_corpus(corpus_path, [
        CorpusItem(id="p1", title="A", pdf_path="pdfs/p1.pdf",
                   human_mean=8.0, human_decision="accept"),
    ])
    manifest = C.write_corpus_manifest(str(corpus_path), venue="test")
    assert manifest.endswith("corpus_manifest.json")
    assert C.verify_corpus_manifest(str(corpus_path))["n_papers"] == 1

    pdf.write_bytes(b"fake-pdf-v2")
    with pytest.raises(RuntimeError, match="PDF changed"):
        C.verify_corpus_manifest(str(corpus_path))


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
    assert agr["decision_balanced_accuracy"] == 1.0
    assert agr["decision_cohen_kappa"] == 1.0
    assert agr["confidence_intervals"]["decision_accuracy"] == [1.0, 1.0]

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


def test_per_run_manifest_timestamps_do_not_create_fake_mixed_configs(tmp_path):
    corpus_path = tmp_path / "corpus.jsonl"
    runs_path = tmp_path / "runs.jsonl"
    _write_corpus(corpus_path, [
        CorpusItem(id="p1", title="A", pdf_path="x", human_mean=8, human_decision="accept"),
        CorpusItem(id="p2", title="B", pdf_path="x", human_mean=2, human_decision="reject"),
    ])
    common = {"config_digest": "same", "provider": "p", "model": "m", "mode": "system"}
    _write_runs(runs_path, [
        RunRecord("p1", 0, True, "accept", 4, manifest={**common, "created_at": 1.0}),
        RunRecord("p2", 0, True, "reject", 2, manifest={**common, "created_at": 2.0}),
    ])
    assert M.build_report(str(corpus_path), str(runs_path))["distinct_configs"] == 1


def test_paired_comparison_reports_delta_and_compute_caveat(tmp_path):
    corpus_path = tmp_path / "corpus.jsonl"
    system_path = tmp_path / "system.jsonl"
    baseline_path = tmp_path / "baseline.jsonl"
    _write_corpus(corpus_path, [
        CorpusItem(id="p1", title="A", pdf_path="x", human_mean=8, human_decision="accept"),
        CorpusItem(id="p2", title="B", pdf_path="x", human_mean=7, human_decision="accept"),
        CorpusItem(id="p3", title="C", pdf_path="x", human_mean=3, human_decision="reject"),
        CorpusItem(id="p4", title="D", pdf_path="x", human_mean=2, human_decision="reject"),
    ])
    system_manifest = {"model": "m", "provider": "p", "config_digest": "x", "mode": "system"}
    baseline_manifest = {
        "model": "m", "provider": "p", "config_digest": "x", "mode": "single-llm",
    }
    _write_runs(system_path, [
        RunRecord("p1", 0, True, "accept", 5, cost_usd=2, latency_s=20,
                  manifest=system_manifest),
        RunRecord("p2", 0, True, "minor", 4, cost_usd=2, latency_s=20,
                  manifest=system_manifest),
        RunRecord("p3", 0, True, "major", 2, cost_usd=2, latency_s=20,
                  manifest=system_manifest),
        RunRecord("p4", 0, True, "reject", 1, cost_usd=2, latency_s=20,
                  manifest=system_manifest),
    ])
    _write_runs(baseline_path, [
        RunRecord("p1", 0, True, "accept", 4, cost_usd=.1, latency_s=2,
                  manifest=baseline_manifest),
        RunRecord("p2", 0, True, "reject", 2, cost_usd=.1, latency_s=2,
                  manifest=baseline_manifest),
        RunRecord("p3", 0, True, "minor", 4, cost_usd=.1, latency_s=2,
                  manifest=baseline_manifest),
        RunRecord("p4", 0, True, "reject", 1, cost_usd=.1, latency_s=2,
                  manifest=baseline_manifest),
    ])
    with pytest.warns(UserWarning, match="No corpus_manifest"):
        report = build_comparison(str(corpus_path), str(system_path), str(baseline_path))
    assert report["n_common_papers"] == 4
    assert report["paired_deltas_system_minus_single_llm"]["decision_accuracy"]["estimate"] > 0
    md = render_comparison(report)
    assert "not compute-matched causal ablation" in md
    assert "Mean cost" in md


# ---------- runner bookkeeping (fake graph, no LLM) ---------------------------


def _run_one_with_state(monkeypatch, state):
    from peerreviewagents.eval import runner as R

    class FakeGraph:
        def __init__(self, config):
            pass

        def review(self, pdf_path):
            return state

    monkeypatch.setattr(R, "PeerReviewGraph", FakeGraph)
    prose = "A complete and substantive saved evaluation artifact. " * 3
    reports = state.get("reports") or []
    present = {report.get("reviewer") for report in reports}
    for report in reports:
        report.setdefault("body", prose)
    for name in sorted(EXPECTED_REVIEWERS - present):
        reports.append({
            "reviewer": name, "score": None, "confidence": 3.0,
            "not_applicable_reason": "synthetic test abstention", "body": prose,
        })
    state["reports"] = reports
    state.setdefault("decision_letter", prose)
    state.setdefault("audits", [
        {"auditor": name, "body": prose} for name in sorted(EXPECTED_AUDITORS)
    ])
    state.setdefault("panel_complete", True)
    state.setdefault("panel_degraded", False)
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


def test_run_one_rejects_decision_with_corrupted_artifact(monkeypatch):
    rec = _run_one_with_state(monkeypatch, {
        "reports": [{
            "reviewer": "rigor", "score": 3.0, "confidence": 4.0,
            "body": "=== MANUSCRIPT ===\n" + "echoed prompt material " * 8,
        }],
        "decision": "major",
    })
    assert not rec.ok
    assert rec.artifact_integrity_ok is False
    assert any("internal prompt marker" in e for e in rec.artifact_integrity_errors)


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
