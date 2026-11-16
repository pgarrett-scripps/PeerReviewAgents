"""The Read stage: one model repairing a draft against its own page images.

The converter is the only thing that has ever read the PDF, and it reads parts
of it badly. On the antenna manuscript it returns eleven displayed equations of
which about half are mangled, and its own confidence does not separate them:
two of the wrong ones come back at 1.0. Tables arrive linearised, with cells
under the wrong heading. Figures arrive as a caption and nothing else, so a
reviewer reading the text cannot see the figure at all.

So between the conversion and the manuscript there is a stage that looks at the
pages. It works on the draft — pieces with no line numbers, which nothing can
cite yet — and for every equation, table and figure it either repairs the piece
from the page image or confirms it as printed. The repairs are the tools here.
Each one records what it replaced, so a reviewer, or a person auditing the run,
can put the two readings side by side and see which the page supports.

Nothing in this module writes ``paper.md``. The draft becomes the manuscript in
one place, :func:`ingest.seal_draft`, after this stage has accounted for
everything. Until then a line number does not exist to be wrong.
"""
from __future__ import annotations

import base64
import json
import subprocess
from pathlib import Path

from . import pages, paper_base
from .storage import write

#: The kinds this stage must account for, as the reader sees them. A figure is
#: whatever renders as a figure caption, which the converter may emit as a
#: ``figure`` piece or as a ``caption`` piece depending on what it saw around
#: it; both need a description, and neither is settled without one.
ACCOUNTABLE = ('equation', 'table', 'figure')

#: What a repair may add that the converter missed.
ADDABLE = ('equation', 'table', 'figure-description')

#: How much of a piece the listing shows. An equation, a table or a caption is
#: shown whole, because that is what gets repaired; prose is context, and a
#: reader that needs more of it is reading the page image anyway.
CONTEXT_CHARACTERS = 300

#: Pieces per part of a long listing.
PART_PIECES = 120


def is_figure(piece):
    """Whether this piece is what a reviewer will read as a figure."""
    if str(piece.get('kind') or '') == 'figure':
        return True
    if str(piece.get('kind') or '') != 'caption':
        return False
    return bool(paper_base.FIGURE_CAPTION.match(str(piece.get('text') or '').strip()))


def account_kind(piece):
    """``'equation'``, ``'table'``, ``'figure'`` — or ``None`` for prose."""
    kind = str(piece.get('kind') or '')
    if kind in ('equation', 'table'):
        return kind
    return 'figure' if is_figure(piece) else None


def number_pieces(draft):
    """Give every piece an id, leaving the ones that have one alone.

    The ids are the reader's handle on a draft that has no line numbers. They
    are assigned once and never reused: a piece added during the stage takes
    the next number, so an id in a tool log names the same piece afterwards as
    it did at the time.
    """
    highest = 0
    for pieces in draft.get('documents', {}).values():
        for piece in pieces:
            current = str(piece.get('piece_id') or '')
            if current.startswith('p') and current[1:].isdigit():
                highest = max(highest, int(current[1:]))
    for pieces in draft.get('documents', {}).values():
        for piece in pieces:
            if not piece.get('piece_id'):
                highest += 1
                piece['piece_id'] = 'p' + str(highest)
    return draft


def locate(draft, piece_id):
    """``(document, index, piece)`` for one id, or a refusal naming it."""
    for document, pieces in draft.get('documents', {}).items():
        for index, piece in enumerate(pieces):
            if piece.get('piece_id') == piece_id:
                return document, index, piece
    raise ValueError('No piece ' + str(piece_id) + ' in this draft. The ids are the ones '
                     'read_draft returned.')


def rendered(piece):
    """The piece as it would read in the manuscript, on one line or several."""
    return '\n'.join(paper_base.piece_lines(piece))


def shown(piece):
    """The text the listing shows for a piece."""
    text = rendered(piece)
    if account_kind(piece) or str(piece.get('kind') or '') in ('figure-description', 'caption'):
        return text
    if len(text) > CONTEXT_CHARACTERS:
        return text[:CONTEXT_CHARACTERS] + ' …'
    return text


