"""The fix check: one verdict per problem a review raised, against a new draft.

Every test here runs offline. Nothing starts Claude Code or any provider, and
the check itself makes no model call — the lane is one queue, one submit tool
and the server's own reading of its own manuscript, which is the whole point of
putting the quotation check in the server.

What these tests hold down is the thing a fix check can get wrong in a way
nobody notices: reporting a problem as fixed. A verdict rests on a passage of
the new draft, on the old quotation being gone, or on the old quotation still
standing word for word, and each of those is checked here against a manuscript
the suite built rather than against a model's account of one.
"""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path

import pytest
from manuscripts import seal_converted

from peerreviewagents import names
from peerreviewagents import problems as problem_list
from peerreviewagents.cli import main as cli
from peerreviewagents.plugin import models as model_plan
from peerreviewagents.plugin import server as srv
from peerreviewagents.plugin import starter
from peerreviewagents.runs import NotARunFolder, read_fix_check

FIXTURE = Path(__file__).parent / 'fixtures' / 'single-column.pdf'

#: Quotations the fixture does not say, for a problem fixed by deletion. Each is
#: ordinary English, so a refusal here means the anchor matched something it
#: should not rather than that the words were exotic.
GONE = ('the authors have since withdrawn this claim',
        'a 42% improvement over the unmodified baseline',
        'all measurements were repeated on a second instrument')

CODES: dict = {}


@pytest.fixture(autouse=True)
def fresh_state():
    srv.STATE['run'] = None
    srv.STATE['plugin_root'] = None
    CODES.clear()
    yield
    srv.STATE['run'] = None


@pytest.fixture(scope='module')
def draft_lines(tmp_path_factory):
    """Passages of the fixture that occur in it exactly once, longest first.

    A problems file has to be written before the run that checks it exists, so
    the quotations of the "old" draft are harvested from the fixture once here,
    through the same import and the same seal a check uses. The run opened for
    the harvest is thrown away — ``fresh_state`` clears the server before the
    first test — and what the tests keep is a list of strings.
    """
    opened = srv.ingest(str(FIXTURE), str(tmp_path_factory.mktemp('harvest') / 'run'))
    assert 'request_rejected' not in opened, opened
    seal_converted(opened)
    run = srv.current()
    text = run.base.text
    found = []
    for line in range(1, run.base.line_count() + 1):
        for piece in run.base.line_text(line).replace(';', '.').split('. '):
            piece = piece.strip().strip('.')
            if 40 < len(piece) < 160 and text.count(piece) == 1:
                found.append(piece)
    assert len(found) > 12, 'the fixture has too few uniquely anchorable passages'
    srv.STATE['run'] = None
    return found


def problems_file(path, rows):
    """A ``--problems`` file at *path*, from ``(id, quote)`` pairs or whole rows."""
    built = []
    for index, row in enumerate(rows, start=1):
        if isinstance(row, dict):
            built.append(row)
            continue
        identity, quote = row
        built.append({'id': identity,
                      'statement': f'Problem {identity}: the draft says something it cannot show.',
                      'severity': 'major',
                      'evidence': [{'document': '', 'quote': quote}],
                      'author_reply': None if index % 2 else 'Rewritten in the revision.'})
    Path(path).write_text(json.dumps(
        {'schema_version': names.PROBLEMS_SCHEMA_VERSION, 'problems': built}, indent=2),
        encoding='utf-8')
    return str(path)


def open_check(tmp_path, rows, seal=True, **kwargs):
    """Open a fix check on the fixture against *rows*, and seal its draft."""
    path = problems_file(tmp_path / 'problems.json', rows)
    opened = srv.ingest(str(FIXTURE), str(tmp_path / 'run'), arm=srv.FIX_ARM, problems=path,
                        **kwargs)
    assert 'request_rejected' not in opened, opened
    CODES.update({row['slot']: row['claim_code'] for row in opened.get('claim_codes', [])})
    CODES['recorder_key'] = opened.get('recorder_key', '')
    if seal:
        seal_converted(opened)
    return opened


