"""A scoped concern cannot disappear between investigation and debate."""
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from peerreviewagents.engine import reviewer
from peerreviewagents.engine.annotations import normalize_review, export_draft
from peerreviewagents.engine.debate_revision import issue_ledger, revision_prompt, validate_revision, revision_package
from peerreviewagents.engine.discovery import AREAS
from peerreviewagents.engine.evidence_handles import build_registry
from peerreviewagents.engine.storage import digest, read, write


class HypothesisHandoffTests(unittest.TestCase):
    def setUp(self):
        text = 'The same parents completed protocol A first, then protocol B.'
        self.note = 'Collection order limits comparisons between protocols. Within-protocol diet contrasts may remain valid.'
        self.paper = {'id':'paper','title':'Feeding protocols','source_sha256':'source',
            'blocks':[{'id':'b1','text':text,'sha256':digest(text)}],
            'statistics':{'total_words':10}, 'assets':[], 'references':[], 'sections':{}}
        self.first = {'summary':'No selected findings.','findings':[], 'limitations':[],
            'discarded_hypotheses':[self.note], 'investigation_summary':'Compared feeding protocols.'}
        self.ledger = issue_ledger(self.first)
        comment = 'Could collection order influence comparisons between protocols? Please qualify those comparisons while retaining valid within-protocol contrasts.'
        finding = {'title':'Collection order','block_id':'b1','quote':text,
            'kind':'question','severity':'moderate','comment':comment,'replacement':'',
            'evidence_blocks':['b1'],'source_urls':[],'counterevidence':'Within-protocol contrasts may remain valid.'}
        evidence = [{'block_id':'b1','quote':text}]
        decision = {'issue_id':'hypothesis-1','scientific_status':'supported_clarification',
            'presentation':'primary','strongest_criticism':'Collection order may affect cross-protocol differences.',
            'strongest_author_defense':'Within-protocol contrasts can remain interpretable.',
            'decisive_question':'Is collection order separable from protocol?',
            'decision_reason':'The narrower question remains useful.', 'evidence':evidence,
            'components':[{'component_id':c['component_id'],'treatment':'preserved',
                'final_finding_number':1,'final_quote':comment,
                'reason':'The scoped question preserves this qualification.','evidence':[]}
                for c in self.ledger['issues'][0]['components']]}
        self.final = {'summary':'One restored question.','findings':[finding],
                      'limitations':[], 'decisions':[decision]}

    def test_discarded_note_is_delivered_without_fabricated_anchor(self):
        prompt = revision_prompt(self.paper,self.paper['blocks'][0]['text'],self.ledger)
        self.assertIn(self.note,prompt)
        self.assertIsNone(self.ledger['issues'][0]['original_finding'])
        self.assertEqual(self.ledger['issues'][0]['source_kind'],'discarded_hypothesis')
        self.first['discarded_hypotheses'][0] = 'Changed after handoff.'
        self.assertEqual(self.ledger['issues'][0]['original_hypothesis'],self.note)

    def test_normalized_review_keeps_exclusions_and_investigation_context(self):
        normalized = normalize_review(self.paper,self.first,'first')
        ledger = issue_ledger(normalized)
        self.assertEqual(ledger['issues'][0]['original_hypothesis'],self.note)
        self.assertEqual(ledger['investigation_context']['investigation_summary'],self.first['investigation_summary'])
        with self.assertRaises(ValueError):
            issue_ledger({**self.first,'discarded_hypotheses':{'unexpected':'shape'}})

    def test_omitted_or_duplicate_hypothesis_decision_fails(self):
        for decisions in ([], self.final['decisions']*2):
            with self.assertRaisesRegex(ValueError,'Every issue'):
                validate_revision({**self.final,'decisions':decisions},self.ledger,self.paper)

    def test_restoration_needs_source_evidence_and_a_real_anchor(self):
        audit = validate_revision(self.final,self.ledger,self.paper)
        self.assertEqual(audit['restored_hypotheses'],1)
        bad = copy.deepcopy(self.final)
        bad['findings'][0]['quote'] = self.note
        with self.assertRaisesRegex(ValueError,'absent or ambiguous'):
            validate_revision(bad,self.ledger,self.paper)
        bad = copy.deepcopy(self.final)
        bad['decisions'][0].update(scientific_status='unresolved',evidence=[])
        with self.assertRaisesRegex(ValueError,'Restored hypothesis requires'):
            validate_revision(bad,self.ledger,self.paper)

    def test_deferred_hypothesis_survives_review_and_export(self):
        value = copy.deepcopy(self.final)
        value['findings'] = []
        d = value['decisions'][0]
        d.update(scientific_status='unresolved',presentation='supporting')
        for c in d['components']:
            c.update(treatment='deferred',final_finding_number=0,final_quote='',reason='Needs further evidence.')
        audit = validate_revision(value,self.ledger,self.paper)
        packaged = revision_package(value,self.ledger,audit)
        normalized = normalize_review(self.paper,packaged,'debate')
        exported = export_draft(self.paper,normalized,{'paper_id':'paper','items':{}})
        self.assertEqual(exported['workflow_version'],'debate-v3')
        self.assertEqual(exported['source_hypotheses'][0]['text'],self.note)
        self.assertEqual(len(exported['source_hypotheses'][0]['components']),2)
        self.assertEqual(exported['component_decisions'][0]['scientific_status'],'unresolved')
        self.assertEqual(exported['findings'],[])
        d['scientific_status'] = 'contradicted'
        with self.assertRaisesRegex(ValueError,'Rejected hypothesis must resolve'):
            validate_revision(value,self.ledger,self.paper)

    def test_rejection_needs_evidence_for_each_component(self):
        value = copy.deepcopy(self.final)
        value['findings'] = []
        decision = value['decisions'][0]
        decision.update(scientific_status='contradicted',presentation='omitted')
        for c in decision['components']:
            c.update(treatment='resolved',final_finding_number=0,final_quote='',evidence=[])
        with self.assertRaisesRegex(ValueError,'Resolved component lacks'):
            validate_revision(value,self.ledger,self.paper)

    def test_panel_passes_note_to_debate_and_stops_on_silent_loss(self):
        handle = build_registry(self.paper).handles[0].handle_id
        first_model = copy.deepcopy(self.first)
        first_model['coverage_checks'] = [
            {'area': area, 'status': 'examined', 'reason': 'Checked protocol order.',
             'evidence': [{'evidence_handle': handle}]}
            for area in AREAS
        ]
        final_model = copy.deepcopy(self.final)
        finding = final_model['findings'][0]
        finding.pop('block_id')
        finding.pop('quote')
        finding.pop('evidence_blocks')
        finding.update(anchor_handle=handle, manuscript_evidence_handles=[handle],
                       source_evidence=[])
        final_model['decisions'][0]['evidence'] = [{'evidence_handle': handle}]
        for lose_note in (False,True):
            with self.subTest(lose_note=lose_note), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                work, out = root/'paper', root/'run'
                write(work/'paper.json',self.paper)
                (work/'manuscript.md').write_text('[b1] '+self.paper['blocks'][0]['text'])
                write(work/'manifest.json',{'files':{name:digest((work/name).read_bytes())
                    for name in ('paper.json','manuscript.md')}})
                debate_model = {**final_model,'decisions':[]} if lose_note else final_model
                with patch.object(reviewer,'require_subscription'), patch.object(reviewer,'inspected_images',return_value=[]), patch.object(reviewer,'model_call',side_effect=[first_model,debate_model]) as invoke:
                    if lose_note:
                        with self.assertRaisesRegex(ValueError,'Every issue'):
                            reviewer.review(work,out,'2023-01-01')
                        self.assertFalse((out/'editor-package.json').exists())
                        self.assertEqual(read(out/'progress.json')['status'],'blocked')
                    else:
                        reviewer.review(work,out,'2023-01-01')
                        self.assertEqual(read(out/'editor-package.json')['source_hypotheses'][0]['text'],self.note)
                    self.assertEqual(invoke.call_count,2)
                    self.assertIn(self.note,invoke.call_args_list[1].args[2])
                    self.assertEqual(read(out/'debate-ledger.json')['issues'][0]['original_hypothesis'],self.note)
                    self.assertEqual(read(out/'responses/workspace-model.json'),first_model)
                    self.assertEqual(read(out/'responses/debate-model.json'),debate_model)


if __name__ == '__main__':
    unittest.main()
