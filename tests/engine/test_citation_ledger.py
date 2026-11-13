"""The citation ledger, offline: what each stage writes and what it refuses to say.

Every lookup here is answered from a table in the test. Nothing opens a socket
and nothing calls a model: stages 1 and 2 of the ledger have no model in them at
all, and stage 3's rules — a passage has to be in the source text, a row with no
source text can only be "cannot tell" — are enforced by the server rather than
asked for in a prompt, which is what makes them testable without one.

The four readings these tests pin are the four the ledger exists to keep apart:

* a reference nothing indexes is ``exists: no``,
* a reference whose printed year disagrees with the indexed record is resolved
  *and* mismatched,
* a reference whose lookups never answered is ``could not check`` — never
  ``no``, because a rate limit is not evidence about a paper,
* a use with no source text behind it can only be ``cannot tell``.
"""

from __future__ import annotations

import io
import json
import urllib.error

import pytest

from peerreviewagents.engine import citation_ledger as cl

TITLE = 'Hippocampal replay during quiet rest'
FAKE_TITLE = 'Synthetic scaffolds for retinal regeneration in zebrafish'


class Net:
    """Every lookup answered from a table of URL fragments. No socket is opened.

    ``answers`` is checked in order, so a more specific fragment goes first.
    A payload that is an int is raised as that HTTP status, and a fragment in
    ``fail`` raises ``OSError`` — the transport failure the ledger has to read
    as "could not check" rather than as a missing paper.
    """

    def __init__(self, answers=(), fail=()):
        self.answers = list(answers)
        self.fail = list(fail)
        self.urls: list[str] = []

    def __call__(self, request, timeout=None):
        url = request.full_url
        self.urls.append(url)
        assert 'mailto' not in url.lower(), 'a lookup must not carry an address'
        for fragment in self.fail:
            if fragment in url:
                raise OSError('temporary failure in name resolution')
        for fragment, payload in self.answers:
            if fragment in url:
                if isinstance(payload, int):
                    raise urllib.error.HTTPError(url, payload, 'no', None, None)
                body = payload if isinstance(payload, str) else json.dumps(payload)
                return io.BytesIO(body.encode('utf-8'))
        raise urllib.error.HTTPError(url, 404, 'Not Found', None, None)


def crossref_work(doi, title, year, author='Lin', venue='Journal of Neuroscience',
                  abstract=None):
    """One Crossref ``/works/{doi}`` answer."""
    return {'message': {'DOI': doi, 'title': [title],
                        'author': [{'given': 'Mei', 'family': author}],
                        'issued': {'date-parts': [[int(year)]]},
                        'container-title': [venue], 'type': 'journal-article',
                        **({'abstract': abstract} if abstract else {})}}


def crossref_search(items):
    return {'message': {'items': items}}


def europepmc(hits):
    return {'hitCount': len(hits), 'resultList': {'result': hits}}


def paper(references=None, text=None):
    """A manuscript with one citing paragraph, as ``paper.json`` holds it."""
    sentence = text or 'Replay has been reported during quiet rest [1].'
    return {
        'id': 'paper-1',
        'blocks': [{'id': 'b1', 'kind': 'paragraph', 'section': 'Introduction',
                    'text': sentence}],
        'references': references if references is not None else [
            {'id': 'r1', 'label': '1', 'title': TITLE, 'year': '2019',
             'first_author': 'Lin, M.', 'doi': '10.1234/replay',
             'raw': '1. Lin M. ' + TITLE + '. Journal of Neuroscience, 2019.'}],
    }


def filled(tmp_path, references=None, text=None):
    ledger = cl.Ledger(tmp_path / 'review')
    cl.build(ledger, paper(references, text))
    return ledger


def lookups(tmp_path, net, limit=600):
    return cl.Lookups(tmp_path / 'review' / 'citations' / 'lookups', opener=net, limit=limit)


# -- stage 1 ----------------------------------------------------------------