def checker(slot='fix-1'):
    """Claim one checker slot with the code minted for it, and read the paper."""
    taken = srv.claim_agent(slot, CODES.get(slot, ''))
    assert 'agent_token' in taken, taken
    CODES.pop(slot, None)
    run = srv.current()
    for part in range(1, srv.paper_view.served(run.base) + 1):
        assert isinstance(srv.read_paper(taken['agent_token'], part=part), str)
    return taken


def refusal(answer):
    """The refusal text of a rejected call, asserting that it was rejected."""
    assert 'request_rejected' in answer, answer
    return answer['request_rejected']


# -- the plan ---------------------------------------------------------------


def test_a_fix_check_plans_one_checker_per_batch_of_problems(tmp_path, draft_lines):
    """The lane is cut from the problems file, not from a count passed in.

    A review's plan is its arm and its reviewer count; a check's plan is the
    list it was given. One slot per :data:`FIX_BATCH` problems means a checker
    that dies takes its own batch and nothing else, and the slot names are what
    the workflow fans out over, so they come back from ``ingest`` rather than
    being computed a second time in the script.
    """
    opened = open_check(tmp_path, [(f'i{n:02d}', draft_lines[n % 12]) for n in range(1, 13)])
    plan = opened['fix_check']
    assert plan['slots'] == ['fix-1', 'fix-2']
    assert (plan['problems'], plan['batch'], plan['limit'], plan['over_cap']) == (
        12, srv.FIX_BATCH, srv.MAX_FIX_CHECKS, 0)
    assert plan['verdicts'] == list(names.FIX_VERDICTS)
    assert opened['arm'] == srv.FIX_ARM
    # The checkers are claimable like any other slot and required like none of
    # them: a batch nobody answered is not a blocked run.
    assert 'fix-1' in {row['slot'] for row in opened['claim_codes']}
    assert 'fix-1' not in opened.get('required', [])


def test_the_slot_list_is_capped_and_a_review_has_no_checkers():
    """``checker_slots`` is the whole of the cut, and it is the fix arm's alone."""
    assert srv.checker_slots(srv.FIX_ARM, 1) == ('fix-1',)
    assert srv.checker_slots(srv.FIX_ARM, srv.FIX_BATCH + 1) == ('fix-1', 'fix-2')
    assert len(srv.checker_slots(srv.FIX_ARM, 500)) == srv.MAX_FIX_CHECKS // srv.FIX_BATCH
    assert srv.checker_slots(srv.DEFAULT_ARM, 12) == ()
    assert srv.checker_slots(srv.FIX_ARM, 0) == ()
    # Every checker runs the specialist's model: `role_of` reads a slot name it
    # does not know as a core reviewer's, which would be the wrong model.
    assert model_plan.role_of('fix-3') == 'specialist'


def test_a_review_refuses_problems_and_a_check_refuses_none(tmp_path, draft_lines):
    """Neither mistake is discovered by a lane with an empty queue.

    A review handed problems would ignore them — the reviewers raise their own
    — and a check with none has nothing to answer. Both are refused at the
    import, where the message can name the command that does the other thing.
    """
    path = problems_file(tmp_path / 'problems.json', [('i01', draft_lines[0])])
    refused = refusal(srv.ingest(str(FIXTURE), str(tmp_path / 'a'), problems=path))
    assert srv.FIX_ARM in refused and 'check-fixes' in refused
    srv.STATE['run'] = None
    empty = problems_file(tmp_path / 'empty.json', [])
    assert 'There is no problems file' not in refusal(
        srv.ingest(str(FIXTURE), str(tmp_path / 'b'), arm=srv.FIX_ARM, problems=empty))
    srv.STATE['run'] = None
    assert '--problems' in refusal(
        srv.ingest(str(FIXTURE), str(tmp_path / 'c'), arm=srv.FIX_ARM))


