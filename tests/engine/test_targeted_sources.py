"""Bounded targeted source discovery over live public endpoints."""
import io
import json
import tempfile
import unittest
from pathlib import Path

from peerreviewagents.engine.external_evidence import SourceClient
from peerreviewagents.engine.storage import read
from peerreviewagents.engine.targeted_sources import (
    TargetedSources, identifier_request, literature_request, parse_identifier, parse_literature,
)


class TargetedSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.responses = []

        def opener(req, timeout):
            self.responses.append(req.full_url)
            return io.BytesIO(json.dumps(self.payload).encode())

        self.client = SourceClient(self.root/'state', self.root/'cache', limit=6, opener=opener)
        self.sources = TargetedSources(self.client)

    def test_literature_search_has_stable_ids_and_explicit_coverage(self):
        self.payload = {'hitCount':18, 'resultList':{'result':[
            {'source':'MED','id':'123','pmid':'123','doi':'10.1/a','title':'First',
             'authorString':'A Author','firstPublicationDate':'2020-01-01','abstractText':'Evidence one.'},
            {'source':'PMC','id':'PMC9','pmcid':'PMC9','title':'Second',
             'firstPublicationDate':'2021-02-03','abstractText':'Evidence two.'}]}}
        card = self.sources.search_literature('protein interaction control','b1',limit=2)
        self.assertTrue(card['eligible'])
        self.assertEqual(card['fields']['returned_count'],2)
        self.assertTrue(card['fields']['truncated'])
        self.assertEqual(len({hit['source_id'] for hit in card['fields']['hits']}),2)
        again = parse_literature('protein interaction control',self.payload,limit=2)
        self.assertEqual(again['hits'][0]['source_id'],card['fields']['hits'][0]['source_id'])
        self.assertIn('First relevance-ranked Europe PMC page only',card['fields']['coverage'])

    def test_long_abstract_and_final_card_are_bounded(self):
        data = {'hitCount':1,'resultList':{'result':[{'source':'MED','id':'1',
            'abstractText':'x' * 2500}]}}
        hit = parse_literature('bounded abstract',data,limit=1)['hits'][0]
        self.assertEqual(len(hit['abstract']),2000)
        self.assertTrue(hit['abstract_truncated'])
        self.assertEqual(hit['abstract_omitted_characters'],500)
        card = self.client.store_card({'provider':'test','identifier':'large',
            'manuscript_block_id':'b1','cutoff':None,
            'record_version':None,'status':'ok','fields':{'large':'x' * 15000},
            'evidence_text':'x' * 15000,'eligible':True,'limitation':'Bounded test.'})
        self.assertEqual(card['status'],'normalized_record_too_large')
        self.assertFalse(card['eligible'])
        self.assertEqual(card['fields'],{})
        self.assertEqual(card['evidence_text'],'')

    def test_cutoff_is_in_request_and_filters_late_rows(self):
        url = literature_request('trial outcome','2020-12-31',3)
        self.assertIn('FIRST_PDATE',url)
        data = {'hitCount':3,'resultList':{'result':[
            {'source':'MED','id':'1','firstPublicationDate':'2020-01-01'},
            {'source':'MED','id':'2','firstPublicationDate':'2021-01-01'},
            {'source':'MED','id':'3','firstIndexDate':'2019-01-01'}]}}
        result = parse_literature('trial outcome',data,'2020-12-31',3)
        self.assertEqual([hit['id'] for hit in result['hits']],['1'])
        self.assertEqual(result['excluded_from_returned_page'],
                         {'unknown_publication_date':1,'after_cutoff':1})

    def test_client_cutoff_reaches_the_request_and_the_card(self):
        dated = SourceClient(self.root/'dated-state',self.root/'dated-cache',
                             limit=6,cutoff='2020-12-31',opener=self.client.opener)
        self.payload = {'hitCount':1,'resultList':{'result':[
            {'source':'MED','id':'42','pmid':'42','title':'Lead',
             'firstPublicationDate':'2019-01-01','abstractText':'Lead only.'}]}}
        card = TargetedSources(dated).search_literature('dated trial outcome','b1',limit=1)
        self.assertEqual(card['cutoff'],'2020-12-31')
        self.assertIn('FIRST_PDATE',self.responses[-1])
        self.assertEqual(card['fields']['cutoff'],'2020-12-31')

    def test_identifier_resolution_exact_and_ambiguous(self):
        pmc = {'resultList':{'result':[{'source':'MED','id':'42','pmid':'42','pmcid':'PMC7','title':'A'}]}}
        result = parse_identifier('pmid','42',pmc)
        self.assertEqual(result['status'],'resolved')
        self.assertTrue(result['source_id'].startswith('identifier-'))
        wrong = parse_identifier('pmid','43',pmc)
        self.assertEqual(wrong['status'],'identity_mismatch')
        ambiguous = parse_identifier('pmid','42',{'resultList':{'result':[
            {'source':'MED','id':'42','pmid':'42'},{'source':'MED','id':'42','pmid':'42'}]}})
        self.assertEqual(ambiguous['status'],'ambiguous')
        self.assertIn('EXT_ID%3A42',identifier_request('pmid','42'))
        self.assertIn('SRC%3AMED',identifier_request('pmid','42'))
        collision = parse_identifier('pmid','42',{'resultList':{'result':[
            {'source':'AGR','id':'42','pmid':'42'}]}})
        self.assertEqual(collision['status'],'identity_mismatch')
        self.assertNotIn('/v1/',identifier_request('doi','10.1234/abc'))

    def test_targeted_calls_share_six_requests_and_two_locations(self):
        self.payload = {'message':{'DOI':'10.1234/abc','title':['A']}}
        first = self.sources.resolve_identifier('doi','10.1234/abc','b1')
        self.assertTrue(first['eligible'])
        self.payload = {'resultList':{'result':[]},'hitCount':0}
        self.sources.search_literature('focused empty query','b2')
        denied = self.sources.search_literature('third location query','b3')
        self.assertEqual(denied['status'],'target_limit')
        self.assertEqual(len(read(self.root/'state/requests.json')['requests']),2)
        self.assertEqual(read(self.root/'state/targets.json'),['b1','b2'])


if __name__ == '__main__':
    unittest.main()
