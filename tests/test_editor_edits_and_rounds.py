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
from pathlib import Path, PurePosixPath, PureWindowsPath

import pytest

import peerreviewagents
from peerreviewagents import issues as issue_model
from peerreviewagents.engine.storage import relative_name
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


def read_whole_paper(token):
    """Read every part of the manuscript, as the brief tells every agent to.

    The server refuses a finding from a slot it has not served the whole paper
    to, and reading spends nothing from the allowance.
    """
    run = srv.current()
    if 'read_paper' in run.agent_for(token).allowed:
        for part in range(1, srv.paper_view.served(run.base) + 1):
            assert isinstance(srv.read_paper(token, part=part), str)
    return token


def token_for(slot):
    taken = srv.claim_agent(slot, CODES.get(slot, ''))
    assert 'agent_token' in taken, taken
    CODES.pop(slot, None)
    return read_whole_paper(taken['agent_token'])


def claim_editor():
    """The editor's claim answer, not just its token: the answer is what it reads."""
    taken = srv.claim_agent('editor', CODES.get('editor', ''))
    assert 'agent_token' in taken, taken
    CODES.pop('editor', None)
    read_whole_paper(taken['agent_token'])
    return taken


def reclaim(slot):
    """A retry: the recorder reissues the code and the replacement claims it."""
    fresh = srv.reissue_claim_code(CODES['recorder_key'], slot)
    assert 'claim_code' in fresh, fresh
    CODES[slot] = fresh['claim_code']
    taken = srv.claim_agent(slot, CODES.pop(slot))
    if 'agent_token' in taken:
        read_whole_paper(taken['agent_token'])  # a new holder has read nothing
    return taken


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


def prior_file(path, issues, version=issue_model.ISSUES_SCHEMA_VERSION, ids_used=None):
    """A prior round's issues.json, as this version publishes one.

    ``issue_ids_used`` is part of that shape — every id the round and its own
    prior rounds spent — and a file without it is refused, so the default here
    is the one the writer would have published: the ids of its own rows.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {'schema_version': version, 'summary': 'The first round.', 'issues': issues}
    if ids_used is not None:
        document['issue_ids_used'] = ids_used
    else:
        document['issue_ids_used'] = sorted({str(row['id']) for row in issues
                                             if isinstance(row, dict) and row.get('id')})
    path.write_text(json.dumps(document), encoding='utf-8')
    return path


def prior_issue(identifier, rank, name, members=(), status='new'):
    return {'id': identifier, 'name': name, 'severity': 'major', 'rank': rank,
            'category': 'claims-and-conclusions', 'status': status,
            'statement': 'The first round said this claim outruns its evidence.',
            'members': [{'finding_uid': 'a' * 16, 'slot': 'core-1', 'track': 'core',
                         'title': name, 'block_id': block, 'quote': quote}
                        for block, quote in members]}


# -- what the editor may change --------------------------------------------


def test_an_editor_that_has_not_read_the_paper_stores_no_issues(tmp_path):
    """The editor read least of all: fifteen per cent of one manuscript.

    It is the slot that decides which findings become the review, and it was
    deciding that from the findings' own quotes. The package is refused until
    the whole manuscript has been served to it, the refusal says how much is
    unread, and the refusal is in ``editor-log.jsonl`` like every other.
    """
    open_run(tmp_path)
    run = srv.current()
    uids = file_findings(run, 2)
    close_the_others(run)
    taken = srv.claim_agent('editor', CODES.pop('editor', ''))
    editor = taken['agent_token']
    refused = srv.submit_issues(editor, [issue('The first reading', 1, [uids[0]])])
    assert 'you have not been served the whole manuscript' in refused['request_rejected']
    assert refused['unread_parts'] == [1]
    assert srv.list_findings(editor)['findings_without_issue'] == 2, 'nothing was stored'
    rows = [json.loads(line) for line
            in (run.review / 'editor-log.jsonl').read_text(encoding='utf-8').splitlines()]
    assert rows[-1]['tool'] == 'submit_issues' and rows[-1]['accepted'] is False
    assert 'whole manuscript' in rows[-1]['refusal']

    read_whole_paper(editor)
    assert srv.submit_issues(editor, [issue('The first reading', 1, uids)])['stored'] == 1


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
    for value in (document['stages']['editor_trace']['log'],
                  *json.loads((run.root / 'manuscript' / 'manifest.json')
                              .read_text(encoding='utf-8'))['files']):
        assert '\\' not in value, value


def test_a_published_path_is_posix_whichever_os_wrote_the_review(tmp_path):
    """A run-relative name is read on another OS than the one that wrote it.

    ``str()`` of a relative path writes the separator of the machine that ran
    the review, so the editor's trace said ``review\\editor-log.jsonl`` on
    Windows and the manuscript manifest keyed its files the same way — which
    the scaffold inventory check, built with ``as_posix``, read as a changed
    manuscript. Nothing matched across the two, and nothing said why.
    """
    windows = PureWindowsPath(r'C:\Users\someone\runs\paper')
    assert relative_name(windows / 'review' / 'editor-log.jsonl',
                         windows) == 'review/editor-log.jsonl'
    assert relative_name(windows / 'scaffold' / 'main.typ', windows) == 'scaffold/main.typ'
    posix = PurePosixPath('/runs/paper')
    assert relative_name(posix / 'review' / 'editor-log.jsonl',
                         posix) == 'review/editor-log.jsonl'
    assert relative_name(str(tmp_path / 'review' / 'issues.json'),
                         str(tmp_path)) == 'review/issues.json'


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


# -- a written review is not a draft ---------------------------------------


def a_written_review(tmp_path, **kwargs):
    """A run taken all the way to a published review, with the editor's token."""
    open_run(tmp_path, **kwargs)
    run = srv.current()
    uids = file_findings(run, 2)
    close_the_others(run)
    editor = token_for('editor')
    srv.submit_issues(editor, [issue('The one reading the passages carry', 1, uids)])
    assert close_the_review(editor)['accepted'] is True
    assert srv.submit_report(editor, 'Grouped and ranked.',
                             model=run.models.get('editor'))['closed']
    recorder = token_for('recorder')
    written = srv.write_review(recorder)
    assert written['status'] == 'complete', written
    return run, editor, recorder


