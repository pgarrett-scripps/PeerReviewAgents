"""End-to-end test for the FastAPI web server using a fake LLM.

Boots the app under uvicorn on a random port, uploads the sample
manuscript, polls the job until the pipeline finishes, and asserts
that per-agent REST inspection and report files are populated. The
WebSocket layer is verified separately by ``test_websocket_stream``.
"""

from __future__ import annotations

import contextlib
import os
import socket
import threading
import time

import httpx
import pytest
import uvicorn
from langchain_core.messages import AIMessage

from peerreviewagents.agents.reviewers import REVIEWER_NAMES
from peerreviewagents.web import create_app


SAMPLE = os.path.join(os.path.dirname(__file__), "sample_manuscript.md")

_CANNED = (
    "---\n"
    "score: 3\n"
    "confidence: 4\n"
    "draft_recommendation: major\n"
    "decision: major\n"
    "---\n"
    "# Review\n\n"
    "## Summary\n"
    "Method is sensible but undertested.\n\n"
    "## Strengths\n"
    "- Clear motivation\n\n"
    "## Weaknesses\n"
    "- Single cluster only\n"
)


class FakeLLM:
    """Drop-in replacement matching what run_agent does to a chat model."""

    def bind(self, **_kwargs):
        return self

    def invoke(self, messages, **_kwargs):
        return AIMessage(content=_CANNED)


@pytest.fixture
def patched_llms(monkeypatch):
    targets = [
        "peerreviewagents.agents.reviewers.base",
        "peerreviewagents.agents.debate.base",
        "peerreviewagents.agents.synthesis.meta_reviewer",
        "peerreviewagents.agents.author.rebuttal",
        "peerreviewagents.agents.editor.editor_in_chief",
    ]
    for mod in targets:
        monkeypatch.setattr(f"{mod}.make_llm", lambda config, **_kw: FakeLLM())


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextlib.contextmanager
def _running_server(app, port):
    cfg = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning",
                         loop="asyncio", lifespan="on")
    server = uvicorn.Server(cfg)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    # Poll the TCP port until uvicorn accepts; `server.started` doesn't
    # always reflect the bound-socket state in time.
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.05)
    else:
        raise RuntimeError(f"uvicorn never bound to port {port}")
    try:
        yield
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_full_web_pipeline(monkeypatch, tmp_path, patched_llms):
    monkeypatch.setenv("PEERREVIEW_OUTPUT_DIR", str(tmp_path / "reports"))
    app = create_app(
        config_overrides={"max_debate_rounds": 1, "output_dir": str(tmp_path / "reports")},
        upload_dir=str(tmp_path / "uploads"),
    )

    port = _free_port()
    with _running_server(app, port):
        base = f"http://127.0.0.1:{port}"

        # Upload a manuscript.
        with open(SAMPLE, "rb") as fh:
            resp = httpx.post(
                f"{base}/jobs",
                files={"manuscript": ("sample.md", fh, "text/markdown")},
                timeout=10.0,
            )
        assert resp.status_code == 200, resp.text
        job_id = resp.json()["job_id"]

        # Concurrent upload while one's running must be rejected.
        with open(SAMPLE, "rb") as fh:
            again = httpx.post(
                f"{base}/jobs",
                files={"manuscript": ("sample.md", fh, "text/markdown")},
                timeout=2.0,
            )
        assert again.status_code == 409

        # Poll /jobs/<id> until the worker thread reports terminal status.
        deadline = time.time() + 60
        job = None
        while time.time() < deadline:
            resp = httpx.get(f"{base}/jobs/{job_id}")
            assert resp.status_code == 200
            job = resp.json()
            if job["status"] in ("done", "error"):
                break
            time.sleep(0.25)
        assert job is not None and job["status"] == "done", f"job: {job}"
        assert job["decision"] == "major"

        # Per-agent REST endpoint returns the finished body.
        sample_reviewer = f"reviewer_{REVIEWER_NAMES[0]}"
        resp = httpx.get(f"{base}/jobs/{job_id}/agents/{sample_reviewer}")
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["known"] is True
        assert payload["status"] == "done"
        assert payload["body"]
        assert payload["body"].startswith("---")
        assert payload["meta"]["score"] == 3.0

        # Reports were written to disk and listed.
        resp = httpx.get(f"{base}/jobs/{job_id}/reports")
        files = resp.json()["files"]
        assert "summary.md" in files
        assert "decision_letter.md" in files

        # Listed report files are fetchable.
        resp = httpx.get(f"{base}/jobs/{job_id}/report/summary.md")
        assert resp.status_code == 200
        assert "Decision" in resp.text


def test_rejects_unknown_suffix(tmp_path):
    app = create_app(upload_dir=str(tmp_path / "uploads"))
    port = _free_port()
    with _running_server(app, port):
        resp = httpx.post(
            f"http://127.0.0.1:{port}/jobs",
            files={"manuscript": ("bad.bin", b"\x00\x01", "application/octet-stream")},
        )
    assert resp.status_code == 400


def test_unknown_job_returns_404(tmp_path):
    app = create_app(upload_dir=str(tmp_path / "uploads"))
    port = _free_port()
    with _running_server(app, port):
        resp = httpx.get(f"http://127.0.0.1:{port}/jobs/deadbeef")
    assert resp.status_code == 404


def test_static_pages_served(tmp_path):
    app = create_app(upload_dir=str(tmp_path / "uploads"))
    port = _free_port()
    with _running_server(app, port):
        for path in ("/", "/job.html", "/agents"):
            resp = httpx.get(f"http://127.0.0.1:{port}{path}")
            assert resp.status_code == 200, path
