from __future__ import annotations

import time
from pathlib import Path

import pytest

from peerreviewagents.mcp import server as mcp_server


def _wait(service: mcp_server.ReviewService, job_id: str) -> dict:
    for _ in range(100):
        status = service.get_status(job_id)
        if status["status"] in ("done", "error", "cancelled"):
            return status
        time.sleep(0.01)
    raise AssertionError("background review did not finish")


def test_review_service_runs_pipeline_and_exposes_artifacts(tmp_path, monkeypatch):
    manuscript = tmp_path / "paper.md"
    manuscript.write_text("# Test paper", encoding="utf-8")
    reports = tmp_path / "reports"
    reports.mkdir()

    class FakeGraph:
        def __init__(self, config):
            assert config["provider"] == "codex"
            assert config["single_model"] is True

        def stream(self, path):
            assert path == str(manuscript)
            yield "reviewer_scientific_validity", {"decision": None, "errors": []}
            yield "editor", {"decision": "minor", "errors": [], "config": {}}

    def fake_write(_state):
        (reports / "summary.md").write_text("# Review Summary", encoding="utf-8")
        (reports / "decision_letter.md").write_text("Minor revision", encoding="utf-8")
        return str(reports)

    monkeypatch.setattr(mcp_server, "PeerReviewGraph", FakeGraph)
    monkeypatch.setattr(mcp_server, "write_reports", fake_write)
    monkeypatch.setattr(mcp_server, "validate_subscription_cli", lambda _provider: "client")
    service = mcp_server.ReviewService(max_workers=1)

    started = service.start_review(str(manuscript), provider="codex", model="default")
    status = _wait(service, started["job_id"])

    assert status["status"] == "done"
    assert status["decision"] == "minor"
    assert {item["name"] for item in service.list_artifacts(started["job_id"])} == {
        "decision_letter.md",
        "summary.md",
    }
    assert service.read_artifact(started["job_id"], "summary.md") == "# Review Summary"


def test_review_service_rejects_missing_input(tmp_path):
    service = mcp_server.ReviewService(max_workers=1)
    with pytest.raises(FileNotFoundError, match="manuscript not found"):
        service.start_review(str(tmp_path / "missing.pdf"))


def test_journal_catalog_is_available_with_a_stable_count():
    service = mcp_server.ReviewService(max_workers=1)
    journals = service.journals()
    assert len(journals) >= 30
    assert {journal["slug"] for journal in journals} >= {"general", "nature", "science"}


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


def test_local_plugin_manifests_and_pi_package_are_well_formed():
    import json

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


def test_artifact_reader_blocks_path_traversal(tmp_path):
    service = mcp_server.ReviewService(max_workers=1)
    job = mcp_server.ReviewJob("job", "paper.md", {}, report_dir=str(tmp_path))
    service._jobs[job.id] = job
    with pytest.raises(FileNotFoundError):
        service.read_artifact(job.id, "../secret.txt")
