"""A run that could not do everything it was asked, and says which part.

Three stages of a run have a fallback behind them rather than a block: the
Read stage (the manuscript is sealed as converted when the reader fails), the
fix-check lanes (a lane that dies costs its own batch) and the code check (a
silent check leaves every finding as its reviewer filed it). Each one is
deliberate — the run is still worth writing — and each one used to be
invisible in the result, which is a different thing from deliberate.

These run through the scripted helpers, so every case here is the orchestrator
and the real tool server with Python where the model sits.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from manuscripts import pdf_with_rotations

import peerreviewagents
from peerreviewagents import names, scripted
from peerreviewagents.plugin import server as srv
from peerreviewagents.runs import read_fix_check, read_run

FIXTURE = Path(__file__).parent / 'fixtures' / 'single-column.pdf'

#: A quotation of the fixture's own draft, so a problem row is one the server
#: will read: it is matched against the old draft when the check is planned.
QUOTED = 'Converter'


def problems(count=1):
    """*count* problem rows, each quoting the fixture, as ``--problems`` holds them."""
    return [{'id': f'i{number:02d}',
             'statement': f'Problem {number} was raised against the first draft.',
             'severity': 'major',
             'evidence': [{'document': '', 'quote': QUOTED}]}
            for number in range(1, count + 1)]


def silent(slot):
    """A seat that did the work and answered nothing — the one thing returning cannot say."""
    return scripted.NO_ANSWER


def broken(slot):
    """A seat that dies where it stands, as a model that crashed does."""
    raise ValueError('this script has a bug in it')


def idle(slot):
    """A reader that reads the draft and never seals it."""
    slot.view_page(1)


def with_code(tmp_path, scripts=None):
    """A two-track review over a one-file snapshot, with no citation lane in it.

    ``tracks=['stats']`` is the point: the citation lane looks its references up
    over the network, which is not what any of these tests is about and not
    something a test should wait for.
    """
    snapshot = tmp_path / 'code'
    snapshot.mkdir()
    (snapshot / 'analysis.py').write_text('def measure(rows):\n    return len(rows)\n',
                                          encoding='utf-8')
    return peerreviewagents.scripted_review(FIXTURE, tmp_path / 'run', scripts,
                                            arm='full-two-track', tracks=['stats'],
                                            code=str(snapshot))


def endings(review):
    """``{slot: ending}`` from ``runtime.json``, where the runner's verdict lands."""
    runtime = json.loads((Path(review) / 'runtime.json').read_text(encoding='utf-8'))
    return {row['label']: row['ending'] for row in runtime['agents']}


@pytest.fixture(autouse=True)
def fresh_state():
    """One run per process, as the server holds it, and no run left attached."""
    srv.STATE['run'] = None
    srv.STATE['plugin_root'] = None
    yield
    srv.STATE['run'] = None


# -- the Read stage: a reader that failed is not a folder that arrived sealed --


def test_a_review_whose_reader_failed_is_written_and_says_which_it_was(tmp_path):
    """Sealed as converted, the run goes on, and ``read`` says the reader failed.

    The fallback is the point of the stage: a review over the converter's own
    equations is worth more than no review. What was missing is the telling —
    ``read`` was ``None`` both for a reader that died and for a folder that
    arrived with its manuscript already sealed, and those are not the same
    run.
    """
    review = peerreviewagents.scripted_review(FIXTURE, tmp_path / 'run', {'reader': silent})

    assert review.status == 'complete'
    assert review.summary['read']['reader_failed'] is True
    assert 'sealed as converted' in review.summary['read']['reason']
    assert review.summary['read']['pages_viewed'] == 0
    assert endings(review.review)['reader'] == 'no answer'
    assert read_run(tmp_path / 'run')['status'] == 'complete'


def test_a_reader_that_never_seals_is_the_same_failure_as_one_that_is_silent(tmp_path):
    """Two attempts that read and do not seal: the server seals it instead."""
    review = peerreviewagents.scripted_review(FIXTURE, tmp_path / 'run', {'reader': idle})

    assert review.status == 'complete'
    assert review.summary['read']['reader_failed'] is True
    assert review.summary['stages']['attempts']['reader'] == 2


