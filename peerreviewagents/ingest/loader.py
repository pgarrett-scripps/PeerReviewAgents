"""Manuscript ingestion: file -> clean Markdown + a coarse section map.

PDF parsing uses marker (datalab-to/marker), which reflows two-column layouts,
emits proper markdown tables, and inlines figure references next to their
captions. When `vision_enabled` is set in config, each extracted figure is
also sent to a vision model and its prose description is injected into the
markdown so text-only reviewer LLMs can reason about figure content.
"""

from __future__ import annotations

import os
import re
from functools import lru_cache

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


def load_manuscript(
    path: str, config: dict | None = None
) -> tuple[str, str, dict[str, str]]:
    """Return (title, full_markdown, sections).

    Transparently uses an on-disk cache (see :mod:`.cache`). Disable with
    ``config["cache_enabled"] = False``.
    """
    config = config or {}
    use_cache = config.get("cache_enabled", True)

    if use_cache:
        from . import cache as _cache

        key = _cache.cache_key(path, config)
        cached = _cache.get(key, config)
        if cached is not None:
            return cached

    title, text, sections = _load_uncached(path, config)

    if use_cache:
        try:
            _cache.put(key, title, text, sections, source_path=path, config=config)
        except OSError:
            # Cache write failure is non-fatal: log nothing, return the
            # result so the run can still proceed.
            pass
    return title, text, sections


def _load_uncached(path: str, config: dict) -> tuple[str, str, dict[str, str]]:
    ext = os.path.splitext(path)[1].lower()
    if ext == ".pdf":
        text = _read_pdf_with_marker(path, config)
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
    return title, text, sections


@lru_cache(maxsize=1)
def _marker_converter():
    """Marker's PdfConverter loads ~1GB of Surya weights — build it once
    per process and reuse across calls.

    Defaults to CPU; small consumer GPUs (<8GB VRAM) OOM during Surya's
    layout pass. Users with capable GPUs can opt in via TORCH_DEVICE=cuda.
    """
    os.environ.setdefault("TORCH_DEVICE", "cpu")
    from marker.converters.pdf import PdfConverter
    from marker.models import create_model_dict

    return PdfConverter(artifact_dict=create_model_dict())


def _read_pdf_with_marker(path: str, config: dict) -> str:
    from marker.output import text_from_rendered

    rendered = _marker_converter()(path)
    markdown, _ext, images = text_from_rendered(rendered)

    if config.get("vision_enabled"):
        from .vision import describe_figures_inline

        markdown = describe_figures_inline(markdown, images, config)

    return markdown


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
            # "bibliography" is an alias; normalize so citations auditor finds it.
            bucket = "references" if matched == "bibliography" else matched
            current = bucket
            sections.setdefault(current, [])
            continue
        sections.setdefault(current, []).append(line)
    return {k: "\n".join(v).strip() for k, v in sections.items() if "".join(v).strip()}
