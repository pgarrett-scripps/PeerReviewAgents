"""Durable debate lineage and bounded external evidence checks."""
import copy
import unittest

from peerreviewagents.engine.annotations import normalize_review
from peerreviewagents.engine.debate_revision import (
    issue_ledger, revision_package, revision_prompt, validate_revision,
)
from peerreviewagents.engine.storage import digest


class DomainDebateTests(unittest.TestCase):
    def setUp(self):
        text = 'The assay used three independent cultures. The mechanism remains uncertain.'
        self.paper = {'id':'paper','title':'Mechanism study','source_sha256':'source',
            'blocks':[{'id':'b1','text':text,'sha256':digest(text)}]}
        finding = {'title':'Mechanism','block_id':'b1','quote':'The mechanism remains uncertain.',
            'kind':'question','severity':'moderate','comment':'Please distinguish the observed effect from the proposed mechanism.',
            'replacement':'','evidence_blocks':['b1'],'source_urls':[],'counterevidence':''}
        self.first = {'summary':'One concern.','findings':[finding], 'limitations':['Source access was bounded.'],
            'discarded_hypotheses':['The replicate count may be missing.'],
            'source_findings':[{'source_finding_id':'upstream-1','finding':finding,
                'components':[{'component_id':'upstream-1-part-1','text':finding['comment']}]}],
            'source_hypotheses':[{'source_finding_id':'upstream-h1','text':'The replicate count may be missing.',
                'components':[{'component_id':'upstream-h1-part-1','text':'The replicate count may be missing.'}]}],
            'component_decisions':[
                {'issue_id':'upstream-1','scientific_status':'unresolved','presentation':'primary',
                 'strongest_criticism':'Mechanism exceeds observation.','strongest_author_defense':'Wording is cautious.',
                 'decisive_question':'Is the mechanism directly tested?','decision_reason':'The distinction remains actionable.',
                 'evidence':[{'block_id':'b1','quote':'The mechanism remains uncertain.'}],
                 'components':[{'component_id':'upstream-1-part-1','treatment':'deferred',
                     'final_finding_number':0,'final_quote':'','reason':'Needs focused review.','evidence':[]}]},
                {'issue_id':'upstream-h1','scientific_status':'contradicted','presentation':'omitted',
                 'strongest_criticism':'Replicates may be absent.','strongest_author_defense':'The manuscript states three cultures.',
                 'decisive_question':'Is the count reported?','decision_reason':'The premise is defeated.',
                 'evidence':[{'block_id':'b1','quote':'three independent cultures'}],
                 'components':[{'component_id':'upstream-h1-part-1','treatment':'resolved',
                     'final_finding_number':0,'final_quote':'','reason':'The count is explicit.',
                     'evidence':[{'block_id':'b1','quote':'three independent cultures'}]}]}
            ],
            'investigation_context':{'source_findings':[{'should_not':'nest'}],
                'source_cards':[{'evidence_id':'source-old'}], 'investigation_summary':'Checked replicates.'}}

    def _next_value(self, ledger):
        issue = ledger['issues'][0]
        finding = copy.deepcopy(issue['original_finding'])
        decision = {'issue_id':issue['issue_id'],'scientific_status':'unresolved','presentation':'primary',
            'strongest_criticism':'The mechanism may exceed the observation.',
            'strongest_author_defense':'The text acknowledges uncertainty.',
            'decisive_question':'What directly tests the mechanism?',
            'decision_reason':'A scoped clarification remains useful.','evidence':[],
            'components':[{'component_id':issue['components'][0]['component_id'],'treatment':'preserved',
                'final_finding_number':1,'final_quote':finding['comment'],
                'reason':'The question remains actionable.','evidence':[]}]}
        resolved = ledger['issues'][1]
        defense = {'block_id':'b1','quote':'three independent cultures'}
        decision2 = {'issue_id':resolved['issue_id'],'scientific_status':'contradicted','presentation':'omitted',
            'strongest_criticism':'Replicates may be absent.',
            'strongest_author_defense':'The count is explicit.',
            'decisive_question':'Is the count reported?','decision_reason':'The allegation remains defeated.',
            'evidence':[defense], 'components':[{'component_id':resolved['components'][0]['component_id'],
                'treatment':'resolved','final_finding_number':0,'final_quote':'',
                'reason':'The author defense remains valid.','evidence':[defense]}]}
        return {'summary':'Scoped review.','findings':[finding],'limitations':[],
                'decisions':[decision,decision2]}

    def test_roundtrip_preserves_lineage_without_recursive_growth(self):
        normalized = normalize_review(self.paper,self.first,'first')
        ledger = issue_ledger(normalized)
        self.assertEqual([i['issue_id'] for i in ledger['issues']],['upstream-1','upstream-h1'])
        self.assertEqual(ledger['issues'][0]['prior_decision']['scientific_status'],'unresolved')
        self.assertEqual(ledger['issues'][1]['prior_decision']['scientific_status'],'contradicted')
        value = self._next_value(ledger)
        audit = validate_revision(value,ledger,self.paper)
        package = revision_package(value,ledger,audit)
        second = normalize_review(self.paper,package,'second')
        next_ledger = issue_ledger(second)
        next_package = revision_package(value,next_ledger,validate_revision(value,next_ledger,self.paper))
        self.assertEqual(next_package['source_findings'],package['source_findings'])
        self.assertEqual(next_package['source_hypotheses'],package['source_hypotheses'])
        self.assertEqual(next_package['candidate_inventory'],package['candidate_inventory'])
        self.assertEqual(len(next_package['candidate_inventory']['items']),2)
        self.assertEqual(len({item['issue_id'] for item in
                              next_package['candidate_inventory']['items']}),2)
        self.assertEqual(next_package['investigation_context'],self.first['investigation_context'])
        self.assertNotIn('investigation_context',next_package['investigation_context'])
        self.assertEqual(len(next_ledger['issues']),2)

    def test_previous_package_appends_only_genuinely_new_material(self):
        extra = {'title':'Dose context','block_id':'b1','quote':'The assay used three independent cultures.',
            'kind':'question','severity':'moderate','comment':'Please report the treatment dose and duration.',
            'replacement':'','evidence_blocks':['b1'],'source_urls':[],'counterevidence':''}
        revised = copy.deepcopy(self.first)
        revised['findings'].append(extra)
        revised['discarded_hypotheses'].append('The treatment timing may differ across groups.')
        ledger = issue_ledger(normalize_review(self.paper,revised,'expanded'))
        new_findings = [i for i in ledger['issues'] if i.get('source_kind') == 'new_current_finding']
        hypotheses = [i for i in ledger['issues'] if i.get('source_kind') == 'discarded_hypothesis']
        self.assertEqual(len(new_findings),1)
        self.assertEqual(new_findings[0]['original_finding']['comment'],extra['comment'])
        self.assertEqual(len(hypotheses),2)
        self.assertEqual(hypotheses[-1]['original_hypothesis'],'The treatment timing may differ across groups.')
        first_ids = [i['issue_id'] for i in ledger['issues']]
        package = revision_package({'summary':'Expanded.','findings':[], 'limitations':[],
            'decisions':[]},ledger,{'limitation':'test'})
        repeated = issue_ledger(normalize_review(self.paper,package,'expanded-again'))
        self.assertEqual([i['issue_id'] for i in repeated['issues']],first_ids)

    def test_legacy_source_wrappers_without_components_are_preserved(self):
        bare = copy.deepcopy(self.first['source_findings'][0]['finding'])
        payload = {'findings':[bare], 'source_findings':[bare],
            'source_hypotheses':['A legacy discarded note.']}
        ledger = issue_ledger(payload)
        self.assertEqual(len(ledger['issues']),2)
        self.assertEqual(ledger['issues'][0]['original_finding']['comment'],bare['comment'])
        self.assertTrue(ledger['issues'][0]['components'])
        self.assertEqual(ledger['issues'][1]['original_hypothesis'],'A legacy discarded note.')
        self.assertTrue(ledger['issues'][1]['components'])

    def test_source_card_can_support_decision_and_component(self):
        review = {'findings':[self.first['source_findings'][0]['finding']]}
        ledger = issue_ledger(review)
        card = {'evidence_id':'source-card-1','eligible':True,
                'evidence_text':'record_status: reviewed\nconclusion: mechanism not established'}
        value = self._next_value({'issues':[ledger['issues'][0],
            {'issue_id':'unused','source_kind':'discarded_hypothesis','original_finding':None,
             'original_hypothesis':'unused','components':[{'component_id':'unused-part-1','text':'unused'}]}]})
        value['decisions'] = value['decisions'][:1]
        ref = {'block_id':'source-card-1','quote':'conclusion: mechanism not established'}
        value['decisions'][0].update(scientific_status='supported_clarification',evidence=[ref])
        value['decisions'][0]['components'][0]['evidence'] = [ref]
        self.assertTrue(validate_revision(value,ledger,self.paper,[card])['structural_validity'])
        bad = copy.deepcopy(card)
        bad['eligible'] = False
        with self.assertRaisesRegex(ValueError,'source quotation'):
            validate_revision(value,ledger,self.paper,[bad])
        value['decisions'][0]['evidence'][0]['quote'] = 'mechanism established'
        with self.assertRaisesRegex(ValueError,'source quotation'):
            validate_revision(value,ledger,self.paper,[card])

    def test_source_card_cannot_replace_manuscript_finding_anchor(self):
        ledger = issue_ledger({'findings':[self.first['source_findings'][0]['finding']]})
        value = self._next_value({'issues':[ledger['issues'][0],
            {'issue_id':'unused','source_kind':'discarded_hypothesis','original_finding':None,
             'original_hypothesis':'unused','components':[{'component_id':'unused-part-1','text':'unused'}]}]})
        value['decisions'] = value['decisions'][:1]
        card = {'evidence_id':'source-card-1','eligible':True,'evidence_text':'external exact quote'}
        value['findings'][0].update(block_id='source-card-1',quote='external exact quote')
        with self.assertRaisesRegex(ValueError,'absent or ambiguous'):
            validate_revision(value,ledger,self.paper,[card])
        value['findings'][0] = copy.deepcopy(self.first['source_findings'][0]['finding'])
        value['findings'][0]['evidence_blocks'] = ['source-card-1']
        with self.assertRaisesRegex(ValueError,'Unknown finding evidence block'):
            validate_revision(value,ledger,self.paper,[card])

    def test_prompt_preserves_legacy_boundary_and_supplies_cards(self):
        ledger = issue_ledger({'findings':[self.first['source_findings'][0]['finding']]})
        legacy = revision_prompt(self.paper,'manuscript',ledger)
        self.assertIn('No literature, shell, code reruns, or author data are available.',legacy)
        card = {'evidence_id':'source-card-1','eligible':True,'evidence_text':'exact evidence'}
        enabled = revision_prompt(self.paper,'manuscript',ledger,[card])
        self.assertIn('SOURCE CARDS:',enabled)
        self.assertIn('source-card-1',enabled)
        self.assertIn('available bounded source tools may be used',enabled)
        self.assertIn('using its evidence_id as block_id and quoting evidence_text exactly',enabled)
        self.assertNotIn('No literature, shell',enabled)


if __name__ == '__main__':
    unittest.main()
