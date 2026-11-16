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

#: What a repair may add that the converter missed. A caption is here because
#: the converter loses them: on one paper it emitted Table 3's caption *inside*
#: the table block, so the caption was nowhere a reader could repair it and
#: nowhere a reviewer could read it. A caption belongs to the display object it
#: names rather than to the authors' running prose, which is the line this list
#: draws.
ADDABLE = ('equation', 'table', 'caption', 'figure-description')

#: What may be added on a page the converter returned nothing from, over and
#: above :data:`ADDABLE`. That page's words are in the draft nowhere at all, so
#: its prose has to be addable somewhere; everywhere else the running text is
#: the authors' as the converter read it, and a model writing a paragraph into
#: it would be writing the paper.
ADDABLE_ON_LOST_PAGE = ('paragraph',)

#: The authors' running text. A piece of one of these kinds is never typed by
#: this stage: a re-read of one names the spans to take out, or the one span to
#: keep, and the engine cuts them — see :func:`prose_from`.
PROSE_KINDS = ('paragraph', 'footnote', 'heading', 'title')

#: How many spans one prose re-read may cut, and how much of the piece they may
#: come to between them. The case they are for is the antenna paragraph that
#: swallowed equation 10: ``"We have SNR ln 2 "``, 17 characters of a
#: 700-character paragraph, 2.4% of it, in one span. Three is room for a
#: paragraph that caught the debris of two displayed pieces and a stray
#: equation number, and a fifth of a paragraph is already far more than any
#: debris — a model asking to cut that much is asking to rewrite, which is the
#: thing this stage must not do.
MOST_SPANS = 3
MOST_REMOVED = 0.2

#: What is printed as a block of its own rather than as running text. The
#: reader writes these from the page image: an equation, a table and a caption
#: are the three things the converter reads worst, and what it produced for one
#: is no constraint on what the page turns out to print there.
DISPLAY_KINDS = ('equation', 'table', 'caption')

#: The kinds one piece may be re-read as. The converter's ``kind`` is a guess
#: from the layout — it emitted a full-page table as a one-line ``footnote``
#: and a displayed equation as a ``paragraph`` — so the kind a piece arrives
#: with is not the kind the page prints, and a stage that could only repair
#: what the converter had already labelled left both of those as garbage.
REREADABLE = DISPLAY_KINDS + PROSE_KINDS

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


def converter_reading(piece):
    """Everything the converter read into one piece, as one string.

    Text, LaTeX and every cell of a table, because a piece being re-read may be
    any of those: the one that held Table 3, its caption and all of Table 4 was
    a table, and the caption a reader takes out of it is in its cells.
    """
    found = [str(piece.get('text') or ''), str(piece.get('latex') or '')]
    for row in piece.get('table') or []:
        found.extend(str(cell) for cell in row)
    return ' '.join(part for part in found if part.strip())


def span(held, wanted, where, named):
    """``(start, end)`` in *held*, for the one place it reads *wanted*.

    The reader names a span of prose; the engine finds it and cuts it. Matching
    is on the normal form, the same one a citation's quote is matched on, so a
    span typed with a hyphen for a minus or without the line break the
    converter left in the middle of it is still the span the page prints; the
    offsets take the match back to the characters, so what comes out is the
    converter's own text and not a normalised copy of it.

    A span that reads nowhere is refused with the whole of what the converter
    read, because the model is working from a page image and the converter's
    line may not be the page's. A span that reads twice is refused for being
    two places at once: the engine will not pick.
    """
    from . import paper_text
    normal, offsets = paper_text.normalise(held)
    needle = paper_text.normalised(wanted)
    if not needle:
        raise ValueError(where + ': ' + named + ' is empty. Give the words to take out, as they '
                         'stand in the converter\'s reading.')
    found = [at for at in range(len(normal) - len(needle) + 1) if normal.startswith(needle, at)]
    if not found:
        raise ValueError(where + ': ' + named + ' reads nowhere in this piece, so there is '
                         'nothing to cut. Give it as the converter has it — it read: ' + held)
    if len(found) > 1:
        raise ValueError(where + ': ' + named + ' reads ' + str(len(found)) + ' times in this '
                         'piece, and the engine will not choose which. Give a longer span, one '
                         'that reads once.')
    at = found[0]
    start, last = offsets[at], offsets[at + len(needle) - 1]
    end = last + 1
    # A cluster the normal form expanded — a ligature, a compatibility form —
    # is several normal characters at one offset, and cutting half of one would
    # leave the other half in the prose. The cut takes the whole character.
    while end < len(held) and (at + len(needle)) < len(offsets) \
            and offsets[at + len(needle)] == last:
        end += 1
    return start, end