def test_a_written_review_cannot_be_rewritten_by_the_editor_that_wrote_it(tmp_path):
    """The same token rewrote a published review: new verdict, new REVIEW.md.

    ``submit_issues`` had the two guards and ``submit_editor`` had neither, so
    after ``write_review`` had published the run the editor's own token could
    submit a different summary and a different verdict, file a second report,
    and a second ``write_review`` republished issues.json and REVIEW.md under
    it. A review that has been handed on is not a draft.
    """
    run, editor, recorder = a_written_review(tmp_path)
    before = {name: (run.review / name).read_bytes() for name in ('issues.json', 'REVIEW.md')}

    again = srv.submit_editor(
        editor, summary='On reflection the paper is fine.',
        significance='Nothing here matters after all.',
        strength_of_evidence='Fully supported.',
        verdict_reasoning='I have changed my mind about every issue of this review.')
    repeated = srv.submit_report(editor, 'A different closing story.',
                                 model=run.models.get('editor'))
    republished = srv.write_review(recorder)
    reissued = srv.reissue_claim_code(CODES['recorder_key'], 'editor')

    for answer in (again, repeated, republished, reissued):
        assert 'request_rejected' in answer, answer
        assert 'complete' in answer['request_rejected'], answer
    for name, raw in before.items():
        assert (run.review / name).read_bytes() == raw, f'{name} was rewritten'
    reading = read_run(run.review)
    assert 'changed my mind' not in json.dumps(reading)
    # Nor are the refusals logged. The log is the record of the review that was
    # written, the published document reports its counts, and a row appended
    # after the fact belongs to no review in the folder.
    rows = [json.loads(line) for line in
            (run.review / 'editor-log.jsonl').read_text(encoding='utf-8').splitlines() if line]
    assert [row['tool'] for row in rows if not row['accepted']] == []
    document = json.loads(before['issues.json'])
    assert document['stages']['editor_trace']['refused'] == 0


def test_a_slot_is_not_claimed_into_a_finished_run(tmp_path):
    """Re-claiming the editor on a published run moved its trace counters.

    Nothing the new holder submitted could be stored, but the claim itself took
    a fresh attempt number and rewrote protocol.json, and its refused calls
    landed in the log the published review is read from.
    """
    run, _, _ = a_written_review(tmp_path)
    before = (run.review / 'protocol.json').read_bytes()

    refused = srv.claim_agent('core-2', 'whatever-code-it-holds')

    assert 'request_rejected' in refused, refused
    assert 'complete' in refused['request_rejected']
    assert 'new run over the revised manuscript' in refused['request_rejected']
    assert (run.review / 'protocol.json').read_bytes() == before
    assert read_run(run.review)['status'] == 'complete'


# -- a complete folder is the review, and nothing writes in it -------------


def folder_state(root):
    """Every file under one run folder, by run-relative name and digest."""
    return {relative_name(path, root): srv.digest(path.read_bytes())
            for path in sorted(Path(root).rglob('*')) if path.is_file()}


