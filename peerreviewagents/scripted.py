"""The model stages, driven in code, for a consumer's own tests.

An application that shows a sealed manuscript renders what the Read stage
wrote: a re-read part's ``part`` and ``of``, the ``absorbed`` reading of a
piece folded into the one it was debris of, the reader's ``note`` in the
manifest. Until this module the only folder holding those came out of a model
run, so a consumer's fixtures for them were hand-built JSON — a copy of the
engine's output shape, which drifts away from the engine the moment the engine
moves, silently, because nothing holds the copy against the code.

These three functions are the other way round. Each one runs a *real* run —
the orchestrator's own stage order, the real tool server, the real recorder,
the real run folder — with Python in the seat the model sits in. Nothing is
launched and no model reads anything: a script is the agent.

    from peerreviewagents import scripted_read

    read = scripted_read('paper.pdf', folder, pieces=CONVERTER_LEFT)
    base = read.base()        # PaperBase: `rows` and `manifest` as sealed

That call ran the whole Read stage with this module's default reader script,
which views every page, accounts for every outstanding piece and seals. A
fixture that wants something else of the reader passes its own script, which
is one function taking the slot it is running as::

    def reader(slot):
        slot.view_page(1)
        slot.replace_equation(slot.find('E = m c^{2'), 1, 'E = m c^{2}', number='(1)')
        slot.describe_figure(slot.find('Figure 1'), 1, 'A photograph of the bench …',
                             absorb=[slot.find('Concentration')])
        slot.seal(note='Table 2 was inside Table 1 and is put back.')

    read = scripted_read('paper.pdf', folder, reader, pieces=CONVERTER_LEFT)

The slot's methods are that agent's own tools under their own names, with the
token supplied, and it is confined to the tools its own agent definition
grants: anything else raises
:class:`~peerreviewagents.plugin.runner.NotGranted`, as it would for a model
whose prompt named a tool it does not have. A tool that answers a refusal
raises it — :class:`ReaderRefused`, :class:`CheckRefused`,
:class:`ReviewRefused` — because a script has no turn to read an answer in.

*pieces* is how a fixture gets the converter's failures into the draft without
a PDF that has them: each one is a piece in the shape
:func:`peerreviewagents.engine.ingest.convert_paper` writes, spliced into the
converted draft before the reference list.

:func:`scripted_fix_check` is the same for the other stage whose output an
application renders: the check of a new draft against the problems a review
raised. Every lane gets its own queue, and the default checker answers every
problem in it::

    from peerreviewagents import read_fix_check, scripted_fix_check

    def checker(slot):
        for problem in slot.fix_queue()['problems']:
            slot.submit_fix_check(problem['id'], 'cannot-tell',
                                  reason='The repeated experiment is not in this draft.')

    check = scripted_fix_check('paper-v2.pdf', folder, problems, {'fix-check': checker})
    reading = read_fix_check(folder)

:func:`scripted_review` is the third, and the one a consumer rendering a review
needs: a finished review folder — ``issues.json``, ``REVIEW.md``, the findings
and the editor's report that :func:`peerreviewagents.read_run` reads — written
by the engine with no model anywhere in it::

    from peerreviewagents import read_run, scripted_review

    def reviewer(slot):
        line, quote = slot.anchor()
        slot.submit_finding(title='The abstract overstates the result',
                            line=line, quote=quote, severity='major',
                            comment='The sentence asserts more than the measurement shows.')
        slot.submit_report('What I checked and what I raised.')

    review = scripted_review('paper.pdf', folder, {'core': reviewer}, reviewers=1)
    reading = read_run(folder)

Scripts are keyed by slot (``core-1``), by the agent definition's name
(``core-reviewer``), by the role the server resolved the slot to (``core``) or
by ``'*'`` for every slot, in that order of preference; a slot no script names
runs this module's default for its role, which is :data:`DEFAULT_SCRIPTS`. So a
one-line call writes a whole folder, and a script is written only for the slot
a fixture cares about.

What a script returns is normally nothing: the stage's answer is read off what
the script did — the seal it took, the findings it stored, the reports it filed
— so a script cannot close a stage it did not do the work for. A dict is merged
over that answer for a fixture that means to say something else, and
:data:`NO_ANSWER` stands in for an agent that did the work and then said
nothing, which is the one thing a scripted run cannot do by returning ``None``.

**This is for tests and fixtures.** A review is a model run launched by
:func:`peerreviewagents.review` and a fix check one launched by
:func:`peerreviewagents.check_fixes`; those are what a consumer's own users
run. What this gives a consumer is a folder of exactly that shape — runtime
rows, token fields, the recorder's receipt, the tool-grant boundary — written
by the engine rather than by hand.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from . import issues as issue_model
from . import names
from . import problems as problem_list
from .engine import paper_base, paper_view
from .engine.ingest import DRAFT_FILE, convert_paper
from .engine.storage import write
from .plugin import runner as agent_runner
from .plugin import server as srv
from .plugin import starter

__all__ = ['scripted_read', 'ScriptedRead', 'ReaderRefused', 'PIECE',
           'scripted_fix_check', 'ScriptedFixCheck', 'CheckRefused',
           'scripted_review', 'ScriptedReview', 'ReviewRefused', 'ToolRefused',
           'ScriptedSlot', 'DEFAULT_SCRIPTS', 'NO_ANSWER']

#: The keys a converter piece carries, with what a fixture usually leaves out.
#: It mirrors the piece ``convert_paper`` writes — ``docs/INTEGRATIONS.md``
#: documents the shape — so a fixture names a kind and a text and gets the
#: rest.
PIECE = {'text': '', 'section': '', 'kind': 'paragraph', 'page': 1, 'bbox': None,
         'citations': [], 'table': None, 'source': 'text', 'page_mapping': 'converter'}

#: What a script returns to stand in for an agent that worked and then answered
#: nothing. Returning ``None`` cannot mean that: almost every script returns
#: ``None`` — the stage's answer is read off the calls it made — so the one
#: outcome a scripted run could not otherwise produce gets a name of its own.
#: A stage that answers nothing is retried once and then blocks the run, which
#: is the behaviour a fixture of a *blocked* folder needs.
NO_ANSWER = 'no answer'

#: The claim code a brief hands its agent. A script does not have to find it —
#: the seat claims the slot before the script runs — but it is read the way the
#: model reads it, out of the prompt, so a brief that stopped carrying one
#: would fail here rather than quietly claiming nothing.
CLAIM_CODE = re.compile(r'claim_code="([^"]*)"')

#: What the default reader writes where a figure has no description, and the
#: note it seals with. Both are placeholders with the shape of the real thing:
#: long enough and specific enough that the server stores them.
FIGURE_DESCRIPTION = ('A photograph of the bench with the instrument clamped at the left and '
                      'three widgets in a row; the scale divisions are 0.5 mm apart.')
READER_NOTE = 'A script read this draft: every page was viewed and every piece accounted for.'


class ToolRefused(RuntimeError):
    """A server tool answered a refusal rather than doing the thing.

    The tools answer a refusal to the model, which keeps its turn and tries
    again. A script has no turn and nothing reads its answers, so a refusal is
    raised here instead: ``tool`` is which one, and ``answer`` is the whole
    thing the server said.

    It is raised inside the script, where the call was made. A script that does
    not catch it dies as a model that crashed would — the stage gets nothing —
    and the helper raises it again when the run is over, so a test sees the
    refusal and not only a blocked folder.

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
    """A fix-check tool, or the ingest or write behind it, answered a refusal."""


