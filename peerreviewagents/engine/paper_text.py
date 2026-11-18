"""One normal form for manuscript text, and offsets back to what was printed.

Everything that matches a reviewer's words against the manuscript goes through
here. A quote is retyped, pasted out of a PDF viewer, or copied from a model's
own earlier output, and on the way it loses the things a reader cannot see: the
non-breaking space becomes a space, the en dash becomes a hyphen, the ligature
``ﬁ`` becomes two letters, the curly apostrophe straightens, the soft hyphen
the typesetter put inside a word disappears. None of that changes the sentence,
so none of it may decide whether the sentence can be cited.

:func:`normalise` therefore returns both the normal form *and*, for every
character of it, the index in the original string it came from. The offsets are
what makes this usable for citing rather than only for comparing: a match found
in the normal form is reported back in the coordinates of the file the author
and the consumer both read, so a highlight lands on the printed characters.

Nothing here folds case. A quote is still a quote: the engine is checking that
the manuscript says this, not that it says something like it.
"""
from __future__ import annotations

import re
import unicodedata

from .storage import digest

#: The markup a reference link is written with in ``paper.md``: the printed
#: marker in brackets, then the entry it ties to. The file keeps it, because it
#: is the one record of which marker cites which reference, and a reader of the
#: Markdown follows it. It is not what the page shows, so the normal form keeps
#: the digits and drops the rest of it: a reviewer quoting the sentence off the
#: page and the ledger quoting it off the markup are quoting the same sentence.
REFERENCE_LINK = re.compile(r'\[(\d{1,3})\]\(#ref-([^)\s]{1,40})\)')

#: Every dash, minus and horizontal bar that a manuscript prints where a reader
#: sees "-". NFKC leaves these alone — they are distinct characters, not
#: compatibility forms — so a minus sign in a printed interval and the hyphen a
#: reviewer types for it would otherwise be two different quotes.
DASHES = {
    '‐': '-',  # hyphen
    '‑': '-',  # non-breaking hyphen
    '‒': '-',  # figure dash
    '–': '-',  # en dash
    '—': '-',  # em dash
    '―': '-',  # horizontal bar
    '−': '-',  # minus sign
    '⁃': '-',  # hyphen bullet
    '－': '-',  # fullwidth hyphen-minus (NFKC already does this one)
}

#: The quotation marks and apostrophes a typesetter curls, flattened back.
QUOTES = {
    '‘': "'", '’': "'", '‚': "'", '‛': "'",
    '′': "'", 'ʼ': "'", '´': "'", '`': "'",
    '“': '"', '”': '"', '„': '"', '‟': '"', '″': '"',
}

#: Characters that carry no text at all: the soft hyphen a line break left
#: behind, the zero-width joiners and marks, the byte-order mark. A word broken
#: across two printed lines holds one of these in the middle, and a reviewer
#: quoting that word types it whole.
DROPPED = frozenset('­​‌‍‎‏⁠﻿')


#: The Unicode categories of a combining mark: a character that is drawn on the
#: one before it and is part of it, not a character of its own.
MARKS = ('Mn', 'Mc', 'Me')


def is_mark(character) -> bool:
    """Whether a character belongs to the character printed before it.

    A span that ends between a letter and its accent is not a span of the text,
    so anything measuring a span in the original string has to ask this.
    """
    return bool(character) and unicodedata.category(character) in MARKS


def _clusters(text):
    """``text`` split so that every combining mark stays with the character it marks.

    NFKC over a whole string composes ``e`` + U+0301 into ``é``, which is the
    point — a reviewer who types the decomposed form must match a manuscript
    that prints the composed one. But normalising character by character would
    not do that, and normalising the whole string at once leaves no way back to
    the original offsets. Clustering is the middle: each base character and the
    marks that follow it are normalised together, and every character the
    cluster produces is reported at the cluster's own offset.
    """
    start = 0
    for index, character in enumerate(text):
        if index > start and not is_mark(character):
            yield start, text[start:index]
            start = index
    if start < len(text):
        yield start, text[start:]


def _link_markup(text) -> frozenset:
    """The indices of ``text`` that are a reference link's markup and not its marker."""
    skipped: set = set()
    for match in REFERENCE_LINK.finditer(text):
        skipped.add(match.start())
        skipped.update(range(match.end(1), match.end()))
    return frozenset(skipped)


def normalise(text):
    """``(normal_form, offsets)``, where ``offsets[i]`` indexes ``text``.

    Whitespace runs — including the newlines that separate the rows of a table
    or the lines of a quotation — collapse to one space, and the ends are
    trimmed. The offsets of a collapsed run point at its first character, so a
    quote that spans a line break still highlights from the right place.

    A reference link keeps the marker it prints and loses its markup, so the
    sentence reads as the page reads it either way round.
    """
    text = str(text or '')
    characters: list[str] = []
    offsets: list[int] = []
    pending_space = -1
    markup = _link_markup(text)
    for index, cluster in _clusters(text):
        if index in markup:
            continue
        for character in unicodedata.normalize('NFKC', cluster):
            character = DASHES.get(character, QUOTES.get(character, character))
            if character in DROPPED:
                continue
            if character.isspace():
                # Remembered, not written: a run of whitespace becomes one
                # space only if text follows it, which is how the trailing end
                # gets trimmed without a second pass.
                if pending_space < 0:
                    pending_space = index
                continue
            if pending_space >= 0:
                if characters:
                    characters.append(' ')
                    offsets.append(pending_space)
                pending_space = -1
            characters.append(character)
            offsets.append(index)
    return ''.join(characters), tuple(offsets)


def normalised(text) -> str:
    """The normal form alone, for comparing and for hashing."""
    return normalise(text)[0]


def fingerprint(text) -> str:
    """The identity of a passage across versions: a digest of its normal form.

    Line numbers move when a manuscript is re-imported or reflowed, so a
    passage is matched by its text rather than by where it sat. Hashing the
    normal form rather than the printed characters means a changed dash or a
    respaced sentence is still the same passage, which is what a reader would
    say too.
    """
    return digest(normalised(text))


def words(text) -> list:
    """The normal form's words, lowered: the unit the nearest-line search uses."""
    return normalised(text).casefold().split()


__all__ = ['DASHES', 'QUOTES', 'DROPPED', 'MARKS', 'REFERENCE_LINK', 'is_mark', 'normalise',
           'normalised', 'fingerprint', 'words']
