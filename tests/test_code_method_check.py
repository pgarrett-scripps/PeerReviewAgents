"""A finding that rests on a method is read against the code before it counts.

The case this file exists for is a real one. In the workbench run of 2026-11,
finding 33 of the spec-numbers lane said a bootstrap limit of 0.33 "cannot
arise from a mean over 4 resolved papers", because every resample keeps the
four papers that have a score. The repository's own bootstrap resamples all
eight assigned papers and averages whichever of them have a score, so a third
is one of the values it can produce. The arithmetic was right, the procedure
under it was guessed, the finding was stored as ``computed`` — and the
synthesis put it into a required change with a receipt beside it. Of the 22
fact-checkable findings in that run graded by hand, it was the only wrong one,
and it was wrong in the one way a receipt cannot catch.

So on a run that was given code, between the reviewers and the synthesis, the
findings that assert or assume a method go to a lane that can read the code:
every ``computed`` finding, plus every finding its reviewer marked with
``assumed_method`` instead of guessing. Each gets one of three answers —
confirmed, contradicted, or the code does not show it — and a contradicted
finding is kept out of the issues and kept on the record with the passage of
the code that disproved it.

What these tests pin:

* the real case end to end: the 0.33 finding, the resample stanza that
  contradicts it, the finding absent from the issues, and the author's own
  record of why it was thrown out in REVIEW.md;
* a confirmed finding keeps its place and carries the code quote;
* "the code does not show it" leaves the finding exactly as filed and says
  what was searched;
* confirmed and contradicted are refused without the verified passage, and
  not-shown without the searches;
* the queue is capped, and what the cap left alone is counted rather than
  quietly dropped;
* a run given no code plans no check at all.

Nothing here starts Claude Code or any provider, and no model is called. The
fixture repository is written by this file — the resample stanza is a copy of
the function from the paper's repository, not a read of it — and every finding
and verdict below is submitted through the same tools an agent calls.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from manuscripts import seal_converted

from peerreviewagents import read_run
from peerreviewagents.engine.run_report import report_blocks
from peerreviewagents.plugin import server as srv

FIXTURE = Path(__file__).parent / 'fixtures' / 'single-column.pdf'

#: The fixture repository. ``bootstrap`` is the real function's resample loop,
#: copied here so the test owns its own code: it resamples every row of each
#: domain and averages only the rows that have a score, which is exactly why a
#: limit of a third can come out of a study with four scored papers.
CODE_FILES = {
    'analysis/scripts/analyze_synthesis_study.py': (
        '"""The synthesis study: paper-level preference, bootstrapped by domain."""\n'
        '\n'
        'import random\n'
        'from collections import defaultdict\n'
        '\n'
        '\n'
        'def bootstrap(records, iterations=2000, seed=11):\n'
        '    """The interval the paper reports, resampled within each domain."""\n'
        '    by_domain = defaultdict(list)\n'
        '    for row in records:\n'
        '        by_domain[row["domain"]].append(row)\n'
        '    generator = random.Random(seed)\n'
        '    estimates = []\n'
        '    for _ in range(iterations):\n'
        '        sample = []\n'
        '        for domain in sorted(by_domain):\n'
        '            rows = by_domain[domain]\n'
        '            sample.extend(generator.choice(rows) for _ in rows)\n'
        '        scores = [row["score"] for row in sample if row["score"] is not None]\n'
        '        if scores:\n'
        '            estimates.append(sum(scores) / len(scores))\n'
        '    estimates.sort()\n'
        '    low = estimates[int(0.025 * len(estimates))]\n'
        '    high = estimates[int(0.975 * len(estimates))]\n'
        '    return low, high\n'
    ),
    'analysis/scripts/thresholds.py': (
        'FDR_THRESHOLD = 0.05\n'
        'MIN_SCORED = 1\n'
    ),
}

#: The lines of the resample loop that decide the real case: the sample is
#: every assigned row of every domain, and the mean is taken over whichever of
#: them have a score.
RESAMPLE = ('            rows = by_domain[domain]',
            '            estimates.append(sum(scores) / len(scores))')

CODES: dict = {}


@pytest.fixture(autouse=True)
def fresh_state():
    srv.STATE['run'] = None
    srv.STATE['plugin_root'] = None
    CODES.clear()
    yield
    srv.STATE['run'] = None


@pytest.fixture
def code_folder(tmp_path):
    """The fixture repository on disk, as a folder a run can be given."""
    folder = tmp_path / 'synthesis-code'
    for name, text in CODE_FILES.items():
        path = folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        # `newline=''` so the bytes on disk are the bytes of the literal: on
        # Windows the default would turn each \n into two and shift nothing
        # here, but a line count asserted elsewhere would be off.
        path.write_text(text, encoding='utf-8', newline='')
    return folder


def open_run(tmp_path, **kwargs):
    opened = srv.ingest(str(FIXTURE), str(tmp_path / 'run'), **kwargs)
    assert 'request_rejected' not in opened, opened
    CODES.update({row['slot']: row['claim_code'] for row in opened.get('claim_codes', [])})
    CODES['recorder_key'] = opened.get('recorder_key', '')
    # ingest leaves a draft, because the Read stage writes the lines. No model
    # reads anything here, so the run is sealed as converted.
    seal_converted(opened)
    return opened


def claim(slot):
    """Claim one slot with its own code, and read the paper as its agent does."""
    taken = srv.claim_agent(slot, CODES.get(slot, ''))
    assert 'agent_token' in taken, taken
    CODES.pop(slot, None)
    run = srv.current()
    if 'read_paper' in run.agent_for(taken['agent_token']).allowed:
        for part in range(1, srv.paper_view.served(run.base) + 1):
            assert isinstance(srv.read_paper(taken['agent_token'], part=part), str)
    return taken['agent_token']


def quote_pool(run, wanted):
    """*wanted* passages of the manuscript that each occur in it exactly once."""
    whole = run.base.text
    pool = []
    for line in range(1, run.base.line_count() + 1):
        for piece in run.base.line_text(line).split('. '):
            piece = piece.strip()
            if 30 < len(piece) < 160 and whole.count(piece) == 1:
                pool.append((line, piece))
    assert len(pool) >= wanted, f'this fixture holds {len(pool)} anchorable passages'
    return pool[:wanted]


def numeric_quote(run):
    """An anchorable line of the paper that prints a number, with that number.

    Never a power of ten: those are the unit conversions a receipt may carry
    without citing the paper, so a receipt over one proves nothing about
    whether the number was matched against the page.
    """
    whole = run.base.text
    for line in range(1, run.base.line_count() + 1):
        text = run.base.line_text(line).strip()
        numbers = [one for one in re.findall(srv.NUMERIC, text)
                   if srv.as_number(one) is not None
                   and not srv.is_power_of_ten(srv.as_number(one))]
        if numbers and 30 < len(text) < 200 and whole.count(text) == 1:
            return line, text, numbers[0]
    raise AssertionError('no anchorable passage with a number in this fixture')


def line_of(path, needle):
    """The line *needle* is on in the fixture file at *path*."""
    for number, line in enumerate(CODE_FILES[path].splitlines(), start=1):
        if needle in line:
            return number
    raise AssertionError(f'{needle} is not in the fixture {path}')


def code_evidence(first, last, path='analysis/scripts/analyze_synthesis_study.py'):
    """The four arguments a verdict carries, for a range of the fixture's lines."""
    start, end = line_of(path, first), line_of(path, last)
    lines = CODE_FILES[path].splitlines()
    return {'code_path': path, 'code_start_line': start, 'code_end_line': end,
            'code_quote': '\n'.join(lines[start - 1:end])}