class ReviewRefused(ToolRefused):
    """A review tool, or the ingest or write behind it, answered a refusal."""


class ScriptedSlot:
    """One agent of a scripted run, with its own tools as methods.

    This is what a script is handed. Every tool the slot's own agent definition
    grants is a method here under its own name, with the agent token supplied
    and the arguments exactly as the tool takes them::

        slot.submit_finding(title=…, line=14, quote=…, comment=…, severity='major')
        slot.submit_report('What I read and what I raised.')

    The boundary is the agent's own: a tool the definition does not grant
    raises :class:`~peerreviewagents.plugin.runner.NotGranted` and one the
    server does not have raises :class:`AttributeError`, which is what a
    scripted panel is for. A tool that answers a refusal raises
    :attr:`refusal`. ``view_page`` answers its JSON payload alone, because the
    image block beside it is for a model.

    Beside the tools there is sugar a script would otherwise write itself:
    :meth:`find` and :meth:`pieces` for the reader's draft, :meth:`anchor` and
    :meth:`line` for a passage of the sealed manuscript to quote,
    :meth:`whole_paper` for the read every reviewing brief opens with. Nothing
    here reaches past the slot's own grants.

    Every call is kept on :attr:`calls`, which is where the stage's answer
    comes from: a script that filed two findings and closed its slot answered
    ``findings_stored: 2`` whether it said so or not.
    """

    def __init__(self, tools, task, token, role, index=0, refusal=ToolRefused):
        #: The slot this agent is: ``core-1``, from a label of ``core-1#2``.
        self.slot = str(task.label).split('#')[0]
        #: Which attempt at that slot this is, counting from 1.
        self.attempt = int(str(task.label).split('#')[1]) if '#' in str(task.label) else 1
        #: The role the server resolved this slot to: ``core``, ``editor``,
        #: ``reader``, ``specialist``, ``fix-check``, ``code-check``,
        #: ``citation-worker``.
        self.role = str(role or '')
        #: Where in the run this slot was claimed, counting from 0. The default
        #: reviewer anchors its finding at this index, so two reviewers of the
        #: same run quote two different passages.
        self.index = int(index)
        #: The agent token, for a call made through something other than this.
        self.token = token
        #: The model this slot was asked to run, for a report that records it.
        self.model = str(task.model or '')
        #: The task the orchestrator built: the brief, the schema, the grants.
        self.task = task
        #: Which :class:`ToolRefused` a refused tool raises.
        self.refusal = refusal
        #: One row per call: ``tool``, ``arguments``, ``keywords``, ``answer``.
        self.calls = []
        #: What this seat says it spent, from :meth:`spend`, or ``None`` for a
        #: seat that said nothing and takes the run's flat number.
        self.tokens = None
        self._tools = tools

    def __repr__(self):
        return f'<{type(self).__name__} {self.slot} ({self.role}), {len(self.calls)} call(s)>'

    # --- the tools ---------------------------------------------------------

    @property
    def granted(self):
        """Every tool this agent's own definition grants it, as it was granted."""
        return tuple(self.task.tools)

    def call(self, tool, *arguments, **named):
        """One of this slot's tools by name, with the token supplied.

        The methods are this. A refusal raises :attr:`refusal`; a tool the
        definition does not grant raises
        :class:`~peerreviewagents.plugin.runner.NotGranted`.
        """
        answer = getattr(self._tools, tool)(self.token, *arguments, **named)
        if isinstance(answer, list):
            # `view_page` answers an image block and a JSON payload. The image
            # is what a model looks at; a script gets what it can read.
            answer = json.loads(answer[-1])
        self.calls.append({'tool': tool, 'arguments': arguments, 'keywords': named,
                           'answer': answer})
        if isinstance(answer, dict) and 'request_rejected' in answer:
            raise self.refusal(tool, answer)
        return answer

    def __getattr__(self, name):
        if name.startswith('_'):
            raise AttributeError(name)
        tools = self.__dict__.get('_tools')
        if tools is None:
            raise AttributeError(name)
        getattr(tools, name)  # The grant boundary, asked before anything is called.

        def tool(*arguments, **named):
            return self.call(name, *arguments, **named)

        tool.__name__ = name
        tool.__doc__ = getattr(srv, name, None).__doc__ if hasattr(srv, name) else None
        return tool

    # --- what this slot did ------------------------------------------------

    def answers(self, tool):
        """Every answer this slot got from *tool*, in the order it called it."""
        return [row['answer'] for row in self.calls if row['tool'] == tool]

    def last(self, tool):
        """The last answer *tool* gave this slot, or ``None`` if it never called it."""
        found = self.answers(tool)
        return found[-1] if found else None

    def count(self, tool):
        """How many times this slot called *tool*."""
        return sum(1 for row in self.calls if row['tool'] == tool)

    def given(self, tool):
        """``(arguments, keywords)`` of every call this slot made to *tool*."""
        return [(row['arguments'], row['keywords']) for row in self.calls
                if row['tool'] == tool]

    # --- the reader's draft ------------------------------------------------

    def pieces(self, document=''):
        """``{piece id: row}`` of one document of the draft, as the reader sees it.

        ``read_draft`` is served a part at a time, as a long paper's draft is
        more than one turn's reading; this is every part of one document.
        """
        first = self.call('read_draft', document=document)
        rows = {row['piece']: row for row in first['pieces']}
        for part in range(2, int(first.get('parts') or 1) + 1):
            more = self.call('read_draft', document=document, part=part)
            rows.update({row['piece']: row for row in more['pieces']})
        return rows

    def outstanding(self, document=''):
        """What the draft still needs a reader for, as ``read_draft`` reports it."""
        return self.call('read_draft', document=document)['outstanding']

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

    def seal(self, note=''):
        """Seal the manuscript, with the reader's note. ``seal_manuscript``, named."""
        return self.call('seal_manuscript', note=note)

    # --- what this seat spent ----------------------------------------------

    def spend(self, tokens):
        """Say what this seat spent, in tokens, in place of the run's flat number.

        A scripted seat has no session to meter, so it declares its own spend.
        The panel handed this seat a ceiling out of the plan's
        ``per_run_tokens`` and ``per_agent_tokens``, and a seat that declares
        more than its ceiling ends over the ceiling with its answer dropped,
        exactly as a real session's child does when it is cut off — which is
        how a consumer builds a run stopped over a ceiling without a model::

            def greedy(slot):
                slot.spend(2_000_000)
                return scripted.NO_ANSWER   # never reached by the panel

        The ceiling itself is on the task, as ``slot.task.token_ceiling``, so a
        script can also spend just under it.
        """
        self.tokens = max(0, int(tokens or 0))
        return self.tokens

    # --- the sealed manuscript ---------------------------------------------

    def whole_paper(self, document=''):
        """Read every part of one document, as every reading brief says to first.

        Every core, specialist and editor seat calls this before it submits:
        the server refuses a submission from a seat that has not been served
        every part of the manuscript, by the same rule a model meets, and the
        refusal names ``read_paper`` — which is this, part by part.
        """
        parts = paper_view.served(self.base(), document or paper_base.MAIN_DOCUMENT)
        return '\n'.join(str(self.call('read_paper', document=document, part=part))
                         for part in range(1, parts + 1))

    # Kept under the name the handle used to carry, because a consumer's script
    # reads the paper and then quotes it, and `served` is what that was called.
    served = whole_paper

    def read_paper(self, document='', part=None):
        """The tool under its own name: one part, or every part with no ``part``.

        A seat that has not been served the whole manuscript is refused when it
        submits, and the refusal tells it to call ``read_paper`` — so that name
        works here, and with no ``part`` it is :meth:`whole_paper`, which is
        what the refusal is asking for.
        """
        if part is None:
            return self.whole_paper(document)
        return self.call('read_paper', document=document, part=part)

    def line(self, number, document=''):
        """One line of this manuscript, by number, as ``read_paper`` serves it.

        A finding and a verdict are both anchored by a passage matched against
        the manuscript, so this is where a script takes one from:
        ``quote=slot.line(14)`` quotes line 14 exactly.
        """
        return self.base().line_text(int(number), document or paper_base.MAIN_DOCUMENT)

    def anchor(self, index=0):
        """A passage of this manuscript that occurs once, and its line.

        ``(line, quote)``, or ``None`` where this manuscript has no *index*-th
        such passage. The server refuses a finding whose quote it cannot place,
        or places twice, so this is how a script quotes a real paper it has
        never seen: ``line, quote = slot.anchor()``.
        """
        base = self.base()
        text = base.text
        found = 0
        for number in range(1, base.line_count() + 1):
            for piece in base.line_text(number).split('. '):
                piece = piece.strip()
                if 30 < len(piece) < 160 and text.count(piece) == 1:
                    if found == index:
                        return number, piece
                    found += 1
                    break
        return None

    def base(self):
        """This run's manuscript as a :class:`PaperBase`, or ``None`` before the seal."""
        return srv.current().base