def test_every_reference_gets_a_row_with_or_without_a_doi(tmp_path):
    """A reference with no DOI is still a row. Sampling is what the ledger replaces."""
    references = [{'id': 'r1', 'label': '1', 'title': TITLE, 'year': '2019',
                   'doi': '10.1234/replay', 'raw': '1. Lin M. ' + TITLE + ', 2019.'},
                  {'id': 'r2', 'label': '2', 'title': 'A book with no identifier',
                   'year': '1998', 'raw': '2. Ross P. A book with no identifier. Press, 1998.'}]
    ledger = filled(tmp_path, references,
                    'Both claims rest on earlier work [1], [2].')
    rows = {row['row_id']: row for row in ledger.rows()}
    assert 'ref:r1' in rows and 'ref:r2' in rows
    assert rows['ref:r2']['printed']['doi'] is None
    uses = [row for row in rows.values() if row['kind'] == 'use']
    assert {row['reference_id'] for row in uses} == {'r1', 'r2'}
    assert all(row['sentence'] and row['block_id'] == 'b1' for row in uses)
    totals = cl.counts(ledger)
    assert (totals['references'], totals['uses']) == (2, 2)
    # Stage 1 reads the manuscript and nothing else: no model, no network.
    assert totals['lookups'] == 0
    assert ledger.state()['fill']['done'] is True


def test_filling_twice_keeps_what_later_stages_wrote(tmp_path):
    """Resumable: a second fill adds the new rows and overwrites nothing."""
    ledger = filled(tmp_path)
    net = Net([('/works/', crossref_work('10.1234/replay', TITLE, '2019')),
               ('DOI%3A', europepmc([]))])
    cl.resolve(ledger, lookups(tmp_path, net))
    again = cl.build(ledger, paper())
    assert again['added'] == 0
    row = {row['row_id']: row for row in ledger.rows()}['ref:r1']
    assert row['resolution']['exists'] == 'yes', 'the fill must not erase the lookup'


# -- stage 2 ----------------------------------------------------------------


def test_a_fabricated_reference_is_recorded_as_not_existing(tmp_path):
    """Both searches answered and neither found it: that is ``exists: no``.

    This is the strongest thing the ledger ever says about a reference, and it
    is said only when every lookup answered. The printed DOI being unregistered
    is recorded beside it rather than instead of it.
    """
    references = [{'id': 'r1', 'label': '1', 'title': FAKE_TITLE, 'year': '2021',
                   'first_author': 'Soto, J.', 'doi': '10.9999/not-a-paper',
                   'raw': '1. Soto J. ' + FAKE_TITLE + '. Nature, 2021.'}]
    ledger = filled(tmp_path, references, 'Scaffolds restore vision in zebrafish [1].')
    net = Net([('/works/10.9999', 404),
               ('/works?', crossref_search([{'DOI': '10.1/other',
                                             'title': ['An unrelated paper about enzymes'],
                                             'issued': {'date-parts': [[2021]]}}])),
               ('/search?', europepmc([]))])
    answer = cl.resolve(ledger, lookups(tmp_path, net))
    assert answer['remaining'] == 0
    row = {row['row_id']: row for row in ledger.rows()}['ref:r1']
    resolution = row['resolution']
    assert resolution['exists'] == 'no'
    assert resolution['method'] == 'title-search'
    assert resolution['printed_doi_registered'] is False
    assert 'not registered' in resolution['note']

    totals = cl.counts(ledger)
    assert (totals['not_found'], totals['resolved'], totals['could_not_check']) == (1, 0, 0)
    # Stage 4 reads this as a problem row; nothing else in the table is one.
    flagged = cl.problem_rows(ledger)
    assert [one['reference_id'] for one in flagged['references']] == ['r1']
    assert 'no' == flagged['references'][0]['exists']


def test_a_real_doi_with_the_wrong_year_resolves_and_mismatches(tmp_path):
    """The work exists, the paper printed it wrong, and those are two answers."""
    ledger = filled(tmp_path)
    net = Net([('/works/10.1234', crossref_work('10.1234/replay', TITLE, '2017')),
               ('DOI%3A', europepmc([]))])
    cl.resolve(ledger, lookups(tmp_path, net))
    resolution = {row['row_id']: row for row in ledger.rows()}['ref:r1']['resolution']
    assert resolution['exists'] == 'yes' and resolution['method'] == 'doi'
    assert resolution['mismatched'] == ['year']
    assert resolution['matches']['title'] is True
    assert resolution['matches']['year'] is False
    assert resolution['record']['year'] == '2017'

    totals = cl.counts(ledger)
    assert (totals['resolved'], totals['mismatched'], totals['not_found']) == (1, 1, 0)
    flagged = cl.problem_rows(ledger)
    assert 'year' in flagged['references'][0]['mismatched']


