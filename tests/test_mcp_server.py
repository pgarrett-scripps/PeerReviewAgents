from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from peerreviewagents import review_service
from peerreviewagents.engine.bundle import ReviewPaths
from peerreviewagents.mcp import server as mcp_server


def _wait(service: mcp_server.ReviewService, job_id: str) -> dict:
    for _ in range(200):
        status = service.get_status(job_id)
        if status["status"] in ("done", "done-without-findings", "error", "cancelled"):
            return status
        time.sleep(0.01)
    raise AssertionError("background review did not finish")


def _pdf(path: Path) -> Path:
    path.write_bytes(b"%PDF-1.7\nnot a real manuscript\n")
    return path


def test_the_server_runs_one_engine_review_and_exposes_its_report(tmp_path, monkeypatch):
    """A tool call is the one-command engine review: the PDF is imported, the
    run lands beside it, and REVIEW.md is what the client reads back."""
    manuscript = _pdf(tmp_path / "paper.pdf")
    reviews = tmp_path / "reviews"
    planned: dict = {}

    def fake_plan(paper, output):
        bundle = Path(output)
        (bundle / "manuscript").mkdir(parents=True)
        (bundle / "manuscript" / "paper.json").write_text("{}", encoding="utf-8")
        planned["paper"], planned["bundle"] = str(paper), str(bundle)
        return ReviewPaths(bundle / "manuscript", bundle / "review", bundle)

    def fake_run(paths, options):
        planned["options"] = options
        paths.run.mkdir(parents=True)
        (paths.run / "progress.json").write_text(
            json.dumps({"status": "complete", "stage": "debate"}), encoding="utf-8"
        )
        report = paths.bundle / "REVIEW.md"
        report.write_text("# Review", encoding="utf-8")
        return {"progress": {"status": "complete"}, "reports": {"markdown": str(report)},
                "failure": None}

    monkeypatch.setattr(review_service, "plan_review", fake_plan)
    monkeypatch.setattr(review_service, "run_review_options", fake_run)
    service = mcp_server.ReviewService(
        max_workers=1, input_roots=[tmp_path], output_root=reviews
    )

    started = service.start_review(str(manuscript), output_name="run-1", provider="codex")
    status = _wait(service, started["job_id"])

    assert status["status"] == "done"
    assert status["progress"]["status"] == "complete"
    assert planned["paper"] == str(manuscript)
    assert planned["bundle"] == str(reviews / "run-1")
    assert planned["options"]["model"] == "gpt-5.6-terra"
    assert planned["options"]["policy"] == "agentic-v1"
    assert [item["name"] for item in service.list_artifacts(started["job_id"])] == [
        "REVIEW.md",
        "review/progress.json",
    ]
    assert service.read_artifact(started["job_id"], "REVIEW.md") == "# Review"


def test_a_stopped_review_is_reported_as_an_error_with_its_cause(tmp_path, monkeypatch):
    manuscript = _pdf(tmp_path / "paper.pdf")

    def fake_plan(paper, output):
        return ReviewPaths(Path(output) / "manuscript", Path(output) / "review", Path(output))

    monkeypatch.setattr(review_service, "plan_review", fake_plan)
    monkeypatch.setattr(
        review_service,
        "run_review_options",
        lambda paths, options: {
            "progress": {"status": "blocked"},
            "reports": {"markdown": str(paths.bundle / "REVIEW.md")},
            "failure": "RuntimeError: usage is unknown",
        },
    )
    service = mcp_server.ReviewService(
        max_workers=1, input_roots=[tmp_path], output_root=tmp_path / "reviews"
    )

    status = _wait(service, service.start_review(str(manuscript))["job_id"])

    assert status["status"] == "error"
    assert status["errors"] == ["review did not finish: RuntimeError: usage is unknown"]
    assert status["reports"] == ["REVIEW.md"]


