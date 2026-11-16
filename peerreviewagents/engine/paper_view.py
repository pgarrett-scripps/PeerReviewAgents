"""The manuscript as a reviewer is served it: numbered lines, in as few parts as fit.

Reviewers have no file access, so whatever they read, they read through a tool
result. Measured over seven saved runs, core reviewers had seen between half and
almost all of the main text and the editor as little as fifteen per cent, while
spending three to twenty of their thirty calls — they were not stopped by the
allowance, they stopped reading. Twelve blocks of JSON at a time is what they
stopped reading, so this serves the file itself: the lines of paper.md with
their numbers, as text, in one part where one part holds it.

The part size is measured against the harness rather than guessed. In Claude
Code 2.1.325 a tool result is capped at ``MAX_MCP_OUTPUT_TOKENS`` tokens,
defaulting to 25,000; the estimate it screens with is the result's length over
four; and a result whose estimate is at or under half the cap is never counted
again or truncated. Half of 25,000 tokens is 50,000 characters, so a part is
held under that and no part is ever cut — a truncated manuscript being the one
failure this must not have, because the lines after the cut are the ones nobody
reviews.
"""
from __future__ import annotations

from . import paper_base

#: The characters of served text one part may hold. See the module docstring:
#: under half of Claude Code's default per-result token cap, in characters.
PART_CHARACTERS = 48000

#: What a line number is printed in. Wide enough for a long manuscript, and
#: ending in a character no sentence of the paper ends with.
GUTTER = '{:>5}| '

#: On every equation line the converter read and nobody checked, because it
#: mangles sums and subscripts and a reviewer who trusts the text files a
#: finding about algebra the authors never wrote. Markers are served, never
#: written into paper.md: the file stays the manuscript.
EQUATION_MARKER = '   [machine-read equation, may be garbled'

#: On an equation or a table a reader rewrote from the page image. It says who
#: read it and off which page, so a reviewer who doubts the transcription can
#: open the same page and compare, and knows the line is not the converter's.
READ_MARKER = '   [read from the page image'

#: On a piece a model wrote where no page was read at all — an application's
#: own source, composed through
#: :func:`~peerreviewagents.engine.ingest.write_manuscript` with ``ai`` pieces
#: in it. The reviewer needs to know the line is a model's and whose, and the
#: page-image wording would claim a read that never happened.
WRITTEN_MARKER = '   [written'

#: On a figure's description. It is not the authors' words and no reviewer
#: should quote it as the paper's own: it is a model's account of an image.
DESCRIPTION_MARKER = '   [AI description of the figure image'

HEADER = """Read every line of this before anything else. A finding quotes this text, and the
line numbers in the left column are what a citation records; quote the text, not
the numbers.

Displayed equations and inline $...$ maths were machine-read from the PDF and are
sometimes garbled — a sum, a subscript, or the word after an inline formula. Every
equation line below carries a marker saying so. Open the page with view_page and
read the equation there before you write anything about an author's mathematics."""

AI_READ_HEADER = """Read every line of this before anything else. A finding quotes this text, and the
line numbers in the left column are what a citation records; quote the text, not
the numbers.

Before you were called, a model read every page of the PDF as an image and went
through the converted text piece by piece. The markers on the lines below say what
it did to each one:

- no marker: the converter's text, as it came out of the PDF.
- read from the page image: this line was written from the page rather than
  from the text layer — an equation or a table the converter garbled, or, on a
  page the converter returned nothing at all from, the paragraphs and captions
  that page prints. Where the converter had a reading of its own it is kept in
  the map, so a transcription can be disputed; the page is named in the marker.
- AI description of the figure image: not the authors' words. A model looked at
  the figure and wrote what it shows. Never quote it as something the paper says,
  and check it against the image before you build a finding on it.

Inline $...$ maths inside a paragraph is still the converter's and was not
rechecked. Open the page with view_page and read an equation there before you
write anything about an author's mathematics."""

AI_PIECES_HEADER = HEADER + """

No stage of this review read the pages: this manuscript was written from the
application's own source. Some of its pieces were written by a model before it
got here, and each one carries a "written by" marker naming the model. A line
with that marker is not the authors' text: never quote one as something the
paper says, and nothing here was checked against a page image, so there is no
page to open and compare it with."""


def reader_model(base):
    """The model that read this manuscript's pages, or ``''`` when none did."""
    read = base.manifest.get('ai_read') or {}
    return str(read.get('reader_model') or '') if read.get('read') else ''


def piece_model(base):
    """The model that wrote this manuscript's non-converter pieces, or ``''``.

    The Read stage's model where one ran, and otherwise the model the caller of
    :func:`~peerreviewagents.engine.ingest.write_manuscript` named for the ``ai``
    pieces it composed: a marker says who wrote the line, and an application's
    own pieces have an author too.
    """
    read = base.manifest.get('ai_read') or {}
    return reader_model(base) or str(read.get('ai_model') or '')


def composed_by_model(base):
    """Whether any piece of this manuscript is not the converter's own text."""
    return any(str(row.get('source') or 'text') != 'text' for row in base.rows)


def line_width(base, document):
    """The served length of each line of a document, gutter and marker included."""
    model = piece_model(base)
    was_read = bool(reader_model(base))
    widths = []
    for number, line in enumerate(base.lines.get(document, []), 1):
        piece = base.piece_at(number, document) or {}
        marker = len(_marker(piece, model, number, was_read))
        widths.append(len(GUTTER.format(number)) + len(line) + marker + 1)
    return widths