def the_real_finding(run, token):
    """Finding 33, as it was filed: right arithmetic over a guessed procedure."""
    line, quote, number = numeric_quote(run)
    receipt = srv.calculate(token, f'{number} - {number}')['receipt']
    stored = srv.submit_finding(
        token, title='A bootstrap limit of 0.33 cannot arise from four resolved papers',
        line=line, quote=quote,
        comment='The lower limit of 0.33 is not a value a mean over the four resolved papers '
                'can take: with scores in {-1, 0, 1} the possible means are quarters, and a '
                'third is not one of them. Either the interval or the count is wrong.',
        severity='major', calculate_receipt=receipt)
    assert stored.get('stored'), stored
    assert stored['basis'] == 'computed', stored
    return stored['finding_uid']


def plain_finding(run, token, line, quote, title='The abstract states it without the caveat'):
    stored = srv.submit_finding(token, title=title, line=line, quote=quote,
                                comment='The abstract carries the claim without the hedge the '
                                        'discussion puts on it.',
                                severity='moderate')
    assert stored.get('stored'), stored
    return stored['finding_uid']


def close_every_stage(run, done=()):
    """Close the stages nothing else in the test closed, so the review can be written."""
    for slot in run.required:
        if slot in done or slot == 'editor':
            continue
        closed = srv.submit_report(claim(slot), 'Nothing further.', none_found=True,
                                   model=run.models.get(slot),
                                   **({'report_markdown': '# Nothing further.'}
                                      if slot.startswith('spec-') else {}))
        assert closed.get('closed'), closed


