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
import urllib.parse

import pytest

from peerreviewagents.engine import citation_ledger as cl
from peerreviewagents.engine import external_evidence as ev

TITLE = 'Hippocampal replay during quiet rest'
FAKE_TITLE = 'Synthetic scaffolds for retinal regeneration in zebrafish'


def _body(payload):
    """A fake response body: a string as itself, anything else as JSON."""
    return (payload if isinstance(payload, str) else json.dumps(payload)).encode('utf-8')


#: What ``https://doi.org/api/handles/{doi}`` answers for a DOI the resolver has
#: no handle for: HTTP 404 with ``responseCode: 100`` in the body. This is the
#: only answer the ledger reads as "that identifier does not exist", so every
#: test that wants ``exists: no`` has to put it on the table.
NO_HANDLE = ('doi.org/api/handles', (404, {'responseCode': 100, 'handle': 'x'}))
#: And the same route for a DOI that does resolve: 1, success.
HANDLE = ('doi.org/api/handles', {'responseCode': 1, 'values': []})


class Net:
    """Every lookup answered from a table of URL fragments. No socket is opened.

    ``answers`` is checked in order, so a more specific fragment goes first.
    A payload that is an int is raised as that HTTP status with no body, a
    ``(status, payload)`` pair is raised as that status *with* the payload as
    its body — which is how the DOI handle API says "no such handle", a 404
    whose body carries ``responseCode: 100`` — and a fragment in ``fail``
    raises ``OSError``, the transport failure the ledger has to read as "could
    not check" rather than as a missing paper.
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
                if isinstance(payload, tuple):
                    status, payload = payload
                    raise urllib.error.HTTPError(url, status, 'no', None,
                                                 io.BytesIO(_body(payload)))
                return io.BytesIO(_body(payload))
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
    """A DOI no agency registered is the one thing that earns ``exists: no``.

    This is the strongest thing the ledger ever says about a reference, and it
    is a claim about the identifier: Crossref does not hold it, no registration
    agency claims it, and neither index lists the title. Everything weaker than
    that is ``not located`` or ``could not check``.
    """
    references = [{'id': 'r1', 'label': '1', 'title': FAKE_TITLE, 'year': '2021',
                   'first_author': 'Soto, J.', 'doi': '10.9999/not-a-paper',
                   'raw': '1. Soto J. ' + FAKE_TITLE + '. Nature, 2021.'}]
    ledger = filled(tmp_path, references, 'Scaffolds restore vision in zebrafish [1].')
    net = Net([NO_HANDLE,
               ('/works/10.9999', 404),
               ('/works?', crossref_search([{'DOI': '10.1/other',
                                             'title': ['An unrelated paper about enzymes'],
                                             'issued': {'date-parts': [[2021]]}}])),
               ('/search?', europepmc([]))])
    answer = cl.resolve(ledger, lookups(tmp_path, net))
    assert answer['remaining'] == 0
    row = {row['row_id']: row for row in ledger.rows()}['ref:r1']
    resolution = row['resolution']
    assert resolution['exists'] == 'no'
    assert resolution['method'] == 'doi-unregistered'
    assert resolution['printed_doi_registered'] is False
    assert ('The DOI resolver has no handle for the DOI the reference list gives'
            in resolution['note'])
    # Two routes were asked, and only both together turn the Crossref 404 into
    # a claim about the identifier: no agency claims it, and the DOI system's
    # own resolver has no handle for it.
    assert any(url.endswith('/agency') for url in net.urls)
    assert any('doi.org/api/handles' in url for url in net.urls)

    totals = cl.counts(ledger)
    assert (totals['not_found'], totals['resolved'], totals['could_not_check']) == (1, 0, 0)
    assert totals['not_located'] == 0
    # Stage 4 reads this as a problem row; nothing else in the table is one.
    flagged = cl.problem_rows(ledger)
    assert [one['reference_id'] for one in flagged['references']] == ['r1']
    assert 'no' == flagged['references'][0]['exists']


def test_a_datacite_doi_is_a_reference_that_exists_and_a_made_up_one_is_not(tmp_path):
    """A DOI Crossref does not hold is not a DOI that does not exist.

    arXiv preprints, Zenodo deposits, datasets, theses and most research
    software register with DataCite and have never been in Crossref's index.
    Reading Crossref's 404 as "not registered" called every one of them
    fabricated. The agency route separates the two cases, and only the DOI no
    agency claims comes out as ``no``.
    """
    references = [{'id': 'r1', 'label': '1', 'title': 'Attention is all you need',
                   'year': '2017', 'first_author': 'Vaswani, A.',
                   'doi': '10.48550/arxiv.1706.03762',
                   'raw': '1. Vaswani A. Attention is all you need. arXiv, 2017.'},
                  {'id': 'r2', 'label': '2', 'title': FAKE_TITLE, 'year': '2021',
                   'first_author': 'Soto, J.', 'doi': '10.9999/not-a-paper',
                   'raw': '2. Soto J. ' + FAKE_TITLE + '. Nature, 2021.'}]
    ledger = filled(tmp_path, references, 'Earlier work differs [1], [2].')
    net = Net([NO_HANDLE,
               ('/works/10.48550/arxiv.1706.03762/agency',
                {'message': {'DOI': '10.48550/arxiv.1706.03762', 'agency': {'id': 'datacite'}}}),
               ('/works/10.48550', 404),
               ('api.datacite.org/dois/10.48550', {'data': {'attributes': {
                   'doi': '10.48550/arxiv.1706.03762',
                   'titles': [{'title': 'Attention is all you need'}],
                   'creators': [{'familyName': 'Vaswani', 'givenName': 'Ashish'}],
                   'publicationYear': 2017, 'publisher': 'arXiv',
                   'types': {'resourceTypeGeneral': 'Preprint'},
                   'descriptions': [{'descriptionType': 'Abstract',
                                     'description': 'The dominant sequence transduction models '
                                                    'use recurrence.'}]}}}),
               ('/works/10.9999', 404),
               ('/works?', crossref_search([])),
               ('/search?', europepmc([]))])
    cl.resolve(ledger, lookups(tmp_path, net))
    rows = {row['row_id']: row for row in ledger.rows()}
    preprint = rows['ref:r1']['resolution']
    assert preprint['exists'] == 'yes'
    assert preprint['method'] == 'doi-datacite'
    assert preprint['registered_with'] == 'datacite'
    assert preprint['record']['year'] == '2017'
    assert preprint['mismatched'] == []
    assert 'recurrence' in preprint['abstract']
    assert rows['ref:r2']['resolution']['exists'] == 'no'
    totals = cl.counts(ledger)
    assert (totals['resolved'], totals['not_found']) == (1, 1)


def test_a_near_neighbour_title_is_a_candidate_and_never_the_resolved_work(tmp_path):
    """An index answers every query with its nearest neighbours.

    A title that scores above the search bar with nothing else agreeing is a
    different paper, and treating it as the reference meant the stored abstract
    — the text every verdict in the table was judged against — belonged to
    someone else's work. It is kept as a candidate, named to the specialist,
    and compared against nothing.
    """
    ledger = filled(tmp_path, [{'id': 'r1', 'label': '1',
                                'title': 'Hippocampal replay during quiet rest',
                                'year': '2019', 'first_author': 'Lin, M.',
                                'raw': '1. Lin M. Hippocampal replay during quiet rest, 2019.'}],
                    'Replay has been reported during quiet rest [1].')
    neighbour = {'DOI': '10.1/neighbour',
                 'title': ['Hippocampal replay during quiet waking'],
                 'author': [{'given': 'Ana', 'family': 'Ferreira'}],
                 'issued': {'date-parts': [[2004]]},
                 'container-title': ['Journal of Neuroscience'],
                 'abstract': 'We recorded replay during quiet waking in six rats.'}
    net = Net([('/works?', crossref_search([neighbour])), ('/search?', europepmc([]))])
    cl.resolve(ledger, lookups(tmp_path, net))
    resolution = {row['row_id']: row for row in ledger.rows()}['ref:r1']['resolution']
    assert resolution['exists'] == 'not located'
    assert resolution.get('record') is None
    assert resolution['mismatched'] == []
    assert resolution['candidate']['title'] == 'Hippocampal replay during quiet waking'
    assert resolution['candidate']['corroborated_by'] is None
    assert resolution.get('abstract') is None, 'a stranger\'s abstract is not this source'
    flagged = cl.problem_rows(ledger)['references']
    assert 'nothing corroborates it as this one' in flagged[0]['problem']
    # And a hit the year or the author corroborates *is* the work.
    second = cl.Ledger(tmp_path / 'second')
    cl.build(second, paper())
    agreeing = {**neighbour, 'title': ['Hippocampal replay during quiet resting'],
                'author': [{'given': 'Mei', 'family': 'Lin'}],
                'issued': {'date-parts': [[2019]]}}
    cl.resolve(second, cl.Lookups(tmp_path / 'second' / 'citations' / 'lookups',
                                  opener=Net([('/works/10.1234', 404),
                                              ('/works/10.1234/replay/agency', 404),
                                              ('/works?', crossref_search([agreeing])),
                                              ('/search?', europepmc([]))])))
    resolution = {row['row_id']: row for row in second.rows()}['ref:r1']['resolution']
    assert resolution['exists'] == 'yes' and resolution['method'] == 'crossref-title'
    assert resolution['record']['first_author'] == 'lin'


def test_an_abbreviated_venue_is_the_same_venue(tmp_path):
    """``J Biol Chem`` is the Journal of Biological Chemistry.

    A reference list prints whichever form its style asks for. String
    similarity scores the two at about 0.4, so every reference in an
    abbreviating style used to raise a venue mismatch against its own correct
    record.
    """
    references = [{'id': 'r1', 'label': '1', 'title': TITLE, 'year': '2019',
                   'first_author': 'Lin, M.', 'doi': '10.1234/replay',
                   'journal': 'J Neurosci',
                   'raw': '1. Lin M. ' + TITLE + '. J Neurosci, 2019.'}]
    ledger = filled(tmp_path, references)
    net = Net([('/works/10.1234', crossref_work('10.1234/replay', TITLE, '2019')),
               ('DOI%3A', europepmc([]))])
    cl.resolve(ledger, lookups(tmp_path, net))
    resolution = {row['row_id']: row for row in ledger.rows()}['ref:r1']['resolution']
    assert resolution['matches']['venue'] is True
    assert resolution['mismatched'] == []
    assert cl._venue_same('Proc. Natl. Acad. Sci. U.S.A.',
                          'Proceedings of the National Academy of Sciences') is True
    assert cl._venue_same('Nature Methods', 'Nature Neuroscience') is not True


def test_a_wrong_shaped_200_is_could_not_check_and_never_a_finding(tmp_path):
    """A 200 whose body is not the documented shape has answered nothing.

    Both indexes answer 200 with a body saying something went wrong. Reading
    one as a record, or as a search that found nothing, turned a working
    reference into a finding about the paper. The row is left open for another
    pass instead, and the request is still on the receipt.
    """
    ledger = filled(tmp_path)
    net = Net([('/works/10.1234/replay/agency', {'status': 'failed', 'message': None}),
               ('/works/10.1234', {'status': 'failed', 'message-type': 'nothing'}),
               ('/works?', {'status': 'failed'}),
               ('/search?', {'errCode': 500, 'errMsg': 'server error'})])
    cl.resolve(ledger, lookups(tmp_path, net))
    resolution = {row['row_id']: row for row in ledger.rows()}['ref:r1']['resolution']
    assert resolution['exists'] == 'could not check'
    assert resolution['exists'] != 'no' and resolution['exists'] != 'not located'
    assert all(one['status'] == 'ok' for one in resolution['requests'])
    assert resolution['attempts'] == 1
    # The row is still open, so the next pass takes it up again. (The bodies
    # themselves are cached for the run, as every 200 is; a pass that follows
    # a recovered service is the one that gets a different answer. `wait=0`
    # because a real pass waits RETRY_WAIT_SECONDS between attempts on one row
    # and a test cannot.)
    cl.resolve(ledger, lookups(tmp_path, net), wait=0)
    again = {row['row_id']: row for row in ledger.rows()}['ref:r1']['resolution']
    assert again['attempts'] == 2 and again['exists'] == 'could not check'


def test_a_rate_limited_or_broken_lookup_is_retried_but_not_for_ever(tmp_path):
    """429 and 503 pass. A row left at 'could not check' is taken up again.

    The stage used to skip any row that had a resolution at all, so one
    rate-limited minute left a reference permanently unchecked while the stage
    reported itself done — the table claiming to have checked what it had not.
    """
    ledger = filled(tmp_path)
    limited = Net([('/works/', 429), ('/works?', 503), ('/search?', 503)])
    cl.resolve(ledger, lookups(tmp_path, limited))
    row = {row['row_id']: row for row in ledger.rows()}['ref:r1']
    assert row['resolution']['exists'] == 'could not check'
    assert row['resolution']['attempts'] == 1

    net = Net([('/works/10.1234', crossref_work('10.1234/replay', TITLE, '2019')),
               ('DOI%3A', europepmc([]))])
    answer = cl.resolve(ledger, lookups(tmp_path, net), wait=0)
    assert answer['resolved_now'] == 1
    row = {row['row_id']: row for row in ledger.rows()}['ref:r1']
    assert row['resolution']['exists'] == 'yes'
    assert row['resolution']['attempts'] == 2

    # And a host that stays down is not retried for ever.
    third = cl.Ledger(tmp_path / 'third')
    cl.build(third, paper())
    for attempt in range(cl.RESOLVE_ATTEMPTS + 2):
        cl.resolve(third, cl.Lookups(tmp_path / 'third' / 'citations' / 'lookups',
                                     opener=Net([('/works/', 429), ('/works?', 503),
                                                 ('/search?', 503)])), wait=0)
    row = {row['row_id']: row for row in third.rows()}['ref:r1']
    assert row['resolution']['attempts'] == cl.RESOLVE_ATTEMPTS
    assert cl.resolve(third, cl.Lookups(tmp_path / 'third' / 'citations' / 'lookups',
                                        opener=Net()), wait=0)['resolved_now'] == 0


def test_one_reference_that_raises_does_not_end_the_stage(tmp_path):
    """A row that throws is a row that could not be checked, not a dead stage.

    One unparseable record used to abort the whole resolve pass, leaving every
    reference after it unlooked-at with no record of why.
    """
    references = [{'id': 'r1', 'label': '1', 'title': 'The first one', 'year': '2019',
                   'doi': '10.1234/first', 'raw': '1. The first one, 2019.'},
                  {'id': 'r2', 'label': '2', 'title': TITLE, 'year': '2019',
                   'doi': '10.1234/replay', 'raw': '2. Lin M. ' + TITLE + ', 2019.'}]
    ledger = filled(tmp_path, references, 'Both of these [1], [2].')
    net = Net([('/works/10.1234/first', crossref_work('10.1234/first', 'The first one', '2019')),
               ('/works/10.1234/replay', crossref_work('10.1234/replay', TITLE, '2019')),
               ('DOI%3A', europepmc([]))])

    broken = {'count': 0}
    real = cl._crossref_record

    def explode(message):
        broken['count'] += 1
        if broken['count'] == 1:
            raise RuntimeError('a record this version cannot read')
        return real(message)

    cl._crossref_record = explode
    try:
        answer = cl.resolve(ledger, lookups(tmp_path, net))
    finally:
        cl._crossref_record = real
    assert answer['resolved_now'] == 2, 'the second row was still looked up'
    rows = {row['row_id']: row for row in ledger.rows()}
    first = rows['ref:r1']['resolution']
    assert first['exists'] == 'could not check'
    assert 'RuntimeError' in first['error']
    assert rows['ref:r2']['resolution']['exists'] == 'yes'
    totals = cl.counts(ledger)
    assert totals['errors'] == 1
    assert any('failed inside the ledger' in line for line in cl.problems(totals))


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
    # A handful of words appear in almost any abstract, so a quote that short
    # is not evidence that this source says anything.
    stored, why = cl.verdict(ledger, 'cite-1', row['row_id'], 'supports', 'Replay was')
    assert stored is None and 'at least %d characters' % cl.MIN_PASSAGE in why
    stored, why = cl.verdict(ledger, 'cite-1', row['row_id'], 'supports',
                             'Replay was observed during quiet rest in all animals.')
    assert why is None and stored['verdict'] == 'supports'
    assert stored['source_url'] and stored['judged_by'] == 'cite-1'

    totals = cl.counts(ledger)
    assert (totals['uses_judged'], totals['supported'], totals['uses_unjudged']) == (1, 1, 0)
    assert totals['complete'] is True


def test_a_row_with_no_source_text_can_only_be_cannot_tell(tmp_path):
    """Nothing to read is not a judgement, and the ledger will not record one."""
    ledger = judging_ledger(tmp_path, abstract=None)
    row = cl.batch(ledger, 'cite-1')[0]
    # `None`, not the string 'none': a worker shown nothing has no source kind,
    # and a page that printed the string had a row claiming a kind called none.
    assert row['source_text'] == '' and row['source_kind'] is None
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


def test_a_verdict_is_refused_on_a_row_the_worker_does_not_hold(tmp_path):
    """The judge of a row is the worker that claimed it, and only that worker.

    Without this a worker could answer the whole table from one tool call
    without ever being handed the rows — and, worse, overwrite another
    worker's finding, leaving the ledger recording the last writer as the
    judge of work it never did.
    """
    ledger = judging_ledger(tmp_path)
    quote = 'Replay was observed during quiet rest in all animals.'
    rows = {row['row_id']: row for row in ledger.rows()}
    unclaimed = next(row for row in rows.values() if row['kind'] == 'use')
    stored, why = cl.verdict(ledger, 'cite-1', unclaimed['row_id'], 'supports', quote)
    assert stored is None and 'is not claimed' in why

    claimed = cl.batch(ledger, 'cite-1')[0]
    stored, why = cl.verdict(ledger, 'cite-2', claimed['row_id'], 'supports', quote)
    assert stored is None and 'still working' in why
    # The worker holding it can judge it, and nobody can judge it twice.
    stored, why = cl.verdict(ledger, 'cite-1', claimed['row_id'], 'supports', quote)
    assert why is None and stored['judged_by'] == 'cite-1'
    stored, why = cl.verdict(ledger, 'cite-1', claimed['row_id'], 'does not support', quote)
    assert stored is None and 'already judged by cite-1' in why
    assert cl.row_detail(ledger, claimed['row_id'])['row']['verdict'] == 'supports'
    # A judged row stays judged however stale the claim on it has gone.
    stored, why = cl.verdict(ledger, 'cite-2', claimed['row_id'], 'does not support', quote,
                             stale_seconds=0)
    assert stored is None and 'already judged by cite-1' in why


def test_the_source_text_a_worker_is_handed_is_bounded(tmp_path):
    """Stage 3 is many small calls, so the text per call is what bounds the run.

    An open-access full text runs to tens of thousands of characters. Handing
    one to a worker whole is the difference between a citation pass that costs
    what it was budgeted and one that does not, so the window is cut to
    ``SOURCE_TEXT_LIMIT`` with the abstract kept first.
    """
    ledger = judging_ledger(tmp_path, abstract='Replay was observed during quiet rest. '
                                               + 'Filler sentence about rats. ' * 4_000)
    row = cl.batch(ledger, 'cite-1')[0]
    assert len(row['source_text']) <= cl.SOURCE_TEXT_LIMIT
    assert row['source_text'].startswith('ABSTRACT')
    assert 'Replay was observed during quiet rest.' in row['source_text']
    # The stored record keeps more than the window shows, so a later pass can
    # cut it differently without fetching again.
    reference = {one['row_id']: one for one in ledger.rows()}['ref:r1']
    assert len(reference['resolution']['abstract']) > cl.SOURCE_TEXT_LIMIT


def test_no_lookup_says_anything_about_whose_manuscript_this_is(tmp_path):
    """Every reference in a private draft is a public query, so it carries nothing.

    Crossref and Europe PMC both invite a ``mailto`` to get the polite pool,
    and taking that invitation would publish the owner's address alongside the
    reference list of an unpublished paper. The client names the software and
    stops there — in the query string as well as in the headers.
    """
    seen = []

    class Recording(Net):
        def __call__(self, request, timeout=None):
            seen.append((request.full_url, dict(request.headers)))
            return super().__call__(request, timeout=timeout)

    net = Recording([('/works/10.1234', crossref_work('10.1234/replay', TITLE, '2019')),
                     ('DOI%3A', europepmc([]))])
    cl.resolve(filled(tmp_path), lookups(tmp_path, net))
    assert seen, 'the stage made no request at all'
    for url, headers in seen:
        assert '@' not in url and 'mailto' not in url.lower()
        assert 'garrett' not in url.lower() and 'peerreview' not in url.lower()
        assert urllib.parse.urlsplit(url).hostname in ev.HOSTS
        assert headers.get('User-agent') == ev.USER_AGENT
        assert '@' not in json.dumps(headers)
    # The receipt kept beside the cache is the same URL, so what was sent is
    # auditable after the run without re-deriving it.
    receipts = json.loads((tmp_path / 'review' / 'citations' / 'lookups'
                           / 'requests.json').read_text())['requests']
    assert [one['source_url'] for one in receipts] == [url for url, _ in seen]
    assert not any('@' in one['source_url'] for one in receipts)


def test_a_body_that_is_not_json_at_all_is_could_not_check_and_is_not_cached(tmp_path):
    """An HTML error page with a 200 on it is not an answer about a paper.

    Caching one made a single bad minute permanent: every later pass read the
    broken copy off disk, spent no lookup, and answered "could not check" for
    the rest of the run.
    """
    net = Net([('/works/', '<html><body>Service Unavailable</body></html>'),
               ('/works?', 'not json either'),
               ('/search?', '<html>nope</html>')])
    ledger = filled(tmp_path)
    cl.resolve(ledger, lookups(tmp_path, net))
    resolution = {row['row_id']: row for row in ledger.rows()}['ref:r1']['resolution']
    assert resolution['exists'] == 'could not check'
    assert cl.counts(ledger)['could_not_check'] == 1

    working = Net([('/works/10.1234', crossref_work('10.1234/replay', TITLE, '2019')),
                   ('DOI%3A', europepmc([]))])
    cl.resolve(ledger, lookups(tmp_path, working), wait=0)
    assert working.urls, 'the broken body was cached and the retry read it back'
    again = {row['row_id']: row for row in ledger.rows()}['ref:r1']['resolution']
    assert again['exists'] == 'yes'


def test_a_not_supported_verdict_is_a_problem_row_and_carries_its_passage(tmp_path):
    """What stage 4 re-checks: the verdict, the passage, and the row behind it."""
    ledger = judging_ledger(tmp_path)
    row = cl.batch(ledger, 'cite-1')[0]
    cl.verdict(ledger, 'cite-1', row['row_id'], 'does not support',
               'Replay was observed during quiet rest in all animals.',
               note='The sentence claims sleep.')
    flagged = cl.problem_rows(ledger)
    assert [one['row_id'] for one in flagged['uses']] == [row['row_id']]
    assert flagged['uses'][0]['verdict'] == 'does not support'
    detail = cl.row_detail(ledger, row['row_id'])
    assert detail['row']['passage'] == 'Replay was observed during quiet rest in all animals.'
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


def test_a_paper_whose_reference_list_was_not_read_says_so_instead_of_nothing(tmp_path):
    """Markers in the text and no reference rows: the loudest silence of the lot.

    A manuscript whose reference list this version could not extract fills a
    table of in-text uses that point at nothing. Every other count is zero and
    every stage is trivially done, so the old reading was a ledger with no
    problems in it — a clean bill of health for a paper where not one citation
    was checked.
    """
    ledger = filled(tmp_path, references=[],
                    text='Replay has been reported during quiet rest [1], [2].')
    totals = cl.counts(ledger)
    assert (totals['references'], totals['uses']) == (0, 2)
    assert totals['uses_unmatched'] == 2
    assert totals['complete'] is False, 'nothing was checked, so nothing is complete'
    lines = cl.problems(totals)
    assert len(lines) == 1
    assert '2 in-text citation marker(s) and no reference list' in lines[0]
    assert 'not one citation was checked' in lines[0]
    assert 'No citation was checked' in cl.summary_line(totals)
    assert '2 in-text citation marker(s)' in cl.summary_line(totals)

    # A table that was filled from a paper with neither references nor markers
    # is a different sentence, and still not silence.
    bare = cl.problems({'references': 0, 'uses': 0, 'stages': {'fill': {'done': True}}})
    assert bare == ['the citation ledger holds no references, so not one citation was checked']


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


# -- what a second review of this table reproduced --------------------------
#
# Each test below is one bug an independent review reproduced offline against
# the first fix set, and each fails without its fix.


def test_only_the_doi_resolver_can_make_a_doi_not_exist(tmp_path):
    """Crossref's agency route answering 404 is not the DOI system saying no.

    The agency index does not carry every registered DOI: the arXiv DOI
    ``10.48550/arxiv.1706.03762`` resolves perfectly well and that route can
    still answer 404 for a DOI like it, after which nothing asked DataCite or
    anything else — ``exists: no``, on one index's silence. ``no`` now needs
    ``https://doi.org/api/handles/{doi}`` to say the handle does not exist.
    """
    references = [{'id': 'r1', 'label': '1', 'title': 'Attention is all you need',
                   'year': '2017', 'first_author': 'Vaswani, A.',
                   'doi': '10.48550/arxiv.1706.03762',
                   'raw': '1. Vaswani A. Attention is all you need. arXiv, 2017.'}]
    ledger = filled(tmp_path, references, 'Earlier work differs [1].')
    net = Net([('/works/10.48550/arxiv.1706.03762/agency', 404),
               ('/works/10.48550', 404),
               HANDLE,
               ('/works?', crossref_search([])),
               ('/search?', europepmc([]))])
    cl.resolve(ledger, lookups(tmp_path, net))
    resolution = {row['row_id']: row for row in ledger.rows()}['ref:r1']['resolution']
    assert any('doi.org/api/handles' in url for url in net.urls)
    assert resolution['exists'] == 'not located'
    assert resolution['exists'] != 'no'
    assert resolution['printed_doi_registered'] is True

    # And the resolver not answering is could not check, never no: a route that
    # is down says nothing about an identifier.
    quiet = cl.Ledger(tmp_path / 'quiet')
    cl.build(quiet, paper(references, 'Earlier work differs [1].'))
    broken = Net([('/works/10.48550/arxiv.1706.03762/agency', 404),
                  ('/works/10.48550', 404),
                  ('/works?', crossref_search([])),
                  ('/search?', europepmc([]))],
                 fail=['doi.org/api/handles'])
    cl.resolve(quiet, cl.Lookups(tmp_path / 'quiet' / 'citations' / 'lookups', opener=broken))
    resolution = {row['row_id']: row for row in quiet.rows()}['ref:r1']['resolution']
    assert resolution['exists'] == 'could not check'
    assert 'the DOI resolver did not answer' in resolution['note']

    # A 200 whose body is not the documented shape is the same: no code, no
    # claim. (The handle API answers its own status in the body, so a 404 there
    # is read for its `responseCode` and nothing else is.)
    odd = cl.Ledger(tmp_path / 'odd')
    cl.build(odd, paper(references, 'Earlier work differs [1].'))
    shapeless = Net([('/works/10.48550/arxiv.1706.03762/agency', 404),
                     ('/works/10.48550', 404),
                     ('doi.org/api/handles', {'values': []}),
                     ('/works?', crossref_search([])),
                     ('/search?', europepmc([]))])
    cl.resolve(odd, cl.Lookups(tmp_path / 'odd' / 'citations' / 'lookups', opener=shapeless))
    assert {row['row_id']: row for row in odd.rows()}['ref:r1'][
        'resolution']['exists'] == 'could not check'


def test_a_doi_is_checked_the_same_way_whatever_printed_it(tmp_path):
    """A DOI the extractor handed over gets the checks a raw string gets.

    ``doi_in`` refuses a DOI broken over a line or cut off mid-suffix, and
    nothing applied those checks to the ``doi`` field ``ingest`` filled: the
    truncated ``10.1016/j.cell.2020.01.`` went to Crossref as printed, 404, and
    the reference read as a DOI nobody registered. And a DOI holding a dot
    segment is a path traversal, not an identifier.
    """
    cut = {'id': 'r1', 'label': '1', 'title': TITLE, 'year': '2020',
           'first_author': 'Lin, M.', 'doi': '10.1016/j.cell.2020.01.',
           'raw': '1. Lin M. ' + TITLE + '. Cell, 2020.'}
    rows = cl.reference_rows({'references': [cut], 'blocks': []})
    assert rows[0]['printed']['doi'] is None, 'a truncated DOI is not a DOI to look up'

    # Nothing with a dot segment reaches a URL, whichever URL it would have
    # been: `10.1234/../../members/98` became `api.crossref.org/members/98`,
    # a 200 about a publisher read as the record for a reference.
    assert cl.clean_doi('10.1234/../../members/98') is None
    assert cl.doi_in('see 10.1234/../../members/98 for this') is None
    for url_of in (cl.crossref_work_url, cl.datacite_url, cl.doi_handle_url):
        with pytest.raises(ValueError):
            url_of('10.1234/../../members/98')

    # End to end: the truncated DOI is never looked up, so the row is decided
    # on the title search alone and is never `no`.
    ledger = filled(tmp_path, [cut], 'Replay has been reported during quiet rest [1].')
    net = Net([('/works?', crossref_search([])), ('/search?', europepmc([]))])
    cl.resolve(ledger, lookups(tmp_path, net))
    resolution = {row['row_id']: row for row in ledger.rows()}['ref:r1']['resolution']
    assert not any('/works/10.1016' in url for url in net.urls)
    assert resolution['exists'] == 'not located'
    assert resolution['printed_doi'] is None


def test_a_hit_by_other_authors_is_not_the_work_even_in_the_same_year(tmp_path):
    """The year cannot corroborate a title hit, because the query is filtered to it.

    Printed: *…the Reynolds Risk Score for women*, Ridker 2005. Returned: the
    same score *for men*, Hennekens 2006 — a different paper by different
    authors, which scored above the search bar and agreed on the year because
    :func:`crossref_search_url` had already filtered the query to that year
    plus or minus one. The row resolved to it, its abstract became the source
    text every verdict about that reference was judged against, and the
    mismatch that came out of it reported the citing paper for printing the
    wrong first author.
    """
    women = ('C-reactive protein and parental history improve global cardiovascular risk '
             'prediction: the Reynolds Risk Score for women')
    men = ('C-reactive protein and parental history improve global cardiovascular risk '
           'prediction: the Reynolds Risk Score for men')
    references = [{'id': 'r1', 'label': '1', 'title': women, 'year': '2005',
                   'first_author': 'Ridker, P. M.',
                   'raw': '1. Ridker PM, et al. ' + women + '. Circulation, 2007.'}]
    ledger = filled(tmp_path, references, 'Risk scores of this kind are established [1].')
    net = Net([('/works?', crossref_search([
                   {'DOI': '10.1001/jama.297.6.611', 'title': [men],
                    'author': [{'given': 'Charles', 'family': 'Hennekens'}],
                    'issued': {'date-parts': [[2006]]},
                    'container-title': ['JAMA'], 'type': 'journal-article',
                    'abstract': 'A risk score for men.'}])),
               ('/search?', europepmc([]))])
    cl.resolve(ledger, lookups(tmp_path, net))
    resolution = {row['row_id']: row for row in ledger.rows()}['ref:r1']['resolution']
    assert resolution['exists'] == 'not located'
    assert resolution['mismatched'] == [], 'no field of another paper is a finding'
    assert resolution.get('abstract') is None
    # The hit is kept, named to the specialist, and never the reference.
    assert cl.surname(resolution['candidate']['first_author']) == 'hennekens'
    assert resolution['candidate']['corroborated_by'] is None
    assert resolution['candidate']['title_similarity'] >= cl.SEARCH_MATCH


def test_a_notice_about_a_work_is_never_the_work(tmp_path):
    """A correction's title is the work's title with a few words round it.

    So it scores at the top of a title search for the work it is about. Only a
    notice word at the very start was matched, and ``Author Correction: …``,
    ``Publisher Correction: …``, ``Addendum: …`` and ``<title>: a comment`` all
    passed as the cited work — with the notice's abstract handed to the workers
    as the source text for it.
    """
    for title in ('Author Correction: ' + TITLE,
                  'Publisher Correction: ' + TITLE,
                  'Addendum: ' + TITLE,
                  'Notice of retraction: ' + TITLE,
                  'RETRACTED ARTICLE: ' + TITLE,
                  TITLE + ': a comment',
                  TITLE + ' — reply',
                  'Retraction of ' + TITLE):
        assert cl.notice_title(title) is True, title
    # A paper about corrections is a paper. The label is read only while it is
    # short enough to be a label.
    assert cl.notice_title('A study of retraction rates in cardiology: evidence from '
                           'PubMed') is False
    assert cl.notice_title(TITLE) is False

    # End to end, both ways a hit can say it is a notice: the title, and the
    # record's own update-pointing field.
    net = Net([('/works?', crossref_search([
                   {'DOI': '10.1234/correction', 'title': ['Author Correction: ' + TITLE],
                    'author': [{'given': 'Mei', 'family': 'Lin'}],
                    'issued': {'date-parts': [[2019]]}, 'type': 'journal-article',
                    'abstract': 'The author list was wrong.'},
                   {'DOI': '10.1234/typed', 'title': [TITLE],
                    'author': [{'given': 'Mei', 'family': 'Lin'}],
                    'issued': {'date-parts': [[2019]]}, 'type': 'journal-article',
                    'update-to': [{'DOI': '10.1234/replay', 'type': 'correction'}],
                    'abstract': 'A correction whose title is the original title.'}])),
               ('/search?', europepmc([]))])
    # No DOI printed, so the title search is the whole lookup.
    bare = [{'id': 'r1', 'label': '1', 'title': TITLE, 'year': '2019',
             'first_author': 'Lin, M.', 'raw': '1. Lin M. ' + TITLE + '. J Neurosci, 2019.'}]
    ledger = filled(tmp_path, bare, 'Replay has been reported during quiet rest [1].')
    cl.resolve(ledger, lookups(tmp_path, net))
    resolution = {row['row_id']: row for row in ledger.rows()}['ref:r1']['resolution']
    assert resolution['exists'] == 'not located'
    assert resolution['candidate'] is None, 'a notice is not even the nearest thing to the work'
    assert resolution.get('abstract') is None


def test_every_rate_limited_row_is_still_owed_its_retries(tmp_path):
    """``remaining`` counts rows owed a lookup, not rows this call did not reach.

    Three references, both indexes rate-limiting, one call: every row was
    attempted once, the queue this call built was empty, so ``remaining: 0``
    and the stage reported itself done — and the two further attempts
    :data:`cl.RESOLVE_ATTEMPTS` promises never happened in any real run. The
    recorder's loop stops on ``remaining``, so the retry existed only in tests
    that called ``resolve`` again by hand.
    """
    references = [{'id': 'r%d' % n, 'label': str(n), 'title': TITLE + ' part %d' % n,
                   'year': '2019', 'first_author': 'Lin, M.', 'doi': '10.1234/replay-%d' % n,
                   'raw': '%d. Lin M. %s part %d. J Neurosci, 2019.' % (n, TITLE, n)}
                  for n in (1, 2, 3)]
    ledger = filled(tmp_path, references, 'Three earlier reports [1], [2], [3].')
    limited = Net([('/works/', 429), ('/works?', 503), ('/search?', 503)])
    answer = cl.resolve(ledger, lookups(tmp_path, limited))
    assert answer['resolved_now'] == 3
    assert answer['remaining'] == 3, 'a row that could not be checked is owed another attempt'
    assert ledger.state()['resolve']['done'] is False
    # And the caller is told to wait rather than spend its turns on rows that
    # are not due: the wait is minutes, because a 429 is a minutes-long state.
    assert answer['retry_wait'] == cl.RETRY_WAIT_SECONDS >= 60
    assert 0 < answer['retry_after'] <= cl.RETRY_WAIT_SECONDS
    assert cl.resolve(ledger, lookups(tmp_path, limited))['resolved_now'] == 0

    # Once the wait has passed, the attempts happen — and the stage is done
    # only when no row is owed one.
    for _ in range(cl.RESOLVE_ATTEMPTS):
        answer = cl.resolve(ledger, lookups(tmp_path, limited), wait=0)
    assert answer['remaining'] == 0 and answer['retry_after'] == 0
    assert ledger.state()['resolve']['done'] is True
    attempts = [row['resolution']['attempts'] for row in ledger.rows('reference')]
    assert attempts == [cl.RESOLVE_ATTEMPTS] * 3


def test_a_line_torn_inside_a_character_is_one_bad_row_not_a_dead_table(tmp_path):
    """A run killed mid-write can cut a line inside a multi-byte character.

    A text stream decodes while it iterates, so one torn ``ö`` raised
    ``UnicodeDecodeError`` out of :meth:`cl.Ledger.load` and every row in the
    file was lost — and the newline repair on the next append wrote a newline
    after the broken bytes and made it permanent. One torn line is one skipped,
    counted row, which is what the rest of the class already promised.
    """
    ledger = filled(tmp_path)
    row = ledger.rows('reference')[0]
    torn = json.dumps({**row, 'row_id': 'ref:r2',
                       'printed': {**row['printed'], 'title': 'Über die Größe'}},
                      ensure_ascii=False).encode('utf-8')
    cut = torn.index('Größe'.encode('utf-8')) + 3  # one byte into the ö
    with ledger.path.open('ab') as stream:
        stream.write(torn[:cut])
    # The append repairs the missing newline, which is what used to make the
    # tear permanent, and the row written after it is readable.
    ledger.append({**row, 'row_id': 'ref:r3'})

    rows, broken = ledger.load()
    assert broken == 1
    assert 'ref:r1' in rows and 'ref:r3' in rows, 'one torn line lost the whole table'
    assert 'ref:r2' not in rows
    totals = cl.counts(ledger)
    assert totals['unreadable_rows'] == 1
    assert any('could not be read' in line for line in cl.problems(totals))


def test_a_journal_is_not_its_own_family(tmp_path):
    """Containment read *Nature* and *Nature Methods* as one venue.

    The family name is a prefix of every journal in the family, so the venue
    mismatch that matters most in a reference list was the one comparison that
    could not raise it. Tokens in place instead, with abbreviation per token.
    """
    for printed, record in (('Nature', 'Nature Methods'),
                            ('Science', 'Science Advances'),
                            ('Cell', 'Cell Reports'),
                            ('Nature Methods', 'Nature Neuroscience'),
                            ('Journal of Neuroscience', 'Journal of Neurochemistry')):
        assert cl._venue_same(printed, record) is False, (printed, record)
    # And what the token comparison exists for keeps working: a style's
    # abbreviation is the same venue, country and edition leftovers included.
    for printed, record in (('J Biol Chem', 'Journal of Biological Chemistry'),
                            ('Proc. Natl. Acad. Sci. U.S.A.',
                             'Proceedings of the National Academy of Sciences'),
                            ('Nat. Methods', 'Nature Methods'),
                            ('J Neurosci', 'Journal of Neuroscience')):
        assert cl._venue_same(printed, record) is True, (printed, record)

    # End to end on the DOI path, where a venue mismatch can be raised: the
    # paper printed the family, the record is one journal in it.
    references = [{'id': 'r1', 'label': '1', 'title': TITLE, 'year': '2019',
                   'first_author': 'Lin, M.', 'doi': '10.1234/replay',
                   'venue': 'Nature',
                   'raw': '1. Lin M. ' + TITLE + '. Nature, 2019.'}]
    ledger = filled(tmp_path, references, 'Replay has been reported during quiet rest [1].')
    net = Net([('/works/10.1234', crossref_work('10.1234/replay', TITLE, '2019',
                                                venue='Nature Methods')),
               ('DOI%3A', europepmc([]))])
    cl.resolve(ledger, lookups(tmp_path, net))
    resolution = {row['row_id']: row for row in ledger.rows()}['ref:r1']['resolution']
    assert resolution['exists'] == 'yes'
    assert resolution['mismatched'] == ['venue']


def test_a_table_of_nothing_but_could_not_check_is_not_complete(tmp_path):
    """``complete`` means answered, not attempted.

    Every reference had a resolution and every use had a verdict, so a run
    whose lookups never answered reported ``complete: True`` — and that one
    number is what a reader uses to decide how much of the reference list was
    checked.
    """
    ledger = filled(tmp_path)
    down = Net(fail=['api.crossref.org', 'www.ebi.ac.uk', 'api.datacite.org', 'doi.org'])
    for _ in range(cl.RESOLVE_ATTEMPTS):
        cl.resolve(ledger, lookups(tmp_path, down), wait=0)
    # Nothing is owed another attempt, so the stage is done: the stage finished
    # and the table still checked nothing.
    assert ledger.state()['resolve']['done'] is True
    row = cl.batch(ledger, 'cite-1')[0]
    stored, why = cl.verdict(ledger, 'cite-1', row['row_id'], 'cannot tell')
    assert why is None and stored['verdict'] == 'cannot tell'

    totals = cl.counts(ledger)
    assert (totals['could_not_check'], totals['uses_unjudged']) == (1, 0)
    assert totals['stages_done'] is True
    assert totals['complete'] is False
    assert any('could not be checked' in line for line in cl.problems(totals))


def test_a_doi_broken_across_a_line_is_not_a_doi_at_all(tmp_path):
    """A two-column PDF breaks a DOI mid-suffix, and the fragment is not one.

    ``doi:10.1007/s00134-020-\\n05991-x`` was read as
    ``10.1007/s00134-020-``, the handle API truthfully said no such handle, and
    the row accused the paper of printing a DOI that was never registered. The
    four ways a line break lands — after a hyphen, after the prefix slash,
    after a dot, after an underscore — all end the fragment on punctuation, so
    a trimmed DOI that does not end on a letter or a digit is not a DOI.
    """
    for text in ('doi:10.1007/s00134-020-\n05991-x',
                 'https://doi.org/10.1093/bioinformatics/\nbtz123',
                 '10.1016/j.cell.2020.01.\n001',
                 'doi: 10.1186/s12859-019-\n2778-5',
                 'Available at 10.5281/zenodo.\n1234567'):
        assert cl.doi_in(text) is None, text

    # The two shapes that are *not* breaks and were being thrown away with
    # them: a DOI at the end of a sentence, and the bracketed suffix every
    # Lancet DOI has. Both are the real identifier.
    assert cl.doi_in('doi:10.1016/j.cell.2020.01.001.') == '10.1016/j.cell.2020.01.001'
    assert cl.doi_in('10.1016/S0140-6736(20)30183-5.') == '10.1016/s0140-6736(20)30183-5'
    assert cl.doi_in('10.1093/nar/gkh103 (2004)') == '10.1093/nar/gkh103'

    # And the two it still refuses: a suffix cut off before its last segment
    # (a one- or two-digit tail before the dot is a number, not a sentence),
    # and a SICI, whose angle brackets make the half that was extracted look
    # like a whole identifier.
    assert cl.doi_in('10.1016/j.cell.2020.01.') is None
    assert cl.doi_in('see 10.1002/(SICI)1097-0258(19980815)17:15<1623::AID-SIM871>'
                     '3.0.CO;2-S here') is None


def test_only_a_doi_the_reference_list_gives_can_be_a_doi_that_does_not_exist(tmp_path):
    """Where the DOI was read decides what an unregistered DOI is allowed to mean.

    ``10.1038/nature12373Published online…`` extracts as a clean-shaped DOI
    with a word stuck on the end, so no shape check can catch it; the handle
    API says no such handle and the row read "the printed DOI is not registered
    with any agency" about a DOI the paper never printed. A truncated
    extraction and a wrong printed DOI look the same from here, so only a DOI
    the reference list hands over as its own field may be ``no``.
    """
    run_on = '1. Lin M. ' + TITLE + '. Nature 2013;499:295. 10.1038/nature12373Published online'
    scraped = [{'id': 'r1', 'label': '1', 'title': TITLE, 'year': '2013',
                'first_author': 'Lin, M.', 'raw': run_on}]
    rows = cl.reference_rows({'references': scraped, 'blocks': []})
    assert rows[0]['printed']['doi'] == '10.1038/nature12373published'
    assert rows[0]['printed']['extracted']['doi'] == 'raw', 'nobody handed this over'

    # Nothing indexes it and the resolver has no handle for it: still not `no`.
    ledger = filled(tmp_path, scraped, 'Replay has been reported during quiet rest [1].')
    net = Net([NO_HANDLE, ('/works/10.1038', 404), ('/works?', crossref_search([])),
               ('/search?', europepmc([]))])
    cl.resolve(ledger, lookups(tmp_path, net))
    row = {r['row_id']: r for r in ledger.rows()}['ref:r1']
    resolution = row['resolution']
    assert resolution['exists'] == 'not located'
    assert resolution['printed_doi_registered'] is False
    assert resolution['printed_doi_source'] == 'raw'
    assert 'check the DOI in the PDF' in resolution['note']
    assert 'check the PDF' in cl.reference_problem(row)

    # The same DOI as the field `ingest` filled is the paper's own claim about
    # its own reference, and that one the resolver may contradict.
    given = [dict(scraped[0], doi='10.1038/nature12373published')]
    field = cl.Ledger(tmp_path / 'field')
    cl.build(field, paper(given, 'Replay has been reported during quiet rest [1].'))
    cl.resolve(field, cl.Lookups(tmp_path / 'field' / 'citations' / 'lookups', opener=Net(
        [NO_HANDLE, ('/works/10.1038', 404), ('/works?', crossref_search([])),
         ('/search?', europepmc([]))])))
    decided = {r['row_id']: r for r in field.rows()}['ref:r1']['resolution']
    assert decided['exists'] == 'no'
    assert decided['printed_doi_source'] == 'reference'
    assert 'check the DOI in the PDF' not in (decided['note'] or '')

    # And when the title search does find the work, the row is the work: the
    # DOI read off the text is not a mismatch against the record's DOI, it is
    # a line break to go and look at.
    hit = {'DOI': '10.1038/nature12373', 'title': [TITLE],
           'author': [{'given': 'Mei', 'family': 'Lin'}],
           'issued': {'date-parts': [[2013]]}, 'container-title': ['Nature'],
           'type': 'journal-article', 'abstract': 'Replay during quiet rest.'}
    found = cl.Ledger(tmp_path / 'found')
    cl.build(found, paper(scraped, 'Replay has been reported during quiet rest [1].'))
    cl.resolve(found, cl.Lookups(tmp_path / 'found' / 'citations' / 'lookups', opener=Net(
        [NO_HANDLE, ('/works/10.1038', 404), ('/works?', crossref_search([hit])),
         ('/search?', europepmc([]))])))
    resolved = {r['row_id']: r for r in found.rows()}['ref:r1']['resolution']
    assert resolved['exists'] == 'yes'
    assert 'doi' not in resolved['mismatched']
    assert 'check the PDF' in resolved['note']


def test_a_throttled_doi_resolver_is_could_not_check_whatever_its_body_says(tmp_path):
    """``responseCode: 100`` only means "no such handle" in an answer about one.

    A proxy in front of doi.org answers 429 and 503 with whatever body it
    likes, and the handle leg read the code out of the body without looking at
    the status it came with — so a throttled route could make a registered DOI
    ``no``. ``no`` now needs the 404 the API uses for a handle it does not
    have.
    """
    references = [{'id': 'r1', 'label': '1', 'title': TITLE, 'year': '2019',
                   'first_author': 'Lin, M.', 'doi': '10.1234/replay',
                   'raw': '1. Lin M. ' + TITLE + '. J Neurosci, 2019.'}]
    for status in (429, 503):
        run = tmp_path / str(status)
        ledger = cl.Ledger(run)
        cl.build(ledger, paper(references, 'Replay has been reported during quiet rest [1].'))
        net = Net([('doi.org/api/handles', (status, {'responseCode': 100, 'handle': 'x'})),
                   ('/works/10.1234', 404), ('/works?', crossref_search([])),
                   ('/search?', europepmc([]))])
        for _ in range(cl.RESOLVE_ATTEMPTS):
            cl.resolve(ledger, cl.Lookups(run / 'citations' / 'lookups', opener=net), wait=0)
        resolution = {r['row_id']: r for r in ledger.rows()}['ref:r1']['resolution']
        assert resolution['exists'] != 'no', status
        assert resolution['printed_doi_registered'] is None, status


def test_an_acronym_is_not_a_different_venue(tmp_path):
    """The reference list prints *PNAS*; the index holds the words it stands for.

    A token comparison has nothing to compare a one-token acronym against, so
    every reference printed in the style most journals ask for raised "printed
    venue differs from the indexed record" — a mismatch the specialist then
    spent a re-check on. One token is read as an initialism of the other
    side's significant words, a parenthetical edition is dropped before the
    comparison, and anything still unclear is not compared rather than wrong.
    """
    for printed, record in (('PNAS', 'Proceedings of the National Academy of Sciences'),
                            ('NEJM', 'The New England Journal of Medicine'),
                            ('JAMA', 'Journal of the American Medical Association'),
                            ('BMJ', 'British Medical Journal'),
                            ('JACS', 'Journal of the American Chemical Society'),
                            ('NeurIPS', 'Advances in Neural Information Processing Systems'),
                            ('PNAS', 'Proc Natl Acad Sci U S A')):
        assert cl._venue_same(printed, record) is True, (printed, record)

    # A parenthetical is an index's own edition note, not part of the name.
    for printed, record in (('Science (80-. )', 'Science'),
                            ('Bioinformatics', 'Bioinformatics (Oxford, England)'),
                            ('Science', 'Science (80-. )')):
        assert cl._venue_same(printed, record) is True, (printed, record)

    # None of which weakens the comparison that matters: a family name is
    # still not one journal in the family.
    for printed, record in (('Nature', 'Nature Methods'),
                            ('Cell', 'Cell Reports'),
                            ('Science', 'Science Advances')):
        assert cl._venue_same(printed, record) is False, (printed, record)

    # And a one-token name that is neither an initialism nor a prefix is not
    # compared, because this cannot tell which it is.
    assert cl._venue_same('Methods', 'Nature Methods') is None

    # End to end on the DOI path, the one path a venue mismatch can be raised
    # from: printed PNAS, indexed in full, and no problem to re-check.
    references = [{'id': 'r1', 'label': '1', 'title': TITLE, 'year': '2019',
                   'first_author': 'Lin, M.', 'doi': '10.1234/replay', 'venue': 'PNAS',
                   'raw': '1. Lin M. ' + TITLE + '. PNAS, 2019.'}]
    ledger = filled(tmp_path, references, 'Replay has been reported during quiet rest [1].')
    net = Net([('/works/10.1234', crossref_work(
                   '10.1234/replay', TITLE, '2019',
                   venue='Proceedings of the National Academy of Sciences')),
               ('DOI%3A', europepmc([]))])
    cl.resolve(ledger, lookups(tmp_path, net))
    row = {r['row_id']: r for r in ledger.rows()}['ref:r1']
    assert row['resolution']['exists'] == 'yes'
    assert row['resolution']['mismatched'] == []
    assert not cl.reference_problem(row)
