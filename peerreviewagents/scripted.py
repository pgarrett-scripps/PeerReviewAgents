"""The Read stage, driven in code, for a consumer's own tests.

An application that shows a sealed manuscript renders what the Read stage
wrote: a re-read part's ``part`` and ``of``, the ``absorbed`` reading of a
piece folded into the one it was debris of, the reader's ``note`` in the
manifest. Until this module the only folder holding those came out of a model
run, so a consumer's fixtures for them were hand-built JSON — a copy of the
engine's output shape, which drifts away from the engine the moment the engine
moves, silently, because nothing holds the copy against the code.

:func:`scripted_read` is the other way round. It opens a real run on a PDF,
claims the reader's slot, and hands back a handle whose methods are the
reader's own tools under their own names, called through
:mod:`peerreviewagents.plugin.server` — the path the Read stage's own tests
drive. Nothing is launched and no model reads anything: the script is the
reader.

    from peerreviewagents import scripted_read

    with scripted_read('paper.pdf', folder) as read:
        read.view_page(1)
        read.replace_equation(read.find('E = m c^{2'), 1, 'E = m c^{2}', number='(1)')
        read.describe_figure(read.find('Figure 1'), 1, 'A photograph of the bench …',
                             absorb=[read.find('Concentration')])
        read.seal(note='Table 2 was inside Table 1 and is put back.')
    base = read.base()        # PaperBase: `rows` and `manifest` as sealed

*pieces* is how a fixture gets the converter's failures into the draft without
a PDF that has them: each one is a piece in the shape
:func:`peerreviewagents.engine.ingest.convert_paper` writes, spliced into the
converted draft before the reference list.

**This is for tests and fixtures.** A review's Read stage is a model stage,
launched by :func:`peerreviewagents.review`, and that is what a consumer's own
users run. What this gives a consumer is a sealed folder to render in a test,
written by the engine rather than by hand.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import names
from .engine import paper_base
from .engine.ingest import DRAFT_FILE, convert_paper
from .engine.storage import write
from .plugin import server as srv

__all__ = ['scripted_read', 'ScriptedRead', 'ReaderRefused', 'PIECE']

#: The keys a converter piece carries, with what a fixture usually leaves out.
#: It mirrors the piece ``convert_paper`` writes — ``docs/INTEGRATIONS.md``
#: documents the shape — so a fixture names a kind and a text and gets the
#: rest.
PIECE = {'text': '', 'section': '', 'kind': 'paragraph', 'page': 1, 'bbox': None,
         'citations': [], 'table': None, 'source': 'text', 'page_mapping': 'converter'}


class ReaderRefused(RuntimeError):
    """A reader tool answered a refusal rather than doing the thing.

    The tools answer a refusal to the model, which keeps its turn and tries
    again. A script has no turn and nothing reads its answers, so a refusal is
    raised here instead: ``tool`` is which one, and ``answer`` is the whole
    thing the server said.
    """

    def __init__(self, tool, answer):
        self.tool = tool
        self.answer = answer
        said = answer.get('request_rejected') if isinstance(answer, dict) else None
        super().__init__(str(tool) + ': ' + str(said or answer))


class ScriptedRead:
    """One run with its Read stage open, and the reader's tools as methods.

    Every tool in :data:`peerreviewagents.plugin.server.READER_TOOLS` is a
    method here under its own name, with the agent token supplied and the
    piece ids, pages and arguments exactly as the tool takes them. A refusal
    is raised as :class:`ReaderRefused`. ``view_page`` answers its JSON
    payload alone, because the image block beside it is for a model.

    One run is open per process, which is what the server enforces, so this
    closes: leaving the ``with`` block puts back whatever run was attached
    before. The folder stays as it is, sealed, for the test to read.
    """

    def __init__(self, paper, folder, opened, token, held):
        self.paper = Path(paper)
        self.folder = Path(folder)
        self.workspace = self.folder / names.MANUSCRIPT_DIRECTORY
        self.opened = opened
        self.token = token
        self.sealed = None
        self._held = held

    # --- the tools ---------------------------------------------------------

    def call(self, tool, *arguments, **named):
        """One reader tool by name, with the token supplied. The methods are this."""
        if tool not in srv.READER_TOOLS:
            raise AttributeError(str(tool) + ' is not one of the reader\'s tools: '
                                 + ', '.join(srv.READER_TOOLS) + '.')
        answer = getattr(srv, tool)(self.token, *arguments, **named)
        if isinstance(answer, dict) and 'request_rejected' in answer:
            raise ReaderRefused(tool, answer)
        if isinstance(answer, list):
            # `view_page` answers an image block and a JSON payload. The image
            # is what a model looks at; a script gets what it can read.
            answer = json.loads(answer[-1])
        if tool == 'seal_manuscript':
            self.sealed = answer
        return answer

    def __getattr__(self, name):
        if name in srv.READER_TOOLS:
            def tool(*arguments, **named):
                return self.call(name, *arguments, **named)
            tool.__name__ = name
            tool.__doc__ = getattr(srv, name).__doc__
            return tool
        raise AttributeError(type(self).__name__ + ' has no attribute ' + repr(name))

    def seal(self, note=''):
        """Seal the manuscript, with the reader's note. :attr:`sealed` is the answer."""
        return self.call('seal_manuscript', note=note)

    # --- finding the pieces to work on -------------------------------------

    def pieces(self, document=''):
        """``{piece id: row}`` of one document of the draft, as the reader sees it."""
        return {row['piece']: row for row in self.call('read_draft', document=document)['pieces']}

    def find(self, text, document=''):
        """The id of the one piece of the draft whose text holds *text*.

        A script works on the pieces by id and the ids are minted at the
        import, so this is how a fixture names one: by a few characters of
        what the converter read there. It is an error for *text* to match
        more than one piece, or none, because a fixture that silently works on
        a different piece than it means to is worse than one that stops.
        """
        rows = self.pieces(document)
        found = [key for key, row in rows.items() if text in str(row.get('text') or '')]
        if len(found) != 1:
            raise LookupError(repr(text) + ' is in ' + str(len(found)) + ' pieces of '
                              + (document or paper_base.MAIN_DOCUMENT)
                              + ', and `find` names one. Pieces there: '
                              + ', '.join(str(row.get('text') or '')[:40]
                                          for row in rows.values()) + '.')
        return found[0]

    # --- after the seal ----------------------------------------------------

    def base(self):
        """The sealed workspace, read back as a :class:`PaperBase`.

        ``rows`` are the map rows the stage wrote and ``manifest`` holds the
        reader's note under ``ai_read``. It is
        :func:`peerreviewagents.read_manuscript` on :attr:`workspace`, which a
        consumer may call itself; this is here so a test does not have to know
        where in the run folder the workspace is.
        """
        return paper_base.load(self.workspace)

    def close(self):
        """Put back the run that was attached before this one. The folder stays."""
        srv.STATE['run'] = self._held

    def __enter__(self):
        return self

    def __exit__(self, *exception):
        self.close()
        return False


