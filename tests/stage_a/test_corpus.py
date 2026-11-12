"""How a candidate paper comes down, and why a page is never kept as one.

The first corpus fetch saved twenty HTML pages under `.pdf` names: PMC's
"Preparing to download" interstitial and a bot challenge, both served with
status 200. Every test here is offline: the network is a dictionary of
`url -> bytes` passed in as `get`, so what is checked is the decision, not
the download.
"""

from __future__ import annotations

import http.client
import json

import corpus
import pytest

PDF = b'%PDF-1.7\nbody'
INTERSTITIAL = b'<html><body>Preparing to download...</body></html>'
CHALLENGE = b'<html><title>Just a moment...</title></html>'

LISTING = b"""<?xml version="1.0" encoding="UTF-8"?>
<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
 <Name>pmc-oa-opendata</Name>
 <Contents><Key>PMC7296073.1/PMC7296073.1.pdf</Key></Contents>
 <Contents><Key>PMC7296073.1/mmc1.pdf</Key></Contents>
 <Contents><Key>PMC7296073.2/PMC7296073.2.pdf</Key></Contents>
 <Contents><Key>PMC7296073.2/media-1.pdf</Key></Contents>
</ListBucketResult>
"""

EMPTY_LISTING = b"""<?xml version="1.0" encoding="UTF-8"?>
<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
 <Name>pmc-oa-opendata</Name><KeyCount>0</KeyCount>
</ListBucketResult>
"""


def fake_get(pages, log=None):
    """A network that answers only the URLs it was given, and records them."""
    def get(url, accept='*/*'):
        if log is not None:
            log.append(url)
        answer = pages.get(url)
        if answer is None:
            raise corpus.urllib.error.HTTPError(url, 404, 'Not Found', {}, None)
        if callable(answer):
            answer = answer()
        if isinstance(answer, Exception):
            raise answer
        return answer
    return get


def listing_url(pmcid):
    return f'{corpus.OA_BUCKET}/?list-type=2&prefix={pmcid}.'


def test_the_listing_is_read_for_keys():
    assert corpus.oa_keys(LISTING) == ['PMC7296073.1/PMC7296073.1.pdf',
                                       'PMC7296073.1/mmc1.pdf',
                                       'PMC7296073.2/PMC7296073.2.pdf',
                                       'PMC7296073.2/media-1.pdf']


def test_a_body_that_is_not_xml_is_no_keys_and_no_crash():
    assert corpus.oa_keys(CHALLENGE) == []


def test_the_article_is_chosen_over_its_supplements_and_the_newest_version():
    key = corpus.article_pdf_key(corpus.oa_keys(LISTING), 'PMC7296073')
    assert key == 'PMC7296073.2/PMC7296073.2.pdf'


def test_another_articles_key_in_the_listing_is_not_taken():
    keys = ['PMC7296073.1/PMC7296073.1.pdf', 'PMC9999999.1/PMC9999999.1.pdf']
    assert corpus.article_pdf_key(keys, 'PMC9999999') == 'PMC9999999.1/PMC9999999.1.pdf'


def test_a_paper_outside_the_subset_has_no_bucket_url():
    get = fake_get({listing_url('PMC1'): EMPTY_LISTING})
    assert corpus.oa_pdf_url('PMC1', get) == ''


def test_a_refused_listing_is_no_url_rather_than_an_error():
    assert corpus.oa_pdf_url('PMC7296073', fake_get({})) == ''


def test_the_pmcid_comes_from_the_slug_when_the_row_has_no_field():
    assert corpus.pmcid_of({'slug': 'trial-pmc6319250'}) == 'PMC6319250'
    assert corpus.pmcid_of({'pmcid': 'pmc12', 'slug': 'x-pmc99'}) == 'PMC12'
    assert corpus.pmcid_of({'slug': 'trial-nothing'}) == ''


def test_an_interstitial_is_never_written_to_the_target(tmp_path):
    path = tmp_path / 'paper.pdf'
    get = fake_get({'http://x/p.pdf': INTERSTITIAL})
    problem = corpus.fetch_pdf('http://x/p.pdf', path, get)
    assert 'not a pdf' in problem
    assert not path.exists()
    assert not list(tmp_path.iterdir())