def test_a_lookup_that_never_answered_is_could_not_check_and_never_not_found(tmp_path):
    """A failed lookup says nothing about whether a paper exists.

    Every request fails at the transport. The row has to come out
    ``could not check``, the counts have to say so, and the problem line has to
    say plainly that it is not a claim about the reference.
    """
    ledger = filled(tmp_path)
    net = Net(fail=['api.crossref.org', 'ebi.ac.uk'])
    answer = cl.resolve(ledger, lookups(tmp_path, net))
    assert answer['lookups_now'] >= 2, 'the DOI and the title search were both tried'
    resolution = {row['row_id']: row for row in ledger.rows()}['ref:r1']['resolution']
    assert resolution['exists'] == 'could not check'
    assert resolution['exists'] != 'no'
    assert resolution['printed_doi_registered'] is None
    assert 'did not answer' in resolution['note']

    totals = cl.counts(ledger)
    assert (totals['could_not_check'], totals['not_found'], totals['resolved']) == (1, 0, 0)
    lines = ' '.join(cl.problems(totals))
    assert 'could not be checked' in lines
    assert 'says nothing about whether they exist' in lines
    assert cl.summary_line(totals).count('could not be checked') == 1
    # The specialist is given the row to try again itself, and it is labelled
    # "could not be checked" — never as a reference that does not exist.
    flagged = cl.problem_rows(ledger)['references']
    assert [one['problem'] for one in flagged] == ['could not be checked']
    assert flagged[0]['exists'] == 'could not check'
    assert 'not indexed' not in flagged[0]['problem']


def test_resolve_is_resumable_and_reports_what_is_left(tmp_path):
    """A limit resolves that many rows and says how many remain, cache included."""
    references = [{'id': f'r{index}', 'label': str(index), 'title': f'{TITLE} {index}',
                   'year': '2019', 'doi': f'10.1234/replay-{index}',
                   'raw': f'{index}. Lin M. {TITLE} {index}, 2019.'}
                  for index in (1, 2, 3)]
    ledger = filled(tmp_path, references, 'Earlier work agrees [1], [2], [3].')
    net = Net([('/works/10.1234/replay-1', crossref_work('10.1234/replay-1', TITLE + ' 1', '2019')),
               ('/works/10.1234/replay-2', crossref_work('10.1234/replay-2', TITLE + ' 2', '2019')),
               ('/works/10.1234/replay-3', crossref_work('10.1234/replay-3', TITLE + ' 3', '2019')),
               ('DOI%3A', europepmc([]))])
    first = cl.resolve(ledger, lookups(tmp_path, net), limit=2)
    assert (first['resolved_now'], first['remaining']) == (2, 1)
    assert ledger.state()['resolve']['done'] is False
    spent = len(net.urls)
    second = cl.resolve(ledger, lookups(tmp_path, net), limit=2)
    assert (second['resolved_now'], second['remaining']) == (1, 0)
    assert ledger.state()['resolve']['done'] is True
    assert len(net.urls) > spent, 'the third row was looked up in the second call'
    # Nothing is looked up twice: a third call resolves nothing and sends nothing.
    third = cl.resolve(ledger, lookups(tmp_path, net), limit=2)
    assert (third['resolved_now'], third['lookups_now']) == (0, 0)


