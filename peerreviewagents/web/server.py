"""FastAPI app wiring: routes, static mounts, WebSocket fan-out."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.types import Scope


class _NoCacheStaticFiles(StaticFiles):
    """StaticFiles subclass that disables browser caching.

    The UI ships with no fingerprinted asset URLs, so a cached app.js
    can survive across deploys and silently keep buggy behavior alive
    (long after the source file on disk was fixed). Forcing no-cache
    is the right default for local-dev / single-host use.
    """

    async def get_response(self, path: str, scope: Scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response

from peerreviewagents.default_config import get_config

from .bus import EventBus
from .jobs import AGENT_LAYOUT, JobManager, JobState
from .runner import JobRunner, render_agent_payload


_STATIC_DIR = Path(__file__).parent / "static"
_ALLOWED_SUFFIXES = {".pdf", ".md", ".markdown", ".tex", ".docx", ".txt"}


def create_app(
    *,
    config_overrides: dict[str, Any] | None = None,
    upload_dir: str | os.PathLike | None = None,
) -> FastAPI:
    """Build a FastAPI app instance.

    ``config_overrides`` is layered onto :func:`get_config` for every
    job. ``upload_dir`` is where uploaded manuscripts are stored (one
    subdirectory per job).
    """

    upload_root = Path(upload_dir) if upload_dir else Path.cwd() / ".peerreview-uploads"
    upload_root.mkdir(parents=True, exist_ok=True)

    app = FastAPI(title="PeerReviewAgents", version="0.1.0")
    app.state.jobs = JobManager()
    app.state.buses: dict[str, EventBus] = {}
    app.state.runners: dict[str, JobRunner] = {}
    app.state.config_overrides = dict(config_overrides or {})
    app.state.upload_root = upload_root

    _register_routes(app)
    return app


def _register_routes(app: FastAPI) -> None:
    jobs: JobManager = app.state.jobs

    # --- static UI -------------------------------------------------------

    if _STATIC_DIR.is_dir():
        app.mount(
            "/static",
            _NoCacheStaticFiles(directory=str(_STATIC_DIR)),
            name="static",
        )

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        path = _STATIC_DIR / "index.html"
        if not path.is_file():
            return HTMLResponse("<h1>PeerReviewAgents</h1><p>UI not built.</p>")
        return HTMLResponse(path.read_text(encoding="utf-8"))

    @app.get("/job.html", response_class=HTMLResponse)
    async def job_page() -> HTMLResponse:
        path = _STATIC_DIR / "job.html"
        if not path.is_file():
            raise HTTPException(404, "job.html missing")
        return HTMLResponse(path.read_text(encoding="utf-8"))

    @app.get("/agents")
    async def agents() -> JSONResponse:
        """Static layout metadata, useful for the frontend init."""
        return JSONResponse({"agents": AGENT_LAYOUT})

    # --- jobs ------------------------------------------------------------

    @app.post("/jobs")
    async def create_job(manuscript: UploadFile) -> JSONResponse:
        if jobs.has_active():
            raise HTTPException(
                409, "another review is currently running; only one job is supported in the MVP"
            )
        suffix = Path(manuscript.filename or "").suffix.lower()
        if suffix not in _ALLOWED_SUFFIXES:
            raise HTTPException(
                400,
                f"unsupported file type {suffix!r}; allowed: {sorted(_ALLOWED_SUFFIXES)}",
            )

        job = jobs.create(manuscript_path="", manuscript_filename=manuscript.filename or "manuscript")
        job_dir = Path(app.state.upload_root) / job.id
        job_dir.mkdir(parents=True, exist_ok=True)
        dest = job_dir / (manuscript.filename or f"manuscript{suffix}")
        with dest.open("wb") as fh:
            shutil.copyfileobj(manuscript.file, fh)
        job.manuscript_path = str(dest)

        loop = asyncio.get_running_loop()
        bus = EventBus(loop)
        app.state.buses[job.id] = bus

        config = get_config(**app.state.config_overrides)
        runner = JobRunner(job, config, bus)
        app.state.runners[job.id] = runner
        jobs.set_active(job.id)
        runner.start()

        return JSONResponse({"job_id": job.id, "status": job.status})

    @app.get("/jobs/{job_id}")
    async def get_job(job_id: str) -> JSONResponse:
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "unknown job")
        return JSONResponse(job.public_dict())

    @app.get("/jobs/{job_id}/agents/{agent}")
    async def get_agent(job_id: str, agent: str) -> JSONResponse:
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "unknown job")
        return JSONResponse(render_agent_payload(job, agent))

    @app.get("/jobs/{job_id}/report/{name}")
    async def get_report_file(job_id: str, name: str) -> FileResponse:
        job = jobs.get(job_id)
        if job is None or not job.report_dir:
            raise HTTPException(404, "no report available")
        # Prevent path traversal: only allow files directly inside the
        # job's report directory.
        report_dir = Path(job.report_dir).resolve()
        target = (report_dir / name).resolve()
        if not str(target).startswith(str(report_dir)) or not target.is_file():
            raise HTTPException(404, "report file not found")
        return FileResponse(str(target), media_type="text/markdown")

    @app.get("/jobs/{job_id}/reports")
    async def list_report_files(job_id: str) -> JSONResponse:
        job = jobs.get(job_id)
        if job is None or not job.report_dir:
            return JSONResponse({"files": []})
        report_dir = Path(job.report_dir)
        files = sorted(p.name for p in report_dir.iterdir() if p.is_file())
        return JSONResponse({"files": files, "dir": str(report_dir)})

    @app.websocket("/jobs/{job_id}/events")
    async def stream_events(ws: WebSocket, job_id: str) -> None:
        job = jobs.get(job_id)
        bus: EventBus | None = app.state.buses.get(job_id)
        if job is None or bus is None:
            await ws.close(code=4404)
            return
        await ws.accept()
        sub = await bus.subscribe()
        try:
            async for event in sub:
                await ws.send_text(json.dumps(event))
        except WebSocketDisconnect:
            pass
        finally:
            sub.close()
            # Best effort: close the socket if still open.
            try:
                await ws.close()
            except Exception:  # noqa: BLE001
                pass


def _serialize_job(job: JobState) -> dict[str, Any]:
    return job.public_dict()