def viewed(draft):
    """Every page this draft's reading has opened, in order."""
    return sorted(int(page) for page in (draft.get('read') or {}).get('pages_viewed') or [])


def record_view(draft, page):
    """Remember that the page was rendered, across claims of the reader slot."""
    read = draft.setdefault('read', {})
    seen = set(read.get('pages_viewed') or [])
    seen.add(int(page))
    read['pages_viewed'] = sorted(seen)
    return read['pages_viewed']


def settle(piece, action, page):
    """Mark a piece accounted for, by what was done to it and off which page."""
    piece['read'] = {'action': action, 'page': int(page)}
    return piece


def outstanding(draft):
    """What still stands between this draft and the seal.

    Two lists: pieces nobody has repaired or confirmed, and pages nobody has
    opened. A page is on it because a mangled equation the converter dropped
    altogether is only visible on the page — a stage that accounted for every
    piece it was handed and never looked at page nine cannot have found what
    page nine is missing.
    """
    left = []
    for document, pieces in draft.get('documents', {}).items():
        for piece in pieces:
            kind = account_kind(piece)
            if kind and not (piece.get('read') or {}).get('action'):
                left.append({'piece': piece.get('piece_id'), 'kind': kind,
                             'document': document, 'page': piece.get('page'),
                             'section': piece.get('section')})
    seen = set(viewed(draft))
    count = int(draft.get('page_count') or 0)
    return {'pieces': left, 'pages': [page for page in range(1, count + 1) if page not in seen]}


def tally(draft):
    """How the stage went: pieces by what was done to them, and pages opened."""
    counts = {}
    for pieces in draft.get('documents', {}).values():
        for piece in pieces:
            action = (piece.get('read') or {}).get('action')
            if action:
                counts[action] = counts.get(action, 0) + 1
    return {'pieces': counts, 'pages_viewed': len(viewed(draft)),
            'pages': int(draft.get('page_count') or 0)}


