"""Public integration boundary, with no provider execution."""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from peerreviewagents.engine import (ReviewRequest, capabilities, load_manuscript,
                                 evidence_registry, validate_review, run_review,
                                 resolved_review_config)
from peerreviewagents.engine.storage import write, digest, read


class CoreAPI(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.workspace=self.root/'workspace'
        self.paper={'id':'example','source_sha256':'source','blocks':[{
            'id':'b1','text':'The study included twelve samples.',
            'sha256':digest('The study included twelve samples.')}]}
        write(self.workspace/'paper.json',self.paper)
        write(self.workspace/'manifest.json',{'files':{
            'paper.json':digest((self.workspace/'paper.json').read_bytes())}})

    def request(self,**overrides):
        return ReviewRequest(**{'workspace':self.workspace,'output':self.root/'run',
            'literature_cutoff':'2026-09-25',**overrides})

    def test_import_does_not_start_or_import_provider_or_web_server(self):
        code='import sys, peerreviewagents.engine; assert "peerreviewagents.engine.server" not in sys.modules; assert "peerreviewagents.engine.model_runtime" not in sys.modules'
        subprocess.run([sys.executable,'-c',code],check=True,capture_output=True)
        self.assertEqual(set(capabilities()['models']), {'gpt-5.6-terra', 'gpt-5.6-sol', 'gpt-6-astra', 'gpt-5.6-luna', 'gpt-5.5',
                                                         'claude-opus-5-5', 'claude-sonnet-5-5'})
        self.assertEqual(capabilities()['models_by_provider']['claude-code'], ['claude-opus-5-5', 'claude-sonnet-5-5'])

    def test_offline_identity_evidence_and_review_validation(self):
        self.assertEqual(load_manuscript(self.workspace)['id'],'example')
        self.assertTrue(evidence_registry(self.workspace).handles)
        normalized=validate_review(self.workspace,{'findings':[{
            'title':'Clarify sample unit','block_id':'b1','quote':'twelve samples',
            'comment':'Clarify the sample unit.','kind':'question','replacement':'',
            'evidence_blocks':['b1']}]})
        self.assertEqual(normalized['annotation_counts']['anchored'],1)
        (self.workspace/'paper.json').write_text('{}')
        with self.assertRaises(ValueError):load_manuscript(self.workspace)

    def test_unsupported_conditions_fail_before_execution(self):
        for args in ({'model':'arbitrary-model'},{'workflow':'tool-free'},{'reviewers':True},
                     {'token_budget':0},{'literature_cutoff':'tomorrow'},{'output':self.workspace}):
            with self.assertRaises(ValueError):self.request(**args)

    def test_one_explicit_run_reuses_engine_and_exposes_artifact(self):
        request=self.request()
        def fake_engine(workspace,output,*args,**kwargs):
            write(output/'editor-package.json',{'summary':'Example','findings':[]})
        with patch('peerreviewagents.engine.reviewer.review',side_effect=fake_engine) as engine:
            result=run_review(request)
            self.assertEqual(engine.call_count,1)
            self.assertEqual(engine.call_args.kwargs['mode'],'ensemble')
            self.assertEqual(engine.call_args.kwargs['reviewers'],2)
            self.assertEqual(result['artifact_version'],'paper-workbench-run-v1')
            self.assertEqual(read(request.output/'core-artifact.json'),result)
            with self.assertRaises(FileExistsError):run_review(request)
            self.assertEqual(engine.call_count,1)

    def test_a_live_review_request_carries_no_frozen_evidence(self):
        """Live retrieval bounded by the cutoff is the only review evidence path."""
        request = self.request()
        config = resolved_review_config(request)
        self.assertEqual(config['evidence_policy'], 'live')
        self.assertNotIn('evidence_mode', config)
        self.assertNotIn('source_packet_manifest', config)
        for unsupported in ('evidence_mode', 'source_packet', 'historical_snapshots', 'scaffold_root'):
            with self.assertRaises(TypeError):
                self.request(**{unsupported: 'anything'})


if __name__=='__main__':unittest.main()
