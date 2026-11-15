"""What the editor may change, and what a later round has to rule on.

Two review findings put this file here. The first: a stored issue could not be
merged, renamed or dropped, while the refusal that fires when a finding is
already a member of another issue told the editor to merge — so an editor that
grouped two problems as one looped to ``maxTurns`` with nowhere to go. The
second: a prior issue nobody raised again was recorded as ``resolved``, which is
not the same claim, and the saved pilots show findings disappearing from text
nobody touched.

Everything here runs offline. The findings are submitted through the server by
the reviewer slots themselves and the grouping is done in Python; no model is
started, and the server makes no model call.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import pytest

from peerreviewagents import issues as issue_model
from peerreviewagents.plugin import server as srv
from peerreviewagents.runs import read_run

FIXTURE = Path(__file__).parent / 'fixtures' / 'single-column.pdf'
EDITOR_FILE = Path(srv.__file__).parent / 'pra' / 'agents' / 'editor.md'

CODES: dict = {}


@pytest.fixture(autouse=True)
def fresh_state():
    srv.STATE['run'] = None
    srv.STATE['plugin_root'] = None
    CODES.clear()
    yield
    srv.STATE['run'] = None


def open_run(tmp_path, **kwargs):
    kwargs.setdefault('arm', 'reviewers-only')
    kwargs.setdefault('reviewers', 2)
    opened = srv.ingest(str(FIXTURE), str(tmp_path / 'run'), **kwargs)
    assert 'request_rejected' not in opened, opened
    CODES.update({row['slot']: row['claim_code'] for row in opened.get('claim_codes', [])})
    CODES['recorder_key'] = opened.get('recorder_key', '')
    return opened


def token_for(slot):
    taken = srv.claim_agent(slot, CODES.get(slot, ''))
    assert 'agent_token' in taken, taken
    CODES.pop(slot, None)
    return taken['agent_token']


def claim_editor():
    """The editor's claim answer, not just its token: the answer is what it reads."""
    taken = srv.claim_agent('editor', CODES.get('editor', ''))
    assert 'agent_token' in taken, taken
    CODES.pop('editor', None)
    return taken


def reclaim(slot):
    """A retry: the recorder reissues the code and the replacement claims it."""
    fresh = srv.reissue_claim_code(CODES['recorder_key'], slot)
    assert 'claim_code' in fresh, fresh
    CODES[slot] = fresh['claim_code']
    return srv.claim_agent(slot, CODES.pop(slot))


def quote_pool(run, wanted):
    pool = []
    for block in run.paper['blocks']:
        text = block.get('text') or ''
        for piece in text.split('. '):
            piece = piece.strip()
            if 40 < len(piece) < 160 and text.count(piece) == 1:
                pool.append((block['id'], piece))
    assert len(pool) >= wanted, f'this fixture holds {len(pool)} anchorable passages'
    return pool[:wanted]


def file_findings(run, count=4, slot='core-1'):
    """``count`` stored findings from one reviewer, in ledger order."""
    pool = quote_pool(run, count)
    token = token_for(slot)
    uids = []
    for index, (block, quote) in enumerate(pool, 1):
        stored = srv.submit_finding(
            token, title=f'Claim {index} is stated more strongly than the passage carries',
            block_id=block, quote=quote,
            comment='The sentence asserts more than the measurement behind it shows.',
            severity=('major', 'moderate', 'minor')[index % 3])
        assert stored.get('stored'), stored
        uids.append(stored['finding_uid'])
    assert srv.submit_report(token, 'What I checked.', model=run.models.get(slot))['closed']
    return uids


def close_the_others(run, slots=('core-2',)):
    for slot in slots:
        token = token_for(slot)
        assert srv.submit_report(token, 'Nothing further.', none_found=True,
                                 model=run.models.get(slot))['closed']


def issue(name, rank, members, **extra):
    row = {'name': name, 'severity': 'major', 'category': 'claims-and-conclusions',
           'rank': rank, 'members': list(members),
           'statement': 'The manuscript states the result more strongly than the passages '
                        'behind it support, and the authors have to say which reading '
                        'they mean.'}
    row.update(extra)
    return row


def close_the_review(editor, **extra):
    return srv.submit_editor(
        editor,
        summary='The reviewers agree on where the paper overreaches.',
        significance='Would matter to the field if the central comparison holds.',
        strength_of_evidence='Partly supported; the main comparison is underpowered.',
        verdict_reasoning='The claim is plausible and the evidence shown does not reach it.',
        **extra)


def refusal_text(answer):
    """A refusal reads the same to the editor whether it is one line or a list."""
    assert 'request_rejected' in answer, answer
    return ' '.join([answer['request_rejected']] + list(answer.get('problems') or []))


def prior_file(path, issues, version=issue_model.ISSUES_SCHEMA_VERSION):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'schema_version': version, 'summary': 'The first round.',
                                'issues': issues}), encoding='utf-8')
    return path


def prior_issue(identifier, rank, name, members=(), status='new'):
    return {'id': identifier, 'name': name, 'severity': 'major', 'rank': rank,
            'category': 'claims-and-conclusions', 'status': status,
            'statement': 'The first round said this claim outruns its evidence.',
            'members': [{'finding_uid': 'a' * 16, 'slot': 'core-1', 'track': 'core',
                         'title': name, 'block_id': block, 'quote': quote}
                        for block, quote in members]}


# -- what the editor may change --------------------------------------------


def test_two_issues_merge_into_one_atomic_call(tmp_path):
    """The edit the refusal asks for: retire one name, resubmit the other wider."""
    open_run(tmp_path)
    run = srv.current()
    uids = file_findings(run, 2)
    close_the_others(run)
    editor = token_for('editor')
    srv.submit_issues(editor, [issue('The first reading', 1, [uids[0]]),
                               issue('The second reading', 2, [uids[1]])])

    merged = srv.submit_issues(editor, [issue('The first reading', 1, uids)],
                               retire=['The second reading'])

    assert 'request_rejected' not in merged, merged
    assert merged['retired'] == ['The second reading']
    assert (merged['issues_held'], merged['findings_without_issue']) == (1, 0)
    assert close_the_review(editor)['accepted'] is True


