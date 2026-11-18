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
from manuscripts import seal_converted

from peerreviewagents import issues as issue_model
from peerreviewagents import names
from peerreviewagents.plugin import server as srv
from peerreviewagents.runs import read_run

FIXTURE = Path(__file__).parent / 'fixtures' / 'single-column.pdf'


def read_whole_paper(token):
    """Read every part of the manuscript, as the brief tells every agent to.

    It spends nothing from the allowance, so a test that reads the paper and
    then counts tool calls counts the same numbers as one that did not.
    """
    run = srv.current()
    if 'read_paper' not in run.agent_for(token).allowed:
        return token  # the recorder and the citation workers read no manuscript
    for part in range(1, srv.paper_view.served(run.base) + 1):
        served = srv.read_paper(token, part=part)
        assert isinstance(served, str), served
    return token



# The claim codes ingest hands back, kept the way the workflow script keeps
# them: one per slot, each going to that slot's agent and nowhere else.
CODES = {}


@pytest.fixture(autouse=True)
def fresh_state():
    srv.STATE['run'] = None
    srv.STATE['plugin_root'] = None
    CODES.clear()
    yield
    srv.STATE['run'] = None


def open_run(tmp_path, seal=True, **kwargs):
    opened = srv.ingest(str(FIXTURE), str(tmp_path / 'run'), **kwargs)
    assert 'request_rejected' not in opened, opened
    CODES.update({row['slot']: row['claim_code'] for row in opened.get('claim_codes', [])})
    CODES['recorder_key'] = opened.get('recorder_key', '')
    if seal:
        # ingest leaves a draft, because the Read stage writes the lines. No
        # model reads anything here, so the run is sealed as converted.
        seal_converted(opened)
    return opened


def claim(slot, code=None):
    """Claim a slot as its own agent does: with the code minted for it at ingest.

    And then read the manuscript, which is the other thing its own agent does
    first — the server refuses a finding from a slot it has not served the
    whole of paper.md to. Reading spends nothing, so the allowance tests below
    still count in single calls.
    """
    taken = srv.claim_agent(slot, CODES.get(slot, '') if code is None else code)
    if 'agent_token' in taken:
        CODES.pop(slot, None)  # spent, exactly as the server treats it
        read_whole_paper(taken['agent_token'])
    return taken


def token_for(slot):
    """The agent token for one slot, claimed with that slot's own code."""
    taken = claim(slot)
    assert 'agent_token' in taken, taken
    return taken['agent_token']


def reclaim(slot):
    """Retry a stage: the recorder reissues the code, and the replacement claims it."""
    fresh = srv.reissue_claim_code(CODES['recorder_key'], slot)
    assert 'claim_code' in fresh, fresh
    CODES[slot] = fresh['claim_code']
    return token_for(slot)


def quote_from(run, index=0):
    """A passage that occurs once in the whole manuscript, and the line it is on.

    ``index`` picks the nth such passage, one per line, so two callers that
    want two different places in the paper get them. A quote that occurs once
    needs no line at all to anchor; the line is returned because a test that
    cites it is testing the line, and because an ambiguity test wants a quote
    whose line it knows.
    """
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


def numeric_quote(run):
    """A uniquely anchorable line carrying a number, its line, and that number.

    Never a power of ten: 10 and 100 are the unit conversions a receipt may
    carry without citing the paper for them, so a test that used one would pass
    whether or not the number was matched against the page at all.
    """
    text = run.base.text
    for line in range(1, run.base.line_count() + 1):
        whole = run.base.line_text(line).strip()
        numbers = [n for n in re.findall(srv.NUMERIC, whole)
                   if srv.as_number(n) is not None and not srv.is_power_of_ten(srv.as_number(n))]
        if numbers and 30 < len(whole) < 200 and text.count(whole) == 1:
            return line, whole, numbers[0]
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
    opened = open_run(tmp_path, arm='draft-check', tracks=['stats', 'numbers'])
    assert opened['slots'] == ['spec-stats', 'spec-numbers', 'recorder']


def test_an_unknown_arm_is_refused(tmp_path):
    assert 'request_rejected' in srv.ingest(str(FIXTURE), str(tmp_path / 'run'), arm='debate')


def test_a_refused_import_is_recorded_so_the_run_report_says_why(tmp_path):
    """The blocked reason is the server's own words about the manuscript.

    The starter makes the review folder and the workflow calls `ingest` from
    inside the session, where a rejection goes back to a model and nowhere
    else. Without this the folder said only that no plan reached the workflow,
    so a paper the server refused to import — a page the converter lost, an
    argument it would not take — read as a workflow that assigned no stage.
    """
    out = tmp_path / 'run'
    (out / names.RUN_DIRECTORY).mkdir(parents=True)  # the folder the starter makes
    refused = srv.ingest(str(FIXTURE), str(out), arm='debate')
    assert 'request_rejected' in refused

    recorded = json.loads((out / names.RUN_DIRECTORY / 'progress.json')
                          .read_text(encoding='utf-8'))
    assert recorded['status'] == 'blocked'
    assert recorded['error'] == refused['request_rejected']
    run = read_run(out)
    assert run['status'] == 'blocked'
    assert run['cause'] == refused['request_rejected']


def test_a_refusal_before_the_folder_exists_still_leaves_nothing_behind(tmp_path):
    """An argument no run can be made from is not a run that stopped."""
    assert 'request_rejected' in srv.ingest(str(FIXTURE), str(tmp_path / 'run'), arm='debate')
    assert not (tmp_path / 'run').exists()


def test_a_refusal_leaves_a_run_s_own_stage_ledger_alone(tmp_path):
    """A folder another process is serving keeps its progress file."""
    open_run(tmp_path)
    standing = tmp_path / 'run' / names.RUN_DIRECTORY / 'progress.json'
    before = standing.read_text(encoding='utf-8')
    assert 'stages' in json.loads(before), 'this test is not looking at a run ledger'
    srv.STATE['run'] = None  # a fresh server process, pointed at the same folder
    other = tmp_path / 'other.pdf'
    other.write_bytes(FIXTURE.read_bytes() + b'\n% a different file\n')
    assert 'request_rejected' in srv.ingest(str(other), str(tmp_path / 'run'))
    assert standing.read_text(encoding='utf-8') == before


def test_a_second_ingest_of_the_same_folder_attaches_to_the_run(tmp_path):
    open_run(tmp_path)
    again = srv.ingest(str(FIXTURE), str(tmp_path / 'run'))
    assert again['attached'] is True
    assert 'claim_codes' not in again, 'the codes go back once, to the recorder'
    assert 'slots' not in again


def test_a_folder_that_holds_another_manuscript_is_refused(tmp_path):
    """A reused folder kept the first import, so the review quoted the wrong paper."""
    open_run(tmp_path)
    other = tmp_path / 'other.pdf'
    other.write_bytes(FIXTURE.read_bytes() + b'\n% a different file\n')
    srv.STATE['run'] = None  # a fresh server process, pointed at the same folder
    refused = srv.ingest(str(other), str(tmp_path / 'run'))
    assert 'already holds a different manuscript' in refused['request_rejected']


def test_a_folder_that_holds_another_arm_is_refused(tmp_path):
    open_run(tmp_path, reviewers=2)
    srv.STATE['run'] = None
    refused = srv.ingest(str(FIXTURE), str(tmp_path / 'run'), arm='single')
    assert 'never pooled in one folder' in refused['request_rejected']
    srv.STATE['run'] = None
    regrown = srv.ingest(str(FIXTURE), str(tmp_path / 'run'), reviewers=3)
    assert 'request_rejected' in regrown
    srv.STATE['run'] = None
    resumed = srv.ingest(str(FIXTURE), str(tmp_path / 'run'), reviewers=2)
    assert 'request_rejected' not in resumed, resumed


# -- identity ---------------------------------------------------------------


