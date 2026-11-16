"""Import source files without rewriting their scientific content."""
import collections
import json
import re
import shutil
import subprocess
import unicodedata
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from ..names import DRAFT_SCHEMA_VERSION
from . import paper_base
from .storage import digest, read, relative_name, write

#: The converted pieces, before any of them has a line. It is deleted at the
#: seal: a folder that holds both a draft and a base holds the manuscript
#: twice, and the one an application opened would be a coin toss.
DRAFT_FILE = 'draft.json'

POPPLER_MISSING = ('Poppler (pdftotext) is not installed. Install it with `brew install poppler` '
                   'on macOS, `sudo apt install poppler-utils` on Debian or Ubuntu, or '
                   '`choco install poppler` on Windows, then run the command again.')

#: What the pinned converter cannot supply, recorded in the manifest rather
#: than left for a reader to discover. A gap named here is a gap: it is not an
#: omission by the authors and not a piece this import dropped on purpose.
KNOWN_GAPS = {
    'figures': ('The pinned converter returns no figure images, so there is no figures/ folder '
                'and a figure is its caption line in the Markdown.'),
    'equation_confidence': ('Displayed equations carry the converter\'s own confidence in the map. '
                            'Sums and subscripts are the ones it gets wrong, so a low confidence '
                            'means read the page, not that the equation is absent.'),
}

#: What each value of a piece's ``source`` means, written into every manifest so
#: a reader of the folder never has to find this module to interpret the map.
SOURCE_VALUES = {
    'text': "the document's own text layer, as the converter read it",
    'ai': ('written by a model from the page image, not printed as text in the source: a '
           "figure's description, or a piece the converter dropped and the reader put back"),
    'ai-reconstructed': ('printed in the source but mangled by the converter, and rewritten from '
                         'the page image by a model: the row keeps what the converter had in '
                         '`original`'),
}

#: What a repaired piece's ``original`` holds the converter's reading under,
#: by kind: the field the repair replaced, and ``text`` for everything else.
ORIGINAL_FIELD = {'equation': 'latex', 'table': 'rows'}

#: How every piece's ``fingerprint`` in ``map.json`` was computed. It is in the
#: manifest because a consumer comparing two versions of a manuscript needs the
#: rule, not only the digests.
FINGERPRINT_RULE = ('sha256 of the piece\'s lines in their document, NFKC-normalised, whitespace '
                    'runs collapsed, dash and quote variants folded, soft hyphens and zero-width '
                    'characters dropped, case kept')

#: What the four numbers of a ``bbox`` in ``map.json`` are. A box with no unit
#: and no origin cannot be drawn: an application placing a highlight on a page
#: image has to know whether 26.8 is millimetres from the bottom or points from
#: the top, and guessing puts the box at the other end of the page. The
#: converter's boxes were checked against ``pdftotext -bbox`` over the same
#: file, word for word, which is where this wording comes from.
BOX_CONVENTION = ('PDF points, 1/72 inch, in the coordinates of the page as the converter read '
                  'it: the origin is the top-left corner of the page, x grows right and y grows '
                  'down, so x0,y0 is the top-left corner of the box and x1,y1 the bottom-right. '
                  'The page a box belongs to is the piece\'s own `page`, numbered from 1, and '
                  'that page\'s size in the same points is in `pages`. Boxes are not rotated: a '
                  'page whose `rotation` is not 0 is printed turned, and an application placing '
                  'a box on a rendered image of it applies that rotation itself.')

#: ``Page    1 size:  612 x 792 pts (letter)`` and ``Page    1 rot:   0``, as
#: ``pdfinfo`` prints them for every page of a document.
PAGE_SIZE = re.compile(r'^Page\s+(\d+) size:\s+([\d.]+) x ([\d.]+) pts', re.M)
PAGE_ROTATION = re.compile(r'^Page\s+(\d+) rot:\s+(-?\d+)', re.M)

#: What a word count here counts, recorded beside the counts themselves.
WORD_CONVENTION = ('Unicode words with internal apostrophes and hyphens. Includes captions, '
                   'tables and equations. References reported separately. No quality inference '
                   'from counts.')

#: A document of the base is a Markdown file whose name survives being written
#: into JSON and read back on another machine.
DOCUMENT_NAME = re.compile(r'[A-Za-z0-9._-]+\.md')


#: ``Figure 3``, ``Fig. 3`` — at the start of a caption, and anywhere in a
#: sentence that points at one.
FIGURE_CAPTION = re.compile(r'^\s*\(?(?:Figure|Fig)\.?\s*(\d{1,3})', re.I)
FIGURE_CITED = re.compile(r'\b(?:Figures|Figure|Figs|Fig)\.?\s*(\d{1,3})', re.I)


def missing_figures(pieces):
    """Figure numbers the text points at that no caption in this import carries.

    A conversion loses a caption now and then — this paper has captions for
    eight figures and the text discusses nine — and the loss is invisible: a
    reviewer asked to check Figure 9 against its caption finds no caption and
    reports the figure as undescribed, which is a finding about the converter
    wearing the clothes of a finding about the manuscript. Counting both sides
    at import is what lets the manifest say so.
    """
    captioned, cited = set(), set()
    for piece in pieces:
        text = str(piece.get('text') or '')
        if piece.get('kind') in ('figure', 'caption'):
            found = FIGURE_CAPTION.match(text)
            if found:
                captioned.add(int(found.group(1)))
        else:
            cited.update(int(number) for number in FIGURE_CITED.findall(text))
    return sorted(cited - captioned), sorted(captioned)


