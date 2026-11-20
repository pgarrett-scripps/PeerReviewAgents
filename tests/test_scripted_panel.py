"""The whole panel, with Python where the models go.

The orchestrator is the thing that used to be ``review.js``: stage order,
fan-out, one retry then blocked, the budget, the recorder in this process.
These tests run all of it — ``review`` and ``check_fixes``, start to finish,
over the real tool server and the real run folder — with a
:class:`~peerreviewagents.plugin.runner.ScriptedRunner` in the seat the model
sits in. Nothing is launched and no subscription usage is spent.

What that buys over a test that drives the server directly is the boundary. A
scripted agent reaches the tools through
:class:`~peerreviewagents.plugin.runner.Tools`, which grants exactly what the
agent's own definition grants and raises :class:`NotGranted` for anything else.
So a prompt that tells a slot to call a tool it does not have fails here, in a
run that goes all the way to the written document — the class of bug the
fix-check lane shipped with when its brief named ``read_paper`` and its
definition did not.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from peerreviewagents import names
from peerreviewagents.engine import paper_base, paper_view
from peerreviewagents.plugin import server as srv
from peerreviewagents.plugin import starter
from peerreviewagents.plugin.runner import NotGranted, ScriptedRunner
from peerreviewagents.runs import read_fix_check, read_run

FIXTURE = Path(__file__).parent / 'fixtures' / 'single-column.pdf'

#: The claim code every brief hands its agent. A script reads it out of its own
#: prompt exactly as the model does, so a brief that stopped carrying one would
#: fail these tests rather than quietly claiming nothing.
CLAIM_CODE = re.compile(r'claim_code="([^"]*)"')

DESCRIPTION = ('A photograph of the bench with the instrument clamped at the left and three '
               'widgets in a row; the scale divisions are 0.5 mm apart.')


@pytest.fixture(autouse=True)
def fresh_state():
    """One run per process, as the server holds it, and no run left attached."""
    srv.STATE['run'] = None
    srv.STATE['plugin_root'] = None
    yield
    srv.STATE['run'] = None


# -- what a scripted agent knows about itself -------------------------------


def slot_of(task):
    """The slot this task is an attempt at: ``core-1`` from ``core-1#2``."""
    return task.label.split('#')[0]


def code_of(task):
    """The claim code out of the brief, the one place an agent is given it."""
    found = CLAIM_CODE.search(task.prompt)
    assert found, f'the brief for {task.label} carries no claim code'
    return found.group(1)


def claimed(tools, task):
    """The first call of every agent's life, and the token it answers with."""
    answer = tools.claim(slot_of(task), code_of(task))
    assert 'agent_token' in answer, answer
    return answer['agent_token']


def whole_paper(tools, token):
    """Read every part of the manuscript, as every reading brief says to."""
    for part in range(1, paper_view.served(srv.current().base) + 1):
        served = tools.read_paper(token, part=part)
        assert isinstance(served, str), served


def anchorable(index=0):
    """A passage of the sealed manuscript that occurs once, and its line.

    The scripts quote the paper because the server refuses a finding that does
    not anchor in it, which is the one thing that makes a scripted review a
    review and not a fixture.
    """
    run = srv.current()
    text = run.base.text
    found = []
    for line in range(1, run.base.line_count() + 1):
        for piece in run.base.line_text(line).split('. '):
            piece = piece.strip()
            if 30 < len(piece) < 160 and text.count(piece) == 1:
                found.append((line, piece))
                break
        if len(found) > index:
            return found[index]
    raise AssertionError('no uniquely anchorable passage in this fixture')


# -- the scripts ------------------------------------------------------------


def read_stage(tools, task):
    """The reader: view every page, account for every piece, seal."""
    token = claimed(tools, task)
    draft = tools.read_draft(token)
    pages = int(draft.get('page_count') or 0)
    for page in range(1, pages + 1):
        tools.view_page(token, page)
    left = tools.read_draft(token)['outstanding']
    assert not left['lost_pages'], left
    for row in left['pieces']:
        if row['kind'] == 'figure':
            tools.describe_figure(token, row['piece'], row['page'], DESCRIPTION)
        else:
            tools.confirm_piece(token, row['piece'], row['page'])
    sealed = tools.seal_manuscript(token, note='The converter read this paper cleanly.')
    assert sealed.get('sealed'), sealed
    counts = sealed.get('citations') or {}
    lines = (sealed.get('statistics') or {}).get('lines') or {}
    return {'sealed': True, 'pages': pages, 'pages_viewed': pages,
            'actions': len(left['pieces']),
            'line_count': int(lines.get(paper_base.MAIN_DOCUMENT) or 0),
            'references': counts.get('references'), 'uses': counts.get('uses')}