# -- the default scripts ----------------------------------------------------


def read_stage(slot):
    """The reader: view every page, account for every outstanding piece, seal."""
    draft = slot.read_draft()
    for page in range(1, int(draft.get('page_count') or 0) + 1):
        slot.view_page(page)
    left = slot.outstanding()
    for row in left['pieces']:
        if row['kind'] == 'figure':
            slot.describe_figure(row['piece'], row['page'], FIGURE_DESCRIPTION)
        else:
            slot.confirm_piece(row['piece'], row['page'])
    slot.seal(note=READER_NOTE)


def reviewer(slot):
    """One reviewer: read the paper, file one anchored finding, close the slot."""
    slot.whole_paper()
    found = slot.anchor(slot.index)
    if found is None:
        return nothing_found(slot)
    line, quote = found
    slot.submit_finding(title=f'{slot.slot} raised one problem',
                        line=line, quote=quote, severity='major',
                        comment='The sentence asserts more than the passage behind it shows.')
    slot.submit_report('What I read and what I raised.', model=slot.model)


def nothing_found(slot):
    """One reviewer with nothing to raise: read the paper, close the slot empty."""
    slot.whole_paper()
    slot.submit_report('I read the manuscript and found nothing to raise.',
                       none_found=True, model=slot.model)


def editor(slot):
    """The editor: one issue per finding, ruled on, then the pooled review."""
    slot.whole_paper()
    uids = [row['finding_uid'] for row in slot.list_findings()['findings']]
    issues = [{'name': f'Issue {rank}', 'severity': 'major',
               'category': 'claims-and-conclusions', 'rank': rank,
               'statement': 'The manuscript asserts more than the passage behind it supports, '
                            'and the authors have to say which reading they mean.',
               'members': [uid]}
              for rank, uid in enumerate(uids, 1)]
    for at in range(0, len(issues), issue_model.MAX_ISSUE_BATCH):
        slot.submit_issues(issues[at:at + issue_model.MAX_ISSUE_BATCH])
    slot.submit_editor(
        summary='The reviewers agree on one weakness, and it is answerable.'
                if issues else 'The reviewers raised nothing.',
        significance='Would matter to the field if it holds.',
        strength_of_evidence='Partly supported; the main comparison is underpowered.',
        verdict_reasoning='The claim is plausible but the evidence shown is thin.')
    slot.submit_report('The pooled review is above.', model=slot.model)