def test_a_review_run_reports_no_fix_check_block(tmp_path):
    """``summary_of`` carries no empty block about a lane that does not exist."""
    opened = srv.ingest(str(FIXTURE), str(tmp_path / 'run'))
    assert 'request_rejected' not in opened, opened
    assert opened['fix_check'] is None
    seal_converted(opened)
    assert srv.current().fix_check_record() is None
    assert json.loads((Path(opened['run']) / 'review' / 'progress.json')
                      .read_text(encoding='utf-8'))['fix_check'] is None


# -- the queue --------------------------------------------------------------


def test_the_checker_is_told_which_old_quotations_still_stand(tmp_path, draft_lines):
    """The one fact a checker would otherwise spend its allowance establishing.

    Whether the words a problem was raised against are still in the new draft
    is the server's own reading of its own manuscript. It is handed over at the
    claim and again on every queue row, because a checker that searched for it
    would be checking the server's manuscript with the server's search and
    calling it evidence.
    """
    open_check(tmp_path, [('still-there', draft_lines[0]), ('gone', GONE[0])])
    taken = checker()
    told = taken['fix_check']
    assert told['count'] == 2 and told['cap'] == srv.MAX_FIX_CHECKS
    assert told['quotes_gone'] == 1
    assert told['verdicts'] == list(names.FIX_VERDICTS)
    queue = srv.fix_queue(taken['agent_token'])
    rows = {row['id']: row for row in queue['problems']}
    assert queue['count'] == 2 and queue['answered'] == 0
    assert rows['still-there']['evidence'][0]['in_new_draft']['present'] is True
    assert rows['still-there']['evidence'][0]['in_new_draft']['exact'] is True
    assert rows['gone']['evidence'][0]['in_new_draft']['present'] is False
    # The problem as it was given, not as an issue: five fields and no anchors
    # of a manuscript this run does not hold.
    assert set(rows['gone']) == {*problem_list.PROBLEM_FIELDS}


def test_a_checker_answers_its_own_batch_and_nobody_else_s(tmp_path, draft_lines):
    """Two checkers never answer the same problem, and cannot reach across.

    The problems are dealt out in slot order, so a verdict from the wrong slot
    is refused rather than stored twice. This is also what makes a retry of one
    slot pick up where the first attempt stopped: an answered problem is not in
    the rows the slot is handed again.
    """
    rows = [(f'i{n:02d}', draft_lines[n % 12]) for n in range(1, 13)]
    open_check(tmp_path, rows)
    first = checker('fix-1')
    second = checker('fix-2')
    mine = srv.fix_queue(first['agent_token'])
    theirs = srv.fix_queue(second['agent_token'])
    assert mine['count'] == srv.FIX_BATCH and theirs['count'] == 2
    assert not ({row['id'] for row in mine['problems']}
                & {row['id'] for row in theirs['problems']})
    trespass = srv.submit_fix_check(first['agent_token'], theirs['problems'][0]['id'],
                                    'cannot-tell', reason='The draft does not settle it.',
                                    note='Nothing in the revision speaks to this.')
    assert 'not in your batch' in refusal(trespass)
    answered = srv.submit_fix_check(first['agent_token'], mine['problems'][0]['id'],
                                    'cannot-tell', reason='The fix would be in the code.',
                                    note='The draft says the analysis was rerun; the code is '
                                         'where that is settled.')
    assert answered['stored'] is True
    again = srv.fix_queue(first['agent_token'])
    assert again['count'] == srv.FIX_BATCH - 1 and again['answered'] == 1
    assert mine['problems'][0]['id'] not in {row['id'] for row in again['problems']}


def test_the_lane_has_no_way_to_raise_a_problem_of_its_own(tmp_path, draft_lines):
    """A check raises nothing, so the checker holds no finding tool at all.

    Not a brief that asks it not to: the grant itself. And the queue is the fix
    arm's — a reviewer on a review that called it would be told so rather than
    handed an empty list.
    """
    open_check(tmp_path, [('i01', draft_lines[0])])
    taken = checker()
    allowed = srv.current().agent_for(taken['agent_token']).allowed
    assert 'fix_queue' in allowed and 'submit_fix_check' in allowed
    for tool in ('submit_finding', 'submit_report', 'submit_editor', 'submit_audit'):
        assert tool not in allowed, tool
    assert 'read_paper' in allowed and 'view_page' in allowed