def test_a_pdf_is_written_and_verified(tmp_path):
    path = tmp_path / 'paper.pdf'
    get = fake_get({'http://x/p.pdf': PDF})
    assert corpus.fetch_pdf('http://x/p.pdf', path, get) == ''
    assert path.read_bytes() == PDF
    assert corpus.is_pdf(path)


def test_a_page_on_the_first_try_and_the_file_on_the_second_is_kept(tmp_path):
    answers = [INTERSTITIAL, PDF]
    get = fake_get({'http://x/p.pdf': lambda: answers.pop(0)})
    path = tmp_path / 'paper.pdf'
    assert corpus.fetch_pdf('http://x/p.pdf', path, get) == ''
    assert path.read_bytes() == PDF


def test_two_failures_leave_nothing_behind_and_say_why(tmp_path):
    path = tmp_path / 'paper.pdf'
    path.write_bytes(CHALLENGE)
    tries = []
    get = fake_get({}, log=tries)
    problem = corpus.fetch_pdf('http://x/p.pdf', path, get)
    assert 'HTTPError' in problem
    assert len(tries) == 2
    assert path.read_bytes() == CHALLENGE  # untouched, and audited as broken


def test_the_bucket_is_tried_before_the_recorded_url(tmp_path):
    entry = {'slug': 'trial-pmc7296073', 'doi': '10.1/x',
             'url': 'https://publisher.example/paper.pdf'}
    asked = []
    get = fake_get({listing_url('PMC7296073'): LISTING,
                    f'{corpus.OA_BUCKET}/PMC7296073.2/PMC7296073.2.pdf': PDF}, log=asked)
    row = corpus.download_candidate(entry, tmp_path, get)
    assert row['state'] == 'downloaded'
    assert row['route'] == 'pmc-oa'
    assert 'publisher.example' not in ' '.join(asked)


def test_a_paper_outside_the_subset_falls_back_to_the_recorded_url(tmp_path):
    entry = {'slug': 'materials-pmc8128876', 'doi': '10.1/x',
             'url': 'https://publisher.example/paper.pdf'}
    get = fake_get({listing_url('PMC8128876'): EMPTY_LISTING,
                    'https://publisher.example/paper.pdf': PDF})
    row = corpus.download_candidate(entry, tmp_path, get)
    assert (row['state'], row['route']) == ('downloaded', 'recorded')


def test_openalex_is_the_last_route_and_not_resolved_before_it_is_needed(tmp_path):
    openalex = corpus.OPENALEX + '10.1/x'
    asked = []
    get = fake_get({listing_url('PMC1'): EMPTY_LISTING,
                    'https://publisher.example/paper.pdf': CHALLENGE,
                    openalex: json.dumps(
                        {'best_oa_location': {'pdf_url': 'https://repo/x.pdf'}}).encode(),
                    'https://repo/x.pdf': PDF}, log=asked)
    entry = {'slug': 'trial-pmc1', 'doi': '10.1/x',
             'url': 'https://publisher.example/paper.pdf'}
    row = corpus.download_candidate(entry, tmp_path, get)
    assert (row['state'], row['route']) == ('downloaded', 'openalex')
    assert asked.index(openalex) > asked.index('https://publisher.example/paper.pdf')


def test_every_route_failing_is_reported_as_every_route(tmp_path):
    entry = {'slug': 'trial-pmc1', 'doi': '',
             'url': 'https://publisher.example/paper.pdf'}
    get = fake_get({listing_url('PMC1'): EMPTY_LISTING,
                    'https://publisher.example/paper.pdf': INTERSTITIAL})
    row = corpus.download_candidate(entry, tmp_path, get)
    assert row['state'].startswith('recorded: not a pdf')
    assert not (tmp_path / 'trial-pmc1.pdf').exists()


def test_a_pdf_already_there_is_not_fetched_again(tmp_path):
    (tmp_path / 'trial-pmc1.pdf').write_bytes(PDF)
    asked = []
    row = corpus.download_candidate({'slug': 'trial-pmc1', 'doi': '', 'url': ''},
                                    tmp_path, fake_get({}, log=asked))
    assert row['state'] == 'already here'
    assert asked == []


