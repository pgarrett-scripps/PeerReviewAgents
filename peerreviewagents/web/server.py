"""The browser surface of the one-command engine review.

A manuscript PDF is uploaded, imported into ``<run>/manuscript``, reviewed in
``<run>/review`` and reported as ``REVIEW.md`` beside them, which is exactly
what ``peerreview review manuscript.pdf <out>`` and the MCP server do: the job
state and the path confinement are :mod:`peerreviewagents.review_service`, so
this module is only HTTP over it.

What the LangGraph UI showed has no engine equivalent and is gone rather than
faked: there is no token stream, no per-agent panel, no running cost, no
editorial decision and no desk screen, because the engine runs each stage as
one long model call that reports when it is done. A review's progress is the
stage it is in, which the run records in ``progress.json``; the page polls it.
Target journals, article types and strictness levels were panel parameters and
are gone with it.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from starlette.types import Scope

from ..engine.bundle import PUBLIC_MODES
from ..review_service import ReviewService

_STATIC_DIR = Path(__file__).parent / "static"
# The engine imports PDFs. The legacy panel also took .md/.tex/.txt, which the
# importer has no path for, so naming one is an error rather than a run that
# fails minutes later.
_ALLOWED_SUFFIX = ".pdf"
_MAX_UPLOAD_BYTES = 50 * 1024 * 1024
_COPY_CHUNK_BYTES = 1024 * 1024
_MAX_LISTED_RUNS = 200
_FINISHED = ("done", "done-without-findings", "error", "cancelled")


class UploadTooLarge(ValueError):
    """Raised when a streamed upload crosses the configured byte limit."""


class _NoCacheStaticFiles(StaticFiles):
    """StaticFiles subclass that disables browser caching.

    The UI ships with no fingerprinted asset URLs, so a cached app.js can
    survive across deploys and silently keep buggy behavior alive long after
    the source file on disk was fixed.
    """

    async def get_response(self, path: str, scope: Scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response


def _upload_name(filename: str) -> str:
    """Reduce a client-supplied upload filename to a safe bare basename.

    The filename is attacker-controlled: ``../../x.pdf`` passes a suffix
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


def _optional_int(value: str, field: str) -> int | None:
    text = value.strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        raise HTTPException(400, f"{field} must be a whole number, not {value!r}") from None


def _page(name: str) -> HTMLResponse:
    path = _STATIC_DIR / name
    if not path.is_file():
        raise HTTPException(404, f"{name} missing")
    return HTMLResponse(path.read_text(encoding="utf-8"))


def _saved_runs(root: Path) -> list[dict[str, Any]]:
    """Finished runs on disk under the output root, newest first.

    A run is a folder holding a ``REVIEW.md``: that is what the engine writes
    when a review reports, and a folder without one is an import or a run that
    never got that far.
    """
    if not root.is_dir():
        return []
    runs: list[dict[str, Any]] = []
    try:
        candidates = [path for path in root.iterdir() if path.is_dir()]
    except OSError:
        return []
    for folder in candidates:
        report = folder / "REVIEW.md"
        if not report.is_file():
            continue
        stat = report.stat()
        runs.append({
            "name": folder.name,
            "finished_at": stat.st_mtime,
            "size": stat.st_size,
        })
    runs.sort(key=lambda run: run["finished_at"], reverse=True)
    return runs[:_MAX_LISTED_RUNS]


def _saved_report(root: Path, name: str) -> Path:
    """Resolve ``name`` to a ``REVIEW.md`` directly under the output root."""
    resolved_root = root.resolve()
    folder = (resolved_root / name).resolve()
    if folder.parent != resolved_root:
        raise HTTPException(404, "unknown run")
    report = folder / "REVIEW.md"
    if not report.is_file():
        raise HTTPException(404, "unknown run")
    return report


def create_app(
    *,
    output_dir: str | os.PathLike[str] | None = None,
    upload_dir: str | os.PathLike[str] | None = None,
    input_roots: list[str | os.PathLike[str]] | None = None,
    defaults: dict[str, Any] | None = None,
    max_upload_bytes: int = _MAX_UPLOAD_BYTES,
    max_workers: int = 1,
) -> FastAPI:
    """Build the FastAPI app.

    ``output_dir`` is where every review writes its run folder, ``upload_dir``
    where uploaded manuscripts are kept (it is an input root, so a review can
    read what the browser just sent), ``input_roots`` any further folders a
    manuscript may be read from, and ``defaults`` the form values the page
    opens with (mode, reviewers, provider, model, policy, literature_cutoff).
    """
    if max_upload_bytes < 1:
        raise ValueError("max_upload_bytes must be positive")
    upload_root = Path(upload_dir) if upload_dir else Path.cwd() / ".peerreview-uploads"
    upload_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    output_root = Path(output_dir) if output_dir else Path.cwd() / "reviews"
    output_root.mkdir(parents=True, exist_ok=True)

    roots: list[str | os.PathLike[str]] = [upload_root, *(input_roots or [])]
    app = FastAPI(title="PeerReviewAgents", version="0.8.0")
    app.state.reviews = ReviewService(
        max_workers=max_workers,
        input_roots=roots,
        output_root=output_root,
    )
    app.state.upload_root = upload_root
    app.state.output_root = app.state.reviews.output_root
    app.state.defaults = dict(defaults or {})
    app.state.max_upload_bytes = max_upload_bytes

    _register_routes(app)
    return app