def test_the_lookup_count_per_stage_is_recorded_and_the_allowance_holds(tmp_path):
    """What stage 2 cost, in requests, is in the ledger's own stage record."""
    ledger = filled(tmp_path)
    net = Net([('/works/10.1234', crossref_work('10.1234/replay', TITLE, '2019')),
               ('DOI%3A', europepmc([]))])
    answer = cl.resolve(ledger, lookups(tmp_path, net))
    totals = cl.counts(ledger)
    assert totals['lookups'] == answer['lookups_total'] == len(net.urls)
    assert ledger.state()['resolve']['lookups'] == totals['lookups']
    assert ledger.state()['fill']['references'] == 1

    # The allowance is the ceiling, and a reference past it is "could not
    # check" rather than a reference the run quietly skipped.
    second = cl.Ledger(tmp_path / 'other')
    cl.build(second, paper())
    stingy = cl.Lookups(tmp_path / 'other' / 'citations' / 'lookups', opener=net, limit=0)
    cl.resolve(second, stingy)
    resolution = {row['row_id']: row for row in second.rows()}['ref:r1']['resolution']
    assert resolution['exists'] == 'could not check'
    assert [one['status'] for one in resolution['requests']] == ['budget_exhausted'] * len(
        resolution['requests'])


def test_nothing_in_a_lookup_identifies_the_user(tmp_path):
    """The client name is the software's, with no address and no owner in it."""
    from peerreviewagents.engine.external_evidence import USER_AGENT

    assert '@' not in USER_AGENT and 'mailto' not in USER_AGENT.lower()
    seen = {}

    class Recording(Net):
        def __call__(self, request, timeout=None):
            seen.update(request.headers)
            return super().__call__(request, timeout)

    ledger = filled(tmp_path)
    net = Recording([('/works/10.1234', crossref_work('10.1234/replay', TITLE, '2019')),
                     ('DOI%3A', europepmc([]))])
    cl.resolve(ledger, lookups(tmp_path, net))
    assert seen['User-agent'] == USER_AGENT
    assert not any('mail' in str(key).lower() for key in seen)


# -- stage 3 ----------------------------------------------------------------


def judging_ledger(tmp_path, abstract='Replay was observed during quiet rest in all animals.'):
    """A ledger filled and resolved, with or without an abstract to judge against."""
    ledger = filled(tmp_path)
    net = Net([('/works/10.1234', crossref_work('10.1234/replay', TITLE, '2019',
                                                abstract=abstract)),
               ('DOI%3A', europepmc([]))])
    cl.resolve(ledger, lookups(tmp_path, net))
    return ledger


def test_a_worker_claims_a_batch_with_the_source_text_and_no_manuscript(tmp_path):
    """One row is a citing sentence and the source the server already fetched."""
    ledger = judging_ledger(tmp_path)
    rows = cl.batch(ledger, 'cite-1', size=5)
    assert len(rows) == 1
    row = rows[0]
    assert row['sentence'].endswith('[1].')
    assert 'quiet rest' in row['source_text'] and row['source_kind'] == 'abstract'
    assert row['reference']['printed_year'] == '2019'
    assert 'manuscript' not in row and 'blocks' not in row
    # The claim is on the row, so a second worker is handed something else.
    assert cl.batch(ledger, 'cite-2', size=5) == []
    assert cl.batch(ledger, 'cite-1', size=5)[0]['row_id'] == row['row_id']


def test_a_stale_claim_is_open_again_so_a_dead_worker_blocks_nothing(tmp_path):
    ledger = judging_ledger(tmp_path)
    claimed = cl.batch(ledger, 'cite-1', size=5)[0]
    assert cl.batch(ledger, 'cite-2', size=5, stale_seconds=0)[0]['row_id'] == claimed['row_id']


def test_a_verdict_must_quote_the_source_it_relied_on(tmp_path):
    """A passage reassembled from memory is refused, so the quote is the source's."""
    ledger = judging_ledger(tmp_path)
    row = cl.batch(ledger, 'cite-1')[0]
    stored, why = cl.verdict(ledger, 'cite-1', row['row_id'], 'supports',
                             'Replay was seen in every animal during rest')
    assert stored is None and 'not in the source text' in why
    stored, why = cl.verdict(ledger, 'cite-1', row['row_id'], 'supports', '')
    assert stored is None and 'Quote the passage' in why
    stored, why = cl.verdict(ledger, 'cite-1', row['row_id'], 'supports',
                             'Replay was observed during quiet rest')
    assert why is None and stored['verdict'] == 'supports'
    assert stored['source_url'] and stored['judged_by'] == 'cite-1'

    totals = cl.counts(ledger)
    assert (totals['uses_judged'], totals['supported'], totals['uses_unjudged']) == (1, 1, 0)
    assert totals['complete'] is True


