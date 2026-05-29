"""Manuscript ingestion: file -> clean Markdown + a coarse section map.

PDF parsing goes through the hosted Datalab marker API
(https://www.datalab.to/api/v1/marker), which runs the same marker engine
that used to ship locally but on Datalab's GPU fleet — typically returning
in seconds rather than the minutes the CPU build took. Requires
``DATALAB_API_KEY`` in the environment.

Each extracted figure is sent to the configured `vision_model` and its
prose description is injected into the markdown so text-only reviewer
LLMs can reason about figure content.
"""

from __future__ import annotations

import base64
import io
import os
import re
import time

# Common manuscript section headings we try to bucket text into.
_SECTION_KEYS = [
    "abstract", "introduction", "background", "related work", "methods",
    "materials and methods", "materials & methods", "methodology", "results",
    "discussion", "conclusion", "conclusions", "references", "acknowledgements",
    "bibliography", "limitations", "supplementary", "supplementary materials",
    "supplementary information", "experimental", "data availability",
]

# Phrases that indicate boilerplate rather than a real title.
_TITLE_BOILERPLATE = (
    "this document is",
    "confidential",
    "proprietary",
    "do not distribute",
    "draft",
    "preprint",
    "abstract",
    "running title",
)

# Regex to strip leading numeric / roman-numeral section prefixes, e.g.
# "1.", "1.1", "2.1.", "II.", "A." before comparing to _SECTION_KEYS.
_NUMERIC_PREFIX_RE = re.compile(
    r"^(?:[ivxlcdm]+\.|\d+(?:\.\d+)*\.?)\s*", re.IGNORECASE
)

from ..observability import AgentEvent, emit

_DATALAB_ENDPOINT = "https://www.datalab.to/api/v1/marker"
# Datalab deletes response data ~1 hour after completion; the polling
# cap below (10 min) is comfortably inside that window for normal papers.
_DATALAB_POLL_INTERVAL_S = 2.0
_DATALAB_POLL_MAX_ATTEMPTS = 300


def load_manuscript(
    path: str, config: dict | None = None
) -> tuple[str, str, dict[str, str]]:
    """Return (title, full_markdown, sections).

    The Datalab parse output (markdown + figure images) is cached on
    disk (see :mod:`.cache`). The vision-model pass that turns figure
    images into prose runs every time on top of the cached parse, so
    swapping ``vision_model`` doesn't bust the expensive parse and each
    run gets fresh figure descriptions. Wipe the cache with
    `just cache-clear` if you need to force a re-parse.
    """
    config = config or {}
    from . import cache as _cache

    key = _cache.cache_key(path, config)
    cached = _cache.get(key, config)
    if cached is not None:
        title, raw_text, sections, images = cached
    else:
        title, raw_text, sections, images = _load_uncached(path, config)
        try:
            _cache.put(
                key, title, raw_text, sections, images,
                source_path=path, config=config,
            )
        except OSError:
            # Cache write failure is non-fatal: log nothing, return the
            # result so the run can still proceed.
            pass

    # Vision pass: deliberately runs OUTSIDE the cache so changing the
    # vision model doesn't invalidate the cached Datalab parse, and so
    # users always see live figure descriptions in the TUI.
    text = raw_text
    if images:
        from .vision import describe_figures_inline

        text = describe_figures_inline(raw_text, images, config)
    return title, text, sections


def _load_uncached(
    path: str, config: dict
) -> tuple[str, str, dict[str, str], dict]:
    """Parse a manuscript from scratch.

    Returns ``(title, raw_markdown, sections, images)`` where ``images``
    is a dict ``{filename: PIL.Image}`` of extracted figures (empty for
    non-PDF inputs). The markdown does **not** include vision-model
    figure annotations — that's applied by ``load_manuscript`` outside
    the cache layer.
    """
    ext = os.path.splitext(path)[1].lower()
    images: dict = {}
    if ext == ".pdf":
        text, images = _read_pdf_with_datalab(path, config)
    elif ext in (".md", ".markdown", ".txt", ".tex"):
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    elif ext == ".docx":
        text = _read_docx(path)
    else:
        raise ValueError(f"Unsupported manuscript type: {ext}")

    text = _normalize(text)
    title = _guess_title(text, fallback=os.path.basename(path))
    sections = _split_sections(text)
    return title, text, sections, images