def test_a_reader_that_dies_leaves_a_written_review_and_is_raised_again(tmp_path):
    """The lane dies, the run is written, and the helper raises the bug afterwards."""
    with pytest.raises(ValueError, match='bug in it'):
        peerreviewagents.scripted_review(FIXTURE, tmp_path / 'run', {'reader': broken})

    run = read_run(tmp_path / 'run')
    assert run['status'] == 'complete'
    assert endings(run['review'])['reader'] == 'failed'


def test_a_fix_check_whose_reader_failed_is_written_the_same_way(tmp_path):
    """A check is not blocked by its reader either: it is the same stage."""
    check = peerreviewagents.scripted_fix_check(FIXTURE, tmp_path / 'run', problems(),
                                                {'reader': silent})

    assert check.status in names.RUN_STATUSES
    assert check.summary['read']['reader_failed'] is True
    assert check.summary['status'] in ('complete', 'complete-without-findings')
    assert check.read()['checked'] == 1


def test_a_review_of_a_sealed_folder_has_no_read_stage_at_all(tmp_path):
    """The other arm of the same field: ``None`` means there was no Read stage.

    The arm is the one ``scripted_read`` left in the folder, because two arms
    are never pooled in one run folder.
    """
    folder = tmp_path / 'run'
    peerreviewagents.scripted_read(FIXTURE, folder)

    review = peerreviewagents.scripted_review(FIXTURE, folder, arm='single')

    assert 'reader' not in review.ran
    assert review.summary['read'] is None


# -- the lanes: a checker that answered nothing, with the cause against it ----


def test_a_silent_lane_leaves_its_problems_not_checked_with_the_cause(tmp_path):
    """Not checked is an answer; an answer with no cause is half of one."""
    check = peerreviewagents.scripted_fix_check(FIXTURE, tmp_path / 'run', problems(),
                                                {'fix-check': silent})

    assert check.status == 'complete-without-findings'
    written = check.read()
    assert (written['checked'], written['not_checked']) == (0, 1)
    assert written['incomplete'] is True
    assert '1 of 1 problem(s) have no verdict' in written['incomplete_reason']
    assert written['unchecked'][0]['lane'] == 'fix-1'
    assert 'fix-1' in written['unchecked'][0]['reason']
    fixes = Path(written['reports']['markdown']).read_text(encoding='utf-8')
    assert 'This check is incomplete' in fixes
    assert 'Why there is no verdict' in fixes
    assert 'fix-1' in fixes


def test_a_check_with_one_silent_lane_is_complete_and_still_not_whole(tmp_path):
    """The case a plain status cannot carry: ten problems answered, one not.

    Eleven problems is two lanes, because the server deals them in batches of
    ten. The first lane answers its batch, so the check is written with
    verdicts in it and the status is ``complete`` — and one problem has no
    verdict, which only ``incomplete`` says.
    """
    check = peerreviewagents.scripted_fix_check(FIXTURE, tmp_path / 'run', problems(11),
                                                {'fix-2': silent})

    assert check.status == 'complete'
    written = check.read()
    assert (written['checked'], written['not_checked']) == (10, 1)
    assert written['incomplete'] is True
    assert written['unchecked'][0]['lane'] == 'fix-2'
    assert 'fix-2' in written['incomplete_reason']
    assert check.summary['incomplete'] is True
    assert check.summary['incomplete_reason'] == written['incomplete_reason']


def test_a_lane_that_died_holding_its_batch_names_itself_in_the_cause(tmp_path):
    """A lane that crashed costs its batch, and the written check says whose."""
    with pytest.raises(ValueError, match='bug in it'):
        peerreviewagents.scripted_fix_check(FIXTURE, tmp_path / 'run', problems(),
                                            {'fix-check': broken})

    written = read_fix_check(tmp_path / 'run')
    assert written['incomplete'] is True
    assert written['unchecked'][0]['lane'] == 'fix-1'
    assert 'fix-1' in written['unchecked'][0]['reason']