def checker(slot):
    """One fix-check lane: a verdict for every problem in its own queue."""
    slot.whole_paper()
    for problem in slot.fix_queue()['problems']:
        slot.submit_fix_check(problem['id'], 'cannot-tell',
                              reason='The experiment this problem asks for is not in this draft.',
                              note='Nothing in the new draft settles it either way.')


def code_checker(slot):
    """The code check: a verdict for every claim in the queue, then the close."""
    queue = slot.method_claims()
    for claim in queue['claims']:
        looked = str(claim.get('assumed_method') or claim.get('title') or '')[:60]
        slot.search_code(looked or 'def ')
        slot.submit_code_check(claim['finding_uid'], 'not-shown',
                               searches=[looked or 'def '],
                               note='The snapshot does not hold the step this finding assumes.')
    slot.submit_report('What I looked for in the snapshot, and what it does not show.',
                       model=slot.model)


def citation_worker(slot):
    """One citation worker: claim nothing and judge nothing.

    The ledger is a specialist's instrument, not a stage: a worker that judges
    nothing leaves rows unjudged and cannot hold a run open. A fixture that
    wants a judged ledger scripts this slot itself.
    """
    return {'judged': 0}


#: The script each role runs where a caller names none, by the role the server
#: resolved the slot to. Together they are the smallest run that writes a whole
#: folder: a read manuscript, one anchored finding per core reviewer, an editor
#: that groups and rules on them, and a verdict on every problem of a check.
DEFAULT_SCRIPTS = {'reader': read_stage, 'core': reviewer, 'specialist': nothing_found,
                   'editor': editor, 'fix-check': checker, 'code-check': code_checker,
                   'citation-worker': citation_worker}


