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
from pathlib import Path

import httpx
import pytest
import uvicorn
from langchain_core.messages import AIMessage

from peerreviewagents.agents.reviewers import CONDENSED_REVIEWER_NAMES
from peerreviewagents.agents.schemas import (
    AuditFinding,
    AuditOutput,
    AuthorRebuttalOutput,
    DebateOutput,
    EditorDecisionOutput,
    GapFinding,
    JournalRecommendationsOutput,
    JournalSuggestion,
    MetaReviewOutput,
    PanelGapOutput,
    ReviewerOutput,
)
from peerreviewagents.web import create_app

SAMPLE = os.path.join(os.path.dirname(__file__), "sample_manuscript.md")

# One canned structured instance per agent boundary. Mirrors the
# fixtures in tests/test_pipeline.py so both end-to-end tests exercise
# the same Phase-C structured-output path.
_CANNED: dict[type, object] = {
    ReviewerOutput: ReviewerOutput(
        score=3, confidence=4,
        summary="Method is sensible but undertested.",
        strengths=["Clear motivation"],
        weaknesses=["Single cluster only"],
    ),
    AuditOutput: AuditOutput(
        summary="A few HARD identifiers are missing.",
        categories_detected=["Computational/ML"],
        findings=[
            AuditFinding(
                category="Computational/ML",
                item="Random seed",
                severity="HARD",
                status="missing",
                evidence="No seed reported.",
            ),
        ],
    ),
    DebateOutput: DebateOutput(
        argument="Contribution is incremental but cleanly evaluated.",
        key_points=["Fair comparisons"],
    ),
    PanelGapOutput: PanelGapOutput(
        findings=[
            GapFinding(
                finding=(
                    "No reviewer examined whether the held-out split was "
                    "drawn before or after feature selection."
                ),
                belongs_to="data_analysis",
                manuscript_evidence="Section 3.2, 'we select the top 200 features and then split 80/20'",
                kind="gap",
                why_it_matters=(
                    "Selection before splitting leaks the test set into the "
                    "model, so the reported accuracy is not held out."
                ),
                severity="HARD",
            )
        ],
    ),
    MetaReviewOutput: MetaReviewOutput(
        draft_recommendation="major",
        synthesis="Panel leans toward major revision.",
        decisive_factors="Generalization claim outruns the evidence.",
    ),
    AuthorRebuttalOutput: AuthorRebuttalOutput(
        load_bearing_critiques=["scope of generalization"],
    ),
    EditorDecisionOutput: EditorDecisionOutput(
        decision="major",
        summary_of_evaluation=(
            "The panel agrees the method is sound but the "
            "generalization claim reaches past the evidence shown. The "
            "scope objection stands unanswered, and closing it needs a "
            "narrowed claim, so the verdict is major."
        ),
        required_revisions=["Narrow the claim."],
    ),
    JournalRecommendationsOutput: JournalRecommendationsOutput(
        after_revision=[JournalSuggestion(
            name="Specialty Journal X",
            fit_reasoning="Topic match.",
            acceptance_realism="Plausible after the claim is narrowed.",
        )],
    ),
}


class _FakeStructuredChain:
    def __init__(self, schema, include_raw: bool):
        self._schema = schema
        self._include_raw = include_raw

    def invoke(self, _messages, **_kwargs):
        instance = _CANNED[self._schema]
        if self._include_raw:
            return {"raw": AIMessage(content=""), "parsed": instance, "parsing_error": None}
        return instance


class FakeLLM:
    """Drop-in replacement matching what run_agent + invoke_structured expect."""

    def bind(self, **_kwargs):
        return self

    def invoke(self, messages, **_kwargs):
        # Keep the web integration aligned with the end-to-end fake: active
        # agents now consume prose, while revision/security boundaries may
        # still request the canned structured instances below.
        from test_pipeline import FakeLLM as PipelineFakeLLM

        return PipelineFakeLLM().invoke(messages, **_kwargs)

    def with_structured_output(self, schema, **kwargs):
        return _FakeStructuredChain(schema, kwargs.get("include_raw", False))


