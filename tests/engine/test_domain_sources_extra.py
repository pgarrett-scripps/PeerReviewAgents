"""Exact identity and bounded field selection for extra public source adapters."""
import unittest
from urllib.parse import urlsplit

from peerreviewagents.engine.source_adapters_extra import PROVIDERS, parse, request


class ExtraSourceAdapterTests(unittest.TestCase):
    def test_requests_use_expected_https_hosts(self):
        identifiers = {
            'string-interactions':'9606:TP53,MDM2',
            'ensembl':'ENSG00000141510',
            'reactome':'R-HSA-69563',
            'gbif-occurrence':'123456789',
            'nasa-cmr':'C1214470488-ASF',
            'worldbank':'SP.POP.TOTL',
        }
        hosts = {
            'string-interactions':'string-db.org',
            'ensembl':'rest.ensembl.org',
            'reactome':'reactome.org',
            'gbif-occurrence':'api.gbif.org',
            'nasa-cmr':'cmr.earthdata.nasa.gov',
            'worldbank':'api.worldbank.org',
        }
        self.assertEqual(set(identifiers),set(PROVIDERS))
        for provider, identifier in identifiers.items():
            with self.subTest(provider=provider):
                parsed = urlsplit(request(provider,identifier))
                self.assertEqual(parsed.scheme,'https')
                self.assertEqual(parsed.hostname,hosts[provider])
        self.assertIsNone(request('unknown','record'))

    def test_string_exact_pair_and_explicit_channels(self):
        row = {'stringId_A':'9606.ENSP1','stringId_B':'9606.ENSP2',
            'preferredName_A':'MDM2','preferredName_B':'TP53','ncbiTaxonId':9606,
            'score':0.99,'nscore':0.0,'fscore':0.0,'pscore':0.0,
            'ascore':0.1,'escore':0.8,'dscore':0.7,'tscore':0.9}
        fields, version, published, resolved = parse('string-interactions','9606:TP53,MDM2',[row])
        self.assertEqual(resolved,'9606:TP53,MDM2')
        self.assertEqual(fields['association_channels']['experimental'],0.8)
        self.assertIn('do not establish direct binding',fields['selection_limit'])
        self.assertIsNone(version)
        self.assertIsNone(published)
        ambiguous = parse('string-interactions','9606:TP53,MDM2',[row,row])
        self.assertIsNone(ambiguous[3])
        wrong = parse('string-interactions','10090:TP53,MDM2',[row])
        self.assertIsNone(wrong[3])

    def test_ensembl_requires_exact_stable_id_and_version(self):
        record = {'id':'ENSG00000141510','object_type':'Gene','species':'homo_sapiens',
            'assembly_name':'GRCh38','biotype':'protein_coding','version':20,
            'seq_region_name':'17','start':1,'end':2,'strand':-1,
            'description':'Tumor protein p53','extra':'omitted'}
        fields, version, _, resolved = parse('ensembl','ENSG00000141510.20',record)
        self.assertEqual((version,resolved),('20','ENSG00000141510.20'))
        self.assertIn('/lookup/id/ENSG00000141510?',request('ensembl','ENSG00000141510.20'))
        self.assertNotIn('ENSG00000141510.20',request('ensembl','ENSG00000141510.20'))
        self.assertNotIn('extra',fields)
        self.assertIsNone(parse('ensembl','ENSG00000141510.19',record)[3])
        self.assertIsNone(parse('ensembl','ENST00000141510',record)[3])

    def test_reactome_exact_record_is_bounded(self):
        record = {'dbId':1,'stId':'R-HSA-69563','displayName':'p53 pathway',
            'schemaClass':'Pathway','isInDisease':False,
            'stableIdentifier':{'identifier':'R-HSA-69563','identifierVersion':3},
            'species':[{'dbId':48887,'displayName':'Homo sapiens','name':['Homo sapiens']}],
            'summation':[{'text':'Curated summary','extra':'omit'}], 'hasEvent':['large']}
        fields, version, _, resolved = parse('reactome','R-HSA-69563.3',record)
        self.assertEqual((version,resolved),('3','R-HSA-69563.3'))
        self.assertNotIn('hasEvent',fields)
        self.assertIsNone(parse('reactome','R-HSA-99999',record)[3])

    def test_gbif_occurrence_is_not_taxon_lookup(self):
        record = {'key':123,'scientificName':'Puma concolor','taxonKey':2435099,
            'basisOfRecord':'HUMAN_OBSERVATION','eventDate':'2024-01-01',
            'datasetKey':'dataset','issues':['A'] * 20,'media':['omitted']}
        fields, _, _, resolved = parse('gbif-occurrence','123',record)
        self.assertEqual(resolved,'123')
        self.assertEqual(fields['taxonKey'],2435099)
        self.assertEqual(len(fields['issues']),12)
        self.assertNotIn('media',fields)
        self.assertEqual(parse('gbif-occurrence','124',record)[3],'123')

    def test_nasa_cmr_and_worldbank_exact_selection(self):
        cmr = {'feed':{'entry':[{'id':'C1214470488-ASF','title':'Collection',
            'short_name':'S1','version_id':'4','insert_time':'2020-01-02T03:04:05Z',
            'links':[{'href':'https://example.test/' + str(i),'rel':'data'} for i in range(12)],
            'summary':'omitted'}]}}
        fields, version, published, resolved = parse('nasa-cmr','C1214470488-ASF',cmr)
        self.assertEqual((version,published,resolved),('4','2020-01-02','C1214470488-ASF'))
        self.assertEqual(len(fields['links']),8)
        self.assertNotIn('summary',fields)
        self.assertIsNone(parse('nasa-cmr','C99999-X',cmr)[3])

        worldbank = [{'lastupdated':'2026-01-15'},[{'id':'SP.POP.TOTL',
            'name':'Population, total','unit':'people','sourceNote':'Definition',
            'sourceOrganization':'World Bank','source':{'id':'2','value':'WDI'},
            'topics':[{'id':str(i),'value':'Topic'} for i in range(10)],
            'country_values':'omitted'}]]
        fields, version, _, resolved = parse('worldbank','SP.POP.TOTL',worldbank)
        self.assertEqual((version,resolved),('2026-01-15','SP.POP.TOTL'))
        self.assertEqual(len(fields['topics']),8)
        self.assertNotIn('country_values',fields)
        self.assertIsNone(parse('worldbank','NY.GDP.MKTP.CD',worldbank)[3])

    def test_invalid_identifiers_are_rejected(self):
        invalid = [('string-interactions','9606:TP53'),('ensembl','TP53'),
            ('reactome','69563'),('gbif-occurrence','taxon:1'),
            ('nasa-cmr','G123-ASF'),('worldbank','bad indicator')]
        for provider, identifier in invalid:
            with self.subTest(provider=provider), self.assertRaises(ValueError):
                request(provider,identifier)


if __name__ == '__main__':
    unittest.main()