def test_an_issue_is_renamed_by_retiring_the_name_it_had(tmp_path):
    open_run(tmp_path)
    run = srv.current()
    uids = file_findings(run, 2)
    close_the_others(run)
    editor = token_for('editor')
    srv.submit_issues(editor, [issue('The wrong label', 1, uids)])

    renamed = srv.submit_issues(editor, [issue('What the passages will not carry', 1, uids)],
                                retire=['The wrong label'])

    assert renamed['issues_held'] == 1 and renamed['findings_without_issue'] == 0
    assert close_the_review(editor)['accepted'] is True
    assert srv.submit_report(editor, 'Grouped.', model=run.models.get('editor'))['closed']
    srv.write_review(token_for('recorder'))
    document = json.loads((run.review / 'issues.json').read_text(encoding='utf-8'))
    assert [row['name'] for row in document['issues']] == ['What the passages will not carry']


def test_dropping_an_issue_frees_its_findings_and_the_close_refuses_until_they_land(tmp_path):
    open_run(tmp_path)
    run = srv.current()
    uids = file_findings(run, 2)
    close_the_others(run)
    editor = token_for('editor')
    srv.submit_issues(editor, [issue('The first reading', 1, [uids[0]]),
                               issue('The second reading', 2, [uids[1]])])

    dropped = srv.submit_issues(editor, retire=['The second reading'])

    assert dropped['retired'] == ['The second reading']
    assert (dropped['issues_held'], dropped['findings_without_issue']) == (1, 1)
    refused = close_the_review(editor)
    assert 'request_rejected' in refused
    assert any(uids[1] in problem for problem in refused['problems']), refused

    srv.submit_issues(editor, [issue('The first reading', 1, uids)])
    assert close_the_review(editor)['accepted'] is True


def test_a_name_in_the_batch_and_in_retire_at_once_is_refused(tmp_path):
    open_run(tmp_path)
    run = srv.current()
    uids = file_findings(run, 2)
    close_the_others(run)
    editor = token_for('editor')
    srv.submit_issues(editor, [issue('The first reading', 1, uids)])

    refused = srv.submit_issues(editor, [issue('The first reading', 1, uids)],
                                retire=['The first reading'])

    assert 'request_rejected' in refused
    assert any('retire' in problem for problem in refused['problems']), refused
    assert srv.list_findings(editor)['issues'], 'the issue it held is still held'


def test_retiring_a_name_nothing_holds_is_refused_and_says_what_is_held(tmp_path):
    open_run(tmp_path)
    run = srv.current()
    uids = file_findings(run, 2)
    close_the_others(run)
    editor = token_for('editor')
    srv.submit_issues(editor, [issue('The first reading', 1, uids)])

    refused = srv.submit_issues(editor, retire=['A name from nowhere'])

    text = refusal_text(refused)
    assert 'A name from nowhere' in text and 'The first reading' in text, text
    assert srv.list_findings(editor)['issues'], 'nothing was stored and nothing was lost'


def test_resending_an_issue_by_name_frees_the_members_it_no_longer_claims(tmp_path):
    """A member list is whatever the latest submission says, so a resend releases."""
    open_run(tmp_path)
    run = srv.current()
    uids = file_findings(run, 2)
    close_the_others(run)
    editor = token_for('editor')
    srv.submit_issues(editor, [issue('Both readings at once', 1, uids)])

    narrowed = srv.submit_issues(editor, [issue('Both readings at once', 1, [uids[0]])])

    assert narrowed['findings_without_issue'] == 1
    landed = srv.submit_issues(editor, [issue('The second reading on its own', 2, [uids[1]])])
    assert landed['findings_without_issue'] == 0
    assert close_the_review(editor)['accepted'] is True


def test_the_refusal_for_a_finding_held_elsewhere_names_the_edit_that_answers_it(tmp_path):
    """The dead end: the refusal said merge, and merging was impossible."""
    open_run(tmp_path)
    run = srv.current()
    uids = file_findings(run, 2)
    close_the_others(run)
    editor = token_for('editor')
    srv.submit_issues(editor, [issue('The first reading', 1, [uids[0]])])

    refused = srv.submit_issues(editor, [issue('A second issue over the same finding', 2, uids)])

    assert 'request_rejected' in refused
    text = ' '.join(refused['problems'])
    assert 'retire' in text, text
    assert 'The first reading' in text


def test_a_re_claimed_editor_keeps_the_issues_the_attempt_before_submitted(tmp_path):
    """Retrying the stage must not mean pooling the whole ledger again."""
    open_run(tmp_path)
    run = srv.current()
    uids = file_findings(run, 2)
    close_the_others(run)
    first = claim_editor()
    srv.submit_issues(first['agent_token'], [issue('The first reading', 1, [uids[0]])])

    second = reclaim('editor')

    assert [held['name'] for held in second['issues_held']] == ['The first reading']
    assert 'holding 1 issue' in second['issues_next']
    editor = second['agent_token']
    assert srv.list_findings(editor)['findings_without_issue'] == 1
    srv.submit_issues(editor, [issue('The second reading', 2, [uids[1]])])
    assert close_the_review(editor)['accepted'] is True


def test_submitting_issues_after_an_accepted_close_reopens_the_review(tmp_path):
    """An accepted package must never be a wall a retry cannot get past."""
    open_run(tmp_path)
    run = srv.current()
    uids = file_findings(run, 2)
    close_the_others(run)
    editor = token_for('editor')
    srv.submit_issues(editor, [issue('Both readings at once', 1, uids)])
    assert close_the_review(editor)['accepted'] is True
    assert (run.agents['editor'].out / 'issues.json').is_file()

    reopened = srv.submit_issues(editor, [issue('Both readings at once', 1, uids)])

    assert reopened['reopened'] is True, reopened
    assert not (run.agents['editor'].out / 'issues.json').exists(), 'the stale document is gone'
    assert close_the_review(editor)['accepted'] is True