def test_the_queue_is_refused_outside_a_fix_check(tmp_path):
    """A review has no problems, and the refusal says which command does."""
    opened = srv.ingest(str(FIXTURE), str(tmp_path / 'run'))
    assert 'request_rejected' not in opened, opened
    seal_converted(opened)
    CODES.update({row['slot']: row['claim_code'] for row in opened['claim_codes']})
    taken = srv.claim_agent('core-1', CODES['core-1'])
    assert 'agent_token' in taken, taken
    assert 'Only a fix-check slot' in refusal(srv.fix_queue(taken['agent_token']))


# -- the verdicts -----------------------------------------------------------


def answer(token, problem_id, verdict, **kwargs):
    kwargs.setdefault('note', 'What the revision now says about this, in one line.')
    return srv.submit_fix_check(token, problem_id, verdict, **kwargs)


def test_a_verdict_quotes_the_new_draft_and_the_server_anchors_it(tmp_path, draft_lines):
    """The same check a finding's quotation gets, for the same reason.

    A verdict that could rest on words the draft does not say is a verdict a
    model can invent, and "fixed" is the one answer nobody re-reads. The quote
    is anchored before anything is stored, and what it anchored to is on the
    row: document, line, page, section and the served text.
    """
    open_check(tmp_path, [('i01', draft_lines[0]), ('i02', draft_lines[1])])
    token = checker()['agent_token']
    stored = answer(token, 'i01', 'fixed', quote=draft_lines[3])
    assert stored['stored'] is True and stored['basis'] == 'quote'
    assert stored['draft_evidence']['quote'] == draft_lines[3]
    assert stored['draft_evidence']['line'] >= 1
    assert stored['draft_evidence']['quoted_exactly'] is True
    invented = answer(token, 'i02', 'fixed',
                      quote='The authors have added a sensitivity analysis over all three '
                            'thresholds, which resolves the concern entirely.')
    assert 'Nothing was stored' in refusal(invented)
    assert len(srv.current().fix_checks) == 1


def test_a_fix_by_deletion_rests_on_the_old_words_being_gone(tmp_path, draft_lines):
    """The case with nothing left to quote, and the two ways it is abused.

    A sentence the authors deleted cannot be quoted from the draft that no
    longer holds it, so the verdict rests on the server confirming the old
    quotation is gone, plus the searches the checker ran. Without the searches
    there is no record of what was looked for; with the old words still in the
    draft the problem was not fixed by deleting them, and the server says so
    rather than taking the verdict.
    """
    open_check(tmp_path, [('deleted', GONE[0]), ('still-there', draft_lines[0])])
    token = checker()['agent_token']
    bare = answer(token, 'deleted', 'fixed')
    assert 'searches' in refusal(bare)
    stored = answer(token, 'deleted', 'fixed', searches=[GONE[0], 'withdrawn this claim'])
    assert stored['basis'] == 'old-quote-gone'
    assert stored['draft_evidence'] is None
    assert stored['searches'] == [GONE[0], 'withdrawn this claim']
    pretend = answer(token, 'still-there', 'fixed', searches=['anything at all'])
    assert 'still in this draft word for word' in refusal(pretend)
    assert len(srv.current().fix_checks) == 1


def test_not_fixed_without_a_new_quote_rests_on_the_old_one_standing(tmp_path, draft_lines):
    """The mirror of the deletion case, and it is deliberately symmetric.

    A deletion-only rule was the obvious design and the wrong one: it left "the
    draft changed nothing here" as the one verdict with nothing behind it. A
    ``not-fixed`` with no new passage to quote is stored only where the server
    confirms a quotation the problem was raised against is still there word for
    word — and where none is, the honest answer is ``cannot-tell``.
    """
    open_check(tmp_path, [('untouched', draft_lines[0]), ('moved', GONE[1])])
    token = checker()['agent_token']
    stored = answer(token, 'untouched', 'not-fixed', searches=[draft_lines[0][:40]])
    assert stored['basis'] == 'old-quote-stands' and stored['draft_evidence'] is None
    nothing_to_stand_on = answer(token, 'moved', 'not-fixed', searches=['the old sentence'])
    assert 'answer cannot-tell' in refusal(nothing_to_stand_on)
    assert len(srv.current().fix_checks) == 1


