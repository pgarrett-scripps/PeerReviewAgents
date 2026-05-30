"""Manuscript ingestion: file -> plain text + a coarse section map.

PDF parsing uses ``pypdf`` locally — no external API, no API key, no GPU.
Text is extracted page-by-page and concatenated; images, tables, and
multi-column layout fidelity are deliberately out of scope. For most
academic manuscripts pypdf's text-layer extraction is good enough, and
the LLM is robust to mild ordering oddities.

Other supported inputs: Markdown / LaTeX / TXT (read directly), DOCX
(via python-docx). All paths converge on ``(title, text, sections)``.
"""

from __future__ import annotations

import os
import re

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


def load_manuscript(
    path: str, config: dict | None = None
) -> tuple[str, str, dict[str, str]]:
    """Return ``(title, text, sections)`` for the given manuscript file.

    The parsed triple is cached on disk keyed by file content (see
    :mod:`.cache`). Wipe the cache with ``just cache-clear`` if you need
    to force a re-parse.
    """
    config = config or {}
    from . import cache as _cache

    key = _cache.cache_key(path, config)
    cached = _cache.get(key, config)
    if cached is not None:
        return cached

    title, text, sections = _load_uncached(path)
    try:
        _cache.put(
            key, title, text, sections,
            source_path=path, config=config,
        )
    except OSError:
        # Cache write failure is non-fatal: the run still has the parsed
        # text in memory, so we just skip persisting it.
        pass
    return title, text, sections


def _load_uncached(path: str) -> tuple[str, str, dict[str, str]]:
    ext = os.path.splitext(path)[1].lower()
    if ext == ".pdf":
        text = _read_pdf(path)
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


def _read_pdf(path: str) -> str:
    """Extract text from a PDF using pypdf, page-by-page.

    Pages are joined with a blank line so downstream section-splitting
    sees natural paragraph boundaries. Pages whose text extraction
    raises (corrupt page, font issues) are skipped rather than aborting
    the whole document.
    """
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "PDF ingest requires the `pypdf` package. "
            "Install with: pip install pypdf"
        ) from exc

    emit(AgentEvent(kind="log", node="ingest", text=f"reading PDF: {os.path.basename(path)}"))
    reader = PdfReader(path)
    parts: list[str] = []
    for i, page in enumerate(reader.pages):
        try:
            page_text = page.extract_text() or ""
        except Exception as exc:  # noqa: BLE001
            emit(AgentEvent(
                kind="log",
                node="ingest",
                text=f"page {i + 1}: extraction failed ({exc}); skipping",
            ))
            continue
        if page_text.strip():
            parts.append(page_text)
    if not parts:
        raise RuntimeError(
            f"pypdf extracted no text from {path}. The PDF may be scanned "
            "(image-only) with no text layer — OCR is out of scope for this "
            "ingest path; convert to text/Markdown first."
        )
    emit(AgentEvent(
        kind="log",
        node="ingest",
        text=f"extracted {len(parts)} of {len(reader.pages)} pages",
    ))
    return "\n\n".join(parts)


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