def prose_from(held, part, where):
    """The text of a prose part of a re-read: a cut of *held*, never typed.

    A model asked to retype a paragraph minus some debris retypes the paragraph
    too, and a character-by-character comparison does not catch it — almost any
    invented sentence is a subsequence of a long paragraph, which is how
    "This result is highly significant." was accepted onto the end of an
    abstract. So prose is not written here at all. The reader says what to take
    out, in ``remove``, or which single run of it to keep, in ``keep``, and the
    engine does the cutting: every character that comes out is one the
    converter read, in the order it read it, because it is a slice of it.
    """
    if str(part.get('text') or '').strip():
        raise ValueError(where + ': the running text is the authors\', so a paragraph, footnote, '
                         'heading or title is not typed here. Name the debris to take out of what '
                         'the converter read, in `remove` — ["We have SNR ln 2"] — or the one run '
                         'of it this piece is, in `keep`. It read: ' + held)
    cuts = [str(one) for one in (part.get('remove') or []) if str(one).strip()]
    keep = str(part.get('keep') or '')
    if cuts and keep.strip():
        raise ValueError(where + ': give `remove` or `keep`, not both. `remove` keeps the piece '
                         'and cuts the spans you name; `keep` takes the one span you name and '
                         'drops the rest.')
    if keep.strip():
        start, end = span(held, keep, where, '`keep`')
        return held[start:end]
    if not cuts:
        raise ValueError(where + ': name the debris to take out, in `remove`, or the one run of '
                         'the converter\'s reading this piece is, in `keep`. To leave the prose '
                         'exactly as the converter read it, leave this part out of `pieces` — the '
                         'piece it comes from is what is being replaced.')
    if len(cuts) > MOST_SPANS:
        raise ValueError(where + ': ' + str(len(cuts)) + ' spans is more than the ' +
                         str(MOST_SPANS) + ' this is for. `remove` takes a displayed piece\'s '
                         'debris out of a paragraph; a paragraph that needs more cuts than that '
                         'is one you are rewriting, and this stage does not rewrite prose.')
    places = sorted(span(held, one, where, '`remove[' + str(number) + ']`')
                    for number, one in enumerate(cuts))
    for (_, ends), (begins, _) in zip(places, places[1:]):
        if begins < ends:
            raise ValueError(where + ': those spans overlap, so one of them is part of another. '
                             'Name each run of debris once.')
    from . import paper_text
    whole = len(paper_text.normalised(held))
    cut = sum(len(paper_text.normalised(held[start:end])) for start, end in places)
    if whole and cut > whole * MOST_REMOVED:
        raise ValueError(where + ': that takes out ' + str(round(100 * cut / whole)) + '% of what '
                         'the converter read here, and `remove` is for a displayed piece\'s '
                         'debris, which is a few words. If most of this piece is not prose, '
                         're-read it as the table or equation it is and leave no paragraph part.')
    rest, kept = 0, []
    for start, end in places:
        kept.append(held[rest:start])
        rest = end
    kept.append(held[rest:])
    written = ' '.join(''.join(kept).split())
    if not written:
        raise ValueError(where + ': those spans are the whole piece, so no prose is left. Leave '
                         'the paragraph part out of `pieces` instead.')
    return written


def original_of(piece):
    """The converter's whole reading of a piece, as a map row keeps it."""
    account = {'kind': str(piece.get('kind') or 'paragraph'), 'text': piece.get('text'),
               'latex': piece.get('latex'), 'number': piece.get('number'),
               'rows': piece.get('table')}
    return {key: value for key, value in account.items() if value not in (None, '')}


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