def test_a_whole_check_is_not_incomplete(tmp_path):
    """The other arm: every problem answered, and nothing claims a gap."""
    check = peerreviewagents.scripted_fix_check(FIXTURE, tmp_path / 'run', problems(2))

    written = check.read()
    assert (written['checked'], written['not_checked']) == (2, 0)
    assert written['incomplete'] is False
    assert written['incomplete_reason'] is None
    assert 'This check is incomplete' not in \
        Path(written['reports']['markdown']).read_text(encoding='utf-8')


# -- the code check: a silent lane is not an absent one ----------------------


def test_a_silent_code_check_is_in_the_result_rather_than_missing_from_it(tmp_path):
    """A review whose code check answered nothing says so, and keeps its findings."""
    review = with_code(tmp_path, {'code-check': silent})

    assert review.status == 'complete'
    assert review.summary['code_check']['silent'] is True
    assert review.summary['code_check']['slot'] == 'code-check'
    assert 'stands as its reviewer filed it' in review.summary['code_check']['reason']
    # The findings are still the reviewers' own, which is what a silent check
    # means: the stage only ever takes findings out.
    assert read_run(tmp_path / 'run')['counts']['findings'] == 1


def test_a_code_check_that_answered_says_what_it_answered(tmp_path):
    """The other arm, so ``silent`` is a fact about the run and not about the key."""
    review = with_code(tmp_path)

    assert review.summary['code_check']['silent'] is False


def test_a_review_with_no_code_lane_has_no_code_check_at_all(tmp_path):
    """``None`` is the arm that was never given any code."""
    review = peerreviewagents.scripted_review(FIXTURE, tmp_path / 'run')

    assert review.summary['code_check'] is None


# -- the ceilings: a seat that spent more than it was given -------------------


def test_a_seat_cut_off_over_its_ceiling_before_it_reported_blocks_the_run(tmp_path):
    """What a script can now say: this seat spent more than its ceiling.

    A real session's child is cut off mid-stream the moment it goes over, so
    whatever it was about to answer is lost with it; the orchestrator retries
    the seat once and then blocks the run. A scripted seat that declares the
    same spend gets the same ending, which is how a consumer builds a run
    stopped over a ceiling without a model.
    """
    def greedy(slot):
        slot.whole_paper()
        slot.spend(40_000)

    review = peerreviewagents.scripted_review(FIXTURE, tmp_path / 'run', {'core-1': greedy},
                                              per_agent_tokens=10_000)

    assert review.status == 'blocked'
    assert 'core-1' in review.blocked_reason
    assert endings(review.review)['core-1'] == 'over the token ceiling'
    assert review.summary['stages']['attempts']['core-1'] == 2


def test_a_seat_that_had_already_reported_keeps_its_work(tmp_path):
    """Over the ceiling is a stop, not an erasure, and the ledger is the record.

    The ceiling catches a seat mid-flight, and this one had already filed its
    finding and closed its slot when it went over: its answer is dropped, the
    stage finds the slot closed in the ledger rather than retrying it, and the
    run carries on from what is on disk. Exactly what a real session does when
    the child is killed after its report reached the server.
    """
    def greedy(slot):
        scripted.DEFAULT_SCRIPTS['core'](slot)
        slot.spend(40_000)

    review = peerreviewagents.scripted_review(FIXTURE, tmp_path / 'run', {'core-1': greedy},
                                              per_agent_tokens=10_000)

    assert review.status == 'complete'
    assert endings(review.review)['core-1'] == 'over the token ceiling'
    assert review.summary['stages']['attempts']['core-1'] == 1
    assert review.summary['tokens_by_agent']['core-1'] == 40_000
    assert read_run(tmp_path / 'run')['counts']['findings'] == 1


def test_a_seat_under_its_ceiling_is_metered_at_what_it_said_it_spent(tmp_path):
    """The declared spend is the run's spend, so a budget row is a real number."""
    def thrifty(slot):
        slot.spend(4_321)
        scripted.DEFAULT_SCRIPTS['core'](slot)

    review = peerreviewagents.scripted_review(FIXTURE, tmp_path / 'run', {'core-1': thrifty},
                                              per_agent_tokens=10_000)

    assert review.status == 'complete'
    assert review.summary['tokens_by_agent']['core-1'] == 4_321
    # Every other seat is metered at the run's flat number, as before.
    assert review.summary['tokens_by_agent']['reader'] == 1_000