def write_the_review(run, editor, issues, done=()):
    """The editor's close and the recorder's write, with nothing left open."""
    close_every_stage(run, done=done)
    answer = srv.submit_issues(editor, issues)
    assert answer.get('stored') == len(issues), answer
    accepted = srv.submit_editor(
        editor,
        summary='The panel agrees on where the paper overreaches.',
        significance='Would matter to the field if the central comparison holds.',
        strength_of_evidence='Partly supported.',
        verdict_reasoning='The claim is plausible and the evidence shown does not reach it.',
        escalation_rulings=[],
        limitations=['The raw measurements were not available to the panel.'])
    assert accepted.get('accepted'), accepted
    assert srv.submit_report(editor, 'The pooled review is above.',
                             model=run.models.get('editor')).get('closed')
    written = srv.write_review(claim('recorder'))
    assert written['status'] == 'complete', written
    return written


def rendered_review(run):
    """REVIEW.md as text, and the blocks it was rendered from."""
    return (run.review / 'REVIEW.md').read_text(encoding='utf-8'), report_blocks(run.review)


# -- the real case -----------------------------------------------------------


def test_the_bootstrap_finding_is_contradicted_by_the_code_and_shown_as_thrown_out(
        tmp_path, code_folder):
    """The whole path, on the finding that went wrong in the workbench run.

    A receipt over the printed numbers made it ``computed``, which is the one
    thing that made it look checked; the code behind it says the resample draws
    every assigned row and averages whichever have a score. So it is
    contradicted, it may not enter an issue, and the author is told it was
    raised and why it was dropped — with the passage of their own code.
    """
    open_run(tmp_path, code=str(code_folder))
    run = srv.current()
    assert run.code_check_slots == (srv.CHECK_TRACK,)

    reviewer = claim('core-1')
    guessed = the_real_finding(run, reviewer)
    line, quote = quote_pool(run, 3)[2]
    stands = plain_finding(run, reviewer, line, quote)
    assert srv.submit_report(reviewer, 'Two findings, one of them arithmetic.',
                             model=run.models.get('core-1')).get('closed')

    # The check runs on the code lane's model, because it is that lane again
    # with the findings in front of it instead of the paper.
    assert run.models[srv.CHECK_TRACK] == run.models['spec-code']
    checker = claim(srv.CHECK_TRACK)
    queued = srv.method_claims(checker)
    assert queued['count'] == 1 and queued['cap'] == srv.MAX_CODE_CHECKS
    assert queued['over_cap'] == 0
    claimed = queued['claims'][0]
    assert claimed['finding_uid'] == guessed
    assert claimed['basis'] == 'computed'
    assert 'says nothing about the procedure' in claimed['why_queued']
    assert [row['finding_uid'] for row in queued['claims']] == [guessed], (
        'a finding with no method behind it is not the check\'s business')

    evidence = code_evidence(*RESAMPLE)
    answered = srv.submit_code_check(
        checker, guessed, 'contradicted', **evidence,
        note='The bootstrap resamples all eight assigned papers and averages whichever of them '
             'have a score, so the number of scored papers varies between resamples and a mean '
             'of a third is one of the values it can take.')
    assert answered['stored'] is True, answered
    assert answered['verdict'] == 'contradicted'
    assert answered['code_evidence']['path'] == evidence['code_path']
    assert 'generator.choice(rows)' in answered['code_evidence']['text']
    assert len(answered['code_evidence']['sha256']) == 64
    assert 'out of the issues' in answered['effect']
    assert answered['claims_left'] == 0
    assert srv.submit_report(checker, 'One claim checked, one contradicted.',
                             model=run.models.get(srv.CHECK_TRACK)).get('closed')

    # The editor is not asked to be careful about it: it is not in the pool it
    # groups, and it is named as contradicted rather than silently absent.
    editor = claim('editor')
    listed = srv.list_findings(editor)
    assert [one['finding_uid'] for one in listed['findings']] == [stands]
    thrown_out = listed['contradicted_by_code']
    assert [one['finding_uid'] for one in thrown_out] == [guessed]
    assert 'generator.choice(rows)' in thrown_out[0]['code']['text']
    assert 'resamples all eight assigned papers' in thrown_out[0]['note']
    assert 'does not do what the finding says' in listed['contradicted_reason']

    issue = {'name': 'The abstract drops the hedge', 'severity': 'moderate',
             'category': 'claims-and-conclusions', 'rank': 1,
             'statement': 'The abstract states the result without the caveat the discussion '
                          'puts on it, and the authors have to say which they mean.',
             'members': [stands, guessed]}
    refused = srv.submit_issues(editor, [issue])
    assert not refused.get('stored'), refused
    assert any(guessed in problem and 'contradicted by the code' in problem
               for problem in refused['problems']), refused
    assert any('already on record' in problem for problem in refused['problems']), refused

    write_the_review(run, editor, [{**issue, 'members': [stands]}],
                     done=('core-1', srv.CHECK_TRACK))

    document = json.loads((run.review / 'issues.json').read_text(encoding='utf-8'))
    members = [member['finding_uid'] for one in document['issues']
               for member in one['members']]
    assert members == [stands], 'a contradicted finding is in no issue of the review'
    record = document['code_check']
    assert record['checked'] == 1 and record['over_cap'] == 0
    assert record['counts'] == {'confirmed': 0, 'contradicted': 1, 'not-shown': 0}

    # It was really filed, and the run says so: out of the issues is not out of
    # the record, and the author can see what was raised and thrown out.
    report, blocks = rendered_review(run)
    assert '## Checked against the code' in report
    assert 'Thrown out: the code does not do what the finding said' in report
    assert 'resamples all eight assigned papers' in report
    assert 'generator.choice(rows)' in report
    assert 'analysis/scripts/analyze_synthesis_study.py lines' in report
    assert ('heading2', 'Checked against the code') in blocks
    stored_row = json.loads(
        (run.review / 'code-checks.jsonl').read_text(encoding='utf-8').strip())
    assert stored_row['finding_uid'] == guessed and stored_row['verdict'] == 'contradicted'
    assert stored_row['track'] == srv.CHECK_TRACK