def test_a_saved_page_under_a_pdf_name_counts_as_missing(tmp_path):
    (tmp_path / 'trial-pmc7296073.pdf').write_bytes(INTERSTITIAL)
    get = fake_get({listing_url('PMC7296073'): LISTING,
                    f'{corpus.OA_BUCKET}/PMC7296073.2/PMC7296073.2.pdf': PDF})
    row = corpus.download_candidate({'slug': 'trial-pmc7296073', 'doi': '', 'url': ''},
                                    tmp_path, get)
    assert row['state'] == 'downloaded'
    assert (tmp_path / 'trial-pmc7296073.pdf').read_bytes() == PDF


def test_the_audit_counts_real_pdfs_by_field_and_names_the_rest(tmp_path):
    (tmp_path / 'trial-pmc1.pdf').write_bytes(PDF)
    (tmp_path / 'trial-pmc2.pdf').write_bytes(PDF)
    (tmp_path / 'ecology-pmc3.pdf').write_bytes(PDF)
    (tmp_path / 'ecology-pmc4.pdf').write_bytes(INTERSTITIAL)
    counts, broken = corpus.audit(tmp_path)
    assert counts == {'trial': 2, 'ecology': 1}
    assert [path.name for path in broken] == ['ecology-pmc4.pdf']


def test_repair_refetches_the_saved_pages_and_names_what_is_still_one(tmp_path):
    (tmp_path / 'trial-pmc7296073.pdf').write_bytes(INTERSTITIAL)
    (tmp_path / 'trial-pmc1.pdf').write_bytes(CHALLENGE)
    (tmp_path / 'ecology-pmc2.pdf').write_bytes(PDF)
    get = fake_get({listing_url('PMC7296073'): LISTING,
                    f'{corpus.OA_BUCKET}/PMC7296073.2/PMC7296073.2.pdf': PDF,
                    listing_url('PMC1'): EMPTY_LISTING})
    report = corpus.repair(tmp_path, get)
    assert [(row['slug'], row['state']) for row in report] == [
        ('trial-pmc1', 'no open-access pdf url'),
        ('trial-pmc7296073', 'downloaded'),
    ]
    counts, broken = corpus.audit(tmp_path)
    assert counts == {'trial': 1, 'ecology': 1}
    assert [path.name for path in broken] == ['trial-pmc1.pdf']


def test_only_real_pdfs_count_as_present_pmcids(tmp_path):
    (tmp_path / 'trial-pmc1.pdf').write_bytes(PDF)
    (tmp_path / 'ecology-pmc2.pdf').write_bytes(CHALLENGE)
    assert corpus.pmcids_present(tmp_path) == {'PMC1'}


def hits(field, count, first=1):
    return [{'field': field, 'slug': f'{field}-pmc{n}', 'pmcid': f'PMC{n}', 'doi': '',
             'url': f'https://bucket/{n}.pdf'} for n in range(first, first + count)]


def test_refill_stops_at_the_quota_and_leaves_the_rest_unfetched(tmp_path):
    folder = tmp_path / 'candidates'
    folder.mkdir()
    (folder / 'trial-pmc1.pdf').write_bytes(PDF)
    asked = []
    get = fake_get({f'https://bucket/{n}.pdf': PDF for n in range(1, 10)}, log=asked)
    report = corpus.refill(tmp_path, ['trial'], 3, get=get,
                           search=lambda field, rows: hits(field, 6, first=2))
    row, = report
    assert (row['had'], row['added'], row['usable']) == (1, 2, 3)
    assert len([url for url in asked if url.startswith('https://bucket/')]) == 2


def test_refill_does_not_fetch_a_paper_another_field_already_holds(tmp_path):
    folder = tmp_path / 'candidates'
    folder.mkdir()
    (folder / 'trial-pmc5.pdf').write_bytes(PDF)
    get = fake_get({'https://bucket/5.pdf': PDF, 'https://bucket/6.pdf': PDF})
    report = corpus.refill(tmp_path, ['ecology'], 1, get=get,
                           search=lambda field, rows: hits(field, 2, first=5))
    row, = report
    assert (row['duplicates'], row['added']) == (1, 1)
    assert not (folder / 'ecology-pmc5.pdf').exists()
    assert (folder / 'ecology-pmc6.pdf').read_bytes() == PDF