# -- the seat the model sits in ---------------------------------------------


def _answer_of(slot):
    """The stage's answer, read off what the script actually did.

    Every stage answers a schema the orchestrator reads — that it sealed, that
    it closed, how many findings it stored — and the server already knows all
    of it, because every one of those is a call this slot made. So the answer
    is assembled here rather than asked of the script: a script that files two
    findings and closes its slot cannot report one, and a script that does the
    work and returns nothing still closes its stage.
    """
    closed = bool((slot.last('submit_report') or {}).get('closed'))
    summary = f'{slot.slot} ran as a script.'
    if slot.role == 'reader':
        sealed = slot.last('seal_manuscript') or {}
        read = sealed.get('ai_read') or {}
        counts = sealed.get('citations') or {}
        lines = (sealed.get('statistics') or {}).get('lines') or {}
        answer = {'sealed': bool(sealed.get('sealed')),
                  'pages': int(read.get('pages') or 0),
                  'pages_viewed': int(read.get('pages_viewed') or 0),
                  'actions': int(read.get('actions') or 0),
                  'tool_calls': int(read.get('tool_calls') or len(slot.calls)),
                  'line_count': int(lines.get(paper_base.MAIN_DOCUMENT) or 0)}
        # Omitted rather than null where the paper has no reference list, which
        # is what the schema asks of a reader that got `citations: null`.
        for key in ('references', 'uses'):
            if counts.get(key) is not None:
                answer[key] = int(counts[key])
        return answer
    if slot.role in ('core', 'specialist'):
        stored = [row for row in slot.answers('submit_finding')
                  if isinstance(row, dict) and row.get('finding_uid')]
        return {'closed': closed, 'findings_stored': len(stored),
                'none_found': not stored, 'model': slot.model,
                'summary': f'{slot.slot} filed {len(stored)} finding(s).'}
    if slot.role == 'editor':
        rows = [row for arguments, named in slot.given('submit_issues')
                for row in (named.get('issues') if named.get('issues') is not None
                            else (arguments[0] if arguments else []) or [])]
        severities = [str(row.get('severity') or '') for row in rows]
        required = sum(1 for one in severities if one in ('major', 'moderate'))
        return {'accepted': bool((slot.last('submit_editor') or {}).get('accepted')),
                'closed': closed, 'issues': len(rows), 'required_changes': required,
                'suggestions': len(rows) - required, 'model': slot.model,
                'summary': f'the editor grouped the findings into {len(rows)} issue(s).'}
    if slot.role == 'code-check':
        verdicts = [(named.get('verdict') if 'verdict' in named
                     else (arguments[1] if len(arguments) > 1 else ''))
                    for arguments, named in slot.given('submit_code_check')]
        queue = slot.last('method_claims') or {}
        return {'closed': closed, 'claims': int(queue.get('count') or 0),
                'confirmed': sum(1 for one in verdicts if one == 'confirmed'),
                'contradicted': sum(1 for one in verdicts if one == 'contradicted'),
                'not_shown': sum(1 for one in verdicts if one == 'not-shown'),
                'model': slot.model,
                'summary': f'the code check answered {len(verdicts)} claim(s).'}
    if slot.role == 'fix-check':
        verdicts = [(named.get('verdict') if 'verdict' in named
                     else (arguments[1] if len(arguments) > 1 else ''))
                    for arguments, named in slot.given('submit_fix_check')]
        queue = slot.last('fix_queue') or {}
        return {'answered': len(verdicts), 'count': int(queue.get('count') or 0),
                'fixed': sum(1 for one in verdicts if one == 'fixed'),
                'not_fixed': sum(1 for one in verdicts if one == 'not-fixed'),
                'cannot_tell': sum(1 for one in verdicts if one == 'cannot-tell'),
                'model': slot.model, 'summary': summary}
    if slot.role == 'citation-worker':
        return {'judged': slot.count('submit_citation_verdict')}
    return {'closed': closed, 'summary': summary, 'model': slot.model}


