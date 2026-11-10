"""Evidence identity, omission semantics, and matched comparison contracts."""
import copy
import tempfile
import unittest
from pathlib import Path
from zipfile import ZipFile

from peerreviewagents.engine.table_evidence import build_catalog, catalog_text, evidence_paper
from peerreviewagents.engine.claim_checks import (CANDIDATE_REVIEW, QUESTION_DEBATE,
    validate_review_contract, audit_checks)
from peerreviewagents.engine.review_schema import REVIEW
from peerreviewagents.engine.storage import digest


class ClaimCheckTests(unittest.TestCase):
    def setUp(self):
        self.paper = {'blocks':[{'id':'b1','text':'The biological stage was inferred.'}]}
        self.finding = {'title':'Stage evidence', 'block_id':'b1',
            'quote':'The biological stage was inferred.', 'kind':'question',
            'severity':'moderate', 'comment':'Which observation distinguishes the proposed stage?',
            'replacement':'', 'evidence_blocks':['b1'], 'counterevidence':'', 'source_urls':[]}
        self.value = {'summary':'Review', 'findings':[self.finding], 'limitations':[],
            'investigation_summary':'Source check', 'discarded_hypotheses':[],
            'selected_finding_numbers':[1], 'claim_checks':[]}

    def test_schemas_reject_format_advantage_and_leave_legacy_unchanged(self):
        validate_review_contract(self.value, self.paper)
        for key, val in [('kind','edit'), ('replacement','A new claim.')]:
            bad = copy.deepcopy(self.value)
            bad['findings'][0][key] = val
            self.assertNotIn(val, CANDIDATE_REVIEW['properties']['findings']['items']['properties'][key]['enum'])
            with self.assertRaises(ValueError):
                validate_review_contract(bad, self.paper)
        self.assertIn('edit', REVIEW['properties']['findings']['items']['properties']['kind']['enum'])
        self.assertEqual(QUESTION_DEBATE['properties']['findings']['items']['properties']['kind']['enum'], ['question'])

    def test_coverage_needs_exact_references_and_unique_required_areas(self):
        rows = [{'area':a, 'status':'consistent', 'evidence':[
            {'block_id':'b1','quote':'The biological stage was inferred.'}]} for a in
            ['reported_results','endpoint_scope','design_scope','measurement_selection']]
        self.assertTrue(audit_checks({'claim_checks':rows},self.paper,True)['valid'])
        bad = copy.deepcopy(rows)
        bad[0]['evidence'][0]['quote'] = 'The biological stage was measured.'
        self.assertFalse(audit_checks({'claim_checks':bad},self.paper,True)['valid'])
        self.assertFalse(audit_checks({'claim_checks':rows + rows[:1]},self.paper,True)['valid'])
        self.assertFalse(audit_checks({'claim_checks':rows[:3]},self.paper,True)['valid'])
        bad = copy.deepcopy(rows)
        bad[-1]['evidence'][0]['quote'] = 'All excluded observations were independently validated.'
        self.assertFalse(audit_checks({'claim_checks':bad},self.paper,True)['valid'])

    def test_mismatched_or_duplicate_selection_rejected(self):
        for numbers in ([1,1], [2], [True]):
            value = {**self.value,'selected_finding_numbers':numbers}
            with self.assertRaises(ValueError):
                validate_review_contract(value,self.paper)

    def test_literal_cells_blank_zero_merge_and_missing_formula_cache(self):
        # Minimal OOXML source fixture. No spreadsheet authoring engine or recalculation.
        ns = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'results.xlsx'
            with ZipFile(path,'w') as archive:
                archive.writestr('[Content_Types].xml', '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>')
                archive.writestr('xl/workbook.xml', f'<workbook xmlns="{ns}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Reported results" sheetId="1" r:id="rId1"/></sheets></workbook>')
                archive.writestr('xl/_rels/workbook.xml.rels', '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>')
                archive.writestr('xl/worksheets/sheet1.xml', f'<worksheet xmlns="{ns}"><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>Parental cohort</t></is></c></row><row r="2"><c r="B2"><v>0</v></c><c r="C2"><v>0.12221</v></c><c r="D2"><f>B2+1</f></c></row></sheetData><mergeCells count="1"><mergeCell ref="A1:C1"/></mergeCells></worksheet>')
            original = digest(path.read_bytes())
            catalog = build_catalog([path])
            self.assertEqual(digest(path.read_bytes()),original)
            cells = catalog['blocks'][1]['cells']
            self.assertEqual(cells,{'B2':0,'C2':0.12221,'D2':None})
            self.assertNotIn('A2',cells)
            self.assertNotIn('Parental',catalog['blocks'][1]['text'])
            sheet = catalog['books'][0]['sheets'][0]
            self.assertEqual(sheet['merged_ranges'],['A1:C1'])
            self.assertFalse(sheet['formula_cells'][0]['cached_value_available'])
            self.assertIn('C2=0.12221',catalog_text(catalog))
            enriched = evidence_paper(self.paper,catalog)
            self.assertEqual(len(enriched['blocks']),3)
            self.assertEqual(len(self.paper['blocks']),1)
            with self.assertRaises(ValueError):
                evidence_paper(enriched,catalog)
            with self.assertRaises(ValueError):
                build_catalog([path,path])


if __name__ == '__main__':
    unittest.main()