def test_a_review_that_endorses_nothing_says_so_instead_of_claiming_a_review(
    tmp_path, monkeypatch
):
    manuscript = _pdf(tmp_path / "paper.pdf")
    monkeypatch.setattr(
        review_service,
        "plan_review",
        lambda paper, output: ReviewPaths(
            Path(output) / "manuscript", Path(output) / "review", Path(output)
        ),
    )
    monkeypatch.setattr(
        review_service,
        "run_review_options",
        lambda paths, options: {
            "progress": {"status": "complete-without-findings"},
            "reports": {},
            "failure": None,
        },
    )
    service = mcp_server.ReviewService(
        max_workers=1, input_roots=[tmp_path], output_root=tmp_path / "reviews"
    )

    status = _wait(service, service.start_review(str(manuscript))["job_id"])

    assert status["status"] == "done-without-findings"
    assert status["errors"] and "no" in status["errors"][0].lower()


def test_the_engine_refusals_reach_the_client_in_the_engine_words(tmp_path):
    manuscript = _pdf(tmp_path / "paper.pdf")
    service = mcp_server.ReviewService(max_workers=1, input_roots=[tmp_path])

    with pytest.raises(ValueError, match="unknown review mode"):
        service.start_review(str(manuscript), mode="journal-panel")
    with pytest.raises(ValueError, match="1 to 16"):
        service.start_review(str(manuscript), reviewers=99)
    with pytest.raises(ValueError, match="only to ensemble mode"):
        service.start_review(str(manuscript), mode="panel", reviewers=3)
    with pytest.raises(ValueError, match="one long investigative session"):
        service.start_review(str(manuscript), policy="workspace-agentic-v1")
    with pytest.raises(ValueError, match="not a claude-code model"):
        service.start_review(str(manuscript), provider="claude-code", model="gpt-5.6-terra")
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        service.start_review(str(manuscript), literature_cutoff="last tuesday")


def test_the_legacy_panel_parameters_are_gone_rather_than_accepted_and_ignored(tmp_path):
    """Target journals, article types, strictness, debate rounds and the
    research agent belonged to the LangGraph panel. The engine has no
    equivalent, so naming one is an error a client can see."""
    manuscript = _pdf(tmp_path / "paper.pdf")
    service = mcp_server.ReviewService(max_workers=1, input_roots=[tmp_path])
    for gone in ("target_journal", "article_type", "strictness", "debate_rounds",
                 "research_enabled", "enable_debate"):
        with pytest.raises(TypeError):
            service.start_review(str(manuscript), **{gone: 1})
    assert not hasattr(service, "journals")


def test_review_service_rejects_missing_input(tmp_path):
    service = mcp_server.ReviewService(max_workers=1, input_roots=[tmp_path])
    with pytest.raises(FileNotFoundError, match="manuscript not found"):
        service.start_review(str(tmp_path / "missing.pdf"))


