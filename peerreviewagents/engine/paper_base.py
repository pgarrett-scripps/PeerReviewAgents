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
    a reviewer reads is the text and nothing else. A row whose ``source`` is
    not ``text`` was written or rewritten by a model reading the page image,
    and carries ``original``: what the converter had in the fields the repair
    replaced.

A piece has no id, in the file or on the wire. Its identity is its line range
while the manuscript is open, and its fingerprint across versions.
"""
from __future__ import annotations

import re
from pathlib import Path

from ..names import BASE_SCHEMA_VERSION, MAIN_DOCUMENT
from . import paper_text
from .storage import read, write

# The schema version and the main document's name live in
# `peerreviewagents.names`, the module that imports nothing, because they are
# the two things a consumer needs before it has read a single file: the name to
# open and the version to check. They are re-exported here so the engine's own
# modules keep reading them from the format they belong to.

#: Where a piece's text came from. ``text`` is the document's own text layer as
#: the converter read it. ``ai-reconstructed`` is an equation or a table the
#: reader rewrote from the page image, so the manuscript did print it and the
#: converter got it wrong; ``ai`` is text the manuscript never printed as text
#: at all — a figure's description, or a piece the converter dropped and the
#: reader put back. A row that is not ``text`` carries ``original``: what the
#: converter had, so a repair can be audited against what it replaced.
SOURCES = ('text', 'ai', 'ai-reconstructed')

#: Every field a piece may carry: what :func:`piece_lines` renders from, what
#: :func:`compose` copies into the map row, and nothing else. A field outside
#: it is one this format has no column for, so it would be dropped between the
#: caller's dict and ``map.json`` without a word — an application that kept its
#: own ``anchor_id`` on a piece found it gone and nothing to say so.
#: :func:`peerreviewagents.write_manuscript` refuses one by name instead. The
#: engine's own pieces carry working fields as well (``piece_id``, ``read``,
#: ``after``), which is why this is checked at the writer rather than here.
PIECE_FIELDS = frozenset({
    'kind', 'text', 'section', 'page', 'bbox', 'source', 'original',
    'level', 'latex', 'number', 'image', 'label', 'table',
    'confidence', 'page_mapping', 'citations', 'document',
})

#: ``Figure 3``, ``Fig. 3`` at the start of a caption. Whether a figure's
#: caption reaches the base as a ``figure`` piece or a ``caption`` piece depends
#: on what the converter saw around it, and this paper's nine figures arrive
#: eight one way and one the other. A reader and a search should see nine alike,
#: so a caption that names a figure is written the way a figure is; the kind the
#: converter chose stays in ``map.json``, where a consumer can still read it.
FIGURE_CAPTION = re.compile(r'^\s*\(?(?:Figure|Fig)\.?\s*\d{1,3}', re.I)


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
                    'Figure: ' + _one_line(text)]
        return ['Figure: ' + _one_line(text)]
    if kind == 'caption':
        line = _one_line(text)
        return [('Figure: ' if FIGURE_CAPTION.match(line) else 'Caption: ') + line]
    if kind == 'figure-description':
        # A description of a figure image, written by a model that looked at
        # the page. It is a piece of its own, after the caption it belongs to,
        # so the caption keeps the authors' words and the row that says `ai`
        # covers only the sentences no author wrote. The prefix is in the file
        # rather than only in the reviewer's view: `paper.md` is read by
        # applications too, and a paragraph of machine description that reads
        # like the manuscript's own prose would be the one piece of this format
        # that lies about itself.
        return ['Figure description: ' + _one_line(text)]
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
        # What this piece held before a model repaired it, in the fields the
        # repair replaced: `latex` for an equation, `rows` for a table, `text`
        # for anything else, and an empty mapping for a piece the converter
        # never produced. Keeping it is what makes a repair auditable — the two
        # readings sit side by side and a person can see which one the page
        # supports — and it is why `source` alone was not enough.
        if row['source'] != 'text':
            row['original'] = dict(piece.get('original') or {})
        # The reference ids a publisher's XML attached to this piece. They are
        # not in the text — a JATS paragraph carries its citations as markup
        # the Markdown cannot hold — and the citation ledger needs them to know
        # what a sentence cites when no marker survived extraction, so the row
        # is where they live.
        if piece.get('citations'):
            row['citations'] = [str(one) for one in piece['citations']]
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
        self._folded = {}
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

    def projection(self, document=MAIN_DOCUMENT):
        """``(normal_form, offsets, line_starts)`` for a whole document, computed once.

        The normal form of the document, not of a line: a quote that runs over
        a line break — the rows of a table, a sentence quoted across the blank
        separator — is one run of words to a reader and has to be one run here
        too. ``offsets`` maps each character of it back to the file, and
        ``line_starts`` turns that offset into the line number a citation is
        recorded as.
        """
        if document not in self._projections:
            text = self.documents[document]
            normal, offsets = paper_text.normalise(text)
            starts = [0]
            for index, character in enumerate(text):
                if character == '\n':
                    starts.append(index + 1)
            self._projections[document] = (normal, offsets, starts)
        return self._projections[document]

    def folded(self, document=MAIN_DOCUMENT):
        """``(casefolded_normal_form, index_into_the_normal_form)``, computed once.

        Case is the one difference the normal form keeps, because a manuscript
        that says one thing and a quote that says something like it are not the
        same claim. But a reviewer who lowers the first letter of a sentence to
        splice it into its own has not misquoted anyone, so a quote that
        differs only in case is matched here and reported as inexact rather
        than refused. Folding a character can lengthen it, so each character of
        the fold carries the index it came from.
        """
        if document not in self._folded:
            normal, _, _ = self.projection(document)
            characters, origins = [], []
            for index, character in enumerate(normal):
                for folded in (character.casefold() or character):
                    characters.append(folded)
                    origins.append(index)
            self._folded[document] = (''.join(characters), origins)
        return self._folded[document]

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


def check_stamp(manifest, folder):
    """Refuse a manuscript that does not say what shape it is, by name.

    The stamp is the one field a reader can act on before it has read
    anything else: it says which columns a row has and what the manifest
    promises. A ``paper-base-v1`` workspace is refused because its rows cannot
    say whether a model wrote a piece — and a manifest carrying *no*
    ``schema_version`` is refused for the same reason, rather than being read
    as the current shape on the strength of having no opinion. An unstamped
    folder is either older than the stamp or written by something that is not
    this format, and in both cases what its rows mean is a guess.
    """
    stamp = (manifest or {}).get('schema_version')
    if stamp is None:
        raise ValueError(
            'This manuscript folder has no schema_version in its manifest, so it does not say '
            'which shape it is: ' + str(folder) + '. This version reads ' + BASE_SCHEMA_VERSION
            + ' only, and an unstamped folder read as one would have its rows trusted to carry '
              'fields they may not have. Re-import the manuscript into a new folder, or write it '
              'with peerreviewagents.write_manuscript, which stamps it.')
    if stamp != BASE_SCHEMA_VERSION:
        raise ValueError(
            'This manuscript folder is ' + str(stamp) + ' and this version reads '
            + BASE_SCHEMA_VERSION + ' only. A ' + str(stamp) + ' map says where every piece sits '
            'but not whether a model wrote it, so a review over it could not say which equations '
            'and figures were read off the page. Re-import the manuscript into a new folder.')


def load(folder) -> PaperBase:
    """Read a base from a manuscript folder.

    A folder in a shape this version does not write — the old ``paper.json``
    with its blocks, or a ``paper-base-v1`` workspace — is refused by name
    rather than half-read. Reading is forward only: ``v1`` has no ``original``
    on a row and no ``ai_read`` in its manifest, so a consumer cannot tell a
    piece the converter produced from one a model wrote, and there is no way to
    infer the difference from what the folder holds. A folder with no manifest,
    or one whose manifest carries no ``schema_version``, is refused the same
    way by :func:`check_stamp`: it is three files or it is not a manuscript.
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
    if not (folder / 'manifest.json').is_file():
        raise ValueError('Not a manuscript folder: ' + str(folder) + ' holds map.json and '
                         + MAIN_DOCUMENT + ' but no manifest.json, which is where the shape of '
                         'the rows, the paper\'s identity and the account of what read it are '
                         'kept. A manuscript is those three files; a folder with two of them is '
                         'a half-written one.')
    check_stamp(read(folder / 'manifest.json'), folder)
    rows = read(folder / 'map.json')
    if not isinstance(rows, list):
        raise ValueError('map.json holds no list of pieces')
    documents = {}
    for name in {MAIN_DOCUMENT, *(str(row.get('document') or MAIN_DOCUMENT) for row in rows)}:
        path = folder / name
        if not re.fullmatch(r'[A-Za-z0-9._-]+\.md', name) or not path.is_file():
            raise ValueError('map.json names a document this folder does not hold: ' + str(name))
        documents[name] = path.read_text(encoding='utf-8')
    return PaperBase(folder, documents, rows, read(folder / 'manifest.json'))


__all__ = ['BASE_SCHEMA_VERSION', 'MAIN_DOCUMENT', 'SOURCES', 'PaperBase', 'check_stamp',
           'compose', 'load',
           'merge_boxes', 'piece_lines', 'table_lines', 'write_base', 'box_list']