class _Seat:
    """What sits where the model sits, for one scripted run.

    One of these is the whole runner's script list: it is registered for every
    slot and dispatches on the task it is handed, because the slots a run plans
    are not known until the ingest. It claims the slot, builds the
    :class:`ScriptedSlot`, runs the script the caller named or the default for
    that role, and assembles the stage's answer out of the calls the script
    made.

    An exception out of a script is not swallowed: the lane dies as a model
    that crashed would, and the exception is kept here so the helper can raise
    it again when the run is over. A blocked folder is a true account of the
    run but a poor account of a script with a bug in it.
    """

    def __init__(self, scripts=None, refusal=ToolRefused, defaults=None, tokens=1000):
        self.scripts = dict(scripts or {})
        self.refusal = refusal
        self.defaults = DEFAULT_SCRIPTS if defaults is None else dict(defaults)
        #: What a seat spends when its script does not say: the run's flat number.
        self.tokens = int(tokens or 0)
        #: ``{slot: ScriptedSlot}`` — every agent this run ran, in order.
        self.slots = {}
        #: Every exception a script raised, as ``(slot, exception)``.
        self.raised = []

    def script_for(self, slot, role, agent):
        """The script this slot runs: the caller's by slot, agent, role or ``'*'``."""
        for key in (slot, agent, role, '*'):
            if key in self.scripts:
                return self.scripts[key]
        return self.defaults.get(role)

    def tokens_for(self, task):
        """What the seat that just ran spent: its own :meth:`ScriptedSlot.spend`.

        The runner asks this after the script returns, so a script that
        declared a spend is metered at that number and one that said nothing
        is metered at the run's flat number — which is what every scripted run
        did before a script could say.
        """
        slot = self.slots.get(str(task.label).split('#')[0])
        declared = getattr(slot, 'tokens', None)
        return self.tokens if declared is None else declared

    def code_of(self, task):
        """The claim code out of the brief, the one place an agent is given it."""
        found = CLAIM_CODE.search(str(task.prompt or ''))
        if not found:
            raise self.refusal('claim_agent', {'request_rejected': 'the brief for ' + task.label
                                               + ' carries no claim code'})
        return found.group(1)

    def __call__(self, tools, task):
        name = str(task.label).split('#')[0]
        taken = tools.claim(name, self.code_of(task))
        if isinstance(taken, dict) and 'request_rejected' in taken:
            raise self.refusal('claim_agent', taken)
        slot = ScriptedSlot(tools, task, taken['agent_token'], taken.get('role'),
                            index=len(self.slots), refusal=self.refusal)
        self.slots[name] = slot
        script = self.script_for(name, slot.role, task.agent.name)
        if script is None:
            return None
        try:
            given = script(slot)
        except BaseException as problem:  # noqa: BLE001 - kept, then raised again
            self.raised.append((name, problem))
            raise
        if given is NO_ANSWER:
            return None
        answer = _answer_of(slot)
        return {**answer, **given} if isinstance(given, dict) else answer


# -- what a scripted run left behind ----------------------------------------


class ScriptedRun:
    """One finished scripted run: its summary, its folder and its slots.

    The run is over by the time a caller holds this. :attr:`summary` is the
    dict :func:`peerreviewagents.review` returns, :attr:`folder` is the run
    folder and :attr:`review` the review folder inside it, and :attr:`slots`
    holds every :class:`ScriptedSlot` that ran — so a test can assert on what
    a script was handed and what it called, after the fact.
    """

    def __init__(self, paper, folder, summary, seat):
        self.paper = Path(paper)
        self.folder = Path(folder)
        self.workspace = self.folder / names.MANUSCRIPT_DIRECTORY
        #: The summary the starter returned: status, folders, tokens, counts.
        self.summary = dict(summary)
        #: The review folder inside the run folder, where the stages wrote.
        self.review = Path(self.summary.get('review') or self.folder)
        #: ``{slot: ScriptedSlot}`` — every agent that ran, in the order it ran.
        self.slots = dict(seat.slots)

    def __repr__(self):
        return (f'<{type(self).__name__} {self.status} '
                f'[{", ".join(self.slots) or "nothing ran"}] {self.folder}>')

    @property
    def status(self):
        """How the run ended: ``complete``, ``blocked``, and the rest."""
        return self.summary.get('status')

    @property
    def blocked_reason(self):
        """Why the run is blocked, or ``None`` where it is not."""
        return self.summary.get('blocked_reason')

    @property
    def ran(self):
        """The slots that ran, in order. The recorder is not one: it is this process."""
        return tuple(self.slots)

    def slot(self, name):
        """One slot's :class:`ScriptedSlot`, by name."""
        held = self.slots.get(str(name))
        if held is None:
            raise LookupError(repr(str(name)) + ' did not run in this run. The slots that ran: '
                              + (', '.join(self.slots) or 'none') + '.')
        return held

    def base(self):
        """The manuscript this run left behind, read back as a :class:`PaperBase`.

        ``rows`` are the map rows the Read stage wrote and ``manifest`` holds
        the reader's note under ``ai_read``. It is
        :func:`peerreviewagents.read_manuscript` on :attr:`workspace`, which a
        consumer may call itself; this is here so a test does not have to know
        where in the run folder the workspace is.
        """
        return paper_base.load(self.workspace)


class ScriptedRead(ScriptedRun):
    """A finished scripted run whose Read stage is what the caller came for."""

    @property
    def sealed(self):
        """The reader's own answer to ``seal_manuscript``, or ``None``.

        ``None`` where no reader ran — a folder whose manuscript was already
        sealed plans no Read stage — or where the script sealed nothing.
        """
        held = self.slots.get('reader')
        return held.last('seal_manuscript') if held else None


