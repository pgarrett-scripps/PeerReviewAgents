"""The editor on a large finding set, with no model anywhere near it.

The overlap experiment found the failure this file exists for. On the fish
antibiotic paper — 42 findings in one arm, 46 in the other — four runs of four
ended with ``The editor never submitted an accepted pooled review`` and left
nothing on disk to say what the editor had tried: no submission trace, no
refusal, no reason. The VR anatomy paper failed the same way once and passed on
the retry, which is what a turn budget that nearly fits looks like.

Two changes answer it, and both are tested here on a ledger of sixty findings,
larger than any run that failed. The editor submits its issues in batches
(:data:`peerreviewagents.issues.MAX_ISSUE_BATCH` per call, name-keyed so a
refused batch is re-sent rather than lost), so the work in one turn is bounded
however large the ledger is; and every submission call, accepted or refused, is
written to ``editor-log.jsonl``, so a run that still fails says why.

Nothing here calls a model. The sixty findings are submitted through the server
by the reviewer slots themselves, the issues are grouped in Python, and the
whole path — ``list_findings``, ``submit_issues``, ``submit_editor``,
``write_review``, :func:`peerreviewagents.read_run` — runs offline.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from peerreviewagents import issues as issue_model
from peerreviewagents.plugin import server as srv
from peerreviewagents.runs import read_run

FIXTURE = Path(__file__).parent / 'fixtures' / 'single-column.pdf'

#: How many findings the large case carries. Larger than the 42 and 46 of the
#: two overlap runs that failed, so a budget that fits here fits those.
LEDGER = 60

#: Which slots raise them, and how many each. Three independent reviewers and
#: the statistics audit, which is the shape the failing runs had.
SHARE = {'core-1': 16, 'core-2': 16, 'core-3': 16, 'spec-stats': 12}

CODES: dict = {}


@pytest.fixture(autouse=True)
def fresh_state():
    srv.STATE['run'] = None
    srv.STATE['plugin_root'] = None
    CODES.clear()
    yield
    srv.STATE['run'] = None


def open_run(tmp_path, **kwargs):
    opened = srv.ingest(str(FIXTURE), str(tmp_path / 'run'), reviewers=3, **kwargs)
    assert 'request_rejected' not in opened, opened
    CODES.update({row['slot']: row['claim_code'] for row in opened.get('claim_codes', [])})
    CODES['recorder_key'] = opened.get('recorder_key', '')
    return opened


def token_for(slot):
    taken = srv.claim_agent(slot, CODES.get(slot, ''))
    assert 'agent_token' in taken, taken
    CODES.pop(slot, None)
    return taken['agent_token']


def quote_pool(run, wanted):
    """``wanted`` passages that each occur exactly once in the block they are in.

    The guard anchors a finding by matching its quote inside the block it
    names, so a test that submits sixty findings needs sixty anchorable
    passages — or, since a finding's identity is its slot and its evidence and
    not its title, fewer passages read by more than one slot.
    """
    pool = []
    for block in run.paper['blocks']:
        text = block.get('text') or ''
        for piece in text.split('. '):
            piece = piece.strip()
            if 30 < len(piece) < 160 and text.count(piece) == 1:
                pool.append((block['id'], piece))
    assert len(pool) >= wanted, f'this fixture holds {len(pool)} anchorable passages'
    return pool[:wanted]


def submit_the_ledger(run, escalate=2):
    """Sixty stored findings, spread over three reviewers and the statistics audit.

    Returns the uids in ledger order. The first *escalate* of the statistics
    audit's are escalated, because an escalated finding with no ruling is one
    of the things that refuses a close, and a large run has some.
    """
    pool = quote_pool(run, max(SHARE.values()))
    uids, escalated = [], 0
    for slot, count in SHARE.items():
        token = token_for(slot)
        for index in range(count):
            block, quote = pool[index]
            rising = slot == 'spec-stats' and escalated < escalate
            stored = srv.submit_finding(
                token, title=f'{slot}: the passage at {block} claim {index + 1}',
                block_id=block, quote=quote,
                comment='The sentence asserts more than the measurement behind it shows.',
                severity=('major', 'moderate', 'minor')[index % 3],
                escalate=rising)
            assert stored.get('stored'), stored
            escalated += 1 if rising else 0
            uids.append(stored['finding_uid'])
        closed = srv.submit_report(token, 'What I checked and what I raised.',
                                   model=run.models.get(slot))
        assert closed.get('closed'), closed
    for track in srv.SPECIALIST_TRACKS:
        if track == 'stats':
            continue
        closed = srv.submit_report(token_for(f'spec-{track}'), 'Nothing further.',
                                   none_found=True, model=run.models.get(f'spec-{track}'))
        assert closed.get('closed'), closed
    assert len(uids) == LEDGER, len(uids)
    return uids


def grouped(uids, per_issue=3):
    """The uids as issues of *per_issue* members each, ranked 1 to N.

    The grouping an editor would do is a judgement; what this stands in for is
    its shape, which is what the server checks: every finding in exactly one
    issue, ranks with no gaps and no ties, a severity and a category on each.
    """
    issues = []
    for rank, start in enumerate(range(0, len(uids), per_issue), 1):
        members = uids[start:start + per_issue]
        severity = 'major' if rank <= 8 else ('moderate' if rank <= 14 else 'minor')
        issues.append({
            'name': f'Issue {rank}: what the passages behind it will not carry',
            'severity': severity,
            'category': issue_model.ISSUE_CATEGORIES[rank % len(issue_model.ISSUE_CATEGORIES)],
            'rank': rank,
            'statement': 'The manuscript states the result more strongly than the passages '
                         'these findings cite support, and the authors have to say which '
                         'reading they mean.',
            'members': list(members),
        })
    return issues


def batches(issues, size=None):
    size = size or issue_model.MAX_ISSUE_BATCH
    return [issues[start:start + size] for start in range(0, len(issues), size)]


def rulings_for(run):
    return [{'finding_uid': finding['finding_uid'],
             'ruling': 'Accepted: the authors must report the correction.'}
            for finding in run.pooled() if finding['escalate']]


def close_the_review(run, editor, issues):
    """The editor's close, with the verdict the server requires of it."""
    return srv.submit_editor(
        editor,
        summary='Three reviewers and the statistics audit agree on where the paper overreaches.',
        significance='Would matter to the field if the central comparison holds.',
        strength_of_evidence='Partly supported; the main comparison is underpowered.',
        verdict_reasoning='The claim is plausible and the evidence shown does not reach it.',
        escalation_rulings=rulings_for(run),
        limitations=['The raw measurements were not available to the panel.'])