def test_cannot_tell_carries_a_reason_and_no_passage(tmp_path, draft_lines):
    """It is the honest answer for a fix outside the manuscript, not a hedge.

    So it is stored only with the reason — what was looked for and what the
    draft does not say — and never with a quotation, which would make it a
    weaker ``not-fixed`` with evidence nobody could read as either.
    """
    open_check(tmp_path, [('i01', draft_lines[0]), ('i02', draft_lines[1])])
    token = checker()['agent_token']
    assert 'needs the reason' in refusal(answer(token, 'i01', 'cannot-tell'))
    assert 'carries no passage' in refusal(
        answer(token, 'i01', 'cannot-tell', quote=draft_lines[2], reason='Unclear.'))
    stored = answer(token, 'i01', 'cannot-tell',
                    reason='The draft says the analysis was rerun with the corrected filter; '
                           'whether it was is in the code, which this check did not read.')
    assert stored['stored'] is True and stored['basis'] == ''
    assert 'not recorded as either fixed or unfixed' in stored['effect']
    assert 'reason belongs to cannot-tell' in refusal(
        answer(token, 'i02', 'fixed', quote=draft_lines[4], reason='Also unclear.'))


def test_every_verdict_carries_a_note_and_a_problem_is_answered_once(tmp_path, draft_lines):
    """The note is what the author reads, and the second verdict is refused.

    One verdict per problem: a checker that restated itself would otherwise put
    two rows with two different answers in the same check, and nothing in the
    file would say which was meant.
    """
    open_check(tmp_path, [('i01', draft_lines[0]), ('i02', draft_lines[1])])
    token = checker()['agent_token']
    assert 'Nothing was stored' in refusal(
        srv.submit_fix_check(token, 'i01', 'fixed', quote=draft_lines[5]))
    assert answer(token, 'i01', 'fixed', quote=draft_lines[5])['stored'] is True
    assert 'already been answered' in refusal(
        answer(token, 'i01', 'not-fixed', searches=[draft_lines[0][:40]]))
    assert 'verdict is one of' in refusal(answer(token, 'i02', 'partly-fixed'))
    assert 'No problem in this run has the id' in refusal(
        answer(token, 'nope', 'cannot-tell', reason='There is no such problem.'))


# -- the written check ------------------------------------------------------


def test_the_written_check_counts_the_problems_nobody_answered(tmp_path, draft_lines):
    """A problem with no verdict is neither fixed nor unfixed, and says so.

    The cap and a checker that died are the same fact to a reader — a problem
    this run did not answer — so the check reports both as not checked, with
    the over-cap count beside it. Writing it is the recorder's, once, and a
    second call hands back the counts of what was written rather than writing
    it again.
    """
    rows = [(f'i{n:02d}', draft_lines[n % 12]) for n in range(1, 13)]
    open_check(tmp_path, rows)
    token = checker('fix-1')['agent_token']
    assert answer(token, 'i01', 'fixed', quote=draft_lines[6])['stored'] is True
    assert answer(token, 'i02', 'not-fixed', searches=['the claim as written'])['stored'] is True
    assert answer(token, 'i03', 'cannot-tell',
                  reason='The fix is a deposited dataset, not a change to the text.'
                  )['stored'] is True
    written = srv.write_fix_check(CODES['recorder_key'])
    assert written['status'] == names.COMPLETE
    assert written['counts'] == {'fixed': 1, 'not-fixed': 1, 'cannot-tell': 1}
    assert (written['checked'], written['not_checked']) == (3, 9)
    report = Path(written['fixes'])
    document = json.loads(Path(written['fix_check']).read_text(encoding='utf-8'))
    assert document['schema_version'] == names.FIX_CHECK_SCHEMA_VERSION
    assert document['problems_schema_version'] == names.PROBLEMS_SCHEMA_VERSION
    assert len(document['given']) == 12 and len(document['unchecked']) == 9
    prose = report.read_text(encoding='utf-8')
    # Not fixed first: a reader of a revision reads what is still open before
    # what is settled.
    assert (prose.index('## Still open')
            < prose.index('## The draft does not settle these')
            < prose.index('## Fixed'))
    assert 'not checked' in prose.lower()
    second = srv.write_fix_check(CODES['recorder_key'])
    assert 'already written' in refusal(second)
    assert second['counts'] == written['counts']
    assert second['not_checked'] == 9


