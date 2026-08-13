"""Structure-aware PDF ingest, via the rustypaper converter.

pypdf reads a PDF's text layer in content-stream order and returns a flat
string. That is adequate on a single-column preprint and poor on anything
else: on a real submission it fused 2% of all words into runs like
``comparableefficacyatlowerdoseusingonlycausallyavailableinformation``, lost
about a sixth of the content outright, and flattened every heading, table and
equation into undifferentiated prose. rustypaper reads the same file with 3
fused tokens instead of 235 and emits Markdown — headings as headings, tables
as tables, display equations as LaTeX.

The structure is not cosmetic. Everything downstream that reasons about
*where* something is in a manuscript — the section map, the revision diff,
the literature reviewer looking for a reference list — needs to know where
the sections are. Given pypdf's output that is a guess; given Markdown it is
a match on heading text; given the converter's own document model it is a
read.

So this returns three things beyond the Markdown, each of which the pipeline
used to re-derive from the prose downstream:

* the **title**, as the converter typed it;
* the **outline** — the document's section tree, in reading order, which the
  loader turns into its section map instead of guessing which lines are
  headings (:func:`.loader._sections_from_outline`);
* the **references**, as typed entries with parsed fields, which the citation
  auditor and the literature reviewer read instead of regexing the prose.

All three come from the same single conversion as the Markdown on a converter
that offers ``rustypaper.convert``, and are absent — not wrong — on one that
does not report them at all.

**Required, and the only converter.** Every failure here raises
:class:`Unavailable` with a reason, and the loader turns that into an error
rather than reading the PDF a worse way — see its module docstring for why
there is no second path.

It is a compiled extension shipped as a per-platform wheel::

    pip install rustypaper
"""

from __future__ import annotations

from dataclasses import dataclass

# A conversion that returns almost nothing is a failure wearing a success's
# clothes: an empty manuscript would run a full panel over nothing and
# publish whatever it invented. Real papers clear this by an order of
# magnitude — the shortest in the review corpus is about 40 000 characters —
# so anything under this is an image-only PDF whose text layer is a handful
# of stray glyphs. OCR is out of scope, so the run stops here.
MIN_PLAUSIBLE_CHARS = 4000

# The 4000 above is calibrated for manuscripts, and the loader reads more
# than manuscripts: applied to everything, it refused a normal one-page
# author response letter (~2,500 characters) as "scanned or image-only" and
# killed the revision run. A scanned page still yields only stray glyphs, so
# a floor far below one page keeps the scan check for documents that are
# legitimately short — a letter, or a single-table supplement.
MIN_PLAUSIBLE_BY_KIND = {
    "manuscript": MIN_PLAUSIBLE_CHARS,
    "letter": 200,
    "supplement": 200,
}

# What the converter will accept. `off` is the default everywhere; see
# `caveman` in default_config.py for why.
CAVEMAN_LEVELS = ("off", "light", "hard")


# How much of the first bibliography entry to hand back as an anchor. Long
# enough to be unique in a paper, short enough to survive the line wrapping
# the Markdown emitter may apply.
_ANCHOR_CHARS = 48


class Unavailable(Exception):
    """The structured backend could not produce a manuscript from this file.

    Carries the reason as its message, because the reason is what the caller
    shows a human: "rustypaper is not installed" and "this PDF is a scan" both
    stop the run, and they ask for completely different things next.
    """


class ConverterMissing(Unavailable):
    """rustypaper itself could not be imported.

    A distinct type because only this case is fixed by installing the
    package. The loader used to append the install line to every
    :class:`Unavailable`, which told a user who already had rustypaper to
    install it when what they actually had was a scanned PDF.
    """


def converter_version() -> str:
    """Version string of the installed converter, or a stable placeholder.

    Hashed into the manuscript cache key (see :mod:`.cache`): an upgraded
    converter reads the same bytes into different text, so the version is
    part of what a cached entry *is*, not just provenance inside it.
    """
    try:
        import rustypaper  # noqa: PLC0415 - optional, imported at point of use
    except Exception:  # noqa: BLE001 - a broken wheel and a missing one hash the same
        return "unavailable"
    return str(getattr(rustypaper, "__version__", "unknown"))


