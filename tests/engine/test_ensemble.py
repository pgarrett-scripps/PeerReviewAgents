"""Offline checks for isolated discovery, candidate pooling, and launch limits."""
import copy
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from peerreviewagents.engine import ensemble,reviewer
from peerreviewagents.engine.billing import claude_billing_record
from peerreviewagents.engine.evidence_handles import build_registry
from peerreviewagents.engine.storage import read,write,digest


class EnsembleTests(unittest.TestCase):
    def setUp(self):
        self.paper={'id':'paper','title':'Study','source_sha256':'original',
            'blocks':[{'id':'b1','text':'We studied five plants.','sha256':digest('We studied five plants.')}],
            'sections':[],'assets':[],'references':[],'statistics':{'total_words':4}}
        self.finding={'title':'Allocation','kind':'question','severity':'moderate',
            'block_id':'b1','quote':'We studied five plants.','evidence_blocks':['b1'],
            'comment':'Clarify allocation.','replacement':'','counterevidence':'','source_urls':[]}

    def record(self,comment):
        return {'review':{'findings':[{**self.finding,'comment':comment}]},
                'provenance':{'paper_id':'paper','origin':'test'}}

    def test_pool_keeps_distinct_issues_on_same_anchor_and_rejects_foreign_paper(self):
        records=[self.record('Clarify allocation.'),self.record('Clarify replication.')]
        ledger=ensemble.combine_reviews(self.paper,records)
        self.assertEqual([i['issue_id'] for i in ledger['issues']],['r1-issue-1','r2-issue-1'])
        self.assertEqual([i['components'][0]['component_id'] for i in ledger['issues']],
                         ['r1-issue-1-part-1','r2-issue-1-part-1'])
        self.assertEqual(records[0]['review']['findings'][0],self.finding)
        records[1]['provenance']['paper_id']='other'
        with self.assertRaisesRegex(ValueError,'identity differs'):
            ensemble.combine_reviews(self.paper,records)

    def test_bad_anchor_and_previously_debated_input_fail_before_pooling(self):
        record=self.record('Clarify allocation.')
        record['review']['findings'][0]['quote']='Absent text'
        with self.assertRaisesRegex(ValueError,'invalid manuscript evidence'):
            ensemble.combine_reviews(self.paper,[record])
        record=self.record('Clarify allocation.')
        record['review']['component_decisions']=[]
        with self.assertRaisesRegex(ValueError,'independent discovery drafts'):
            ensemble.combine_reviews(self.paper,[record])

    def test_invalid_counts_and_unresolved_usage_stop(self):
        for count in (0,17,True,2.5,'3'):
            with self.assertRaises(ValueError):ensemble.validate_options(count)
        with tempfile.TemporaryDirectory() as directory:
            out=Path(directory)
            write(out/'calls/one.started.json',{})
            self.assertTrue(ensemble.observed_usage(out)['unknown'])
            with self.assertRaisesRegex(ValueError,'Unresolved'):ensemble.check_budget(out,100,3)
            (out/'usage.jsonl').write_text(json.dumps({'observed_tokens':None,'usage_unknown':True})+'\n')
            with self.assertRaisesRegex(ValueError,'Unresolved'):ensemble.check_budget(out,100,3)
            (out/'usage.jsonl').write_text(json.dumps({'observed_tokens':100})+'\n')
            self.assertFalse(ensemble.observed_usage(out)['unknown'])
            with self.assertRaisesRegex(ValueError,'launch budget'):ensemble.check_budget(out,100,3)

    def simulate(self,count=None,budget=750000,empty_components=False,model=None):
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory)
            root,out=base/'paper',base/'run'
            write(root/'paper.json',self.paper)
            write(root/'manifest.json',{'files':{'paper.json':digest((root/'paper.json').read_bytes())}})
            write(base/'billing.json',{'checked_at':time.time(),'no_paid_credits_verified':True})
            if model and model.startswith('claude-'):
                write(base/'billing.json',claude_billing_record({'loggedIn':True,'authMethod':'claude.ai'}))
            handle=build_registry(self.paper).handles[0].handle_id
            calls=[]
            findings=[]
            def fake(protocol,execution,prompt,schema,**kwargs):
                config=read(execution/'workspace/tool-config.json')
                calls.append({'prompt':prompt,'schema':schema,'config':config,'label':kwargs['label'],
                              'provider':protocol['provider'],'model':protocol['model']})
                write(execution/'calls/test.started.json',{})
                (execution/'usage.jsonl').write_text(json.dumps({'observed_tokens':100})+'\n')
                finding={k:v for k,v in self.finding.items() if k not in ('block_id','quote','evidence_blocks')}
                finding.update(anchor_handle=handle,manuscript_evidence_handles=[handle],source_evidence=[])
                if kwargs['label']=='independent':
                    finding['comment']='Private reviewer request '+str(len(findings)+1)+'.'
                    findings.append(finding)
                    return {'summary':'Review','findings':[copy.deepcopy(finding)],'limitations':[],
                            'investigation_summary':'Checked manuscript','discarded_hypotheses':[]}
                decisions=[]
                for index,item in enumerate(findings,1):
                    ident=f'r{index}-issue-1'
                    decisions.append({'issue_id':ident,'scientific_status':'supported_clarification',
                        'presentation':'primary','strongest_criticism':'Allocation needs clarification.',
                        'strongest_author_defense':'A sample count is present.','decisive_question':'How allocated?',
                        'decision_reason':'Retain a proportionate question.','evidence':[{'evidence_handle':handle}],
                        'components':[] if empty_components else [{'component_id':ident+'-part-1','treatment':'preserved',
                            'final_finding_number':index,'final_quote':item['comment'],
                            'reason':'The request is retained.','evidence':[]}]})
                return {'summary':'Review','findings':copy.deepcopy(findings),'limitations':[],
                        'decisions':decisions,'new_candidates':[]}
            with patch.object(ensemble,'require_subscription'),patch.object(reviewer,'require_subscription'), \
                    patch.object(reviewer,'model_call',side_effect=fake),patch.object(ensemble,'model_call',side_effect=fake), \
                    patch.object(reviewer,'inspected_images',return_value=[]):
                options={} if count is None else {'reviewers':count}
                if model:options['model']=model
                if budget<=100:
                    with self.assertRaisesRegex(ValueError,'launch budget'):
                        ensemble.ensemble_review(root,out,base/'billing.json','2024-01-01',token_budget=budget,**options)
                    self.assertEqual(len(calls),1)
                    self.assertEqual(read(out/'progress.json')['status'],'blocked')
                    return
                if empty_components:
                    with self.assertRaisesRegex(ValueError,'exactly one treatment'):
                        ensemble.ensemble_review(root,out,base/'billing.json','2024-01-01',**options)
                    validation=read(out/'debate/synthesis-validation.json')
                    self.assertFalse(validation['valid'])
                    self.assertEqual({p['issue_id']:p['component_ids'] for p in validation['problems']
                                      if p['code']=='missing_components'},
                                     {'r1-issue-1':['r1-issue-1-part-1'],'r2-issue-1':['r2-issue-1-part-1']})
                    self.assertEqual(validation['component_ledger']['accounted_components'],0)
                    self.assertTrue((out/'debate/responses/debate-model.json').exists())
                    self.assertEqual(read(out/'debate/progress.json')['status'],'blocked')
                    return
                status=ensemble.ensemble_review(root,out,base/'billing.json','2024-01-01',**options)
            expected=2 if count is None else count
            self.assertEqual(status['usage']['calls'],expected+1)
            self.assertEqual(read(out/'editor-package.json')['reviewer_count'],expected)
            self.assertEqual(len(read(out/'debate/debate-ledger.json')['issues']),expected)
            self.assertEqual(len({c['config']['source_state'] for c in calls}),expected+1)
            for call in calls[:-1]:
                self.assertNotIn('Private reviewer request',call['prompt'])
                self.assertEqual(call['schema']['properties']['findings']['maxItems'],10)
            self.assertIn('Private reviewer request',calls[-1]['prompt'])
            self.assertEqual([c['config']['tool_limit'] for c in calls],[4]*expected+[2])
            required=[f'r{i}-issue-1' for i in range(1,expected+1)]
            decisions=calls[-1]['schema']['properties']['decisions']
            self.assertEqual((decisions['minItems'],decisions['maxItems']),(expected,expected))
            self.assertEqual(decisions['items']['properties']['issue_id']['enum'],required)
            self.assertEqual(decisions['items']['properties']['components']['items']['properties']['component_id']['enum'],
                             [i+'-part-1' for i in required])
            self.assertIn('REQUIRED COMPONENT LEDGER',calls[-1]['prompt'])
            validation=read(out/'debate/synthesis-validation.json')
            self.assertTrue(validation['valid'])
            self.assertEqual(validation['component_ledger']['accounted_components'],expected)
            provider='claude-code' if model and model.startswith('claude-') else 'codex'
            self.assertEqual({(c['provider'],c['model']) for c in calls},{(provider,model or 'gpt-5.6-terra')})

    def test_default_two_reviewers_and_one_debate(self):self.simulate()

    def test_three_reviewers_and_one_debate(self):self.simulate(3)

    def test_budget_stops_before_second_discovery(self):self.simulate(budget=100)

    def test_empty_component_synthesis_fails_and_names_missing_ids(self):self.simulate(empty_components=True)

    def test_claude_model_runs_every_stage_through_claude_code(self):self.simulate(model='claude-sonnet-5-5')

    def test_billing_record_from_the_other_provider_stops_before_inference(self):
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory)
            write(base/'codex.json',{'checked_at':time.time(),'no_paid_credits_verified':True})
            write(base/'claude.json',claude_billing_record({'loggedIn':True,'authMethod':'claude.ai'}))
            with patch.object(ensemble,'require_subscription') as auth,patch.object(ensemble,'model_call') as call:
                for billing,model in (('codex.json','claude-opus-5-5'),('claude.json','gpt-5.6-terra')):
                    with self.assertRaises(ValueError):
                        ensemble._prepare(base/model,base/billing,ensemble._protocol(model,2,750000))
                auth.assert_not_called()
                call.assert_not_called()


if __name__=='__main__':unittest.main()
