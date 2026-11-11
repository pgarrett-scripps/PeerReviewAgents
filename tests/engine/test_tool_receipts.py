"""Offline checks for investigation exposure and call receipts."""
import datetime
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from peerreviewagents.engine.investigation import Investigation
from peerreviewagents.engine.storage import digest, write
from peerreviewagents.engine.evidence_handles import build_registry


class ToolReceipts(unittest.TestCase):
    def workspace(self, root):
        paper = {
            'id': 'receipt-paper',
            'source_sha256': 'source',
            'blocks': [{'id': 'b1', 'text': 'A source sentence.',
                        'sha256': digest('A source sentence.')}],
        }
        write(root/'paper.json', paper)
        write(root/'manifest.json', {'files': {
            'paper.json': digest((root/'paper.json').read_bytes()),
        }})
        return paper

    def config(self, root):
        return {'paper': str(root), 'output': str(root/'out'),
                'cache': str(root/'cache'), 'tool_limit': 3}

    def test_calls_receive_ordered_receipts_and_log_failures(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.workspace(root)
            server = Investigation(self.config(root))

            rejected = server.call('search', {'query': ''})
            calculated = server.call('calculate', {'expression': '2 + 2'})

            self.assertEqual(rejected['tool_receipt'],
                             {'call_index': 1, 'tool': 'search'})
            self.assertIn('request_rejected', rejected)
            self.assertEqual(calculated['tool_receipt'],
                             {'call_index': 2, 'tool': 'calculate'})
            logs = [json.loads(line) for line in
                    (root/'out/tool-log.jsonl').read_text().splitlines()]
            self.assertEqual([row['result']['tool_receipt'] for row in logs],
                             [rejected['tool_receipt'], calculated['tool_receipt']])
            self.assertIn('request_rejected', logs[0]['result'])

    def test_mcp_initialize_and_listing_record_exposed_tools(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.workspace(root)
            config = self.config(root)
            write(root/'config.json', config)
            requests = [
                {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
                 'params': {'protocolVersion': '2024-11-05'}},
                {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
            ]
            process = subprocess.run(
                [sys.executable, '-m', 'peerreviewagents.engine.manuscript_tools',
                 str(root/'config.json')],
                input=''.join(json.dumps(request)+'\n' for request in requests),
                capture_output=True, text=True, check=True)

            responses = [json.loads(line) for line in process.stdout.splitlines()]
            exposed = [tool['name'] for tool in responses[1]['result']['tools']]
            audit = [json.loads(line) for line in
                     (root/'out/tool-discovery.jsonl').read_text().splitlines()]
            self.assertEqual([entry['event'] for entry in audit],
                             ['initialize', 'tools/list'])
            self.assertEqual(audit[0]['server'],
                             {'name': 'paper-workspace', 'version': '0.1.0'})
            self.assertEqual(audit[1]['tools'], exposed)
            for entry in audit:
                self.assertIsNotNone(datetime.datetime.fromisoformat(entry['timestamp']))

    def test_focused_reads_supply_exact_source_handles(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            paper=self.workspace(root)
            server=Investigation({**self.config(root),'manuscript_handles':True})
            result=server.call('read_blocks',{'ids':['b1']})
            self.assertEqual(result['manuscript_handles'],[h.model_record() for h in build_registry(paper).handles])

    def test_image_transport_preserves_page_metadata(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            paper=self.workspace(root)
            (root/'preview.png').write_bytes(b'preview fixture')
            paper['assets']=[{'id':'s1','label':'Supplement','preview':'preview.png'}]
            write(root/'paper.json',paper)
            write(root/'manifest.json',{'files':{name:digest((root/name).read_bytes()) for name in ('paper.json','preview.png')}})
            write(root/'config.json',self.config(root))
            request={'jsonrpc':'2.0','id':1,'method':'tools/call',
                     'params':{'name':'view_supplement','arguments':{'id':'s1'}}}
            result=subprocess.run([sys.executable,'-m','peerreviewagents.engine.manuscript_tools',str(root/'config.json')],
                                  input=json.dumps(request)+'\n',text=True,capture_output=True,check=True)
            content=json.loads(result.stdout)['result']['content']
            metadata=json.loads(content[1]['text'])
            self.assertEqual(metadata['asset_id'],'s1')
            self.assertEqual(metadata['page'],1)
            self.assertEqual(metadata['pages'],1)
            self.assertEqual(metadata['source_sha256'],digest(b'preview fixture'))
            self.assertEqual(metadata['tool_receipt']['tool'],'view_supplement')


if __name__ == '__main__':
    unittest.main()
