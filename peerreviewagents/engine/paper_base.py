"""The manuscript base: one Markdown file per document, one map row per piece.

An imported manuscript used to be a list of blocks inside ``paper.json``, each
with an id minted at import. Three applications had to agree on that numbering,
the ids said nothing to a reader, and a reviewer could only see the manuscript
through a tool that handed back twelve blocks at a time wrapped in more JSON
than text. Measured over seven saved runs, core reviewers had seen between half
and almost all of the main text, and the editor as little as fifteen per cent,
while leaving most of their call allowance unspent: the shape, not the budget,
was what stopped them reading.

So the base is a file a person can open:

``paper.md``
    the whole paper in reading order, LF endings, one piece per line with a
    blank line between pieces, and no ids in the text. A heading keeps its
    hashes, a displayed equation is ``$$ latex \\tag{n} $$``, a table is
    consecutive Markdown rows, a figure is a caption line, a reference is one
    line. Supplements are their own ``.md`` beside it.

``map.json``
    one row per piece, keyed by the line range it occupies, holding the kind,
    the section, the page, the box on that page, where the text came from and a
    fingerprint of its normal form. Pages and boxes live only here, so the text
    a reviewer reads is the text and nothing else.

A piece has no id, in the file or on the wire. Its identity is its line range
while the manuscript is open, and its fingerprint across versions.
"""
from __future__ import annotations

import re
from pathlib import Path

from . import paper_text
from .storage import read, write

#: What ``map.json`` and ``manifest.json`` are. The base is written once per
#: import and never edited, so a reader checks this before trusting the rows.
BASE_SCHEMA_VERSION = 'paper-base-v1'

#: The name of the main document. Supplements are ``supplement-<name>.md``.
MAIN_DOCUMENT = 'paper.md'

#: Where a piece's text came from. ``text`` is the document's own text layer as
#: the converter read it. ``ai`` is a page image read by a model and
#: ``ai-reconstructed`` is a passage not printed at all, rebuilt from context —
#: both are values this format can hold, and nothing writes them yet. A
#: consumer that treats every piece as printed text would be wrong later, so
#: the field exists from the start.
SOURCES = ('text', 'ai', 'ai-reconstructed')


def _one_line(text) -> str:
    """``text`` as a single line: a piece is one line, so its newlines are spaces.

    Nothing is dropped except layout. A paragraph the converter broke across
    the column gap reads as one sentence again, which is also how a reviewer
    will quote it.
    """
    return ' '.join(str(text or '').split())


def table_lines(rows) -> list:
    """A table as consecutive Markdown rows, header first, then the delimiter.

    The delimiter row is part of the table's own line range. It is not text the
    manuscript printed, and no reviewer will quote it, but leaving it out would
    make the piece something Markdown does not render as a table — and the
    point of the base is that the file is readable as it stands.
    """
    cleaned = [[_one_line(cell).replace('|', '\\|') for cell in row] for row in rows if row]
    if not cleaned:
        return []
    width = max(len(row) for row in cleaned)
    out = []
    for index, row in enumerate(cleaned):
        padded = list(row) + [''] * (width - len(row))
        out.append('| ' + ' | '.join(padded) + ' |')
        if index == 0:
            out.append('| ' + ' | '.join(['---'] * width) + ' |')
    return out


def piece_lines(piece) -> list:
    """The lines one piece occupies in its document, rendered from its kind."""
    kind = str(piece.get('kind') or 'paragraph')
    text = piece.get('text') or ''
    if kind == 'table' and piece.get('table'):
        return table_lines(piece['table'])
    if kind == 'title':
        return ['# ' + _one_line(text)]
    if kind == 'heading':
        # The converter's level 1 is a section heading under the title, so the
        # hashes start at two. A deeper level than Markdown has is clamped
        # rather than dropped: the text matters, the depth is a hint.
        level = piece.get('level')
        depth = min(6, max(2, int(level) + 1 if isinstance(level, int) else 2))
        return ['#' * depth + ' ' + _one_line(text)]
    if kind == 'equation':
        latex = _one_line(piece.get('latex') or text)
        number = _one_line(piece.get('number') or '')
        return ['$$ ' + latex + (' \\tag{' + number + '}' if number else '') + ' $$']
    if kind == 'figure':
        image = piece.get('image')
        if image:
            return ['![' + _one_line(piece.get('label') or 'Figure') + '](' + str(image) + ')',
                    'Caption: ' + _one_line(text)]
        return ['Figure: ' + _one_line(text)]
    if kind == 'caption':
        return ['Caption: ' + _one_line(text)]
    return [_one_line(text)]