def test_a_run_with_its_ceiling_spent_hands_out_the_smallest_one_not_none(tmp_path):
    """The per-run ceiling, once spent, is not a licence to spend anything.

    Zero on the task means *no* ceiling, so a run with nothing left used to
    hand the next seat an unlimited one — the exact opposite of what a per-run
    ceiling is for, and the one case where it mattered. The reader spends the
    whole of it, and every seat after it is over its ceiling before it starts.
    These two had each closed their slot by then, so the run is written from
    the ledger rather than blocked: what the ceiling changed is that they were
    stopped at all.
    """
    review = peerreviewagents.scripted_review(FIXTURE, tmp_path / 'run', per_run_tokens=1_000)

    ran = endings(review.review)
    assert ran['reader'] == 'answered'
    assert ran['core-1'] == ran['editor'] == 'over the token ceiling'
    assert review.status == 'complete'


def test_the_ceilings_still_do_nothing_when_the_plan_sets_none(tmp_path):
    """A scripted run with no ceilings in its plan is the run it always was."""
    review = peerreviewagents.scripted_review(FIXTURE, tmp_path / 'run',
                                              per_agent_tokens=0, per_run_tokens=0, tokens=50_000)

    assert review.status == 'complete'
    assert review.summary['tokens_total'] == 150_000  # three seats at the flat number


# -- the folder on its own: a consumer opens it later ------------------------
#
# Everything above is read from what the call returned. A consumer opens a
# folder days later with no call to return anything, so every one of these
# facts has to be on disk and in the reading of it.


def test_a_folder_whose_reader_failed_says_so_without_the_return_value(tmp_path):
    """``read_run``'s own ``read`` block, from ``runtime.json``.

    The reader failed and the draft was sealed as the converter read it, which
    is a fact about every finding in the folder. It used to live only in what
    ``review`` returned, so a consumer opening the folder afterwards had to
    infer it from the manuscript's manifest and a row in the agent table.
    """
    review = peerreviewagents.scripted_review(FIXTURE, tmp_path / 'run', {'reader': silent})

    runtime = json.loads((Path(review.review) / 'runtime.json').read_text(encoding='utf-8'))
    assert runtime['read']['reader_failed'] is True

    read = read_run(tmp_path / 'run')['read']
    assert read['reader_failed'] is True
    assert 'sealed as converted' in read['reason']
    assert read['sealed'] is True
    assert read['model']
    assert read['pages_viewed'] == 0
    # Two readers were started and neither answered, which is what the stage
    # costs when it fails: the count used to be `None` on exactly this run,
    # because the fallback the server builds the record from has no number in
    # it. It is the budget's own tally now.
    assert read['attempts'] == 2


def test_a_folder_whose_reader_worked_says_that_too(tmp_path):
    """The other arm, so ``reader_failed`` is a fact about the run, not the key."""
    peerreviewagents.scripted_review(FIXTURE, tmp_path / 'run')

    read = read_run(tmp_path / 'run')['read']
    assert read['reader_failed'] is False
    assert read['reason'] is None
    assert read['pages_viewed'] >= 1
    assert read['attempts'] == 1


def test_the_read_block_counts_a_reader_that_failed_and_then_worked(tmp_path):
    """``attempts`` is readers started, not whether the stage ended well.

    The two arms above each have one number to report. This is the run that
    tells them apart: the first reader said nothing, the second sealed the
    manuscript, and the folder holds both facts — a Read stage that cost two
    launches and a manuscript that is the reader's after all.
    """
    started = []

    def once(slot):
        """Silent the first time it is called, a working reader the second."""
        started.append(slot.slot)
        if len(started) == 1:
            return scripted.NO_ANSWER
        return scripted.DEFAULT_SCRIPTS['reader'](slot)

    peerreviewagents.scripted_review(FIXTURE, tmp_path / 'run', {'reader': once})

    read = read_run(tmp_path / 'run')['read']
    assert read['reader_failed'] is False
    assert read['sealed'] is True
    assert read['attempts'] == 2


