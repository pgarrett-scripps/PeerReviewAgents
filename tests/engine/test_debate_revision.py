"""Preservation, source scope, and export checks for revised debate."""
import copy
import unittest

from peerreviewagents.engine.annotations import normalize_review, export_draft
from peerreviewagents.engine.debate_revision import issue_ledger, validate_revision, revision_package
from peerreviewagents.engine.model_runtime import tool_profile, APPROVED_MANUSCRIPT_TOOLS, RunBlocked
from peerreviewagents.engine.storage import digest


class DebateRevisionTests(unittest.TestCase):
    def setUp(self):
        text = 'The signal was measured. Replicate counts are not specified.'
        self.paper = {'id':'paper','title':'A causal claim','source_sha256':'source','assets':[],
                      'sections':{'Results':['b1']}, 'blocks':[
                          {'id':'b1','text':text,'sha256':digest(text),'page':1,'section':'Results'},
                          {'id':'b2','text':'The mechanism is a hypothesis.','page':2,'section':'Discussion'},
                          {'id':'b3','text':'We report causal proof.','page':1,'section':'Abstract'}]}
        self.finding = {'title':'Replication','block_id':'b1','quote':'The signal was measured.',
                        'kind':'question','severity':'major','comment':'Please quantify the signal. Report independent replicates.',
                        'replacement':'','evidence_blocks':['b1'],'counterevidence':'','source_urls':[]}
        self.ledger = issue_ledger({'findings':[self.finding]})
        ref = {'block_id':'b1','quote':'Replicate counts are not specified.'}
        components = [{'component_id':c['component_id'],'treatment':'preserved','final_finding_number':1,
                       'final_quote':c['text'],'reason':'The request remains explicit.','evidence':[]}
                      for c in self.ledger['issues'][0]['components']]
        self.value = {'summary':'Review','findings':[self.finding], 'limitations':[], 'decisions':[
            {'issue_id':'issue-1','scientific_status':'supported_clarification','presentation':'primary',
             'strongest_criticism':'Precision is unclear.','strongest_author_defense':'The signal is reported.',
             'decisive_question':'Is replication defined?','decision_reason':'Reporting remains unclear.',
             'evidence':[ref],'components':components}]}

    def check(self, value):
        return validate_revision(value, self.ledger, self.paper)

    def test_merge_cannot_silently_lose_quantification(self):
        self.assertTrue(self.check(self.value)['structural_validity'])
        bad = copy.deepcopy(self.value)
        bad['findings'][0]['comment'] = 'Report independent replicates.'
        with self.assertRaisesRegex(ValueError, 'destination'):
            self.check(bad)

    def test_every_component_requires_accounting(self):
        bad = copy.deepcopy(self.value)
        bad['decisions'][0]['components'].pop()
        with self.assertRaisesRegex(ValueError, 'Every original component'):
            self.check(bad)

    def test_overlapping_quote_occurrences_are_ambiguous(self):
        self.paper['blocks'][0]['text'] = 'aaa'
        value = copy.deepcopy(self.value)
        value['findings'][0]['quote'] = 'aa'
        with self.assertRaisesRegex(ValueError, 'ambiguous'):
            self.check(value)

    def test_resolution_requires_real_source_evidence(self):
        bad = copy.deepcopy(self.value)
        c = bad['decisions'][0]['components'][0]
        c.update(treatment='resolved',final_finding_number=0,final_quote='')
        with self.assertRaisesRegex(ValueError, 'lacks source evidence'):
            self.check(bad)
        c['evidence'] = [{'block_id':'b2','quote':'The source answers everything.'}]
        with self.assertRaisesRegex(ValueError, 'source quotation'):
            self.check(bad)

    def test_genuine_resolution_can_remove_a_finding(self):
        value = copy.deepcopy(self.value)
        self.paper['blocks'].append({'id':'b4','text':'Normalized signal and independent replicate counts are reported in Table 1.'})
        value['findings'] = []
        d = value['decisions'][0]
        d.update(scientific_status='contradicted',presentation='omitted')
        d['evidence'] = [{'block_id':'b4','quote':'Normalized signal and independent replicate counts are reported in Table 1.'}]
        for c in d['components']:
            c.update(treatment='resolved',final_finding_number=0,final_quote='',evidence=d['evidence'])
        self.assertEqual(self.check(value)['selected_findings'],0)

    def test_contradicted_finding_cannot_be_endorsed(self):
        bad = copy.deepcopy(self.value)
        bad['decisions'][0]['scientific_status'] = 'contradicted'
        with self.assertRaisesRegex(ValueError, 'Contradicted'):
            self.check(bad)

    def test_priority_does_not_imply_scientific_rejection(self):
        value = copy.deepcopy(self.value)
        value['findings'] = []
        value['decisions'][0]['presentation'] = 'supporting'
        for c in value['decisions'][0]['components']:
            c.update(treatment='deferred',final_finding_number=0,final_quote='',reason='Available as a supporting comment.')
        self.assertEqual(self.check(value)['selected_findings'],0)
        package = revision_package(value,self.ledger,self.check(value))
        review = normalize_review(self.paper,package,'revised')
        exported = export_draft(self.paper,review,{'paper_id':'paper','items':{}})
        self.assertEqual(exported['component_decisions'][0]['scientific_status'],'supported_clarification')
        self.assertEqual(len(exported['source_findings'][0]['components']),2)
        inventory = package['candidate_inventory']
        self.assertEqual(inventory['items'][0]['discovery_status'], 'discovered_candidate')
        self.assertEqual(inventory['items'][0]['selection_outcome'], 'supported_secondary')
        self.assertEqual(inventory['secondary_candidates'][0]['issue_id'], 'issue-1')
        self.assertIn('absent from the inventory', inventory['limitation'])
        self.assertEqual(exported['candidate_inventory'], inventory)
        source_by_id = {item['source_finding_id']: item
                        for item in exported['source_findings']}
        secondary_id = inventory['secondary_candidates'][0]['issue_id']
        self.assertEqual(source_by_id[secondary_id]['finding']['comment'],
                         self.finding['comment'])

    def test_fully_resolved_items_are_not_supported_secondary_concerns(self):
        value=copy.deepcopy(self.value)
        value['findings']=[]
        decision=value['decisions'][0]
        decision.update(scientific_status='supported_clarification',presentation='omitted')
        for component in decision['components']:
            component.update(treatment='resolved',final_finding_number=0,final_quote='',
                             evidence=decision['evidence'])
        package=revision_package(value,self.ledger,self.check(value))
        self.assertEqual(package['candidate_inventory']['items'][0]['selection_outcome'],'resolved_or_contradicted')
        self.assertEqual(package['candidate_inventory']['secondary_candidates'],[])

    def test_unresolved_secondary_is_not_endorsed_as_selected_or_supported(self):
        value = copy.deepcopy(self.value)
        value['findings'] = []
        decision = value['decisions'][0]
        decision.update(scientific_status='unresolved', presentation='supporting',
                        evidence=[], decision_reason='The available evidence does not decide this concern.')
        for component in decision['components']:
            component.update(treatment='deferred', final_finding_number=0,
                             final_quote='', reason='The evidence remains incomplete.', evidence=[])
        package = revision_package(value, self.ledger, self.check(value))
        item = package['candidate_inventory']['items'][0]
        self.assertEqual(item['selection_outcome'], 'unresolved_secondary')
        self.assertEqual(item['scientific_status'], 'unresolved')
        self.assertEqual(item['presentation'], 'supporting')
        self.assertEqual(item['component_treatments'], ['deferred', 'deferred'])
        self.assertEqual(package['findings'], [])
        self.assertNotEqual(item['selection_outcome'], 'supported_secondary')
        self.assertNotEqual(item['selection_outcome'], 'selected')

    def test_selected_candidate_is_distinct_from_supported_secondary(self):
        package = revision_package(self.value, self.ledger, self.check(self.value))
        item = package['candidate_inventory']['items'][0]
        self.assertEqual(item['selection_outcome'], 'selected')
        self.assertEqual(package['candidate_inventory']['secondary_candidates'], [])

    def test_unknown_issue_and_unsupported_new_finding_rejected(self):
        bad = copy.deepcopy(self.value)
        bad['decisions'][0]['issue_id']='invented'
        with self.assertRaises(ValueError):self.check(bad)
        bad = copy.deepcopy(self.value)
        bad['findings'].append(copy.deepcopy(self.finding))
        with self.assertRaisesRegex(ValueError,'no preserved'):self.check(bad)

    def test_the_domain_profile_is_the_only_accepted_profile(self):
        server,tools=tool_profile({'tool_profile':'domains-v1'})
        self.assertEqual(server,'manuscript_tools.py')
        self.assertEqual(tools[:len(APPROVED_MANUSCRIPT_TOOLS)],APPROVED_MANUSCRIPT_TOOLS)
        self.assertEqual(tool_profile({}),(server,tools))
        with self.assertRaises(RunBlocked):tool_profile({'tool_profile':'/tmp/arbitrary.py'})
        with self.assertRaises(RunBlocked):tool_profile({'tool_profile':'debate-v2'})


if __name__ == '__main__':
    unittest.main()