def reviewer(tools, task):
    """One core reviewer: read the paper, file one anchored finding, report."""
    token = claimed(tools, task)
    whole_paper(tools, token)
    return review_with(tools, task, token)


def review_with(tools, task, token):
    """A reviewer's own work, once it holds a token and has read the paper."""
    index = int(slot_of(task).rsplit('-', 1)[-1]) - 1
    line, quote = anchorable(index)
    stored = tools.submit_finding(token, title=f'{slot_of(task)} raised one problem',
                                 line=line, quote=quote, severity='major',
                                 comment='The sentence asserts more than the passage shows.')
    assert 'finding_uid' in stored, stored
    closed = tools.submit_report(token, 'What I read and what I raised.', model=task.model)
    return {'closed': bool(closed.get('closed')), 'findings_stored': 1,
            'finding_uid': stored['finding_uid'], 'model': task.model}


def editor(tools, task):
    """The editor: one issue per finding, ruled on, then the pooled review."""
    token = claimed(tools, task)
    whole_paper(tools, token)
    pooled = tools.list_findings(token)
    uids = [row['finding_uid'] for row in pooled['findings']]
    assert uids, pooled
    issues = [{'name': f'Issue {rank}', 'severity': 'major',
               'category': 'claims-and-conclusions', 'rank': rank,
               'statement': 'The manuscript asserts more than the passage behind it supports, '
                            'and the authors have to say which reading they mean.',
               'members': [uid]}
              for rank, uid in enumerate(uids, 1)]
    stored = tools.submit_issues(token, issues)
    assert stored.get('stored') == len(issues), stored
    accepted = tools.submit_editor(
        token,
        summary='The reviewers agree on one weakness, and it is answerable.',
        significance='Would matter to the field if it holds.',
        strength_of_evidence='Partly supported; the main comparison is underpowered.',
        verdict_reasoning='The claim is plausible but the evidence shown is thin.')
    assert accepted.get('accepted'), accepted
    closed = tools.submit_report(token, 'The pooled review is above.', model=task.model)
    return {'accepted': True, 'closed': bool(closed.get('closed')),
            'issues': len(issues), 'required_changes': len(issues), 'suggestions': 0,
            'model': task.model}


def checker(tools, task):
    """One fix-check lane: a verdict for every problem in its own queue."""
    token = claimed(tools, task)
    whole_paper(tools, token)
    queue = tools.fix_queue(token)
    answered = 0
    for problem in queue['problems']:
        said = tools.submit_fix_check(
            token, problem['id'], 'cannot-tell',
            reason='The experiment this problem asks for is not in this draft.',
            note='Nothing in the new draft settles it either way.')
        assert 'request_rejected' not in said, said
        answered += 1
    return {'answered': answered, 'count': int(queue.get('count') or 0),
            'fixed': 0, 'not_fixed': 0, 'cannot_tell': answered, 'model': task.model}


REVIEW_SCRIPTS = {'reader': read_stage, 'core-reviewer': reviewer, 'editor': editor}
CHECK_SCRIPTS = {'reader': read_stage, 'fix-check': checker}


def problems_file(path, quotes):
    """A ``--problems`` file naming one problem per quotation of the old draft."""
    rows = [{'id': f'i{index:02d}',
             'statement': f'Problem i{index:02d}: the draft says something it cannot show.',
             'severity': 'major',
             'evidence': [{'document': '', 'quote': quote}]}
            for index, quote in enumerate(quotes, 1)]
    Path(path).write_text(json.dumps(
        {'schema_version': names.PROBLEMS_SCHEMA_VERSION, 'problems': rows}, indent=2),
        encoding='utf-8')
    return str(path)


# -- a whole review ---------------------------------------------------------