def test_a_folder_that_arrived_sealed_has_no_read_block_at_all(tmp_path):
    """``None`` is the run that planned no reader, as in the returned summary."""
    folder = tmp_path / 'run'
    peerreviewagents.scripted_read(FIXTURE, folder)

    peerreviewagents.scripted_review(FIXTURE, folder, arm='single')

    assert read_run(folder)['read'] is None


def test_a_fix_checks_folder_says_its_reader_failed_too(tmp_path):
    """The same block on the other reading: the verdicts were reached on that text."""
    peerreviewagents.scripted_fix_check(FIXTURE, tmp_path / 'run', problems(),
                                        {'reader': silent})

    read = read_fix_check(tmp_path / 'run')['read']
    assert read['reader_failed'] is True
    assert 'sealed as converted' in read['reason']


def test_a_silent_code_check_is_silent_in_the_folder_as_well(tmp_path):
    """``silent`` was only in what the call returned, and the counts cannot say it.

    A lane that reported and settled nothing of its queue and a lane that never
    reported at all leave the same ``checked: 0``, and they are not the same
    run to come back to.
    """
    with_code(tmp_path, {'code-check': silent})

    assert read_run(tmp_path / 'run')['code_check']['silent'] is True


def test_a_code_check_that_reported_is_not_silent_in_the_folder(tmp_path):
    """The other arm of the same key."""
    with_code(tmp_path)

    assert read_run(tmp_path / 'run')['code_check']['silent'] is False


def test_a_check_with_no_document_still_has_a_row_for_every_problem(tmp_path):
    """Every unchecked problem carries its reason, document or no document.

    A check that stopped before ``write_fix_check`` — the reader could not seal
    the new draft, so no lane was ever started — has no written check to read
    the rows out of, and ``not_checked: 3`` with ``unchecked: []`` is a count
    nobody can act on. The rows are in ``progress.json`` now, which is what a
    folder missing its document is read from.
    """
    check = peerreviewagents.scripted_fix_check(FIXTURE, tmp_path / 'run', problems(3),
                                                {'fix-check': silent})
    Path(check.read()['reports']['fix_check']).unlink()

    written = read_fix_check(tmp_path / 'run')
    assert written['not_checked'] == 3
    assert len(written['unchecked']) == 3
    assert {row['lane'] for row in written['unchecked']} == {'fix-1'}
    assert all('fix-1' in row['reason'] for row in written['unchecked'])


# -- the run that could not read at all: blocked in the Read stage -----------
#
# The runs above all sealed something: the reader did it or the server sealed
# what the converter read. A paper with a page the converter returned nothing
# from has neither — the fallback would write a manuscript missing a page, so
# it is refused — and the run blocks there. It is the only run whose Read stage
# failed twice over, so it is where `reader_failed` with no seal is readable,
# and it is built from a real PDF rather than by deleting a file afterwards.


def lost_page(tmp_path):
    """A two-page paper whose second page the pinned converter reads nothing from."""
    return pdf_with_rotations(tmp_path / 'turned.pdf', [0, 90])


def test_a_review_blocked_because_nothing_could_be_sealed_says_the_reader_failed(tmp_path):
    """``reader_failed`` with ``sealed: False``, and the reason for both.

    The run stops in its Read stage, so nothing downstream writes a record
    over it: the folder used to fall back to the block in ``progress.json``,
    which reads as ``reader_failed: False`` with no reason at all — a run whose
    reader never answered, filed as a run whose reader was fine.
    """
    review = peerreviewagents.scripted_review(lost_page(tmp_path), tmp_path / 'run',
                                              {'reader': silent})

    assert review.status == 'blocked'
    read = read_run(tmp_path / 'run')['read']
    assert read['reader_failed'] is True
    assert read['sealed'] is False
    assert 'did not seal' in read['reason']
    assert 'page(s) 2' in read['reason'] and 'rotated 90' in read['reason']
    # Both readers were started and the fallback was refused after them, so the
    # stage cost two launches and sealed nothing.
    assert read['attempts'] == 2
    assert read['pages'] == 2 and read['pages_viewed'] == 0
    assert read['model']


