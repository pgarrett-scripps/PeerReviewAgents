"""Bounded targeted source discovery and historical registration snapshots."""
import io
import json
import tempfile
import unittest
from pathlib import Path

from peerreviewagents.engine.external_evidence import SourceClient
from peerreviewagents.engine.storage import digest, read, write
from peerreviewagents.engine.targeted_sources import (
    HistoricalRegistrationStore, TargetedSources, curate_registration_snapshot, identifier_request,
    literature_request, parse_identifier, parse_literature,
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
            'manuscript_block_id':'b1','evidence_mode':'current','cutoff':None,
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

    def snapshot_store(self):
        folder = self.root/'snapshots'
        raw = self.root/'official.json'
        write(raw,{'protocolSection':{
            'identificationModule':{'nctId':'NCT01234567','briefTitle':'Trial'},
            'statusModule':{'lastUpdatePostDateStruct':{'date':'2020-01-01'}},
            'designModule':{'enrollmentInfo':{'count':100}},
            'outcomesModule':{'primaryOutcomes':[{'measure':'Pain','timeFrame':'12 weeks'}]}}})
        curate_registration_snapshot(raw,folder,'clinicaltrials','NCT01234567',
            '2020-01-01T00:00:00Z','https://clinicaltrials.gov/api/v2/studies/NCT01234567')
        return folder

    def test_historical_store_selects_latest_eligible_snapshot_read_only(self):
        folder = self.snapshot_store()
        before = {path.name:digest(path.read_bytes()) for path in folder.iterdir()}
        store = HistoricalRegistrationStore(folder)
        result = store.lookup('clinicaltrials','NCT01234567','2020-06-01')
        self.assertTrue(result['eligible'])
        self.assertEqual(result['fields']['designModule']['enrollmentInfo']['count'],100)
        self.assertEqual(result['effective_date'],'2020-01-01')
        self.assertTrue(result['source_id'].startswith('historical-'))
        missing = store.lookup('clinicaltrials','NCT01234567','2019-01-01')
        self.assertEqual(missing['status'],'historical_snapshot_unavailable')
        self.assertEqual(before,{path.name:digest(path.read_bytes()) for path in folder.iterdir()})

    def test_historical_snapshot_tampering_and_unofficial_urls_fail(self):
        folder = self.snapshot_store()
        raw_file = next(folder.glob('*.raw.json'))
        raw_file.write_text('{}')
        with self.assertRaisesRegex(ValueError,'changed'):
            HistoricalRegistrationStore(folder).lookup('clinicaltrials','NCT01234567','2020-06-01')
        folder = self.root/'metadata-mismatch'
        original = self.root/'official.json'
        curate_registration_snapshot(original,folder,'clinicaltrials','NCT01234567',
            '2020-01-01T00:00:00Z','https://clinicaltrials.gov/api/v2/studies/NCT01234567')
        metadata = next(folder.glob('*.snapshot.json'))
        row = read(metadata)
        row['fields']['designModule']['enrollmentInfo']['count'] = 999
        write(metadata,row)
        manifest = read(folder/'manifest.json')
        manifest['files'][metadata.name] = digest(metadata.read_bytes())
        write(folder/'manifest.json',manifest)
        with self.assertRaisesRegex(ValueError,'does not match'):
            HistoricalRegistrationStore(folder).lookup('clinicaltrials','NCT01234567','2020-06-01')
        raw = self.root/'wrong-url.json'
        write(raw,{'protocolSection':{'identificationModule':{'nctId':'NCT01234567'}}})
        with self.assertRaisesRegex(ValueError,'official endpoint'):
            curate_registration_snapshot(raw,self.root/'bad-snapshots','clinicaltrials',
                'NCT01234567','2020-01-01T00:00:00Z','https://example.com/NCT01234567')
        with self.assertRaisesRegex(ValueError,'does not match'):
            curate_registration_snapshot(raw,self.root/'wrong-id','clinicaltrials',
                'NCT01234567','2020-01-01T00:00:00Z',
                'https://clinicaltrials.gov/api/v2/studies/NCT99999999')

    def test_loaded_manifest_wrong_url_and_oversized_raw_are_rechecked(self):
        folder = self.snapshot_store()
        store = HistoricalRegistrationStore(folder)
        manifest = read(folder/'manifest.json')
        write(folder/'manifest.json',{**manifest,'note':'changed'})
        with self.assertRaisesRegex(ValueError,'manifest changed'):
            store.lookup('clinicaltrials','NCT01234567','2020-06-01')

        folder = self.root/'wrong-loaded-url'
        raw = self.root/'official.json'
        curate_registration_snapshot(raw,folder,'clinicaltrials','NCT01234567',
            '2020-01-01T00:00:00Z','https://clinicaltrials.gov/api/v2/studies/NCT01234567')
        metadata = next(folder.glob('*.snapshot.json'))
        row = read(metadata)
        row['source_url'] = 'https://api.osf.io/v2/registrations/NCT01234567/'
        write(metadata,row)
        manifest = read(folder/'manifest.json')
        manifest['files'][metadata.name] = digest(metadata.read_bytes())
        write(folder/'manifest.json',manifest)
        with self.assertRaisesRegex(ValueError,'source URL'):
            HistoricalRegistrationStore(folder).lookup('clinicaltrials','NCT01234567','2020-06-01')

        folder = self.root/'oversized'
        curate_registration_snapshot(raw,folder,'clinicaltrials','NCT01234567',
            '2020-01-01T00:00:00Z','https://clinicaltrials.gov/api/v2/studies/NCT01234567')
        raw_file = next(folder.glob('*.raw.json'))
        raw_file.write_bytes(b' ' * 2_000_001)
        metadata = next(folder.glob('*.snapshot.json'))
        row = read(metadata)
        row['raw_sha256'] = digest(raw_file.read_bytes())
        write(metadata,row)
        manifest = read(folder/'manifest.json')
        manifest['files'][raw_file.name] = digest(raw_file.read_bytes())
        manifest['files'][metadata.name] = digest(metadata.read_bytes())
        write(folder/'manifest.json',manifest)
        with self.assertRaisesRegex(ValueError,'too large'):
            HistoricalRegistrationStore(folder).lookup('clinicaltrials','NCT01234567','2020-06-01')

    def test_historical_path_uses_target_limit_without_network_request(self):
        store = HistoricalRegistrationStore(self.snapshot_store())
        sources = TargetedSources(self.client,store)
        card = sources.historical_registration('clinicaltrials','NCT01234567','2020-06-01','b1')
        self.assertTrue(card['eligible'])
        self.assertEqual(card['evidence_mode'],'historical')
        self.assertFalse((self.root/'state/requests.json').exists())
        self.assertEqual(read(self.root/'state/targets.json'),['b1'])

    def test_modern_capture_cannot_be_relabelled_historical_without_attestation(self):
        raw = self.root/'modern.json'
        write(raw,{'protocolSection':{
            'identificationModule':{'nctId':'NCT01234567'},
            'statusModule':{'lastUpdatePostDateStruct':{'date':'2026-01-01'}}}})
        with self.assertRaisesRegex(ValueError,'version locator and curator attestation'):
            curate_registration_snapshot(raw,self.root/'relabelled','clinicaltrials','NCT01234567',
                '2026-09-25T00:00:00Z','https://clinicaltrials.gov/api/v2/studies/NCT01234567',
                archive_effective_date='2020-01-01')
        result = curate_registration_snapshot(raw,self.root/'attested','clinicaltrials','NCT01234567',
            '2026-09-25T00:00:00Z','https://clinicaltrials.gov/api/v2/studies/NCT01234567',
            archive_effective_date='2020-01-01',version_locator='official-history-version-3',
            curator_attestation='Captured from the registry history view for version 3.')
        self.assertEqual(result['provenance_status'],'curated_snapshot_not_verified_historical_truth')
        card = HistoricalRegistrationStore(self.root/'attested').lookup(
            'clinicaltrials','NCT01234567','2020-06-01')
        self.assertTrue(card['eligible'])
        self.assertIn('does not certify historical completeness',card['limitation'])

    def test_current_records_are_ineligible_in_historical_mode_and_dates_cannot_exceed_cutoff(self):
        historical_client = SourceClient(self.root/'historical-state',self.root/'historical-cache',
            limit=6,mode='historical',cutoff='2020-12-31',opener=self.client.opener)
        sources = TargetedSources(historical_client)
        self.payload = {'message':{'DOI':'10.1234/abc','title':['A']}}
        card = sources.resolve_identifier('doi','10.1234/abc','b1')
        self.assertEqual(card['status'],'version_unavailable')
        self.assertFalse(card['eligible'])
        self.payload = {'hitCount':1,'resultList':{'result':[
            {'source':'MED','id':'42','pmid':'42','title':'Historical lead',
             'firstPublicationDate':'2019-01-01','abstractText':'Lead only.'}]}}
        search = sources.search_literature('historical trial outcome','b1',limit=1)
        self.assertEqual(search['status'],'version_unavailable')
        self.assertFalse(search['eligible'])
        store = HistoricalRegistrationStore(self.snapshot_store())
        with self.assertRaisesRegex(ValueError,'exceeds'):
            TargetedSources(historical_client,store).historical_registration(
                'clinicaltrials','NCT01234567','2021-01-01','b1')
        self.assertEqual(read(self.root/'historical-state/targets.json'),['b1'])
        with self.assertRaises(ValueError):
            TargetedSources(historical_client,store).historical_registration(
                'clinicaltrials','NCT01234567','not-a-date','b2')
        self.assertEqual(read(self.root/'historical-state/targets.json'),['b1'])


if __name__ == '__main__':
    unittest.main()