# -- the other two answers ---------------------------------------------------


def test_a_marked_method_the_code_confirms_keeps_its_place_and_carries_the_quote(
        tmp_path, code_folder):
    """``assumed_method`` is the mark the briefs ask for instead of a guess.

    A reviewer that says which step it took for granted gets that step read
    against the code — and a confirmed finding is a finding like any other,
    with the code quote shown beside it so the author can see the method claim
    was not taken on trust.
    """
    open_run(tmp_path, code=str(code_folder))
    run = srv.current()
    reviewer = claim('core-1')
    line, quote = quote_pool(run, 1)[0]
    stored = srv.submit_finding(
        agent_token=reviewer, title='The scored-paper threshold is one paper', line=line,
        quote=quote,
        comment='A single scored paper is enough for a domain to contribute a mean, which the '
                'methods never state.',
        severity='moderate',
        assumed_method='that a domain with one scored paper still contributes a mean')
    assert stored.get('stored'), stored
    assert stored['basis'] != 'computed', 'this one is queued by its mark, not by a receipt'
    uid = stored['finding_uid']

    checker = claim(srv.CHECK_TRACK)
    queued = srv.method_claims(checker)
    assert [row['finding_uid'] for row in queued['claims']] == [uid]
    assert queued['claims'][0]['assumed_method'].startswith('that a domain with one')
    assert queued['claims'][0]['why_queued'] == 'its reviewer marked the method it assumed'

    evidence = code_evidence('MIN_SCORED = 1', 'MIN_SCORED = 1',
                             path='analysis/scripts/thresholds.py')
    answered = srv.submit_code_check(checker, uid, 'confirmed', **evidence,
                                     note='One scored paper is the threshold the code sets, so '
                                          'the finding describes what it does.')
    assert answered['stored'] is True and answered['verdict'] == 'confirmed'
    assert 'The finding stands' in answered['effect']
    assert srv.submit_report(checker, 'One claim checked and confirmed.',
                             model=run.models.get(srv.CHECK_TRACK)).get('closed')
    assert srv.submit_report(reviewer, 'One finding.',
                             model=run.models.get('core-1')).get('closed')

    editor = claim('editor')
    listed = srv.list_findings(editor)
    assert [one['finding_uid'] for one in listed['findings']] == [uid]
    assert listed['contradicted_by_code'] == []
    issue = {'name': 'The threshold is never stated', 'severity': 'moderate',
             'category': 'methods-and-reporting', 'rank': 1,
             'statement': 'The methods do not state how few scored papers a domain may have '
                          'and still contribute a mean.',
             'members': [uid]}
    write_the_review(run, editor, [issue], done=('core-1', srv.CHECK_TRACK))

    report, _ = rendered_review(run)
    assert 'Confirmed by the code' in report
    assert 'MIN_SCORED = 1' in report
    assert 'Thrown out' not in report
    document = json.loads((run.review / 'issues.json').read_text(encoding='utf-8'))
    assert [member['finding_uid'] for one in document['issues']
            for member in one['members']] == [uid]
    assert document['code_check']['counts']['confirmed'] == 1