def test_a_blocked_fix_check_keeps_the_cause_once_and_points_its_rows_at_it(tmp_path):
    """The whole cause on the check, a short reason on every row.

    A check blocked before its lanes started has one cause under every problem
    it holds, and that cause is a paragraph: which reader failed, which page
    the converter lost, what to do about it. Printed again under each of twenty
    problems it is still one fact, so it is kept once as ``stopped`` and each
    row says that the run stopped and where to read why.
    """
    rows = [dict(row, evidence=[{'document': '', 'quote': 'upright'}])
            for row in problems(3)]
    check = peerreviewagents.scripted_fix_check(lost_page(tmp_path), tmp_path / 'run', rows,
                                                {'reader': silent})

    assert check.status == 'blocked'
    found = read_fix_check(tmp_path / 'run')
    assert found['read']['reader_failed'] is True
    assert found['read']['sealed'] is False

    # Once, in full, where a consumer reads why the run ended.
    assert 'page(s) 2' in found['stopped']

    # And three rows that point there, rather than three copies of it.
    assert found['not_checked'] == 3 and len(found['unchecked']) == 3
    for row in found['unchecked']:
        assert row['lane'] == 'fix-1'
        assert row['reason'] == ('fix-1 never claimed its batch, so nothing read the draft '
                                 'against it; the run stopped first, and stopped says why')
    assert 'page(s) 2' not in found['incomplete_reason']
    assert found['incomplete_reason'].startswith('3 of 3 problem(s) have no verdict')


def test_a_blocked_review_says_how_each_missing_seat_ended(tmp_path):
    """The ending, not the retry: a seat over its ceiling is not a silent one.

    Every seat that returns nothing twice blocks the run the same way, and the
    reason used to be the same sentence for all of them — "these stages
    returned nothing twice" — which names what the orchestrator did and not
    what happened to the seat.
    """
    def greedy(slot):
        slot.whole_paper()
        slot.spend(40_000)

    review = peerreviewagents.scripted_review(FIXTURE, tmp_path / 'run', {'core-1': greedy},
                                              per_agent_tokens=10_000)

    assert review.status == 'blocked'
    assert 'core-1 (over the token ceiling)' in review.blocked_reason
    assert read_run(tmp_path / 'run')['cause'] == review.blocked_reason


def test_a_seat_that_truly_said_nothing_reads_as_that(tmp_path):
    """The plain case keeps its own words, so the ending is the thing that varies."""
    review = peerreviewagents.scripted_review(FIXTURE, tmp_path / 'run', {'core-1': silent})

    assert review.status == 'blocked'
    assert 'core-1 (returned nothing)' in review.blocked_reason


def test_a_run_stopped_at_its_own_ceiling_says_the_ceiling_in_the_folder(tmp_path):
    """The starter's stop case, built by a script rather than written by hand.

    The per-run ceiling is spent, so the seat is not started again — and that
    is the fact a consumer needs: the stage is missing because the *run* ran
    out, not because this seat is a bad one. Both halves are in the reason now,
    the seat's own ending and the stop that followed it.
    """
    def greedy(slot):
        slot.whole_paper()
        slot.spend(40_000)

    review = peerreviewagents.scripted_review(FIXTURE, tmp_path / 'run', {'core-1': greedy},
                                              per_run_tokens=5_000)

    assert review.status == 'blocked'
    assert 'core-1 (over the token ceiling, and nothing was started again)' in \
        review.blocked_reason
    assert 'this run reached its token ceiling of 5,000' in review.blocked_reason
    assert review.summary['stages']['attempts']['core-1'] == 1
    run = read_run(tmp_path / 'run')
    assert run['status'] == 'blocked'
    assert run['cause'] == review.blocked_reason
    assert run['runtime']['per_run_token_ceiling'] == 5_000