# -- the rank rule ----------------------------------------------------------


def test_ranks_with_a_tie_or_a_gap_refuse_the_close(tmp_path):
    """Patching the rank rule out used to leave the suite green."""
    open_run(tmp_path)
    run = srv.current()
    uids = file_findings(run, 2)
    close_the_others(run)
    editor = token_for('editor')
    srv.submit_issues(editor, [issue('The first reading', 1, [uids[0]]),
                               issue('The second reading', 1, [uids[1]])])

    tied = close_the_review(editor)

    assert 'request_rejected' in tied
    assert any('more than one issue: 1' in problem for problem in tied['problems']), tied

    srv.submit_issues(editor, [issue('The second reading', 3, [uids[1]])])
    gapped = close_the_review(editor)

    assert 'request_rejected' in gapped
    assert any('nothing holds: 2' in problem for problem in gapped['problems']), gapped

    srv.submit_issues(editor, [issue('The second reading', 2, [uids[1]])])
    assert close_the_review(editor)['accepted'] is True


# -- the turn cap and the call budget --------------------------------------


def test_the_turn_cap_in_the_agent_file_is_the_one_the_budget_is_built_on():
    """`maxTurns` and the call ceiling drift apart in silence otherwise."""
    front = EDITOR_FILE.read_text(encoding='utf-8').split('---')[1]
    stated = re.search(r'^maxTurns:\s*(\d+)\s*$', front, re.MULTILINE)
    assert stated, front
    assert int(stated.group(1)) == srv.EDITOR_MAX_TURNS
    assert srv.EDITOR_CALL_CEILING == srv.EDITOR_MAX_TURNS - srv.EDITOR_TURN_RESERVE