def test_a_scripted_panel_runs_a_whole_review_and_writes_it(tmp_path):
    """Ingest, Read, the reviewers, the editor, the recorder — and the document.

    The orchestrator's own test: every stage in order, the fan-out over two
    reviewers, the recorder held in this process, and a run folder a consumer
    can read at the end of it.
    """
    runner = ScriptedRunner(REVIEW_SCRIPTS)
    summary = starter.review(str(FIXTURE), str(tmp_path / 'run'), reviewers=2,
                             arm='reviewers-only', runner=runner)
    assert summary['status'] == 'complete', summary
    assert summary['blocked_reason'] is None
    assert summary['stage'] == 'session'
    assert summary['runner'] == 'scripted'
    assert summary['issues']['issues'] == 2
    assert summary['findings_delivered'] == 2
    assert summary['stages']['missing'] == []
    assert summary['stages']['editor_accepted'] is True

    # Every slot the arm plans ran, and the recorder is not among them: that
    # slot is this process's and no agent is launched for it.
    ran = [task.label for task in runner.ran]
    assert ran[0] == 'reader'
    assert set(ran) == {'reader', 'core-1', 'core-2', 'editor'}
    assert summary['agents_run'] == len(ran), summary['tokens_by_agent']
    assert 'recorder' not in summary['tokens_by_agent']
    assert summary['tokens_total'] == len(ran) * runner.tokens

    reading = read_run(tmp_path / 'run')
    assert reading['complete'] is True
    assert reading['publishable'] is True, reading['not_publishable_reasons']
    assert len(reading['issues']) == 2
    review = Path(summary['review'])
    for name in ('protocol.json', 'progress.json', 'manifest.json', 'issues.json', 'REVIEW.md',
                 'findings.jsonl', 'runtime.json', 'startup.json'):
        assert (review / name).is_file(), name
    startup = json.loads((review / 'startup.json').read_text(encoding='utf-8'))
    assert startup['runner'] == 'scripted'
    # A local runner needs no server on the wire: the agents are in this
    # process and call the tools directly.
    assert startup['server'] is None
    assert startup['problems'] == []


def test_the_manuscript_a_scripted_reader_sealed_is_the_one_reviewed(tmp_path):
    """The Read stage ran as a stage: the pages were viewed and the seal is its own."""
    summary = starter.review(str(FIXTURE), str(tmp_path / 'run'), reviewers=1,
                             arm='reviewers-only', runner=ScriptedRunner(REVIEW_SCRIPTS))
    assert summary['status'] == 'complete', summary
    manifest = json.loads((Path(summary['run']) / names.MANUSCRIPT_DIRECTORY / 'manifest.json')
                          .read_text(encoding='utf-8'))
    read = manifest.get('ai_read') or {}
    assert read.get('read') is True
    assert read.get('pages_viewed') == read.get('pages')
    assert 'converter read this paper cleanly' in str(read.get('note') or '')


def test_usage_names_every_attempt_and_the_tools_each_one_called(tmp_path):
    """The usage receipts are the run's own account of what it spent, per agent."""
    summary = starter.review(str(FIXTURE), str(tmp_path / 'run'), reviewers=1,
                             arm='reviewers-only', runner=ScriptedRunner(REVIEW_SCRIPTS))
    usage = Path(summary['review']) / 'usage'
    receipts = {}
    for folder in sorted(usage.iterdir()):
        rows = [json.loads(line) for line in
                (folder / 'usage.jsonl').read_text(encoding='utf-8').splitlines() if line.strip()]
        receipts[folder.name] = rows[-1]
    assert set(receipts) == {'reader', 'core-1', 'editor', 'recorder'}
    assert receipts['core-1']['state'] == 'answered'
    assert receipts['core-1']['tool_calls'] > 0
    assert receipts['core-1']['observed_tokens'] == 1000
    # And in the unit each number is in: the cumulative spend, its four parts,
    # and the widest single call, which is the figure the pre-orchestrator runs
    # reported per agent.
    assert receipts['core-1']['tokens_cumulative'] == 1000
    assert sum(receipts['core-1'][field] for field in names.TOKEN_FIELDS[1:5]) == 1000
    assert receipts['core-1'][names.TOKEN_PEAK] == 1000
    assert receipts['core-1']['detail'] is None, 'it answered and nothing stopped it'
    # The recorder is accounted for without being run: no model, no tokens.
    assert receipts['recorder']['state'] == starter.IN_PROCESS
    assert receipts['recorder']['observed_tokens'] == 0
    assert all(receipts['recorder'][field] == 0 for field in names.TOKEN_FIELDS), \
        'zero in every unit, because no session ran for this slot'
    assert receipts['recorder']['model'] is None
    assert receipts['recorder']['tool_calls'] > 0

    # The same numbers in runtime.json, which is what the public reader reads.
    agents = {row['slot']: row for row in read_run(summary['review'])['agents']}
    assert agents['core-1']['tokens_cumulative'] == 1000
    assert agents['core-1']['ending'] == 'answered'
    assert agents['recorder']['ending'] == starter.IN_PROCESS, \
        'no model ran for it, and the row says so in words'
    assert 'called in this process' in agents['recorder']['detail']


