import copy
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace
from peerreviewagents.engine.focused_investigation import packet, investigate, hydrate_memo_evidence
from peerreviewagents.engine.evidence_handles import build_registry
from peerreviewagents.engine.supplement_view import supplement_page
from peerreviewagents.engine.storage import write, digest
from peerreviewagents.engine.investigation_policy import evidence_operation


class FocusedInvestigationTests(unittest.TestCase):
    def paper(self):
        return {'id':'paper', 'title':'Example', 'sections':[], 'assets':[],
                'blocks':[{'id':'b1','text':'Natal dispersal was measured.'},
                          {'id':'b2','text':'A defense appears here.'}]}

    def test_packet_never_silently_truncates_or_accepts_unknown_ids(self):
        p=self.paper()
        self.assertEqual(packet(p,['b1'])['blocks'][0]['text'],p['blocks'][0]['text'])
        for ids in ([],['b3'],['b1','b1']):
            with self.assertRaises(ValueError):packet(p,ids)
        with self.assertRaises(ValueError):packet(p,['b1'],max_words=2)

    def test_reread_is_distinguished_from_added_context_and_visual_check(self):
        row={'tool':'read_blocks','result':{'blocks':[{'id':'b2'}]}}
        self.assertEqual(evidence_operation(row,{'b1'}),'expanded_manuscript_context')
        self.assertEqual(evidence_operation(row,{'b1','b2'}),'manuscript_reread')
        self.assertEqual(evidence_operation({'tool':'inspect_results'}),'reorganized_manuscript')
        self.assertEqual(evidence_operation({'tool':'view_supplement'}),'visual_inspection')

    def test_invalid_quote_quarantines_memo_and_existing_run_never_retries(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            paper=self.paper()
            write(root/'paper.json',paper)
            write(root/'manifest.json',{'files':{'paper.json':digest((root/'paper.json').read_bytes())}})
            write(root/'billing.json',{'checked_at':time.time(),'no_paid_credits_verified':True})
            response={'answer':'Narrow the claim.','counterevidence':'Defense.','status':'narrowed',
                      'limitations':[],'investigation_summary':'Inspected a source.',
                      'manuscript_handles':['invented-handle'],
                      'tool_checks':[{'question':'Does it hold?','call_index':1,'tool':'read_blocks',
                                      'outcome':'narrowed','interpretation':'Source changes the claim.'}]}
            def call(*args,**kwargs):
                log=root/'out/investigation/tool-log.jsonl'
                log.parent.mkdir()
                import json
                log.write_text(json.dumps({'tool':'read_blocks','result':{'blocks':paper['blocks'],
                    'tool_receipt':{'call_index':1,'tool':'read_blocks'}}})+'\n')
                return copy.deepcopy(response)
            with patch('peerreviewagents.engine.focused_investigation.require_subscription'), patch('peerreviewagents.engine.focused_investigation.model_call',side_effect=call) as model:
                result=investigate(root,root/'out',root/'billing.json','2026-09-25','Does the source support the claim?',['b1'])
                self.assertFalse(result['ready_for_adjudication'])
                self.assertTrue(result['tool_policy_audit']['complete'])
                with self.assertRaises(FileExistsError):
                    investigate(root,root/'out',root/'billing.json','2026-09-25','Does the source support the claim?',['b1'])
                self.assertEqual(model.call_count,1)

    def test_claude_model_requires_its_own_billing_record_and_provider(self):
        from peerreviewagents.engine.billing import claude_billing_record
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            write(root/'paper.json',self.paper())
            write(root/'manifest.json',{'files':{'paper.json':digest((root/'paper.json').read_bytes())}})
            write(root/'codex.json',{'checked_at':time.time(),'no_paid_credits_verified':True})
            write(root/'claude.json',claude_billing_record({'loggedIn':True,'authMethod':'claude.ai'}))
            question='Does the source support the claim?'
            with patch('peerreviewagents.engine.focused_investigation.require_subscription'), \
                    patch('peerreviewagents.engine.focused_investigation.model_call',side_effect=RuntimeError('stop')) as model:
                with self.assertRaises(ValueError):
                    investigate(root,root/'first',root/'codex.json','2026-09-25',question,['b1'],model='claude-opus-5-5')
                self.assertFalse((root/'first').exists())
                with self.assertRaises(ValueError):
                    investigate(root,root/'second',root/'claude.json','2026-09-25',question,['b1'],model='claude-fable-5-1')
                with self.assertRaisesRegex(RuntimeError,'stop'):
                    investigate(root,root/'third',root/'claude.json','2026-09-25',question,['b1'],model='claude-opus-5-5')
                self.assertEqual(model.call_args.args[0]['provider'],'claude-code')

    def test_handles_require_exposure_and_preserve_exact_text(self):
        paper=self.paper()
        h=next(h for h in build_registry(paper).handles if h.source_id=='b2')
        response={'manuscript_handles':[h.handle_id]}
        value,audit=hydrate_memo_evidence(response,paper,{'b1'},[])
        self.assertFalse(audit[0]['valid'])
        row={'result':{'manuscript_handles':[h.model_record()]}}
        value,audit=hydrate_memo_evidence(response,paper,{'b1'},[row])
        self.assertTrue(audit[0]['valid'])
        self.assertEqual(value['manuscript_evidence'][0]['quote'],h.text)
        row['result']['manuscript_handles'][0]['text']='Changed source'
        self.assertFalse(hydrate_memo_evidence(response,paper,{'b1'},[row])[1][0]['valid'])

    def test_pdf_page_is_bounded_and_source_verified(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            (root/'table.pdf').write_bytes(b'pdf fixture')
            paper={'assets':[{'id':'s1','label':'S1','path':'table.pdf'}]}
            write(root/'manifest.json',{'files':{'table.pdf':digest(b'pdf fixture')}})
            def run(command,**kwargs):
                if command[0]=='pdfinfo':return SimpleNamespace(stdout='Pages: 2\n')
                Path(command[-1]+'.png').write_bytes(b'png fixture')
                return SimpleNamespace()
            with patch('peerreviewagents.engine.supplement_view.subprocess.run',side_effect=run):
                image,meta=supplement_page(root,paper,'s1',root/'out',2)
                self.assertEqual(image.read_bytes(),b'png fixture')
                self.assertEqual(meta['page'],2)
                self.assertEqual(meta['pages'],2)
                for page in (0,True,3,101):
                    with self.assertRaises(ValueError):supplement_page(root,paper,'s1',root/'out',page)
                (root/'table.pdf').write_bytes(b'changed')
                with self.assertRaises(ValueError):supplement_page(root,paper,'s1',root/'out')
                paper['assets'][0]['path']='../escape.pdf'
                with self.assertRaises(ValueError):supplement_page(root,paper,'s1',root/'out')

    def test_tool_audit_survives_debate_ledger(self):
        from peerreviewagents.engine.debate_revision import issue_ledger
        review={'findings':[],'tool_checks':[{'question':'Check a claim.'}],
                'tool_policy_audit':{'complete':False}}
        ledger=issue_ledger(review)
        self.assertEqual(ledger['investigation_context']['tool_checks'],review['tool_checks'])
        self.assertFalse(ledger['investigation_context']['tool_policy_audit']['complete'])

    def test_debate_handoff_recomputes_audit_and_rejects_changes(self):
        import json
        from peerreviewagents.engine.focused_investigation import load_for_debate
        from peerreviewagents.engine.investigation_policy import audit_tool_checks
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            paper=self.paper()
            write(root/'paper.json',paper)
            write(root/'manifest.json',{'files':{'paper.json':digest((root/'paper.json').read_bytes())}})
            sha=digest((root/'manifest.json').read_bytes())
            run=root/'run'
            selected=packet(paper,['b1'])
            handle=next(h for h in build_registry(paper).handles if h.source_id=='b1')
            raw={'manuscript_handles':[handle.handle_id], 'tool_checks':[{'question':'Check source',
                 'call_index':1,'tool':'read_blocks','outcome':'narrowed','interpretation':'Narrowed.'}]}
            row={'tool':'read_blocks','arguments':{'ids':['b1']},'result':{'blocks':[paper['blocks'][0]],
                 'tool_receipt':{'call_index':1,'tool':'read_blocks'}}}
            hydrated,quotes=hydrate_memo_evidence(raw,paper,{'b1'},[row])
            memo={'version':'focused-investigation-v2','paper_id':paper['id'],'source_manifest_sha256':sha,
                  'question':'Check source','response':hydrated,'quote_audit':quotes,
                  'tool_policy_audit':audit_tool_checks(raw,[row],{'b1'})}
            write(run/'memo.json',memo)
            write(run/'response.json',raw)
            write(run/'packet.json',selected)
            write(run/'manifest.json',{'question':'Check source','source_manifest_sha256':sha,
                  'packet_sha256':digest(json.dumps(selected,sort_keys=True))})
            (run/'investigation').mkdir()
            (run/'investigation/tool-log.jsonl').write_text(json.dumps(row)+'\n')
            result,images=load_for_debate(root,run)
            self.assertTrue(result['ready_for_adjudication'])
            self.assertFalse(result['scientifically_verified'])
            self.assertEqual(images,[])
            memo['response']['manuscript_evidence'][0]['quote']='Invented source'
            write(run/'memo.json',memo)
            with self.assertRaisesRegex(ValueError,'recorded source audit'):load_for_debate(root,run)