def test_a_slot_outside_the_plan_is_refused(tmp_path):
    open_run(tmp_path, arm='single')
    assert 'request_rejected' in claim('spec-stats')
    assert 'request_rejected' in claim('core-2')


def test_the_token_carries_the_track_and_the_tool_list(tmp_path):
    open_run(tmp_path)
    core = claim('core-1')
    stats = claim('spec-stats')
    assert core['track'] == 'core' and stats['track'] == 'stats'
    assert 'view_supplement' in core['allowed_tools']
    assert 'view_supplement' not in stats['allowed_tools']
    assert core['agent_token'] != stats['agent_token']


def test_a_tool_call_needs_a_live_token(tmp_path):
    open_run(tmp_path)
    first = token_for('core-1')
    assert 'request_rejected' in srv.read_paper('not-a-token')
    reclaim('core-1')  # the recorder retried the stage, dropping the first holder
    assert 'superseded' in srv.read_paper(first)['request_rejected']


def test_a_slot_is_not_claimed_without_the_code_minted_for_it(tmp_path):
    """The probe claimed `recorder`, `core-2` and `spec-stats` by naming them.

    Nothing about a slot name is secret — the workflow's own log prints them —
    so a slot has to be held against something the other agents do not have.
    """
    open_run(tmp_path, reviewers=2)
    token_for('core-1')
    for slot in ('core-2', 'spec-stats', 'editor', 'recorder'):
        refused = srv.claim_agent(slot, 'core-1-guessed-the-name')
        assert 'not the claim code' in refused['request_rejected'], slot
    assert 'request_rejected' in srv.claim_agent('core-2')  # and no code at all is no claim


def test_a_code_belongs_to_one_slot_and_works_once(tmp_path):
    open_run(tmp_path, reviewers=2)
    elsewhere = srv.claim_agent('core-2', CODES['core-1'])
    assert 'not the claim code' in elsewhere['request_rejected']
    code = CODES['core-2']
    assert 'agent_token' in srv.claim_agent('core-2', code)
    again = srv.claim_agent('core-2', code)
    assert 'already been claimed' in again['request_rejected']
    assert 'recorder' in again['request_rejected']  # which says where a retry gets one


def test_a_fresh_code_for_a_retry_comes_from_the_recorder_alone(tmp_path):
    open_run(tmp_path, arm='single')
    first = token_for('core-1')
    assert 'request_rejected' in srv.reissue_claim_code('not-the-run-key', 'core-1')
    assert 'request_rejected' in srv.reissue_claim_code(CODES['recorder_key'], 'spec-stats')
    second = reclaim('core-1')
    assert second != first
    assert 'superseded' in srv.read_paper(first)['request_rejected']


def test_a_retry_does_not_buy_the_slot_a_second_tool_allowance(tmp_path, monkeypatch):
    """The allowance is the slot's, not the claim's: a re-claim continued the count."""
    monkeypatch.setitem(srv.CALL_BUDGET, 'core', 2)
    open_run(tmp_path, arm='single')
    phrase = srv.current().base.line_text(1)[:20]
    assert 'request_rejected' not in srv.search(token_for('core-1'), phrase)
    retried = reclaim('core-1')
    assert 'request_rejected' not in srv.search(retried, phrase)
    assert srv.search(retried, phrase).get('budget_exhausted') is True


def test_an_agent_cannot_call_a_tool_outside_its_track(tmp_path):
    open_run(tmp_path)
    editor = claim('editor')['agent_token']
    refusal = srv.view_page(editor, 1)['request_rejected']
    assert 'may not call view_page' in refusal


def test_the_manuscript_only_arm_removes_the_page_and_lookup_tools(tmp_path):
    open_run(tmp_path, arm='manuscript-only')
    core = claim('core-1')
    assert 'view_page' not in core['allowed_tools']
    assert 'search_literature' not in core['allowed_tools']
    assert 'read_paper' in core['allowed_tools']


def test_the_per_agent_tool_allowance_is_a_hard_stop(tmp_path, monkeypatch):
    monkeypatch.setitem(srv.CALL_BUDGET, 'core', 2)
    open_run(tmp_path, arm='single')
    token = claim('core-1')['agent_token']
    phrase = srv.current().base.line_text(1)[:20]
    assert 'request_rejected' not in srv.search(token, phrase)
    assert 'request_rejected' not in srv.search(token, phrase)
    exhausted = srv.search(token, phrase)
    assert exhausted.get('budget_exhausted') is True


def test_reading_the_manuscript_does_not_spend_the_allowance(tmp_path, monkeypatch):
    """The whole paper, twice over, and the allowance is untouched.

    A reviewer that pays per read spends its allowance deciding what to skip.
    The budget is for investigating what it read.
    """
    monkeypatch.setitem(srv.CALL_BUDGET, 'core', 2)
    open_run(tmp_path, arm='single')
    run = srv.current()
    token = claim('core-1')['agent_token']
    for _ in range(3):
        served = srv.read_paper(token)
        assert served.startswith('paper.md - part 1 of 1 - lines 1-')
    assert run.agent_for(token).calls == 0 and run.calls == 0
    assert 'request_rejected' not in srv.search(token, 'the')
    assert run.agent_for(token).calls == 1


def test_a_finding_from_a_slot_that_has_not_read_the_paper_stores_nothing(tmp_path):
    """Reviewers stopped reading and reviewed the part they had seen.

    Nothing refused that before: a reviewer that read a third of the paper
    could file a finding against it, and the review read as if the whole
    manuscript had been examined. The slot is served the whole file or it
    submits nothing, and the refusal says which parts are missing and that
    reading them is free.
    """
    open_run(tmp_path, arm='single')
    run = srv.current()
    taken = srv.claim_agent('core-1', CODES['core-1'])
    token = taken['agent_token']
    assert 'Call read_paper now' in taken['read_first']
    line, quote = quote_from(run, 0)
    refused = srv.submit_finding(token, title='Read nothing, found something', line=line,
                                 quote=quote, comment='Filed before reading.', severity='major')
    assert 'you have not been served the whole manuscript' in refused['request_rejected']
    assert 'costs nothing from your tool allowance' in refused['request_rejected']
    assert refused['unread_parts'] == [1]
    assert not (run.review / 'findings.jsonl').is_file()

    assert isinstance(srv.read_paper(token), str)
    stored = srv.submit_finding(token, title='Read it, then found it', line=line,
                                quote=quote, comment='Filed after reading.', severity='major')
    assert 'request_rejected' not in stored, stored

    # A reissued code is a new holder, and a new holder has read nothing.
    second = srv.reissue_claim_code(CODES['recorder_key'], 'core-1')
    retried = srv.claim_agent('core-1', second['claim_code'])['agent_token']
    again = srv.submit_finding(retried, title='A retry that read nothing', line=line,
                               quote=quote, comment='Filed before reading.', severity='major')
    assert again['unread_parts'] == [1]


# -- the guard --------------------------------------------------------------


def test_a_quote_that_is_not_in_the_manuscript_stores_nothing(tmp_path):
    open_run(tmp_path, arm='single')
    token = claim('core-1')['agent_token']
    run = srv.current()
    result = srv.submit_finding(token, title='Invented',
                                quote='a sentence this paper does not contain anywhere',
                                comment='...', severity='major')
    assert result.get('quarantined') is True
    assert 'nearest' in result, result
    assert run.findings == []
    assert (run.review / 'quarantined.jsonl').is_file()


def test_an_anchored_finding_is_stored_with_its_track_and_a_uid(tmp_path):
    open_run(tmp_path, arm='single')
    token = claim('core-1')['agent_token']
    run = srv.current()
    line, quote = quote_from(run)
    stored = srv.submit_finding(token, title='The central claim outruns the data',
                                line=line, quote=quote,
                                comment='The result shown does not reach this conclusion.',
                                severity='major')
    assert stored['stored'] is True
    assert stored['track'] == 'core'
    assert len(stored['finding_uid']) == 16
    assert stored['basis'] == 'judgement'
    rows = [json.loads(line) for line in
            (run.review / 'findings.jsonl').read_text(encoding='utf-8').splitlines()]
    assert [row['finding_uid'] for row in rows] == [stored['finding_uid']]