def test_refill_drops_a_candidate_that_will_not_come_down_and_names_it(tmp_path):
    get = fake_get({'https://bucket/1.pdf': INTERSTITIAL, 'https://bucket/2.pdf': PDF})
    report = corpus.refill(tmp_path, ['trial'], 1, get=get,
                           search=lambda field, rows: hits('trial', 2))
    row, = report
    assert (row['dropped'], row['added'], row['usable']) == (1, 1, 1)
    assert row['failures'] and 'trial-pmc1' in row['failures'][0]


def test_a_field_already_full_is_not_searched(tmp_path):
    folder = tmp_path / 'candidates'
    folder.mkdir()
    (folder / 'trial-pmc1.pdf').write_bytes(PDF)
    searched = []

    def search(field, rows):
        searched.append(field)
        return hits(field, 3)

    report = corpus.refill(tmp_path, ['trial'], 1, get=fake_get({}), search=search)
    assert searched == []
    assert report[0]['usable'] == 1


def test_a_search_hit_without_a_pmcid_is_not_a_candidate():
    payload = {'resultList': {'result': [
        {'doi': '10.1/a', 'pmcid': 'PMC42', 'pubYear': 2020, 'title': 'A'},
        {'doi': '10.1/b', 'title': 'no pmcid'},
        {'pmcid': 'PMC43', 'title': 'no doi'},
    ]}}
    def answer(url, accept='*/*'):
        assert url.startswith(corpus.SEARCH)
        return json.dumps(payload).encode()

    out = corpus.search('trial', 3, get=answer)
    assert [row['slug'] for row in out] == ['trial-pmc42']
    assert out[0]['pmcid'] == 'PMC42'


def test_a_truncated_answer_is_asked_for_again():
    """Europe PMC cut one chunked response short and killed a whole refill."""
    truncated = http.client.IncompleteRead(b'{"resultList"')
    answers = [truncated, truncated, json.dumps({'resultList': {'result': []}}).encode()]
    asked = []
    get = fake_get({'http://x/q': lambda: answers.pop(0)}, log=asked)
    out = corpus.read_json('http://x/q', get)
    assert out == {'resultList': {'result': []}}
    assert len(asked) == 3


def test_an_answer_that_never_arrives_whole_is_unreachable_not_a_crash():
    with pytest.raises(corpus.Unreachable) as raised:
        corpus.read_json('http://x/q', fake_get({}), attempts=2)
    assert 'http://x/q' in str(raised.value)


def test_one_fields_search_failing_does_not_stop_the_other_fields(tmp_path):
    def search(field, rows):
        if field == 'trial':
            raise corpus.Unreachable('europepmc -> IncompleteRead')
        return hits(field, 1)

    get = fake_get({'https://bucket/1.pdf': PDF})
    report = corpus.refill(tmp_path, ['trial', 'ecology'], 1, get=get, search=search)
    assert [(row['field'], row['usable']) for row in report] == [('trial', 0), ('ecology', 1)]
    assert 'search:' in report[0]['failures'][0]


@pytest.mark.parametrize('argv', (['audit'], ['refill'], ['roster']))
def test_the_new_subcommands_still_need_a_data_directory(argv, capsys, monkeypatch):
    monkeypatch.delenv('PRA_STAGE_A_ROOT', raising=False)
    assert corpus.main(argv) == 2
    assert 'no Stage A data directory' in capsys.readouterr().err


def test_audit_prints_a_count_per_field_and_a_total(tmp_path, capsys):
    folder = tmp_path / 'candidates'
    folder.mkdir()
    (folder / 'trial-pmc1.pdf').write_bytes(PDF)
    (folder / 'ecology-pmc2.pdf').write_bytes(INTERSTITIAL)
    assert corpus.main(['audit', '--root', str(tmp_path)]) == 0
    printed = capsys.readouterr()
    assert 'trial          1' in printed.out
    assert 'total          1' in printed.out
    assert 'not a pdf' in printed.err