def merge_boxes(boxes, pages):
    """One box for a piece built from several of the converter's own blocks.

    The page is the first contributor's, the box is the union of the boxes on
    that page, and the answer is ``None`` the moment a contributor is missing a
    box or sits on another page: a rectangle spanning two pages would point at
    nothing, and a guess would point somewhere wrong.
    """
    pages = list(pages)
    boxes = list(boxes)
    if not boxes or not pages or any(box is None for box in boxes):
        return None, (pages[0] if pages else None)
    page = pages[0]
    kept = [box for box, where in zip(boxes, pages) if where == page]
    if len(kept) != len(boxes):
        return None, page
    return [min(box[0] for box in kept), min(box[1] for box in kept),
            max(box[2] for box in kept), max(box[3] for box in kept)], page


def box_list(bbox):
    """The converter's box as four numbers, or ``None``."""
    if isinstance(bbox, dict):
        try:
            return [float(bbox['x0']), float(bbox['y0']), float(bbox['x1']), float(bbox['y1'])]
        except (KeyError, TypeError, ValueError):
            return None
    if isinstance(bbox, (list, tuple)) and len(bbox) == 4:
        try:
            return [float(value) for value in bbox]
        except (TypeError, ValueError):
            return None
    return None


def compose(pieces, document=MAIN_DOCUMENT):
    """``(markdown, rows)`` for one document: its text, and one row per piece.

    Line numbers are 1-based and count every line of the file, the blank
    separators included, so the numbers the engine reports are the numbers a
    text editor shows. A piece that renders as nothing — the converter does
    emit empty blocks — gets no line and no row.
    """
    lines: list[str] = []
    rows: list[dict] = []
    for piece in pieces:
        rendered = [line for line in piece_lines(piece) if line.strip()]
        if not rendered:
            continue
        if lines:
            lines.append('')
        start = len(lines) + 1
        lines.extend(rendered)
        row = {'lines': [start, len(lines)], 'kind': str(piece.get('kind') or 'paragraph'),
               'section': piece.get('section'), 'page': piece.get('page'),
               'bbox': box_list(piece.get('bbox')),
               'source': piece.get('source') if piece.get('source') in SOURCES else 'text',
               'fingerprint': paper_text.fingerprint('\n'.join(rendered)),
               'document': document}
        for extra in ('confidence', 'page_mapping'):
            if piece.get(extra) is not None:
                row[extra] = piece[extra]
        rows.append(row)
    return '\n'.join(lines) + ('\n' if lines else ''), rows


def write_base(folder, documents):
    """Write every document and the one ``map.json`` that covers all of them.

    ``documents`` maps a file name to its pieces, the main document first. The
    files are written with LF endings whatever the platform, because the line
    numbers in ``map.json`` are the identity of every citation made against
    them and a translated newline would renumber nothing while changing the
    bytes a digest is taken over.
    """
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    rows = []
    for name, pieces in documents.items():
        markdown, document_rows = compose(pieces, name)
        (folder / name).write_text(markdown, encoding='utf-8', newline='\n')
        rows.extend(document_rows)
    write(folder / 'map.json', rows)
    return rows