def _register_routes(app: FastAPI) -> None:
    reviews: ReviewService = app.state.reviews

    if _STATIC_DIR.is_dir():
        app.mount("/static", _NoCacheStaticFiles(directory=str(_STATIC_DIR)), name="static")

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        return _page("index.html")

    @app.get("/review.html", response_class=HTMLResponse)
    async def review_page() -> HTMLResponse:
        return _page("review.html")

    @app.get("/api/capabilities")
    async def capabilities() -> JSONResponse:
        """The engine's modes, models, policies and this server's defaults."""
        record = reviews.capabilities()
        record["form_defaults"] = {
            "mode": app.state.defaults.get("mode", "ensemble"),
            "reviewers": app.state.defaults.get("reviewers", 2),
            "provider": app.state.defaults.get("provider") or "",
            "model": app.state.defaults.get("model") or "",
            "policy": app.state.defaults.get("policy") or "",
            "literature_cutoff": app.state.defaults.get("literature_cutoff") or "",
        }
        record["review_modes"] = list(PUBLIC_MODES)
        record["manuscript_format"] = "PDF"
        record["max_upload_bytes"] = app.state.max_upload_bytes
        return JSONResponse(record)

    @app.post("/api/reviews")
    async def start_review(
        manuscript: UploadFile,
        mode: str = Form(""),
        reviewers: str = Form(""),
        provider: str = Form(""),
        model: str = Form(""),
        policy: str = Form(""),
        token_budget: str = Form(""),
        literature_cutoff: str = Form(""),
    ) -> JSONResponse:
        """Upload a manuscript PDF and start one engine review of it."""
        name = _upload_name(manuscript.filename or "")
        if Path(name).suffix.lower() != _ALLOWED_SUFFIX:
            raise HTTPException(
                400,
                f"the engine reviews PDFs; {Path(name).suffix or name!r} is not one",
            )
        defaults = app.state.defaults
        options: dict[str, Any] = {
            "mode": mode.strip() or defaults.get("mode") or "ensemble",
            "reviewers": _optional_int(reviewers, "reviewers")
            or int(defaults.get("reviewers") or 2),
            "provider": provider.strip() or defaults.get("provider") or None,
            "model": model.strip() or defaults.get("model") or None,
            "policy": policy.strip() or defaults.get("policy") or None,
            "token_budget": _optional_int(token_budget, "token_budget")
            or defaults.get("token_budget"),
            "literature_cutoff": literature_cutoff.strip()
            or defaults.get("literature_cutoff")
            or None,
        }

        folder = Path(app.state.upload_root) / f"{int(time.time() * 1000):x}"
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        dest = folder / name
        try:
            # Copy off the event loop: a large upload written synchronously
            # here freezes every other request for its duration.
            await run_in_threadpool(
                _copy_upload, manuscript.file, dest, app.state.max_upload_bytes
            )
        except UploadTooLarge as exc:
            raise HTTPException(413, str(exc)) from exc

        try:
            return JSONResponse(reviews.start_review(str(dest), **options))
        except (ValueError, PermissionError) as exc:
            # Engine refusals reach the browser in the engine's own words.
            raise HTTPException(400, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/api/reviews")
    async def list_reviews() -> JSONResponse:
        return JSONResponse({"reviews": reviews.list_jobs()})

    @app.get("/api/reviews/{job_id}")
    async def review_status(job_id: str) -> JSONResponse:
        try:
            return JSONResponse(reviews.get_status(job_id))
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.post("/api/reviews/{job_id}/cancel")
    async def cancel_review(job_id: str) -> JSONResponse:
        """Cancel a queued review. A running model call cannot be interrupted."""
        try:
            return JSONResponse(reviews.cancel(job_id))
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.get("/api/reviews/{job_id}/artifacts")
    async def list_review_artifacts(job_id: str) -> JSONResponse:
        try:
            return JSONResponse({"artifacts": reviews.list_artifacts(job_id)})
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.get("/api/reviews/{job_id}/artifacts/{name:path}", response_class=PlainTextResponse)
    async def read_review_artifact(job_id: str, name: str) -> PlainTextResponse:
        try:
            text = reviews.read_artifact(job_id, name)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        return PlainTextResponse(text, media_type="text/markdown; charset=utf-8")

    @app.get("/api/runs")
    async def saved_runs() -> JSONResponse:
        """Reviews already on disk, so a restarted server still finds them."""
        return JSONResponse({"runs": _saved_runs(Path(app.state.output_root))})

    @app.get("/api/runs/{name}/report", response_class=PlainTextResponse)
    async def saved_run_report(name: str) -> PlainTextResponse:
        report = _saved_report(Path(app.state.output_root), name)
        return PlainTextResponse(
            report.read_text(encoding="utf-8", errors="replace"),
            media_type="text/markdown; charset=utf-8",
        )

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        jobs = reviews.list_jobs()
        return JSONResponse({
            "status": "ok",
            "running": sum(1 for job in jobs if job["status"] not in _FINISHED),
        })