@dataclass(frozen=True)
class Section:
    """One heading from the converter's own section tree.

    ``title`` is the heading as printed, numbering and all — the same string
    the Markdown emitter puts on the ``#``-prefixed line, which is how the
    loader joins a block-indexed section map to the Markdown everything
    downstream slices (see :func:`.loader._sections_from_outline`).
    """

    title: str
    # 1 for a top-level section, 2 for a subsection, and so on.
    level: int


@dataclass(frozen=True)
class Converted:
    """A converted manuscript, and what the converter knew about it."""

    markdown: str
    # The converter's own typed title block, not a guess from line order.
    # Empty when it identified none.
    title: str
    # Names the version that produced the text, for provenance.
    tool: str
    # The opening of the first bibliography entry, or "". Still here after the
    # section tree arrived, because the tree names no bibliography on four of
    # the sixteen corpus papers — see :func:`_first_reference`.
    references_anchor: str = ""
    # The section tree flattened to reading order, empty when the converter
    # reports none. The loader builds its section map from this when it is
    # there and falls back to matching heading text out of the Markdown when
    # it is not.
    outline: tuple[Section, ...] = ()
    # Bibliography entries as the converter typed them, in citation order.
    # Each is a dict carrying whatever it could parse — ``raw`` always, and
    # ``label``, ``authors``, ``title``, ``year``, ``doi``, ``arxiv`` where
    # they were extractable. A field it could not read confidently is absent
    # rather than empty, so a consumer reading ``entry.get("doi")`` gets
    # "not known" and never a wrong string.
    references: tuple[dict, ...] = ()


def convert(path: str, caveman: str = "off", kind: str = "manuscript") -> Converted:
    """Convert ``path`` to Markdown.

    ``kind`` names what the caller believes the document is — a key of
    :data:`MIN_PLAUSIBLE_BY_KIND` — and sets the too-short-to-be-real floor
    accordingly; an unknown kind gets the manuscript floor.

    Raises :class:`Unavailable` for every failure mode: no extension, an
    unreadable or image-only PDF, or output too short to be a ``kind`` —
    and :class:`ConverterMissing` specifically when rustypaper is absent.
    """
    if caveman not in CAVEMAN_LEVELS:
        # Not an Unavailable: a bad config value is the operator's mistake and
        # should not silently degrade to a different ingest path.
        raise ValueError(
            f"unknown caveman level {caveman!r}; expected one of "
            + ", ".join(repr(level) for level in CAVEMAN_LEVELS)
        )

    try:
        import rustypaper  # noqa: PLC0415 - optional, imported at point of use
    except Exception as exc:  # noqa: BLE001 - a broken wheel and a missing one are the same outcome
        raise ConverterMissing(
            f"rustypaper unavailable ({exc.__class__.__name__}: {exc})"
        ) from exc

    version = getattr(rustypaper, "__version__", "unknown")
    try:
        markdown, doc = _read(rustypaper, path, caveman)
    except Exception as exc:  # noqa: BLE001 - scanned, malformed, or unreadable
        raise Unavailable(
            f"rustypaper could not convert the file ({exc.__class__.__name__}: {exc})"
        ) from exc

    if len(markdown) < MIN_PLAUSIBLE_BY_KIND.get(kind, MIN_PLAUSIBLE_CHARS):
        raise Unavailable(
            f"rustypaper produced only {len(markdown)} characters, which is not a "
            f"readable {kind} — the PDF is most likely scanned or image-only"
        )

    # What only the typed block model knows: the title, the section tree, and
    # the bibliography as entries rather than as prose. On a converter without
    # `convert` this is a second conversion, which costs about 60ms against a
    # review that costs minutes and dollars. A failure here is not worth
    # losing a good conversion over — the loader's heuristics still run.
    title, outline, references, anchor = "", (), (), ""
    try:
        if doc is None:
            doc = rustypaper.to_document(path, caveman)
        title = (doc.get("title") or "").strip()
        outline = _outline(doc)
        references = _references(doc)
        anchor = _first_reference(doc)
    except Exception:  # noqa: BLE001 - all of these are refinements, not requirements
        pass

    return Converted(
        markdown=markdown,
        title=title,
        tool=f"rustypaper {version}",
        references_anchor=anchor,
        outline=outline,
        references=references,
    )