def test_a_check_that_answered_nothing_is_a_finished_check(tmp_path, draft_lines):
    """Not a blocked run: every problem it was given is written down unanswered.

    A lane that died holding its batch costs that batch, and the document that
    says so is the run's answer. There is no third kind of finished run, so the
    status is the one a reader of a run folder already knows.
    """
    open_check(tmp_path, [('i01', draft_lines[0]), ('i02', draft_lines[1])])
    written = srv.write_fix_check(CODES['recorder_key'])
    assert written['status'] == names.COMPLETE_WITHOUT_FINDINGS
    assert (written['checked'], written['not_checked']) == (0, 2)
    assert written['counts'] == {'fixed': 0, 'not-fixed': 0, 'cannot-tell': 0}


def test_the_check_is_written_by_the_recorder_and_not_on_a_review(tmp_path, draft_lines):
    """Two refusals that keep the two kinds of run from writing each other's files."""
    open_check(tmp_path, [('i01', draft_lines[0])])
    assert 'written by the recorder' in refusal(srv.write_fix_check('not-the-key'))
    srv.STATE['run'] = None
    review = srv.ingest(str(FIXTURE), str(tmp_path / 'review'))
    assert 'request_rejected' not in review, review
    seal_converted(review)
    assert 'write_review' in refusal(srv.write_fix_check(review['recorder_key']))


# -- reading it back --------------------------------------------------------


def test_read_fix_check_reads_the_folder_the_check_wrote(tmp_path, draft_lines):
    """The reading a consumer gets: one call, the same keys whatever happened.

    ``read_run`` is a review's reading and reports findings, issues and a
    guard, none of which a check has. This is the counterpart, and it answers
    for a damaged folder the same way: nothing raised for anything on disk, and
    ``file_problems`` naming what could not be read.
    """
    open_check(tmp_path, [('kept', draft_lines[0]), ('cut', GONE[2])])
    token = checker()['agent_token']
    answer(token, 'cut', 'fixed', searches=[GONE[2]])
    answer(token, 'kept', 'not-fixed', searches=[draft_lines[0][:40]])
    written = srv.write_fix_check(CODES['recorder_key'])
    check = read_fix_check(str(tmp_path / 'run'))
    assert check['status'] == names.COMPLETE and check['complete'] is True
    assert check['counts'] == {'fixed': 1, 'not-fixed': 1, 'cannot-tell': 0}
    assert (check['checked'], check['not_checked'], check['over_cap']) == (2, 0, 0)
    assert check['cap'] == srv.MAX_FIX_CHECKS and check['unchecked'] == []
    rows = {row['problem_id']: row for row in check['checks']}
    assert rows['cut']['basis'] == 'old-quote-gone'
    assert rows['kept']['basis'] == 'old-quote-stands'
    assert rows['kept']['old_evidence'][0]['in_new_draft']['present'] is True
    assert [one['id'] for one in check['problems']] == ['kept', 'cut']
    assert Path(check['reports']['fix_check']) == Path(written['fix_check'])
    assert Path(check['reports']['markdown']).is_file()
    assert Path(check['reports']['checks']).is_file()
    assert check['file_problems'] == []
    # No session ran, so there are no usage receipts to read: the keys are there
    # and empty rather than missing, which is what a consumer reads either way.
    assert check['tokens'] is None and check['usage']['calls'] == 0
    assert {row['slot'] for row in check['agents']} == {'fix-1'}


