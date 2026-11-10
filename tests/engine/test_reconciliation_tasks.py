import copy
import unittest

from peerreviewagents.engine.reconciliation_tasks import build_tasks, audit_tasks, figure_ids, stage_claim


class ReconciliationTaskTests(unittest.TestCase):
    def setUp(self):
        self.paper = {'blocks':[
            {'id':'c1','kind':'caption','text':'Fig 2. Treatment effects were significant for reproduction.'},
            {'id':'r1','kind':'paragraph','text':'Fig 2 shows the reproductive effects reported in S7 Table.'},
            {'id':'r2','kind':'paragraph','text':'These were exclusively postembryonic effects.'},
            {'id':'m1','kind':'paragraph','text':'Pupation and eclosion were scored daily.'}]}
        self.catalog = {'books':[{'id':'t01','file':'S7_Table.xlsx'}],
                        'blocks':[{'id':'t01-s01-r009','text':'F9=0.12221'}]}
        self.tasks = build_tasks(self.paper,self.catalog)

    def response(self):
        rows = []
        for task in self.tasks['tasks']:
            refs = [task['target']]
            if task['kind'] == 'caption_results_table':
                refs += [{'block_id':'r1','quote':self.paper['blocks'][1]['text']},
                         {'block_id':'t01-s01-r009','quote':'F9=0.12221'}]
            else:
                refs += [{'block_id':'m1','quote':self.paper['blocks'][3]['text']}]
            rows.append({'task_id':task['id'],'status':'compared','reason':'Specific comparison checked.',
                         'outcome':'Reproduction','comparison':'Treatment groups',
                         'measurement_or_test':'Reported outcomes','strongest_author_defense':'Scope may differ.',
                         'evidence':refs})
        return {'reconciliation_results':rows}

    def test_targets_include_specific_caption_and_stage_with_source_links(self):
        self.assertEqual(len(self.tasks['tasks']),2)
        caption, stage = self.tasks['tasks']
        self.assertEqual(caption['related_block_ids'],['r1'])
        self.assertEqual(caption['reported_table_ids'],['t01'])
        self.assertEqual(stage['related_block_ids'],['m1'])
        for task in self.tasks['tasks']:
            block = next(b for b in self.paper['blocks'] if b['id'] == task['target']['block_id'])
            self.assertIn(task['target']['quote'],block['text'])

    def test_consistent_results_do_not_close_unchecked_caption(self):
        response = self.response()
        self.assertTrue(audit_tasks(self.tasks,response,self.paper,self.catalog)['all_targets_compared'])
        response['reconciliation_results'][0]['evidence'].pop(0)
        result = audit_tasks(self.tasks,response,self.paper,self.catalog)
        self.assertFalse(result['all_targets_compared'])
        self.assertIn('Comparison did not cite its exact target statement',result['records'][0]['problems'])

    def test_missing_cells_or_wrong_quote_cannot_pass(self):
        for change in ('remove','change'):
            response = self.response()
            if change == 'remove':
                response['reconciliation_results'][0]['evidence'].pop()
            else:
                response['reconciliation_results'][0]['evidence'][-1]['quote'] = 'F9=0.001'
            self.assertFalse(audit_tasks(self.tasks,response,self.paper,self.catalog)['all_targets_compared'])

    def test_unresolved_and_legacy_coverage_are_not_completed_comparisons(self):
        response = self.response()
        response['reconciliation_results'][0]['status'] = 'unresolved'
        result = audit_tasks(self.tasks,response,self.paper,self.catalog)
        self.assertTrue(result['accounting_valid'])
        self.assertFalse(result['all_targets_compared'])
        self.assertFalse(audit_tasks(self.tasks,{'claim_checks':[]},self.paper,self.catalog)['accounting_valid'])
        response = self.response()
        response['reconciliation_results'].append(copy.deepcopy(response['reconciliation_results'][0]))
        self.assertFalse(audit_tasks(self.tasks,response,self.paper,self.catalog)['accounting_valid'])

    def test_supplementary_figures_do_not_link_to_main_figure_with_same_number(self):
        self.assertEqual(figure_ids('S2 Fig. See Fig 2C and Fig S3.'),{'S2','2','S3'})
        paper = copy.deepcopy(self.paper)
        paper['blocks'][0]['text'] = 'S2 Fig. Treatment effects were significant for reproduction.'
        task = build_tasks(paper,self.catalog)['tasks'][0]
        self.assertEqual(task['related_block_ids'],[])
        self.assertFalse(stage_claim('Lethality was evident ' + 'in this study ' * 10 + 'only for one comparison.'))


if __name__ == '__main__':
    unittest.main()