def test_a_claim_the_code_does_not_settle_leaves_the_finding_exactly_as_filed(
        tmp_path, code_folder):
    """The third answer, and the one that must not read as either of the others.

    The finding stands, the review says the code was looked at and did not
    settle it, and the searches are on the record — because "the code does not
    show it" is a claim about what was looked for.
    """
    open_run(tmp_path, code=str(code_folder))
    run = srv.current()
    reviewer = claim('core-1')
    line, quote = quote_pool(run, 1)[0]
    stored = srv.submit_finding(
        agent_token=reviewer, title='The exclusion rule is never given', line=line, quote=quote,
        comment='Which records were dropped before the mean is not stated anywhere.',
        severity='moderate', assumed_method='that records with no score were dropped silently')
    uid = stored['finding_uid']
    assert srv.submit_report(reviewer, 'One finding.',
                             model=run.models.get('core-1')).get('closed')

    checker = claim(srv.CHECK_TRACK)
    assert srv.method_claims(checker)['count'] == 1
    answered = srv.submit_code_check(
        checker, uid, 'not-shown',
        searches=['"drop" across analysis/scripts', '"exclude" across analysis/scripts',
                  'read analysis/scripts/analyze_synthesis_study.py whole'],
        note='Nothing in the two scripts of the snapshot drops a record before the mean; the '
             'records arrive already built and the code that builds them is not here.')
    assert answered['stored'] is True and answered['verdict'] == 'not-shown'
    assert answered['code_evidence'] is None
    assert len(answered['searches']) == 3
    assert 'stands as its reviewer filed it' in answered['effect']
    assert srv.submit_report(checker, 'One claim, not settled by the code.',
                             model=run.models.get(srv.CHECK_TRACK)).get('closed')

    editor = claim('editor')
    assert srv.list_findings(editor)['contradicted_by_code'] == []
    issue = {'name': 'The exclusion rule is never given', 'severity': 'moderate',
             'category': 'methods-and-reporting', 'rank': 1,
             'statement': 'The methods never say which records were dropped.',
             'members': [uid]}
    write_the_review(run, editor, [issue], done=('core-1', srv.CHECK_TRACK))

    report, _ = rendered_review(run)
    assert 'The code does not settle it' in report
    assert '"exclude" across analysis/scripts' in report
    document = json.loads((run.review / 'issues.json').read_text(encoding='utf-8'))
    assert [member['finding_uid'] for one in document['issues']
            for member in one['members']] == [uid], 'the finding is in the review as filed'
    assert document['code_check']['counts']['not-shown'] == 1