def holds_path(text, machine):
    """Whether one file names a path — as plain text, or escaped inside JSON.

    A Windows path is written ``C:\\\\Users\\\\...`` inside a JSON string, so
    scanning the raw file for ``str(path)`` finds nothing on the one OS where a
    leaked path looks different from the path itself.
    """
    return machine in text or json.dumps(machine)[1:-1] in text


def test_a_complete_folder_is_not_a_place_to_start_a_run(tmp_path):
    """Ingest into a written review was accepted, and undid it.

    A new server process — a restarted plugin, a second worktree, a workflow
    run again over the same out folder — opened the folder, rewrote
    progress.json to ``running`` and took the run over. ``read_run`` then said
    the review was neither complete nor publishable while REVIEW.md and
    issues.json sat beside it saying otherwise, and the only exit was
    ``mark_blocked``. Ingest refuses by name now, and changes nothing.
    """
    run, _, _ = a_written_review(tmp_path)
    before = folder_state(run.root)
    srv.STATE['run'] = None

    refused = srv.ingest(str(FIXTURE), str(run.root), arm='reviewers-only', reviewers=2)

    assert 'request_rejected' in refused, refused
    said = refused['request_rejected']
    assert 'already holds a complete review' in said, said
    assert 'prior_issues=' + str(run.review / 'issues.json') in said, 'and how a round two runs'
    assert str(run.review / 'REVIEW.md') in said, 'and where the review it holds is'
    assert folder_state(run.root) == before, 'the finished folder was written to'
    reading = read_run(run.review)
    assert reading['status'] == 'complete' and reading['publishable'] is True


def test_a_blocked_folder_is_still_ingested_because_that_is_the_recovery(tmp_path):
    """The complete-folder refusal does not catch the folder it must not catch.

    A blocked run is left open on purpose: re-ingesting into it is the
    documented way back, and the status guard is on the two complete statuses
    alone.
    """
    open_run(tmp_path)
    run = srv.current()
    blocked = srv.mark_blocked(CODES['recorder_key'], 'The second reviewer never answered.')
    assert blocked['status'] == 'blocked', blocked
    srv.STATE['run'] = None

    again = srv.ingest(str(FIXTURE), str(run.root), arm='reviewers-only', reviewers=2)

    assert 'request_rejected' not in again, again
    assert read_run(run.review)['status'] == 'running'


def test_not_one_file_in_a_complete_folder_is_written_again(tmp_path):
    """Refused calls still wrote: the logs grew after the review was handed on.

    Every guard added before this one refused the call and then recorded the
    refusal — ``closures.jsonl`` and ``editor-log.jsonl`` from the editor side,
    and the reviewer tools wrote whatever they were asked for, because the
    investigation appends to ``tool-log.jsonl`` on every read and ``save_note``
    appends to ``notes.jsonl``. So a reviewer or an editor still holding a
    token went on adding rows to the record of a review already written, rows
    no document in the folder counts. The whole folder is checked here, by
    digest, not the two published files.
    """
    run, editor, recorder = a_written_review(tmp_path)
    reviewer = next(token for token, agent in run.tokens.items() if agent.slot == 'core-1')
    block = run.paper['blocks'][0]
    before = folder_state(run.root)
    spent = run.agent_for(reviewer).calls

    answers = {
        'read_paper': srv.read_paper(reviewer),
        'search': srv.search(reviewer, block['text'].split('. ')[0]),
        'save_note': srv.save_note(reviewer, 'One more thought about the bench.',
                                   [block['id']]),
        'submit_finding': srv.submit_finding(
            reviewer, title='A late reading', block_id=block['id'],
            quote=block['text'].split('. ')[0], comment='Raised after the review was written.',
            severity='minor'),
        'submit_issues': srv.submit_issues(editor, retire=['The one reading the passages carry']),
        'submit_report': srv.submit_report(editor, 'A different closing story.',
                                           model=run.models.get('editor')),
        'write_review': srv.write_review(recorder),
        'claim_agent': srv.claim_agent('core-2', 'whatever-code-it-holds'),
        'reissue_claim_code': srv.reissue_claim_code(CODES['recorder_key'], 'editor'),
    }

    for name, answer in answers.items():
        assert 'request_rejected' in answer, (name, answer)
        assert 'complete' in answer['request_rejected'], (name, answer)
    assert folder_state(run.root) == before, 'a refused call wrote in a finished folder'
    assert run.agent_for(reviewer).calls == spent, 'and the refusals cost no allowance'