def scripted_read(paper, folder, pieces=(), *, title='', reviewers=1, arm='single', **plan):
    """Open a run on *paper* in *folder* with its Read stage claimed, for a script.

    *pieces* are extra converter pieces to put in the draft, each in the shape
    :func:`peerreviewagents.engine.ingest.convert_paper` writes and merged over
    :data:`PIECE`, so ``{'kind': 'equation', 'latex': 'E = m c^{2',
    'number': '(1)'}`` is a piece. They go in where the converter's own end
    and before the reference list, in the order given; one that names a
    ``document`` other than ``paper.md`` is appended to that document instead,
    which is how a fixture gets a supplement. This is how a fixture holds the
    failures this stage exists for — a mangled equation, a split one, a figure
    with nothing behind its caption, debris left as a heading of its own —
    without a PDF that has them.

    Anything else in *plan* is :func:`peerreviewagents.plugin.server.ingest`'s:
    the default is the smallest run there is, one core reviewer in the single
    arm, because nothing downstream of the seal runs here.

    Returns a :class:`ScriptedRead`. Use it as a context manager, or call
    :meth:`ScriptedRead.close` when the script is done.
    """
    paper, folder = Path(paper), Path(folder)
    workspace = folder / names.MANUSCRIPT_DIRECTORY
    draft = convert_paper(str(paper), workspace, title=title or None)
    for piece in pieces:
        piece = {**PIECE, **dict(piece)}
        document = str(piece.get('document') or paper_base.MAIN_DOCUMENT)
        piece['document'] = document
        held = draft['documents'].setdefault(document, [])
        if document == paper_base.MAIN_DOCUMENT:
            at = next((index for index, one in enumerate(held)
                       if str(one.get('section')) == 'References'), len(held))
            held.insert(at, piece)
        else:
            held.append(piece)
    write(workspace / DRAFT_FILE, draft)
    held = srv.STATE['run']
    srv.STATE['run'] = None
    opened = srv.ingest(str(paper), str(folder), reviewers=reviewers, arm=arm,
                        title=title, **plan)
    if 'request_rejected' in opened:
        srv.STATE['run'] = held
        raise ReaderRefused('ingest', opened)
    codes = {row['slot']: row['claim_code'] for row in opened.get('claim_codes') or []}
    taken = srv.claim_agent('reader', codes['reader'])
    if 'request_rejected' in taken:
        srv.STATE['run'] = held
        raise ReaderRefused('claim_agent', taken)
    return ScriptedRead(paper, folder, opened, taken['agent_token'], held)
