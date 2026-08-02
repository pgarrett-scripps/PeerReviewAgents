"""Manuscript ingestion: file -> text + a coarse section map.

PDF parsing is local — no external API, no API key, no GPU — and has two
backends. :mod:`.structured` converts to Markdown with rustypdf, keeping
headings, tables and equations; ``pypdf`` extracts the raw text layer
page-by-page and flattens everything. The first is much better and is
optional; the second is a hard dependency and always works. ``pdf_backend``
picks between them and defaults to ``"auto"``: try the good one, fall back,
and record which happened so a published review can say how it was read.

**The file handed in is always the original.** Converting a PDF to Markdown
before calling the pipeline looks equivalent and is not: the submission
integrity screen (:mod:`.integrity`) dispatches on file type and can only
find concealed text — white fill, zero opacity, off-page placement — by
reading the PDF's content streams. Hand it a converted ``.md`` and the screen
still reports as having run, having looked for nothing it can find. So
conversion happens *here*, behind the loader, where the screen has already
seen the real bytes.

Other supported inputs: Markdown / LaTeX / TXT (read directly), DOCX
(via python-docx). All paths converge on ``(title, text, sections)``.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from ..observability import AgentEvent, emit
from . import structured

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


def ingest_config(config: dict | None = None) -> dict:
    """The slice of config that changes what ingestion produces.

    Split out because it is hashed into the manuscript cache key. Two runs
    that read the same bytes through different backends — or at different
    compression levels — are two different manuscripts, and letting them
    share a cache entry would serve one run the other's text.
    """
    config = config or {}
    return {
        "pdf_backend": config.get("pdf_backend") or "auto",
        "caveman": config.get("caveman") or "off",
    }


@dataclass(frozen=True)
class Manuscript:
    """A parsed manuscript, plus the record of how it was parsed."""

    title: str
    text: str
    sections: dict[str, str] = field(default_factory=dict)
    # Published verbatim in a review's provenance. Shape:
    #   format  "markdown" (converted, structure preserved) | "text" (flat)
    #   tool    what produced it, with version, e.g. "rustypdf 0.1.0"
    #   caveman compression level applied, or None
    #   chars   length of the parsed text
    #   reason  why the structured backend was not used; "" when it was
    ingest: dict = field(default_factory=dict)

    def as_triple(self) -> tuple[str, str, dict[str, str]]:
        return self.title, self.text, self.sections


def _plain_ingest(tool: str, text: str, reason: str = "") -> dict:
    return {
        "format": "text",
        "tool": tool,
        "caveman": None,
        "chars": len(text),
        "reason": reason,
    }


def load_manuscript(
    path: str, config: dict | None = None
) -> tuple[str, str, dict[str, str]]:
    """Return ``(title, text, sections)`` for the given manuscript file.

    The convenience form. Callers that publish how the manuscript was read —
    the graph, and anything writing provenance — want
    :func:`load_manuscript_record` instead.
    """
    return load_manuscript_record(path, config).as_triple()


def load_manuscript_record(path: str, config: dict | None = None) -> Manuscript:
    """Parse ``path``, or serve it from the on-disk cache.

    The result is cached keyed by file content *and* the ingest config (see
    :mod:`.cache`). Wipe the cache with ``just cache-clear`` if you need to
    force a re-parse.
    """
    config = config or {}
    from . import cache as _cache

    key = _cache.cache_key(path, config)
    cached = _cache.get(key, config)
    if cached is not None:
        return cached

    parsed = _load_uncached(path, config)
    try:
        _cache.put(key, parsed, source_path=path, config=config)
    except (OSError, ValueError):
        # Cache write failure is non-fatal: the run still has the parsed
        # text in memory, so we just skip persisting it. ValueError covers
        # encoding failures on text this loader could not sanitise — losing
        # the cache entry is the right cost, losing the review is not.
        pass
    return parsed


def _load_uncached(path: str, config: dict | None = None) -> Manuscript:
    ext = os.path.splitext(path)[1].lower()
    title = ""
    anchor = ""
    if ext == ".pdf":
        text, title, anchor, record = _read_pdf(path, ingest_config(config))
    elif ext in (".md", ".markdown", ".txt", ".tex"):
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
        record = _plain_ingest(f"read as {ext.lstrip('.')}", text)
        # A submitted Markdown file already carries its own structure, so it
        # is "markdown" for the same reason a converted PDF is: quotations
        # and section boundaries can be trusted.
        if ext in (".md", ".markdown"):
            record["format"] = "markdown"
    elif ext == ".docx":
        text = _read_docx(path)
        record = _plain_ingest("python-docx", text)
    else:
        raise ValueError(f"Unsupported manuscript type: {ext}")

    text = _normalize(text)
    record["chars"] = len(text)
    if not title:
        title = _guess_title(text, fallback=os.path.basename(path))
    return Manuscript(
        title=title,
        text=text,
        sections=_split_sections(text, references_anchor=anchor),
        ingest=record,
    )


def _read_pdf(path: str, ingest: dict) -> tuple[str, str, str, dict]:
    """Read a PDF through the configured backend.

    Returns ``(text, title, references_anchor, record)``. The title is empty
    unless the backend identified one itself, in which case it beats the
    loader's heuristic; the anchor is empty unless it located a bibliography.

    ``pdf_backend`` is ``"auto"`` (prefer rustypdf, fall back to pypdf),
    ``"rustypdf"`` (require it) or ``"pypdf"`` (never try it). Only ``auto``
    falls back — an explicit choice that silently became a different choice
    would be worse than an error, because the difference shows up in the
    review rather than in the log.
    """
    backend = ingest["pdf_backend"]
    caveman = ingest["caveman"]
    if backend not in ("auto", "rustypdf", "pypdf"):
        raise ValueError(
            f"unknown pdf_backend {backend!r}; expected 'auto', 'rustypdf' or 'pypdf'"
        )

    reason = ""
    if backend in ("auto", "rustypdf"):
        try:
            converted = structured.convert(path, caveman)
        except structured.Unavailable as exc:
            if backend == "rustypdf":
                raise RuntimeError(
                    f"pdf_backend is 'rustypdf' but it could not read {path}: {exc}"
                ) from exc
            reason = str(exc)
            emit(AgentEvent(
                kind="log", node="ingest",
                text=f"falling back to pypdf — {reason}",
            ))
        else:
            emit(AgentEvent(
                kind="log", node="ingest",
                text=f"read {os.path.basename(path)} as markdown via "
                     f"{converted.tool}"
                     + (f" (caveman {caveman})" if caveman != "off" else ""),
            ))
            return converted.markdown, converted.title, converted.references_anchor, {
                "format": "markdown",
                "tool": converted.tool,
                "caveman": None if caveman == "off" else caveman,
                "chars": len(converted.markdown),
                "reason": "",
            }

    return _read_pypdf(path), "", "", _plain_ingest("pypdf", "", reason)


def _read_pypdf(path: str) -> str:
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
    return _drop_surrogates(text.strip())


def _drop_surrogates(text: str) -> str:
    """Replace lone surrogates, which are not encodable as UTF-8.

    A PDF with a broken encoding map makes pypdf emit unpaired surrogates.
    Python holds them happily and then refuses to encode them, so they used
    to abort the run at the first thing that serialises the text — in
    practice the cache write, several seconds into a review, with a
    ``UnicodeEncodeError`` that names a codec rather than a manuscript. Every
    provider request would have hit the same wall a moment later. One
    substitution here keeps the rest of the pipeline unable to encounter it.
    """
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return text.encode("utf-8", "replace").decode("utf-8")
    return text


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


def _split_sections(text: str, references_anchor: str = "") -> dict[str, str]:
    """Best-effort bucketing of text into known sections by heading match.

    When the document has real headings — Markdown, submitted or converted —
    only those lines are considered. Flat text has none, so every line is a
    candidate and a body line reading "Results." can start a section that
    isn't there; with structure available that guesswork is unnecessary.

    ``references_anchor`` is the opening of the first bibliography entry, as
    identified by the converter's block types rather than by a heading. When
    given, the line it appears on starts the references section even if no
    "References" heading survived conversion — which on real papers it often
    does not.

    Note that Markdown headings are *not* treated as the only candidates,
    tempting as that is. Measured across a ten-paper corpus it loses more
    than it gains: on a two-column IEEE paper the converter marked up four
    headings, none of them the real sections, and restricting the match to
    those lines took the document from five sections to two. A heading that
    was not detected as a heading is still a line reading "Introduction".
    """
    anchor = references_anchor.strip()

    sections: dict[str, list[str]] = {"_preamble": []}
    current = "_preamble"
    for line in text.splitlines():
        # Checked before the heading match: the anchor's whole purpose is to
        # work on a line no heading rule will fire on. The line is kept —
        # unlike a heading, it is a reference in its own right.
        if anchor and current != "references" and anchor in line:
            current = "references"
            sections.setdefault(current, []).append(line)
            continue
        candidate = line.strip().lower().lstrip("#").strip().rstrip(":").strip()
        # Strip leading numeric / roman-numeral prefixes ("1.", "2.1", "II.").
        numbered = bool(_NUMERIC_PREFIX_RE.match(candidate))
        candidate = _NUMERIC_PREFIX_RE.sub("", candidate).strip()
        matched = next((k for k in _SECTION_KEYS if candidate == k or candidate.startswith(k + " ")), None)
        # The length guard keeps a body sentence opening "Discussion of these
        # results…" from starting a section. A *numbered* line needs no such
        # protection — prose does not begin "IV. " — and waiving it for those
        # recovers headings a converter fused into the paragraph below them,
        # which is how `I. Introduction LECTROMAGNETIC (EM) metasurfaces…`
        # arrives. That line is a heading with its section's first sentence
        # stuck to it, and treating it as one puts the section back.
        if matched and (numbered or len(line.strip()) < 80):
            # "bibliography" is an alias; normalize so the literature reviewer finds it.
            bucket = "references" if matched == "bibliography" else matched
            current = bucket
            sections.setdefault(current, [])
            # A short line is a heading and nothing else, so it is dropped. A
            # long one carries the section's opening sentence, so it is kept
            # whole — the few words of heading left at its front cost far
            # less than the paragraph would.
            if len(line.strip()) >= 80:
                sections[current].append(line)
            continue
        sections.setdefault(current, []).append(line)
    return {k: "\n".join(v).strip() for k, v in sections.items() if "".join(v).strip()}