def test_a_second_write_review_hands_back_the_counts_of_what_was_written(tmp_path):
    """The recorder's answer was lost, and the retry had no counts to report.

    ``write_review`` refuses a second call over a folder it already wrote, and
    the refusal carried the two paths and nothing else — so a recorder whose
    first answer never arrived could report the run's status and no numbers,
    and the workflow read its counts as undefined. The counts come back with
    the refusal now, read from the document that was published.
    """
    run, _, recorder = a_written_review(tmp_path)
    document = json.loads((run.review / 'issues.json').read_text(encoding='utf-8'))

    again = srv.write_review(recorder)

    assert 'request_rejected' in again, again
    assert again['status'] == 'complete'
    assert again['issue_counts'] == document['counts'], 'the counts of the review on disk'
    assert again['required_changes'] == document['counts']['required']
    assert again['suggestions'] == document['counts']['suggestions']
    assert again['escalated'] == document['escalated_count']
    assert again['review'] == str(run.review / 'REVIEW.md')


def test_the_paths_in_a_write_review_answer_are_in_no_file_of_the_run(tmp_path):
    """The tool answer names the machine; nothing in the folder may.

    ``write_review`` returns absolute paths, which is what a local tool answer
    is for — the recorder has to be able to open the files it just wrote. The
    rule is that none of it is persisted: no file in the run folder holds the
    run's own location, so the folder reads the same wherever it is moved to.
    The one absolute path written anywhere is the manuscript that was passed
    in, in review/manifest.json, which is the record of what was ingested and
    not a name anything in the review resolves.
    """
    run, _, recorder = a_written_review(tmp_path)

    refused = srv.write_review(recorder)

    assert refused['review'] == str(run.review / 'REVIEW.md'), 'the answer opens on this machine'
    assert refused['issues'] == str(run.review / 'issues.json')
    assert Path(refused['run']).is_absolute()
    named_the_input = []
    for path in sorted(run.root.rglob('*')):
        if not path.is_file():
            continue
        text = path.read_text(encoding='utf-8', errors='ignore')
        where = relative_name(path, run.root)
        for machine in (str(run.root), str(tmp_path)):
            assert not holds_path(text, machine), f'{where} names {machine}'
        if holds_path(text, str(FIXTURE)):
            named_the_input.append(where)
    assert named_the_input == ['review/manifest.json'], named_the_input
    manifest = json.loads((run.review / 'manifest.json').read_text(encoding='utf-8'))
    assert manifest['pdf'] == str(FIXTURE), 'the manuscript that was ingested'
    assert str(FIXTURE) not in json.dumps(
        {key: value for key, value in manifest.items() if key != 'pdf'})


@pytest.mark.parametrize('arm', ['single', 'manuscript-only'])
def test_run_status_carries_the_counts_of_a_written_review_on_every_arm(tmp_path, arm):
    """The one answer a recorder whose write answer was lost can read.

    ``issues`` was the editor's working tally and nothing else, so on an arm
    that runs no editor it was ``null`` after the review was written — and the
    workflow's fallback, which reads this answer for the counts of the document
    on disk, had nothing to report. The document is the source once it exists,
    on every arm, and the escalated count comes with it.
    """
    open_run(tmp_path, arm=arm)
    run = srv.current()
    if 'core-1' in run.required:
        file_findings(run, 2)
    for slot in run.required:
        agent = run.agents.get(slot)
        if agent is not None and agent.report:
            continue
        assert srv.submit_report(token_for(slot), 'Nothing further.', none_found=True,
                                 model=run.models.get(slot))['closed']
    written = srv.write_review(token_for('recorder'))
    assert written['status'] in srv.COMPLETE_STATUSES, written
    document = json.loads((run.review / 'issues.json').read_text(encoding='utf-8'))
    assert run.issue_counts() is None, 'this arm runs no editor, so the run holds no tally'

    standing = srv.run_status(CODES['recorder_key'])

    assert standing['status'] in srv.COMPLETE_STATUSES
    assert standing['issues'] == document['counts'], 'the counts of the document on disk'
    assert standing['escalated'] == document['escalated_count']
    assert standing['review'] == str(run.review / 'REVIEW.md')
    if 'core-1' in run.required:
        assert standing['issues']['issues'] == 2, 'and they are not a review of nothing'