class Desk:
    """The reader's tools over one draft, in the shape :class:`Investigation` has.

    It is handed to the reader's slot where every other slot gets an
    investigation, so one dispatch in the server covers both: a tool call is a
    tool call, the allowance is spent the same way, and the page the reader
    opens is recorded by the same line that records a reviewer's.
    """

    def __init__(self, workspace, draft, out, pages_seen=None):
        self.workspace = Path(workspace)
        self.draft = number_pieces(draft)
        self.out = Path(out)
        self.out.mkdir(parents=True, exist_ok=True)
        #: Pages this holder has opened — the slot holder's own set, shared
        #: with its :class:`Agent` so there is one answer to the question. The
        #: draft remembers every page the stage has opened across claims; this
        #: is one holder's, and it is what a repair is checked against, because
        #: the point of the gate is that the model writing the LaTeX looked at
        #: the page.
        self.pages_seen = pages_seen if isinstance(pages_seen, set) else set(pages_seen or ())

    def save(self):
        from .ingest import DRAFT_FILE
        write(self.workspace / DRAFT_FILE, self.draft)

    def log(self, name, args, result):
        """One line per call, refusals included, beside the stage's own output.

        The page image is not written into the log — it is in ``views/`` — and
        neither is the draft listing, which is a copy of a file the folder
        holds. What a repair replaced is, because that is the one record of
        what the converter had and what the model put there instead.
        """
        line = {'tool': name, 'arguments': args,
                'result': {key: value for key, value in result.items()
                           if key not in ('image', 'pieces')}}
        with (self.out / 'tool-log.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(line, ensure_ascii=False) + '\n')

    def call(self, name, args):
        """One tool call, in the shape the server's dispatch expects of it."""
        args = dict(args or {})
        try:
            result = self.dispatch(name, args)
        except (ValueError, KeyError, TypeError, IndexError,
                subprocess.SubprocessError) as exc:
            result = {'request_rejected': str(exc)}
        self.log(name, args, result)
        return result

    # --- the gate ---------------------------------------------------------

    def off_page(self, page):
        """The page a repair was read from, refused unless this holder saw it."""
        if type(page) is not int:
            raise ValueError('Give the page you read this from, as a number.')
        if page not in self.pages_seen:
            raise ValueError(
                'You have not viewed page ' + str(page) + ' in this attempt. A repair is a '
                'transcription of what is printed on a page, so the server takes one only from a '
                'reader that has opened that page: call view_page(' + str(page) + ') first.')
        return page

    def target(self, piece_id, kinds, verb):
        document, index, piece = locate(self.draft, piece_id)
        kind = account_kind(piece) or str(piece.get('kind') or '')
        if kind not in kinds:
            # A figure is never settled by confirming it. The converter returns
            # no figure images at all, so "as printed" for a figure is a
            # caption and a blank where the picture was, which is the gap this
            # stage exists to close.
            pointer = (' A figure is accounted for by describe_figure: what the image shows is '
                       'not in the text, so there is nothing to confirm.'
                       if kind == 'figure' else '')
            raise ValueError(str(piece_id) + ' is a ' + kind + ' piece, and ' + verb
                             + ' is for ' + ' or '.join(kinds) + ' pieces.' + pointer)
        return document, index, piece

    # --- the tools --------------------------------------------------------

    def dispatch(self, name, args):
        if name == 'read_draft':
            return self.read_draft(str(args.get('document') or ''), args.get('part') or 1)
        if name == 'view_page':
            return self.view_page(args.get('page'))
        if name == 'replace_equation':
            return self.replace_equation(str(args.get('piece') or ''), args.get('latex') or '',
                                         args.get('number') or '', args.get('page'))
        if name == 'replace_table':
            return self.replace_table(str(args.get('piece') or ''), args.get('rows') or [],
                                      args.get('page'))
        if name == 'describe_figure':
            return self.describe_figure(str(args.get('piece') or ''),
                                        args.get('description') or '', args.get('page'))
        if name == 'add_piece':
            return self.add_piece(str(args.get('after') or ''), str(args.get('kind') or ''),
                                  args.get('page'), args.get('text') or '',
                                  args.get('latex') or '', args.get('number') or '',
                                  args.get('rows') or [])
        if name == 'merge_equations':
            return self.merge_equations(list(args.get('pieces') or []), args.get('latex') or '',
                                        args.get('number') or '', args.get('page'))
        if name == 'confirm_piece':
            return self.confirm_piece(str(args.get('piece') or ''), args.get('page'))
        raise ValueError('Unknown reader tool')

    def read_draft(self, document='', part=1):
        """The converted pieces of one document, numbered, with what is left to do."""
        documents = list(self.draft.get('documents') or {})
        name = document or (documents[0] if documents else paper_base.MAIN_DOCUMENT)
        if name not in documents:
            raise ValueError('No document named ' + name + '. This draft holds: '
                             + ', '.join(documents))
        pieces = self.draft['documents'][name]
        if part in (None, ''):
            part = 1
        parts = max(1, -(-len(pieces) // PART_PIECES))
        if type(part) is not int or not 1 <= part <= parts:
            raise ValueError('Part must be between 1 and ' + str(parts))
        chosen = pieces[(part - 1) * PART_PIECES:part * PART_PIECES]
        listed = []
        for piece in chosen:
            row = {'piece': piece.get('piece_id'), 'kind': str(piece.get('kind') or 'paragraph'),
                   'section': piece.get('section'), 'page': piece.get('page'),
                   'source': piece.get('source') or 'text', 'text': shown(piece)}
            needs = account_kind(piece)
            if needs:
                row['account'] = needs
                row['settled'] = (piece.get('read') or {}).get('action')
            if piece.get('confidence') is not None:
                row['confidence'] = piece['confidence']
            listed.append(row)
        return {'document': name, 'documents': documents, 'part': part, 'parts': parts,
                'pieces': listed, 'outstanding': outstanding(self.draft),
                'page_count': int(self.draft.get('page_count') or 0),
                'reading': ('The text here is the converter\'s. Where it disagrees with the page '
                            'image, the page image is the manuscript.')}

    def view_page(self, page):
        """One page of the original PDF, as the image a repair is read from."""
        target = pages.page_image(self.workspace, page, self.out,
                                  int(self.draft.get('page_count') or 0))
        self.pages_seen.add(int(page))
        record_view(self.draft, page)
        self.save()
        return {'label': 'Original PDF page ' + str(page), 'page': int(page),
                'image': base64.b64encode(target.read_bytes()).decode()}

    def replace_equation(self, piece_id, latex, number, page):
        """Rewrite one displayed equation's LaTeX from the page image."""
        page = self.off_page(page)
        latex = ' '.join(str(latex).split())
        if not latex:
            raise ValueError('Give the equation\'s LaTeX. To keep what the converter had, '
                             'confirm_piece it instead.')
        document, index, piece = self.target(piece_id, ('equation',), 'replace_equation')
        piece.setdefault('original', {'latex': piece.get('latex'), 'number': piece.get('number'),
                                      'text': piece.get('text')})
        piece['latex'] = latex
        piece['number'] = ' '.join(str(number).split())
        piece['text'] = latex
        piece['source'] = 'ai-reconstructed'
        settle(piece, 'repaired', page)
        self.save()
        return {'piece': piece_id, 'kind': 'equation', 'source': piece['source'],
                'text': rendered(piece), 'replaced': piece['original'],
                'outstanding': outstanding(self.draft)}

    def replace_table(self, piece_id, rows, page):
        """Rewrite one table's cells from the page image, row by row."""
        page = self.off_page(page)
        rows = [[str(cell) for cell in row] for row in rows]
        if not rows or not any(any(cell.strip() for cell in row) for row in rows):
            raise ValueError('Give the table\'s rows, the header row first. To keep what the '
                             'converter had, confirm_piece it instead.')
        document, index, piece = self.target(piece_id, ('table',), 'replace_table')
        piece.setdefault('original', {'rows': piece.get('table'), 'text': piece.get('text')})
        piece['table'] = rows
        piece['text'] = ''
        piece['source'] = 'ai-reconstructed'
        settle(piece, 'repaired', page)
        self.save()
        return {'piece': piece_id, 'kind': 'table', 'source': piece['source'],
                'text': rendered(piece), 'replaced': piece['original'],
                'outstanding': outstanding(self.draft)}

    def describe_figure(self, piece_id, description, page):
        """Write what the figure's image shows, as a piece after its caption."""
        page = self.off_page(page)
        description = ' '.join(str(description).split())
        if len(description) < 40:
            raise ValueError('A figure description says what is plotted, the axes and their '
                             'units, the series, and the values and trends that are visible. '
                             'This one is too short to be that.')
        document, index, piece = self.target(piece_id, ('figure',), 'describe_figure')
        pieces = self.draft['documents'][document]
        following = pieces[index + 1] if index + 1 < len(pieces) else None
        if following is not None and str(following.get('kind')) == 'figure-description' \
                and following.get('describes') == piece_id:
            following['text'] = description
            following['page'] = page
            written = following
        else:
            written = {'text': description, 'section': piece.get('section'),
                       'kind': 'figure-description', 'page': page, 'bbox': None,
                       'citations': [], 'table': None, 'source': 'ai',
                       'document': document, 'describes': piece_id,
                       'original': {}, 'piece_id': self.next_id()}
            pieces.insert(index + 1, written)
        settle(piece, 'described', page)
        settle(written, 'added', page)
        self.save()
        return {'piece': piece_id, 'description_piece': written['piece_id'], 'source': 'ai',
                'text': rendered(written), 'outstanding': outstanding(self.draft)}

    def add_piece(self, after, kind, page, text='', latex='', number='', rows=()):
        """Add a piece the converter never produced, after the one it belongs behind."""
        page = self.off_page(page)
        if kind not in ADDABLE:
            raise ValueError('A reader adds ' + ', '.join(ADDABLE) + '. For anything else the '
                             'manuscript is the authors\' and stays as converted.')
        document, index, anchor = locate(self.draft, after)
        piece = {'text': ' '.join(str(text).split()), 'section': anchor.get('section'),
                 'kind': kind, 'page': page, 'bbox': None, 'citations': [], 'table': None,
                 'source': 'ai', 'document': document, 'original': {},
                 'piece_id': self.next_id()}
        if kind == 'equation':
            piece['latex'] = ' '.join(str(latex).split())
            piece['number'] = ' '.join(str(number).split())
            piece['text'] = piece['latex']
            if not piece['latex']:
                raise ValueError('Give the equation\'s LaTeX.')
        if kind == 'table':
            piece['table'] = [[str(cell) for cell in row] for row in rows]
            if not piece['table']:
                raise ValueError('Give the table\'s rows, the header row first.')
        if kind == 'figure-description':
            piece['describes'] = after
            if len(piece['text']) < 40:
                raise ValueError('A figure description says what is plotted, the axes and their '
                                 'units, the series, and the values and trends that are visible.')
        if not any(line.strip() for line in paper_base.piece_lines(piece)):
            raise ValueError('That piece would render as nothing.')
        settle(piece, 'added', page)
        self.draft['documents'][document].insert(index + 1, piece)
        self.save()
        return {'piece': piece['piece_id'], 'kind': kind, 'after': after, 'source': 'ai',
                'text': rendered(piece), 'outstanding': outstanding(self.draft)}

    def merge_equations(self, piece_ids, latex, number, page):
        """One equation the converter split into several pieces, put back together."""
        page = self.off_page(page)
        latex = ' '.join(str(latex).split())
        if not latex:
            raise ValueError('Give the whole equation\'s LaTeX, as one line.')
        if len(piece_ids) < 2:
            raise ValueError('Merging is for two or more pieces that are one equation.')
        found = [self.target(str(one), ('equation',), 'merge_equations') for one in piece_ids]
        if len({document for document, _, _ in found}) != 1:
            raise ValueError('Those pieces are in different documents.')
        document = found[0][0]
        keeper = found[0][2]
        keeper['original'] = {'latex': keeper.get('latex'), 'number': keeper.get('number'),
                              'text': keeper.get('text'),
                              'merged': [{'piece': piece.get('piece_id'),
                                          'latex': piece.get('latex'),
                                          'number': piece.get('number')}
                                         for _, _, piece in found[1:]]}
        keeper['latex'] = latex
        keeper['number'] = ' '.join(str(number).split())
        keeper['text'] = latex
        keeper['source'] = 'ai-reconstructed'
        settle(keeper, 'merged', page)
        dropped = {piece.get('piece_id') for _, _, piece in found[1:]}
        self.draft['documents'][document] = [piece for piece
                                             in self.draft['documents'][document]
                                             if piece.get('piece_id') not in dropped]
        self.save()
        return {'piece': keeper['piece_id'], 'removed': sorted(dropped), 'source': keeper['source'],
                'text': rendered(keeper), 'replaced': keeper['original'],
                'outstanding': outstanding(self.draft)}

    def confirm_piece(self, piece_id, page):
        """Say that the converter's text is what the page prints, having looked."""
        page = self.off_page(page)
        document, index, piece = self.target(piece_id, ('equation', 'table'), 'confirm_piece')
        settle(piece, 'confirmed', page)
        self.save()
        return {'piece': piece_id, 'kind': account_kind(piece), 'source': piece.get('source'),
                'text': rendered(piece), 'outstanding': outstanding(self.draft)}

    def next_id(self):
        highest = 0
        for pieces in self.draft.get('documents', {}).values():
            for piece in pieces:
                current = str(piece.get('piece_id') or '')
                if current.startswith('p') and current[1:].isdigit():
                    highest = max(highest, int(current[1:]))
        return 'p' + str(highest + 1)