# -- the boundary the scripted panel exists to hold -------------------------


def test_an_agent_cannot_call_a_tool_its_own_definition_does_not_grant(tmp_path):
    """The reviewer asks for the editor's tool and the run stops.

    This is the fix-check lane's bug, in a review: a brief that names a tool
    the definition does not carry. Nothing quietly works — the lane dies, the
    stage returns nothing twice and the run is blocked rather than written.
    """
    def overreaching(tools, task):
        token = claimed(tools, task)
        whole_paper(tools, token)
        return tools.submit_issues(token, [])

    runner = ScriptedRunner({**REVIEW_SCRIPTS, 'core-reviewer': overreaching})
    summary = starter.review(str(FIXTURE), str(tmp_path / 'run'), reviewers=1,
                             arm='reviewers-only', runner=runner)
    assert summary['status'] == 'blocked'
    assert 'core-1' in summary['blocked_reason']
    # A lane that dies is not retried — a retry is for an agent that answered
    # nothing, and this one could not have answered — so the run stops at the
    # stage that overreached: no second attempt, no editor, no review.
    assert [task.label for task in runner.ran] == ['reader', 'core-1']
    assert not (Path(summary['review']) / 'issues.json').is_file()


def test_the_tools_one_slot_holds_are_the_ones_its_definition_names(tmp_path):
    """Granted, ungranted and unknown, asked of the agent that is running."""
    held = {}

    def inspect(tools, task):
        held[slot_of(task)] = (sorted(task.tools), tools)
        token = claimed(tools, task)
        whole_paper(tools, token)
        with pytest.raises(NotGranted) as refused:
            tools.write_review(token)
        assert 'core-reviewer' in str(refused.value)
        with pytest.raises(AttributeError):
            tools.not_a_tool_at_all(token)
        return review_with(tools, task, token)

    summary = starter.review(str(FIXTURE), str(tmp_path / 'run'), reviewers=1,
                             arm='reviewers-only',
                             runner=ScriptedRunner({**REVIEW_SCRIPTS,
                                                    'core-reviewer': inspect}))
    assert summary['status'] == 'complete', summary
    granted, _ = held['core-1']
    assert 'submit_finding' in granted and 'submit_issues' not in granted


def test_a_reader_whose_answer_was_lost_is_not_asked_to_read_a_sealed_paper(tmp_path):
    """The run that sent us here: all the work done, the answer gone.

    The reader viewed all 29 pages of a real manuscript, repaired every piece
    and sealed it — and was then stopped on a token ceiling before the harness
    wrote the result event, so the stage saw no answer, retried, and was
    refused by the server because the manuscript was already sealed. The run
    blocked on work that was finished.

    So before any retry the stage asks the server what is on disk. The seal is
    the reader's own, the counts come from the server, and the run goes on.
    """
    tried = []

    def seals_then_says_nothing(tools, task):
        tried.append(task.label)
        read_stage(tools, task)
        return None

    summary = starter.review(str(FIXTURE), str(tmp_path / 'run'), reviewers=1,
                             arm='reviewers-only',
                             runner=ScriptedRunner({**REVIEW_SCRIPTS,
                                                    'reader': seals_then_says_nothing}))

    assert tried == ['reader'], 'asked once: the second attempt would have been refused'
    assert summary['status'] == 'complete', summary
    manifest = json.loads((Path(summary['run']) / names.MANUSCRIPT_DIRECTORY / 'manifest.json')
                          .read_text(encoding='utf-8'))
    assert (manifest.get('ai_read') or {}).get('read') is True, \
        'the seal is the reader\'s own, not a seal-as-converted'
    assert read_run(summary['review'])['manuscript']['state'] == names.MANUSCRIPT_SEALED_BY_READER