class ScriptedFixCheck(ScriptedRun):
    """A finished scripted fix check: the verdicts are in the folder."""

    def read(self):
        """The written check, read back with :func:`peerreviewagents.read_fix_check`."""
        from .runs import read_fix_check  # Local: `runs` is the reader of what this writes.

        return read_fix_check(self.folder)


class ScriptedReview(ScriptedRun):
    """A finished scripted review: the issues and the report are in the folder."""

    def read(self):
        """The written review, read back with :func:`peerreviewagents.read_run`."""
        from .runs import read_run  # Local: `runs` is the reader of what this writes.

        return read_run(self.folder)


# -- the three helpers ------------------------------------------------------


def scripted_read(paper, folder, reader=None, *, pieces=(), title='', supplements=None,
                  scripts=None, reviewers=1, arm='single', **plan):
    """Run a review on *paper* in *folder* whose Read stage is a script.

    *reader* is the reader's script: one callable taking its
    :class:`ScriptedSlot`. The default views every page, accounts for every
    outstanding piece and seals, which is the fixture most tests want; a script
    that means to exercise one repair writes that one instead. Anything else
    the arm plans runs its own default, because a run is not a stage — the
    manuscript this seals is read by a reviewer and recorded by the recorder,
    which is what makes the folder the shape a model run leaves.

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

    *scripts* names the other slots' scripts, as :func:`scripted_review`
    documents them. Anything else in *plan* is
    :func:`peerreviewagents.review`'s: the default is the smallest run there
    is, one core reviewer in the single arm, because what this is for is the
    manuscript.

    Returns a :class:`ScriptedRead`.
    """
    named = dict(scripts or {})
    if reader is not None:
        if not callable(reader):
            raise TypeError('the third argument of scripted_read is the reader\'s script, one '
                            'callable taking its slot. Converter pieces go in pieces=.')
        named['reader'] = reader
    return _run(paper, folder, ScriptedRead, ReaderRefused, named,
                pieces=pieces, title=title, supplements=supplements,
                reviewers=reviewers, arm=arm, **plan)


def scripted_fix_check(paper, folder, problems, scripts=None, *, title='', supplements=None,
                       **plan):
    """Run a fix check on *paper* against *problems* in *folder*, from a script.

    *problems* is the ``--problems`` file: a path to one, or the document
    itself — a list of problem rows or an object with ``problems`` — which is
    written to ``<folder>/problems.json`` and read back from there, so a test
    does not have to write the file itself. Either way the run validates it
    against :mod:`peerreviewagents.problems`, so a row the check cannot look
    for raises :class:`CheckRefused` naming every reason rather than being
    checked against nothing.

    The new draft is *read* before it is checked, by the Read stage, because a
    verdict quotes the lines of this draft and there are no lines until the
    manuscript is sealed. The default reader seals it; ``{'reader': …}`` in
    *scripts* is how a fixture reads it differently.

    *scripts* names each lane's script, as :func:`scripted_review` documents
    them. How many lanes there are is not a caller's to choose: the server
    plans one per batch of problems, up to its own cap, and each one is served
    its own queue — ``slot.fix_queue()`` is the problems that lane is to
    answer, and only those.

    A lane that answers nothing, or dies, does not block the check: its
    problems are written down as not checked with that lane named as the
    cause, and the written check says ``incomplete`` with the reason. Nor does
    a reader that fails — the draft is sealed as converted and the check runs
    over the converter's own text, with ``read`` in the result saying the
    reader failed. The same is true of a review: both are the one stage with a
    fallback behind it.

    ``tokens=`` and the ceilings work as in :func:`scripted_review`.

    Returns a :class:`ScriptedFixCheck`.
    """
    folder = Path(folder)
    if isinstance(problems, (str, Path)):
        path = Path(problems)
    else:
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / 'problems.json'
        path.write_text(json.dumps(problem_list.normalize(problems), indent=2) + '\n',
                        encoding='utf-8')
    return _run(paper, folder, ScriptedFixCheck, CheckRefused, scripts,
                title=title, supplements=supplements, problems=str(path), **plan)