# -- what a verdict must carry ----------------------------------------------


def test_a_verdict_without_the_code_behind_it_stores_nothing(tmp_path, code_folder):
    """Confirmed and contradicted carry the verified passage; not-shown carries searches.

    The same rule a code finding has, for the same reason: the lane's answer is
    worth what the code behind it is worth, and a verdict the server could not
    check against the snapshot is an opinion about somebody else's finding.
    """
    open_run(tmp_path, code=str(code_folder))
    run = srv.current()
    reviewer = claim('core-1')
    line, quote = quote_pool(run, 1)[0]
    uid = srv.submit_finding(agent_token=reviewer, title='A method taken for granted', line=line,
                             quote=quote, comment='The step behind it is not stated.',
                             severity='minor',
                             assumed_method='that the mean is taken over resolved papers only',
                             )['finding_uid']
    checker = claim(srv.CHECK_TRACK)

    assert 'request_rejected' in srv.submit_code_check(checker, uid, 'settled', note='x')
    bare = srv.submit_code_check(checker, uid, 'contradicted', note='The code does otherwise.')
    assert 'request_rejected' in bare
    assert 'code_path' in bare['request_rejected'] and 'code_quote' in bare['request_rejected']

    wrong = srv.submit_code_check(checker, uid, 'confirmed', note='As described.',
                                  **{**code_evidence(*RESAMPLE),
                                     'code_quote': '    sample = generator.sample(rows, 4)'})
    assert 'request_rejected' in wrong
    assert 'is not what' in wrong['request_rejected']

    searched = srv.submit_code_check(checker, uid, 'not-shown', note='Nothing settles it.')
    assert 'needs the searches you ran' in searched['request_rejected']
    both = srv.submit_code_check(checker, uid, 'not-shown', note='Nothing settles it.',
                                 searches=['"mean" across analysis'], **code_evidence(*RESAMPLE))
    assert 'carries no passage of the code' in both['request_rejected']
    silent = srv.submit_code_check(checker, uid, 'confirmed', note='',
                                   **code_evidence(*RESAMPLE))
    assert 'Nothing was stored' in silent['request_rejected']
    assert run.code_checks == [], 'none of the above stored anything'

    good = srv.submit_code_check(checker, uid, 'confirmed', note='The mean is over scored rows.',
                                 **code_evidence(*RESAMPLE))
    assert good['stored'] is True
    again = srv.submit_code_check(checker, uid, 'contradicted', note='On second thought.',
                                  **code_evidence(*RESAMPLE))
    assert 'already answered' in again['request_rejected']
    assert len(run.code_checks) == 1


# -- the bound ---------------------------------------------------------------