def page_geometry(pdf, pages):
    """Every page's width and height in points, with the rotation a viewer applies.

    A box in ``map.json`` is a rectangle on a page, and a rectangle is only
    placeable against the size of the page it is on: an app rendering page 3 at
    1500 pixels wide scales by ``1500 / width``, and without the width it is
    scaling by a guess. The numbers come from ``pdfinfo``, which ships with the
    ``pdftotext`` the import already needs. A machine without it loses the page
    sizes rather than the import: the boxes are still in the map, and the
    manifest says ``pages: []`` so a reader can tell that they were not
    recorded rather than that the paper has no pages.
    """
    try:
        printed = subprocess.run(['pdfinfo', '-f', '1', '-l', str(max(pages, 1)), str(pdf)],
                                 check=True, capture_output=True, text=True).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    rotations = {int(number): int(degrees) for number, degrees in PAGE_ROTATION.findall(printed)}
    return [{'page': int(number), 'width': float(width), 'height': float(height),
             'rotation': rotations.get(int(number), 0)}
            for number, width, height in PAGE_SIZE.findall(printed)]


def statistics_of(folder, documents, rows, references=(), pdf_pages=0):
    """The ``statistics`` block of a manuscript manifest.

    Every count is taken on the text that actually went into the written files,
    read back out of them, rather than on the text the pieces were built from:
    the Markdown is the manuscript, and a count of anything else would be a
    count of something no reviewer sees. *documents* is the document names,
    *rows* the map rows :func:`paper_base.write_base` returned.
    """
    folder = Path(folder)
    by_document = collections.defaultdict(list)
    for row in rows:
        by_document[row['document']].append(row)
    written = []
    for name in documents:
        lines = (folder / name).read_text(encoding='utf-8').split('\n')
        for row in by_document[name]:
            start, end = row['lines']
            written.append((row, '\n'.join(lines[start - 1:end])))
    main = [(row, text) for row, text in written
            if row['document'] == paper_base.MAIN_DOCUMENT]
    sections = collections.OrderedDict()
    for row, _ in main:
        sections.setdefault(row['section'], []).append(row['lines'][0])
    return {
        'total_words': sum(len(words(text)) for row, text in written
                           if row['kind'] != 'heading'),
        'main_words': sum(len(words(text)) for row, text in main
                          if row['kind'] not in ('heading', 'reference', 'title')),
        'reference_words': sum(len(words(text)) for row, text in written
                               if row['kind'] == 'reference'),
        'reference_entries': len(references), 'pieces': len(rows),
        'pdf_pages': pdf_pages,
        'lines': {name: len((folder / name).read_text(encoding='utf-8').splitlines())
                  for name in documents},
        # A section is counted by the lines its pieces start on, because a line
        # is what a reader of this manuscript can point at.
        'sections': {name: {'pieces': len(starts),
                            'words': sum(len(text.split()) for row, text in main
                                         if row['lines'][0] in starts
                                         and row['kind'] != 'heading')}
                     for name, starts in sections.items()},
        'convention': WORD_CONVENTION}


def file_digests(folder):
    """Every file in a workspace against its digest, the way the manifest holds it.

    POSIX keys: the scaffold inventory check builds its names with ``as_posix``,
    so a manifest keyed with the OS separator read as a changed inventory on
    Windows and refused every run over a scaffold manuscript.
    The workspace's own ``manifest.json`` is not in it, because it is what
    holds the map. Only that one: a file called ``manifest.json`` deeper in the
    tree is a file like any other — a scaffold manuscript carries one of its
    own — and skipping it by name left it uncovered, so it could be edited
    under a workspace that still verified.
    """
    folder = Path(folder)
    own = folder / 'manifest.json'
    return {relative_name(path, folder): digest(path.read_bytes())
            for path in sorted(folder.rglob('*'))
            if path.is_file() and path != own}


def check_pieces(documents, ai_model=''):
    """Refuse a piece this format would write nothing of, by name.

    Three silences are what this is for. A piece that renders to no line — a
    ``list`` with its items under ``items``, a paragraph with an empty
    ``text`` — was dropped by :func:`~peerreviewagents.engine.paper_base.compose`
    and left no row, so an application that composed twelve pieces got a
    manuscript of eleven and a count that agreed with itself. And a field the
    renderer does not know was dropped between the caller's dict and the map
    row, so an app's own ``anchor_id`` was gone with nothing to say so. And a
    piece whose ``source`` is not ``text`` was written by a model rather than
    by the authors: an ``ai-reconstructed`` piece with no ``original`` claims a
    converter reading it cannot show, and a manuscript holding either kind
    needs *ai_model* — the name of the model that wrote them, which the served
    marker puts beside the line and the manifest records.

    All three are refused here rather than at the row, because the caller still
    has the piece in hand and the index of the piece is the only name it has.
    """
    for name, pieces in documents.items():
        for index, piece in enumerate(pieces, start=1):
            if not isinstance(piece, dict):
                raise ValueError('Piece ' + str(index) + ' of ' + name + ' is a '
                                 + type(piece).__name__ + ': a piece is a mapping of a kind, '
                                 'its text, and whatever that kind renders from.')
            kind = str(piece.get('kind') or 'paragraph')
            unknown = sorted(str(field) for field in piece if field not in paper_base.PIECE_FIELDS)
            if unknown:
                raise ValueError(
                    'Piece ' + str(index) + ' of ' + name + ' (kind "' + kind + '") carries '
                    + 'field(s) this format has no column for: ' + ', '.join(unknown)
                    + '. They would be dropped between here and map.json without a word, so '
                      'the write is refused instead. The fields a piece may carry are '
                    + ', '.join(sorted(paper_base.PIECE_FIELDS))
                    + ', and docs/INTEGRATIONS.md says what each kind renders from.')
            if not [line for line in paper_base.piece_lines(piece) if line.strip()]:
                raise ValueError(
                    'Piece ' + str(index) + ' of ' + name + ' (kind "' + kind + '") renders to '
                    'no line, so it would be dropped and the manuscript would be short of it '
                    'with nothing to say so. A piece is its text: give it text, give a table '
                    'its rows, and leave out what the manuscript does not print.')
            source = str(piece.get('source') or 'text')
            if source == 'ai-reconstructed' and not (piece.get('original') or {}):
                raise ValueError(
                    'Piece ' + str(index) + ' of ' + name + ' (kind "' + kind + '") has source '
                    '"ai-reconstructed" and no original. That source says the text was printed '
                    'in the paper and a model rewrote it from the page, and the map row is where '
                    'the reading it replaced is kept — written without one it would promise an '
                    'audit the folder cannot give. Pass original={"' + ORIGINAL_FIELD.get(
                        kind, 'text') + '": …} with what the piece replaced, or use source "ai" '
                    'for a piece the converter never had.')
            if source != 'text' and not ai_model:
                raise ValueError(
                    'Piece ' + str(index) + ' of ' + name + ' (kind "' + kind + '") has source "'
                    + source + '", so a model wrote it rather than the authors. Name that model '
                    'in ai_model=: a reviewer is served a marker saying who wrote the line, and '
                    'the manifest records it, so a manuscript cannot carry a model\'s words '
                    'with nothing to say whose they are.')