def scripted_review(paper, folder, scripts=None, *, title='', supplements=None,
                    reviewers=1, arm='reviewers-only', **plan):
    """Run a review on *paper* in *folder* with a script in every seat.

    *scripts* is ``{key: script}``, where a script is one callable taking its
    :class:`ScriptedSlot` and a key is

    * a slot — ``'core-1'``, ``'editor'``, ``'reader'``, ``'code-check'``;
    * an agent definition's name — ``'core-reviewer'``, ``'fix-check'``;
    * a role — ``'core'``, ``'specialist'``, ``'editor'``, ``'reader'``;
    * or ``'*'``, for every slot the run plans,

    in that order of preference. A slot no key names runs the default for its
    role — :data:`DEFAULT_SCRIPTS` — so ``scripted_review('paper.pdf', folder)``
    writes a whole review folder with no script at all, and a fixture writes a
    script only for the slot it cares about.

    A script normally returns nothing: the stage's answer is read off the calls
    it made. Returning a dict overrides fields of that answer, and returning
    :data:`NO_ANSWER` stands in for an agent that worked and then said nothing,
    which the orchestrator retries once and then blocks over — the way to a
    *blocked* folder. An exception out of a script kills its lane, as a model
    that crashed would, and is raised again from this call when the run is
    over.

    Every core, specialist and editor seat calls ``slot.whole_paper()`` before
    it submits anything: the server refuses a submission from a seat it has
    not served the whole manuscript to, by the same rule a model meets. The
    defaults do it first thing, and a script that skips it is refused.

    What a seat spends is ``tokens=`` — one number for the run, overridden per
    seat by ``slot.spend(n)`` inside a script. The ceilings in *plan*
    (``per_run_tokens``, ``per_agent_tokens``) are enforced against it: a seat
    that spends more than the ceiling the panel handed it ends over the
    ceiling with its answer dropped, which the orchestrator retries once and
    then blocks over, as it does for a real session cut off mid-stream.

    *arm* and *reviewers* are the plan, and anything else in *plan* is
    :func:`peerreviewagents.review`'s — ``tracks``, ``code``,
    ``citation_workers``, the token ceilings. The default is the smallest
    review that writes a whole folder: one core reviewer and the editor, no
    specialists. ``arm='full-two-track'`` with ``code=`` adds the code lane and
    the ``code-check`` slot, which is scriptable like any other.

    A folder whose manuscript is already sealed — one :func:`scripted_read`
    left behind — plans no Read stage, which is how a fixture reviews a
    manuscript a reader read.

    Returns a :class:`ScriptedReview`.
    """
    return _run(paper, folder, ScriptedReview, ReviewRefused, scripts,
                title=title, supplements=supplements, reviewers=reviewers, arm=arm, **plan)


def _run(paper, folder, held, refusal, scripts, *, pieces=(), title='', supplements=None,
         problems=None, tokens=1000, **plan):
    """One scripted run, start to finish, through the orchestrator.

    The whole of what the three helpers share: the draft pre-written where a
    fixture asked for pieces or a title of its own, the seat registered as the
    runner's one script, the run driven by the starter exactly as a model run
    is, and the refusals a script met raised again once the folder is written.
    """
    paper, folder = Path(paper), Path(folder)
    _prepare(paper, folder, pieces, title, supplements)
    seat = _Seat(scripts, refusal, tokens=tokens)
    runner = agent_runner.ScriptedRunner({'*': seat}, tokens=seat.tokens_for)
    # One process serves one run: an ingest is refused while another run is
    # attached, and the run this opens is detached again after. A consumer's
    # suite drives several of these in one process, and a server that is still
    # holding the last one refuses the next ingest rather than running it.
    attached = srv.STATE.get('run')
    srv.STATE['run'] = None
    try:
        if problems is None:
            summary = starter.review(str(paper), str(folder), runner=runner, **plan)
        else:
            summary = starter.check_fixes(str(paper), str(folder), problems, runner=runner,
                                          **plan)
    except ValueError as refused:
        # A plan no run can be made from — a manuscript that is not there, a
        # problems file the server will not read. Nothing ran and nothing was
        # written, which is a caller's mistake and not a run that stopped.
        raise refusal('ingest', {'request_rejected': str(refused)}) from refused
    finally:
        srv.STATE['run'] = attached
    if seat.raised:
        slot, problem = seat.raised[0]
        raise problem
    if summary.get('stage') == 'startup' and summary.get('blocked_reason'):
        raise refusal('ingest', {'request_rejected': str(summary['blocked_reason'])})
    return held(paper, folder, summary, seat)


def _prepare(paper, folder, pieces, title, supplements):
    """Convert the paper into the run's workspace, where a fixture asked for that.

    A run converts the PDF itself, and reuses a draft that is already in the
    workspace rather than converting over it — which is what lets a fixture put
    its own pieces, or its own title, in the draft the Read stage then reads. A
    call that asks for none of that leaves the conversion to the run.
    """
    workspace = folder / names.MANUSCRIPT_DIRECTORY
    if (workspace / 'manifest.json').is_file():
        return  # Already sealed: this run plans no Read stage and no draft.
    if not (pieces or title or supplements):
        return
    draft = convert_paper(str(paper), workspace, title=title or None,
                          supplements=supplements)
    for piece in pieces:
        piece = {**PIECE, **dict(piece)}
        document = str(piece.get('document') or paper_base.MAIN_DOCUMENT)
        piece['document'] = document
        into = draft['documents'].setdefault(document, [])
        if document == paper_base.MAIN_DOCUMENT:
            at = next((index for index, one in enumerate(into)
                       if str(one.get('section')) == 'References'), len(into))
            into.insert(at, piece)
        else:
            into.append(piece)
    write(workspace / DRAFT_FILE, draft)