def test_the_queue_is_capped_and_says_what_it_left_alone(tmp_path, code_folder):
    """Bounded, and honest about the bound.

    The cap is on the findings handed to the check, so the stage costs the same
    on a run with fifteen method claims as on one with twelve. What the cap
    left out is counted in the record and printed in the review, because an
    unchecked finding is neither cleared nor thrown out and a review that said
    nothing would read as the first.
    """
    open_run(tmp_path, code=str(code_folder))
    run = srv.current()
    reviewer = claim('core-1')
    pool = quote_pool(run, srv.MAX_CODE_CHECKS + 3)
    uids = []
    for index, (line, quote) in enumerate(pool):
        stored = srv.submit_finding(
            agent_token=reviewer, title=f'Method claim {index + 1}', line=line, quote=quote,
            comment='The step behind this number is not stated.',
            # Majors first, so the cut is over severity and not arrival: the
            # last three filed are the minors, and they are the three left out.
            severity='minor' if index >= srv.MAX_CODE_CHECKS else 'major',
            assumed_method='that the mean is taken over the resolved papers only')
        assert stored.get('stored'), stored
        uids.append(stored['finding_uid'])
    assert len(uids) == srv.MAX_CODE_CHECKS + 3
    assert srv.submit_report(reviewer, 'Many method claims.',
                             model=run.models.get('core-1')).get('closed')

    checker = claim(srv.CHECK_TRACK)
    queued = srv.method_claims(checker)
    assert queued['count'] == srv.MAX_CODE_CHECKS
    assert queued['over_cap'] == 3
    assert str(srv.MAX_CODE_CHECKS) in queued['next'] and 'past the cap' in queued['next']
    assert [row['finding_uid'] for row in queued['claims']] == uids[:srv.MAX_CODE_CHECKS]

    # A finding past the cap is not the check's to answer, and saying so is not
    # the same as pretending it does not exist.
    past = srv.submit_code_check(checker, uids[-1], 'confirmed', note='As described.',
                                 **code_evidence(*RESAMPLE))
    assert 'not in your queue' in past['request_rejected']

    answered = srv.submit_code_check(checker, uids[0], 'confirmed', note='The code does this.',
                                     **code_evidence(*RESAMPLE))
    assert answered['claims_left'] == srv.MAX_CODE_CHECKS - 1
    # The cut is over every qualifying finding, answered or not, so a retry of
    # the slot is handed what is still unanswered rather than a fresh twelve.
    assert answered['over_cap'] == 3
    assert srv.submit_report(checker, 'Twelve claims, one answered.',
                             model=run.models.get(srv.CHECK_TRACK)).get('closed')

    progress = run.code_check_counts()
    # Eleven of the twelve queued claims went unanswered, which is not the same
    # as three left past the cap: the cap was never handed them, and these the
    # lane was handed and did not read.
    assert progress == {'cap': srv.MAX_CODE_CHECKS, 'silent': False, 'checked': 1,
                        'not_checked': 11,
                        'incomplete': True,
                        'incomplete_reason': progress['incomplete_reason'],
                        'over_cap': 3,
                        'counts': {'confirmed': 1, 'contradicted': 0, 'not-shown': 0}}
    assert '11 method claim(s) were not read against the code' in progress['incomplete_reason']
    assert 'code-check answered 1 of 12' in progress['incomplete_reason']

    editor = claim('editor')
    issue = {'name': 'The methods state none of this', 'severity': 'moderate',
             'category': 'methods-and-reporting', 'rank': 1,
             'statement': 'The steps behind the reported numbers are not stated.',
             'members': list(uids)}
    write_the_review(run, editor, [issue], done=('core-1', srv.CHECK_TRACK))
    report, _ = rendered_review(run)
    assert 'Not checked' in report
    assert '3 further finding(s)' in report
    assert f"limit of {srv.MAX_CODE_CHECKS}" in report
    assert 'left exactly as their reviewers filed them' in report


# -- no code, no change ------------------------------------------------------


def test_a_run_given_no_code_plans_no_check_and_is_the_review_it_always_was(tmp_path):
    """The stage exists where the code does, and nowhere else.

    No slot, no claim code, nothing in ``progress.json``, no section in
    REVIEW.md and no refusal an editor has to understand: a run with no
    snapshot behaves exactly as it did before this stage existed.
    """
    opened = open_run(tmp_path)
    run = srv.current()
    assert run.code_check_slots == ()
    assert srv.CHECK_TRACK not in [row['slot'] for row in opened['claim_codes']]
    assert srv.code_check_slots('full-two-track', 3, code=False) == ()
    assert srv.code_check_slots('full-two-track', 3, code=True) == (srv.CHECK_TRACK,)
    assert run.code_check_record() is None and run.code_check_counts() is None

    refused = srv.claim_agent(srv.CHECK_TRACK, 'any-code-at-all')
    assert 'request_rejected' in refused, refused

    reviewer = claim('core-1')
    line, quote = quote_pool(run, 1)[0]
    # The mark costs nothing on a run with no code: it is stored, and no stage
    # reads it, because there is nothing to read it against.
    uid = srv.submit_finding(agent_token=reviewer, title='A method taken for granted', line=line,
                             quote=quote, comment='The step is not stated.', severity='moderate',
                             assumed_method='that the mean is over resolved papers only',
                             )['finding_uid']
    assert srv.submit_report(reviewer, 'One finding.',
                             model=run.models.get('core-1')).get('closed')
    run.write_progress()
    progress = json.loads((run.review / 'progress.json').read_text(encoding='utf-8'))
    assert progress['code_check'] is None

    editor = claim('editor')
    listed = srv.list_findings(editor)
    assert listed['contradicted_by_code'] == []
    assert 'contradicted_reason' not in listed
    issue = {'name': 'The step is never stated', 'severity': 'moderate',
             'category': 'methods-and-reporting', 'rank': 1,
             'statement': 'The methods never state the step behind the reported mean.',
             'members': [uid]}
    write_the_review(run, editor, [issue], done=('core-1',))

    document = json.loads((run.review / 'issues.json').read_text(encoding='utf-8'))
    assert document['code_check'] is None
    report, _ = rendered_review(run)
    assert 'Checked against the code' not in report
    assert read_run(run.root)['code_check'] is None