def write_manuscript(folder, documents, title=None, paper_id=None, source=None, parser=None,
                     source_url=None, references=None, assets=None, warnings=None,
                     known_gaps=None, pages=None, ai_model=''):
    """Write a manuscript workspace from pieces an application composed itself.

    This is :func:`import_paper`'s other end: the same workspace, written from
    pieces a caller already has — a Typst or LaTeX source it parsed, a database
    row, an editor's own document — rather than from a PDF this package
    converts. An application that writes one by hand would be a second copy of
    the format, so it writes it with this instead, and what comes back is the
    manifest, the same object :func:`import_paper` returns.

    *documents* is a mapping of file name to that document's pieces, the main
    document first, or just the main document's pieces as a list. A piece is a
    ``kind``, its ``text``, and whatever that kind renders from;
    :mod:`peerreviewagents.engine.paper_base` renders them and
    ``docs/INTEGRATIONS.md`` documents the shape. ``page`` and ``bbox`` are
    optional, because a manuscript built from a source file has no pages: a
    piece without them gets ``null`` in both columns of ``map.json``, and
    nothing that highlights a page region has anything to highlight. Lines are
    the citation either way.

    A caller whose pieces *do* carry boxes passes *pages* as well — one
    ``{'page': n, 'width': w, 'height': h, 'rotation': d}`` per page, in the
    same points the boxes are in, since a box cannot be placed on an image of a
    page whose size is unknown. :data:`BOX_CONVENTION` is the unit and the
    origin, and it is written into the manifest so the folder says it too.

    A piece whose ``source`` is ``ai`` or ``ai-reconstructed`` was written by a
    model, not by the authors, and *ai_model* is that model's name. It is
    required as soon as one such piece is passed and refused with the piece
    otherwise: the marker a reviewer is served says who wrote the line, and a
    manifest that said a manuscript needed no reading while holding a model's
    sentences would be saying something untrue about the paper under review.
    An ``ai-reconstructed`` piece must also carry the ``original`` it replaced.
    No page-reading stage runs here either way — ``ai_read['read']`` stays
    false and says that the pieces were composed by the caller — so what the
    manifest records is authorship, not a read.

    The digests are taken over the folder as it stands, so any source tree the
    caller keeps beside the base — ``scaffold/``, ``original/`` — must be
    written **before** this call, and is then covered by
    :func:`peerreviewagents.verify_workspace` like every other file.
    """
    folder = Path(folder)
    if not isinstance(documents, dict):
        documents = {paper_base.MAIN_DOCUMENT: list(documents or [])}
    documents = collections.OrderedDict((str(name), list(pieces))
                                        for name, pieces in documents.items())
    if paper_base.MAIN_DOCUMENT not in documents:
        raise ValueError('A manuscript is ' + paper_base.MAIN_DOCUMENT + ' and the documents '
                         'beside it: pass that document\'s pieces under its name, or pass them '
                         'as a list on their own.')
    for name in documents:
        if not DOCUMENT_NAME.fullmatch(name):
            raise ValueError('A document name is letters, digits, dots, dashes and underscores '
                             'and ends in .md: ' + name)
    if (folder / 'map.json').exists():
        raise ValueError('Workspace already contains an immutable manuscript')
    ai_model = ' '.join(str(ai_model or '').split())
    check_pieces(documents, ai_model)
    rows = paper_base.write_base(folder, documents)
    if len(rows) != sum(len(pieces) for pieces in documents.values()):
        # Unreachable while `check_pieces` holds, and worth saying out loud if
        # it ever stops holding: the map is the manuscript's index, and an
        # index missing a piece is wrong in a way nothing downstream can see.
        raise ValueError('The map holds ' + str(len(rows)) + ' rows for '
                         + str(sum(len(pieces) for pieces in documents.values()))
                         + ' pieces: a piece reached the writer and left no row.')
    if not any(row['document'] == paper_base.MAIN_DOCUMENT for row in rows):
        raise ValueError('No piece of ' + paper_base.MAIN_DOCUMENT + ' rendered to a line: a '
                         'manuscript with no text is not one, and a workspace holding an empty '
                         'document would anchor every finding at nothing.')
    references = list(references or [])
    # A page number is the caller's, not this function's: pages are what the
    # pieces say they are, and a manuscript with none has none.
    geometry = [{'page': int(page['page']), 'width': float(page['width']),
                 'height': float(page['height']), 'rotation': int(page.get('rotation') or 0)}
                for page in pages or []]
    given = dict(source or {})
    page_count = given.get('pages')
    if page_count is None:
        page_count = max([len(geometry)]
                         + [row['page'] for row in rows if isinstance(row.get('page'), int)])
    text = (folder / paper_base.MAIN_DOCUMENT).read_text(encoding='utf-8').split('\n')
    if not title:
        head = next((row for row in rows if row['kind'] == 'title'
                     and row['document'] == paper_base.MAIN_DOCUMENT), None)
        title = text[head['lines'][0] - 1].lstrip('#').strip() if head else None
    if not title:
        raise ValueError('A manuscript needs a title a reviewer can be told it is reviewing: '
                         'pass title=, or give the main document a piece of kind "title".')
    stats = statistics_of(folder, documents, rows, references, page_count)
    uncaptioned, captioned = missing_figures(documents[paper_base.MAIN_DOCUMENT])
    ai_pieces = sum(1 for row in rows if row['source'] != 'text')
    gaps = dict(known_gaps or {})
    if uncaptioned:
        gaps.setdefault('figure_captions', (
            'The text points at ' + ', '.join('Figure ' + str(n) for n in uncaptioned)
            + ', which no caption in this manuscript carries. Captions were found for '
            + (', '.join(str(n) for n in captioned) or 'no figures')
            + '. Read the figure itself before reporting it as undescribed.'))
    manifest = {
        'schema_version': paper_base.BASE_SCHEMA_VERSION,
        # The identity a review is bound to. Taken over the written documents
        # when the caller does not give one, so the same text written twice is
        # the same manuscript and a redraft is a different one — which is what
        # makes `export_draft` refuse a review of the version before it.
        'paper_id': paper_id or digest(json.dumps(
            {name: digest((folder / name).read_bytes()) for name in documents},
            sort_keys=True))[:16],
        'title': title, 'source_url': source_url,
        'source': {'file': None, 'sha256': None, 'xml': None, 'xml_sha256': None, **given,
                   'pages': page_count},
        'parser': parser or 'unknown',
        'documents': list(documents),
        # The geometry a box is drawn against, and the unit it is written in.
        # An empty list is a manuscript with no pages to draw on.
        'pages': geometry,
        'box_convention': BOX_CONVENTION,
        'counts': {'pieces': len(rows), 'references': len(references),
                   'figure_captions': len(captioned),
                   **collections.Counter(row['kind'] for row in rows)},
        'pieces_per_source': dict(collections.Counter(row['source'] for row in rows)),
        'source_values': dict(SOURCE_VALUES),
        # An application's own source needs no reader: its equations and
        # tables are the ones the author wrote, not a converter's reading of a
        # picture of them, so there is nothing for a model to repair and
        # nothing it would be repairing against. Where some of its pieces were
        # written by a model anyway — a description of a figure, an equation
        # the app's own pipeline rebuilt — no read happened here either, and
        # what the manifest records is who wrote them.
        'ai_read': {'read': False,
                    'reason': ('written from an application\'s own source, which needs no '
                               'reading: every piece is the text the caller composed'
                               if not ai_pieces else
                               'written from an application\'s own source, which needs no '
                               'reading: no stage here read the pages. ' + str(ai_pieces)
                               + ' of its ' + str(len(rows)) + ' pieces were written by '
                               + ai_model + ', which the caller named, and carry source "ai" '
                                            'or "ai-reconstructed"'),
                    **({'ai_model': ai_model, 'ai_pieces': ai_pieces} if ai_pieces else {}),
                    'lost_pages': []},
        'assets': list(assets or []), 'statistics': stats,
        'warnings': list(warnings or []), 'known_gaps': gaps,
        'fingerprint_rule': FINGERPRINT_RULE,
    }
    write(folder / 'references.json', references)
    write(folder / 'statistics.json', stats)
    manifest['files'] = file_digests(folder)
    write(folder / 'manifest.json', manifest)
    return manifest