def _marker(piece, model='', number=None, read=True):
    """What is served after one line, or ``''`` when the line needs no marker.

    A table is many lines and the marker belongs on the first of them: repeated
    down every row it would be read as part of the data.

    *read* is whether a model read this manuscript's pages. When none did, a
    model's line is still marked and the model still named, but the marker
    does not say it was read off a page image, because no page image was ever
    opened.
    """
    kind = str(piece.get('kind') or '')
    source = str(piece.get('source') or 'text')
    page = piece.get('page')
    where = ' of page ' + str(page) if page else ''
    by = ' by ' + model if model else ' by a model'
    if number is not None and piece.get('lines') and int(piece['lines'][0]) != number:
        return ''
    if kind == 'figure-description':
        return DESCRIPTION_MARKER + where + by + ']'
    # Every line whose source is not the text layer says so, whatever kind it
    # is. The marker used to be written for equations and tables only, which
    # are the pieces a reader repairs — but a paragraph or a caption the reader
    # added off a page the converter returned nothing from is a model's reading
    # of an image too, and it reached the reviewer looking like the authors'
    # own converted text.
    if source in ('ai-reconstructed', 'ai'):
        if not read:
            return (WRITTEN_MARKER + by
                    + ('; the text it replaced is in the map]' if source == 'ai-reconstructed'
                       else ']'))
        return (READ_MARKER + where + by
                + ('; the converter\'s reading is in the map]' if source == 'ai-reconstructed'
                   else '; the converter missed it]'))
    if kind == 'equation':
        return EQUATION_MARKER + ('; view_page ' + str(page) + ']' if page else ']')
    return ''


def parts(base, document=paper_base.MAIN_DOCUMENT, budget=None):
    """The line ranges the document is served in, split only between pieces.

    A piece is never cut in half: a part that ended inside a table or between a
    figure and its caption would make the reader stitch, and the stitching is
    where the reading stops.
    """
    budget = PART_CHARACTERS if budget is None else budget
    widths = line_width(base, document)
    if not widths:
        return [(1, 0)]
    found, start, spent = [], 1, 0
    for number, width in enumerate(widths, 1):
        piece = base.piece_at(number, document)
        at_boundary = piece is None or int(piece['lines'][0]) == number
        if spent and spent + width > budget and at_boundary:
            found.append((start, number - 1))
            start, spent = number, 0
        spent += width
    found.append((start, len(widths)))
    return found


def render(base, document=paper_base.MAIN_DOCUMENT, part=1):
    """One part of a document as the plain text a reviewer reads.

    The answer is text and not JSON on purpose: the JSON a block reader wrapped
    each piece in came to 1.4 to 1.9 times the size of the text it carried, and
    every character of it was spent on telling a reviewer there was more to
    fetch.
    """
    if document not in base.documents:
        raise ValueError('This manuscript has no document named ' + str(document)
                         + '. It holds: ' + ', '.join(base.document_names())
                         + '. Read one of those.')
    ranges = parts(base, document)
    if type(part) is not int or not 1 <= part <= len(ranges):
        raise ValueError(document + ' is served in ' + str(len(ranges)) + ' part'
                         + ('' if len(ranges) == 1 else 's') + ', so there is no part '
                         + str(part) + '.')
    start, end = ranges[part - 1]
    total = base.line_count(document)
    model = piece_model(base)
    was_read = bool(reader_model(base))
    if was_read:
        header = AI_READ_HEADER
    else:
        header = AI_PIECES_HEADER if composed_by_model(base) else HEADER
    head = [document + ' - part ' + str(part) + ' of ' + str(len(ranges)) + ' - lines '
            + str(start) + '-' + str(end) + ' of ' + str(total)
            + (' - ' + str(base.title) if base.title else ''), '',
            header, '']
    body = []
    for number in range(start, end + 1):
        piece = base.piece_at(number, document) or {}
        line = GUTTER.format(number) + base.line_text(number, document)
        body.append(line + _marker(piece, model, number, was_read))
    tail = ['']
    if part < len(ranges):
        tail.append('Lines ' + str(end + 1) + '-' + str(total) + ' are in part ' + str(part + 1)
                    + '. Read it now: read_paper with part ' + str(part + 1) + '.')
    else:
        tail.append('End of ' + document + ', line ' + str(total) + '.')
        others = [name for name in base.document_names() if name != document]
        if others:
            tail.append('Also in this manuscript: ' + ', '.join(others)
                        + '. Read one with read_paper and its document name.')
    return '\n'.join([*head, *body, *tail])


def served(base, document=paper_base.MAIN_DOCUMENT):
    """How many parts a document is served in."""
    return len(parts(base, document))


def search(base, query, limit=20):
    """Every line holding a phrase, with its number, section and page.

    The phrase is matched over each line's normal form, so a search for a
    printed interval written with a minus sign finds it typed with a hyphen.
    """
    from . import paper_text
    needle = paper_text.normalised(query).casefold()
    found = []
    for name in base.document_names():
        for number, line in enumerate(base.lines.get(name, []), 1):
            if needle and needle in paper_text.normalised(line).casefold():
                piece = base.piece_at(number, name) or {}
                found.append({'document': name, 'line': number, 'text': line,
                              'section': piece.get('section'), 'kind': piece.get('kind'),
                              'page': piece.get('page')})
                if len(found) >= limit:
                    return found, True
    return found, False


__all__ = ['PART_CHARACTERS', 'EQUATION_MARKER', 'READ_MARKER', 'WRITTEN_MARKER',
           'DESCRIPTION_MARKER',
           'composed_by_model', 'parts', 'piece_model', 'render', 'reader_model',
           'search', 'served']