class PaperBase:
    """A loaded base: the documents' text, their lines, and the map over them.

    This is the index every reader works from. It holds no ids — a piece is
    reached by a line number, and a line number is what a finding cites.
    """

    def __init__(self, folder, documents, rows, manifest):
        self.folder = Path(folder)
        self.documents = documents
        self.rows = rows
        self._projections = {}
        self.manifest = manifest or {}
        self.paper_id = self.manifest.get('paper_id')
        self.title = self.manifest.get('title')
        self.lines = {name: text.split('\n') for name, text in documents.items()}
        for name, lines in self.lines.items():
            if lines and lines[-1] == '':
                lines.pop()
        self._by_line = {}
        for row in rows:
            name = row.get('document') or MAIN_DOCUMENT
            start, end = row['lines']
            for number in range(int(start), int(end) + 1):
                self._by_line[(name, number)] = row

    @property
    def text(self) -> str:
        """The main document, as written."""
        return self.documents.get(MAIN_DOCUMENT, '')

    def document_names(self) -> list:
        """Every document in the base, the main one first."""
        names = [name for name in self.documents if name == MAIN_DOCUMENT]
        return names + sorted(name for name in self.documents if name != MAIN_DOCUMENT)

    def line_count(self, document=MAIN_DOCUMENT) -> int:
        return len(self.lines.get(document, []))

    def line_text(self, line, document=MAIN_DOCUMENT) -> str:
        """One line of a document, or ``''`` when the number is outside it."""
        lines = self.lines.get(document, [])
        return lines[line - 1] if 1 <= int(line) <= len(lines) else ''

    def range_text(self, start, end=None, document=MAIN_DOCUMENT) -> str:
        """The lines from ``start`` to ``end``, joined as they stand."""
        end = start if end is None else end
        return '\n'.join(self.line_text(number, document)
                         for number in range(int(start), int(end) + 1))

    def piece_at(self, line, document=MAIN_DOCUMENT):
        """The map row whose line range holds this line, or ``None``.

        A line inside a table or a two-line figure answers with the piece that
        contains it, which is how a citation of one table row still reports the
        page and the box of the table it belongs to.
        """
        return self._by_line.get((document, int(line)))

    def pieces(self, document=MAIN_DOCUMENT) -> list:
        return [row for row in self.rows if (row.get('document') or MAIN_DOCUMENT) == document]

    def section_at(self, line, document=MAIN_DOCUMENT):
        piece = self.piece_at(line, document)
        return (piece or {}).get('section')


def load(folder) -> PaperBase:
    """Read a base from a manuscript folder.

    A folder in the shape this version does not write — the old ``paper.json``
    with its blocks — is refused by name rather than half-read, because every
    line number a run would record against it would be invented.
    """
    folder = Path(folder)
    if not (folder / 'map.json').is_file() or not (folder / MAIN_DOCUMENT).is_file():
        if (folder / 'paper.json').is_file():
            raise ValueError(
                'This manuscript folder holds paper.json and its blocks, which this version does '
                'not read: findings cite lines of paper.md now. Re-import the manuscript from '
                'original/original.pdf — or from the source the folder was built from — into a '
                'new folder.')
        raise ValueError('Not a manuscript folder: ' + str(folder)
                         + ' holds neither map.json nor ' + MAIN_DOCUMENT)
    rows = read(folder / 'map.json')
    if not isinstance(rows, list):
        raise ValueError('map.json holds no list of pieces')
    documents = {}
    for name in {MAIN_DOCUMENT, *(str(row.get('document') or MAIN_DOCUMENT) for row in rows)}:
        path = folder / name
        if not re.fullmatch(r'[A-Za-z0-9._-]+\.md', name) or not path.is_file():
            raise ValueError('map.json names a document this folder does not hold: ' + str(name))
        documents[name] = path.read_text(encoding='utf-8')
    manifest = read(folder / 'manifest.json') if (folder / 'manifest.json').is_file() else {}
    return PaperBase(folder, documents, rows, manifest)


__all__ = ['BASE_SCHEMA_VERSION', 'MAIN_DOCUMENT', 'SOURCES', 'PaperBase', 'compose', 'load',
           'merge_boxes', 'piece_lines', 'table_lines', 'write_base', 'box_list']