class PopplerMissing(OSError):
    """Poppler is absent from this machine, which is the operator's to fix.

    The import died as ``FileNotFoundError: [Errno 2] ... 'pdftotext'``, which
    reads like a missing manuscript rather than a missing package, so a person
    on a fresh machine had nothing to act on.
    """


def page_text(pdf):
    """The manuscript's text layer, one entry per page, as Poppler lays it out."""
    try:
        extracted = subprocess.run(['pdftotext', '-layout', str(pdf), '-'],
                                   check=True, capture_output=True, text=True).stdout
    except FileNotFoundError as error:
        raise PopplerMissing(POPPLER_MISSING) from error
    return extracted.split('\f')


#: A page the converter returned nothing from needs this many words in the text
#: layer before the import calls it a loss. A full-page figure carries a running
#: head, a page number and a caption the converter does read; a page of prose
#: carries hundreds. The threshold is what sits between those two.
LOST_PAGE_WORDS = 25


def unconverted_pages(blocks, pages, geometry=()):
    """Every page the PDF has text on that the converter returned no block from.

    One entry per lost page: its number, how many words the text layer reads
    off it, and the rotation a viewer applies to it. The pinned converter
    returns nothing at all for a page whose ``/Rotate`` is not zero — the
    landscape table page comes back empty while every upright page around it
    converts — and nothing for some upright full-page tables either, so the
    rotation is a symptom rather than the rule. The loss is per page either
    way. Poppler's text layer, which the import already reads for its page
    anchors, is the second opinion: a page it reads whole sentences off and the
    converter returned nothing from is a page the conversion lost, and a
    manuscript written from it would be missing a page with nothing to say so.
    """
    seen = {int(block.get('page') or 0) + 1 for block in blocks}
    turned = {int(page['page']): int(page.get('rotation') or 0) for page in geometry or ()}
    found = []
    for number, text in enumerate(pages, start=1):
        count = len(words(text))
        if number not in seen and count >= LOST_PAGE_WORDS:
            found.append({'page': number, 'words': count, 'rotation': turned.get(number, 0)})
    return found


