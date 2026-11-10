"""Behavioral contracts for routing, source identity, budgets and evidence transport."""
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from urllib.error import HTTPError

from peerreviewagents.engine.domain_registry import catalog, route, context
from peerreviewagents.engine.external_evidence import SourceClient, validate_references
from peerreviewagents.engine.source_adapters import request, parse
from peerreviewagents.engine.storage import read
from peerreviewagents.engine.model_runtime import tool_profile
from peerreviewagents.engine.investigation import specs
from peerreviewagents.engine.review_schema import REVIEW, with_external_evidence


class Sources(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.calls = []
        self.payload = {'primaryAccession': 'P04637', 'entryType': 'UniProtKB reviewed',
                        'organism': {'scientificName': 'Homo sapiens', 'taxonId': 9606},
                        'entryAudit': {'entryVersion': 310}, 'sequence': {'length': 393},
                        'features': [{'type': 'Site', 'description': 'Binding site'}]}

    def opener(self, req, timeout):
        self.calls.append(req.full_url)
        return io.BytesIO(json.dumps(self.payload).encode())

    def client(self, **kwargs):
        return SourceClient(self.root/'state', self.root/'cache', opener=self.opener, **kwargs)

    def test_exact_identity_cache_integrity_and_shared_budget(self):
        client = self.client(limit=1)
        card = client.lookup('uniprot', 'P04637', 'b1')
        self.assertTrue(card['eligible'])
        self.assertEqual(card['record_version'], '310')
        second = self.client(limit=1).lookup('uniprot', 'P04637', 'b1')
        self.assertTrue(second['cache_hit'])
        self.assertEqual(card['evidence_id'], second['evidence_id'])
        self.assertEqual(len(client.cards()), 1)
        self.assertEqual(len(self.calls), 1)
        denied = self.client(limit=1).lookup('uniprot', 'P12345', 'b2')
        self.assertEqual(denied['status'], 'budget_exhausted')
        self.assertEqual(len(read(self.root/'state/requests.json')['requests']), 1)
        raw = next((self.root/'cache').glob('*.raw'))
        raw.write_text('{}')
        self.assertEqual(client.lookup('uniprot', 'P04637', 'b1')['status'], 'cache_invalid')

    def test_wrong_record_never_becomes_support(self):
        card = self.client().lookup('uniprot', 'P12345', 'b1')
        self.assertEqual(card['status'], 'identity_mismatch')
        self.assertFalse(card['eligible'])
        with self.assertRaises(ValueError):
            validate_references({'findings': [{'source_evidence': [{'evidence_id': card['evidence_id'], 'quote': 'Homo sapiens'}]}]}, [card])

    def test_current_database_is_not_historical_ground_truth(self):
        card = self.client(mode='historical', cutoff='2020-01-01').lookup('uniprot', 'P04637', 'b1')
        self.assertEqual(card['status'], 'version_unavailable')
        self.assertFalse(card['eligible'])
        self.assertEqual(card['fields']['organism']['taxonId'], 9606)

    def test_older_publication_does_not_date_current_metadata(self):
        self.payload = {'message': {'DOI': '10.1234/abc', 'published': {'date-parts': [[2010, 1, 1]]},
                                    'update-to': [{'type': 'retraction', 'updated': {'date-parts': [[2026]]}}]}}
        card = self.client(mode='historical', cutoff='2020-01-01').lookup('crossref', '10.1234/abc', 'b1')
        self.assertEqual(card['status'], 'version_unavailable')
        self.assertFalse(card['eligible'])

    def test_frozen_packet_is_shared_without_network_or_shared_budget(self):
        self.client().lookup('uniprot', 'P04637', 'b1')
        from peerreviewagents.engine.storage import digest
        packet = self.root/'cache'
        hashes = {p.name: digest(p.read_bytes()) for p in packet.iterdir()}
        for condition in ('single', 'panel'):
            client = SourceClient(self.root/condition, self.root/(condition+'-cache'),
                                  frozen_cache=packet, frozen_hashes=hashes, opener=self.opener)
            self.assertTrue(client.lookup('uniprot', 'P04637', 'b1')['eligible'])
            self.assertEqual(client.lookup('uniprot', 'P12345', 'b1')['status'], 'not_in_source_packet')
        self.assertEqual(len(self.calls), 1)
        next(packet.glob('*.raw')).write_text('{}')
        with self.assertRaises(ValueError):
            client.lookup('uniprot', 'P04637', 'b1')

    def test_source_targets_are_shared_across_process_instances(self):
        self.client().lookup('uniprot', 'P04637', 'b1')
        self.client().lookup('uniprot', 'P04637', 'b2')
        self.assertEqual(self.client().lookup('uniprot', 'P04637', 'b3')['status'], 'target_limit')

    def test_fabricated_or_inexact_evidence_is_rejected(self):
        card = self.client().lookup('uniprot', 'P04637', 'b1')
        value = {'findings': [{'source_evidence': [{'evidence_id': card['evidence_id'], 'quote': 'Homo sapiens'}]}]}
        self.assertEqual(validate_references(value, [card])['checked_references'], 1)
        value['findings'][0]['source_evidence'][0]['quote'] = 'Mus musculus'
        with self.assertRaises(ValueError):
            validate_references(value, [card])

    def test_source_records_survive_normalization_and_revision_export(self):
        from peerreviewagents.engine.external_evidence import bind_sources
        from peerreviewagents.engine.annotations import normalize_review, export_draft
        from peerreviewagents.engine.storage import digest
        card = self.client().lookup('uniprot', 'P04637', 'b1')
        paper = {'id': 'paper', 'source_sha256': 'source', 'blocks': [
            {'id': 'b1', 'text': 'We studied TP53.', 'sha256': digest('We studied TP53.')} ]}
        value = {'findings': [{'title': 'Confirm organism', 'comment': 'Confirm the organism.',
            'block_id': 'b1', 'quote': 'TP53', 'kind': 'question', 'evidence_blocks': ['b1'],
            'source_evidence': [{'evidence_id': card['evidence_id'], 'quote': 'Homo sapiens'},
                                {'evidence_id': card['evidence_id'], 'quote': 'sapiens'}]}]}
        normalized = normalize_review(paper, bind_sources(value, [card]), 'review')
        exported = export_draft(paper, normalized, {'paper_id': 'paper', 'items': {}})
        self.assertEqual(exported['source_cards'][0]['evidence_id'], card['evidence_id'])
        self.assertEqual(exported['findings'][0]['external_sources'][0]['limitation'], card['limitation'])
        self.assertEqual(len(exported['findings'][0]['external_sources']), 1)
        self.assertEqual(len(exported['findings'][0]['source_evidence']), 2)

    def test_source_failures_count_and_are_typed(self):
        for code, expected in [(404, 'not_found'), (401, 'auth_required'), (429, 'rate_limited')]:
            with self.subTest(code=code):
                def fail(req, timeout):
                    raise HTTPError(req.full_url, code, 'unavailable', {}, None)
                client = SourceClient(self.root/str(code), self.root/'empty-cache', opener=fail)
                card = client.lookup('uniprot', 'P04637', 'b1')
                self.assertEqual(card['status'], expected)
                self.assertFalse(card['eligible'])
                self.assertEqual(len(read(self.root/str(code)/'requests.json')['requests']), 1)

    def test_unimplemented_provider_does_not_issue_a_request(self):
        self.assertEqual(self.client().lookup('intact', 'P04637', 'b1')['status'], 'not_implemented')
        self.assertEqual(self.calls, [])

    def test_url_and_identifier_boundaries(self):
        for url in ('http://api.crossref.org/works/x', 'https://localhost/private', 'https://secret@api.crossref.org/works/x'):
            with self.assertRaises(ValueError):
                self.client().fetch(url)
        with self.assertRaises(ValueError):
            request('uniprot', '../secret')
        self.assertIn('10.1234%2Fabc', request('crossref', '10.1234/abc'))

    def test_registry_and_taxonomy_context_are_explicit(self):
        fields, version, date, identity = parse('clinicaltrials', 'NCT01234567', {'protocolSection': {
            'identificationModule': {'nctId': 'NCT01234567'},
            'statusModule': {'lastUpdatePostDateStruct': {'date': '2026-09-01'}},
            'outcomesModule': {'primaryOutcomes': [{'measure': 'Pain at 12 weeks'}]}}})
        self.assertEqual(identity, 'NCT01234567')
        self.assertEqual(fields['historical_state'], 'unknown')
        self.assertEqual(version, '2026-09-01')
        self.assertIsNone(date)
        fields, _, _, identity = parse('string', '9606:TP53', [{'preferredName': 'TP53', 'ncbiTaxonId': 10090}])
        self.assertIsNone(identity)

    def test_domain_profile_is_explicit_and_legacy_schema_unchanged(self):
        original = copy.deepcopy(REVIEW)
        enhanced = with_external_evidence(REVIEW)
        self.assertEqual(REVIEW, original)
        self.assertIn('source_evidence', enhanced['properties']['findings']['items']['required'])
        self.assertNotIn('lookup_source', tool_profile({})[1])
        self.assertEqual(set(tool_profile({'tool_profile': 'domains-v1'})[1]), {s['name'] for s in specs(True)})


class Routing(unittest.TestCase):
    def test_registry_loads_and_routes_from_narrative_not_bibliography(self):
        packages = catalog()
        self.assertGreaterEqual(len(packages), 13)
        paper = {'blocks': [{'id': 'b1', 'text': 'We performed co-immunoprecipitation after CRISPR knockout.'},
                            {'id': 'b2', 'text': 'A randomized controlled trial with trial registration.', 'section_id': 'refs'}],
                 'sections': [{'id': 'refs', 'title': 'References'}]}
        profile = route(paper, packages)
        self.assertEqual(profile['selected'][0]['id'], 'molecular-biology')
        self.assertTrue(all(e['block_id'] != 'b2' for m in profile['selected'] for e in m['evidence']))
        self.assertIn('molecular', context(profile, packages).lower())
        self.assertEqual(route({'blocks': [{'id': 'b1', 'text': 'Ordinary unclassified discussion.'}]}, packages)['selected'], [])
        imported = {'sections': ['Methods', 'References'], 'blocks': [
            {'id': 'b1', 'text': 'We performed co-immunoprecipitation after CRISPR knockout.', 'section': 'Methods'},
            {'id': 'b2', 'text': 'A randomized controlled trial with trial registration.', 'section': 'References'}]}
        self.assertEqual(route(imported, packages)['selected'], profile['selected'])

    def test_skill_path_escape_and_duplicate_catalog_ids_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            package = root/'fake'
            package.mkdir()
            (root/'outside.md').write_text('---\nname: fake\ndescription: Test\n---\nText')
            item = {'schema_version': 1, 'id': 'fake', 'catalog_id': 'D99', 'name': 'Fake',
                    'description': 'Test', 'triggers': ['fake term'], 'skills': ['../outside.md'],
                    'providers': [], 'limitations': [], 'maturity': {}}
            (package/'manifest.json').write_text(json.dumps(item))
            with self.assertRaises(ValueError):
                catalog(root)


if __name__ == '__main__':
    unittest.main()
