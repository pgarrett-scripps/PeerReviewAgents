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
the literature reviewer looking for a reference list — is built by matching
heading text. Given pypdf's output that matching is a guess; given Markdown
it is a read.

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
class Converted:
    """A converted manuscript, and what the converter knew about it."""

    markdown: str
    # The converter's own typed title block, not a guess from line order.
    # Empty when it identified none.
    title: str
    # Names the version that produced the text, for provenance.
    tool: str
    # The opening of the first bibliography entry, or "". The section map is
    # built by matching heading text, and a "References" heading is the one
    # that most often fails to survive conversion — see :func:`convert`. The
    # block model types bibliography entries regardless, so this hands the
    # loader an anchor that does not depend on a heading existing.
    references_anchor: str = ""


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
        markdown = rustypaper.to_markdown(path, caveman)
    except Exception as exc:  # noqa: BLE001 - scanned, malformed, or unreadable
        raise Unavailable(
            f"rustypaper could not convert the file ({exc.__class__.__name__}: {exc})"
        ) from exc

    if len(markdown) < MIN_PLAUSIBLE_BY_KIND.get(kind, MIN_PLAUSIBLE_CHARS):
        raise Unavailable(
            f"rustypaper produced only {len(markdown)} characters, which is not a "
            f"readable {kind} — the PDF is most likely scanned or image-only"
        )

    # A second conversion, for what only the typed block model knows: the
    # title, and where the bibliography starts. It costs about 60ms against a
    # review that costs minutes and dollars, and it replaces two guesses with
    # two reads. A failure here is not worth losing a good conversion over —
    # the loader's heuristics still run.
    title, anchor = "", ""
    try:
        doc = rustypaper.to_document(path, caveman)
        title = (doc.get("title") or "").strip()
        anchor = _first_reference(doc)
    except Exception:  # noqa: BLE001 - both are refinements, not requirements
        pass

    return Converted(
        markdown=markdown,
        title=title,
        tool=f"rustypaper {version}",
        references_anchor=anchor,
    )


def _first_reference(doc: dict) -> str:
    """The opening of the first bibliography entry, or "".

    Worth doing because the "References" heading is the least reliable one in
    the document: it is short, often set no larger than body text, and sits
    immediately above a dense block. On the BERT paper the converter fuses it
    into the entry below it — the Markdown reads ``ReferencesKevin Clark,
    …`` — so a section map built from headings alone loses the bibliography
    entirely. Typed reference blocks survive that.
    """
    for block in doc.get("blocks") or []:
        if (block.get("kind") or {}).get("type") != "reference":
            continue
        text = (block.get("text") or "").strip()
        return text[:_ANCHOR_CHARS] if text else ""
    return ""
