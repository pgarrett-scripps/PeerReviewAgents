"""Regression checks for conservative review and image handoff."""
import json
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from peerreviewagents.engine.reviewer import editor_package,inspected_images
from peerreviewagents.engine.storage import digest,write


def finding(title,comment):
    return {'title':title,'block_id':'b1','quote':'shared passage','kind':'question',
            'severity':'major','comment':comment,'replacement':'','evidence_blocks':['b1'],
            'counterevidence':'','source_urls':[]}


class ReviewHandoff(unittest.TestCase):
    def test_distinct_same_anchor_findings_follow_explicit_dispositions(self):
        causal=finding('Causal interpretation','The design does not establish direction.')
        reporting=finding('Missing denominator','The passage does not report the denominator.')
        final_finding={**causal,'comment':'The causal wording should be narrowed.'}
        final={'summary':'One endorsed concern.','findings':[final_finding],
               'source_dispositions':[
                   {'source_finding_id':'r1-f1','disposition':'retained',
                    'final_finding_numbers':[1],'reason':'Retained after checking the design.',
                    'evidence_blocks':['b1']},
                   {'source_finding_id':'r1-f2','disposition':'needs_verification',
                    'final_finding_numbers':[],'reason':'The denominator could not be located.',
                    'evidence_blocks':['b1']}],
               'limitations':[],'investigation_summary':'','discarded_hypotheses':[]}
        package=editor_package(final,[{'findings':[causal,reporting]}])
        self.assertEqual(package['editor_findings_count'],1)
        self.assertEqual(package['verification_queue_count'],1)
        self.assertEqual(len(package['source_findings']),2)
        queued=package['findings'][1]
        self.assertEqual(queued['source_finding_id'],'r1-f2')
        self.assertFalse(queued['endorsed'])
        self.assertEqual(queued['disposition'],'needs_verification')

    def test_missing_disposition_stays_unendorsed(self):
        first=finding('First concern','First concern text.')
        second=finding('Second concern','Second concern text.')
        final={'summary':'Final.','findings':[],
               'source_dispositions':[{'source_finding_id':'r1-f1','disposition':'rejected',
                                       'final_finding_numbers':[],'reason':'Contradicted by b1.',
                                       'evidence_blocks':['b1']}]}
        package=editor_package(final,[{'findings':[first,second]}])
        self.assertEqual(package['verification_queue_count'],1)
        self.assertEqual(package['findings'][0]['source_finding_id'],'r1-f2')
        self.assertIn('did not provide a valid disposition',package['findings'][0]['comment'])

    def test_duplicate_or_invalid_dispositions_cannot_suppress_a_finding(self):
        source=finding('Concern','Concern text.')
        valid={'source_finding_id':'r1-f1','disposition':'rejected',
               'final_finding_numbers':[],'reason':'The source contradicts the concern.',
               'evidence_blocks':['b1']}
        invalid={**valid,'reason':''}
        for dispositions in ([valid,valid],[invalid,valid],[valid,invalid]):
            with self.subTest(dispositions=dispositions):
                package=editor_package({'summary':'Final.','findings':[],
                                        'source_dispositions':dispositions},
                                       [{'findings':[source]}])
                self.assertEqual(package['verification_queue_count'],1)
                self.assertFalse(package['findings'][0]['endorsed'])
                self.assertEqual(package['source_disposition_validation'][0]['status'],'invalid')

    def test_legacy_handoff_does_not_equate_a_shared_anchor(self):
        first=finding('First concern','First concern text.')
        second=finding('Second concern','Second concern text.')
        package=editor_package({'summary':'Final.','findings':[first]},
                               [{'findings':[first,second]}])
        self.assertEqual(package['editor_findings_count'],1)
        self.assertEqual(package['verification_queue_count'],1)
        self.assertEqual(package['findings'][1]['title'],'Needs verification: Second concern')

    def make_workspace(self,root):
        image=Image.new('RGB',(20,20),(10,80,140))
        original=root/'original.pdf'
        image.save(original,'PDF')
        preview=root/'supplements/figure.png'
        preview.parent.mkdir()
        image.save(preview,'PNG')
        text='shared passage'
        paper={'id':'paper','source_sha256':digest(original.read_bytes()),
               'blocks':[{'id':'b1','text':text,'sha256':digest(text)}],
               'assets':[{'id':'figure','label':'Original figure.tiff','kind':'supplement',
                          'path':'supplements/figure.tiff','preview':'supplements/figure.png',
                          'available':'file'}],
               'statistics':{'pdf_pages':1}}
        write(root/'paper.json',paper)
        files={str(path.relative_to(root)):digest(path.read_bytes())
               for path in root.rglob('*') if path.is_file()}
        write(root/'manifest.json',{'paper_id':'paper','files':files})
        return paper,preview

    def test_supplement_handoff_is_bound_to_verified_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            paper,preview=self.make_workspace(root)
            investigation=root/'investigation'
            investigation.mkdir()
            row={'tool':'view_supplement','arguments':{'id':'figure'},
                 'result':{'label':'Untrusted log label'}}
            (investigation/'tool-log.jsonl').write_text(json.dumps(row)+'\n')
            images=inspected_images(paper,root,investigation)
            self.assertEqual(images,[(preview.resolve(),'Original figure.tiff')])
            preview.write_bytes(b'unrelated image bytes')
            with self.assertRaises((ValueError,OSError)):
                inspected_images(paper,root,investigation)

    def test_missing_or_forged_page_evidence_is_not_handed_off(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            paper,preview=self.make_workspace(root)
            investigation=root/'investigation'
            investigation.mkdir()
            row={'tool':'view_page','arguments':{'page':1},'result':{'label':'Page 1'}}
            (investigation/'tool-log.jsonl').write_text(json.dumps(row)+'\n')
            self.assertEqual(inspected_images(paper,root,investigation),[])
            (investigation/'page_001.png').write_bytes(preview.read_bytes())
            self.assertEqual(inspected_images(paper,root,investigation),[])


if __name__=='__main__':
    unittest.main()
