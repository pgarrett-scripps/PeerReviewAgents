import copy
import json
import unittest
from peerreviewagents.engine.source_payload import text_block
from peerreviewagents.engine.debate_evidence import context_bundle, search_source


class SourcePayloadTests(unittest.TestCase):
    def test_exact_table_text_survives_without_duplicate_rows(self):
        block = {'id':'b1', 'kind':'table', 'text':'Results\nTrait\tp\nFecundity\t0.12221',
                 'sha256':'unchanged', 'table':[['Trait','p'],['Fecundity','0.12221']]}
        original = copy.deepcopy(block)
        result = text_block(block)
        self.assertEqual(result['text'], block['text'])
        self.assertEqual(result['sha256'], block['sha256'])
        self.assertNotIn('table', result)
        self.assertEqual(block, original)

    def test_nonredundant_or_unsupported_tables_are_preserved(self):
        for table in ([['Additional result','0.05']], [[12]], {'merged':True}, []):
            block = {'id':'b1','text':'A caption only','table':table}
            self.assertEqual(text_block(block),block)

    def test_search_and_context_keep_evidence_and_reduce_payload(self):
        rows = [['Sample '+str(i),'0.12221'] for i in range(100)]
        text = '\n'.join('\t'.join(row) for row in rows)
        block = {'id':'b1','text':text,'table':rows,'page':2}
        paper = {'title':'Study','source_sha256':'source','blocks':[block],'assets':[]}
        original = copy.deepcopy(paper)
        found = search_source(paper,['Sample'])['blocks'][0]
        bundle = context_bundle(paper,['b1'],['Sample'])
        for item in [found,bundle['requested_blocks'][0],bundle['matching_blocks'][0]]:
            self.assertEqual(item['text'],text)
            self.assertLess(len(json.dumps(item)),len(json.dumps(block)))
        self.assertEqual(paper,original)


if __name__=='__main__':unittest.main()