def lost_page_names(lost):
    """``2 (330 words, rotated 90°)`` for each lost page, for a refusal to name."""
    return ', '.join(
        str(page['page']) + ' (' + str(page['words']) + ' words'
        + (', rotated ' + str(page['rotation']) + '°' if page.get('rotation') else '') + ')'
        for page in lost)


def sealed_lost_pages(lost, documents, rows):
    """Each lost page with the lines its added pieces became, for the manifest.

    The draft names an added piece by its draft ``piece_id``, which is the only
    name it has before the seal — and which is a name nothing downstream can
    use: ``map.json`` has no ids, a citation is a document and a line, and an
    application told that page 8 was put back by ``p-141`` could not find a
    word of it. So the manifest's copy carries where each one landed: its
    document and its line range in the sealed base.
    """
    placed = {}
    for name, pieces in documents.items():
        document_rows = [row for row in rows if (row.get('document') or name) == name]
        rendered = [piece for piece in pieces
                    if any(line.strip() for line in paper_base.piece_lines(piece))]
        for piece, row in zip(rendered, document_rows):
            placed[piece.get('piece_id')] = {'document': name, 'lines': list(row['lines'])}
    found = []
    for page in lost:
        found.append({**page,
                      'added': [{'piece': piece_id, **placed.get(piece_id, {})}
                                for piece_id in page.get('added') or []]})
    return found


def lost_page_lines(page):
    """``paper.md lines 120-126`` for every piece added off one lost page."""
    return ', '.join(
        str(one.get('document') or '?') + ' lines '
        + '-'.join(str(number) for number in one.get('lines') or ['?'])
        if one.get('lines') else str(one.get('piece'))
        for one in page.get('added') or [])


def text_of(element):
    return re.sub(r'\s+', ' ', ''.join(element.itertext())).strip() if element is not None else ''


def words(text):
    return re.findall(r"\b[^\W_]+(?:[’'-][^\W_]+)*\b", text, re.UNICODE)


def norm(text):
    return re.sub(r'[^\w]', '', unicodedata.normalize('NFKC', text).lower())


def document_name(stem):
    """The ``.md`` a supplement is written to, from its file name.

    A supplement is a document of the base like the paper itself, so it is read
    by name and its name has to survive being written into JSON and read back
    on another machine: anything that is not a letter, a digit, a dot, a dash
    or an underscore becomes a dash.
    """
    cleaned = re.sub(r'[^A-Za-z0-9._-]+', '-', str(stem)).strip('-.') or 'supplement'
    return 'supplement-' + cleaned + '.md'