def test_the_session_that_wrote_the_review_does_not_attach_to_it_either(tmp_path):
    """Ingest in the process that wrote the review answered ``attached: true``.

    The folder guard catches a second process. This is the one that wrote the
    review, whose run object says complete, and it handed the caller a finished
    review as a run to work in — a workflow stage run twice, a second paper
    passed to a live server — with nothing at the start saying the review was
    over, while the docs say ingest refuses a complete folder by name. One
    paper per session, and both cases refuse in the same words.
    """
    run, _, _ = a_written_review(tmp_path)
    before = folder_state(run.root)

    refused = srv.ingest(str(FIXTURE), str(run.root), arm='reviewers-only', reviewers=2)

    assert 'request_rejected' in refused, refused
    said = refused['request_rejected']
    assert 'already holds a complete review' in said, said
    assert 'prior_issues=' + str(run.review / 'issues.json') in said, 'and how a round two runs'
    assert str(run.review / 'REVIEW.md') in said, 'and where the review it holds is'
    assert 'attached' not in refused, 'nothing was attached to'
    assert folder_state(run.root) == before, 'the finished folder was written to'
    assert srv.STATE['run'] is run, 'and the session still serves the run it wrote'


def test_the_record_stage_treats_an_already_written_review_as_the_run_it_is():
    """The workflow's Record stage had one attempt at the one call that writes.

    A ``write_review`` that landed and an answer that was lost left the stage
    with nothing: it returned success with the counts undefined, or called
    ``block()`` and threw "the run files were never written" over a folder
    holding a finished review. Asserted on the source, because the workflow is
    JavaScript the plugin ships and no test runner here loads it.
    """
    source = (Path(srv.__file__).parent / 'pra' / 'workflows' / 'review.js').read_text(
        encoding='utf-8')
    record = source.split("phase('Record')", 1)[1]

    assert 'run_status with recorder_key=' in record, 'the folder is asked what it holds'
    assert 'Do not call write_review twice' in record
    assert record.index('const asFound') < record.index('await block('), (
        'and it is asked before the run is blocked')
    assert 'written(asFound.status)' in record, 'a complete folder is the success'
    # And a count the run did not report is unknown, not zero. The stage used
    # to tell the recorder to report 0 where a count was absent, and `issues`
    # is absent from run_status on every arm that runs no editor — so a lost
    # answer came back as a review with no issues while two sat in issues.json.
    assert 'Where a count is absent' not in record, 'a count nobody read is not a count of none'
    assert 'never report 0 for a count that is absent' in record
    assert 'counts_unknown' in record, 'and the result says which counts those were'
    # And the refusal that is not final is retried rather than read as a run
    # that will not be written: a resolve pass holds the writing stage for as
    # long as its lookups take, and it writes nothing while it does.
    assert 'resolve_citations pass is still' in record, 'the stage knows that refusal'
    assert 'write_review once more' in record, 'and writes after the pass it waited for'
    assert 'not calling write_review twice' in record, 'a refused write is not a write'


# -- the editor closes after its review, not before ------------------------


def test_the_editor_cannot_close_its_stage_before_its_review_is_accepted(tmp_path):
    """An accepted report stranded the run: submit_issues was then refused for good.

    The editor filed ``submit_report`` first, the stage read as closed, and
    every ``submit_issues`` after it was refused because the report was in —
    so the issues in the editor's context could not be stored by any call, and
    the run needed a reissued claim code to get anywhere.
    """
    open_run(tmp_path)
    run = srv.current()
    uids = file_findings(run, 2)
    close_the_others(run)
    editor = token_for('editor')
    srv.submit_issues(editor, [issue('The first reading', 1, [uids[0]])])

    premature = srv.submit_report(editor, 'I grouped what I could.',
                                  model=run.models.get('editor'))

    assert 'request_rejected' in premature, premature
    assert 'submit_editor has not accepted a review yet' in premature['request_rejected']
    assert '1 finding(s) belong to no issue' in premature['request_rejected']
    assert premature['findings_without_issue'] == 1, 'the refusal says what is still open'
    assert run.agents['editor'].report is None, 'the stage is not closed'

    # And nothing is stranded: the editor carries on from where it was.
    assert srv.submit_issues(editor, [issue('The first reading', 1, uids)])['issues_held'] == 1
    assert close_the_review(editor)['accepted'] is True
    assert srv.submit_report(editor, 'Grouped.', model=run.models.get('editor'))['closed']


