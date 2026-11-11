import copy
import unittest
from peerreviewagents.engine.source_payload import text_block


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


if __name__=='__main__':unittest.main()