def test_review_service_confines_inputs_and_output(tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = _pdf(tmp_path / "outside.pdf")
    inside = _pdf(allowed / "paper.pdf")
    service = mcp_server.ReviewService(
        max_workers=1, input_roots=[allowed], output_root=tmp_path / "reviews"
    )

    with pytest.raises(PermissionError, match="outside"):
        service.start_review(str(outside))
    with pytest.raises(PermissionError, match="output root"):
        service.start_review(str(inside), output_name="../escaped")


def test_artifact_reader_blocks_path_traversal_and_the_imported_manuscript(tmp_path):
    service = mcp_server.ReviewService(max_workers=1)
    bundle = tmp_path / "bundle"
    (bundle / "manuscript").mkdir(parents=True)
    (bundle / "manuscript" / "original.pdf").write_bytes(b"%PDF-1.7\n")
    (tmp_path / "secret.txt").write_text("private", encoding="utf-8")
    job = mcp_server.ReviewJob("job", "paper.pdf", {}, bundle=str(bundle))
    service._jobs[job.id] = job

    with pytest.raises(FileNotFoundError):
        service.read_artifact(job.id, "../secret.txt")
    with pytest.raises(FileNotFoundError):
        service.read_artifact(job.id, "manuscript/original.pdf")
    assert service.list_artifacts(job.id) == []


def test_public_job_state_does_not_expose_absolute_paths(tmp_path):
    manuscript = str(tmp_path / "paper.pdf")
    bundle = str(tmp_path / "reviews" / "run-1")
    job = mcp_server.ReviewJob(
        "job",
        manuscript,
        {"mode": "ensemble", "policy": "agentic-v1"},
        bundle=bundle,
        reports=[bundle + "/REVIEW.md"],
        errors=[f"failed to read {manuscript} and write {bundle}"],
    )
    public = job.public()
    assert public["manuscript_name"] == "paper.pdf"
    assert "manuscript_path" not in public and "bundle" not in public
    assert str(tmp_path) not in public["errors"][0]
    assert public["reports"] == ["REVIEW.md"]
    assert public["review_mode"] == "ensemble"


def test_cancelling_a_running_review_says_the_call_cannot_be_interrupted(tmp_path):
    service = mcp_server.ReviewService(max_workers=1)
    job = mcp_server.ReviewJob("job", "paper.pdf", {}, status="running")
    service._jobs[job.id] = job
    state = service.cancel(job.id)
    assert state["cancelled"] is False
    assert "cannot be interrupted" in state["note"]
    assert state["status"] == "running"


def test_the_server_advertises_the_engine_capabilities(tmp_path):
    service = mcp_server.ReviewService(max_workers=1, input_roots=[tmp_path])
    record = service.capabilities()
    assert record["default_policy"] == "agentic-v1"
    assert record["default_provider"] == "claude-code"
    assert record["review_modes"] == ["ensemble", "independent", "panel"]
    assert "docs/study-arms.md" in record["study_arms"]
    assert record["input_roots"] == [tmp_path.name]


def test_all_agent_packages_share_the_same_skill():
    root = Path(__file__).resolve().parents[1]
    claude_skill = root / "skills" / "peer-review-manuscript" / "SKILL.md"
    codex_skill = (
        root
        / "plugins"
        / "peer-review-agents"
        / "skills"
        / "peer-review-manuscript"
        / "SKILL.md"
    )
    pi_skill = (
        root
        / "integrations"
        / "pi"
        / "skills"
        / "peer-review-manuscript"
        / "SKILL.md"
    )
    assert claude_skill.read_text(encoding="utf-8") == codex_skill.read_text(encoding="utf-8")
    assert claude_skill.read_text(encoding="utf-8") == pi_skill.read_text(encoding="utf-8")


def test_the_skill_describes_the_engine_review_the_server_actually_runs():
    """The skill told agents to pick a target journal, keep research_enabled
    false and read summary.md and decision_letter.md. None of those exist."""
    root = Path(__file__).resolve().parents[1]
    skill = (root / "skills" / "peer-review-manuscript" / "SKILL.md").read_text(encoding="utf-8")
    for gone in ("target_journal", "research_enabled", "article_type", "summary.md",
                 "decision_letter.md", "provider: pi", "API key", "list_peer_review_journals"):
        assert gone not in skill
    for named in ("start_peer_review", "get_peer_review_status", "list_peer_review_artifacts",
                  "read_peer_review_artifact", "cancel_peer_review", "REVIEW.md",
                  "done-without-findings", 'provider: "codex"', "literature_cutoff"):
        assert named in skill


def test_local_plugin_manifests_and_pi_package_are_well_formed():
    root = Path(__file__).resolve().parents[1]
    claude_marketplace = json.loads(
        (root / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8")
    )
    codex_marketplace = json.loads(
        (root / ".agents" / "plugins" / "marketplace.json").read_text(encoding="utf-8")
    )
    pi_package = json.loads(
        (root / "integrations" / "pi" / "package.json").read_text(encoding="utf-8")
    )

    assert claude_marketplace["plugins"][0]["source"] == "./plugins/peer-review-agents"
    assert codex_marketplace["plugins"][0]["source"]["path"] == (
        "./plugins/peer-review-agents"
    )
    assert pi_package["pi"]["extensions"] == ["./peer-review-mcp.ts"]
    assert pi_package["pi"]["skills"] == ["./skills/peer-review-manuscript"]


def test_review_service_bounds_retained_job_state(tmp_path):
    service = mcp_server.ReviewService(
        max_workers=1,
        input_roots=[tmp_path],
        max_jobs=2,
    )
    old = mcp_server.ReviewJob("old", "old.pdf", {}, finished_at=1.0)
    current = mcp_server.ReviewJob("current", "current.pdf", {})
    service._jobs = {old.id: old, current.id: current}
    with service._lock:
        service._prune_finished_locked()
    assert set(service._jobs) == {current.id}