def test_the_uid_is_stable_across_a_re_review_of_the_same_passage(tmp_path):
    open_run(tmp_path, arm='single')
    run = srv.current()
    line, quote = quote_from(run)
    token = token_for('core-1')
    first = srv.submit_finding(token, title='Same issue',
                               line=line, quote=quote, comment='x', severity='minor')
    second = srv.submit_finding(reclaim('core-1'), title='Same issue',
                                line=line, quote=quote, comment='x', severity='minor')
    assert first['finding_uid'] == second['finding_uid']


def test_only_a_receipt_over_quoted_numbers_earns_computed(tmp_path):
    open_run(tmp_path, arm='single')
    token = claim('core-1')['agent_token']
    run = srv.current()
    line, quote, number = numeric_quote(run)
    receipt = srv.calculate(token, f'{number} + {number}')['receipt']
    computed = srv.submit_finding(token, title='The arithmetic does not hold', line=line,
                                  quote=quote, comment='The sum is not what is reported.',
                                  severity='moderate', calculate_receipt=receipt)
    assert computed['basis'] == 'computed'
    off_paper = srv.calculate(token, '987654 * 31')['receipt']
    judged = srv.submit_finding(token, title='Numbers from nowhere', line=line, quote=quote,
                                comment='...', severity='minor', calculate_receipt=off_paper)
    assert judged['basis'] == 'judgement'
    assert 'none of the cited lines print' in judged['basis_reason']
    assert '987654' in judged['basis_reason']


def quote_without(run, number, exclude):
    """A uniquely anchorable passage on a line that does not print ``number``."""
    index = 0
    while index < run.base.line_count():
        line, quote = quote_from(run, index)
        if line != exclude and number not in run.base.line_text(line):
            return line, quote
        index += 1
    raise AssertionError('every anchorable line in this fixture prints that number')


def test_a_receipt_over_numbers_on_a_cited_line_earns_computed(tmp_path):
    """The numbers a receipt checks rarely sit in the sentence worth quoting.

    A reviewer quotes the sentence that makes the claim and cites the lines
    that print the figures behind it — a results paragraph, a table row. That
    receipt is still arithmetic over what the manuscript printed, so it earns
    ``computed``; the narrower rule, which read only the quoted sentence,
    called it judgement and lost the strongest findings in a run. What is not
    widened is the guarantee: a number that appears on none of the lines the
    finding cites still earns nothing.
    """
    open_run(tmp_path, arm='single')
    token = claim('core-1')['agent_token']
    run = srv.current()
    numeric_line, _, number = numeric_quote(run)
    line, quote = quote_without(run, number, exclude=numeric_line)
    receipt = srv.calculate(token, f'{number} + {number}')['receipt']

    cited = srv.submit_finding(token, title='The totals do not add up', line=line,
                               quote=quote, comment='The figures it cites sum to something else.',
                               severity='moderate', evidence_lines=[numeric_line],
                               calculate_receipt=receipt)
    assert cited['basis'] == 'computed'
    assert 'lines this finding cites' in cited['basis_reason']

    uncited = srv.submit_finding(token, title='The totals do not add up either', line=line,
                                 quote=quote, comment='Same arithmetic, nothing cited for it.',
                                 severity='minor', calculate_receipt=receipt)
    assert uncited['basis'] == 'judgement'
    assert number in uncited['basis_reason']


def test_two_anchored_quotes_earn_two_quotes_and_claim_nothing_more(tmp_path):
    open_run(tmp_path, arm='single')
    token = claim('core-1')['agent_token']
    run = srv.current()
    first_line, first_quote = quote_from(run, 0)
    second_line, second_quote = quote_from(run, 1)
    stored = srv.submit_finding(token, title='Two passages disagree', line=first_line,
                                quote=first_quote, comment='...', severity='moderate',
                                corroborating_line=second_line,
                                corroborating_quote=second_quote)
    assert stored['basis'] == 'two-quotes'
    assert 'does not prove they conflict' in stored['basis_reason']


def test_a_corroborating_quote_that_does_not_anchor_stores_nothing(tmp_path):
    open_run(tmp_path, arm='single')
    token = claim('core-1')['agent_token']
    run = srv.current()
    line, quote = quote_from(run)
    result = srv.submit_finding(token, title='x', line=line, quote=quote, comment='...',
                                severity='minor',
                                corroborating_quote='nowhere in this manuscript at all')
    assert 'request_rejected' in result
    assert run.findings == []


def test_escalate_is_the_specialist_track_only(tmp_path):
    open_run(tmp_path)
    run = srv.current()
    line, quote = quote_from(run)
    core = srv.submit_finding(claim('core-1')['agent_token'], title='x',
                              line=line, quote=quote, comment='...', severity='major',
                              escalate=True)
    spec = srv.submit_finding(claim('spec-stats')['agent_token'], title='y',
                              line=line, quote=quote, comment='...', severity='major',
                              escalate=True)
    assert core['escalate'] is False
    assert spec['escalate'] is True


def test_none_found_cannot_contradict_the_ledger(tmp_path):
    open_run(tmp_path, arm='single')
    token = claim('core-1')['agent_token']
    run = srv.current()
    line, quote = quote_from(run)
    srv.submit_finding(token, title='x', line=line, quote=quote, comment='...',
                       severity='minor')
    assert 'request_rejected' in srv.submit_report(token, 'All clear.', none_found=True)


# -- isolation --------------------------------------------------------------


def test_a_reviewer_sees_only_its_own_findings_and_the_editor_sees_all(tmp_path):
    open_run(tmp_path, reviewers=2)
    run = srv.current()
    line, quote = quote_from(run)
    other_line, other_quote = quote_from(run, 1)
    first = claim('core-1')['agent_token']
    second = claim('core-2')['agent_token']
    srv.submit_finding(first, title='mine', line=line, quote=quote, comment='...',
                       severity='minor')
    srv.submit_finding(second, title='theirs', line=other_line, quote=other_quote,
                       comment='...', severity='minor')
    assert [f['title'] for f in srv.list_findings(first)['findings']] == ['mine']
    assert [f['title'] for f in srv.list_findings(second)['findings']] == ['theirs']
    editor = srv.list_findings(claim('editor')['agent_token'])
    assert sorted(f['title'] for f in editor['findings']) == ['mine', 'theirs']
    assert editor['order'] == 'randomised per run'


def test_a_reviewer_is_never_told_which_other_slots_the_run_has(tmp_path):
    """Whether a core reviewer knows the specialists exist is what the arms compare.

    The ledger it gets back from its own calls named every slot in the run, so a
    core reviewer could read that six specialists would check its work.
    """
    open_run(tmp_path, reviewers=2)
    run = srv.current()
    line, quote = quote_from(run)
    core = token_for('core-1')
    srv.submit_finding(core, title='mine', line=line, quote=quote, comment='...',
                       severity='minor')
    listed = srv.list_findings(core)
    closed = close(core, model='claude-sonnet-5-5')
    unknown = srv.claim_agent('core-9', 'guessed')['request_rejected']
    for text in (json.dumps(listed['ledger']), json.dumps(closed['ledger']), unknown):
        assert 'spec-' not in text, text
        assert 'core-2' not in text, text
        assert 'editor' not in text, text
    assert listed['ledger']['scope'] == 'your own stage only'
    full = json.dumps(srv.list_findings(token_for('editor'))['ledger'])
    assert 'core-2' in full and 'spec-stats' in full


