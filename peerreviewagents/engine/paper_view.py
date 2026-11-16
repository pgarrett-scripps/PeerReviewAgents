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

#: On every equation line, because the converter mangles sums and subscripts and
#: a reviewer who trusts the text files a finding about algebra the authors
#: never wrote. It is served, never written into paper.md: the file stays the
#: manuscript.
EQUATION_MARKER = '   [machine-read equation, may be garbled'

HEADER = """Read every line of this before anything else. A finding quotes this text, and the
line numbers in the left column are what a citation records; quote the text, not
the numbers.

Displayed equations and inline $...$ maths were machine-read from the PDF and are
sometimes garbled — a sum, a subscript, or the word after an inline formula. Every
equation line below carries a marker saying so. Open the page with view_page and
read the equation there before you write anything about an author's mathematics."""


def line_width(base, document):
    """The served length of each line of a document, gutter and marker included."""
    widths = []
    for number, line in enumerate(base.lines.get(document, []), 1):
        piece = base.piece_at(number, document) or {}
        marker = len(_marker(piece)) if piece.get('kind') == 'equation' else 0
        widths.append(len(GUTTER.format(number)) + len(line) + marker + 1)
    return widths


def _marker(piece):
    page = piece.get('page')
    return EQUATION_MARKER + ('; view_page ' + str(page) + ']' if page else ']')


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
    head = [document + ' - part ' + str(part) + ' of ' + str(len(ranges)) + ' - lines '
            + str(start) + '-' + str(end) + ' of ' + str(total)
            + (' - ' + str(base.title) if base.title else ''), '', HEADER, '']
    body = []
    for number in range(start, end + 1):
        piece = base.piece_at(number, document) or {}
        line = GUTTER.format(number) + base.line_text(number, document)
        body.append(line + (_marker(piece) if piece.get('kind') == 'equation' else ''))
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


__all__ = ['PART_CHARACTERS', 'EQUATION_MARKER', 'parts', 'render', 'search', 'served']