def shaped(kind, page, document, section, where, text='', latex='', number='', rows=()):
    """One new piece of a kind, refused by *where* if it carries no content.

    *where* is how the refusal names this piece — ``'add_piece'`` or a part's
    position in a re-read — because a caller that passed the wrong argument has
    to be told which piece it got wrong. The check is on the content and not on
    the rendered line: a caption with no text renders as ``Caption: ``, which
    is not empty and is not a caption either, and one of those reached a sealed
    manuscript where nothing could take it out again.
    """
    piece = {'text': ' '.join(str(text).split()), 'section': section, 'kind': kind,
             'page': int(page), 'bbox': None, 'citations': [], 'table': None,
             'source': 'ai', 'document': document, 'original': {}}
    if kind == 'equation':
        piece['latex'] = ' '.join(str(latex).split())
        piece['number'] = ' '.join(str(number).split())
        piece['text'] = piece['latex']
        if not piece['latex']:
            raise ValueError(where + ': give the equation\'s LaTeX, in `latex`.')
    elif kind == 'table':
        piece['table'] = [[str(cell) for cell in row] for row in rows]
        if not any(any(str(cell).strip() for cell in row) for row in piece['table']):
            raise ValueError(where + ': give the table\'s rows, the header row first, in `rows`.')
    elif kind == 'figure-description':
        if len(piece['text']) < 40:
            raise ValueError(where + ': a figure description says what is plotted, the axes and '
                                     'their units, the series, and the values and trends that are '
                                     'visible. This one is too short to be that.')
    elif not piece['text']:
        raise ValueError(where + ': a ' + kind + ' is its text, and this one has none. Give it in '
                                                 '`text`.')
    if not any(line.strip() for line in paper_base.piece_lines(piece)):
        raise ValueError(where + ': that piece would render as nothing.')
    return piece


def settle(piece, action, page):
    """Mark a piece accounted for, by what was done to it and off which page."""
    piece['read'] = {'action': action, 'page': int(page)}
    return piece


def pieces_added_on(draft, page):
    """The ids of the pieces a reader added off one page image."""
    found = []
    for pieces in draft.get('documents', {}).values():
        for piece in pieces:
            account = piece.get('read') or {}
            if account.get('action') == 'added' and int(account.get('page') or 0) == int(page):
                found.append(piece.get('piece_id'))
    return found


def lost_pages(draft):
    """Each page the converter returned nothing from, with what was put back.

    The import records them — number, the words the text layer reads off the
    page, the rotation a viewer applies to it — because the converter returns
    nothing at all for some pages, a turned one and the odd upright full-page
    table, and the page would otherwise be absent from the manuscript with
    nothing to say so. A page is accounted for once the reader has added a
    piece off it: that is the one action that puts text back that the
    conversion never had.
    """
    found = []
    for entry in draft.get('lost_pages') or []:
        page = int(entry.get('page') or 0)
        found.append({'page': page, 'words': int(entry.get('words') or 0),
                      'rotation': int(entry.get('rotation') or 0),
                      'added': pieces_added_on(draft, page)})
    return found


