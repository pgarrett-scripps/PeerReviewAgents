"""Checks for scientific source integrity and bounded investigation."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from peerreviewagents.engine.annotations import normalize_review,export_draft,verify_workspace
from peerreviewagents.engine.calculator import calculate
from peerreviewagents.engine.storage import digest,write
from peerreviewagents.engine.investigation import Investigation


class SourceIntegrity(unittest.TestCase):
    def setUp(self):
        text='A 🧠 finding has 12 samples. A second statement.'
        self.paper={'id':'paper','source_sha256':'source','blocks':[{'id':'b1','text':text,'sha256':digest(text)}]}
        self.finding={'title':'Clarify unit','block_id':'b1','quote':'12 samples','kind':'edit','replacement':'12 donors','evidence_blocks':['b1'],'comment':'Clarify unit'}

    def review(self,findings=None):
        return normalize_review(self.paper,{'findings':findings or [self.finding]},'review')

    def decisions(self,review):
        return {'paper_id':'paper','review_id':'review','items':{a['id']:'accepted' for a in review['findings']}}

    def test_unicode_and_immutable_source(self):
        r=self.review()
        before=copy.deepcopy(self.paper)
        out=export_draft(self.paper,r,self.decisions(r))
        self.assertIn('12 donors',out['proposed_blocks'][0]['text'])
        self.assertEqual(self.paper,before)
        self.assertEqual(r['findings'][0]['issue_status'],'open')

    def test_stale_source_rejected(self):
        r=self.review()
        self.paper['blocks'][0]['text']+=' Changed.'
        with self.assertRaises(ValueError):export_draft(self.paper,r,self.decisions(r))

    def test_overlapping_edits_rejected(self):
        r=self.review([self.finding,{**self.finding,'quote':'samples','replacement':'participants'}])
        with self.assertRaises(ValueError):export_draft(self.paper,r,self.decisions(r))

    def test_novel_numbers_withheld(self):
        r=self.review([{**self.finding,'replacement':'24 samples'}])
        self.assertEqual(r['findings'][0]['kind'],'question')
        with self.assertRaises(ValueError):export_draft(self.paper,r,self.decisions(r))

    def test_gene_identifier_is_not_a_new_measurement(self):
        r=self.review([{**self.finding,'replacement':'12 samples with ELK1 expression'}])
        self.assertEqual(r['findings'][0]['kind'],'edit')

    def test_ambiguous_anchor_is_retained(self):
        self.paper['blocks'][0]['text']='aaa'
        r=self.review([{**self.finding,'quote':'aa'}])
        self.assertEqual(r['findings'][0]['placement'],'unplaced')
        self.assertEqual(len(r['findings']),1)

    def test_missing_anchor_does_not_discard_valid_findings(self):
        r=self.review([self.finding,{**self.finding,'quote':'absent'}])
        self.assertEqual(r['annotation_counts'],{'total':2,'anchored':1})

    def test_manifest_tampering_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            write(root/'paper.json',self.paper)
            write(root/'manifest.json',{'files':{'paper.json':digest((root/'paper.json').read_bytes())}})
            verify_workspace(root)
            write(root/'paper.json',{'changed':True})
            with self.assertRaises(ValueError):verify_workspace(root)

    def test_calculator_blocks_code_and_unbounded_values(self):
        self.assertAlmostEqual(calculate('mean([2,4,6])'),4)
        for expression in ['__import__("os")','2**100000','[x for x in [1]]','open("file")']:
            with self.assertRaises(ValueError):calculate(expression)

    def test_tool_limit_and_notes(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            write(root/'paper.json',self.paper)
            write(root/'manifest.json',{'files':{'paper.json':digest((root/'paper.json').read_bytes())}})
            server=Investigation({'paper':d,'output':str(root/'out'),'cache':str(root/'cache'),'tool_limit':1})
            self.assertTrue(server.call('save_note',{'text':'A hypothesis','evidence_blocks':['b1']})['saved'])
            self.assertIn('notice',server.call('search',{'query':'samples'}))
            self.assertEqual(len((root/'out/notes.jsonl').read_text().splitlines()),1)

    def test_imported_pilot_references_and_table_boundaries(self):
        path=Path('runs/pra-workspace-pilot-v1/paper-v2/paper.json')
        if not path.exists():self.skipTest('Pilot artifact unavailable')
        paper=json.loads(path.read_text())
        self.assertEqual(len(paper['references']),67)
        tables=[b for b in paper['blocks'] if b['document']=='S1_Table.docx' and b['kind']=='table']
        self.assertEqual(len(tables),1)
        self.assertTrue(any(len(row)>1 for row in tables[0]['table']))
        self.assertTrue(all(b['page_mapping']=='unavailable' for b in paper['blocks'] if b['page'] is None))

    def test_corrected_doi_import_preserves_source_blocks(self):
        new=Path('runs/pra-workspace-pilot-v1/import-qa-v3/paper.json')
        if not new.exists():self.skipTest('Corrected import unavailable')
        original=json.loads(Path('runs/pra-workspace-pilot-v1/paper-v2/paper.json').read_text())
        corrected=json.loads(new.read_text())
        self.assertEqual(corrected['blocks'],original['blocks'])
        self.assertEqual(sum(bool(r['doi']) for r in corrected['references']),61)


if __name__=='__main__':unittest.main()