@pytest.fixture
def patched_llms(monkeypatch):
    targets = [
        "peerreviewagents.agents.reviewers.base",
        "peerreviewagents.agents.auditors.base",
        "peerreviewagents.agents.debate.base",
        "peerreviewagents.agents.author.rebuttal",
        "peerreviewagents.agents.editor.editor_in_chief",
        "peerreviewagents.agents.journal_recommender.recommender",
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
        assert "report_dir" not in job
        assert not (tmp_path / "uploads" / job_id).exists()

        # Per-agent REST endpoint returns the finished body.
        sample_reviewer = f"reviewer_{CONDENSED_REVIEWER_NAMES[0]}"
        resp = httpx.get(f"{base}/jobs/{job_id}/agents/{sample_reviewer}")
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["known"] is True
        assert payload["status"] == "done"
        assert payload["body"]
        # Bodies are rendered markdown from the structured ReviewerOutput,
        # no YAML frontmatter at the top.
        assert payload["body"].startswith("# ")
        assert payload["meta"]["score"] == 3.0

        # Reports were written to disk and listed.
        resp = httpx.get(f"{base}/jobs/{job_id}/reports")
        listing = resp.json()
        assert "dir" not in listing
        files = listing["files"]
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


def test_rejects_oversized_upload_and_removes_partial_file(tmp_path):
    uploads = tmp_path / "uploads"
    app = create_app(upload_dir=str(uploads), max_upload_bytes=8)
    port = _free_port()
    with _running_server(app, port):
        resp = httpx.post(
            f"http://127.0.0.1:{port}/jobs",
            files={"manuscript": ("paper.md", b"012345678", "text/markdown")},
        )
    assert resp.status_code == 413
    assert not any(uploads.iterdir())


def test_rejects_invalid_strictness(tmp_path):
    app = create_app(upload_dir=str(tmp_path / "uploads"))
    port = _free_port()
    with _running_server(app, port):
        resp = httpx.post(
            f"http://127.0.0.1:{port}/jobs",
            files={"manuscript": ("paper.md", b"# Title\n\nBody.", "text/markdown")},
            data={"review_strictness": "9"},
        )
    assert resp.status_code == 400
    assert "strictness" in resp.text.lower()


def test_journals_endpoint_exposes_strictness_defaults(tmp_path):
    app = create_app(
        config_overrides={"review_strictness": 4},
        upload_dir=str(tmp_path / "uploads"),
    )
    port = _free_port()
    with _running_server(app, port):
        resp = httpx.get(f"http://127.0.0.1:{port}/journals")
    assert resp.status_code == 200
    body = resp.json()
    assert body["default_strictness"] == 4
    # Labels are keyed by level (JSON stringifies the int keys).
    assert body["strictness_labels"]["3"] == "Balanced"


def test_journals_endpoint_exposes_article_types(tmp_path):
    app = create_app(
        config_overrides={"article_type": "review"},
        upload_dir=str(tmp_path / "uploads"),
    )
    port = _free_port()
    with _running_server(app, port):
        resp = httpx.get(f"http://127.0.0.1:{port}/journals")
    assert resp.status_code == 200
    body = resp.json()
    keys = [at["key"] for at in body["article_types"]]
    assert "review" in keys and "technical-note" in keys
    assert body["default_article_type"] == "review"


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


def _config_for_upload(tmp_path, monkeypatch, data):
    """POST an upload and return the config the job would have run with.

    JobRunner is stubbed so nothing starts: the question is only what the
    upload form resolved to, and starting a real review to find out would need
    a provider key.
    """
    captured = {}

    class _Stub:
        def __init__(self, job, config, bus, **_kwargs):
            captured["config"] = config

        def start(self):
            pass

    monkeypatch.setattr("peerreviewagents.web.server.JobRunner", _Stub)
    app = create_app(upload_dir=str(tmp_path / "uploads"))
    port = _free_port()
    with _running_server(app, port):
        resp = httpx.post(
            f"http://127.0.0.1:{port}/jobs",
            files={"manuscript": ("paper.md", b"# Title\n\nBody.", "text/markdown")},
            data=data,
        )
    assert resp.status_code == 200, resp.text
    return captured["config"]


def test_venue_agnostic_selection_is_honoured(tmp_path, monkeypatch):
    """Venue-agnostic is a choice, and has to survive as one.

    The form used to submit "" for it, which is also what an unsent field
    looks like — an optional form value arrives as None either way. So the one
    selection asking for no venue framing fell through to the config default,
    `general`, and produced a venue-framed review saying nothing about it.
    """
    from peerreviewagents.web.server import NO_JOURNAL

    config = _config_for_upload(tmp_path, monkeypatch, {"target_journal": NO_JOURNAL})
    assert config["target_journal"] == ""


def test_absent_journal_field_falls_through_to_the_default(tmp_path, monkeypatch):
    """A client that sends no journal at all still gets the server default."""
    config = _config_for_upload(tmp_path, monkeypatch, {})
    assert config["target_journal"] == "general"


def test_the_form_offers_the_sentinel_the_server_expects(tmp_path):
    """The two halves have to agree, and nothing else checks that they do."""
    from peerreviewagents.web.server import NO_JOURNAL

    page = (Path(__file__).parent.parent / "peerreviewagents/web/static/index.html").read_text()
    assert f'value="{NO_JOURNAL}"' in page


def test_named_journal_still_wins(tmp_path, monkeypatch):
    config = _config_for_upload(tmp_path, monkeypatch, {"target_journal": "plos-one"})
    assert config["target_journal"] == "plos-one"


# --- upload filename safety --------------------------------------------------


def _stub_runner(monkeypatch, captured):
    class _Stub:
        def __init__(self, job, config, bus, **_kwargs):
            captured["job"] = job
            captured["config"] = config

        def start(self):
            pass

    monkeypatch.setattr("peerreviewagents.web.server.JobRunner", _Stub)


def test_traversal_upload_filename_cannot_escape_the_upload_dir(tmp_path, monkeypatch):
    """'../../evil.md' passes the suffix allowlist; without sanitization the
    write lands outside the upload root entirely."""
    captured = {}
    _stub_runner(monkeypatch, captured)
    uploads = tmp_path / "uploads"
    app = create_app(upload_dir=str(uploads))
    port = _free_port()
    with _running_server(app, port):
        resp = httpx.post(
            f"http://127.0.0.1:{port}/jobs",
            files={"manuscript": ("../../evil.md", b"# T\n\nBody.", "text/markdown")},
        )
    assert resp.status_code == 200, resp.text
    job = captured["job"]
    # Nothing escaped: the file exists only as a bare basename inside the job dir.
    assert not (tmp_path / "evil.md").exists()
    assert Path(job.manuscript_path) == uploads / job.id / "evil.md"
    assert Path(job.manuscript_path).is_file()
    assert job.manuscript_filename == "evil.md"


def test_traversal_supplement_filename_is_sanitized_too(tmp_path, monkeypatch):
    captured = {}
    _stub_runner(monkeypatch, captured)
    uploads = tmp_path / "uploads"
    app = create_app(upload_dir=str(uploads))
    port = _free_port()
    with _running_server(app, port):
        resp = httpx.post(
            f"http://127.0.0.1:{port}/jobs",
            files={
                "manuscript": ("paper.md", b"# T\n\nBody.", "text/markdown"),
                "supplement": ("../../si.md", b"# SI", "text/markdown"),
            },
        )
    assert resp.status_code == 200, resp.text
    job = captured["job"]
    sup = Path(captured["config"]["supplement_path"])
    assert not (tmp_path / "si.md").exists()
    assert sup == uploads / job.id / "si.md"
    assert sup.is_file()


def test_upload_name_that_reduces_to_a_dotfile_is_rejected(tmp_path):
    app = create_app(upload_dir=str(tmp_path / "uploads"))
    port = _free_port()
    with _running_server(app, port):
        for bad in ("../..", ".md", "..", "a/.md"):
            resp = httpx.post(
                f"http://127.0.0.1:{port}/jobs",
                files={"manuscript": (bad, b"x", "text/markdown")},
            )
            assert resp.status_code == 400, bad


# --- explicit --config TOML --------------------------------------------------


def test_config_path_reaches_the_web_app(tmp_path):
    toml = tmp_path / "explicit.toml"
    toml.write_text("strictness = 5\n", encoding="utf-8")
    app = create_app(config_path=str(toml), upload_dir=str(tmp_path / "uploads"))
    port = _free_port()
    with _running_server(app, port):
        resp = httpx.get(f"http://127.0.0.1:{port}/journals")
    assert resp.status_code == 200
    assert resp.json()["default_strictness"] == 5


def test_serve_subcommand_threads_config_path(tmp_path, monkeypatch):
    """`peerreview serve --config <file>` used to stash the path under a
    dunder key and strip it before create_app — silently ignored."""
    import sys

    import uvicorn as uvicorn_mod

    from peerreviewagents.cli.main import run

    toml = tmp_path / "explicit.toml"
    toml.write_text("strictness = 5\n", encoding="utf-8")
    served = {}
    monkeypatch.setattr(uvicorn_mod, "run", lambda app, **_kw: served.update(app=app))
    monkeypatch.setattr(sys, "argv", [
        "peerreview", "serve", "--config", str(toml),
        "--upload-dir", str(tmp_path / "uploads"),
    ])
    run()
    app = served["app"]
    port = _free_port()
    with _running_server(app, port):
        resp = httpx.get(f"http://127.0.0.1:{port}/journals")
    assert resp.json()["default_strictness"] == 5


# --- desk-screen enablement --------------------------------------------------


def test_desk_screen_sprite_follows_graph_enablement(tmp_path, monkeypatch):
    """The sprite must be drawn whenever the desk node will run — including
    desk_screen_mode = "warm", which the old bool(config["desk_screen"])
    computation ignored."""
    captured = {}
    _stub_runner(monkeypatch, captured)
    app = create_app(
        config_overrides={"desk_screen_mode": "warm", "conversion_gate": "off"},
        upload_dir=str(tmp_path / "uploads"),
    )
    port = _free_port()
    with _running_server(app, port):
        resp = httpx.post(
            f"http://127.0.0.1:{port}/jobs",
            files={"manuscript": ("paper.md", b"# T\n\nBody.", "text/markdown")},
        )
    assert resp.status_code == 200, resp.text
    assert captured["job"].desk_screen is True


def test_journals_default_desk_screen_honours_mode(tmp_path):
    app = create_app(
        config_overrides={"desk_screen_mode": "warm"},
        upload_dir=str(tmp_path / "uploads"),
    )
    port = _free_port()
    with _running_server(app, port):
        resp = httpx.get(f"http://127.0.0.1:{port}/journals")
    assert resp.json()["default_desk_screen"] is True


# --- per-job state reclamation -----------------------------------------------


def test_prune_finished_keeps_the_most_recent_finished_job():
    from peerreviewagents.web.jobs import JobManager

    jm = JobManager()
    old = jm.create("p", "a.md")
    old.status, old.finished_at = "done", 100.0
    newer = jm.create("p", "b.md")
    newer.status, newer.finished_at = "error", 200.0
    pending = jm.create("p", "c.md")

    removed = jm.prune_finished(keep=1)
    assert removed == [old.id]
    assert jm.get(old.id) is None
    assert jm.get(newer.id) is not None
    assert jm.get(pending.id) is not None


# --- offline, sanitized markdown rendering -----------------------------------


def test_static_ui_is_offline_and_sanitized():
    """No CDN script tags (the UI must work offline) and every markdown
    renderer routes through DOMPurify before touching innerHTML."""
    static = Path(__file__).parent.parent / "peerreviewagents/web/static"
    assert (static / "vendor/marked.min.js").is_file()
    assert (static / "vendor/purify.min.js").is_file()
    for page in list(static.glob("*.html")) + [static / "app.js"]:
        text = page.read_text(encoding="utf-8")
        assert "cdn.jsdelivr.net" not in text, page.name
        assert 'src="https://' not in text and "src='https://" not in text, page.name
    for renderer in ("app.js", "review.html"):
        text = (static / renderer).read_text(encoding="utf-8")
        assert "DOMPurify.sanitize" in text, renderer