def test_read_fix_check_says_so_when_the_folder_holds_a_review(tmp_path):
    """A review read as a check is a damage report, not an exception."""
    opened = srv.ingest(str(FIXTURE), str(tmp_path / 'run'))
    assert 'request_rejected' not in opened, opened
    seal_converted(opened)
    check = read_fix_check(str(tmp_path / 'run'))
    assert check['checks'] == [] and check['problems'] == []
    assert any('fix check' in one for one in check['file_problems']), check['file_problems']
    with pytest.raises(NotARunFolder):
        read_fix_check(str(tmp_path / 'nowhere'))


# -- the problems file ------------------------------------------------------


def test_a_problems_file_is_written_from_a_finished_review(tmp_path):
    """The file a check takes, built from the issues a review published.

    The member line numbers are deliberately left behind: they are positions in
    the *old* manuscript, and the check reads a draft whose lines are somewhere
    else. An issue whose members carry no quotation is left out, because a
    problem with no passage to look for is one this module's own reader refuses.
    """
    review = tmp_path / 'first' / 'review'
    review.mkdir(parents=True)
    (review / 'issues.json').write_text(json.dumps({
        'schema_version': 'review-issues-v4',
        'issues': [
            {'id': 'i01', 'statement': 'The abstract overstates the gain.',
             'severity': 'major',
             'members': [{'document': '', 'quote': 'a large improvement', 'line': 4},
                         {'document': '', 'quote': 'a large improvement', 'line': 4},
                         {'document': 'supplement.md', 'quote': 'the second cohort', 'line': 9}]},
            {'id': 'i02', 'statement': 'No quotation was filed for this one.',
             'severity': 'minor', 'members': []},
        ]}), encoding='utf-8')
    built = problem_list.problems_from_run(str(tmp_path / 'first'))
    assert [one['id'] for one in built['problems']] == ['i01']
    evidence = built['problems'][0]['evidence']
    assert evidence == [{'document': '', 'quote': 'a large improvement'},
                        {'document': 'supplement.md', 'quote': 'the second cohort'}]
    assert built['problems'][0]['author_reply'] is None
    assert built['schema_version'] == names.PROBLEMS_SCHEMA_VERSION
    on_disk = json.loads((review / 'problems.json').read_text(encoding='utf-8'))
    assert on_disk == built
    # And the file that comes out is one the check's own reader takes.
    assert problem_list.read(review / 'problems.json')['problems'][0]['id'] == 'i01'


def test_a_problems_file_the_check_cannot_look_for_is_refused(tmp_path):
    """Named at once and all of them, before a session is started.

    A check run against half a problems file would report a paper as fixed
    because the problem it was raised against was never read, so nothing is
    dropped quietly.
    """
    path = tmp_path / 'problems.json'
    path.write_text(json.dumps({'problems': [
        {'id': 'i01', 'statement': 'Fine.', 'severity': 'major',
         'evidence': [{'document': '', 'quote': 'something the old draft said'}]},
        {'id': '', 'statement': 'No id.', 'severity': 'major',
         'evidence': [{'document': '', 'quote': 'words'}]},
        {'id': 'i01', 'statement': 'A second row with the same id.', 'severity': 'major',
         'evidence': [{'document': '', 'quote': 'words'}]},
        {'id': 'i04', 'statement': 'No evidence.', 'severity': 'major', 'evidence': []},
        {'id': 'i05', 'statement': 'Bad severity.', 'severity': 'catastrophic',
         'evidence': [{'document': '', 'quote': 'words'}]},
    ]}), encoding='utf-8')
    with pytest.raises(ValueError) as refused:
        problem_list.read(path)
    said = str(refused.value)
    assert said.count('  - ') == 4, said
    with pytest.raises(ValueError) as missing:
        problem_list.read(tmp_path / 'not-there.json')
    assert 'problems_from_run' in str(missing.value)


# -- the command ------------------------------------------------------------


