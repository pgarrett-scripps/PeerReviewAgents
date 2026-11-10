"""Result linkage, comparability and source validation checks."""
import copy
import unittest
from peerreviewagents.engine.result_map import build_result_map, inspect_results, compare_observations


class ResultMapTests(unittest.TestCase):
    def setUp(self):
        self.paper = {'id':'paper', 'blocks':[
            {'id':'b1','text':'The result was significant (Fig 3 and Table 2).'},
            {'id':'b2','text':'Fig 3 The line is from the SLR model (Table 2g).'},
            {'id':'b3','kind':'table','text':'Table 2 Multiple regression models\nDiversity\t1.6763'},
            {'id':'b4','kind':'reference','text':'2019. Figure 8 in another publication.'},
            {'id':'b5','text':'A claim refers to Table S1.'},
            {'id':'b6','text':'The estimate is 1.2.'},
            {'id':'b7','text':'The estimate is 2.4.'}]}

    def test_map_links_claims_and_labels_without_equating_models(self):
        result = build_result_map(self.paper)
        self.assertEqual(len(result['nodes']),4)
        edge = next(e for e in result['edges'] if e['from']=='b2')
        self.assertEqual(edge['to'],['b3'])
        self.assertEqual(result['checks'][0]['status'],'needs_comparability_check')
        self.assertFalse(result['checks'][0]['scientifically_verified'])
        self.assertEqual(result['coverage']['unresolved_links'],1)
        self.assertFalse(result['coverage']['figure_pixels_transcribed'])
        view = inspect_results(result,'b2')
        self.assertEqual(view['nodes'][0]['block_id'],'b2')
        self.assertIn('b3',[n['block_id'] for n in view['nodes']])
        self.assertEqual(inspect_results(result,'b6')['nodes'],[])

    def observations(self):
        common = dict(metric='slope', outcome='diversity', model='SLR',population='all transects',
                      denominator='not applicable',contrast='per unit abundance',timepoint='full season',units='species',scale='raw')
        return [dict(common,block_id='b6',quote='The estimate is 1.2.',value='1.2'),
                dict(common,block_id='b7',quote='The estimate is 2.4.',value='2.4')]

    def test_numeric_difference_needs_reconciliation_not_endorsement(self):
        result = compare_observations(self.paper,self.observations())
        self.assertEqual(result['status'],'needs_reconciliation')
        self.assertFalse(result['scientifically_verified'])
        self.assertEqual(result,compare_observations(self.paper,self.observations()))

    def test_model_units_population_and_scale_prevent_false_equivalence(self):
        for field in ('model','units','population','denominator','contrast','scale','metric','timepoint','outcome'):
            values = self.observations()
            values[1][field]='different'
            result = compare_observations(self.paper,values)
            self.assertEqual(result['status'],'different_context')
            self.assertEqual(result['different_dimensions'],[field])
        values = self.observations()
        values[1]['model']='unknown'
        self.assertEqual(compare_observations(self.paper,values)['status'],'incomplete_context')

    def test_wrong_anchor_and_substring_value_rejected(self):
        for field,value in [('block_id','foreign'),('quote','Estimate is 1.2'),('value','1')]:
            observations = self.observations()
            observations[0][field]=value
            with self.subTest(field=field),self.assertRaises(ValueError):
                compare_observations(self.paper,observations)

    def test_duplicate_display_ids_remain_ambiguous(self):
        paper = copy.deepcopy(self.paper)
        paper['blocks'].append({'id':'b8','text':'Table 2 A second extracted caption.'})
        result = build_result_map(paper)
        self.assertEqual(result['coverage']['ambiguous_definitions'],['table-2'])
        edge = next(e for e in result['edges'] if e['from']=='b2')
        self.assertEqual(edge['to'],['b3','b8'])

    def test_supplement_reference_and_paragraph_are_not_false_caption(self):
        paper = copy.deepcopy(self.paper)
        paper['blocks'].extend([
            {'id':'b8','kind':'paragraph','text':'Table 2 confirms the effect.'},
            {'id':'b9','kind':'caption','text':'S1 Table Additional counts.'}])
        result = build_result_map(paper)
        self.assertEqual(result['coverage']['ambiguous_definitions'],[])
        self.assertEqual(next(e for e in result['edges'] if e['from']=='b5')['to'],['b9'])


if __name__=='__main__':
    unittest.main()