# -- the large case ---------------------------------------------------------


def test_sixty_findings_reach_the_review_in_batches_of_issues(tmp_path):
    """The whole editor path on a ledger larger than the runs that failed.

    Sixty findings, twenty issues, two ``submit_issues`` calls and one close —
    and the editor's whole allowance is spent reading and grouping rather than
    on one submission per finding.
    """
    open_run(tmp_path)
    run = srv.current()
    uids = submit_the_ledger(run)

    editor = token_for('editor')
    listed = srv.list_findings(editor)
    assert listed['count'] == LEDGER
    assert listed['findings_without_issue'] == LEDGER
    assert listed['issues'] == [], 'nothing is grouped before the editor groups it'

    issues = grouped(uids)
    assert len(issues) == 20
    sent = batches(issues)
    assert [len(batch) for batch in sent] == [12, 8], 'the cap decides the batches'

    held = 0
    for batch in sent:
        answer = srv.submit_issues(editor, batch)
        assert answer.get('stored') == len(batch), answer
        held += len(batch)
        assert answer['issues_held'] == held
        assert answer['findings_without_issue'] == LEDGER - held * 3
    assert answer['next'].startswith('Every finding now belongs to an issue')

    accepted = close_the_review(run, editor, issues)
    assert accepted['accepted'] is True, accepted
    assert (accepted['issues'], accepted['required_changes'], accepted['suggestions']) == (
        20, 14, 6)
    assert accepted['escalated_ruled'] == 2

    closed = srv.submit_report(editor, 'The pooled review is above.',
                               model=run.models.get('editor'))
    assert closed.get('closed'), closed
    written = srv.write_review(token_for('recorder'))
    assert written['status'] == 'complete', written
    assert written['issue_counts']['issue_members'] == LEDGER

    document = json.loads((run.review / 'issues.json').read_text(encoding='utf-8'))
    members = [member['finding_uid'] for issue in document['issues']
               for member in issue['members']]
    assert sorted(members) == sorted(uids), 'every finding is in exactly one issue of the review'
    assert [issue['rank'] for issue in document['issues']] == list(range(1, 21))

    report = (run.review / 'REVIEW.md').read_text(encoding='utf-8')
    assert '## Required changes' in report and '## Suggestions' in report
    assert 'Issue 1: what the passages behind it will not carry' in report
    assert 'Issue 20: what the passages behind it will not carry' in report, \
        'nothing is capped and nothing is hidden'

    reading = read_run(run.review)
    assert reading['publishable'] is True, reading['not_publishable_reasons']
    assert reading['counts']['findings'] == LEDGER
    assert reading['counts']['issues'] == 20
    assert reading['counts']['issue_members'] == LEDGER
    assert reading['counts']['required'] == 14 and reading['counts']['suggestions'] == 6
    assert Path(reading['reports']['editor_log']).is_file()


