"""The peerreview server's ownership layer: identity, the guard, the ledger, the files.

Every test here runs offline. Nothing in this file starts Claude Code, Codex or
any provider, and the server itself makes no model calls — which is the point
of putting identity, the finding guard and the stage ledger in it.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from peerreviewagents.plugin import server as srv

FIXTURE = Path(__file__).parent / 'fixtures' / 'single-column.pdf'


@pytest.fixture(autouse=True)
def fresh_state():
    srv.STATE['run'] = None
    srv.STATE['plugin_root'] = None
    yield
    srv.STATE['run'] = None


def open_run(tmp_path, **kwargs):
    opened = srv.ingest(str(FIXTURE), str(tmp_path / 'run'), **kwargs)
    assert 'request_rejected' not in opened, opened
    return opened


def quote_from(run, index=0):
    """A passage that occurs exactly once in its block, so the guard anchors it."""
    for block in run.paper['blocks'][index:]:
        text = (block.get('text') or '').strip()
        for piece in text.split('. '):
            piece = piece.strip()
            if 30 < len(piece) < 160 and block['text'].count(piece) == 1:
                return block['id'], piece
    raise AssertionError('no uniquely anchorable passage in this fixture')


def numeric_quote(run):
    """A uniquely anchorable passage carrying a number, and that number."""
    for block in run.paper['blocks']:
        text = (block.get('text') or '').strip()
        numbers = re.findall(srv.NUMERIC, text)
        if numbers and len(text) < 200 and block['text'].count(text) == 1:
            return block['id'], text, numbers[0]
    raise AssertionError('no anchorable passage with a number in this fixture')


def close(token, **kwargs):
    result = srv.submit_report(token, kwargs.pop('summary', 'Nothing further.'), **kwargs)
    assert result.get('closed'), result
    return result


# -- the plan ---------------------------------------------------------------


def test_arm_decides_which_stages_must_report(tmp_path):
    opened = open_run(tmp_path, reviewers=3)
    assert opened['arm'] == 'full-two-track'
    assert opened['slots'][:3] == ['core-1', 'core-2', 'core-3']
    assert [s for s in opened['slots'] if s.startswith('spec-')] == [
        f'spec-{track}' for track in srv.SPECIALIST_TRACKS]
    assert 'editor' in opened['slots']


def test_single_arm_fixes_the_reviewer_count_it_names(tmp_path):
    opened = open_run(tmp_path, reviewers=5, arm='single')
    assert opened['reviewers'] == 1
    assert opened['slots'] == ['core-1', 'recorder']


def test_draft_check_runs_chosen_specialists_and_no_core_track(tmp_path):
    opened = open_run(tmp_path, arm='draft-check', tracks=['stats', 'consistency'])
    assert opened['slots'] == ['spec-stats', 'spec-consistency', 'recorder']


def test_an_unknown_arm_is_refused(tmp_path):
    assert 'request_rejected' in srv.ingest(str(FIXTURE), str(tmp_path / 'run'), arm='debate')


def test_a_second_ingest_of_the_same_folder_attaches_to_the_run(tmp_path):
    open_run(tmp_path)
    again = srv.ingest(str(FIXTURE), str(tmp_path / 'run'))
    assert again['attached'] is True


# -- identity ---------------------------------------------------------------


def test_a_slot_outside_the_plan_is_refused(tmp_path):
    open_run(tmp_path, arm='single')
    assert 'request_rejected' in srv.claim_agent('spec-stats')
    assert 'request_rejected' in srv.claim_agent('core-2')


def test_the_token_carries_the_track_and_the_tool_list(tmp_path):
    open_run(tmp_path)
    core = srv.claim_agent('core-1')
    stats = srv.claim_agent('spec-stats')
    assert core['track'] == 'core' and stats['track'] == 'stats'
    assert 'view_supplement' in core['allowed_tools']
    assert 'view_supplement' not in stats['allowed_tools']
    assert core['agent_token'] != stats['agent_token']


def test_a_tool_call_needs_a_live_token(tmp_path):
    open_run(tmp_path)
    first = srv.claim_agent('core-1')['agent_token']
    assert 'request_rejected' in srv.read_blocks('not-a-token', ['b1'])
    srv.claim_agent('core-1')  # a second attempt supersedes the first token
    assert 'superseded' in srv.read_blocks(first, ['b1'])['request_rejected']


def test_an_agent_cannot_call_a_tool_outside_its_track(tmp_path):
    open_run(tmp_path)
    editor = srv.claim_agent('editor')['agent_token']
    refusal = srv.view_page(editor, 1)['request_rejected']
    assert 'may not call view_page' in refusal


def test_the_manuscript_only_arm_removes_the_page_and_lookup_tools(tmp_path):
    open_run(tmp_path, arm='manuscript-only')
    core = srv.claim_agent('core-1')
    assert 'view_page' not in core['allowed_tools']
    assert 'search_literature' not in core['allowed_tools']
    assert 'read_blocks' in core['allowed_tools']


def test_the_per_agent_tool_allowance_is_a_hard_stop(tmp_path, monkeypatch):
    monkeypatch.setitem(srv.CALL_BUDGET, 'core', 2)
    open_run(tmp_path, arm='single')
    token = srv.claim_agent('core-1')['agent_token']
    block = srv.current().paper['blocks'][0]['id']
    assert 'request_rejected' not in srv.read_blocks(token, [block])
    assert 'request_rejected' not in srv.read_blocks(token, [block])
    exhausted = srv.read_blocks(token, [block])
    assert exhausted.get('budget_exhausted') is True


# -- the guard --------------------------------------------------------------


def test_a_quote_that_is_not_in_the_block_stores_nothing(tmp_path):
    open_run(tmp_path, arm='single')
    token = srv.claim_agent('core-1')['agent_token']
    run = srv.current()
    block = run.paper['blocks'][0]['id']
    result = srv.submit_finding(token, title='Invented', block_id=block,
                                quote='a sentence this paper does not contain anywhere',
                                comment='...', severity='major')
    assert result.get('quarantined') is True
    assert run.findings == []
    assert (run.review / 'quarantined.jsonl').is_file()


def test_an_anchored_finding_is_stored_with_its_track_and_a_uid(tmp_path):
    open_run(tmp_path, arm='single')
    token = srv.claim_agent('core-1')['agent_token']
    run = srv.current()
    block, quote = quote_from(run)
    stored = srv.submit_finding(token, title='The central claim outruns the data',
                                block_id=block, quote=quote,
                                comment='The result shown does not reach this conclusion.',
                                severity='major')
    assert stored['stored'] is True
    assert stored['track'] == 'core'
    assert len(stored['finding_uid']) == 16
    assert stored['basis'] == 'judgement'
    rows = [json.loads(line) for line in
            (run.review / 'findings.jsonl').read_text().splitlines()]
    assert [row['finding_uid'] for row in rows] == [stored['finding_uid']]


def test_the_uid_is_stable_across_a_re_review_of_the_same_passage(tmp_path):
    open_run(tmp_path, arm='single')
    run = srv.current()
    block, quote = quote_from(run)
    first = srv.submit_finding(srv.claim_agent('core-1')['agent_token'], title='Same issue',
                               block_id=block, quote=quote, comment='x', severity='minor')
    second = srv.submit_finding(srv.claim_agent('core-1')['agent_token'], title='Same issue',
                                block_id=block, quote=quote, comment='x', severity='minor')
    assert first['finding_uid'] == second['finding_uid']


def test_only_a_receipt_over_quoted_numbers_earns_computed(tmp_path):
    open_run(tmp_path, arm='single')
    token = srv.claim_agent('core-1')['agent_token']
    run = srv.current()
    block, quote, number = numeric_quote(run)
    receipt = srv.calculate(token, f'{number} + {number}')['receipt']
    computed = srv.submit_finding(token, title='The arithmetic does not hold', block_id=block,
                                  quote=quote, comment='The sum is not what is reported.',
                                  severity='moderate', calculate_receipt=receipt)
    assert computed['basis'] == 'computed'
    off_paper = srv.calculate(token, '987654 * 31')['receipt']
    judged = srv.submit_finding(token, title='Numbers from nowhere', block_id=block, quote=quote,
                                comment='...', severity='minor', calculate_receipt=off_paper)
    assert judged['basis'] == 'judgement'
    assert 'none of the cited blocks print' in judged['basis_reason']
    assert '987654' in judged['basis_reason']


def quote_without(run, number, exclude):
    """A uniquely anchorable passage whose whole block never prints ``number``."""
    for index in range(len(run.paper['blocks'])):
        block, quote = quote_from(run, index)
        text = run.paper['blocks'][index].get('text') or ''
        if block != exclude and number not in text:
            return block, quote
    raise AssertionError('every block in this fixture prints that number')


def test_a_receipt_over_numbers_elsewhere_in_a_cited_block_earns_computed(tmp_path):
    """The numbers a receipt checks rarely sit in the sentence worth quoting.

    A reviewer quotes the sentence that makes the claim and cites the block
    that prints the figures behind it — a results paragraph, a table row. That
    receipt is still arithmetic over what the manuscript printed, so it earns
    ``computed``; the narrower rule, which read only the quoted sentence,
    called it judgement and lost the strongest findings in a run. What is not
    widened is the guarantee: a number that appears in none of the blocks the
    finding cites still earns nothing.
    """
    open_run(tmp_path, arm='single')
    token = srv.claim_agent('core-1')['agent_token']
    run = srv.current()
    numeric_block, _, number = numeric_quote(run)
    block, quote = quote_without(run, number, exclude=numeric_block)
    receipt = srv.calculate(token, f'{number} + {number}')['receipt']

    cited = srv.submit_finding(token, title='The totals do not add up', block_id=block,
                               quote=quote, comment='The figures it cites sum to something else.',
                               severity='moderate', evidence_blocks=[numeric_block],
                               calculate_receipt=receipt)
    assert cited['basis'] == 'computed'
    assert 'blocks this finding cites' in cited['basis_reason']

    uncited = srv.submit_finding(token, title='The totals do not add up either', block_id=block,
                                 quote=quote, comment='Same arithmetic, nothing cited for it.',
                                 severity='minor', calculate_receipt=receipt)
    assert uncited['basis'] == 'judgement'
    assert number in uncited['basis_reason']


def test_two_anchored_quotes_earn_two_quotes_and_claim_nothing_more(tmp_path):
    open_run(tmp_path, arm='single')
    token = srv.claim_agent('core-1')['agent_token']
    run = srv.current()
    first_block, first_quote = quote_from(run, 0)
    second_block, second_quote = quote_from(run, 1)
    stored = srv.submit_finding(token, title='Two passages disagree', block_id=first_block,
                                quote=first_quote, comment='...', severity='moderate',
                                corroborating_block_id=second_block,
                                corroborating_quote=second_quote)
    assert stored['basis'] == 'two-quotes'
    assert 'does not prove they conflict' in stored['basis_reason']


def test_a_corroborating_quote_that_does_not_anchor_stores_nothing(tmp_path):
    open_run(tmp_path, arm='single')
    token = srv.claim_agent('core-1')['agent_token']
    run = srv.current()
    block, quote = quote_from(run)
    result = srv.submit_finding(token, title='x', block_id=block, quote=quote, comment='...',
                                severity='minor', corroborating_block_id=block,
                                corroborating_quote='nowhere in this manuscript at all')
    assert 'request_rejected' in result
    assert run.findings == []


def test_escalate_is_the_specialist_track_only(tmp_path):
    open_run(tmp_path)
    run = srv.current()
    block, quote = quote_from(run)
    core = srv.submit_finding(srv.claim_agent('core-1')['agent_token'], title='x',
                              block_id=block, quote=quote, comment='...', severity='major',
                              escalate=True)
    spec = srv.submit_finding(srv.claim_agent('spec-stats')['agent_token'], title='y',
                              block_id=block, quote=quote, comment='...', severity='major',
                              escalate=True)
    assert core['escalate'] is False
    assert spec['escalate'] is True


def test_none_found_cannot_contradict_the_ledger(tmp_path):
    open_run(tmp_path, arm='single')
    token = srv.claim_agent('core-1')['agent_token']
    run = srv.current()
    block, quote = quote_from(run)
    srv.submit_finding(token, title='x', block_id=block, quote=quote, comment='...',
                       severity='minor')
    assert 'request_rejected' in srv.submit_report(token, 'All clear.', none_found=True)


# -- isolation --------------------------------------------------------------


def test_a_reviewer_sees_only_its_own_findings_and_the_editor_sees_all(tmp_path):
    open_run(tmp_path, reviewers=2)
    run = srv.current()
    block, quote = quote_from(run)
    other_block, other_quote = quote_from(run, 1)
    first = srv.claim_agent('core-1')['agent_token']
    second = srv.claim_agent('core-2')['agent_token']
    srv.submit_finding(first, title='mine', block_id=block, quote=quote, comment='...',
                       severity='minor')
    srv.submit_finding(second, title='theirs', block_id=other_block, quote=other_quote,
                       comment='...', severity='minor')
    assert [f['title'] for f in srv.list_findings(first)['findings']] == ['mine']
    assert [f['title'] for f in srv.list_findings(second)['findings']] == ['theirs']
    editor = srv.list_findings(srv.claim_agent('editor')['agent_token'])
    assert sorted(f['title'] for f in editor['findings']) == ['mine', 'theirs']
    assert editor['order'] == 'randomised per run'


# -- the ledger and the files ----------------------------------------------


def two_track_run(tmp_path, reviewers=2):
    """A whole run short of the editor: every stage reported, findings stored."""
    open_run(tmp_path, reviewers=reviewers)
    run = srv.current()
    uids = {}
    for index in range(1, reviewers + 1):
        token = srv.claim_agent(f'core-{index}')['agent_token']
        block, quote = quote_from(run, index - 1)
        stored = srv.submit_finding(token, title=f'Reviewer {index} finding', block_id=block,
                                    quote=quote, comment='...', severity='major')
        uids[f'core-{index}'] = stored['finding_uid']
        close(token, model='claude-sonnet-5-5')
    for position, track in enumerate(srv.SPECIALIST_TRACKS):
        token = srv.claim_agent(f'spec-{track}')['agent_token']
        if track == 'stats':
            block, quote = quote_from(run, position + 2)
            stored = srv.submit_finding(token, title='Statistics finding', block_id=block,
                                        quote=quote, comment='...', severity='moderate',
                                        escalate=True)
            uids['spec-stats'] = stored['finding_uid']
            close(token, model='claude-sonnet-5-5', report_markdown='# what I checked')
        else:
            close(token, none_found=True, model='claude-sonnet-5-5')
    return run, uids


def accept_editor(run, uids, **overrides):
    token = srv.claim_agent('editor')['agent_token']
    decisions = overrides.pop('decisions', [
        {'finding_uid': uid, 'placement': 'body', 'reason': 'It bears on the central claim.'}
        for uid in uids.values()])
    payload = {
        'summary': 'Two reviewers and the statistics audit agree on one weakness.',
        'decisions': decisions,
        'significance': 'Would matter to the field if it holds.',
        'strength_of_evidence': 'Partly supported; the main comparison is underpowered.',
        'verdict_reasoning': 'The claim is plausible but the evidence shown is thin.',
        'escalation_rulings': [{'finding_uid': uids['spec-stats'],
                                'ruling': 'Accepted: the authors must report the correction.'}],
    }
    payload.update(overrides)
    return token, srv.submit_editor(token, **payload)


def test_write_review_refuses_while_a_stage_has_not_reported(tmp_path):
    open_run(tmp_path, reviewers=2)
    run = srv.current()
    close(srv.claim_agent('core-1')['agent_token'], none_found=True)
    recorder = srv.claim_agent('recorder')['agent_token']
    refusal = srv.write_review(recorder)
    assert 'never reported' in refusal['request_rejected']
    assert 'core-2' in refusal['request_rejected']
    assert refusal['status'] == 'blocked'
    assert json.loads((run.review / 'progress.json').read_text())['status'] == 'blocked'


def test_write_review_refuses_without_an_accepted_editor_package(tmp_path):
    run, _ = two_track_run(tmp_path)
    close(srv.claim_agent('editor')['agent_token'], model='claude-sonnet-5-5')
    refusal = srv.write_review(srv.claim_agent('recorder')['agent_token'])
    assert 'never submitted an accepted pooled review' in refusal['request_rejected']


def test_every_finding_needs_exactly_one_decision(tmp_path):
    run, uids = two_track_run(tmp_path)
    _, refused = accept_editor(run, uids, decisions=[
        {'finding_uid': uids['core-1'], 'placement': 'body', 'reason': 'first'},
        {'finding_uid': uids['core-1'], 'placement': 'appendix', 'reason': 'again'},
    ])
    problems = ' '.join(refused['problems'])
    assert 'already decided' in problems
    assert 'have no decision' in problems


def test_a_placement_without_a_reason_is_refused(tmp_path):
    run, uids = two_track_run(tmp_path)
    _, refused = accept_editor(run, uids, decisions=[
        {'finding_uid': uid, 'placement': 'body', 'reason': ''} for uid in uids.values()])
    assert any('logged reason' in problem for problem in refused['problems'])


def test_the_body_holds_at_most_about_eight_findings(tmp_path, monkeypatch):
    monkeypatch.setattr(srv, 'BODY_LIMIT', 2)
    run, uids = two_track_run(tmp_path)
    _, refused = accept_editor(run, uids)
    assert any('at most about 2' in problem for problem in refused['problems'])


def test_an_escalated_finding_must_be_ruled_on_by_name(tmp_path):
    run, uids = two_track_run(tmp_path)
    _, refused = accept_editor(run, uids, escalation_rulings=[])
    assert any(uids['spec-stats'] in problem and 'needs a ruling' in problem
               for problem in refused['problems'])


def test_a_numeric_verdict_is_refused(tmp_path):
    run, uids = two_track_run(tmp_path)
    _, refused = accept_editor(run, uids, significance='7/10')
    assert any('judgement in words' in problem for problem in refused['problems'])


def test_a_computed_finding_leaves_the_body_only_over_an_anchored_rebuttal(tmp_path):
    open_run(tmp_path, arm='single')
    run = srv.current()
    token = srv.claim_agent('core-1')['agent_token']
    block, quote, number = numeric_quote(run)
    receipt = srv.calculate(token, f'{number} + {number}')['receipt']
    stored = srv.submit_finding(token, title='Arithmetic', block_id=block, quote=quote,
                                comment='...', severity='major', calculate_receipt=receipt)
    assert stored['basis'] == 'computed'
    close(token, model='claude-sonnet-5-5')
    # The arm has no editor slot, so the validator is exercised directly.
    run.required = (*run.required, 'editor')
    editor = srv.claim_agent('editor')['agent_token']
    base = {'summary': 's', 'significance': 'Modest.', 'strength_of_evidence': 'Thin.',
            'verdict_reasoning': 'r'}
    refused = srv.submit_editor(editor, decisions=[
        {'finding_uid': stored['finding_uid'], 'placement': 'appendix',
         'reason': 'I do not agree.'}], **base)
    assert any('rebuttal_quote' in problem for problem in refused['problems'])
    accepted = srv.submit_editor(editor, decisions=[
        {'finding_uid': stored['finding_uid'], 'placement': 'appendix',
         'reason': 'The paper states the correction elsewhere.',
         'rebuttal_block_id': block, 'rebuttal_quote': quote}], **base)
    assert accepted['accepted'] is True


def test_a_finished_run_writes_the_files_a_consumer_reads(tmp_path):
    run, uids = two_track_run(tmp_path)
    editor, accepted = accept_editor(run, uids)
    assert accepted['accepted'] is True, accepted
    close(editor, model='claude-sonnet-5-5')
    written = srv.write_review(srv.claim_agent('recorder')['agent_token'])
    assert written['status'] == 'complete'
    assert written['findings_by_track'] == {'core': 2, 'stats': 1}
    assert written['escalated'] == 1

    review = run.review
    for name in ('protocol.json', 'progress.json', 'manifest.json', 'editor-package.json',
                 'REVIEW.md'):
        assert (review / name).is_file(), name
    assert (review / 'audit_stats.md').read_text().startswith('# stats audit')
    assert (review / 'audit_citations.md').is_file()

    package = json.loads((review / 'editor-package.json').read_text())
    assert len(package['findings']) == 3
    assert package['verdict']['basis'] == 'judgement'
    assert package['escalation_rulings'][0]['finding_uid'] == uids['spec-stats']
    assert package['reviewer_count'] == 2
    assert {row['slot'] for row in package['reviewer_provenance']} >= {'core-1', 'spec-stats'}
    assert all(row['model'] == 'claude-sonnet-5-5' for row in package['reviewer_provenance'])

    protocol = json.loads((review / 'protocol.json').read_text())
    assert protocol['provider'] == 'claude-code'
    assert protocol['arm'] == 'full-two-track'
    assert protocol['finding_schema_version'] == srv.FINDING_SCHEMA_VERSION

    manifest = json.loads((review / 'manifest.json').read_text())
    assert manifest['paper_id'] == run.paper['id']
    assert any(key.startswith('engine/') for key in manifest['code'])
    assert manifest['plan']['reviewers'] == 2

    progress = json.loads((review / 'progress.json').read_text())
    assert progress['status'] == 'complete'
    assert progress['stages']['missing'] == []
    assert progress['review_guard_status'] == 'clean'
    assert (review / 'reviewer-1' / 'editor-package.json').is_file()


def test_a_run_where_nobody_found_anything_is_complete_without_findings(tmp_path):
    open_run(tmp_path, arm='single')
    close(srv.claim_agent('core-1')['agent_token'], none_found=True,
          model='claude-sonnet-5-5')
    written = srv.write_review(srv.claim_agent('recorder')['agent_token'])
    assert written['status'] == 'complete-without-findings'
    assert written['body_findings'] == 0


def test_mark_blocked_is_recorded_in_the_run_files(tmp_path):
    open_run(tmp_path, arm='single')
    run = srv.current()
    assert srv.mark_blocked('the subscription rate limit was reached')['status'] == 'blocked'
    progress = json.loads((run.review / 'progress.json').read_text())
    assert progress['status'] == 'blocked'
    assert 'rate limit' in progress['error']
    assert (run.review / 'manifest.json').is_file()


def test_only_the_recorder_writes_the_review(tmp_path):
    open_run(tmp_path, arm='single')
    token = srv.claim_agent('core-1')['agent_token']
    assert 'recorder writes' in srv.write_review(token)['request_rejected']


def test_the_tool_list_is_the_one_the_agents_are_allowed_to_call(tmp_path):
    names = {tool.__name__ for tool in srv.TOOLS}
    assert names >= set(srv.PAPER_TOOLS)
    assert {'ingest', 'claim_agent', 'submit_finding', 'submit_report', 'list_findings',
            'submit_editor', 'write_review', 'mark_blocked', 'run_status'} <= names