@pytest.mark.parametrize('findings', [0, 1, 12, 40, 60, 100, 150, 200])
def test_the_allowance_stays_under_the_turn_cap_and_the_batches_fit_in_it(findings):
    allowance = srv.editor_call_budget(findings)
    limit = issue_model.issue_batch_limit(findings)
    submissions = -(-findings // limit) if findings else 0

    assert allowance <= srv.EDITOR_CALL_CEILING < srv.EDITOR_MAX_TURNS
    assert submissions <= issue_model.MAX_ISSUE_BATCHES
    # Every submission, the close and the report, inside the allowance.
    assert submissions + 2 <= allowance


def test_a_submit_call_is_counted_against_the_allowance(tmp_path):
    """The budget test was vacuous: submissions never moved the counter."""
    open_run(tmp_path)
    run = srv.current()
    uids = file_findings(run, 2)
    close_the_others(run)
    editor = token_for('editor')
    before = run.agents['editor'].calls

    srv.submit_issues(editor, [issue('The first reading', 1, uids)])
    after_batch = run.agents['editor'].calls
    close_the_review(editor)
    after_close = run.agents['editor'].calls

    assert after_batch == before + 1
    assert after_close == after_batch + 1
    trace = read_run(run.review)['stages'].get('editor_trace') or {}
    assert trace.get('calls_spent') == after_close


def test_beyond_the_finding_count_the_editor_path_supports_the_slot_is_refused(
        tmp_path, monkeypatch):
    """Degrade by name rather than hand an editor more work than its turns hold."""
    monkeypatch.setattr(srv, 'MAX_EDITOR_FINDINGS', 1)
    open_run(tmp_path)
    run = srv.current()
    file_findings(run, 2)
    close_the_others(run)

    refused = srv.claim_agent('editor', CODES.get('editor', ''))

    assert 'request_rejected' in refused
    assert '2' in refused['request_rejected'] and '1' in refused['request_rejected']

    written = srv.write_review(CODES['recorder_key'] and token_for('recorder'))
    assert written['status'] == 'complete', written
    reading = read_run(run.review)
    assert reading['issues_source'] == 'server'
    assert reading['publishable'] is False
    assert any('editor' in str(item) for item in reading['limitations']), reading['limitations']


# -- the editor that never closed ------------------------------------------


def test_an_editor_that_never_submits_anything_is_refused_with_its_trace(tmp_path):
    """The one case that needed the numbers was the one that printed none."""
    open_run(tmp_path)
    run = srv.current()
    file_findings(run, 2)
    close_the_others(run)
    token_for('editor')  # claimed, and then it dies without a single submit call

    refused = srv.write_review(token_for('recorder'))

    assert 'request_rejected' in refused
    assert 'editor' in refused['request_rejected']
    assert 'editor_trace' in refused, refused
    assert refused['editor_trace']['calls'] == 0


# -- malformed input -------------------------------------------------------


def test_malformed_input_is_refused_with_a_reason_and_logged(tmp_path):
    open_run(tmp_path)
    run = srv.current()
    uids = file_findings(run, 2)
    close_the_others(run)
    editor = token_for('editor')

    cases = [
        (srv.submit_issues(editor, 5), 'int'),
        (srv.submit_issues(editor, [issue('A count where a list belongs', 1, uids) | {
            'members': 5}]), 'members'),
        (srv.submit_issues(editor, [issue('A string where a list belongs', 1, uids) | {
            'members': uids[0]}]), 'members'),
        (srv.submit_issues(editor, [issue('The right shape', 1, uids)], retire='a name'), 'str'),
    ]
    for refused, said in cases:
        assert said in refusal_text(refused), refused
    assert not srv.list_findings(editor)['issues'], 'none of that was stored'

    srv.submit_issues(editor, [issue('The right shape', 1, uids)])
    bad_ruling = close_the_review(editor, escalation_rulings=['abc'])
    assert 'object' in refusal_text(bad_ruling), bad_ruling
    bad_prior = close_the_review(editor, prior_rulings=5)
    assert 'list' in refusal_text(bad_prior), bad_prior

    rows = [json.loads(line) for line in
            (run.review / 'editor-log.jsonl').read_text(encoding='utf-8').splitlines() if line]
    assert len([row for row in rows if not row['accepted']]) >= 6, rows


def test_a_null_name_or_a_null_quote_is_absent_and_not_the_word_none(tmp_path):
    open_run(tmp_path)
    run = srv.current()
    uids = file_findings(run, 2)
    close_the_others(run)
    editor = token_for('editor')

    refused = srv.submit_issues(editor, [issue(None, 1, uids)])

    said = ' '.join(refused['problems'])
    assert 'name is required' in said, said
    assert 'None' not in said, 'a null name is absent, not an issue called None'
    srv.submit_issues(editor, [issue('The right shape', 1, uids) | {
        'rebuttal_quote': None, 'rebuttal_block_id': None}])
    assert close_the_review(editor)['accepted'] is True
    assert srv.submit_report(editor, 'Grouped.', model=run.models.get('editor'))['closed']
    srv.write_review(token_for('recorder'))
    stored = json.loads((run.review / 'issues.json').read_text(encoding='utf-8'))['issues'][0]
    assert stored['rebuttal_quote'] is None and stored['rebuttal_block_id'] is None


# -- a later round ---------------------------------------------------------


@pytest.mark.parametrize('arm', ['single', 'single-revision', 'manuscript-only', 'draft-check'])
def test_an_arm_with_no_editor_refuses_a_prior_round(tmp_path, arm):
    """Nothing in those arms could rule on a prior issue, and the ids collide."""
    path = prior_file(tmp_path / 'prior' / 'issues.json',
                      [prior_issue('i01', 1, 'The first round said so')])

    opened = srv.ingest(str(FIXTURE), str(tmp_path / arm / 'run'), arm=arm,
                        tracks=['stats'] if arm == 'draft-check' else None,
                        prior_issues=str(path))

    assert 'request_rejected' in opened, opened
    assert 'editor' in opened['request_rejected']


def test_a_matched_prior_issue_keeps_its_id_and_the_match_is_the_ruling(tmp_path):
    path = prior_file(tmp_path / 'prior' / 'issues.json',
                      [prior_issue('i01', 1, 'The first round said so')])
    open_run(tmp_path, prior_issues=str(path))
    run = srv.current()
    uids = file_findings(run, 2)
    close_the_others(run)
    claimed = claim_editor()
    assert [row['id'] for row in claimed['prior_issues']] == ['i01']
    editor = claimed['agent_token']
    srv.submit_issues(editor, [issue('The same claim, still unsupported', 1, uids) | {
        'prior_id': 'i01', 'status': 'persisting',
        'status_reason': 'The revision rewrote the sentence and kept the claim.'}])

    accepted = close_the_review(editor)

    assert accepted['accepted'] is True, accepted
    assert srv.submit_report(editor, 'Grouped.', model=run.models.get('editor'))['closed']
    srv.write_review(token_for('recorder'))
    document = json.loads((run.review / 'issues.json').read_text(encoding='utf-8'))
    assert [row['id'] for row in document['issues']] == ['i01']
    assert document['issues'][0]['status'] == 'persisting'
    assert document['prior_round']['issues'] == 1
    assert document['prior_round']['tally']['persisting'] == 1


def test_a_prior_issue_with_no_match_and_no_ruling_refuses_the_close(tmp_path):
    """Nothing is resolved by omission, which is what the pilots recorded."""
    path = prior_file(tmp_path / 'prior' / 'issues.json',
                      [prior_issue('i01', 1, 'The first round said so')])
    open_run(tmp_path, prior_issues=str(path))
    run = srv.current()
    uids = file_findings(run, 2)
    close_the_others(run)
    editor = token_for('editor')
    srv.submit_issues(editor, [issue('A problem this round is the first to raise', 1, uids)])

    refused = close_the_review(editor)

    assert 'request_rejected' in refused
    assert any('i01' in problem for problem in refused['problems']), refused

    ruled = close_the_review(editor, prior_rulings=[{
        'prior_id': 'i01', 'status': 'unverified',
        'reason': 'No reviewer raised it this round and the methods section still omits it.'}])
    assert ruled['accepted'] is True, ruled
    assert ruled['unverified_from_prior'] == 1
    assert ruled.get('resolved_from_prior', 0) == 0


def test_resolved_over_text_that_still_reads_the_same_takes_a_stated_reason(tmp_path):
    """A passage the authors did not touch is not evidence of a fix."""
    run_quotes = None
    path = tmp_path / 'prior' / 'issues.json'
    open_run(tmp_path, arm='reviewers-only', reviewers=2)
    run = srv.current()
    run_quotes = quote_pool(run, 3)
    srv.STATE['run'] = None
    CODES.clear()
    prior_file(path, [prior_issue('i01', 1, 'The first round said so',
                                  members=[run_quotes[2]])])

    open_run(tmp_path / 'second', prior_issues=str(path))
    run = srv.current()
    uids = file_findings(run, 2)
    close_the_others(run)
    editor = token_for('editor')
    srv.submit_issues(editor, [issue('A problem this round is the first to raise', 1, uids)])

    refused = close_the_review(editor, prior_rulings=[{
        'prior_id': 'i01', 'status': 'resolved',
        'reason': 'The authors say in the response letter that they fixed it.'}])

    assert 'request_rejected' in refused
    text = ' '.join(refused['problems'])
    assert 'word for word' in text, text
    assert run_quotes[2][1][:40] in text

    accepted = close_the_review(editor, prior_rulings=[{
        'prior_id': 'i01', 'status': 'resolved',
        'reason': 'The authors added the missing power calculation.',
        'text_unchanged_reason': 'The sentence is unchanged on purpose: the number it reports is '
                                'now supported by the new appendix table rather than by the '
                                'estimate the first round objected to.'}])
    assert accepted['accepted'] is True, accepted
    assert accepted['resolved_from_prior'] == 1
    assert srv.submit_report(editor, 'Grouped.', model=run.models.get('editor'))['closed']
    srv.write_review(token_for('recorder'))
    document = json.loads((run.review / 'issues.json').read_text(encoding='utf-8'))
    closed = [row for row in document['issues'] if row['id'] == 'i01'][0]
    assert closed['status'] == 'resolved'
    assert closed['rank'] is None and closed['prior_rank'] == 1
    assert closed['surviving_quotes'], closed
    assert 'appendix table' in closed['text_unchanged_reason']
    review = (run.review / 'REVIEW.md').read_text(encoding='utf-8')
    assert '## Since the prior round' in review
    assert 'appendix table' in review
    assert 'The first round said this claim outruns its evidence.' in review


def test_a_resolved_prior_row_is_reported_once_and_not_carried_again(tmp_path):
    """Round three reported 37 resolved with 19 live: the carry was recounted."""
    path = prior_file(tmp_path / 'prior' / 'issues.json', [
        prior_issue('i01', 1, 'Still open', status='persisting'),
        prior_issue('i02', None, 'Closed in the round before', status='resolved'),
        prior_issue('i03', None, 'Never verified', status='unverified'),
    ])
    open_run(tmp_path, prior_issues=str(path))
    run = srv.current()

    assert [row['id'] for row in run.prior_issues] == ['i01', 'i03'], 'resolved is not carried'
    claimed = claim_editor()
    assert [row['id'] for row in claimed['prior_issues']] == ['i01', 'i03']
    assert claimed['prior_round']['issues'] == 2


def test_the_prior_round_is_recorded_without_a_path_from_the_machine(tmp_path):
    path = prior_file(tmp_path / 'prior' / 'issues.json',
                      [prior_issue('i01', 1, 'The first round said so')])
    open_run(tmp_path, prior_issues=str(path))
    run = srv.current()
    uids = file_findings(run, 2)
    close_the_others(run)
    editor = token_for('editor')
    srv.submit_issues(editor, [issue('The same claim, still unsupported', 1, uids) | {
        'prior_id': 'i01', 'status': 'persisting',
        'status_reason': 'The revision rewrote the sentence and kept the claim.'}])
    assert close_the_review(editor)['accepted'] is True
    assert srv.submit_report(editor, 'Grouped.', model=run.models.get('editor'))['closed']
    srv.write_review(token_for('recorder'))

    for name in ('issues.json', 'protocol.json', 'REVIEW.md'):
        text = (run.review / name).read_text(encoding='utf-8')
        assert str(path) not in text, name
        assert str(path.parent) not in text, name
    document = json.loads((run.review / 'issues.json').read_text(encoding='utf-8'))
    assert document['prior_round']['file'] == 'issues.json'
    assert len(document['prior_round']['digest']) == 16


def test_a_prior_round_file_this_version_cannot_read_is_refused_at_ingest(tmp_path):
    path = prior_file(tmp_path / 'prior' / 'issues.json',
                      [prior_issue('i01', 1, 'The first round said so')],
                      version='review-issues-v9')

    opened = srv.ingest(str(FIXTURE), str(tmp_path / 'run'), arm='reviewers-only',
                        reviewers=2, prior_issues=str(path))

    assert 'request_rejected' in opened, opened
    assert 'review-issues-v9' in opened['request_rejected']


# -- what a refused or late call must not destroy ---------------------------


def test_a_refused_submit_issues_leaves_the_accepted_review_standing(tmp_path):
    """The reopen belongs to the landing, not to the validation above it.

    A malformed call dropped the accepted package and deleted the issues.json
    it had written on its way to answering that nothing was stored.
    """
    open_run(tmp_path)
    run = srv.current()
    uids = file_findings(run, 2)
    close_the_others(run)
    editor = token_for('editor')
    srv.submit_issues(editor, [issue('The one reading', 1, uids)])
    assert close_the_review(editor)['accepted'] is True
    package = run.agents['editor'].editor_package
    written = run.agents['editor'].out / 'issues.json'
    assert package and written.is_file()

    refused = srv.submit_issues(editor, 5)

    assert 'request_rejected' in refused
    assert refused.get('reopened') is None, 'a refused call reopens nothing'
    assert run.agents['editor'].editor_package is package, 'the accepted review is untouched'
    assert written.is_file(), 'and the file it wrote is still there'
    assert srv.submit_report(editor, 'Grouped.', model=run.models.get('editor'))['closed']
    assert srv.write_review(token_for('recorder'))['status'] == 'complete'
    assert read_run(run.review)['issues_source'] == 'editor'


def test_nothing_reopens_the_review_once_the_editor_has_filed_its_report(tmp_path):
    open_run(tmp_path)
    run = srv.current()
    uids = file_findings(run, 2)
    close_the_others(run)
    editor = token_for('editor')
    srv.submit_issues(editor, [issue('The one reading', 1, uids)])
    assert close_the_review(editor)['accepted'] is True
    assert srv.submit_report(editor, 'Grouped.', model=run.models.get('editor'))['closed']

    refused = srv.submit_issues(editor, [issue('A second reading', 1, uids)])

    assert 'submit_report' in refused['request_rejected'], refused
    assert srv.write_review(token_for('recorder'))['status'] == 'complete'
    document = json.loads((run.review / 'issues.json').read_text(encoding='utf-8'))
    assert [row['name'] for row in document['issues']] == ['The one reading']


def test_no_call_reopens_a_run_whose_files_are_written(tmp_path):
    """After ``write_review`` there is a published document, and it stands."""
    open_run(tmp_path)
    run = srv.current()
    uids = file_findings(run, 2)
    close_the_others(run)
    editor = token_for('editor')
    srv.submit_issues(editor, [issue('The one reading', 1, uids)])
    assert close_the_review(editor)['accepted'] is True
    assert srv.submit_report(editor, 'Grouped.', model=run.models.get('editor'))['closed']
    assert srv.write_review(token_for('recorder'))['status'] == 'complete'

    refused = srv.submit_issues(editor, retire=['The one reading'])

    assert refused['status'] == 'complete', refused
    assert 'written' in refused['request_rejected'], refused
    assert (run.review / 'issues.json').is_file(), 'the published review is still on disk'
    reading = read_run(run.review)
    assert reading['issues_source'] == 'editor' and reading['publishable'] is True


def test_the_same_name_retired_twice_is_one_retirement(tmp_path):
    """``retire=['C', 'C']`` popped the name once and raised on the second."""
    open_run(tmp_path)
    run = srv.current()
    uids = file_findings(run, 2)
    close_the_others(run)
    editor = token_for('editor')
    srv.submit_issues(editor, [issue('The first reading', 1, [uids[0]]),
                               issue('The second reading', 2, [uids[1]])])

    merged = srv.submit_issues(editor, [issue('The first reading', 1, uids)],
                               retire=['The second reading', 'The second reading'])

    assert 'request_rejected' not in merged, merged
    assert merged['retired'] == ['The second reading']
    assert (merged['issues_held'], merged['findings_without_issue']) == (1, 0)
    rows = [json.loads(line) for line in
            (run.review / 'editor-log.jsonl').read_text(encoding='utf-8').splitlines() if line]
    assert rows[-1]['accepted'] is True, 'the call was recorded, not raised out of'
    assert close_the_review(editor)['accepted'] is True


# -- an id is never reused --------------------------------------------------


def test_an_id_an_earlier_round_spent_is_never_minted_again(tmp_path):
    """i01 was resolved in round one and handed to a new problem in round two."""
    path = prior_file(tmp_path / 'prior' / 'issues.json', [
        prior_issue('i01', 1, 'Fixed in the first round', status='resolved'),
        prior_issue('i02', 2, 'Still open'),
    ])
    open_run(tmp_path, prior_issues=str(path))
    run = srv.current()
    uids = file_findings(run, 2)
    close_the_others(run)
    editor = token_for('editor')
    srv.submit_issues(editor, [
        issue('The same claim, still unsupported', 1, [uids[0]]) | {
            'prior_id': 'i02', 'status': 'persisting',
            'status_reason': 'The revision rewrote the sentence and kept the claim.'},
        issue('A problem this round is the first to raise', 2, [uids[1]])])
    assert close_the_review(editor)['accepted'] is True
    assert srv.submit_report(editor, 'Grouped.', model=run.models.get('editor'))['closed']
    srv.write_review(token_for('recorder'))

    document = json.loads((run.review / 'issues.json').read_text(encoding='utf-8'))
    minted = [row['id'] for row in document['issues'] if not row['prior_id']]
    assert minted == ['i03'], 'i01 belongs to the problem the first round reported'
    assert set(document['issue_ids_used']) >= {'i01', 'i02', 'i03'}


def test_a_spent_id_stays_spent_through_a_round_that_never_held_it(tmp_path):
    """Round three reads the reserved ids, not only the rows it was handed.

    The resolved row is dropped from the carry, so by round three ``i01`` is in
    no row anywhere — only in the list the document publishes for this.
    """
    second = prior_file(tmp_path / 'second' / 'issues.json', [
        prior_issue('i02', 1, 'Still open'),
        prior_issue('i03', 2, 'Raised in the second round'),
    ])
    document = json.loads(second.read_text(encoding='utf-8'))
    document['issue_ids_used'] = ['i01', 'i02', 'i03']
    second.write_text(json.dumps(document), encoding='utf-8')
    open_run(tmp_path, prior_issues=str(second))
    run = srv.current()
    uids = file_findings(run, 2)
    close_the_others(run)
    editor = token_for('editor')
    assert [row['id'] for row in run.prior_issues] == ['i02', 'i03']
    srv.submit_issues(editor, [
        issue('The same claim, still unsupported', 1, [uids[0]]) | {
            'prior_id': 'i02', 'status': 'persisting',
            'status_reason': 'The revision rewrote the sentence and kept the claim.'},
        issue('A problem this round is the first to raise', 2, [uids[1]])])
    closed = close_the_review(editor, prior_rulings=[{
        'prior_id': 'i03', 'status': 'unverified',
        'reason': 'Nobody raised it this round and the passage is gone from the manuscript.'}])

    assert closed['accepted'] is True, closed
    assert srv.submit_report(editor, 'Grouped.', model=run.models.get('editor'))['closed']
    srv.write_review(token_for('recorder'))
    written = json.loads((run.review / 'issues.json').read_text(encoding='utf-8'))
    minted = [row['id'] for row in written['issues'] if not row['prior_id']]
    assert minted == ['i04'], 'i01 was spent two rounds ago'
    assert set(written['issue_ids_used']) >= {'i01', 'i02', 'i03', 'i04'}


# -- no published file holds a path from the machine ------------------------


def test_no_published_file_holds_a_path_from_the_machine(tmp_path):
    """Scanned whole, not field by field: the editor's trace named one.

    ``issues.json`` carries the stage ledger, and the ledger carries where the
    editor's log is. It was the absolute path, so every published review named
    the home directory of whoever ran it.
    """
    open_run(tmp_path)
    run = srv.current()
    uids = file_findings(run, 2)
    close_the_others(run)
    editor = token_for('editor')
    srv.submit_issues(editor, 5)
    srv.submit_issues(editor, [issue('The one reading', 1, uids)])
    assert close_the_review(editor)['accepted'] is True
    assert srv.submit_report(editor, 'Grouped.', model=run.models.get('editor'))['closed']
    srv.write_review(token_for('recorder'))

    for name in ('issues.json', 'REVIEW.md'):
        text = (run.review / name).read_text(encoding='utf-8')
        for machine in (str(run.root), str(tmp_path), str(FIXTURE), str(Path.home())):
            assert machine not in text, f'{name} names {machine}'
    document = json.loads((run.review / 'issues.json').read_text(encoding='utf-8'))
    assert document['stages']['editor_trace']['log'] == 'review/editor-log.jsonl'
    assert document['stages']['editor_trace']['refused'] == 1


# -- the rebuttal quote answers one thing only ------------------------------


def test_a_rebuttal_quote_outside_the_case_it_answers_is_refused(tmp_path):
    """It was stored on a major issue, with an invented block, and printed."""
    open_run(tmp_path)
    run = srv.current()
    uids = file_findings(run, 2)
    close_the_others(run)
    editor = token_for('editor')

    major = srv.submit_issues(editor, [issue('The one reading', 1, uids) | {
        'rebuttal_quote': 'The authors address this in a sentence nobody wrote.',
        'rebuttal_block_id': 'b9999'}])

    assert 'rebuttal_quote' in refusal_text(major), major
    minor = srv.submit_issues(editor, [issue('The one reading', 1, uids) | {
        'severity': 'minor',
        'rebuttal_quote': 'The authors address this in a sentence nobody wrote.'}])
    assert 'rebuttal_quote' in refusal_text(minor), minor
    assert not srv.list_findings(editor)['issues'], 'neither was stored'

    srv.submit_issues(editor, [issue('The one reading', 1, uids)])
    assert close_the_review(editor)['accepted'] is True
    assert srv.submit_report(editor, 'Grouped.', model=run.models.get('editor'))['closed']
    srv.write_review(token_for('recorder'))
    stored = json.loads((run.review / 'issues.json').read_text(encoding='utf-8'))['issues'][0]
    assert stored['rebuttal_quote'] is None and stored['rebuttal_block_id'] is None


# -- what an unverified row has to carry ------------------------------------


def test_an_unverified_row_keeps_the_quotes_the_next_round_tests(tmp_path):
    """``unverified`` then ``resolved`` skipped the surviving-quote check.

    A closed row has no members — the findings were that round's — so unless it
    carries what the check needs, the round after it can call the issue fixed
    over a passage nobody touched.
    """
    open_run(tmp_path)
    passages = quote_pool(srv.current(), 3)
    srv.STATE['run'] = None
    CODES.clear()
    first = prior_file(tmp_path / 'first' / 'issues.json',
                       [prior_issue('i01', 1, 'The first round said so',
                                    members=[passages[2]])])

    open_run(tmp_path / 'second', prior_issues=str(first))
    run = srv.current()
    uids = file_findings(run, 2)
    close_the_others(run)
    editor = token_for('editor')
    srv.submit_issues(editor, [issue('A problem this round is the first to raise', 1, uids)])
    assert close_the_review(editor, prior_rulings=[{
        'prior_id': 'i01', 'status': 'unverified',
        'reason': 'Nobody raised it again and I cannot show the revision fixed it.',
    }])['accepted'] is True
    assert srv.submit_report(editor, 'Grouped.', model=run.models.get('editor'))['closed']
    srv.write_review(token_for('recorder'))
    document = run.review / 'issues.json'
    carried = [row for row in json.loads(document.read_text(encoding='utf-8'))['issues']
               if row['id'] == 'i01'][0]
    assert carried['status'] == 'unverified'
    assert carried['prior_rank'] == 1, 'the rank it held is what the next round reads'
    assert [row['quote'] for row in carried['surviving_quotes']] == [passages[2][1]]

    srv.STATE['run'] = None
    CODES.clear()
    open_run(tmp_path / 'third', prior_issues=str(document))
    third = srv.current()
    uids = file_findings(third, 2)
    close_the_others(third)
    editor = token_for('editor')
    srv.submit_issues(editor, [issue('A problem of this round', 1, uids)])

    refused = close_the_review(editor, prior_rulings=[{
        'prior_id': 'i01', 'status': 'resolved',
        'reason': 'The authors say in the response letter that they fixed it.'}])

    assert 'word for word' in refusal_text(refused), refused
    assert passages[2][1][:40] in refusal_text(refused)


# -- the types the tool layer used to swallow -------------------------------


def through_the_tool_layer(server, name, arguments):
    """One ``call_tool`` as an MCP client makes it, as the dict it answered."""
    blocks = asyncio.run(server.call_tool(name, arguments))
    text = blocks[0].text if not isinstance(blocks, tuple) else blocks[0][0].text
    return json.loads(text)


def test_a_wrong_type_through_the_tool_layer_is_refused_by_the_server_and_logged(tmp_path):
    """The pydantic layer rejected these before the function ran.

    Nothing was logged, so a run whose editor died on them reported that no
    submission call had reached the server — about calls that were made.
    """
    server = srv.create_server()
    open_run(tmp_path)
    run = srv.current()
    uids = file_findings(run, 2)
    close_the_others(run)
    editor = token_for('editor')

    cases = [
        ('submit_issues', {'agent_token': editor, 'issues': 5}, 'int'),
        ('submit_issues', {'agent_token': editor, 'retire': 'The one reading'}, 'str'),
        ('submit_issues', {'agent_token': editor, 'retire': [5]}, 'int'),
        ('submit_issues', {'agent_token': editor,
                           'issues': [issue(5, 1, uids)]}, 'name is text'),
        ('submit_editor', {'agent_token': editor, 'summary': 5}, 'summary is text'),
        ('submit_editor', {'agent_token': editor, 'summary': 'A summary.',
                           'significance': None}, 'significance'),
        ('submit_report', {'agent_token': editor, 'summary': 'Grouped.',
                           'limitations': [5, None]}, 'limitations is a list of strings'),
    ]
    for tool, arguments, said in cases:
        answer = through_the_tool_layer(server, tool, arguments)
        assert said in refusal_text(answer), (tool, answer)

    assert not srv.list_findings(editor)['issues'], 'none of that was stored'
    assert run.agents['editor'].report is None, 'and the stage is not closed'
    rows = [json.loads(line) for line in
            (run.review / 'editor-log.jsonl').read_text(encoding='utf-8').splitlines() if line]
    refusals = [row for row in rows if not row['accepted']]
    assert len(refusals) >= 6, refusals
    assert run.ledger()['editor_trace']['calls'] >= 6, 'the trace says what it tried'


# -- the degrade path says the true reason ----------------------------------


def test_a_ledger_past_the_editor_limit_says_so_rather_than_naming_the_arm(tmp_path,
                                                                          monkeypatch):
    """Three runs end with ``issues_source: server`` and this named the wrong one."""
    monkeypatch.setattr(srv, 'MAX_EDITOR_FINDINGS', 1)
    open_run(tmp_path)
    run = srv.current()
    file_findings(run, 2)
    close_the_others(run)
    assert 'request_rejected' in srv.claim_agent('editor', CODES.get('editor', ''))

    assert srv.write_review(token_for('recorder'))['status'] == 'complete'

    document = json.loads((run.review / 'issues.json').read_text(encoding='utf-8'))
    reason = document['issues_source_reason']
    assert '2 findings' in reason and 'at most 1' in reason, reason
    assert 'runs no editor' not in reason, reason
    assert all('at most 1' in row['status_reason'] for row in document['issues'])
    review = (run.review / 'REVIEW.md').read_text(encoding='utf-8')
    assert 'at most 1' in review and 'runs no editor' not in review
    reading = read_run(run.review)
    assert reading['issues_source'] == 'server' and reading['publishable'] is False
    assert any('at most 1' in str(item) for item in reading['not_publishable_reasons'])


def test_findings_passing_the_limit_after_the_editor_claimed_keep_its_review(tmp_path,
                                                                            monkeypatch):
    """The editor claimed under the limit and grouped everything that arrived.

    ``submit_editor`` refuses while any finding has no issue, so its review
    covers the whole ledger however the ledger grew. Throwing it away because
    the count later passed the limit would discard work nobody can redo; the
    caveat is stated instead.
    """
    open_run(tmp_path)
    run = srv.current()
    uids = file_findings(run, 2)
    close_the_others(run)
    editor = token_for('editor')
    monkeypatch.setattr(srv, 'MAX_EDITOR_FINDINGS', 1)
    srv.submit_issues(editor, [issue('The one reading', 1, uids)])
    assert close_the_review(editor)['accepted'] is True
    assert srv.submit_report(editor, 'Grouped.', model=run.models.get('editor'))['closed']

    assert srv.write_review(token_for('recorder'))['status'] == 'complete'

    document = json.loads((run.review / 'issues.json').read_text(encoding='utf-8'))
    assert [row['name'] for row in document['issues']] == ['The one reading']
    assert document['issues_source_reason'] is None, 'the editor grouped these'
    reading = read_run(run.review)
    assert reading['issues_source'] == 'editor'
    assert any('every finding is in it' in str(item) for item in reading['limitations']), \
        reading['limitations']


# -- a prior-round file this version will not read -------------------------


def test_a_prior_round_row_that_is_not_an_object_is_refused_by_name(tmp_path):
    """It raised an AttributeError that named neither the file nor the row."""
    path = tmp_path / 'prior' / 'issues.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'schema_version': issue_model.ISSUES_SCHEMA_VERSION,
                                'summary': 'The first round.',
                                'issues': [prior_issue('i01', 1, 'The first round said so'),
                                           'i02']}), encoding='utf-8')

    opened = srv.ingest(str(FIXTURE), str(tmp_path / 'run'), arm='reviewers-only',
                        reviewers=2, prior_issues=str(path))

    assert 'request_rejected' in opened, opened
    assert 'str' in opened['request_rejected'] and 'not objects' in opened['request_rejected']