def test_every_editor_refusal_of_a_closure_is_in_the_editor_log(tmp_path):
    """``none_found: "abc"`` raised a pydantic error and logged nothing anywhere.

    A wrong-typed closure was recorded in closures.jsonl and nowhere else, and
    the editor's trace — the one record a blocked run is reported from — reads
    the editor log alone. So a run whose editor died closing its stage said
    that no submission call had reached the server.
    """
    server = srv.create_server()
    open_run(tmp_path)
    run = srv.current()
    uids = file_findings(run, 2)
    close_the_others(run)
    editor = token_for('editor')
    srv.submit_issues(editor, [issue('The one reading', 1, uids)])

    cases = [({'none_found': 'abc'}, 'none_found is true or false'),
             ({'none_found': 5}, 'arrived as a int'),
             ({'summary': 5}, 'summary is text'),
             ({}, 'submit_editor has not accepted a review yet')]
    for extra, said in cases:
        answer = through_the_tool_layer(
            server, 'submit_report', {'agent_token': editor, 'summary': 'Grouped.', **extra})
        assert said in refusal_text(answer), (extra, answer)

    rows = [json.loads(line) for line in
            (run.review / 'editor-log.jsonl').read_text(encoding='utf-8').splitlines() if line]
    closures = [row for row in rows if row['tool'] == 'submit_report']
    assert len(closures) == len(cases) and not any(row['accepted'] for row in closures), closures
    assert all(row['refusal'] for row in closures), 'with the reason, not only the count'
    assert run.ledger()['editor_trace']['refused'] == len(cases)
    # Both logs, not one: closures.jsonl is where every slot's closure is read.
    logged = (run.review / 'closures.jsonl').read_text(encoding='utf-8')
    assert logged.count('"tool": "submit_report"') >= len(cases)

    assert close_the_review(editor)['accepted'] is True
    assert srv.submit_report(editor, 'Grouped.', model=run.models.get('editor'))['closed']
    rows = [json.loads(line) for line in
            (run.review / 'editor-log.jsonl').read_text(encoding='utf-8').splitlines() if line]
    assert rows[-1] == {**rows[-1], 'tool': 'submit_report', 'accepted': True, 'refusal': None}


# -- the degraded round still accounts for the prior round ------------------


def test_a_degraded_round_mints_above_the_spent_ids_and_carries_the_prior_issues(
        tmp_path, monkeypatch):
    """The no-editor path numbered from i01 and published no prior row at all.

    A second round that degrades — here a ledger past what the editor path
    supports — handed i01 to a new finding while i01 was still the id of a
    prior issue, and the prior issues appeared in no row of the file: neither
    resolved nor open, simply gone from the review the authors were sent.
    """
    monkeypatch.setattr(srv, 'MAX_EDITOR_FINDINGS', 1)
    open_run(tmp_path)
    passages = quote_pool(srv.current(), 3)
    srv.STATE['run'] = None
    CODES.clear()
    path = prior_file(tmp_path / 'prior' / 'issues.json', [
        prior_issue('i01', 1, 'Fixed in the first round', status='resolved'),
        prior_issue('i02', 2, 'Still open', members=[passages[2]]),
    ])

    open_run(tmp_path / 'second', prior_issues=str(path))
    run = srv.current()
    file_findings(run, 2)
    close_the_others(run)
    assert 'request_rejected' in srv.claim_agent('editor', CODES.get('editor', ''))
    assert srv.write_review(token_for('recorder'))['status'] == 'complete'

    document = json.loads((run.review / 'issues.json').read_text(encoding='utf-8'))
    assert document['issues_source'] == 'server'
    minted = [row['id'] for row in document['issues'] if not row['prior_id']]
    assert minted == ['i03', 'i04'], 'i01 and i02 belong to the problems of the prior round'
    assert [row['rank'] for row in document['issues'] if not row['prior_id']] == [1, 2]
    carried = [row for row in document['issues'] if row['id'] == 'i02']
    assert len(carried) == 1, 'the prior issue nobody ruled on is in the file'
    assert carried[0]['status'] == 'unverified'
    assert 'No editor ruled on this issue' in carried[0]['status_reason']
    assert 'at most 1' in carried[0]['status_reason'], 'and why there was no editor'
    assert carried[0]['rank'] is None and carried[0]['prior_rank'] == 2
    assert [row['quote'] for row in carried[0]['surviving_quotes']] == [passages[2][1]]
    assert set(document['issue_ids_used']) >= {'i01', 'i02', 'i03', 'i04'}
    assert sorted(carried[0]) == sorted(issue_model.ISSUE_FIELDS), 'the same shape as any row'
    reading = read_run(run.review)
    assert {row['id'] for row in reading['issues']} == {'i02', 'i03', 'i04'}


# -- a prior-round file is read, never trusted -----------------------------


def refused_ingest(tmp_path, name, document):
    """One hand-written prior file through ``ingest``, as the refusal it draws."""
    path = tmp_path / name / 'issues.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document), encoding='utf-8')
    answer = srv.ingest(str(FIXTURE), str(tmp_path / name / 'run'),
                        arm='reviewers-only', reviewers=2, prior_issues=str(path))
    assert 'request_rejected' in answer, answer
    return answer['request_rejected']