def convert_paper(pdf, output, xml=None, title=None, source_url='', supplements=None):
    """Convert a PDF into a draft: the pieces, before any of them has a line.

    This is the first half of an import. It writes ``original/``,
    ``converter.json`` and ``draft.json``, and no part of the base: no
    ``paper.md``, no ``map.json``, no ``manifest.json``. Nothing cites a draft
    and nothing can — a draft has no line numbers, which is the point. A piece
    in it can still be rewritten, added or merged, and
    :func:`seal_draft` turns whatever the draft holds at the end into the
    manuscript, once.

    A caller that wants both halves at once calls :func:`import_paper`.
    """
    import rustypaper
    pdf, output = Path(pdf), Path(output)
    if (output / 'map.json').exists():
        raise ValueError('Workspace already contains an immutable manuscript')
    output.mkdir(parents=True, exist_ok=True)
    (output / 'original').mkdir(exist_ok=True)
    shutil.copy2(pdf, output / 'original' / 'original.pdf')
    markdown, doc = rustypaper.convert(str(pdf))
    # The converter's own view of the PDF, kept for diagnosing a conversion
    # rather than for reading the paper: the Markdown a reviewer reads is
    # composed from the pieces below, where every piece has a line and a map
    # row, and `markdown` has neither.
    write(output / 'converter.json', doc)
    raw_pages = page_text(pdf)
    if raw_pages and not raw_pages[-1].strip():
        raw_pages.pop()
    normalized_pages = [norm(page) for page in raw_pages]
    geometry = page_geometry(pdf, len(raw_pages))
    parser = 'rustypaper ' + getattr(rustypaper, '__version__', 'unknown')
    # With publisher XML the pieces are the publisher's and the PDF is only
    # consulted for page anchors, so a page the converter dropped costs an
    # anchor rather than the text. Without it, the converter is the only
    # reading of the paper there is, and a page it returned nothing from is a
    # page of the paper that would be absent from the manuscript. It is
    # recorded here and carried on the draft rather than refused: the Read
    # stage looks at every page anyway, and putting back what the converter
    # dropped is the reader's work. :func:`seal_draft` is where it is refused,
    # and only for a manuscript no reader is going to read.
    lost = [] if xml else unconverted_pages(doc.get('blocks') or [], raw_pages, geometry)
    pieces, references, assets, warnings = [], [], [], []
    supplement_pieces = collections.OrderedDict()

    def locate(text):
        clean = norm(text)
        if len(clean) < 35:
            return None
        probes = [clean[i:i+55] for i in range(0, min(len(clean), 500)-54, 55)]
        hits = [(sum(probe in page for probe in probes), number+1) for number, page in enumerate(normalized_pages)]
        best = max(hits, default=(0, None))
        return best[1] if best[0] >= 2 else None

    def add(text, section, kind='paragraph', page=None, bbox=None, citations=None,
            document=paper_base.MAIN_DOCUMENT, table=None, **extra):
        """One piece of one document, in reading order.

        A piece with nothing to render is not a piece: the converter emits
        empty blocks, and a blank line in the Markdown with a map row over it
        would be a citable nothing.
        """
        piece = {'text': text, 'section': section, 'kind': kind, 'page': page, 'bbox': bbox,
                 'citations': citations or [], 'table': table, 'source': 'text',
                 'document': document, **extra}
        if not any(line.strip() for line in paper_base.piece_lines(piece)):
            return
        if document == paper_base.MAIN_DOCUMENT:
            if page is None and extra.get('page_mapping') is None:
                located = locate(text) if kind != 'equation' else None
                piece['page'] = located
                piece['page_mapping'] = 'text_anchor' if located else 'unavailable'
            elif page is not None and extra.get('page_mapping') is None:
                piece['page_mapping'] = 'converter'
            pieces.append(piece)
        else:
            supplement_pieces.setdefault(document, []).append(piece)

    if xml:
        xml = Path(xml)
        raw = xml.read_bytes()
        if re.search(br'<!ENTITY\s', raw, re.I):
            raise ValueError('XML entity declarations are not accepted')
        root = ET.fromstring(raw)
        shutil.copy2(xml, output / 'original' / 'original.xml')
        source_title = text_of(root.find('.//article-title'))
        title = title or source_title
        if title:
            add(title, 'Front matter', 'title')
        for p in root.findall('./front/article-meta/abstract/p'):
            add(text_of(p), 'Abstract')

        def walk(parent, section, depth=1):
            for e in parent:
                tag = e.tag.rsplit('}', 1)[-1]
                if tag == 'sec':
                    heading = text_of(e.find('title')) or section
                    add(heading, heading, 'heading', level=depth)
                    walk(e, heading, min(depth + 1, 5))
                elif tag == 'p':
                    cites = [rid for x in e.findall('.//xref') if x.get('ref-type')=='bibr' for rid in x.get('rid','').split()]
                    add(text_of(e), section, citations=cites)
                elif tag == 'disp-formula':
                    # A publisher's displayed equation is TeX when the XML
                    # carries it and the rendered characters otherwise. Either
                    # way it is a piece of its own, on its own line, so it can
                    # be cited like any other.
                    latex = text_of(e.find('.//tex-math'))
                    add(latex or text_of(e), section, 'equation',
                        latex=latex or text_of(e), number=text_of(e.find('label')))
                elif tag in ('fig','table-wrap','supplementary-material'):
                    label = text_of(e.find('label'))
                    caption = ' '.join(filter(None,[label,text_of(e.find('caption'))]))
                    rows = [[text_of(c) for c in row if c.tag in ('td','th')] for row in e.findall('.//tr')]
                    if rows:
                        add(caption, section, 'caption', label=label)
                        add('', section, 'table', table=rows)
                    else:
                        add(caption, section, 'caption', label=label)
                    assets.append({'id':e.get('id',''),'label':label,
                                   'kind':tag,'available':'caption_in_manuscript','href':e.get('{http://www.w3.org/1999/xlink}href','')})
                elif tag in ('boxed-text','list','list-item'):
                    walk(e, section, depth)
        body = root.find('body')
        if body is not None:
            walk(body,'Body')
        for e in root.findall('.//ref-list/ref'):
            raw_ref = text_of(e)
            doi = next((text_of(x) for x in e.findall('.//pub-id') if x.get('pub-id-type')=='doi'), '')
            if not doi:
                match=re.search(r'(?:doi:\s*|https?://(?:dx\.)?doi\.org/)(10\.\d{4,9}/\S+)',raw_ref,re.I)
                doi=match.group(1).rstrip('.,') if match else ''
            references.append({'id':e.get('id',f'r{len(references)+1}'),'label':text_of(e.find('label')),
                               'raw':raw_ref,'doi':doi,'title':text_of(e.find('.//article-title')),'year':text_of(e.find('.//year'))})
        # Supporting captions can occur in back matter instead of the body.
        existing = {a['id'] for a in assets}
        for e in root.findall('./back//supplementary-material'):
            if e.get('id') not in existing:
                holder=ET.Element('wrapper')
                holder.append(e)
                walk(holder,'Supporting information')
        warnings.append('Publisher XML supplies text and references. PDF page links are conservative text matches, not complete layout reconstruction.')
    else:
        title = title or doc.get('title') or pdf.stem
        section = 'Front matter'
        for b in doc['blocks']:
            kind = b['kind']['type']
            if kind == 'heading':
                section = b['text']
            rows = b.get('table', {}).get('rows')
            text = b.get('text', '') or ''
            if kind == 'reference':
                # The box travels with the reference. A reference is a piece of
                # the paper like any other and a reader following a citation
                # wants it highlighted on the page it is printed on, so where
                # the converter saw it is kept rather than re-found by text.
                ref = b.get('reference', {})
                references.append({'id': f'r{len(references)+1}', **ref, 'raw': text,
                                   'page': b.get('page', 0) + 1, 'bbox': b.get('bbox')})
                continue
            math = b.get('math') or {}
            if kind == 'equation':
                # The converter returns an equation with no text and its LaTeX
                # under `math`, so an importer that reads `text` alone dropped
                # every displayed equation in the paper — eleven of eleven in
                # the antenna manuscript — and the reviewers argued about
                # algebra they had never been shown.
                add(math.get('latex') or '', section, 'equation', page=b.get('page', 0) + 1,
                    bbox=b.get('bbox'), latex=math.get('latex') or '',
                    number=math.get('number') or '', confidence=math.get('confidence'))
                continue
            if rows and not text.strip():
                add('', section, 'table', page=b.get('page', 0) + 1, bbox=b.get('bbox'), table=rows)
                continue
            add(text, section, kind, page=b.get('page', 0) + 1, bbox=b.get('bbox'),
                table=rows, level=b['kind'].get('level'))
        warnings.append('PDF-only references and structure are provisional. Check extraction against the original before review.')
    for ref in references:
        add(ref['raw'], 'References', 'reference', source_id=ref['id'], page=ref.get('page'),
            bbox=ref.get('bbox'))
    if supplements:
        for path in sorted(Path(supplements).glob('*')):
            if path.suffix.lower() not in ('.docx','.png','.tif','.tiff','.jpg','.pdf'):
                continue
            dest=output/'supplements'/path.name
            dest.parent.mkdir(exist_ok=True)
            shutil.copy2(path,dest)
            name = document_name(path.stem)
            if path.suffix.lower()=='.docx':
                with zipfile.ZipFile(path) as z:
                    item=z.getinfo('word/document.xml')
                    if item.file_size>5_000_000:
                        raise ValueError('Supplement document exceeds text limit')
                    root=ET.fromstring(z.read(item))
                ns={'w':'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
                section = 'Supplement ' + path.stem
                add(path.stem, section, 'title', document=name, source_file=path.name)
                for e in root.find('w:body',ns):
                    if e.tag.endswith('}tbl'):
                        rows=[[' '.join(text_of(p) for p in cell.findall('w:p',ns)) for cell in row.findall('w:tc',ns)] for row in e.findall('w:tr',ns)]
                        add('', section, 'table', document=name, table=rows, source_file=path.name)
                    elif e.tag.endswith('}p'):
                        add(text_of(e), section, 'supplement', document=name,
                            source_file=path.name)
            elif path.suffix.lower() in ('.tif','.tiff'):
                from PIL import Image
                image=Image.open(path)
                image.thumbnail((1800,1800))
                image.convert('RGB').save(dest.with_suffix('.png'))
            assets.append({'id':path.stem,'label':path.name,'kind':'supplement','path':'supplements/'+path.name,'preview':'supplements/'+path.with_suffix('.png').name if path.suffix.lower() in ('.tif','.tiff') else None,'available':'file','document':name if path.suffix.lower()=='.docx' else None})
    if not pieces:
        # Every page empty rather than one of them: a scan with no text layer,
        # or a PDF whose text neither the converter nor Poppler can reach. The
        # draft would hold nothing, the reader would have nothing to repair,
        # and the manuscript would be a file with no lines in it.
        raise ValueError(
            'Nothing was converted from this PDF: ' + parser + ' returned no text and the text '
            'layer has none either, which is what a scan with no text layer reads like. The '
            'import is refused rather than writing a manuscript with no lines in it.')
    documents = collections.OrderedDict([(paper_base.MAIN_DOCUMENT, pieces)])
    documents.update(supplement_pieces)
    draft = {
        'schema_version': DRAFT_SCHEMA_VERSION,
        # The identity is the source file's, not the written text's, so it is
        # the same manuscript whether or not a reader repaired anything. A run
        # knows its `paper_id` before the base exists, which is what lets
        # protocol.json be written during the Read stage.
        'paper_id': digest(json.dumps({'pdf': digest(pdf.read_bytes()),
                                       'xml': digest(Path(xml).read_bytes()) if xml else None},
                                      sort_keys=True))[:16],
        'title': title, 'source_url': source_url,
        'source': {'file': 'original/original.pdf', 'sha256': digest(pdf.read_bytes()),
                   'pages': len(raw_pages),
                   'xml': 'original/original.xml' if xml else None,
                   'xml_sha256': digest(Path(xml).read_bytes()) if xml else None},
        'parser': parser,
        'documents': documents,
        'references': references,
        'assets': assets,
        'warnings': warnings,
        # Every page's size in the points the boxes are written in, so a viewer
        # can scale a box to the image it rendered of that page, and the unit
        # and origin those four numbers are in.
        'pages': geometry,
        'page_count': len(raw_pages),
        # The pages the converter returned nothing from, each with the words
        # the text layer reads off it. They are the reader's to put back, and
        # nothing seals over one that is still empty.
        'lost_pages': lost,
        'diagnostics': ['converter.json'],
    }
    write(output / DRAFT_FILE, draft)
    return draft


def seal_draft(output, draft=None, ai_read=None):
    """Write the base from a draft, once, and return the manifest.

    This is the only writer of ``paper.md``, ``map.json`` and
    ``manifest.json``: an import with no AI read and an import a reader worked
    over both end here, so there is one place where a line number comes into
    existence. After it the lines never move again.

    *ai_read* is the account of the Read stage — the model, the pages it
    viewed, what it repaired — or ``None`` for a manuscript nothing read, which
    the manifest then says in as many words.
    """
    output = Path(output)
    if (output / 'map.json').exists():
        raise ValueError('Workspace already contains an immutable manuscript')
    if draft is None:
        draft = read(output / DRAFT_FILE)
    if draft.get('schema_version') != DRAFT_SCHEMA_VERSION:
        raise ValueError('Not a ' + DRAFT_SCHEMA_VERSION + ' draft: '
                         + str(draft.get('schema_version')))
    from . import reader as read_stage
    documents = collections.OrderedDict(
        (str(name), list(pieces)) for name, pieces in draft['documents'].items())
    references = list(draft.get('references') or [])
    page_count = int(draft.get('page_count') or 0)
    parser = draft.get('parser') or 'unknown'
    lost = read_stage.lost_pages(draft)
    empty = [page for page in lost if not page['added']]
    if empty and not (ai_read or {}).get('read'):
        # No reader is going to put these pages back, and sealing would write a
        # manuscript the paper's own pages are missing from with nothing
        # downstream able to tell. With the Read stage on, the same pages are
        # outstanding items the reader adds the text of off the page image.
        raise ValueError(
            parser + ' returned no text from page(s) ' + lost_page_names(empty)
            + ' of this PDF, which the text layer reads whole sentences from. Sealing as '
              'converted is refused: the manuscript would be missing those pages and nothing '
              'downstream could tell. Import with the Read stage on, which hands those pages to '
              'the reader to transcribe, or report the pages against ' + parser + '.')
    if empty:
        # The reader ran and left a page the converter returned nothing from
        # with nothing on it. `seal_manuscript` refuses this before it gets
        # here; this is the same refusal at the one place that writes lines.
        raise ValueError(
            'Page(s) ' + lost_page_names(empty) + ' are pages ' + parser + ' returned no text '
            'from, and the read added nothing off them, so the manuscript would be missing them. '
            'View each page and add what it prints with add_piece before sealing.')
    rows = paper_base.write_base(output, documents)
    stats = statistics_of(output, documents, rows, references, page_count)
    uncaptioned, captioned = missing_figures(documents[paper_base.MAIN_DOCUMENT])
    gaps = dict(KNOWN_GAPS)
    read_account = dict(ai_read) if ai_read else {
        'read': False,
        'reason': 'no reader ran over this manuscript: its equations and tables are the '
                  'converter\'s reading of the page, mangled where it misread one, and its '
                  'figures are captions with no description of the image'}
    if read_account.get('read'):
        # The two gaps this stage closes. A figure now has a description and a
        # low-confidence equation has been looked at, so leaving the import's
        # wording in place would send a reviewer to the page image for work
        # that has already been done.
        gaps['figures'] = (
            'The pinned converter returns no figure images, so there is no figures/ folder. A '
            'figure is its caption line followed by a description of the image written by '
            + str(read_account.get('reader_model') or 'a model') + ', which is a separate piece '
            'whose source is "ai". The description is not the authors\' words.')
        gaps['equation_confidence'] = (
            'Displayed equations carry the converter\'s own confidence in the map. Every piece '
            'the converter emitted as an equation was checked against the page image: one whose '
            'source is "ai-reconstructed" was rewritten from that image and keeps the '
            'converter\'s LaTeX in `original`, and one whose source is "text" was confirmed as '
            'the converter had it. The confidence is the converter\'s and was not updated. What '
            'the converter emitted as something else is not covered by that: it labels a '
            'displayed equation a paragraph often enough that an equation may be sitting in a '
            'line whose source is "text", and the reader is asked to look for those but nothing '
            'can list them. Inline maths inside a paragraph was not rechecked at all.')
    # A page nothing was converted from is in the manuscript only because a
    # model transcribed it off the image. A reader of the base is told which
    # pages those are, and which lines of which document they became, rather
    # than meeting ordinary-looking lines whose source happens to be "ai". The
    # list is written on every seal, empty included, so an application reading
    # a manifest can tell a manuscript that lost no page from one written
    # before anything counted them.
    sealed = sealed_lost_pages(lost, documents, rows)
    read_account['lost_pages'] = sealed
    if lost:
        gaps['converter_pages'] = (
            parser + ' returned no text from page(s) ' + lost_page_names(lost)
            + ', which the text layer reads whole sentences from. What those pages contribute to '
              'this manuscript was transcribed from the page image by '
            + str(read_account.get('reader_model') or 'a model')
            + ' and its pieces carry source "ai": '
            + '; '.join('page ' + str(page['page']) + ' → ' + lost_page_lines(page)
                        for page in sealed)
            + '. They are not the converter\'s reading of the text layer, because there was none. '
              'The same pages are in ai_read.lost_pages as data, each with the lines its '
              'transcription became.')
    if uncaptioned:
        gaps['figure_captions'] = (
            'The text points at ' + ', '.join('Figure ' + str(n) for n in uncaptioned)
            + ', which no caption in this manuscript carries. Captions were found for '
            + (', '.join(str(n) for n in captioned) or 'no figures')
            + '. A figure with no caption here may still be captioned in the PDF: read the page '
              'before reporting it as undescribed.')
    manifest = {
        'schema_version': paper_base.BASE_SCHEMA_VERSION,
        'paper_id': draft['paper_id'], 'title': draft.get('title'),
        'source_url': draft.get('source_url') or '',
        'source': dict(draft.get('source') or {}),
        'parser': draft.get('parser') or 'unknown',
        'documents': list(documents),
        'pages': list(draft.get('pages') or []),
        'box_convention': BOX_CONVENTION,
        # The captioned figures are counted on their own because the kinds do
        # not answer the question: a caption arrives as a `figure` piece or as
        # a `caption` piece depending on what the converter saw around it, so
        # neither count tells a reader how many figures this paper describes.
        'counts': {'pieces': len(rows), 'references': len(references),
                   'figure_captions': len(captioned),
                   **collections.Counter(row['kind'] for row in rows)},
        'pieces_per_source': dict(collections.Counter(row['source'] for row in rows)),
        'source_values': dict(SOURCE_VALUES),
        # Whether a model read this manuscript against its own pages, and which
        # one. A folder that says `read: false` is the converter's output as it
        # stood: its equations are whatever the converter made of them and its
        # figures are captions with nothing behind them.
        'ai_read': read_account,
        'assets': list(draft.get('assets') or []), 'statistics': stats,
        'warnings': list(draft.get('warnings') or []),
        'known_gaps': gaps,
        'diagnostics': list(draft.get('diagnostics') or []),
        'fingerprint_rule': FINGERPRINT_RULE,
    }
    write(output / 'references.json', references)
    write(output / 'statistics.json', stats)
    (output / DRAFT_FILE).unlink(missing_ok=True)
    manifest['files'] = file_digests(output)
    write(output / 'manifest.json', manifest)
    # The manifest, which is the import's own account of what it wrote: the
    # manuscript itself is `paper.md` and its map, and a caller that wants the
    # text reads it with `paper_base.load`.
    return manifest


def import_paper(pdf, output, xml=None, title=None, source_url='', supplements=None,
                 ai_read=None):
    """Convert a PDF and seal it in one call, with no stage in between.

    The whole import for a caller that has no reader to run: the pieces the
    converter produced become the manuscript exactly as they are, and the
    manifest says the paper was not AI-read.
    """
    draft = convert_paper(pdf, output, xml=xml, title=title, source_url=source_url,
                          supplements=supplements)
    return seal_draft(output, draft, ai_read=ai_read)