def test_a_retitled_finding_is_the_same_finding_and_both_rows_are_kept(tmp_path):
    """The uid hashed the model-written title, so a better title filed a second issue."""
    open_run(tmp_path, arm='single')
    run = srv.current()
    token = token_for('core-1')
    line, quote = quote_from(run)
    first = srv.submit_finding(token, title='The sample size is unclear', line=line,
                               quote=quote, comment='...', severity='moderate')
    second = srv.submit_finding(token, title='Underpowered: the sample size is never stated',
                                line=line, quote=quote, comment='A clearer account.',
                                severity='major')
    assert first['finding_uid'] == second['finding_uid']
    assert second['findings_stored_by_you'] == 1
    assert second['submissions_by_you'] == 2
    rows = [json.loads(line) for line in
            (run.review / 'findings.jsonl').read_text(encoding='utf-8').splitlines()]
    assert len(rows) == 2, 'storage merged two submissions and lost one'
    assert [f['title'] for f in srv.list_findings(token)['findings']] == [
        'Underpowered: the sample size is never stated']


def test_a_revision_shares_no_finding_uid_with_the_run_it_revises(tmp_path):
    """A new PDF renames every finding, because the paper's own id is in the uid.

    A consumer comparing an old run against the re-review of a revision cannot
    pair them by ``finding_uid``: a paper's id is a digest of the PDF it came
    from, so two runs over two files never share one — the comparison would
    have called every finding gone and every finding new, on a revision that
    changed nothing at all. The same reviewer, the same line and the same quote
    are used here, so the only difference is the file.
    """
    revision = tmp_path / 'revision.pdf'
    revision.write_bytes(FIXTURE.read_bytes() + b'\n% a revised manuscript\n')
    open_run(tmp_path / 'first', arm='single')
    line, quote = quote_from(srv.current())
    before = srv.submit_finding(token_for('core-1'), title='The sample size is unclear',
                                line=line, quote=quote, comment='...', severity='major')

    # The re-review is its own server process, as the skill requires: one
    # process serves one run, and it refuses a second.
    assert 'request_rejected' in srv.ingest(str(revision), str(tmp_path / 'second' / 'run'))
    srv.STATE['run'] = None
    CODES.clear()
    opened = srv.ingest(str(revision), str(tmp_path / 'second' / 'run'), arm='single')
    assert 'request_rejected' not in opened, opened
    CODES.update({row['slot']: row['claim_code'] for row in opened.get('claim_codes', [])})
    CODES['recorder_key'] = opened.get('recorder_key', '')
    seal_converted(opened)
    again = srv.current()
    assert quote_from(again) == (line, quote), 'the revision was meant to read the same'
    after = srv.submit_finding(token_for('core-1'), title='The sample size is unclear',
                               line=line, quote=quote, comment='...', severity='major')
    assert after['finding_uid'] != before['finding_uid']


def test_agreement_between_two_reviewers_is_two_rows_the_editor_merges(tmp_path):
    open_run(tmp_path, reviewers=2, arm='reviewers-only')
    run = srv.current()
    line, quote = quote_from(run)
    first = srv.submit_finding(token_for('core-1'), title='Same worry', line=line,
                               quote=quote, comment='...', severity='major')
    second = srv.submit_finding(token_for('core-2'), title='Same worry', line=line,
                                quote=quote, comment='...', severity='major')
    assert first['finding_uid'] != second['finding_uid'], 'two reviewers, two rows'
    close(run.agents['core-1'].token, model='claude-sonnet-5-5')
    close(run.agents['core-2'].token, model='claude-sonnet-5-5')
    editor = token_for('editor')
    stored = srv.submit_issues(editor, [{
        'name': 'Both reviewers read the same sentence as unsupported',
        'severity': 'major', 'category': 'claims-and-conclusions', 'rank': 1,
        'statement': 'Two reviewers independently read the same passage as claiming more than '
                     'the data behind it support.',
        'members': [first['finding_uid'], second['finding_uid']]}])
    assert stored['stored'] == 1, stored
    assert stored['findings_without_issue'] == 0
    accepted = srv.submit_editor(
        editor, summary='Both reviewers raise one weakness.',
        significance='Would matter to the field if it holds.',
        strength_of_evidence='Partly supported.',
        verdict_reasoning='One weakness, raised twice.')
    assert accepted['accepted'] is True, accepted
    close(editor, model='claude-sonnet-5-5')
    assert srv.write_review(token_for('recorder'))['status'] == 'complete'
    document = json.loads((run.review / 'issues.json').read_text(encoding='utf-8'))
    assert len(document['issues']) == 1, 'one issue, raised by two reviewers'
    issue = document['issues'][0]
    assert issue['raised_by'] == 2
    assert [member['finding_uid'] for member in issue['members']] == [
        first['finding_uid'], second['finding_uid']]
    assert {member['slot'] for member in issue['members']} == {'core-1', 'core-2'}


def test_a_receipt_input_is_matched_by_value_not_by_how_it_is_written(tmp_path):
    """0.50 is 0.5. String-matching a receipt against the page lost real arithmetic."""
    open_run(tmp_path, arm='single')
    run = srv.current()
    token = token_for('core-1')
    line, quote, number = numeric_quote(run)
    rewritten = number + '0' if '.' in number else number + '.0'
    assert rewritten != number and float(rewritten) == float(number)
    receipt = srv.calculate(token, f'{rewritten} * 100')['receipt']
    stored = srv.submit_finding(token, title='The percentage does not follow', line=line,
                                quote=quote, comment='The conversion is out by a factor.',
                                severity='moderate', calculate_receipt=receipt)
    assert stored['basis'] == 'computed', stored['basis_reason']


def test_a_finding_cites_the_lines_it_rests_on_and_not_every_line(tmp_path):
    open_run(tmp_path, arm='single')
    run = srv.current()
    token = token_for('core-1')
    line, quote = quote_from(run)
    every = list(range(1, srv.MAX_EVIDENCE_LINES + 2))
    assert len(every) == srv.MAX_EVIDENCE_LINES + 1, 'this fixture is too short to test the cap'
    refused = srv.submit_finding(token, title='Everything is evidence', line=line,
                                 quote=quote, comment='...', severity='minor',
                                 evidence_lines=every)
    assert f'at most {srv.MAX_EVIDENCE_LINES}' in refused['request_rejected']
    assert run.findings == []


# -- the ledger and the files ----------------------------------------------


def two_track_run(tmp_path, reviewers=2, model='claude-sonnet-5-5', unclosed=(), **opened):
    """A whole run short of the editor: every stage reported, findings stored.

    ``model`` is what every stage *reports* running, and anything in ``opened``
    goes to ``ingest`` — which is how a test asks for a run whose plan and
    whose reports disagree. A core slot named in ``unclosed`` stores its
    finding and never calls ``submit_report``, which is the shape SPOT 0.9.0's
    dark-kinome run ended in.
    """
    open_run(tmp_path, reviewers=reviewers, **opened)
    run = srv.current()
    uids = {}
    for index in range(1, reviewers + 1):
        token = claim(f'core-{index}')['agent_token']
        line, quote = quote_from(run, index - 1)
        stored = srv.submit_finding(token, title=f'Reviewer {index} finding', line=line,
                                    quote=quote, comment='...', severity='major')
        uids[f'core-{index}'] = stored['finding_uid']
        if f'core-{index}' not in unclosed:
            close(token, model=model)
    for position, track in enumerate(srv.SPECIALIST_TRACKS):
        token = claim(f'spec-{track}')['agent_token']
        if track == 'stats':
            line, quote = quote_from(run, position + 2)
            stored = srv.submit_finding(token, title='Statistics finding', line=line,
                                        quote=quote, comment='...', severity='moderate',
                                        escalate=True)
            uids['spec-stats'] = stored['finding_uid']
            close(token, model=model, report_markdown='# what I checked')
        else:
            close(token, none_found=True, model=model)
    return run, uids