def test_a_prior_file_that_cannot_say_which_ids_it_spent_is_refused_by_name(tmp_path):
    """``issue_ids_used`` was iterated on trust, whatever it held.

    ``5`` raised a bare "'int' object is not iterable" out of ingest, a string
    was iterated one character at a time into ids like ``i``, and a dict
    quietly contributed its keys — each of which would hand a spent id to a new
    problem one round later.
    """
    rows = [prior_issue('i01', 1, 'Still open')]
    document = {'schema_version': issue_model.ISSUES_SCHEMA_VERSION,
                'summary': 'The first round.', 'issues': rows}

    cases = {'missing': document,
             'int': {**document, 'issue_ids_used': 5},
             'true': {**document, 'issue_ids_used': True},
             'string': {**document, 'issue_ids_used': 'i01'},
             'dict': {**document, 'issue_ids_used': {'i01': 1}},
             'not-an-id': {**document, 'issue_ids_used': ['i01', 7]},
             'bare-number': {**document, 'issue_ids_used': ['1']}}
    for name, candidate in cases.items():
        refusal = refused_ingest(tmp_path, name, candidate)
        assert 'issue_ids_used' in refusal, (name, refusal)
        assert str(tmp_path / name / 'issues.json') in refusal, 'the file is named'

    # And the shape this version publishes is accepted.
    ok = srv.ingest(str(FIXTURE), str(tmp_path / 'good' / 'run'), arm='reviewers-only',
                    reviewers=2, prior_issues=str(prior_file(tmp_path / 'good' / 'issues.json',
                                                             rows)))
    assert 'request_rejected' not in ok, ok


def test_a_prior_row_this_round_cannot_read_is_refused_at_ingest(tmp_path):
    """Half-validated rows reached the editor and raised TypeError an hour later.

    The duplicate-id check ran over the carried rows alone, so a live row and a
    resolved row sharing an id passed — the id of a problem the authors were
    told was fixed was also the id of one they were told was still open. A
    ``members`` or ``surviving_quotes`` that was not a list was accepted here
    and raised out of every ``submit_editor`` call afterwards.
    """
    def document(rows):
        return {'schema_version': issue_model.ISSUES_SCHEMA_VERSION,
                'summary': 'The first round.', 'issues': rows,
                'issue_ids_used': ['i01', 'i02']}

    live = prior_issue('i01', 1, 'Still open')
    cases = {
        'closed-duplicate': (document([live, prior_issue('i01', 2, 'Fixed', status='resolved')]),
                             'more than once'),
        'int-id': (document([{**live, 'id': 7}]), 'whose id is a int'),
        'no-id': (document([{k: v for k, v in live.items() if k != 'id'}]), 'whose id is absent'),
        'members-dict': (document([{**live, 'members': {'finding_uid': 'a' * 16}}]),
                         'whose members is a dict'),
        'quotes-string': (document([{**live, 'surviving_quotes': 'the passage'}]),
                          'whose surviving_quotes is a str'),
    }
    for name, (candidate, said) in cases.items():
        refusal = refused_ingest(tmp_path, name, candidate)
        assert said in refusal, (name, refusal)
        assert str(tmp_path / name / 'issues.json') in refusal, 'the file is named'


def test_the_public_validator_runs_the_checks_the_loader_runs(tmp_path):
    """A consumer holds the file it is about to publish against this code.

    A consumer publishes an issues.json for a later round to read, and the only
    way to ask whether that round could read it was to start a review: the
    rules lived inside the loader behind ``--prior-issues``. They are one
    function now, so the problems a consumer sees before publishing are the
    problems the loader would raise, by construction rather than by a copy of
    the rules kept in step by hand.
    """
    assert peerreviewagents.validate_prior_issues is issue_model.validate_prior_issues

    live = prior_issue('i01', 1, 'Still open')
    published = json.loads(prior_file(tmp_path / 'good' / 'issues.json',
                                      [live]).read_text(encoding='utf-8'))
    # The shape this version publishes is the shape the next round reads.
    assert issue_model.validate_prior_issues(published) == []

    broken = {
        'closed-duplicate': [live, prior_issue('i01', 2, 'Fixed', status='resolved')],
        'int-id': [{**live, 'id': 7}],
        'members-dict': [{**live, 'members': {'finding_uid': 'a' * 16}}],
        'quotes-string': [{**live, 'surviving_quotes': 'the passage'}],
        'not-an-object': [live, 'i02'],
        'all-resolved': [prior_issue('i01', 1, 'Fixed', status='resolved')],
    }
    for name, rows in broken.items():
        document = {**published, 'issues': rows}
        # Named as the consumer's own file, which is what it is holding.
        problems = issue_model.validate_prior_issues(document, label='issues.json')
        assert problems, name
        # And the round that reads it refuses it, with the validator's first
        # problem as the refusal: one check, read twice.
        refusal = refused_ingest(tmp_path / 'published', name, document)
        first = issue_model.validate_prior_issues(
            document, label='The prior-round file at '
            + str(tmp_path / 'published' / name / 'issues.json'))[0]
        assert refusal.startswith(first), (name, refusal, first)
        assert problems[0].split('issues.json', 1)[1] in refusal, 'the same problem, named twice'

    # An issue_ids_used a round cannot mint above is the same answer, and the
    # consumer hears it before publishing rather than from the next round.
    assert 'issue_ids_used' in issue_model.validate_prior_issues(
        {**published, 'issue_ids_used': 5})[0]
    assert 'issue_ids_used' in issue_model.validate_prior_issues(
        {k: v for k, v in published.items() if k != 'issue_ids_used'})[0]
    # And a stamp this version does not read is the first thing it says, with
    # nothing read past it.
    stamped = issue_model.validate_prior_issues({**published,
                                                 'schema_version': 'review-issues-v9'})
    assert len(stamped) == 1 and 'review-issues-v9' in stamped[0]


