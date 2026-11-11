"""The web UI is the one-command engine review in a browser.

An upload is imported, reviewed and reported exactly as ``peerreview review
manuscript.pdf <out>`` does, because both run ``peerreviewagents.engine.bundle``
through :mod:`peerreviewagents.review_service`. These tests drive the HTTP
surface: the review a form starts, the refusals that reach the page in the
engine's own words, the confinement of uploads and artifacts, and the absence
of the LangGraph panel's endpoints.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from peerreviewagents import review_service
from peerreviewagents.engine.bundle import ReviewPaths
from peerreviewagents.engine.storage import write
from peerreviewagents.web import create_app

PDF = b"%PDF-1.7\nnot a real manuscript\n"
FINISHED = ("done", "done-without-findings", "error", "cancelled")


@pytest.fixture(autouse=True)
def _stub_both_provider_clis(no_provider_cli):
    """No test here may reach an installed `claude` or `codex`.

    These tests drive the real job service, so a test that forgets to fake the
    engine would otherwise start a real review on whichever CLI happens to be
    installed. Stubbing only one transport is how that last leaked.
    """


def _client(tmp_path, **kwargs) -> TestClient:
    kwargs.setdefault("host", "127.0.0.1")
    app = create_app(
        output_dir=tmp_path / "reviews",
        upload_dir=tmp_path / "uploads",
        **kwargs,
    )
    # The server answers to its own host only, so the client has to be one.
    return TestClient(app, base_url="http://127.0.0.1:8765")


def _upload(client, name="paper.pdf", content=PDF, **fields):
    return client.post(
        "/api/reviews",
        files={"manuscript": (name, content, "application/pdf")},
        data=fields,
    )


def _wait(client, job_id: str) -> dict:
    for _ in range(300):
        state = client.get(f"/api/reviews/{job_id}").json()
        if state["status"] in FINISHED:
            return state
        time.sleep(0.01)
    raise AssertionError("background review did not finish")


def _fake_review(monkeypatch, *, failure=None, status="complete", report=True):
    """Stand in for the engine: write the files a real run would leave."""
    seen: dict = {}

    def fake_plan(paper, output):
        bundle = Path(output)
        (bundle / "manuscript").mkdir(parents=True)
        (bundle / "manuscript" / "paper.json").write_text("{}", encoding="utf-8")
        seen["paper"], seen["bundle"] = str(paper), str(bundle)
        return ReviewPaths(bundle / "manuscript", bundle / "review", bundle)

    def fake_run(paths, options):
        seen["options"] = options
        paths.run.mkdir(parents=True)
        # Through the engine's own atomic writer, as every real stage does: the
        # page polls this file while the run rewrites it, and a plain
        # write_text truncates first, so a poll can land on an empty file.
        write(paths.run / "progress.json", {"status": status, "stage": "debate", "reviewers": 2})
        reports = {}
        if report:
            path = paths.bundle / "REVIEW.md"
            path.write_text("# Review\n\nThe claim on page 2 is unsupported.\n", encoding="utf-8")
            reports["markdown"] = str(path)
        return {"progress": {"status": status}, "reports": reports, "failure": failure}

    monkeypatch.setattr(review_service, "plan_review", fake_plan)
    monkeypatch.setattr(review_service, "run_review_options", fake_run)
    return seen


def test_an_upload_is_one_engine_review_and_the_page_reads_its_report(tmp_path, monkeypatch):
    seen = _fake_review(monkeypatch)
    client = _client(tmp_path)

    started = _upload(client, provider="codex", literature_cutoff="2025-01-31").json()
    state = _wait(client, started["job_id"])

    assert state["status"] == "done"
    assert state["progress"]["stage"] == "debate"
    assert state["reports"] == ["REVIEW.md"]
    assert seen["options"]["model"] == "gpt-5.6-terra"
    assert seen["options"]["literature_cutoff"] == "2025-01-31"
    assert seen["bundle"].startswith(str(tmp_path / "reviews"))
    # The uploaded PDF is what was reviewed, from inside the upload root.
    assert seen["paper"].startswith(str(tmp_path / "uploads"))

    report = client.get(f"/api/reviews/{started['job_id']}/artifacts/REVIEW.md")
    assert report.status_code == 200
    assert "unsupported" in report.text
    names = [item["name"] for item in
             client.get(f"/api/reviews/{started['job_id']}/artifacts").json()["artifacts"]]
    assert names == ["REVIEW.md", "review/progress.json"]


def test_a_stopped_run_still_reports_and_says_why_it_stopped(tmp_path, monkeypatch):
    _fake_review(monkeypatch, failure="RuntimeError: usage is unknown", status="blocked")
    client = _client(tmp_path)

    state = _wait(client, _upload(client).json()["job_id"])

    assert state["status"] == "error"
    assert state["errors"] == ["review did not finish: RuntimeError: usage is unknown"]
    assert state["reports"] == ["REVIEW.md"]


def test_a_review_that_endorses_nothing_is_its_own_status(tmp_path, monkeypatch):
    _fake_review(monkeypatch, status="complete-without-findings", report=False)
    client = _client(tmp_path)

    state = _wait(client, _upload(client).json()["job_id"])

    assert state["status"] == "done-without-findings"
    assert state["reports"] == []


def test_the_engine_reviews_pdfs_and_says_so_about_anything_else(tmp_path):
    client = _client(tmp_path)
    response = _upload(client, name="paper.md", content=b"# manuscript\n")
    assert response.status_code == 400
    assert "PDF" in response.json()["detail"]


def test_engine_refusals_reach_the_browser_in_the_engine_words(tmp_path):
    client = _client(tmp_path)
    cases = {
        "unknown review mode": {"mode": "journal-panel"},
        "1 to 16": {"reviewers": "99"},
        "only to ensemble mode": {"mode": "panel", "reviewers": "3"},
        "one long investigative session": {"policy": "workspace-agentic-v1"},
        "YYYY-MM-DD": {"literature_cutoff": "last tuesday"},
    }
    for expected, fields in cases.items():
        response = _upload(client, **fields)
        assert response.status_code == 400, fields
        assert expected in response.json()["detail"], fields


def test_a_reviewer_count_that_is_not_a_number_is_refused_not_ignored(tmp_path):
    client = _client(tmp_path)
    response = _upload(client, reviewers="two")
    assert response.status_code == 400
    assert "whole number" in response.json()["detail"]


def test_an_oversized_upload_is_refused_and_leaves_no_partial_file(tmp_path):
    client = _client(tmp_path, max_upload_bytes=16)
    response = _upload(client, content=PDF * 10)
    assert response.status_code == 413
    assert not list((tmp_path / "uploads").rglob("*.pdf"))


def test_a_traversal_upload_filename_cannot_escape_the_upload_folder(tmp_path, monkeypatch):
    _fake_review(monkeypatch)
    client = _client(tmp_path)
    response = _upload(client, name="../../escaped.pdf")
    assert response.status_code == 200
    assert not (tmp_path / "escaped.pdf").exists()
    assert [path.name for path in (tmp_path / "uploads").rglob("*.pdf")] == ["escaped.pdf"]


def test_an_upload_name_that_reduces_to_a_dotfile_is_rejected(tmp_path):
    client = _client(tmp_path)
    assert _upload(client, name=".pdf").status_code == 400


def test_an_unknown_job_and_a_traversing_artifact_name_are_both_404(tmp_path, monkeypatch):
    _fake_review(monkeypatch)
    client = _client(tmp_path)
    assert client.get("/api/reviews/nope").status_code == 404
    assert client.post("/api/reviews/nope/cancel").status_code == 404

    job_id = _wait(client, _upload(client).json()["job_id"])["job_id"]
    (tmp_path / "secret.txt").write_text("private", encoding="utf-8")
    assert client.get(f"/api/reviews/{job_id}/artifacts/../../secret.txt").status_code == 404
    # The imported manuscript is the input, not a review artifact.
    assert client.get(f"/api/reviews/{job_id}/artifacts/manuscript/paper.json").status_code == 404


def test_saved_reviews_are_listed_and_served_from_the_output_root(tmp_path):
    saved = tmp_path / "reviews" / "earlier-run"
    saved.mkdir(parents=True)
    (saved / "REVIEW.md").write_text("# Earlier review\n", encoding="utf-8")
    (tmp_path / "reviews" / "half-made").mkdir()
    client = _client(tmp_path)

    runs = client.get("/api/runs").json()["runs"]
    assert [run["name"] for run in runs] == ["earlier-run"]
    assert "Earlier review" in client.get("/api/runs/earlier-run/report").text
    assert client.get("/api/runs/half-made/report").status_code == 404
    assert client.get("/api/runs/..%2F..%2Fetc/report").status_code == 404


def test_the_page_advertises_what_the_engine_can_be_asked_for(tmp_path):
    client = _client(tmp_path, defaults={"mode": "independent", "policy": "standard-v1"})
    record = client.get("/api/capabilities").json()

    assert record["review_modes"] == ["ensemble", "independent", "panel"]
    assert record["default_provider"] == "claude-code"
    assert record["manuscript_format"] == "PDF"
    assert record["form_defaults"]["mode"] == "independent"
    assert record["form_defaults"]["policy"] == "standard-v1"


def test_server_level_defaults_are_what_a_bare_form_submits(tmp_path, monkeypatch):
    seen = _fake_review(monkeypatch)
    client = _client(tmp_path, defaults={"mode": "independent", "policy": "standard-v1"})

    _wait(client, _upload(client).json()["job_id"])

    assert seen["options"]["mode"] == "independent"
    assert seen["options"]["policy"] == "standard-v1"


def test_the_langgraph_panel_surface_is_gone_rather_than_kept_alive(tmp_path):
    """The office view, the per-agent panels, the token stream, the journal
    catalogue and the editorial decision were the panel's. The engine has no
    equivalent for any of them, so the routes are gone, not stubbed."""
    client = _client(tmp_path)
    for gone in ("/agents", "/journals", "/history", "/jobs/x", "/jobs/x/agents/editor",
                 "/job.html"):
        assert client.get(gone).status_code == 404, gone
    with pytest.raises(Exception):
        with client.websocket_connect("/jobs/x/events"):
            pass
    for gone in ("config_overrides", "config_path", "target_journal"):
        with pytest.raises(TypeError):
            create_app(**{gone: "x"})
    for module in ("runner", "jobs", "bus"):
        with pytest.raises(ImportError):
            __import__(f"peerreviewagents.web.{module}")


def test_the_pages_are_served_and_name_no_token_stream(tmp_path):
    client = _client(tmp_path)
    index = client.get("/")
    review = client.get("/review.html")
    assert index.status_code == 200 and review.status_code == 200
    assert "Review a manuscript" in index.text
    assert "REVIEW.md" in review.text
    assert client.get("/static/app.js").status_code == 200
    assert "WebSocket" not in client.get("/static/app.js").text


def test_no_test_in_this_module_can_reach_a_provider_cli(tmp_path):
    """The guard the rest of this module relies on, checked rather than assumed.

    A reviewer's run of the web tests hit the real installed `codex` binary
    because only the `claude` command line was stubbed. Both are unreachable
    here: by name, by override variable, and at the launch itself.
    """
    from peerreviewagents.engine import model_runtime

    for provider in ("claude-code", "codex"):
        status = model_runtime.auth_status(provider)
        assert status["loggedIn"] is False
        assert "not found" in status["error"]
    with pytest.raises(model_runtime.RunBlocked, match="not found"):
        model_runtime.require_subscription(tmp_path)
    with pytest.raises(AssertionError, match="must be stubbed"):
        model_runtime.subprocess.run(["codex", "--version"])


def test_a_post_from_another_origin_does_not_start_a_review(tmp_path, monkeypatch):
    """A page on any other origin can make the browser of whoever is running
    this server post to it. The form is same-origin, so an Origin naming
    another host is never this page and never queues a run."""
    seen = _fake_review(monkeypatch)
    client = _client(tmp_path)

    refused = client.post(
        "/api/reviews",
        files={"manuscript": ("paper.pdf", PDF, "application/pdf")},
        headers={"Origin": "http://evil.example"},
    )
    assert refused.status_code == 403
    assert "evil.example" in refused.json()["detail"]
    assert "options" not in seen
    assert client.get("/api/reviews").json()["reviews"] == []

    # The page's own post carries this server's origin and is a review.
    allowed = client.post(
        "/api/reviews",
        files={"manuscript": ("paper.pdf", PDF, "application/pdf")},
        headers={"Origin": "http://127.0.0.1:8765"},
    )
    assert allowed.status_code == 200
    _wait(client, allowed.json()["job_id"])


def test_a_request_for_another_host_is_refused_before_a_route_runs(tmp_path):
    """A DNS name pointed at 127.0.0.1 is how a remote page reaches a local
    server as a same-origin one. The server answers to its own host only."""
    app = create_app(
        output_dir=tmp_path / "reviews",
        upload_dir=tmp_path / "uploads",
        host="127.0.0.1",
    )
    stranger = TestClient(app, base_url="http://attacker.example")
    assert stranger.get("/").status_code == 400
    assert stranger.get("/api/capabilities").status_code == 400
    assert TestClient(app, base_url="http://localhost:8765").get("/").status_code == 200


def test_a_server_bound_to_every_interface_answers_to_any_host(tmp_path):
    """Asking for 0.0.0.0 is asking to be reached from elsewhere, and such a
    server cannot know its own name, so the host check is not what stops it."""
    app = create_app(
        output_dir=tmp_path / "reviews",
        upload_dir=tmp_path / "uploads",
        host="0.0.0.0",  # noqa: S104 - the case under test
    )
    assert TestClient(app, base_url="http://somewhere.example").get("/").status_code == 200


def test_two_uploads_of_one_filename_are_two_runs(tmp_path, monkeypatch):
    """Same name, same millisecond: the folder used to collide and the second
    upload failed on the exclusive create."""
    _fake_review(monkeypatch)
    client = _client(tmp_path)

    first = _upload(client).json()["job_id"]
    second = _upload(client).json()["job_id"]
    assert first != second
    uploaded = sorted((tmp_path / "uploads").rglob("paper.pdf"))
    assert len(uploaded) == 2
    assert uploaded[0].parent != uploaded[1].parent
    for job_id in (first, second):
        assert _wait(client, job_id)["status"] == "done"


def test_a_submitted_zero_is_refused_rather_than_read_as_unset(tmp_path):
    """`or` read a submitted 0 as "not submitted" and ran the default two
    reviewers instead of refusing a count the engine does not accept."""
    client = _client(tmp_path)
    response = _upload(client, reviewers="0")
    assert response.status_code == 400
    assert "1 to 16" in response.json()["detail"]


def test_serve_threads_its_own_defaults_into_the_app(tmp_path, monkeypatch):
    import peerreviewagents.web as web
    from peerreviewagents.cli import main as cli_main

    captured: dict = {}

    def fake_create_app(**kwargs):
        captured.update(kwargs)
        return create_app(output_dir=tmp_path / "reviews", upload_dir=tmp_path / "uploads")

    monkeypatch.setattr(web, "create_app", fake_create_app)
    monkeypatch.setattr("uvicorn.run", lambda *args, **kwargs: None)
    monkeypatch.setattr("sys.argv", [
        "peerreview", "serve",
        "--output-dir", str(tmp_path / "out"),
        "--upload-dir", str(tmp_path / "in"),
        "--host", "127.0.0.1",
        "--mode", "independent",
        "--reviewers", "3",
        "--provider", "codex",
        "--policy", "standard-v1",
    ])

    cli_main.run()

    assert captured["output_dir"] == str(tmp_path / "out")
    assert captured["upload_dir"] == str(tmp_path / "in")
    # The bound address reaches the app, which is what it answers to besides
    # the loopback names. An upload is the only way in, so there is no input
    # root to thread any more.
    assert captured["host"] == "127.0.0.1"
    assert "input_roots" not in captured
    assert captured["defaults"] == {
        "mode": "independent",
        "reviewers": 3,
        "provider": "codex",
        "policy": "standard-v1",
    }