def test_the_cli_has_a_third_command_and_routes_it(tmp_path, monkeypatch):
    """``peerreview check-fixes`` reaches the starter and nothing else does.

    The bare-path form is still a review — ``peerreview paper.pdf run/`` — so
    the new command has to be named to be reached, and naming it must not reach
    ``review``.
    """
    seen: dict = {}

    def fake_check_fixes(pdf, out, problems, **options):
        seen.update(pdf=pdf, out=out, problems=problems, options=options)
        return {'status': 'complete', 'stage': 'session'}

    monkeypatch.setattr(starter, 'check_fixes', fake_check_fixes)
    monkeypatch.setattr('sys.argv', ['peerreview', 'check-fixes', 'paper-v2.pdf', 'out',
                                     '--problems', 'p.json', '--specialist-model', 'sonnet'])
    with contextlib.redirect_stdout(io.StringIO()) as printed:
        with pytest.raises(SystemExit) as code:
            cli.run()
    assert code.value.code == 0
    assert (seen['pdf'], seen['out'], seen['problems']) == ('paper-v2.pdf', 'out', 'p.json')
    assert seen['options']['specialist_model'] == 'sonnet'
    assert json.loads(printed.getvalue())['status'] == 'complete'
    assert 'check-fixes' in names.CLI_COMMANDS
    assert 'check-fixes' in cli.build_parser().format_help()


def test_the_command_needs_a_problems_file_and_says_so(tmp_path):
    """Two refusals before anything is started, each as a code and not a traceback."""
    parser = starter.check_fixes_parser()
    assert parser.prog == 'peerreview check-fixes'
    with contextlib.redirect_stderr(io.StringIO()):
        with pytest.raises(SystemExit):
            parser.parse_args(['paper.pdf', 'out'])
    pdf = tmp_path / 'paper-v2.pdf'
    pdf.write_bytes(b'%PDF-1.7\nnot a real manuscript\n')
    with contextlib.redirect_stderr(io.StringIO()) as said:
        code = starter.check_fixes_main([str(pdf), str(tmp_path / 'out'),
                                         '--problems', str(tmp_path / 'nope.json')])
    assert code == 2
    assert 'problems file' in said.getvalue()
    with contextlib.redirect_stderr(io.StringIO()) as missing:
        code = starter.check_fixes_main([str(tmp_path / 'gone.pdf'), str(tmp_path / 'out'),
                                         '--problems', str(tmp_path / 'nope.json')])
    assert code == 2 and str(tmp_path / 'gone.pdf') in missing.getvalue()


def test_review_refuses_the_fix_arm_by_name(tmp_path):
    """The arm exists, so `--arm fix-check` has to be refused rather than run.

    It would open a run with no problems and no way to be given any, and the
    refusal names the command that takes them.
    """
    with pytest.raises(ValueError) as refused:
        starter.review(FIXTURE, tmp_path / 'out', arm=srv.FIX_ARM)
    assert 'check-fixes' in str(refused.value)
    assert srv.FIX_ARM not in [name for name in str(refused.value).split('Arms this command '
                                                                       'runs: ')[-1].split(', ')]


def test_the_check_has_its_own_ceilings(tmp_path):
    """A review's ceiling would never fire on a run this size, which is no ceiling.

    The numbers themselves are the claim: one check is a read of a paper and a
    queue of at most forty problems, so its per-agent and per-run ceilings are
    a fraction of a review's and its wall clock is under an hour.
    """
    assert starter.FIX_PER_AGENT_TOKENS < starter.PER_AGENT_TOKENS
    assert starter.FIX_PER_RUN_TOKENS < starter.PER_RUN_TOKENS
    assert starter.FIX_WALL_TIMEOUT < starter.WALL_TIMEOUT
    defaults = {action.dest: action.default
                for action in starter.check_fixes_parser()._actions}
    assert defaults['per_agent_tokens'] == starter.FIX_PER_AGENT_TOKENS
    assert defaults['per_run_tokens'] == starter.FIX_PER_RUN_TOKENS
    assert defaults['timeout'] == starter.FIX_WALL_TIMEOUT
