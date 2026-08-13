"""Manuscript ingestion: file -> text + a coarse section map.

PDF parsing is local — no external API, no API key, no GPU — and goes through
exactly one converter, :mod:`.structured`, which renders the PDF to Markdown
with rustypaper and keeps headings, tables and equations.

**There is deliberately no fallback.** pypdf, which this used to fall back to,
returns the raw text layer in content-stream order: on one real submission it
fused 2% of all words into runs like
``comparableefficacyatlowerdoseusingonlycausallyavailableinformation``, lost
about a sixth of the content, and flattened every heading and table into
prose. A panel reading that produces a review of a document the authors did
not write, and a fallback makes it happen on exactly the runs nobody is
watching. A missing converter is now an error with an install line in it.

**The file handed in is always the original.** Converting a PDF to Markdown
before calling the pipeline looks equivalent and is not: every run records
which converter read the manuscript, and a conversion done upstream is
recorded as though this one did it. So conversion happens *here*, behind the
loader, where what the panel read and what the record claims cannot diverge.

Other supported inputs: Markdown / LaTeX / TXT, read directly. All paths
converge on ``(title, text, sections)``.

The section map has two sources, and which one a run used is recorded on the
ingest record as ``section_source``. A converted PDF has a document model
behind it, so its sections are *read* from the converter's own section tree
(:func:`_sections_from_outline`). Everything else — a Markdown submission, a
LaTeX source, an older converter — has only the text, so its sections are
*guessed* from lines that look like headings (:func:`_split_sections`). Both
produce the same map, and both must keep working: the guess is what a
non-PDF submission has, and it is not going away.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field

from ..observability import AgentEvent, emit
from . import prose, structured

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

# Synonym families collapsed to one bucket name at split time. The revision
# diff matches sections by exact name, so before this map a "Conclusion"
# heading renamed "Conclusions" between rounds reported the identical text as
# a removed section plus an added one — a substantive change the authors
# never made. Every canonical name here is also in agent_utils'
# _PRIORITY_SECTIONS, so fit_manuscript keeps finding the priority sections.
_SECTION_ALIASES = {
    "bibliography": "references",
    "conclusions": "conclusion",
    "materials and methods": "methods",
    "materials & methods": "methods",
    "methodology": "methods",
    "supplementary materials": "supplementary",
    "supplementary information": "supplementary",
}

# Regex to strip leading numeric / roman-numeral section prefixes, e.g.
# "1.", "1.1", "2.1.", "II." before comparing to _SECTION_KEYS. Roman
# numerals match uppercase only: with IGNORECASE, any word spelled from
# [ivxlcdm] read as one — a body line opening "Mild. …" counted as numbered
# and was waved past the heading length guard.
_NUMERIC_PREFIX_RE = re.compile(r"^(?:[IVXLCDM]+\.|\d+(?:\.\d+)*\.?)\s*")


def ingest_config(config: dict | None = None) -> dict:
    """The slice of config that changes what ingestion produces.

    Split out because it is hashed into the manuscript cache key. Two runs
    that read the same bytes at different compression levels are two
    different manuscripts, and letting them share a cache entry would serve
    one run the other's text.
    """
    config = config or {}
    return {"caveman": config.get("caveman") or "off"}


@dataclass(frozen=True)
class Manuscript:
    """A parsed manuscript, plus the record of how it was parsed."""

    title: str
    text: str
    sections: dict[str, str] = field(default_factory=dict)
    # The bibliography as typed entries, in the order the document prints
    # them; see :data:`.structured.Converted.references` for the shape. Empty
    # for a Markdown or LaTeX submission, which has no document model, and for
    # a converter that types no reference blocks — so every consumer keeps the
    # path that reads the prose.
    references: list[dict] = field(default_factory=list)
    # Published verbatim in a review's provenance. Shape:
    #   format  "markdown" (structure preserved) | "text" (flat)
    #   tool    what produced it, with version, e.g. "rustypaper 0.1.0"
    #   caveman compression level applied, or None
    #   chars   length of the parsed text
    #   text_sha256  fingerprint of the converted text — NOT of the file; see
    #           _load_uncached for why the two are different questions
    #   prose   deterministic text statistics, see :mod:`.prose`
    ingest: dict = field(default_factory=dict)

    def as_triple(self) -> tuple[str, str, dict[str, str]]:
        return self.title, self.text, self.sections

    def health(self) -> str:
        """Conversion verdict from :mod:`.prose`, or ``"clean"`` if unmeasured."""
        return prose.verdict_of(self.ingest)

    def health_notes(self) -> list[str]:
        """Plain-language reasons behind a non-clean verdict."""
        return prose.notes_of(self.ingest)


class ManuscriptUnreadable(RuntimeError):
    """The file converted badly enough that reviewing it would be reviewing
    the converter.

    Raised before any agent is paid, and deliberately *not* a desk rejection:
    a desk rejection is a judgment about a manuscript, and this is a statement
    about a file. The two must not arrive looking the same, because a review
    bundle recording "reject" would follow the paper around as a verdict on
    work no model ever read.
    """

    def __init__(self, verdict: str, notes: list[str]):
        self.verdict = verdict
        self.notes = list(notes)
        detail = "; ".join(self.notes) or "see the ingest statistics"
        super().__init__(
            f"the manuscript converted {verdict}: {detail}. No review was run. "
            "This is a conversion failure, not an assessment of the paper — a "
            "scanned or image-only PDF is the usual cause."
        )


def conversion_gate(config: dict | None = None) -> str:
    """Resolve the conversion-health gate: ``"broken"`` | ``"degraded"`` | ``"off"``.

    Default ``"broken"``. The calibration corpus found no middle ground —
    healthy conversions score 0.0 fused tokens per 1000 words and broken ones
    score 22.8 — so stopping at ``broken`` costs nothing on a readable paper
    and saves a full panel on an unreadable one. ``degraded`` is for callers
    who would rather resubmit than read around damage; ``off`` restores the
    prior behaviour of reviewing whatever arrives.
    """
    raw = str((config or {}).get("conversion_gate") or "broken").lower().strip()
    return raw if raw in ("broken", "degraded", "off") else "broken"


def require_readable(ingest: dict | None, config: dict | None = None) -> None:
    """Raise :class:`ManuscriptUnreadable` if the conversion failed the gate.

    Takes the stored ingest record rather than a :class:`Manuscript` so the
    desk node can ask the question straight off ``state["ingest"]``.

    Called at the desk, on the manuscript only — not inside the parser, which
    also reads supplementary information and prior rounds. A damaged SI table
    is a reason to note something, not to abandon the review.
    """
    gate = conversion_gate(config)
    if gate == "off":
        return
    verdict = prose.verdict_of(ingest)
    if verdict == prose.BROKEN or (gate == "degraded" and verdict == prose.DEGRADED):
        raise ManuscriptUnreadable(verdict, prose.notes_of(ingest))


def _plain_ingest(tool: str, text: str) -> dict:
    return {"format": "text", "tool": tool, "caveman": None, "chars": len(text)}


def load_manuscript(
    path: str, config: dict | None = None, *, kind: str = "manuscript"
) -> tuple[str, str, dict[str, str]]:
    """Return ``(title, text, sections)`` for the given manuscript file.

    The convenience form. Callers that publish how the manuscript was read —
    the graph, and anything writing provenance — want
    :func:`load_manuscript_record` instead.

    ``kind`` names what the document is expected to be ("manuscript",
    "letter", "supplement"). It does not change the parse; it sets the
    too-short-to-be-real floor and how a refusal is worded — a one-page
    response letter is a normal document, not a failed manuscript scan.
    """
    return load_manuscript_record(path, config, kind=kind).as_triple()


def load_manuscript_record(
    path: str, config: dict | None = None, *, kind: str = "manuscript"
) -> Manuscript:
    """Parse ``path``, or serve it from the on-disk cache.

    The result is cached keyed by file content *and* the ingest config (see
    :mod:`.cache`). Wipe the cache with ``just cache-clear`` if you need to
    force a re-parse. ``kind`` is threaded to the converter's plausibility
    floor only — it never changes the text, so it is not part of the key.
    """
    config = config or {}
    from . import cache as _cache

    key = _cache.cache_key(path, config)
    cached = _cache.get(key, config)
    if cached is not None:
        return cached

    parsed = _load_uncached(path, config, kind=kind)
    try:
        _cache.put(key, parsed, source_path=path, config=config)
    except (OSError, ValueError):
        # Cache write failure is non-fatal: the run still has the parsed
        # text in memory, so we just skip persisting it. ValueError covers
        # encoding failures on text this loader could not sanitise — losing
        # the cache entry is the right cost, losing the review is not.
        pass
    return parsed


def _load_uncached(
    path: str, config: dict | None = None, *, kind: str = "manuscript"
) -> Manuscript:
    ext = os.path.splitext(path)[1].lower()
    title = ""
    converted = None
    if ext == ".pdf":
        converted, record = _read_pdf(path, ingest_config(config), kind)
        text, title = converted.markdown, converted.title
    elif ext in (".md", ".markdown", ".txt", ".tex"):
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
        record = _plain_ingest(f"read as {ext.lstrip('.')}", text)
        # A submitted Markdown file already carries its own structure, so it
        # is "markdown" for the same reason a converted PDF is: quotations
        # and section boundaries can be trusted.
        if ext in (".md", ".markdown"):
            record["format"] = "markdown"
    else:
        raise ValueError(f"Unsupported manuscript type: {ext}")

    text = _normalize(text)
    record["chars"] = len(text)
    # Fingerprint of the text the panel reads, which is not the same question
    # as a fingerprint of the file. Measured: three downloads of one bioRxiv
    # PDF over ten hours gave three different file checksums at an identical
    # 1,689,095 bytes — the server stamps something fixed-width into the
    # container — while the converted text came back byte-identical all three
    # times at 86,988 characters.
    #
    # So a caller asking "is this the draft we reviewed before?" cannot use
    # the file hash: it answers no for every bioRxiv paper. This is the hash
    # of what was actually reviewed.
    record["text_sha256"] = hashlib.sha256(text.encode("utf-8")).hexdigest()
    if not title:
        title = _guess_title(text, fallback=os.path.basename(path))
    # The converter's own section tree where there is one, and the heading
    # heuristic where there is not — a Markdown, LaTeX or plain-text
    # submission has no document model behind it, and neither does a
    # rustypaper old enough to predate the tree.
    if converted is not None and converted.outline:
        sections = _sections_from_outline(
            text, converted.outline, references_anchor=converted.references_anchor
        )
    else:
        sections = _split_sections(
            text, references_anchor=converted.references_anchor if converted else ""
        )
    # None, not [], for a document with no block model behind it: a PDF whose
    # bibliography typed as zero entries and a Markdown file nobody could have
    # typed one from are different answers, and prose.analyze reports the
    # second as unknown rather than as none.
    references = list(converted.references) if converted else None
    # Measured here rather than by a caller so that every path into a review —
    # graph, cache hit, web job — reports the same numbers, and so that a
    # conversion bad enough to be worth stopping over is known before any
    # agent has been paid to read it.
    record["prose"] = prose.analyze(
        text, sections=sections, caveman=record.get("caveman"), references=references
    ).to_dict()
    return Manuscript(
        title=title,
        text=text,
        sections=sections,
        references=references or [],
        ingest=record,
    )


def _read_pdf(
    path: str, ingest: dict, kind: str = "manuscript"
) -> tuple[structured.Converted, dict]:
    """Convert a PDF to Markdown.

    Returns ``(converted, record)`` — the converter's whole result, because
    the loader reads more of it than the text: the title (its own, which beats
    the loader's line-order heuristic), the section tree, and the typed
    bibliography.

    Raises when the converter is unavailable, rather than reading the PDF a
    worse way. See this module's docstring for why there is no second path.
    """
    caveman = ingest["caveman"]
    try:
        converted = structured.convert(path, caveman, kind=kind)
    except structured.ConverterMissing as exc:
        # Only a missing converter earns the install line. It used to close
        # every Unavailable — including "produced only N characters… scanned
        # or image-only" — so a user who had rustypaper and submitted a scan
        # was told to install a package already present instead of being told
        # their file has no text layer. The two ask for completely different
        # things next (see structured.Unavailable).
        raise RuntimeError(
            f"Could not read {os.path.basename(path)}: {exc}\n"
            "PDF ingest requires rustypaper:\n"
            "    pip install rustypaper\n"
            "or, from a checkout, pip install -e /path/to/rustypaper/python"
        ) from exc
    except structured.Unavailable as exc:
        raise RuntimeError(
            f"Could not read {os.path.basename(path)}: {exc}"
        ) from exc

    emit(AgentEvent(
        kind="log", node="ingest",
        text=f"read {os.path.basename(path)} as markdown via {converted.tool}"
             + (f" (caveman {caveman})" if caveman != "off" else ""),
    ))
    return converted, {
        "format": "markdown",
        "tool": converted.tool,
        "caveman": None if caveman == "off" else caveman,
        "chars": len(converted.markdown),
        # Provenance for the section map: which of the two ways it was built.
        # A reader comparing two rounds of the same paper needs to know
        # whether a section appearing or vanishing is the manuscript changing
        # or the converter having learned to read its headings.
        "section_source": "document" if converted.outline else "headings",
    }


def _normalize(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\n{3,}", "\n\n", text)
    return _drop_surrogates(text.strip())


def _drop_surrogates(text: str) -> str:
    """Replace lone surrogates, which are not encodable as UTF-8.

    Python holds an unpaired surrogate happily and then refuses to encode it,
    so one aborts the run at the first thing that serialises the text — in
    practice the cache write, several seconds into a review, with a
    ``UnicodeEncodeError`` naming a codec rather than a manuscript. Every
    provider request would have hit the same wall a moment later.

    The PDF path can no longer produce them: Rust strings are UTF-8 by
    construction, and pypdf — which emitted them from a broken encoding map —
    is no longer in it. DOCX still can, and one substitution here is cheaper
    than reasoning about which reader is safe.
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


def _match_section_key(raw: str) -> tuple[str | None, bool]:
    """The section key ``raw`` names, and whether it carried a section number.

    ``raw`` is a heading with its Markdown hashes and trailing colon already
    stripped. Returns the matched key *before* aliasing, because the heuristic
    path's guards below are phrased over the match itself, and ``None`` when
    the text names no known section.

    Shared by both ways of building the map — the converter's outline and the
    heading heuristic — so a heading spelled "Materials & Methods" lands in
    the same bucket whichever path read it.
    """
    prefix = _NUMERIC_PREFIX_RE.match(raw)
    candidate = (raw[prefix.end():] if prefix else raw).strip().lower()
    matched = next(
        (k for k in _SECTION_KEYS if candidate == k or candidate.startswith(k + " ")),
        None,
    )
    return matched, bool(prefix)


def _heading_text(line: str) -> str | None:
    """The title on a Markdown heading line, or ``None`` if it is not one.

    The converter's Markdown emitter escapes a heading that opens with a
    character Markdown would otherwise read as syntax, so a single leading
    backslash is removed before the text is compared to the outline's.
    """
    stripped = line.strip()
    if not stripped.startswith("#"):
        return None
    title = stripped.lstrip("#").strip()
    if title.startswith("\\") and len(title) > 1:
        title = title[1:]
    return title.rstrip(":").strip()


def _outline_opens(lines: list[str], outline) -> dict[int, tuple[str, bool]]:
    """Where each section of the converter's outline starts, by line.

    The outline and the Markdown are indexed differently and have to be joined
    somewhere: the outline is a tree over *blocks*, while everything
    downstream of the loader — budget fitting, the revision diff, the
    per-section statistics — slices the *Markdown*. They are joined on the
    heading text itself, which is the one thing both hold: the emitter renders
    a heading block as a ``#``-prefixed line carrying exactly the title the
    tree reports, so walking the outline in reading order against the
    Markdown's heading lines in document order pairs each section with the
    line it starts on. Nothing is re-derived and nothing is re-rendered, so
    each section's text stays a literal slice of the text the panel reads.

    A title with no heading line to pair it with is skipped rather than
    searched for out of order: its section then reads as part of the section
    above it, which is a smaller lie than pairing it with some other section's
    heading further down.

    Subsections open a bucket too. A "Data availability" that a paper prints
    under Methods is that section wherever it sits, and the numbering the
    outline reports is not a reason to lose it.
    """
    headings = [(i, _heading_text(line)) for i, line in enumerate(lines)]
    headings = [(i, title) for i, title in headings if title]

    opens: dict[int, tuple[str, bool]] = {}
    at = 0
    for section in outline:
        title = section.title.strip().rstrip(":").strip()
        found = next(
            (
                n for n in range(at, len(headings))
                if headings[n][1].casefold() == title.casefold()
            ),
            None,
        )
        if found is None:
            continue
        at = found + 1
        matched, _numbered = _match_section_key(title)
        if matched:
            # False: a heading line is the section's name, not its text, and
            # is dropped as it is in the heuristic path — the revision diff
            # compares section bodies between rounds.
            opens[headings[found][0]] = (_SECTION_ALIASES.get(matched, matched), False)
    return opens


def _sections_from_outline(text: str, outline, references_anchor: str = "") -> dict[str, str]:
    """Bucket ``text`` using the converter's own section tree, then the prose.

    The outline is authoritative where it speaks. Where it says nothing, the
    heading heuristic still runs, and this is not belt-and-braces: measured
    over the sixteen-paper corpus, the outline alone lost the bibliography on
    four papers and every section on one — a two-column IEEE paper whose
    headings the converter reads as body text. On those the "References" line
    is still sitting there in the Markdown, and the pipeline used to find it.
    A structural improvement that quietly drops a section a heading match had
    been finding is a regression however much better its provenance is.

    So the heuristic fills gaps only: it may open a bucket the outline never
    named, and never a second one for a bucket the outline already placed.
    That is also what suppresses its false positives on the papers where the
    outline works — a body line reading "Discussion of these results…" cannot
    open a discussion the outline has already located.

    The result is the same shape as :func:`_split_sections` — canonical bucket
    names, ``_preamble`` for everything ahead of the first one — because the
    map is a contract, and this is a better way of building it rather than a
    different thing. Sections the document has and the bucket list does not
    ("4 Why Self-Attention") stay with the bucket above them, exactly as they
    do when the heading is matched out of the prose.
    """
    lines = text.splitlines()
    opens = _outline_opens(lines, outline)
    placed = {bucket for bucket, _keep in opens.values()}
    for i, (bucket, keep) in _heading_opens(lines, references_anchor.strip()).items():
        if bucket not in placed and i not in opens:
            opens[i] = (bucket, keep)
    return _bucket_lines(lines, opens)


def _bucket_lines(lines: list[str], opens: dict[int, tuple[str, bool]]) -> dict[str, str]:
    """Cut ``lines`` into sections at ``opens``.

    ``opens`` maps a line index to the bucket it starts and whether the line
    itself belongs to that bucket's text: a bare heading does not, a heading
    with the section's first sentence fused onto it does.
    """
    sections: dict[str, list[str]] = {"_preamble": []}
    current = "_preamble"
    for i, line in enumerate(lines):
        opened = opens.get(i)
        if opened is not None:
            current, keep = opened
            sections.setdefault(current, [])
            if not keep:
                continue
        sections.setdefault(current, []).append(line)
    return {k: "\n".join(v).strip() for k, v in sections.items() if "".join(v).strip()}


def _split_sections(text: str, references_anchor: str = "") -> dict[str, str]:
    """Best-effort bucketing of text into known sections by heading match.

    The whole map for a Markdown, LaTeX or plain-text submission — those have
    no document model behind them — and for a converter too old to report a
    section tree. Where there is one, :func:`_sections_from_outline` reads the
    sections the converter typed and calls this only for the ones it did not.

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
    lines = text.splitlines()
    return _bucket_lines(lines, _heading_opens(lines, references_anchor.strip()))


def _heading_opens(lines: list[str], anchor: str = "") -> dict[int, tuple[str, bool]]:
    """Where each section starts, guessed from lines that read like headings.

    The heuristic behind :func:`_split_sections`, factored out so that the
    outline path can use it to fill what the converter did not name — see
    :func:`_sections_from_outline`.
    """
    opens: dict[int, tuple[str, bool]] = {}
    found_anchor = False
    in_fence = False
    for i, line in enumerate(lines):
        # Fenced code is opaque to the heading match. A `# results` line
        # inside a fence is code, and treating it as a heading both invented
        # a section that was never there and deleted the line from the text.
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        # Checked before the heading match: the anchor's whole purpose is to
        # work on a line no heading rule will fire on. The line is kept —
        # unlike a heading, it is a reference in its own right.
        if anchor and not found_anchor and anchor in line:
            found_anchor = True
            opens[i] = ("references", True)
            continue
        raw = line.strip().lstrip("#").strip().rstrip(":").strip()
        # Leading numeric / roman-numeral prefixes ("1.", "2.1", "II.") are
        # stripped before the match, against the original case — see
        # _NUMERIC_PREFIX_RE.
        matched, numbered = _match_section_key(raw)
        # A numbered line that ends the way a sentence does is a Markdown
        # list item, not a heading: "2. Results were consistent with prior
        # work." used to open a phantom results section and then vanish from
        # the text entirely, because a short heading line is dropped.
        if matched and numbered and raw.endswith((".", "!", "?")):
            matched = None
        # The length guard keeps a body sentence opening "Discussion of these
        # results…" from starting a section. A *numbered* line needs no such
        # protection — prose does not begin "IV. " — and waiving it for those
        # recovers headings a converter fused into the paragraph below them,
        # which is how `I. Introduction LECTROMAGNETIC (EM) metasurfaces…`
        # arrives. That line is a heading with its section's first sentence
        # stuck to it, and treating it as one puts the section back.
        if matched and (numbered or len(line.strip()) < 80):
            # Aliases ("bibliography", "conclusions", "materials and
            # methods", …) collapse to one bucket name, so the literature
            # reviewer and the revision diff find the section whatever the
            # authors called it this round.
            bucket = _SECTION_ALIASES.get(matched, matched)
            # A short line is a heading and nothing else, so it is dropped. A
            # long one carries the section's opening sentence, so it is kept
            # whole — the few words of heading left at its front cost far
            # less than the paragraph would.
            opens[i] = (bucket, len(line.strip()) >= 80)
    return opens