def test_a_row_with_no_source_text_can_only_be_cannot_tell(tmp_path):
    """Nothing to read is not a judgement, and the ledger will not record one."""
    ledger = judging_ledger(tmp_path, abstract=None)
    row = cl.batch(ledger, 'cite-1')[0]
    assert row['source_text'] == '' and row['source_kind'] == 'none'
    for answer in ('supports', 'partly supports', 'does not support'):
        stored, why = cl.verdict(ledger, 'cite-1', row['row_id'], answer, 'anything at all')
        assert stored is None and 'cannot tell' in why

    stored, why = cl.verdict(ledger, 'cite-1', row['row_id'], 'cannot tell')
    assert why is None and stored['passage'] is None

    totals = cl.counts(ledger)
    assert (totals['cannot_tell'], totals['uses_judged']) == (1, 1)
    assert totals['with_source_text'] == 0
    # A "cannot tell" is the part of the list this run could not check, and it
    # is reported as that rather than as something wrong with the paper.
    assert cl.problem_rows(ledger)['uses'] == []
    unverified = cl.unverified_rows(ledger)
    assert [one['row_id'] for one in unverified['uses']] == [row['row_id']]


def test_a_verdict_is_refused_for_an_unknown_row_or_an_unknown_answer(tmp_path):
    ledger = judging_ledger(tmp_path)
    row = cl.batch(ledger, 'cite-1')[0]
    assert cl.verdict(ledger, 'cite-1', 'use:nope', 'supports', 'x')[1].startswith(
        'There is no citation use row')
    stored, why = cl.verdict(ledger, 'cite-1', row['row_id'], 'looks fine', 'Replay was observed')
    assert stored is None and 'A verdict is one of' in why


def test_a_not_supported_verdict_is_a_problem_row_and_carries_its_passage(tmp_path):
    """What stage 4 re-checks: the verdict, the passage, and the row behind it."""
    ledger = judging_ledger(tmp_path)
    row = cl.batch(ledger, 'cite-1')[0]
    cl.verdict(ledger, 'cite-1', row['row_id'], 'does not support',
               'Replay was observed during quiet rest', note='The sentence claims sleep.')
    flagged = cl.problem_rows(ledger)
    assert [one['row_id'] for one in flagged['uses']] == [row['row_id']]
    assert flagged['uses'][0]['verdict'] == 'does not support'
    detail = cl.row_detail(ledger, row['row_id'])
    assert detail['row']['passage'] == 'Replay was observed during quiet rest'
    assert detail['row']['note'] == 'The sentence claims sleep.'
    assert detail['source_text'].startswith('ABSTRACT')
    assert detail['reference']['exists'] == 'yes'


# -- the counts a report prints ---------------------------------------------


def test_an_unfinished_ledger_is_reported_and_is_not_a_clean_bill_of_health(tmp_path):
    ledger = filled(tmp_path)
    totals = cl.counts(ledger)
    assert totals['complete'] is False
    lines = cl.problems(totals)
    assert any('never looked up' in line for line in lines)
    assert any('never judged' in line for line in lines)


def test_no_ledger_at_all_says_so_rather_than_reporting_zero_problems(tmp_path):
    empty = cl.Ledger(tmp_path / 'review')
    assert empty.exists() is False
    totals = cl.counts(empty)
    assert totals['references'] == 0 and cl.problems(totals) == []
    assert cl.summary_line(totals) == 'No citation ledger was filled for this run.'


def test_an_unreadable_line_is_counted_and_does_not_hide_the_others(tmp_path):
    ledger = judging_ledger(tmp_path)
    with ledger.path.open('a', encoding='utf-8') as stream:
        stream.write('{not json at all\n')
    totals = cl.counts(ledger)
    assert totals['unreadable_rows'] == 1 and totals['references'] == 1
    assert any('could not be read' in line for line in cl.problems(totals))


def test_the_lookup_allowance_is_bounded_and_refuses_a_silly_limit(tmp_path):
    with pytest.raises(ValueError):
        cl.Lookups(tmp_path / 'lookups', limit=10_000)
    with pytest.raises(ValueError):
        cl.Lookups(tmp_path / 'lookups', limit=-1)