def test_a_reviewer_whose_answer_was_lost_is_not_asked_to_report_twice(tmp_path):
    """The same rule, for a stage the ledger holds rather than the manuscript.

    A reviewer that filed its findings and closed its slot has reported; if the
    answer never reached us the work is still on the ledger, and asking again
    would be asking for work the server will refuse.
    """
    tried = []

    def reports_then_says_nothing(tools, task):
        tried.append(task.label)
        reviewer(tools, task)
        return None

    summary = starter.review(str(FIXTURE), str(tmp_path / 'run'), reviewers=1,
                             arm='reviewers-only',
                             runner=ScriptedRunner({**REVIEW_SCRIPTS,
                                                    'core-reviewer': reports_then_says_nothing}))

    assert tried == ['core-1'], 'the ledger already had its report'
    assert summary['status'] == 'complete', summary
    filed = [row['finding_uid'] for row in read_run(summary['review'])['findings']]
    assert filed, 'the findings it filed are the ones the review was written from'
    runtime = json.loads((Path(summary['review']) / 'runtime.json').read_text(encoding='utf-8'))
    stage = next(row for row in runtime['result']['stages'] if row['slot'] == 'core-1')
    assert stage['findings'] == len(filed), \
        'the count comes from the server, not from the answer that was lost'


def test_a_stage_that_answers_nothing_is_retried_once_and_then_blocks(tmp_path):
    """One retry on a fresh claim code, and the run blocked rather than written."""
    tried = []

    def silent(tools, task):
        tried.append(task.label)
        return None

    summary = starter.review(str(FIXTURE), str(tmp_path / 'run'), reviewers=1,
                             arm='reviewers-only',
                             runner=ScriptedRunner({**REVIEW_SCRIPTS, 'core-reviewer': silent}))
    assert tried == ['core-1', 'core-1#2']
    assert summary['status'] == 'blocked'
    assert 'core-1' in summary['blocked_reason']
    runtime = json.loads((Path(summary['review']) / 'runtime.json').read_text(encoding='utf-8'))
    assert runtime['status'] == 'blocked'


# -- a whole fix check ------------------------------------------------------


def test_a_scripted_panel_runs_a_whole_fix_check_and_writes_it(tmp_path):
    """The other run this orchestrator drives: one lane, one verdict per problem."""
    quotes = ['the authors have since withdrawn this claim',
              'a 42% improvement over the unmodified baseline']
    path = problems_file(tmp_path / 'problems.json', quotes)
    runner = ScriptedRunner(CHECK_SCRIPTS)
    summary = starter.check_fixes(str(FIXTURE), str(tmp_path / 'run'), path, runner=runner)
    assert summary['status'] == 'complete', summary
    assert summary['blocked_reason'] is None
    assert summary['problems'] == 2
    assert summary['checked'] == 2
    assert summary['counts'] == {'fixed': 0, 'not-fixed': 0, 'cannot-tell': 2}
    assert [task.label for task in runner.ran] == ['reader', 'fix-1']

    reading = read_fix_check(tmp_path / 'run')
    assert reading['complete'] is True
    assert len(reading['problems']) == 2
    assert len(reading['checks']) == 2
    review = Path(summary['review'])
    assert (review / 'fix-check.json').is_file()
    assert (review / 'FIXES.md').is_file()


def test_a_fix_check_lane_reads_the_paper_its_definition_grants_it(tmp_path):
    """The point-1 bug, held down from the other side.

    ``fix-check`` is told to read the paper and is granted ``read_paper``; the
    lane above calls it on every run, so a definition that stopped granting it
    would fail this whole file. This test names it, so the failure says why.
    """
    path = problems_file(tmp_path / 'problems.json', ['all measurements were repeated'])
    granted = {}

    def lane(tools, task):
        granted[slot_of(task)] = sorted(task.tools)
        return checker(tools, task)

    summary = starter.check_fixes(str(FIXTURE), str(tmp_path / 'run'), path,
                                  runner=ScriptedRunner({**CHECK_SCRIPTS, 'fix-check': lane}))
    assert summary['status'] == 'complete', summary
    assert {'read_paper', 'fix_queue', 'submit_fix_check'} <= set(granted['fix-1'])