def _read(rustypaper, path: str, caveman: str) -> tuple[str, dict | None]:
    """The Markdown, and the document model when one run yields both.

    ``rustypaper.convert`` runs the pipeline once and returns both renderings
    of the same result. Asking for them separately — ``to_markdown`` then
    ``to_document``, which is what this did — reads, lays out and classifies
    the PDF twice for two views of one conversion.

    The two-call path is kept, and reached by feature detection rather than by
    a version comparison, because the converter is a released package: an
    installed 0.1.x has no ``convert`` and PRA has to keep running against it.
    """
    if hasattr(rustypaper, "convert"):
        result = rustypaper.convert(path, caveman)
        return result.markdown, result.document
    return rustypaper.to_markdown(path, caveman), None


def _outline(doc: dict) -> tuple[Section, ...]:
    """The converter's section tree, flattened to reading order.

    Preorder, because that is the order the headings appear in the Markdown: a
    parent's heading is emitted before its children's, so the loader can walk
    the outline and the Markdown's heading lines in step.

    The front matter — title, authors, abstract on templates that print no
    "Abstract" heading — is reported as a section with no title, because no
    heading introduces it. It is skipped here for the same reason: there is no
    line in the Markdown to pair it with, and everything before the first
    heading is the loader's preamble already.
    """
    flat: list[Section] = []

    def walk(nodes) -> None:
        for node in nodes or []:
            title = (node.get("title") or "").strip()
            if title:
                flat.append(Section(title=title, level=int(node.get("level") or 1)))
            walk(node.get("children"))

    walk(doc.get("sections"))
    return tuple(flat)


# Fields the converter parses out of a bibliography entry, in the order they
# read best. `raw` is the entry as printed with its label removed, and is the
# only one always present.
_REFERENCE_FIELDS = ("label", "authors", "title", "year", "doi", "arxiv")


def _references(doc: dict) -> tuple[dict, ...]:
    """Typed bibliography entries, in the order the document prints them.

    Only the fields the converter actually parsed are carried through: an
    entry whose authors it could not read has no ``authors`` key rather than
    an empty list, which is the difference between "this entry names nobody"
    and "we could not tell". A converter old enough to type reference blocks
    without parsing their fields yields entries carrying ``raw`` alone, which
    is still the bibliography as a list instead of as a wall of prose.
    """
    entries: list[dict] = []
    for block in doc.get("blocks") or []:
        if (block.get("kind") or {}).get("type") != "reference":
            continue
        parsed = block.get("reference") or {}
        entry = {k: parsed[k] for k in _REFERENCE_FIELDS if parsed.get(k)}
        entry["raw"] = str(parsed.get("raw") or block.get("text") or "").strip()
        if entry["raw"]:
            entries.append(entry)
    return tuple(entries)


def _first_reference(doc: dict) -> str:
    """The opening of the first bibliography entry, or "".

    The "References" heading is the least reliable one in the document: it is
    short, often set no larger than body text, and sits immediately above a
    dense block. rustypaper 0.1.x fuses it into the entry below it on the BERT
    paper — the Markdown reads ``ReferencesKevin Clark, …`` — so a section map
    built from headings alone loses the bibliography entirely. Typed reference
    blocks survive that.

    A section tree does not retire this. Measured over the sixteen-paper
    rustypaper corpus, four papers' trees name no bibliography section at all
    (the heading is set at body size and reads as body text), so the anchor is
    still what puts their reference list in the map.

    Taken from the block's own text rather than from the parsed entry: the
    anchor has to match the Markdown line, and on exactly the papers this
    exists for that line carries the fused heading with it.
    """
    for block in doc.get("blocks") or []:
        if (block.get("kind") or {}).get("type") != "reference":
            continue
        text = (block.get("text") or "").strip()
        return text[:_ANCHOR_CHARS] if text else ""
    return ""
