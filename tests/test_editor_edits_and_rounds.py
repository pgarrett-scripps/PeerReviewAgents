"""What the editor may change.

A review finding put this file here: a stored issue could not be merged,
renamed or dropped, while the refusal that fires when a finding is already a
member of another issue told the editor to merge — so an editor that grouped
two problems as one looped to ``maxTurns`` with nowhere to go.

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
from manuscripts import seal_converted

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
    # ingest leaves a draft, because the Read stage writes the lines. No model
    # reads anything here, so the run is sealed as converted.
    seal_converted(opened)
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
    """``wanted`` passages that each occur once in the paper, with their lines."""
    whole = run.base.text
    pool = []
    for line in range(1, run.base.line_count() + 1):
        for piece in run.base.line_text(line).split('. '):
            piece = piece.strip()
            if 40 < len(piece) < 160 and whole.count(piece) == 1:
                pool.append((line, piece))
    assert len(pool) >= wanted, f'this fixture holds {len(pool)} anchorable passages'
    return pool[:wanted]


def file_findings(run, count=4, slot='core-1'):
    """``count`` stored findings from one reviewer, in ledger order."""
    pool = quote_pool(run, count)
    token = token_for(slot)
    uids = []
    for index, (line, quote) in enumerate(pool, 1):
        stored = srv.submit_finding(
            token, title=f'Claim {index} is stated more strongly than the passage carries',
            line=line, quote=quote,
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

    rows = [json.loads(line) for line in
            (run.review / 'editor-log.jsonl').read_text(encoding='utf-8').splitlines() if line]
    assert len([row for row in rows if not row['accepted']]) >= 5, rows


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
        'rebuttal_quote': None, 'rebuttal_line': None}])
    assert close_the_review(editor)['accepted'] is True
    assert srv.submit_report(editor, 'Grouped.', model=run.models.get('editor'))['closed']
    srv.write_review(token_for('recorder'))
    stored = json.loads((run.review / 'issues.json').read_text(encoding='utf-8'))['issues'][0]
    assert stored['rebuttal_quote'] is None and stored['rebuttal_line'] is None


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
    """It was stored on a major issue, pointed at a line nobody read, and printed."""
    open_run(tmp_path)
    run = srv.current()
    uids = file_findings(run, 2)
    close_the_others(run)
    editor = token_for('editor')

    major = srv.submit_issues(editor, [issue('The one reading', 1, uids) | {
        'rebuttal_quote': 'The authors address this in a sentence nobody wrote.',
        'rebuttal_line': 9999}])

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
    assert stored['rebuttal_quote'] is None and stored['rebuttal_line'] is None


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


# -- every published key is documented -------------------------------------


def test_the_integrations_doc_names_every_key_the_document_publishes(tmp_path):
    """A published key nobody documents is a key no integration reads.

    ``issues_source_reason`` arrived in 0.10.0: a consumer that does not know
    the key cannot tell a review the editor grouped from one the server pooled
    after the editor was refused.
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
    assert 'fresh out folder' in said, 'and how a further review runs'
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
    line, quote = quote_pool(run, 1)[0]
    before = folder_state(run.root)
    spent = run.agent_for(reviewer).calls

    answers = {
        'read_paper': srv.read_paper(reviewer),
        'search': srv.search(reviewer, quote),
        'save_note': srv.save_note(reviewer, 'One more thought about the bench.', [line]),
        'submit_finding': srv.submit_finding(
            reviewer, title='A late reading', line=line,
            quote=quote, comment='Raised after the review was written.',
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
    assert 'fresh out folder' in said, 'and how a further review runs'
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


# -- what REVIEW.md's header names -----------------------------------------


def test_the_review_header_names_the_run_folder_and_not_the_folder_inside_it(tmp_path):
    """The header's "Run folder | review" said nothing at all.

    Every caller hands the renderer the review directory, so printing that
    directory's own name printed the word "review" and named nothing.
    """
    run, _, _ = a_written_review(tmp_path)

    review = (run.review / 'REVIEW.md').read_text(encoding='utf-8')
    assert '| Run folder | run |' in review, 'the run folder, not the folder inside it'
    assert 'review |' not in review, 'it printed the word "review" and named nothing'
