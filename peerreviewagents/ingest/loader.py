"""Manuscript ingestion: file -> normalized Markdown + a coarse section map."""

from __future__ import annotations

import os
import re

# Common manuscript section headings we try to bucket text into.
_SECTION_KEYS = [
    "abstract", "introduction", "background", "related work", "methods",
    "materials and methods", "methodology", "results", "discussion",
    "conclusion", "conclusions", "references", "acknowledgements",
]

_HEADING_RE = re.compile(r"^\s{0,3}(#{1,6}\s+|\d+\.?\s+)?(.{0,80})$")


def load_manuscript(path: str) -> tuple[str, str, dict[str, str]]:
    """Return (title, full_markdown, sections)."""
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
    from pypdf import PdfReader

    reader = PdfReader(path)
    return "\n\n".join((page.extract_text() or "") for page in reader.pages)


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
    for line in text.splitlines():
        line = line.strip().lstrip("# ").strip()
        if len(line) > 8:
            return line[:200]
    return fallback


def _split_sections(text: str) -> dict[str, str]:
    """Best-effort bucketing of text into known sections by heading match."""
    sections: dict[str, list[str]] = {"_preamble": []}
    current = "_preamble"
    for line in text.splitlines():
        candidate = line.strip().lower().lstrip("#").strip().rstrip(":").strip()
        matched = next((k for k in _SECTION_KEYS if candidate == k or candidate.startswith(k + " ")), None)
        if matched and len(line.strip()) < 60:
            current = matched
            sections.setdefault(current, [])
            continue
        sections.setdefault(current, []).append(line)
    return {k: "\n".join(v).strip() for k, v in sections.items() if "".join(v).strip()}