def test_the_editor_spends_less_of_its_allowance_than_the_ledger_has_findings(tmp_path):
    """Why batching and not a larger turn budget: the work per turn is bounded.

    ``maxTurns`` is written into the agent file and the workflow cannot raise
    it for one run, so the quantity a run can scale is the work each turn does.
    Twenty issues over sixty findings cost two submission calls and one close,
    and the trace on disk says what was spent against what was allowed.
    """
    open_run(tmp_path)
    run = srv.current()
    uids = submit_the_ledger(run)

    editor = token_for('editor')
    allowance = srv.editor_call_budget(LEDGER)
    assert allowance == 37, 'twelve to read and rule, two per batch, one per four findings'

    issues = grouped(uids)
    for batch in batches(issues):
        assert srv.submit_issues(editor, batch).get('stored')
    assert close_the_review(run, editor, issues)['accepted'] is True

    trace = run.editor_trace()
    assert trace['calls'] == 3 and trace['refused'] == 0
    assert trace['call_budget'] == allowance
    assert trace['calls_spent'] <= allowance, trace
    assert trace['calls_spent'] < LEDGER, 'one call per finding is what the budget could not hold'


def test_closing_on_ungrouped_findings_says_what_to_do_and_keeps_the_batches(tmp_path):
    """A refusal names the work left, and nothing already submitted is lost."""
    open_run(tmp_path)
    run = srv.current()
    uids = submit_the_ledger(run)
    editor = token_for('editor')

    early = close_the_review(run, editor, [])

    assert 'request_rejected' in early
    assert any(f'{LEDGER} finding(s) are in the ledger' in problem
               for problem in early['problems']), early['problems']
    assert any(str(issue_model.MAX_ISSUE_BATCH) in problem for problem in early['problems'])

    issues = grouped(uids)
    first, rest = batches(issues)
    assert srv.submit_issues(editor, first).get('stored') == 12

    partly = close_the_review(run, editor, issues)

    assert 'request_rejected' in partly
    assert partly['issues_held'] == 12, 'the batch that landed is still held'
    assert partly['findings_without_issue'] == LEDGER - 36
    assert any('belong to no issue' in problem for problem in partly['problems'])

    too_many = srv.submit_issues(editor, issues)
    assert 'request_rejected' in too_many
    assert too_many['issues_submitted'] == 12, 'a refused batch does not take the held ones with it'

    assert srv.submit_issues(editor, rest).get('stored') == 8
    assert close_the_review(run, editor, issues)['accepted'] is True

    trace = run.editor_trace()
    assert trace['calls'] == 6, 'two closes refused, one batch refused, three accepted'
    assert trace['refused'] == 3
    assert [row['tool'] for row in trace['last_refusals']] == [
        'submit_editor', 'submit_editor', 'submit_issues']
    assert all(row['refusal'] for row in trace['last_refusals']), \
        'every refusal is logged with its reason'


def test_a_run_whose_editor_never_closed_says_why_and_keeps_the_trace(tmp_path):
    """The overlap experiment's silent failure, with the trace it did not have."""
    open_run(tmp_path)
    run = srv.current()
    uids = submit_the_ledger(run)
    editor = token_for('editor')

    # One over-size batch, refused, and then the editor stops — the shape of a
    # run that ran out of turns with its issues still in its context.
    refused = srv.submit_issues(editor, grouped(uids))
    assert 'request_rejected' in refused
    srv.submit_report(editor, 'I ran out of room.', model=run.models.get('editor'))

    blocked = srv.write_review(token_for('recorder'))

    assert blocked['status'] == 'blocked'
    assert 'The editor never submitted an accepted pooled review.' in blocked['request_rejected']
    assert 'submission call(s) were refused' in blocked['request_rejected']
    assert 'submit_issues' in blocked['request_rejected']
    # Run-relative: the trace is named so the holder of the folder can find it,
    # and never as a path on the machine that happened to run the review.
    assert 'review/editor-log.jsonl' in blocked['request_rejected']
    assert str(run.root) not in blocked['request_rejected']
    assert blocked['editor_trace']['refused'] == 1

    reading = read_run(run.review)
    assert reading['status'] == 'blocked'
    assert 'submission call(s) were refused' in reading['cause']
    assert reading['findings_source'] == 'ledger', 'the sixty findings are still readable'
    assert reading['counts']['findings'] == LEDGER
    assert Path(reading['reports']['editor_log']).is_file()


def test_the_editor_tool_allowance_grows_with_the_ledger(tmp_path):
    """A fixed allowance is what a 42-finding ledger outgrew, so it is not fixed."""
    assert srv.editor_call_budget(0) == srv.EDITOR_BASE_CALLS
    assert srv.editor_call_budget(12) == srv.EDITOR_BASE_CALLS + 2 + 3
    assert srv.editor_call_budget(42) > srv.editor_call_budget(12)
    assert srv.editor_call_budget(LEDGER) > srv.editor_call_budget(42)

    open_run(tmp_path)
    run = srv.current()
    submit_the_ledger(run)

    taken = srv.claim_agent('editor', CODES.get('editor', ''))

    assert taken['tool_call_budget'] == srv.editor_call_budget(LEDGER)
    protocol = json.loads((run.review / 'protocol.json').read_text(encoding='utf-8'))
    assert protocol['editor_tool_allowance'] == srv.editor_call_budget(LEDGER)
    assert protocol['issue_batch_limit'] == issue_model.MAX_ISSUE_BATCH