def outstanding(draft):
    """What still stands between this draft and the seal.

    Three lists: pieces nobody has repaired or confirmed, pages nobody has
    opened, and pages the converter returned nothing from that nobody has put
    anything back on. A page is on the second because a mangled equation the
    converter dropped altogether is only visible on the page — a stage that
    accounted for every piece it was handed and never looked at page nine
    cannot have found what page nine is missing — and on the third because
    that page's text is not in the draft at all.
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
    return {'pieces': left,
            'pages': [page for page in range(1, count + 1) if page not in seen],
            'lost_pages': [page for page in lost_pages(draft) if not page['added']]}


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

    def retype(self, piece_id, kind, page, verb):
        """One piece re-read as a kind of its own, with the converter's reading kept.

        The kind the converter gave a piece is not checked, because it is a
        guess from the layout and the page is the fact: it labelled a full-page
        table a one-line ``footnote`` and a displayed equation a ``paragraph``,
        and while this gate was on the converter's kind neither of them could
        be repaired at all. What is refused is a figure — its image is not in
        the text, so there is nothing to retype — and a piece this stage has
        already settled some other way.
        """
        document, index, piece = locate(self.draft, piece_id)
        if is_figure(piece):
            raise ValueError(str(piece_id) + ' is the caption of a figure, and ' + verb + ' would '
                             'replace it. Its image is what is missing, not its text: describe it '
                             'with describe_figure.')
        held = str(piece.get('kind') or 'paragraph')
        if held == 'caption' and kind != 'caption':
            raise ValueError(str(piece_id) + ' is a caption, and ' + verb + ' would write over '
                             'it. A reviewer reads a caption and the piece it names together, so '
                             'a ' + kind + ' written in its place leaves the manuscript with no '
                             'caption and nothing saying it had one. Use reread_piece, which '
                             'writes several pieces where there was one: give the caption and '
                             'the ' + kind + ' as two.')
        if held in PROSE_KINDS and kind in PROSE_KINDS:
            raise ValueError(str(piece_id) + ' is a ' + held + ' and ' + verb + ' would rewrite it '
                             'as prose. The running text is the authors\' as the converter read '
                             'it. To take a displayed piece\'s debris out of a paragraph, use '
                             'reread_piece, which only lets you remove what is there.')
        piece['original'] = piece.get('original') or original_of(piece)
        piece['kind'] = kind
        piece['source'] = 'ai-reconstructed'
        if kind == 'equation':
            piece['table'] = None
        if kind == 'table':
            piece['latex'] = ''
            piece['number'] = ''
        return document, index, piece

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
        if name == 'reread_piece':
            return self.reread_piece(str(args.get('piece') or ''), args.get('page'),
                                     list(args.get('pieces') or []))
        if name == 'withdraw_piece':
            return self.withdraw_piece(str(args.get('piece') or ''))
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
        left = outstanding(self.draft)
        answer = {'document': name, 'documents': documents, 'part': part, 'parts': parts,
                  'pieces': listed, 'outstanding': left,
                  'page_count': int(self.draft.get('page_count') or 0),
                  'reading': ('The text here is the converter\'s, and so is every `kind`: it '
                              'guesses from the layout, and it is wrong often enough that a '
                              'one-line paragraph of run-together numbers is usually a table it '
                              'failed to see, and a short paragraph ending in "(9)" is usually a '
                              'displayed equation. Only `account` says a piece is waiting on you. '
                              'Where any of this disagrees with the page image, the page image is '
                              'the manuscript: reread_piece turns a piece into the pieces the '
                              'page really prints.')}
        if left['lost_pages']:
            answer['unconverted_pages'] = (
                'The converter returned nothing at all from page(s) '
                + ', '.join(str(one['page']) + ' (' + str(one['words']) + ' words'
                            + (', rotated ' + str(one['rotation']) + '°' if one['rotation']
                               else '') + ')' for one in left['lost_pages'])
                + '. None of what those pages print is listed above, because none of it reached '
                  'this draft. View each one and add what it prints with add_piece — the table, '
                  'the prose, the caption — after the piece it follows. Nothing else puts those '
                  'pages into the manuscript, and the draft cannot be sealed while one is empty.')
        return answer

    def view_page(self, page):
        """One page of the original PDF, as the image a repair is read from.

        With it, every piece the converter put on that page, whole. The listing
        shows a long paragraph cut off at :data:`CONTEXT_CHARACTERS`, which is
        enough to recognise a piece by and not enough to name a span inside it:
        a reader asked to cut the debris out of a 700-character paragraph could
        not see the end of the paragraph it was cutting. A page is a bounded
        amount of text, and this is the call made while looking at it.
        """
        target = pages.page_image(self.workspace, page, self.out,
                                  int(self.draft.get('page_count') or 0))
        self.pages_seen.add(int(page))
        record_view(self.draft, page)
        self.save()
        here = []
        for name, pieces in (self.draft.get('documents') or {}).items():
            for piece in pieces:
                if int(piece.get('page') or 0) != int(page):
                    continue
                here.append({'piece': piece.get('piece_id'),
                             'kind': str(piece.get('kind') or 'paragraph'),
                             'document': name, 'source': piece.get('source') or 'text',
                             'text': rendered(piece)})
        return {'label': 'Original PDF page ' + str(page), 'page': int(page),
                'pieces': here,
                'reading': ('Those are the converter\'s pieces for this page, whole, with the '
                            'text it read. A span you name in `remove` or `keep` is matched '
                            'against this, not against the page.'),
                'image': base64.b64encode(target.read_bytes()).decode()}

    def replace_equation(self, piece_id, latex, number, page):
        """Rewrite one displayed equation's LaTeX from the page image."""
        page = self.off_page(page)
        latex = ' '.join(str(latex).split())
        if not latex:
            raise ValueError('Give the equation\'s LaTeX. To keep what the converter had, '
                             'confirm_piece it instead.')
        document, index, piece = self.retype(piece_id, 'equation', page, 'replace_equation')
        piece['latex'] = latex
        piece['number'] = ' '.join(str(number).split())
        piece['text'] = latex
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
        document, index, piece = self.retype(piece_id, 'table', page, 'replace_table')
        piece['table'] = rows
        piece['text'] = ''
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
        # Only the figure is settled. The description is this call's own work,
        # not a piece a reader acted on, and settling it as `added` counted it
        # among the pieces `add_piece` put back: a manifest reported nine
        # described and nine added on a run where `add_piece` was never called.
        settle(piece, 'described', page)
        self.save()
        return {'piece': piece_id, 'description_piece': written['piece_id'], 'source': 'ai',
                'text': rendered(written), 'outstanding': outstanding(self.draft)}

    def add_piece(self, after, kind, page, text='', latex='', number='', rows=()):
        """Add a piece the converter never produced, after the one it belongs behind.

        On a page the converter returned nothing from, this is the only way the
        page reaches the manuscript at all, so a paragraph and a caption may be
        added there as well as the three kinds addable anywhere. Adding one is
        also what accounts for that page: the seal is refused while a lost page
        has nothing on it.
        """
        page = self.off_page(page)
        empty = {one['page'] for one in lost_pages(self.draft)}
        allowed = ADDABLE + ADDABLE_ON_LOST_PAGE if page in empty else ADDABLE
        if kind not in allowed:
            raise ValueError(
                'A reader adds ' + ', '.join(ADDABLE) + '. For anything else the manuscript is '
                'the authors\' and stays as converted. '
                + ('Page ' + str(page) + ' is not one of the pages the converter returned nothing '
                   'from, so its text is already in the draft.'
                   if kind in ADDABLE_ON_LOST_PAGE
                   else 'On a page the converter returned nothing from, '
                        + ' and '.join(ADDABLE_ON_LOST_PAGE) + ' may be added too.'))
        document, index, anchor = locate(self.draft, after)
        piece = shaped(kind, page, document, anchor.get('section'), 'add_piece',
                       text=text, latex=latex, number=number, rows=rows)
        piece['piece_id'] = self.next_id()
        if kind == 'figure-description':
            piece['describes'] = after
        settle(piece, 'added', page)
        self.draft['documents'][document].insert(index + 1, piece)
        self.save()
        return {'piece': piece['piece_id'], 'kind': kind, 'after': after, 'source': 'ai',
                'text': rendered(piece), 'outstanding': outstanding(self.draft)}

    def reread_piece(self, piece_id, page, parts):
        """One converted piece re-read as the pieces the page actually prints.

        The converter emits one block per thing it *thinks* it found, and when
        it is wrong about that nothing else could put it right: its table on
        one page held Table 3, Table 3's caption, all of Table 4 and Table 4's
        caption, and ``replace_table`` wrote Table 3 over the lot — the rest
        left the manuscript with nothing saying it had. So a piece may be
        replaced by several, in printed order.

        Two rules hold it to transcription. A part of one of the
        :data:`DISPLAY_KINDS` is written freely, because it is a transcription
        of something printed as a block and the converter's reading of it is
        kept beside it. A part that is prose is not written at all: it names
        the spans to cut, or the one span to keep, and the engine cuts them —
        see :func:`prose_from` — so a displayed piece's debris can leave a
        paragraph and nothing a model types can get into one.
        """
        page = self.off_page(page)
        parts = [dict(part or {}) for part in parts]
        if not parts:
            raise ValueError('Give the pieces this one is really made of, in printed order, in '
                             '`pieces`. To replace it with one equation or one table, '
                             'replace_equation and replace_table are shorter.')
        document, index, piece = locate(self.draft, piece_id)
        if is_figure(piece):
            raise ValueError(str(piece_id) + ' is the caption of a figure. Its image is what is '
                             'missing from the manuscript, not its text: describe_figure.')
        if (piece.get('read') or {}).get('action') == 'added':
            raise ValueError(str(piece_id) + ' is a piece this stage added, so there is no '
                             'converter reading under it to re-read. Withdraw it with '
                             'withdraw_piece and add what the page prints.')
        held = converter_reading(piece)
        written = []
        for at, part in enumerate(parts, 1):
            kind = str(part.get('kind') or '')
            where = 'pieces[' + str(at) + ']'
            if kind not in REREADABLE:
                raise ValueError(where + ': a piece is re-read as one of '
                                 + ', '.join(REREADABLE) + ', and not as ' + (kind or 'nothing')
                                 + '. A figure\'s image is describe_figure\'s.')
            if kind in PROSE_KINDS:
                text = prose_from(held, part, where)
            else:
                text = str(part.get('text') or '')
                if part.get('remove') or part.get('keep'):
                    raise ValueError(where + ': `remove` and `keep` are for prose, which this '
                                     'stage cuts rather than writes. A ' + kind + ' is '
                                     'transcribed from the page image, in `' +
                                     ('rows' if kind == 'table' else
                                      'latex' if kind == 'equation' else 'text') + '`.')
            made = shaped(kind, page, document, piece.get('section'), where,
                          text=text, latex=part.get('latex') or '',
                          number=part.get('number') or '', rows=part.get('rows') or [])
            made['source'] = 'ai-reconstructed'
            made['original'] = {**original_of(piece), 'piece': piece_id,
                                'part': at, 'of': len(parts)}
            if len(parts) == 1:
                made['bbox'] = piece.get('bbox')
            made['piece_id'] = self.next_id()
            settle(made, 'reread', page)
            written.append(made)
            self.draft['documents'][document].insert(index + len(written), made)
        self.draft['documents'][document] = [one for one in self.draft['documents'][document]
                                             if one.get('piece_id') != piece_id]
        self.save()
        return {'replaced': piece_id, 'pieces': [one['piece_id'] for one in written],
                'kinds': [one['kind'] for one in written], 'source': 'ai-reconstructed',
                'text': '\n'.join(rendered(one) for one in written),
                'was': original_of(piece), 'outstanding': outstanding(self.draft)}

    def withdraw_piece(self, piece_id):
        """Take out a piece this stage added, having got it wrong.

        Only this stage's own: a piece the converter produced is the authors'
        text and leaving it out is not a repair. The one that made this
        necessary was a caption added with the wrong argument, which rendered
        as ``Caption: `` and sat in the sealed manuscript with no way to
        remove it. Withdrawing a figure's description un-settles the figure, so
        the seal waits for a description again rather than going out with none.
        """
        document, index, piece = locate(self.draft, piece_id)
        action = (piece.get('read') or {}).get('action')
        describes = piece.get('describes')
        if action != 'added' and not (str(piece.get('kind')) == 'figure-description' and describes):
            raise ValueError(str(piece_id) + ' is not a piece this stage added' + (
                ', it is the converter\'s reading of the paper. The manuscript holds what the '
                'authors printed, whether or not it reads well.' if action is None else
                ' — it was ' + str(action) + '. Nothing this stage did to the authors\' own text '
                'is undone by taking the text out.') + ' To re-read it as what the page prints, '
                'reread_piece.')
        self.draft['documents'][document] = [one for one in self.draft['documents'][document]
                                             if one.get('piece_id') != piece_id]
        unsettled = ''
        if describes:
            try:
                _, _, figure = locate(self.draft, describes)
            except ValueError:
                figure = None
            if figure is not None and (figure.get('read') or {}).get('action') == 'described':
                figure.pop('read', None)
                unsettled = describes
        self.save()
        return {'withdrawn': piece_id, 'kind': str(piece.get('kind') or ''),
                'text': rendered(piece), 'unsettled': unsettled,
                'outstanding': outstanding(self.draft)}

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