def test_a_file_the_validator_passes_is_a_file_the_loader_opens(tmp_path):
    """The validator said nothing about the fields the loader required anyway.

    A document with no ``issues`` list returned no problems at all, so a
    consumer was told its file was loadable and the round then opened on a prior
    round of nothing; with only a stamp the loader refused it with a bare
    ``'issue_ids_used'`` — a KeyError, from a requirement that lived outside the
    check a consumer can run. Every requirement of the loader is a problem this
    function returns, and a list field held as ``null`` is one of them: every
    list in this document is a list in what the engine writes, ``[]`` where it
    does not apply.
    """
    live = prior_issue('i01', 1, 'Still open')
    published = json.loads(prior_file(tmp_path / 'good' / 'issues.json',
                                      [live]).read_text(encoding='utf-8'))
    stamp_only = {'schema_version': issue_model.ISSUES_SCHEMA_VERSION}
    cases = {
        'stamp-only': (stamp_only, 'holds no issues list'),
        'stamp-and-ids': ({**stamp_only, 'issue_ids_used': ['i01']}, 'holds no issues list'),
        'issues-null': ({**published, 'issues': None}, 'holds no issues list'),
        'members-null': ({**published, 'issues': [{**live, 'members': None}]},
                         'whose members is null'),
        'quotes-null': ({**published, 'issues': [{**live, 'surviving_quotes': None}]},
                        'whose surviving_quotes is null'),
    }
    for name, (document, said) in cases.items():
        problems = issue_model.validate_prior_issues(document)
        assert problems, name
        assert said in problems[0], (name, problems)
        # And the round refuses it with that same first problem — not with a
        # KeyError out of a check the consumer had no way to run.
        refusal = refused_ingest(tmp_path / 'opened', name, document)
        assert said in refusal, (name, refusal)
        assert refusal.startswith(issue_model.validate_prior_issues(
            document, label='The prior-round file at '
            + str(tmp_path / 'opened' / name / 'issues.json'))[0]), (name, refusal)

    # A stamp-only file is refused for its issues *and* its spent ids: the
    # checks run on, rather than stopping at the first field that is not there.
    assert any('issue_ids_used' in problem
               for problem in issue_model.validate_prior_issues(stamp_only))
    # The file this version publishes still opens, with every requirement
    # listed: nothing above tightened the shape the engine writes.
    assert issue_model.validate_prior_issues(published) == []


# -- what REVIEW.md says about a closed row --------------------------------


def test_a_closed_rows_surviving_quotes_are_labelled_and_the_header_names_the_run(tmp_path):
    """The page printed a bare blockquote under "Why it is closed".

    The authors could not tell whether they were being quoted their own
    revision or the prior round's complaint, and under ``unverified`` — where
    the passage standing is the whole of what the round can say — the quote was
    the part that mattered. The header's "Run folder | review" said nothing at
    all: every caller hands the renderer the review directory.
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

    review = (run.review / 'REVIEW.md').read_text(encoding='utf-8')
    label = "**The prior round's evidence still in the manuscript:**"
    quote = '> ' + passages[2][1]
    assert label in review, review[-2000:]
    assert quote in review
    assert review.index(label) < review.index(quote), 'the label comes before the quote'
    assert 'No ruling was made on this issue this round' in review
    assert '| Run folder | run |' in review, 'the run folder, not the folder inside it'
    assert 'review |' not in review, 'it printed the word "review" and named nothing'