def test_a_prior_round_file_holding_one_id_twice_is_refused(tmp_path):
    """Two rows under one id match two issues of this round to the same one."""
    path = prior_file(tmp_path / 'prior' / 'issues.json', [
        prior_issue('i01', 1, 'One reading of it'),
        prior_issue('i01', 2, 'Another row under the same id'),
    ])

    opened = srv.ingest(str(FIXTURE), str(tmp_path / 'run'), arm='reviewers-only',
                        reviewers=2, prior_issues=str(path))

    assert 'request_rejected' in opened, opened
    assert 'i01' in opened['request_rejected'] and 'more than once' in opened['request_rejected']


# -- the agent file and the server agree on the number ---------------------


def test_the_text_unchanged_length_in_the_agent_file_is_the_one_the_server_asks_for(tmp_path):
    """The file said "two sentences" and the server counted 40 characters."""
    stated = re.search(r'(\d+) characters at least', EDITOR_FILE.read_text(encoding='utf-8'))
    assert stated, 'the editor file no longer states the length it needs'
    floor = int(stated.group(1))

    open_run(tmp_path)
    passages = quote_pool(srv.current(), 3)
    srv.STATE['run'] = None
    CODES.clear()
    path = prior_file(tmp_path / 'prior' / 'issues.json',
                      [prior_issue('i01', 1, 'The first round said so',
                                   members=[passages[2]])])
    open_run(tmp_path / 'second', prior_issues=str(path))
    run = srv.current()
    uids = file_findings(run, 2)
    close_the_others(run)
    editor = token_for('editor')
    srv.submit_issues(editor, [issue('A problem of this round', 1, uids)])

    def rule(length):
        return close_the_review(editor, prior_rulings=[{
            'prior_id': 'i01', 'status': 'resolved',
            'reason': 'The authors added the missing power calculation.',
            'text_unchanged_reason': 'x' * length}])

    assert 'request_rejected' in rule(floor - 1), 'one short of the stated floor passed'
    assert rule(floor)['accepted'] is True


def test_the_integrations_doc_names_every_key_the_document_publishes(tmp_path):
    """A published key nobody documents is a key no integration reads.

    ``issues_source_reason`` and ``issue_ids_used`` both arrived in 0.10.0, and
    the second one has to be carried forward by anyone writing an
    ``issues.json`` for a later round to match against — undocumented, that
    round mints an id an earlier one already reported.
    """
    open_run(tmp_path)
    run = srv.current()
    uids = file_findings(run, 2)
    close_the_others(run)
    editor = token_for('editor')
    srv.submit_issues(editor, [issue('The one reading', 1, uids)])
    assert close_the_review(editor)['accepted'] is True
    assert srv.submit_report(editor, 'Grouped.', model=run.models.get('editor'))['closed']
    srv.write_review(token_for('recorder'))

    document = json.loads((run.review / 'issues.json').read_text(encoding='utf-8'))
    text = (Path(__file__).parent.parent / 'docs' / 'INTEGRATIONS.md').read_text(encoding='utf-8')

    missing = [key for key in document if f'`{key}`' not in text]
    assert not missing, f'docs/INTEGRATIONS.md names none of these published keys: {missing}'
