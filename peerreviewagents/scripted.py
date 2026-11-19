"""The model stages, driven in code, for a consumer's own tests.

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

:func:`scripted_fix_check` is the same thing for the other stage whose output an
application renders: the check of a new draft against the problems a review
raised. It opens a fix check on a PDF with a problems file, claims every checker
slot, and hands back a handle whose methods are the checker's own tools —
``fix_queue``, ``submit_fix_check`` and the tools that read the paper — so a
fixture holding ``fix-check.json`` and ``FIXES.md`` is written by the lane
rather than by hand::

    from peerreviewagents import read_fix_check, scripted_fix_check

    with scripted_fix_check('paper-v2.pdf', folder, problems) as check:
        check.submit_fix_check('i01', 'fixed', quote=check.line(14),
                               note='The abstract now reports 24%.')
        check.submit_fix_check('i02', 'cannot-tell',
                               reason='The repeated experiment is not in this draft.')
        check.write()
    reading = read_fix_check(folder)

:func:`scripted_review` is the third, and the one a consumer rendering a review
needs: a finished review folder — ``issues.json``, ``REVIEW.md``, the findings
and the editor's report that :func:`peerreviewagents.read_run` reads — written
by the engine with no model anywhere in it. It claims every slot the arm plans
and the recorder, and the script is each of them in turn::

    from peerreviewagents import read_run, scripted_review

    with scripted_review('paper.pdf', folder, arm='reviewers-only', reviewers=1) as review:
        stored = review.submit_finding(
            slot='core-1', title='The abstract overstates the result',
            line=14, quote=review.line(14, slot='core-1'),
            comment='The sentence asserts more than the measurement shows.',
            severity='major')
        review.submit_report('What I checked and what I raised.', slot='core-1')
        review.submit_issues([{...'members': [stored['finding_uid']]}])   # the editor's
        review.submit_editor(summary='…', significance='…')
        review.submit_report('The pooled review is above.', slot='editor')
        review.write()
    reading = read_run(folder)

**This is for tests and fixtures.** A review's Read stage is a model stage,
launched by :func:`peerreviewagents.review`, and a fix check is a model run
launched by :func:`peerreviewagents.check_fixes`; those are what a consumer's
own users run. What this gives a consumer is a folder to render in a test,
written by the engine rather than by hand.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import names
from . import problems as problem_list
from .engine import paper_base, paper_view
from .engine.ingest import DRAFT_FILE, convert_paper
from .engine.storage import write
from .plugin import server as srv

__all__ = ['scripted_read', 'ScriptedRead', 'ReaderRefused', 'PIECE',
           'scripted_fix_check', 'ScriptedFixCheck', 'CheckRefused',
           'scripted_review', 'ScriptedReview', 'ReviewRefused', 'ToolRefused',
           'SCRIPTED_SEAL']

#: The keys a converter piece carries, with what a fixture usually leaves out.
#: It mirrors the piece ``convert_paper`` writes — ``docs/INTEGRATIONS.md``
#: documents the shape — so a fixture names a kind and a text and gets the
#: rest.
PIECE = {'text': '', 'section': '', 'kind': 'paragraph', 'page': 1, 'bbox': None,
         'citations': [], 'table': None, 'source': 'text', 'page_mapping': 'converter'}


#: Why the manuscript of a scripted check or review is sealed without a reader.
#: Both quote the lines of the paper, and there are no lines until the
#: manuscript is sealed; no model runs here, so it is sealed the one other way a
#: run can seal it — the recorder's ``seal_as_converted``, the path a run takes
#: when its reader failed. A fixture that wants a *read* manuscript instead
#: builds one with :func:`scripted_read`.
SCRIPTED_SEAL = ('no reader ran: a script drove this run, so the manuscript is the '
                 'converter\'s reading of the paper')

#: What a slot may submit, by the role the server resolved it to, beside the
#: tools ``claim_agent`` grants it. A submission is not an investigation call —
#: it spends no allowance and is rationed by nothing — so the server does not
#: list these back with the reading tools; the lane's own brief tells each slot
#: what to submit. A script has no brief, so a handle's methods come from here
#: as well, which is also what makes a tool one slot's alone: the editor is the
#: only role that submits issues, and the recorder the only one that writes.
SUBMISSIONS = {'core': ('submit_finding', 'submit_report'),
               'specialist': ('submit_finding', 'submit_report'),
               'editor': ('list_findings', 'submit_issues', 'submit_editor', 'submit_report'),
               'recorder': ('write_review',)}


class ToolRefused(RuntimeError):
    """A server tool answered a refusal rather than doing the thing.

    The tools answer a refusal to the model, which keeps its turn and tries
    again. A script has no turn and nothing reads its answers, so a refusal is
    raised here instead: ``tool`` is which one, and ``answer`` is the whole
    thing the server said.

    :class:`ReaderRefused` is the Read stage's, :class:`CheckRefused` the fix
    check's and :class:`ReviewRefused` the review's, so a test may catch either
    the stage it drives or all of them.
    """

    def __init__(self, tool, answer):
        self.tool = tool
        self.answer = answer
        said = answer.get('request_rejected') if isinstance(answer, dict) else None
        super().__init__(str(tool) + ': ' + str(said or answer))


class ReaderRefused(ToolRefused):
    """A reader tool, or the ingest behind it, answered a refusal."""


class CheckRefused(ToolRefused):
    """A fix-check tool, or the ingest, seal or write behind it, answered a refusal."""


class ReviewRefused(ToolRefused):
    """A review tool, or the ingest, seal or write behind it, answered a refusal."""


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


class _ScriptedSlots:
    """What a handle with several claimed slots is, for the two that have them.

    A fix check and a review are the same shape to a script: a run is open, its
    slots are claimed, and every tool those slots were granted is a method here
    under its own name, with the agent token supplied and the arguments exactly
    as the tool takes them. ``slot=`` says which slot calls; what it means when
    a caller leaves it out is :meth:`slot_for`, which is the one thing the two
    stages answer differently. A refusal is raised as :attr:`refusal`.

    One run is open per process, which is what the server enforces, so this
    closes: leaving the ``with`` block puts back whatever run was attached
    before. The folder stays as it is, with whatever :meth:`write` wrote in it,
    for the test to read.
    """

    #: Which :class:`ToolRefused` a refused tool is raised as.
    refusal = ToolRefused

    def __init__(self, paper, folder, opened, claims, held):
        self.paper = Path(paper)
        self.folder = Path(folder)
        self.workspace = self.folder / names.MANUSCRIPT_DIRECTORY
        self.opened = opened
        self.claims = dict(claims)
        self.recorder_key = str(opened.get('recorder_key') or '')
        self.written = None
        self.tokens = {slot: taken['agent_token'] for slot, taken in self.claims.items()}
        #: ``{slot: every tool it may call}`` — what the server granted it, and
        #: then what its role may submit, which the grants leave out because a
        #: submission spends no allowance. See :data:`SUBMISSIONS`.
        self.granted = {slot: (*(taken.get('allowed_tools') or ()),
                               *SUBMISSIONS.get(str(taken.get('role') or ''), ()))
                        for slot, taken in self.claims.items()}
        self._tools = tuple(dict.fromkeys(name for granted in self.granted.values()
                                          for name in granted))
        self._held = held

    # --- the slots ---------------------------------------------------------

    @property
    def slots(self):
        """The slots this run planned and this handle holds, in the order claimed."""
        return tuple(self.claims)

    @property
    def tools(self):
        """Every tool these slots were granted, as the server granted them."""
        return self._tools

    def slot_for(self, tool):
        """Which slot calls *tool* when the caller names none: here, the first."""
        return self.slots[0] if self.slots else ''

    def token(self, slot=''):
        """The agent token of one slot. The default is the first."""
        name = str(slot or '') or (self.slots[0] if self.slots else '')
        if name not in self.tokens:
            raise LookupError(repr(name) + ' is not a slot of this run. The slots are: '
                              + (', '.join(self.slots) or 'none') + '.')
        return self.tokens[name]

    # --- the tools ---------------------------------------------------------

    def call(self, tool, *arguments, slot='', **named):
        """One tool by name, with the token supplied. The methods are this."""
        if tool not in self._tools:
            raise AttributeError(str(tool) + ' is not one of these slots\' tools: '
                                 + ', '.join(self._tools) + '.')
        answer = getattr(srv, tool)(self.token(slot or self.slot_for(tool)),
                                    *arguments, **named)
        if isinstance(answer, dict) and 'request_rejected' in answer:
            raise self.refusal(tool, answer)
        if isinstance(answer, list):
            # `view_page` answers an image block and a JSON payload. The image
            # is what a model looks at; a script gets what it can read.
            answer = json.loads(answer[-1])
        return answer

    def __getattr__(self, name):
        tools = self.__dict__.get('_tools') or ()
        if name in tools:
            def tool(*arguments, slot='', **named):
                return self.call(name, *arguments, slot=slot, **named)
            tool.__name__ = name
            tool.__doc__ = getattr(srv, name).__doc__
            return tool
        raise AttributeError(type(self).__name__ + ' has no attribute ' + repr(name))

    # --- reading the paper -------------------------------------------------

    def line(self, number, document='', slot=''):
        """One line of this manuscript, by number, as ``read_paper`` serves it.

        A finding and a verdict are both anchored by a passage matched against
        the manuscript, so this is where a script takes one from:
        ``quote=handle.line(14)`` quotes line 14 exactly. It reads the run's own
        base, so it works while the run is open, and *slot* is ignored — the
        lines are the manuscript's, not a slot's.
        """
        base = srv.current().base
        if base is None:
            raise LookupError('This manuscript is not sealed, so it has no lines yet.')
        return base.line_text(int(number), document or paper_base.MAIN_DOCUMENT)

    def served(self, document='', slot=''):
        """Every part of one document, as ``read_paper`` served it, in one string."""
        base = srv.current().base
        parts = paper_view.served(base, document or paper_base.MAIN_DOCUMENT)
        return '\n'.join(str(self.call('read_paper', slot=slot, document=document, part=part))
                         for part in range(1, parts + 1))

    def base(self):
        """The manuscript of this run, read back as a :class:`PaperBase`."""
        return paper_base.load(self.workspace)

    def close(self):
        """Put back the run that was attached before this one. The folder stays."""
        srv.STATE['run'] = self._held

    def __enter__(self):
        return self

    def __exit__(self, *exception):
        self.close()
        return False


class ScriptedFixCheck(_ScriptedSlots):
    """One fix check open, with the checker's own tools as methods.

    Every tool a checker slot was granted — ``fix_queue``, ``submit_fix_check``
    and the tools that read the paper — is a method here under its own name,
    with the agent token supplied and the arguments exactly as the tool takes
    them. A refusal is raised as :class:`CheckRefused`.

    The problems are split between the checkers by the server, so a script does
    not have to know which slot holds which: :meth:`submit_fix_check` routes a
    verdict to the slot whose batch holds that problem, and *slot* is there for
    a test that means to send one to the wrong slot. :attr:`slots` names them
    all and :meth:`fix_queue` serves one slot's batch.

    One run is open per process, which is what the server enforces, so this
    closes: leaving the ``with`` block puts back whatever run was attached
    before. The folder stays as it is, with whatever :meth:`write` wrote in it,
    for the test to read.
    """

    refusal = CheckRefused

    def __init__(self, paper, folder, opened, claims, batches, problems, held):
        super().__init__(paper, folder, opened, claims, held)
        #: ``{problem id: slot}`` — which checker holds which problem, as the
        #: server split them. A problem past the check's cap is in no batch and
        #: so is in no entry here.
        self.batches = dict(batches)
        #: The problems this check was given, as the run holds them.
        self.problems = list(problems)

    # --- the slots ---------------------------------------------------------

    def slot_of(self, problem_id):
        """Which checker slot holds *problem_id*, as the server split the batches."""
        name = self.batches.get(str(problem_id).strip())
        if name is None:
            raise LookupError('No checker of this run holds problem '
                              + (str(problem_id).strip() or '(none given)')
                              + '. Either no problem has that id, or it is past the cap of '
                              + str(srv.MAX_FIX_CHECKS) + ' and is nobody\'s to check: the '
                              'written check counts those as not checked.')
        return name

    # --- the tools ---------------------------------------------------------

    def fix_queue(self, slot=''):
        """The problems one checker slot is still to answer, as the tool serves them."""
        return self.call('fix_queue', slot=slot)

    def submit_fix_check(self, problem_id, verdict, *arguments, slot='', **named):
        """One verdict on one problem, from the slot whose batch holds it.

        The arguments are ``submit_fix_check``'s own — ``quote`` and
        ``document`` and ``line`` for a verdict that rests on a passage of this
        draft, ``searches`` for one that rests on the old quotation being gone
        or still standing, ``reason`` for ``cannot-tell``, and ``note`` for the
        author. *slot* overrides the routing, which is how a test sends a
        verdict to the checker that does not hold that problem.
        """
        return self.call('submit_fix_check', problem_id, verdict, *arguments,
                         slot=slot or self.slot_of(problem_id), **named)

    # --- writing the check -------------------------------------------------

    def write(self):
        """Write the check — ``fix-check.json`` and ``FIXES.md`` — as the recorder.

        The server's answer, with the counts it wrote and the paths it wrote
        them to, is returned and kept on :attr:`written`. Written once, like a
        review: a second call raises :class:`CheckRefused`.
        """
        answer = srv.write_fix_check(self.recorder_key)
        if 'request_rejected' in answer:
            raise CheckRefused('write_fix_check', answer)
        self.written = answer
        return answer

    def read(self):
        """The written check, read back with :func:`peerreviewagents.read_fix_check`."""
        from .runs import read_fix_check  # Local: `runs` is the reader of what this writes.

        return read_fix_check(self.folder)


def scripted_fix_check(paper, folder, problems, *, title='', **plan):
    """Open a fix check on *paper* against *problems* in *folder*, for a script.

    *problems* is the ``--problems`` file: a path to one, or the document
    itself — a list of problem rows or an object with ``problems`` — which is
    written to ``<folder>/problems.json`` and read back from there, so a test
    does not have to write the file itself. Either way the ingest validates it
    against :mod:`peerreviewagents.problems`, so a row the check cannot look
    for raises :class:`CheckRefused` naming every reason rather than being
    checked against nothing.

    The manuscript is sealed as the converter read it, with
    :data:`SCRIPTED_SEAL` as the reason: a check quotes the lines of the new
    draft and there are no lines until the manuscript is sealed, and no model
    runs here to read one. Every checker slot the run planned is then claimed
    and served the whole manuscript, which is what the lane tells a checker to
    do first.

    Anything else in *plan* is :func:`peerreviewagents.plugin.server.ingest`'s.
    How many checkers there are is not: the server plans one per batch of
    problems, up to its own cap.

    Returns a :class:`ScriptedFixCheck`. Use it as a context manager, or call
    :meth:`ScriptedFixCheck.close` when the script is done.
    """
    paper, folder = Path(paper), Path(folder)
    if isinstance(problems, (str, Path)):
        path = Path(problems)
    else:
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / 'problems.json'
        path.write_text(json.dumps(problem_list.normalize(problems), indent=2) + '\n',
                        encoding='utf-8')
    opened, run, claims, held, refuse = _open(
        paper, folder, CheckRefused, lambda one: one.checker_slots,
        arm=srv.FIX_ARM, problems=str(path), title=title, **plan)
    batches = {}
    for slot, taken in claims.items():
        queued = srv.fix_queue(taken['agent_token'])
        if 'request_rejected' in queued:
            raise refuse('fix_queue', queued)
        for row in queued['problems']:
            batches[str(row['id'])] = slot
    return ScriptedFixCheck(paper, folder, opened, claims, batches, run.problems, held)


class ScriptedReview(_ScriptedSlots):
    """One review open, with every slot's own tools as methods.

    A review is written by many hands, so this holds them all: every slot the
    arm planned — the core reviewers, the specialist tracks, the revision
    reader, the editor — and the recorder, each claimed and served the whole
    manuscript. Every tool those slots were granted is a method here under its
    own name, and ``slot=`` says who calls::

        review.submit_finding(slot='core-1', title=…, line=14, quote=…,
                              comment=…, severity='major')
        review.submit_report('What I checked.', slot='core-1')

    A tool only one slot holds needs no ``slot``: ``submit_issues``,
    ``submit_editor`` and ``list_findings`` are the editor's alone, so they go
    there by themselves. A tool several slots hold — ``submit_finding``,
    ``submit_report``, ``read_paper`` — raises :class:`LookupError` naming them
    if the caller does not say which, because a finding filed by the wrong
    reviewer is a review that passes its tests and says the wrong thing.

    What the script still has to do is the judgement no handle can supply: the
    editor groups every finding into issues, ranks them with no gaps or ties,
    and rules on each escalation. :meth:`write` is the recorder's
    ``write_review``, which refuses until every stage the arm requires has
    reported.

    One run is open per process, which is what the server enforces, so this
    closes: leaving the ``with`` block puts back whatever run was attached
    before. The folder stays as it is, written, for the test to read.
    """

    refusal = ReviewRefused

    def slot_for(self, tool):
        """The one slot granted *tool*, or :class:`LookupError` naming the several."""
        holders = [slot for slot, granted in self.granted.items() if tool in granted]
        if len(holders) == 1:
            return holders[0]
        return self._ambiguous(tool, holders)

    def _ambiguous(self, tool, holders):
        raise LookupError(str(tool) + ' is a tool of ' + str(len(holders)) + ' slots of this '
                          'review, so say which one calls it'
                          + (': slot=' + repr(holders[0]) if holders else '')
                          + '. The slots holding it: ' + (', '.join(holders) or 'none') + '.')

    # --- writing the review ------------------------------------------------

    def write(self):
        """Write the review — ``issues.json``, ``REVIEW.md``, … — as the recorder.

        The server's answer, with the status and the counts it wrote, is
        returned and kept on :attr:`written`. Written once: a second call
        raises :class:`ReviewRefused`, as does a review a stage has not
        reported on yet.
        """
        answer = srv.write_review(self.token('recorder'))
        if 'request_rejected' in answer:
            raise ReviewRefused('write_review', answer)
        self.written = answer
        return answer

    def read(self):
        """The written review, read back with :func:`peerreviewagents.read_run`."""
        from .runs import read_run  # Local: `runs` is the reader of what this writes.

        return read_run(self.folder)


def scripted_review(paper, folder, *, title='', reviewers=1, arm='reviewers-only', **plan):
    """Open a review on *paper* in *folder* with every slot claimed, for a script.

    The manuscript is sealed as the converter read it, with
    :data:`SCRIPTED_SEAL` as the reason — a finding quotes the lines of the
    paper and there are none until the manuscript is sealed, and no model runs
    here to read one. Every slot the arm planned is then claimed and served the
    whole manuscript, which is what the lane tells a reviewer to do first, and
    so is the recorder, whose token :meth:`ScriptedReview.write` writes with. A
    fixture that wants a *read* manuscript under the review builds one with
    :func:`scripted_read` first and keeps that folder.

    *arm* and *reviewers* are the plan, and anything else in *plan* is
    :func:`peerreviewagents.plugin.server.ingest`'s — ``tracks``, ``code``,
    ``revision``. The default is the smallest review that writes a whole
    folder: one core reviewer and the editor, no specialists, so a script that
    only wants a rendered review has two slots to act as rather than nine.

    Returns a :class:`ScriptedReview`. Use it as a context manager, or call
    :meth:`ScriptedReview.close` when the script is done.
    """
    paper, folder = Path(paper), Path(folder)
    opened, run, claims, held, _ = _open(
        paper, folder, ReviewRefused, lambda one: (*one.required, 'recorder'),
        arm=arm, reviewers=reviewers, title=title, **plan)
    return ScriptedReview(paper, folder, opened, claims, held)


def _open(paper, folder, refusal, slots_of, **plan):
    """Ingest, seal as the converter read it, claim the slots, serve the paper.

    What the two multi-slot handles share: the run held and put back, the
    refusals raised as *refusal* rather than answered, and every slot
    *slots_of* names claimed with its own code and served the whole manuscript
    if it was granted ``read_paper``. Answers
    ``(opened, run, claims, held, refuse)``, where ``refuse`` builds a refusal
    and puts the held run back first.
    """
    held = srv.STATE['run']
    srv.STATE['run'] = None

    def refuse(tool, answer):
        srv.STATE['run'] = held
        return refusal(tool, answer)

    opened = srv.ingest(str(paper), str(folder), **plan)
    if 'request_rejected' in opened:
        raise refuse('ingest', opened)
    run = srv.current()
    if run.base is None:
        sealed = srv.seal_as_converted(opened.get('recorder_key'), reason=SCRIPTED_SEAL)
        if 'request_rejected' in sealed:
            raise refuse('seal_as_converted', sealed)
    codes = {row['slot']: row['claim_code'] for row in opened.get('claim_codes') or []}
    claims = {}
    for slot in slots_of(run):
        taken = srv.claim_agent(slot, codes.get(slot, ''))
        if 'request_rejected' in taken:
            raise refuse('claim_agent', taken)
        claims[slot] = taken
        if 'read_paper' in (taken.get('allowed_tools') or ()):
            for part in range(1, paper_view.served(run.base) + 1):
                srv.read_paper(taken['agent_token'], part=part)
    return opened, run, claims, held, refuse
