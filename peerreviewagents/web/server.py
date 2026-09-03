"""FastAPI app wiring: routes, static mounts, WebSocket fan-out."""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
import os
import shutil
from pathlib import Path
from typing import Any

from fastapi import (
    FastAPI,
    File,
    Form,
    HTTPException,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from starlette.types import Scope

from peerreviewagents.agents.editor.desk_screen import (
    node_enabled as desk_screen_enabled,
)
from peerreviewagents.agents.editor.desk_screen import (
    screen_mode as desk_screen_mode,
)
from peerreviewagents.article_types import ARTICLE_TYPES, normalize_article_type
from peerreviewagents.default_config import get_config
from peerreviewagents.journals import list_journals, load_journal
from peerreviewagents.strictness import DEFAULT_LEVEL, LABELS, normalize_strictness

from .bus import EventBus
from .jobs import AGENT_LAYOUT, JobManager, JobState
from .runner import JobRunner, render_agent_payload


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


_STATIC_DIR = Path(__file__).parent / "static"
_ALLOWED_SUFFIXES = {".pdf", ".md", ".markdown", ".tex", ".txt"}
_MAX_UPLOAD_BYTES = 50 * 1024 * 1024
_COPY_CHUNK_BYTES = 1024 * 1024


class UploadTooLarge(ValueError):
    """Raised when a streamed upload crosses the configured byte limit."""

# What the upload form submits for "no target venue". A reserved token rather
# than "": an optional form field that is empty and one that was never sent
# both arrive as None, so an empty value cannot express a choice.
NO_JOURNAL = "__none__"

# Maps the human decision label written into summary.md back to the machine
# key the UI colours badges with.
_DECISION_KEY = {
    "Accept": "accept",
    "Minor Revision": "minor",
    "Major Revision": "major",
    "Reject": "reject",
}


def _prettify_slug(slug: str) -> str:
    """Turn a run-dir slug ('gap-ms-automated…') into a readable fallback title."""
    return " ".join(w for w in slug.replace("-", " ").split() if w).strip() or slug


def _date_from_run_name(name: str) -> str:
    """Extract the 'YYYY-MM-DD HH:MM' timestamp from a run-dir name, or ''."""
    try:
        return _dt.datetime.strptime(name[:15], "%Y%m%d-%H%M%S").strftime("%Y-%m-%d %H:%M")
    except (ValueError, IndexError):
        return ""


def _parse_summary(text: str) -> dict[str, Any]:
    """Pull title / decision / venue / cost out of a run's summary.md.

    Best-effort and format-tolerant: any field that isn't found is simply
    omitted so the caller's defaults survive.
    """
    out: dict[str, Any] = {}
    for line in text.splitlines():
        s = line.strip()
        if "title" not in out and s.startswith("# Review Summary") and "—" in s:
            out["title"] = s.split("—", 1)[1].strip()
        elif s.startswith("**Decision:**"):
            label = s.split("**Decision:**", 1)[1].strip()
            out["decision_label"] = label
            out["decision"] = _DECISION_KEY.get(label, "")
        elif s.startswith("**Target venue:**"):
            out["venue"] = s.split("**Target venue:**", 1)[1].strip()
        elif s.startswith("**Outcome:**") and "Desk reject" in s:
            out["decision"] = "reject"
            out.setdefault("decision_label", "Desk Reject")
        elif s.startswith("**OpenRouter cost:**") and "$" in s:
            out["cost"] = s.split("$", 1)[1].strip() or None
    return out


def _scan_history(root: Path, limit: int = 200) -> list[dict[str, Any]]:
    """List past review runs on disk, newest first.

    Each run is a directory under the reports output dir; we read its
    summary.md for display metadata. Directory names are timestamp-prefixed
    so a reverse name sort is chronological.
    """
    if not root.is_dir():
        return []
    runs: list[dict[str, Any]] = []
    dirs = sorted(
        (p for p in root.iterdir() if p.is_dir()),
        key=lambda p: p.name,
        reverse=True,
    )[:limit]
    for d in dirs:
        try:
            md_files = [p for p in d.iterdir() if p.is_file() and p.suffix == ".md"]
        except OSError:
            continue
        # A run with no markdown reports (e.g. a leftover scratch dir) isn't a
        # real review — skip it rather than surface an empty history row.
        if not md_files:
            continue
        meta: dict[str, Any] = {
            "id": d.name,
            "title": _prettify_slug(d.name[16:] or d.name),
            "decision": "",
            "decision_label": "",
            "venue": "",
            "cost": None,
            "date": _date_from_run_name(d.name),
            "files": len(md_files),
        }
        summary = d / "summary.md"
        if summary.is_file():
            try:
                meta.update(_parse_summary(summary.read_text(encoding="utf-8")))
            except OSError:
                pass
        runs.append(meta)
    return runs


def _upload_name(filename: str) -> str:
    """Reduce a client-supplied upload filename to a safe bare basename.

    The filename is attacker-controlled: ``../../x.md`` passes a suffix
    allowlist while escaping the upload directory entirely. Everything up to
    the last path separator (either flavour) is dropped, and a name that
    reduces to nothing — or to a dotfile — is rejected outright.
    """
    name = Path(filename.replace("\\", "/")).name
    if not name or name.startswith("."):
        raise HTTPException(400, f"invalid upload filename {filename!r}")
    return name


def _copy_upload(src, dest: Path, max_bytes: int) -> None:
    written = 0
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        with os.fdopen(os.open(dest, flags, 0o600), "wb") as fh:
            while chunk := src.read(_COPY_CHUNK_BYTES):
                written += len(chunk)
                if written > max_bytes:
                    raise UploadTooLarge(f"upload exceeds {max_bytes} bytes")
                fh.write(chunk)
    except Exception:
        dest.unlink(missing_ok=True)
        raise


def _safe_run_dir(root: Path, run: str) -> Path:
    """Resolve ``run`` to a direct child directory of ``root`` or 404.

    Guards against path traversal: the resolved target's parent must be the
    reports root exactly (so '..'/nested paths are rejected).
    """
    resolved_root = root.resolve()
    target = (resolved_root / run).resolve()
    if target.parent != resolved_root or not target.is_dir():
        raise HTTPException(404, "unknown run")
    return target


def create_app(
    *,
    config_overrides: dict[str, Any] | None = None,
    config_path: str | os.PathLike | None = None,
    upload_dir: str | os.PathLike | None = None,
    max_upload_bytes: int = _MAX_UPLOAD_BYTES,
) -> FastAPI:
    """Build a FastAPI app instance.

    ``config_overrides`` is layered onto :func:`get_config` for every
    job; ``config_path`` is an explicit TOML file (CLI ``--config``)
    handed to every :func:`get_config` call the app makes. ``upload_dir``
    is where uploaded manuscripts are stored (one subdirectory per job).
    """

    upload_root = Path(upload_dir) if upload_dir else Path.cwd() / ".peerreview-uploads"
    if max_upload_bytes < 1:
        raise ValueError("max_upload_bytes must be positive")
    upload_root.mkdir(parents=True, exist_ok=True, mode=0o700)

    app = FastAPI(title="PeerReviewAgents", version="0.1.0")
    app.state.jobs = JobManager()
    app.state.buses: dict[str, EventBus] = {}
    app.state.runners: dict[str, JobRunner] = {}
    app.state.config_overrides = dict(config_overrides or {})
    app.state.config_path = config_path
    app.state.upload_root = upload_root
    app.state.max_upload_bytes = max_upload_bytes

    _register_routes(app)
    return app


def _register_routes(app: FastAPI) -> None:
    jobs: JobManager = app.state.jobs

    def _config(**overrides: Any) -> dict[str, Any]:
        """Resolve config the way every route must: explicit --config TOML
        first, then the server-level overrides, then per-call ones."""
        merged = dict(app.state.config_overrides)
        merged.update(overrides)
        return get_config(config_path=app.state.config_path, **merged)

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

    @app.get("/review.html", response_class=HTMLResponse)
    async def review_page() -> HTMLResponse:
        path = _STATIC_DIR / "review.html"
        if not path.is_file():
            raise HTTPException(404, "review.html missing")
        return HTMLResponse(path.read_text(encoding="utf-8"))

    @app.get("/agents")
    async def agents() -> JSONResponse:
        """Static layout metadata, useful for the frontend init."""
        return JSONResponse({"agents": AGENT_LAYOUT})

    @app.get("/journals")
    async def journals() -> JSONResponse:
        """Available target-journal options for the upload form.

        The 'general' fallback profile is pinned to the top so the default
        choice (review against sound, field-general standards) is the first
        real option, ahead of the alphabetical list of specific venues.
        """
        config = _config()
        profiles = list_journals(config)
        profiles.sort(key=lambda p: (p.slug != "general", p.name.lower()))
        try:
            default_strictness = normalize_strictness(
                config.get("review_strictness", DEFAULT_LEVEL)
            )
        except ValueError:
            default_strictness = DEFAULT_LEVEL
        try:
            default_article_type = normalize_article_type(config.get("article_type"))
        except ValueError:
            default_article_type = ""
        return JSONResponse({
            "journals": [
                {
                    "slug": p.slug,
                    "name": p.name,
                    "field": p.field,
                    # Type keys this venue defines caps for, so the form can
                    # hint which selections carry a word limit.
                    "article_types": sorted(p.article_types),
                }
                for p in profiles
            ],
            "default": config.get("target_journal") or "",
            # Article-type taxonomy is venue-general, so it's a single list
            # independent of the chosen journal (caps come from the journal).
            "article_types": [
                {"key": at.key, "name": at.name, "description": at.description}
                for at in ARTICLE_TYPES.values()
            ],
            "default_article_type": default_article_type,
            "default_strictness": default_strictness,
            "strictness_labels": LABELS,
            # Whether the triage screen runs by default. Resolved through the
            # same mode logic the graph uses, so desk_screen_mode = "gate" /
            # "warm" in config is reflected in the form instead of ignored.
            "default_desk_screen": desk_screen_mode(config) != "off",
        })

    # --- history (disk-backed past runs) ---------------------------------

    def _reports_root() -> Path:
        return Path(_config()["output_dir"])

    @app.get("/history")
    async def history() -> JSONResponse:
        """Past review runs on disk, newest first, for the home page list."""
        return JSONResponse({"runs": _scan_history(_reports_root())})

    @app.get("/history/{run}/reports")
    async def history_reports(run: str) -> JSONResponse:
        run_dir = _safe_run_dir(_reports_root(), run)
        files = sorted(
            p.name for p in run_dir.iterdir() if p.is_file() and p.suffix == ".md"
        )
        return JSONResponse({"files": files})

    @app.get("/history/{run}/report/{name}")
    async def history_report_file(run: str, name: str) -> FileResponse:
        run_dir = _safe_run_dir(_reports_root(), run)
        target = (run_dir / name).resolve()
        if target.parent != run_dir.resolve() or not target.is_file():
            raise HTTPException(404, "report file not found")
        return FileResponse(str(target), media_type="text/markdown")

    # --- jobs ------------------------------------------------------------

    @app.post("/jobs")
    async def create_job(
        manuscript: UploadFile,
        target_journal: str = Form(""),
        article_type: str = Form(""),
        review_strictness: str = Form(""),
        desk_screen: str = Form(""),
        supplement: UploadFile | None = File(None),
    ) -> JSONResponse:
        if jobs.has_active():
            raise HTTPException(
                409, "another review is currently running; only one job is supported in the MVP"
            )
        # Reclaim per-job state from older finished runs. The most recent
        # finished job stays replayable (its page may still be open); the
        # rest would otherwise accumulate for the life of the process.
        for stale_id in jobs.prune_finished(keep=1):
            app.state.buses.pop(stale_id, None)
            app.state.runners.pop(stale_id, None)
            shutil.rmtree(Path(app.state.upload_root) / stale_id, ignore_errors=True)

        manuscript_name = _upload_name(manuscript.filename or "")
        suffix = Path(manuscript_name).suffix.lower()
        if suffix not in _ALLOWED_SUFFIXES:
            raise HTTPException(
                400,
                f"unsupported file type {suffix!r}; allowed: {sorted(_ALLOWED_SUFFIXES)}",
            )
        # Optional supplementary information, validated up front so a bad SI
        # file is rejected before any job state or upload bytes exist.
        supplement_name: str | None = None
        if supplement is not None and supplement.filename:
            supplement_name = _upload_name(supplement.filename)
            sup_suffix = Path(supplement_name).suffix.lower()
            if sup_suffix not in _ALLOWED_SUFFIXES:
                raise HTTPException(
                    400,
                    f"unsupported SI file type {sup_suffix!r}; "
                    f"allowed: {sorted(_ALLOWED_SUFFIXES)}",
                )

        # Per-upload journal / strictness selection overrides any server-level
        # default; a field the form did not send falls through to that default.
        job_overrides = dict(app.state.config_overrides)
        # Venue-agnostic is a choice and has to be spelled. The form used to
        # submit "" for it, which is what an unsent field also looks like — an
        # optional form value arrives as None either way — so the one selection
        # asking for no venue framing fell through to the config default,
        # `general`, and produced a venue-framed review saying nothing about it.
        if target_journal == NO_JOURNAL:
            job_overrides["target_journal"] = ""
        elif target_journal:
            try:
                load_journal(target_journal, _config())
            except FileNotFoundError as exc:
                raise HTTPException(400, str(exc))
            job_overrides["target_journal"] = target_journal
        if article_type:
            try:
                job_overrides["article_type"] = normalize_article_type(article_type)
            except ValueError as exc:
                raise HTTPException(400, str(exc))
        if review_strictness:
            try:
                job_overrides["review_strictness"] = normalize_strictness(review_strictness)
            except ValueError as exc:
                raise HTTPException(400, str(exc))
        low = desk_screen.strip().lower()
        if low in ("1", "true", "yes", "on"):
            job_overrides["desk_screen"] = True
        elif low in ("0", "false", "no", "off"):
            job_overrides["desk_screen"] = False

        job = jobs.create(manuscript_path="", manuscript_filename=manuscript_name)
        # Claim the single-job slot BEFORE the first await: the has_active
        # check above and this claim run without yielding to the event loop,
        # which is what keeps the one-job invariant race-free once the file
        # copies below happen asynchronously.
        jobs.set_active(job.id)
        job_dir = Path(app.state.upload_root) / job.id
        try:
            job_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            dest = job_dir / manuscript_name
            # Copy off the event loop: a large upload written synchronously
            # here froze every route and WebSocket for its duration.
            await run_in_threadpool(
                _copy_upload,
                manuscript.file,
                dest,
                app.state.max_upload_bytes,
            )
            job.manuscript_path = str(dest)

            # Optional supplementary information. Saved alongside the
            # manuscript and handed to the methods_completeness auditor only.
            if supplement is not None and supplement_name:
                sup_dest = job_dir / supplement_name
                await run_in_threadpool(
                    _copy_upload,
                    supplement.file,
                    sup_dest,
                    app.state.max_upload_bytes,
                )
                job_overrides["supplement_path"] = str(sup_dest)

            loop = asyncio.get_running_loop()
            bus = EventBus(loop)
            app.state.buses[job.id] = bus

            config = get_config(config_path=app.state.config_path, **job_overrides)
            # The sprite is drawn whenever the desk node will actually run —
            # resolved through the same enablement logic as the graph, so
            # desk_screen_mode = "warm"/"gate" isn't ignored here.
            job.desk_screen = desk_screen_enabled(config)
            runner = JobRunner(job, config, bus, cleanup_dir=job_dir)
            app.state.runners[job.id] = runner
            runner.start()
        except Exception as exc:
            # Release the slot: a failed upload must not wedge the server
            # into refusing every future job with a 409.
            job.status = "error"
            jobs.set_active(None)
            app.state.buses.pop(job.id, None)
            app.state.runners.pop(job.id, None)
            shutil.rmtree(job_dir, ignore_errors=True)
            if isinstance(exc, UploadTooLarge):
                raise HTTPException(413, str(exc)) from exc
            raise

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
        # Prevent path traversal: only files directly inside the job's report
        # directory. Same parent== guard as the history routes — a prefix
        # match also admits sibling dirs like "<report_dir>-evil".
        report_dir = Path(job.report_dir).resolve()
        target = (report_dir / name).resolve()
        if target.parent != report_dir or target.suffix != ".md" or not target.is_file():
            raise HTTPException(404, "report file not found")
        return FileResponse(str(target), media_type="text/markdown")

    @app.get("/jobs/{job_id}/reports")
    async def list_report_files(job_id: str) -> JSONResponse:
        job = jobs.get(job_id)
        if job is None or not job.report_dir:
            return JSONResponse({"files": []})
        report_dir = Path(job.report_dir)
        files = sorted(
            p.name for p in report_dir.iterdir() if p.is_file() and p.suffix == ".md"
        )
        return JSONResponse({"files": files})

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