# -- what a consumer reads back ----------------------------------------------


def test_the_run_reading_carries_the_check_and_a_thrown_out_only_review_is_finished(
        tmp_path, code_folder):
    """``read_run`` answers both questions about the check.

    Two things were wrong. An application that wanted to show what the check
    did had to open ``issues.json`` itself, which is the one file this package
    promises to read on its behalf. And a run whose single finding the code
    disproved read as raw findings from the ledger and as not publishable —
    over a review that is finished with nothing left wrong in it: the
    reviewers raised one problem, the authors' own code disproved it, and the
    review says so with the passage that did. A thrown-out finding is
    accounted for, so the reading is of the review and not of the ledger.
    """
    open_run(tmp_path, code=str(code_folder))
    run = srv.current()
    reviewer = claim('core-1')
    guessed = the_real_finding(run, reviewer)
    assert srv.submit_report(reviewer, 'One finding, and it is arithmetic.',
                             model=run.models.get('core-1')).get('closed')

    checker = claim(srv.CHECK_TRACK)
    answered = srv.submit_code_check(
        checker, guessed, 'contradicted', **code_evidence(*RESAMPLE),
        note='The bootstrap resamples all eight assigned papers and averages whichever of them '
             'have a score, so a mean of a third is one of the values it can take.')
    assert answered['stored'] is True and answered['verdict'] == 'contradicted'
    assert srv.submit_report(checker, 'One claim checked, one contradicted.',
                             model=run.models.get(srv.CHECK_TRACK)).get('closed')

    # Nothing is left for the editor to group: the one finding is out of the
    # pool, so it closes the review with no issue in it.
    editor = claim('editor')
    assert srv.list_findings(editor)['findings'] == []
    close_every_stage(run, done=('core-1', srv.CHECK_TRACK))
    accepted = srv.submit_editor(
        editor,
        summary='The one problem the panel raised is not what the code does.',
        significance='Nothing make-or-break is left standing.',
        strength_of_evidence='The claim the finding rested on is disproved by the authors\' '
                             'own code.',
        verdict_reasoning='The arithmetic was right and the procedure under it was not the one '
                          'the finding assumed.',
        escalation_rulings=[],
        limitations=['Only the two analysis scripts in the snapshot were read.'])
    assert accepted.get('accepted'), accepted
    assert srv.submit_report(editor, 'No issue survives the code check.',
                             model=run.models.get('editor')).get('closed')
    written = srv.write_review(claim('recorder'))
    assert written['status'] == 'complete-without-findings', written

    reading = read_run(run.root)
    document = json.loads((run.review / 'issues.json').read_text(encoding='utf-8'))
    record = reading['code_check']
    assert record == document['code_check'], 'the reading is the published record itself'
    assert record['cap'] == srv.MAX_CODE_CHECKS and record['over_cap'] == 0
    assert record['checked'] == 1
    assert record['counts'] == {'confirmed': 0, 'contradicted': 1, 'not-shown': 0}
    assert [row['finding_uid'] for row in record['checks']] == [guessed]
    assert 'generator.choice(rows)' in record['checks'][0]['code_evidence']['text']
    assert 'resamples all eight assigned papers' in record['checks'][0]['note']

    assert reading['status'] == 'complete-without-findings'
    assert reading['findings_source'] == 'issues', 'the thrown-out finding is accounted for'
    assert reading['not_publishable_reasons'] == []
    assert reading['publishable'] is True
    assert reading['issues'] == [] and reading['counts']['quarantined'] == 0
    assert [one['finding_uid'] for one in reading['findings']] == [guessed], (
        'out of the issues is not off the ledger')