def issue_batch(uids, severity='major', category='claims-and-conclusions'):
    """One issue per finding, ranked in the order the uids came in.

    This is the smallest grouping that meets the editor's contract — every
    finding in the ledger belongs to exactly one issue, and the ranks are 1 to
    N — so a test that is not about the grouping can say nothing about it.
    """
    return [{'name': f'What {slot} raised', 'severity': severity, 'category': category,
             'rank': rank,
             'statement': 'The manuscript asserts more than the passage behind it supports, '
                          'and the authors have to say which reading they mean.',
             'members': [uid]}
            for rank, (slot, uid) in enumerate(uids.items(), 1)]


def accept_editor(run, uids, **overrides):
    """Group the findings into issues, then close the pooled review.

    ``issues`` replaces the default one-issue-per-finding grouping; everything
    else in ``overrides`` goes to ``submit_editor``.
    """
    token = claim('editor')['agent_token']
    issues = overrides.pop('issues', issue_batch(uids))
    if issues:
        stored = srv.submit_issues(token, issues)
        assert stored.get('stored') == len(issues), stored
    payload = {
        'summary': 'Two reviewers and the statistics audit agree on one weakness.',
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
    close(claim('core-1')['agent_token'], none_found=True)
    recorder = claim('recorder')['agent_token']
    refusal = srv.write_review(recorder)
    assert 'never reported' in refusal['request_rejected']
    assert 'core-2' in refusal['request_rejected']
    assert refusal['status'] == 'blocked'
    assert json.loads((run.review / 'progress.json').read_text(encoding='utf-8'))['status'] == 'blocked'


def test_a_stage_that_delivered_findings_without_a_report_is_a_gap_not_a_blocked_run(tmp_path):
    """SPOT 0.9.0, dark-kinome: the findings were in the review and the run was blocked.

    core-1 stored five findings, the editor ruled on them, the pooled review
    was accepted — and ``write_review`` refused and marked the run blocked
    because that slot had no report, which threw out a finished review in order
    to report a missing summary. A stage that delivered findings is a gap in
    the record, not a run that did not happen: the review is written, the
    status is complete, and the ledger, the status note and ``REVIEW.md`` all
    name the stage whose summary and limitations are missing from it.
    """
    run, uids = two_track_run(tmp_path, unclosed=('core-1',))
    editor, accepted = accept_editor(run, uids)
    assert accepted['accepted'] is True, accepted
    close(editor, model='claude-sonnet-5-5')
    written = srv.write_review(claim('recorder')['agent_token'])
    assert written['status'] == 'complete', written
    assert written['ledger']['missing'] == ['core-1']
    assert written['ledger']['missing_with_findings'] == ['core-1']
    assert written['ledger']['silent'] == []

    document = json.loads((run.review / 'issues.json').read_text(encoding='utf-8'))
    members = [member['finding_uid'] for issue in document['issues']
               for member in issue['members']]
    assert uids['core-1'] in members, \
        'the finding is in an issue of the review, which is why blocking the run lost it'
    assert 'core-1' not in {row['slot'] for row in document['reviewer_provenance']}, \
        'there is no report to take a provenance row from, and none is invented'

    reading = read_run(run.review)
    assert reading['status'] == 'complete'
    assert 'core-1 delivered findings but never filed a report' in reading['note']
    assert 'summary and limitations are not in this review' in reading['note']
    assert 'never filed a report' in (run.review / 'REVIEW.md').read_text(encoding='utf-8')


def test_a_stage_that_reviewed_nothing_holds_the_run_beside_one_that_only_missed_its_report(
        tmp_path):
    """The other half of the rule, and the reason it is not simply `missing`.

    core-1 stored a finding and filed no report; core-2 was never claimed at
    all. The first is a gap, the second is a third of the manuscript nobody
    reviewed, so the run is still blocked — and the refusal names the silent
    slots rather than every slot absent from the ledger.
    """
    open_run(tmp_path, reviewers=2)
    run = srv.current()
    token = claim('core-1')['agent_token']
    line, quote = quote_from(run, 0)
    srv.submit_finding(token, title='Stored and never closed', line=line, quote=quote,
                       comment='...', severity='major')
    for track in srv.SPECIALIST_TRACKS:
        close(claim(f'spec-{track}')['agent_token'], none_found=True)
    refusal = srv.write_review(claim('recorder')['agent_token'])
    assert refusal['status'] == 'blocked'
    assert 'core-2' in refusal['request_rejected']
    assert 'core-1' not in refusal['request_rejected']
    assert refusal['ledger']['missing_with_findings'] == ['core-1']
    assert refusal['ledger']['silent'] == ['core-2', 'editor']
    assert not (run.review / 'REVIEW.md').is_file()


def test_every_closure_is_on_disk_whether_it_was_taken_or_refused(tmp_path):
    """A refused ``submit_report`` used to leave nothing behind at all.

    dark-kinome could be read no further than "core-1 has no report":
    ``tool-log.jsonl`` records the investigation tools only and the ledger
    records the reports that landed, so whether that slot never called
    ``submit_report`` or called it and was refused — over a spent token, or
    over a ``none_found`` that contradicted its own findings — was not in the
    run folder in any form. One row per call now says which.
    """
    open_run(tmp_path, arm='single')
    run = srv.current()
    token = claim('core-1')['agent_token']
    line, quote = quote_from(run, 0)
    srv.submit_finding(token, title='One finding', line=line, quote=quote, comment='...',
                       severity='major')
    assert 'request_rejected' in srv.submit_report(token, 'Nothing to report.', none_found=True)
    assert 'request_rejected' in srv.submit_report('not-a-token', 'Let me close something')
    close(token, model='claude-sonnet-5-5')

    rows = [json.loads(line) for line
            in (run.review / 'closures.jsonl').read_text(encoding='utf-8').splitlines()]
    assert [row['accepted'] for row in rows] == [False, False, True]
    assert all(row['tool'] == 'submit_report' for row in rows)
    assert rows[0]['slot'] == 'core-1'
    assert 'none_found contradicts the ledger' in rows[0]['refusal']
    assert rows[1]['slot'] is None, 'a token the run does not know names no slot'
    assert rows[2] == {'at': rows[2]['at'], 'tool': 'submit_report', 'slot': 'core-1',
                       'attempt': 1, 'accepted': True, 'refusal': None}


def test_write_review_refuses_without_an_accepted_editor_package(tmp_path):
    run, _ = two_track_run(tmp_path)
    # The editor cannot close its stage before its review is accepted either,
    # so the run reaches write_review with no package and no closure.
    premature = srv.submit_report(claim('editor')['agent_token'], 'Nothing further.',
                                  model='claude-sonnet-5-5')
    assert 'submit_editor has not accepted a review yet' in premature['request_rejected']
    refusal = srv.write_review(claim('recorder')['agent_token'])
    assert 'never submitted an accepted pooled review' in refusal['request_rejected']


def test_every_finding_belongs_to_exactly_one_issue(tmp_path):
    """A finding in two issues, and a finding in none: both refused, and said plainly.

    The issue is the unit of the review and the ledger is what it is drawn
    from, so the two have to account for each other exactly. A finding in two
    issues would be one weakness counted twice in "raised by N"; a finding in
    none would be a reviewer's work the review never mentions.
    """
    run, uids = two_track_run(tmp_path)
    editor = claim('editor')['agent_token']
    first = issue_batch({'core-1': uids['core-1']})
    assert srv.submit_issues(editor, first)['stored'] == 1
    again = [{**first[0], 'name': 'A second issue over the same finding', 'rank': 2}]
    refused = srv.submit_issues(editor, again)
    assert 'request_rejected' in refused
    problems = ' '.join(refused['problems'])
    assert 'already a member of' in problems and uids['core-1'] in problems

    closing = srv.submit_editor(
        editor, summary='Only one of three findings is in an issue.',
        significance='Would matter to the field if it holds.',
        strength_of_evidence='Partly supported.',
        verdict_reasoning='The claim is plausible but the evidence shown is thin.')
    assert 'request_rejected' in closing
    assert closing['findings_without_issue'] == 2
    assert any('belong to no issue' in problem for problem in closing['problems'])


def test_an_issue_without_a_statement_or_a_category_is_refused(tmp_path):
    """The statement is the paragraph the authors read, so an issue without one is not one."""
    run, uids = two_track_run(tmp_path)
    editor = claim('editor')['agent_token']
    batch = issue_batch(uids)
    batch[0] = {**batch[0], 'statement': 'Needs work.'}
    batch[1] = {**batch[1], 'category': 'vibes'}
    refused = srv.submit_issues(editor, batch)
    problems = ' '.join(refused['problems'])
    assert 'statement is the one paragraph' in problems
    assert 'category is one of' in problems
    assert srv.current().agents['editor'].issues == {}, 'nothing in a bad batch is stored'


def test_a_batch_larger_than_the_cap_is_refused_and_keeps_what_landed(tmp_path):
    """The batch size is what makes a large finding set fit a bounded turn budget.

    The editor's turns are what ran out on the 40-plus finding runs, so issues
    go in batch by batch and a batch over the cap is refused by size alone —
    with the issues already submitted still held, so the editor splits the
    batch rather than starting again.
    """
    run, uids = two_track_run(tmp_path)
    editor = claim('editor')['agent_token']
    assert srv.submit_issues(editor, issue_batch({'core-1': uids['core-1']}))['stored'] == 1
    oversized = [{**issue_batch(uids)[0], 'name': f'Issue {number}', 'rank': number}
                 for number in range(2, srv.issue_model.MAX_ISSUE_BATCH + 3)]
    refused = srv.submit_issues(editor, oversized)
    assert f'At most {srv.issue_model.MAX_ISSUE_BATCH} issues in one call' in \
        refused['request_rejected']
    assert refused['issues_submitted'] == 1, 'what already landed is still held'


def test_an_escalated_finding_must_be_ruled_on_by_name(tmp_path):
    run, uids = two_track_run(tmp_path)
    _, refused = accept_editor(run, uids, escalation_rulings=[])
    assert any(uids['spec-stats'] in problem and 'needs a ruling' in problem
               for problem in refused['problems'])


def test_a_numeric_verdict_is_refused(tmp_path):
    run, uids = two_track_run(tmp_path)
    _, refused = accept_editor(run, uids, significance='7/10')
    assert any('judgement in words' in problem for problem in refused['problems'])


def test_a_computed_finding_becomes_a_suggestion_only_over_an_anchored_rebuttal(tmp_path):
    """Nothing is hidden now, so burying a checked sum means calling it minor.

    The protection is the same one the body and the appendix needed, moved to
    the axis where burial now happens: an issue holding a finding whose
    arithmetic was checked against the manuscript cannot be a suggestion
    unless the editor quotes the passage that answers it, anchored exactly.
    """
    open_run(tmp_path, arm='single')
    run = srv.current()
    token = claim('core-1')['agent_token']
    line, quote, number = numeric_quote(run)
    receipt = srv.calculate(token, f'{number} + {number}')['receipt']
    stored = srv.submit_finding(token, title='Arithmetic', line=line, quote=quote,
                                comment='...', severity='major', calculate_receipt=receipt)
    assert stored['basis'] == 'computed'
    close(token, model='claude-sonnet-5-5')
    # The arm has no editor slot, so the validator is exercised directly. The
    # slot appears after ingest minted the codes, so its code comes from the
    # recorder, which is where a code for a slot without one comes from.
    run.required = (*run.required, 'editor')
    editor = reclaim('editor')
    issue = {'name': 'The sum is a detail', 'severity': 'minor',
             'category': 'statistics-and-inference', 'rank': 1,
             'statement': 'The arithmetic does not come out, but the point it is attached to '
                          'does not rest on that number.',
             'members': [stored['finding_uid']]}
    refused = srv.submit_issues(editor, [issue])
    assert any('rebuttal_quote' in problem for problem in refused['problems'])
    accepted = srv.submit_issues(editor, [{**issue, 'rebuttal_line': line,
                                           'rebuttal_quote': quote}])
    assert accepted['stored'] == 1, accepted
    closed = srv.submit_editor(editor, summary='One suggestion over an answered sum.',
                               significance='Modest.', strength_of_evidence='Thin.',
                               verdict_reasoning='r')
    assert closed['accepted'] is True, closed
    # And a major issue over the same finding needs no rebuttal: the rule is
    # about calling a receipt a suggestion, not about receipts.
    srv.current().agents['editor'].issues.clear()
    srv.current().agents['editor'].editor_package = None
    assert srv.submit_issues(editor, [{**issue, 'severity': 'major'}])['stored'] == 1


#: The passage the supplement prints, which the rebuttal below quotes.
SUPPLEMENT_SENTENCE = ('The totals in the main text are rounded to whole units, so the parts '
                       'sum to the reported figure only to that precision.')


def supplement_folder(tmp_path, sentence=SUPPLEMENT_SENTENCE):
    """A folder holding one ``.docx`` supplement carrying *sentence*.

    Written byte by byte, because what the import reads out of a ``.docx`` is
    one entry in the zip — ``word/document.xml`` — and a fixture file would
    carry a hundred others that say nothing about this test.
    """
    import zipfile

    folder = Path(tmp_path) / 'supplements'
    folder.mkdir(parents=True, exist_ok=True)
    body = ''.join(f'<w:p><w:r><w:t>{text}</w:t></w:r></w:p>' for text in
                   ('Supplementary note on the totals', sentence))
    with zipfile.ZipFile(folder / 'supplementary-note.docx', 'w') as archive:
        archive.writestr('word/document.xml',
                         '<?xml version="1.0"?><w:document xmlns:w="http://schemas.'
                         'openxmlformats.org/wordprocessingml/2006/main"><w:body>'
                         + body + '</w:body></w:document>')
    return folder


def computed_run_with_a_supplement(tmp_path):
    """A run whose manuscript has a supplement, and one computed finding in it."""
    open_run(tmp_path, arm='single', supplements=str(supplement_folder(tmp_path)))
    run = srv.current()
    token = claim('core-1')['agent_token']
    line, quote, number = numeric_quote(run)
    receipt = srv.calculate(token, f'{number} + {number}')['receipt']
    stored = srv.submit_finding(token, title='Arithmetic', line=line, quote=quote,
                                comment='the parts do not sum to the total as printed',
                                severity='major', calculate_receipt=receipt)
    assert stored['basis'] == 'computed', stored
    close(token, model='claude-sonnet-5-5')
    run.required = (*run.required, 'editor')
    return run, reclaim('editor'), stored['finding_uid']


def test_a_rebuttal_quoted_from_a_supplement_is_anchored_in_the_supplement(tmp_path):
    """The quote was looked for in paper.md, where a supplement's words are not.

    An issue holds members from several documents at once, so the member's
    document is not the rebuttal's: the pointer carries its own, read exactly
    as a member's ``document`` is, and empty means ``paper.md``.
    """
    run, editor, uid = computed_run_with_a_supplement(tmp_path)
    document, = [name for name in run.base.document_names() if name != 'paper.md']
    assert SUPPLEMENT_SENTENCE in run.base.documents[document]
    issue = {'name': 'Rounding, not arithmetic', 'severity': 'minor',
             'category': 'statistics-and-inference', 'rank': 1,
             'statement': 'The sum does not come out as printed, and the supplement says the '
                          'figures are rounded, so this is a presentation point.',
             'members': [uid], 'rebuttal_quote': SUPPLEMENT_SENTENCE}

    # Without the document it is looked for in paper.md, which does not hold it.
    refused = srv.submit_issues(editor, [issue])
    assert any('rebuttal_quote' in problem for problem in refused['problems']), refused
    assert any('rebuttal_document' in problem for problem in refused['problems']), refused

    accepted = srv.submit_issues(editor, [{**issue, 'rebuttal_document': document}])
    assert accepted['stored'] == 1, accepted

    closed = srv.submit_editor(editor, summary='One suggestion the supplement answers.',
                               significance='Modest.', strength_of_evidence='Thin.',
                               verdict_reasoning='r')
    assert closed['accepted'] is True, closed
    close(editor, model='claude-sonnet-5-5')
    srv.write_review(claim('recorder')['agent_token'])

    stored, = json.loads((run.review / 'issues.json').read_text(encoding='utf-8'))['issues']
    assert stored['rebuttal_document'] == document
    assert stored['rebuttal_quote'] == SUPPLEMENT_SENTENCE
    assert stored['rebuttal_line'] == run.locate(SUPPLEMENT_SENTENCE, None, document)['line']
    # And the review says which file that line is in, because a line number on
    # its own reads as a line of the manuscript.
    review = (run.review / 'REVIEW.md').read_text(encoding='utf-8')
    assert f"in {document} on line {stored['rebuttal_line']}" in review


def test_a_rebuttal_document_is_a_document_of_this_manuscript_or_nothing(tmp_path):
    """A name nothing holds is refused by name, with what the base does hold."""
    run, editor, uid = computed_run_with_a_supplement(tmp_path)
    issue = {'name': 'Rounding, not arithmetic', 'severity': 'minor',
             'category': 'statistics-and-inference', 'rank': 1,
             'statement': 'The sum does not come out as printed, and the supplement says the '
                          'figures are rounded, so this is a presentation point.',
             'members': [uid], 'rebuttal_quote': SUPPLEMENT_SENTENCE,
             'rebuttal_document': 'appendix-that-was-never-imported.md'}

    refused = srv.submit_issues(editor, [issue])

    assert any('no document named appendix-that-was-never-imported.md' in problem
               for problem in refused['problems']), refused
    assert any('paper.md' in problem for problem in refused['problems']), refused
    assert not srv.list_findings(editor)['issues'], 'nothing was stored'

    # And it is the rebuttal's own field: an issue with nothing to rebut may
    # not carry it, exactly as it may not carry the quote or the line.
    document, = [name for name in run.base.document_names() if name != 'paper.md']
    stray = srv.submit_issues(editor, [{**issue, 'severity': 'major',
                                        'rebuttal_quote': '', 'rebuttal_document': document}])
    assert any('rebuttal_document' in problem for problem in stray['problems']), stray
    assert not srv.list_findings(editor)['issues'], 'nothing was stored'


def test_a_finished_run_writes_the_files_a_consumer_reads(tmp_path):
    run, uids = two_track_run(tmp_path)
    editor, accepted = accept_editor(run, uids)
    assert accepted['accepted'] is True, accepted
    close(editor, model='claude-sonnet-5-5')
    written = srv.write_review(claim('recorder')['agent_token'])
    assert written['status'] == 'complete'
    assert written['findings_by_track'] == {'core': 2, 'stats': 1}
    assert written['escalated'] == 1

    review = run.review
    for name in ('protocol.json', 'progress.json', 'manifest.json', 'issues.json',
                 'editor-log.jsonl', 'REVIEW.md'):
        assert (review / name).is_file(), name
    assert (review / 'audit_stats.md').read_text(encoding='utf-8').startswith('# stats audit')
    assert (review / 'audit_citations.md').is_file()

    document = json.loads((review / 'issues.json').read_text(encoding='utf-8'))
    assert document['schema_version'] == issue_model.ISSUES_SCHEMA_VERSION
    assert document['issues_source'] == 'editor'
    assert len(document['issues']) == 3
    assert document['counts'] == {**document['counts'], 'issues': 3, 'required': 3,
                                  'suggestions': 0, 'issue_members': 3}
    assert [issue['id'] for issue in document['issues']] == ['i01', 'i02', 'i03']
    assert [issue['rank'] for issue in document['issues']] == [1, 2, 3]
    assert document['verdict']['basis'] == 'judgement'
    assert document['escalation_rulings'][0]['finding_uid'] == uids['spec-stats']
    assert document['reviewer_count'] == 2
    assert {row['slot'] for row in document['reviewer_provenance']} >= {'core-1', 'spec-stats'}
    assert all(row['model'] == 'claude-sonnet-5-5' for row in document['reviewer_provenance'])
    # The specialist audit the issue rests on is named on the issue and stays
    # on disk as the evidence behind it.
    stats = [issue for issue in document['issues']
             if any(m['slot'] == 'spec-stats' for m in issue['members'])]
    assert stats and stats[0]['audits'] == ['audit_stats.md']

    protocol = json.loads((review / 'protocol.json').read_text(encoding='utf-8'))
    assert protocol['provider'] == 'claude-code'
    assert protocol['arm'] == 'full-two-track'
    assert protocol['finding_schema_version'] == names.FINDING_SCHEMA_VERSION

    manifest = json.loads((review / 'manifest.json').read_text(encoding='utf-8'))
    assert manifest['paper_id'] == run.base.paper_id
    assert any(key.startswith('engine/') for key in manifest['code'])
    assert manifest['plan']['reviewers'] == 2

    progress = json.loads((review / 'progress.json').read_text(encoding='utf-8'))
    assert progress['status'] == 'complete'
    assert progress['stages']['missing'] == []
    assert progress['review_guard_status'] == 'clean'
    assert (review / 'reviewer-1' / 'findings.json').is_file()


def test_a_published_review_can_say_which_model_wrote_which_report(tmp_path):
    """Two facts per slot: the model it was asked to run, and the one it says it ran.

    A review is published with the reviewer reports beside it, so the arm has to
    be legible from the folder — and a model the harness substituted has to be
    visible as a disagreement rather than written over the plan.
    """
    run, uids = two_track_run(tmp_path)  # every agent self-reports Sonnet
    editor, accepted = accept_editor(run, uids)
    assert accepted['accepted'] is True, accepted
    close(editor, model='claude-fable-5-2')
    srv.write_review(claim('recorder')['agent_token'])

    document = json.loads((run.review / 'issues.json').read_text(encoding='utf-8'))
    rows = {row['slot']: row for row in document['reviewer_provenance']}
    assert rows['core-1']['assigned_model'] == 'claude-sonnet-5-5'
    assert rows['core-2']['assigned_model'] == 'claude-opus-5-5', 'two reviewers, two models'
    assert rows['core-2']['model'] == 'claude-sonnet-5-5', 'what it reported, kept as it is'
    assert rows['spec-stats']['assigned_model'] == 'claude-sonnet-5-5'

    protocol = json.loads((run.review / 'protocol.json').read_text(encoding='utf-8'))
    assert protocol['agents']['core-2']['assigned_model'] == 'claude-opus-5-5'
    assert protocol['agents']['editor']['model'] == 'claude-fable-5-2'
    assert protocol['models']['editor'] == 'claude-fable-5-2'

    # core-2 was assigned Opus and reported Sonnet, so this run is not the run
    # it records: a different family in a slot is a problem and a reason not to
    # publish, named slot by slot. The folder is whole, so nothing else is.
    reading = read_run(run.review)
    assert reading['status'] == 'complete' and reading['publishable'] is False
    reasons = reading['not_publishable_reasons']
    # The citation ledger of this run was filled and never resolved, which is a
    # problem line and deliberately not a reason: see `citation_ledger.problems`.
    assert len(reasons) == 1
    assert reasons == [line for line in reading['problems'] if 'citation' not in line]
    assert 'core-2' in reasons[0] and 'claude-opus-5-5' in reasons[0]
    assert 'claude-sonnet-5-5' in reasons[0] and 'different model family' in reasons[0]


def test_a_slot_that_ran_another_version_of_its_own_family_is_not_a_problem(tmp_path):
    """The family is the plan; the version is what the family resolved to.

    A per-spawn override takes the family alias, so the version that answers it
    is Claude Code's to choose and a newer one is not a substitution. Only a
    different family means the run did not happen as it is recorded.
    """
    run, uids = two_track_run(tmp_path, model='claude-sonnet-5-6',
                              reviewer_models='sonnet')
    editor, accepted = accept_editor(run, uids)
    assert accepted['accepted'] is True, accepted
    close(editor, model='claude-fable-5-2')
    srv.write_review(claim('recorder')['agent_token'])

    reading = read_run(run.review)
    # The unresolved citation ledger is the only problem line this run has, and
    # it is not a reason to withhold the review.
    assert [line for line in reading['problems'] if 'citation' not in line] == []
    assert reading['publishable'] is True, reading['not_publishable_reasons']


def test_a_slot_that_reported_no_model_is_unverified_and_still_publishable(tmp_path):
    """Nothing was seen to disagree with the plan, so the review stands.

    What cannot be said is that the slot ran what it was asked to run: the
    session reported no model and the agent never said. `models_unverified`
    names those slots so an application can show the plan as a plan, and it is
    deliberately not a problem and not a reason to withhold the review.
    """
    open_run(tmp_path, arm='single')
    close(claim('core-1')['agent_token'], none_found=True)
    srv.write_review(claim('recorder')['agent_token'])

    reading = read_run(srv.current().review)
    assert 'core-1' in reading['identity']['models_unverified']
    assert reading['identity']['models']['core-1'] == 'claude-sonnet-5-5', 'the plan is recorded'
    assert [row['model'] for row in reading['agents'] if row['slot'] == 'core-1'] == [None], \
        'never the assignment repeated back'
    assert reading['problems'] == []
    assert reading['publishable'] is True, reading['not_publishable_reasons']


def test_a_slot_that_reported_its_model_is_not_unverified(tmp_path):
    run, uids = two_track_run(tmp_path)
    editor, accepted = accept_editor(run, uids)
    assert accepted['accepted'] is True, accepted
    close(editor, model='claude-fable-5-2')
    srv.write_review(claim('recorder')['agent_token'])
    reading = read_run(run.review)
    assert 'core-1' not in reading['identity']['models_unverified']
    assert 'editor' not in reading['identity']['models_unverified']


def test_a_run_where_nobody_found_anything_is_complete_without_findings(tmp_path):
    open_run(tmp_path, arm='single')
    close(claim('core-1')['agent_token'], none_found=True,
          model='claude-sonnet-5-5')
    written = srv.write_review(claim('recorder')['agent_token'])
    assert written['status'] == 'complete-without-findings'
    assert written['issue_counts']['issues'] == 0
    assert written['required_changes'] == 0 and written['suggestions'] == 0


def test_a_run_whose_issues_are_all_suggestions_still_completed(tmp_path):
    """A suggestion is delivered: `complete-without-findings` would be a lie.

    Nothing is capped or hidden, so the question the status answers is whether
    the run raised anything at all — not whether what it raised was required.
    Three minor issues are three issues, and the run is complete.
    """
    run, uids = two_track_run(tmp_path)
    editor, accepted = accept_editor(run, uids, issues=issue_batch(
        uids, severity='minor', category='presentation'))
    assert accepted['accepted'] is True, accepted
    close(editor, model='claude-sonnet-5-5')
    written = srv.write_review(token_for('recorder'))
    assert written['status'] == 'complete'
    assert written['required_changes'] == 0
    assert written['suggestions'] == 3
    assert written['issue_counts']['issue_members'] == 3


def test_the_guard_status_in_the_run_files_is_read_and_not_assumed(tmp_path):
    open_run(tmp_path, arm='single')
    run = srv.current()
    token = token_for('core-1')
    line, quote = quote_from(run)
    srv.submit_finding(token, title='Invented',
                       quote='a sentence this paper does not contain anywhere',
                       comment='...', severity='major')
    srv.submit_finding(token, title='Anchored', line=line, quote=quote, comment='...',
                       severity='major')
    close(token, model='claude-sonnet-5-5')
    assert srv.write_review(token_for('recorder'))['status'] == 'complete'
    progress = json.loads((run.review / 'progress.json').read_text(encoding='utf-8'))
    assert progress['review_guard_status'] == 'quarantined'
    assert progress['quarantined_findings_count'] == 1


def test_nothing_is_stored_after_the_stage_closed_or_the_editor_ruled(tmp_path):
    """A late finding was written to findings.jsonl and then left out of the package."""
    run, uids = two_track_run(tmp_path)
    line, quote = quote_from(run, 3)
    late = srv.submit_finding(run.agents['core-1'].token, title='One more thing',
                              line=line, quote=quote, comment='...', severity='major')
    assert 'closed this stage' in late['request_rejected']
    assert len(run.findings) == 3
    editor, accepted = accept_editor(run, uids)
    assert accepted['accepted'] is True, accepted
    after = srv.submit_finding(reclaim('core-2'), title='After the ruling', line=line,
                               quote=quote, comment='...', severity='major')
    assert 'already been ruled on' in after['request_rejected']
    assert len(run.findings) == 3


def test_a_finding_filed_after_the_run_was_blocked_is_refused(tmp_path):
    open_run(tmp_path, arm='single')
    run = srv.current()
    token = token_for('core-1')
    line, quote = quote_from(run)
    srv.mark_blocked(CODES['recorder_key'], 'the subscription rate limit was reached')
    refused = srv.submit_finding(token, title='Too late', line=line, quote=quote,
                                 comment='...', severity='major')
    assert 'run is blocked' in refused['request_rejected']
    assert run.findings == []


def test_mark_blocked_is_recorded_in_the_run_files(tmp_path):
    open_run(tmp_path, arm='single')
    run = srv.current()
    key = CODES['recorder_key']
    assert srv.mark_blocked(key, 'the subscription rate limit was reached')['status'] == 'blocked'
    progress = json.loads((run.review / 'progress.json').read_text(encoding='utf-8'))
    assert progress['status'] == 'blocked'
    assert 'rate limit' in progress['error']
    assert (run.review / 'manifest.json').is_file()


def test_only_the_run_key_marks_a_run_blocked_and_never_a_finished_one(tmp_path):
    """A reviewer that could call this could end a run whose findings it disliked."""
    open_run(tmp_path, arm='single')
    run = srv.current()
    token_for('core-1')
    assert 'request_rejected' in srv.mark_blocked('', 'I would rather it stopped')
    assert 'request_rejected' in srv.mark_blocked('not-the-run-key', 'stop')
    assert run.status == 'running'
    close(run.agents['core-1'].token, none_found=True, model='claude-sonnet-5-5')
    assert srv.write_review(token_for('recorder'))['status'] == 'complete-without-findings'
    refused = srv.mark_blocked(CODES['recorder_key'], 'too late')
    assert 'finished run is not marked blocked' in refused['request_rejected']
    assert json.loads((run.review / 'progress.json').read_text(encoding='utf-8'))['status'] == (
        'complete-without-findings')


def test_only_the_recorder_writes_the_review(tmp_path):
    open_run(tmp_path, arm='single')
    token = claim('core-1')['agent_token']
    assert 'recorder writes' in srv.write_review(token)['request_rejected']


def test_the_tool_list_is_the_one_the_agents_are_allowed_to_call(tmp_path):
    names = {tool.__name__ for tool in srv.TOOLS}
    assert names >= set(srv.PAPER_TOOLS)
    assert {'ingest', 'claim_agent', 'reissue_claim_code', 'submit_finding', 'submit_report',
            'list_findings', 'submit_issues', 'submit_editor', 'write_review', 'mark_blocked',
            'run_status'} <= names
    assert {'resolve_citations', *srv.LEDGER_TOOLS} <= names
    assert set(srv.READER_TOOLS) <= names
    # The fallback seal is the recorder's and is deliberately not one of the
    # reader's tools: it writes the manuscript as converted when the reader
    # did not, which is not a thing the reader may choose to do.
    assert 'seal_as_converted' in names and 'seal_as_converted' not in srv.READER_TOOLS
    assert {'method_claims', 'submit_code_check'} <= names
    assert len(srv.TOOLS) == len(names) == 44