def _read_pdf_with_datalab(path: str, config: dict) -> tuple[str, dict]:
    """POST the PDF to Datalab, poll until done, return (markdown, images).

    Image extraction is always requested; base64 PNGs are decoded back
    to PIL.Images and returned alongside the markdown so the cache can
    persist them and the vision pass can describe them on every run.
    """
    try:
        import requests
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "PDF ingest requires the `requests` package. "
            "Install with: pip install -e '.[pdf-ingest]'"
        ) from exc

    api_key = os.environ.get("DATALAB_API_KEY")
    if not api_key:
        raise RuntimeError(
            "DATALAB_API_KEY is not set. Add it to your .env or environment "
            "to ingest PDFs via the Datalab marker API."
        )

    headers = {"X-Api-Key": api_key}
    form: dict[str, tuple[None, str]] = {
        "output_format": (None, "markdown"),
        "paginate": (None, "false"),
        "force_ocr": (None, str(bool(config.get("pdf_force_ocr", False))).lower()),
        "use_llm": (None, str(bool(config.get("pdf_use_llm", False))).lower()),
        "disable_image_extraction": (None, "false"),
    }
    if config.get("pdf_max_pages"):
        form["max_pages"] = (None, str(int(config["pdf_max_pages"])))
    if config.get("pdf_page_range"):
        form["page_range"] = (None, str(config["pdf_page_range"]))
    if config.get("pdf_langs"):
        form["langs"] = (None, str(config["pdf_langs"]))

    emit(AgentEvent(kind="log", node="ingest", text="uploading PDF to Datalab…"))
    with open(path, "rb") as fh:
        files = dict(form)
        files["file"] = (os.path.basename(path), fh, "application/pdf")
        resp = requests.post(_DATALAB_ENDPOINT, files=files, headers=headers, timeout=60)
    resp.raise_for_status()
    payload = resp.json()
    if not payload.get("success"):
        raise RuntimeError(f"Datalab upload failed: {payload.get('error', payload)}")

    check_url = payload["request_check_url"]
    emit(AgentEvent(kind="log", node="ingest", text="waiting for Datalab to parse PDF…"))
    result = _datalab_poll(check_url, headers, requests)
    markdown = result.get("markdown") or ""
    if not markdown:
        raise RuntimeError(f"Datalab returned empty markdown: {result.get('error', result)}")

    images = _decode_images(result.get("images") or {})
    return markdown, images


def _datalab_poll(check_url: str, headers: dict, requests_mod) -> dict:
    for _ in range(_DATALAB_POLL_MAX_ATTEMPTS):
        time.sleep(_DATALAB_POLL_INTERVAL_S)
        r = requests_mod.get(check_url, headers=headers, timeout=30)
        r.raise_for_status()
        data = r.json()
        if data.get("status") == "complete":
            if not data.get("success"):
                raise RuntimeError(f"Datalab processing failed: {data.get('error', data)}")
            return data
    raise TimeoutError(
        f"Datalab did not finish within "
        f"{_DATALAB_POLL_INTERVAL_S * _DATALAB_POLL_MAX_ATTEMPTS:.0f}s"
    )


def _decode_images(images: dict) -> dict:
    """API returns {filename: base64-PNG}; vision.py expects {filename: PIL.Image}."""
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover
        return {}
    out = {}
    for fname, b64 in images.items():
        try:
            out[fname] = Image.open(io.BytesIO(base64.b64decode(b64)))
        except Exception:  # noqa: BLE001
            # Skip any image we can't decode rather than failing the whole parse.
            continue
    return out


def _read_docx(path: str) -> str:
    try:
        import docx  # python-docx
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("Install python-docx to read .docx files") from exc
    doc = docx.Document(path)
    return "\n".join(p.text for p in doc.paragraphs)


def _normalize(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _guess_title(text: str, fallback: str) -> str:
    def _is_boilerplate(s: str) -> bool:
        low = s.lower()
        return any(low.startswith(p) for p in _TITLE_BOILERPLATE)

    lines = text.splitlines()

    # Pass 1: first h1 heading.  Length guard is relaxed for structured
    # headings (> 4) since the markdown prefix already filters stray lines.
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("# ") and not stripped.startswith("## "):
            candidate = stripped.lstrip("# ").strip()
            if len(candidate) > 4 and not _is_boilerplate(candidate):
                return candidate[:200]

    # Pass 2: first h2 heading.
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("## ") and not stripped.startswith("### "):
            candidate = stripped.lstrip("# ").strip()
            if len(candidate) > 4 and not _is_boilerplate(candidate):
                return candidate[:200]

    # Pass 3: first non-blank, non-boilerplate plain line >8 chars.
    for line in lines:
        candidate = line.strip().lstrip("# ").strip()
        if len(candidate) > 8 and not _is_boilerplate(candidate):
            return candidate[:200]

    return fallback


def _split_sections(text: str) -> dict[str, str]:
    """Best-effort bucketing of text into known sections by heading match."""
    sections: dict[str, list[str]] = {"_preamble": []}
    current = "_preamble"
    for line in text.splitlines():
        candidate = line.strip().lower().lstrip("#").strip().rstrip(":").strip()
        # Strip leading numeric / roman-numeral prefixes ("1.", "2.1", "II.").
        candidate = _NUMERIC_PREFIX_RE.sub("", candidate).strip()
        matched = next((k for k in _SECTION_KEYS if candidate == k or candidate.startswith(k + " ")), None)
        if matched and len(line.strip()) < 80:
            # "bibliography" is an alias; normalize so the literature reviewer finds it.
            bucket = "references" if matched == "bibliography" else matched
            current = bucket
            sections.setdefault(current, [])
            continue
        sections.setdefault(current, []).append(line)
    return {k: "\n".join(v).strip() for k, v in sections.items() if "".join(v).strip()}
