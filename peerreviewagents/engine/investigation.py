"""Bounded manuscript investigation with cached literature provenance."""
import base64
import datetime
import json
import os
import re
import subprocess
from pathlib import Path
from .annotations import verify_workspace
from .calculator import calculate
from .storage import digest, read
from .source_payload import text_block


class Investigation:
    def __init__(self, config):
        self.root=Path(config['paper']).resolve()
        self.out=Path(config['output']).resolve()
        self.out.mkdir(parents=True,exist_ok=True)
        self.paper=verify_workspace(self.root)
        from .result_map import build_result_map
        self.result_map = build_result_map(self.paper)
        self.by={b['id']:b for b in self.paper['blocks']}
        self.limit=config.get('tool_limit',8)
        self.cutoff=config.get('literature_cutoff')
        self.calls=0
        self.evidence_handles=config.get('evidence_handles',False)
        self.manuscript_handles=config.get('manuscript_handles',False)
        self.synthesis_preflight = None
        from .external_evidence import SourceClient
        self.sources = SourceClient(config.get('source_state') or str(self.out/'sources'), config['cache'],
                                    config.get('external_request_limit', 6), self.cutoff)
        from .targeted_sources import TargetedSources
        self.targeted = TargetedSources(self.sources)
        if config.get('synthesis_ledger'):
            from .synthesis_validation import SynthesisPreflight
            self.synthesis_preflight = SynthesisPreflight(
                read(config['synthesis_ledger']), self.paper, self.out/'synthesis-preflight',
                self.sources.cards)

    def audit_tool_discovery(self, event, server_info, tool_names=None):
        """Append the MCP exposure observed by this server process."""
        record = {
            'timestamp': datetime.datetime.now(datetime.timezone.utc).isoformat(),
            'event': event,
            'server': server_info,
        }
        if tool_names is not None:
            record['tools'] = list(tool_names)
        with (self.out/'tool-discovery.jsonl').open('a') as stream:
            stream.write(json.dumps(record, ensure_ascii=False)+'\n')
            stream.flush()
            os.fsync(stream.fileno())

    def fetch(self, url):
        """Retrieve one allowlisted public record through the shared source client."""
        record = self.sources.fetch(url)
        if record['status'] != 'ok':
            record['unavailable'] = record['status']
        return record

    def call(self,name,args):
        if name == 'validate_synthesis' and self.synthesis_preflight is not None:
            return self.synthesis_preflight.call(args.get('draft_json'))
        self.calls+=1
        if self.calls>self.limit:
            return {'notice':'Tool allowance exhausted. Finish from existing evidence.'}
        try:
            result=self.dispatch(name,args)
            if self.evidence_handles:
                result=self.attach_handles(result)
            if self.manuscript_handles and name in ('read_blocks','search'):
                from .evidence_handles import build_registry
                blocks=result.get('blocks',result.get('matches',[]))
                result['manuscript_handles']=[h.model_record() for h in build_registry({'blocks':blocks}).handles]
        except (ValueError,KeyError,TypeError,ArithmeticError,SyntaxError,StopIteration,subprocess.SubprocessError) as exc:
            result={'request_rejected':str(exc)}
        result['tool_receipt']={'call_index':self.calls,'tool':name}
        log={ 'tool':name,'arguments':args,'result':{k:v for k,v in result.items() if k!='image'}}
        with (self.out/'tool-log.jsonl').open('a') as stream:
            stream.write(json.dumps(log,ensure_ascii=False)+'\n')
        return result

    @staticmethod
    def attach_handles(value):
        """Expose immutable external spans without changing stored source cards."""
        from .evidence_handles import source_card_handles
        if isinstance(value,list):
            return [Investigation.attach_handles(item) for item in value]
        if not isinstance(value,dict):
            return value
        result={key:Investigation.attach_handles(item) for key,item in value.items()}
        if 'evidence_id' in value and 'evidence_text' in value:
            result['evidence_handles']=[h.model_record() for h in source_card_handles(value)]
        return result

    def dispatch(self,name,args):
        if name == 'resolve_identifier':
            if args['block_id'] not in self.by:
                raise ValueError('A source investigation needs a valid manuscript block ID')
            return self.targeted.resolve_identifier(args['kind'],args['identifier'],args['block_id'])
        if name == 'inspect_results':
            from .result_map import inspect_results
            if args['block_id'] and args['block_id'] not in self.by:
                raise ValueError('Unknown manuscript block ID')
            return inspect_results(self.result_map, args['block_id'] or None)
        if name == 'record_comparison':
            from .result_map import compare_observations
            result = compare_observations(self.paper, args['observations'])
            target = self.out/'comparisons.jsonl'
            with target.open('a') as stream:
                stream.write(json.dumps(result, ensure_ascii=False)+'\n')
            return result
        if name == 'lookup_source':
            if args['block_id'] not in self.by:
                raise ValueError('A source lookup needs a valid manuscript block ID')
            return self.sources.lookup(args['provider'], args['identifier'], args['block_id'])
        if name=='read_blocks':
            ids=args['ids']
            if len(ids)>12:raise ValueError('Read at most 12 blocks')
            return {'blocks':[text_block(self.by[i]) for i in ids]}
        if name=='search':
            query=args['query']
            if not query or len(query)>200:raise ValueError('Use a short literal query')
            matches=[text_block(b) for b in self.paper['blocks'] if query.casefold() in b['text'].casefold()][:12]
            result={'matches':matches}
            if not matches:
                result['search_guidance']='No exact contiguous phrase matched. This does not establish absence of the concept. Use a shorter phrase or read known block IDs from the section index.'
            return result
        if name=='calculate':
            result={'expression':args['expression'],'result':calculate(args['expression']),'limitation':'Arithmetic only. Applicability requires source evidence.'}
            result['receipt']=digest(json.dumps(result,sort_keys=True))[:16]
            return result
        if name=='save_note':
            if len(args['text'])>4000:raise ValueError('Note exceeds 4000 characters')
            ids=args['evidence_blocks']
            if any(i not in self.by for i in ids):raise ValueError('Unknown evidence block')
            with (self.out/'notes.jsonl').open('a') as stream:
                stream.write(json.dumps(args,ensure_ascii=False)+'\n')
            return {'saved':True,'limitation':'A saved hypothesis is not a verified finding.'}
        if name=='view_page':
            page=args['page']
            if type(page) is not int or not 1<=page<=self.paper['statistics']['pdf_pages']:raise ValueError('Unknown page')
            target=self.out/f'page_{page:03d}.png'
            if not target.exists():
                subprocess.run(['pdftoppm','-f',str(page),'-l',str(page),'-scale-to','1500','-singlefile','-png',str(self.root/'original.pdf'),str(target.with_suffix(''))],check=True,capture_output=True,timeout=30)
            return {'label':f'Original PDF page {page}','image':base64.b64encode(target.read_bytes()).decode()}
        if name=='view_supplement':
            from .supplement_view import supplement_page
            path, metadata = supplement_page(self.root,self.paper,args['id'],self.out,args.get('page',1))
            return {**metadata,'image':base64.b64encode(path.read_bytes()).decode()}
        if name=='check_citation':
            ref=next((r for r in self.paper['references'] if r['id']==args['reference_id']),None)
            if ref is None:raise ValueError('Unknown reference ID')
            ref=dict(ref)
            if not ref.get('doi'):
                match=re.search(r'(?:doi:\s*|https?://(?:dx\.)?doi\.org/)(10\.\d{4,9}/\S+)',ref['raw'],re.I)
                if match:ref['doi']=match.group(1).rstrip('.,')
            if not ref.get('doi'):return {'reference':ref,'unavailable':'No extracted DOI. Use a focused title search.'}
            # Bibliography IDs locate metadata checks, not manuscript scientific claims.
            card = self.sources.lookup('crossref', ref['doi'], ref['id'])
            if card['status'] == 'not_found':
                card = self.sources.lookup('datacite', ref['doi'], ref['id'])
            return {'reference': ref, 'source_card': card,
                    'limitation': 'Bibliographic identity only. Compare the returned title and authors with the extracted reference.'}
        if name=='search_literature':
            if args['block_id'] not in self.by:
                raise ValueError('A literature investigation needs a valid manuscript block ID')
            return self.targeted.search_literature(args['query'],args['block_id'])
        raise ValueError('Unknown investigation tool')


def specs(synthesis_preflight=False):
    definitions=[
        ('read_blocks','Read source blocks by stable IDs.',{'ids':{'type':'array','items':{'type':'string'}}}),
        ('search','Search for one short exact contiguous phrase in manuscript and supplements, not a list of keywords. Read known table block IDs directly when available.',{'query':{'type':'string'}}),
        ('calculate','Bounded numeric arithmetic on reported values. Functions include mean, sum, sqrt, log, exp, round. No code or data reruns.',{'expression':{'type':'string'}}),
        ('save_note','Save a source-linked hypothesis or contradiction check to the investigation ledger.',{'text':{'type':'string'},'evidence_blocks':{'type':'array','items':{'type':'string'}}}),
        ('view_page','Inspect one original PDF page, including figures.',{'page':{'type':'integer'}}),
        ('view_supplement','Inspect one original supplement PDF page or supplied image preview. IDs are in the asset inventory. Page defaults to 1.',{'id':{'type':'string'},'page':{'type':'integer','minimum':1,'maximum':100}}),
        ('check_citation','Resolve an extracted reference DOI through Crossref. Metadata only.',{'reference_id':{'type':'string'}}),
        ('search_literature', 'Search one Europe PMC page for a manuscript question. At most three leads with explicit coverage. Results are not an exhaustive literature review.',
         {'query':{'type':'string'},'block_id':{'type':'string'}}),
        ('resolve_identifier', 'Resolve one exact DOI, PMID, PMCID, NCT or OSF identifier. Identity is separate from scientific support.',
         {'kind':{'type':'string','enum':['doi','pmid','pmcid','nct','osf']},'identifier':{'type':'string'},'block_id':{'type':'string'}}),
    ]
    from .result_map import DIMENSIONS
    observation = {'type':'object', 'properties':{k:{'type':'string'} for k in
        (*DIMENSIONS, 'block_id', 'quote', 'value')},
        'required':[*DIMENSIONS, 'block_id', 'quote', 'value'], 'additionalProperties':False}
    definitions.extend([
        ('inspect_results', 'Inspect linked claims, tables and figure captions. Empty block_id selects a bounded overview. Missing targets reflect extraction limits.',
         {'block_id':{'type':'string'}}),
        ('record_comparison', 'Record two exact textual quantities with model, population, metric, units and scale context. Use unknown for unresolved dimensions. No automatic scientific endorsement.',
         {'observations':{'type':'array','items':observation,'minItems':2,'maxItems':2}}),
    ])
    from .source_adapters import PROVIDERS
    definitions.append(('lookup_source', 'Retrieve one exact public record linked to a manuscript block. Active providers: ' +
                        ', '.join(k + ' (' + v[0] + ')' for k, v in PROVIDERS.items()) +
                        '. Returns dated evidence with limitations. Unlisted providers are unavailable.',
                        {'provider': {'type': 'string'}, 'identifier': {'type': 'string'}, 'block_id': {'type': 'string'}}))
    if synthesis_preflight:
        definitions.append(('validate_synthesis', 'Validate a complete synthesis draft before submission. Two local checks, separate from the source allowance. No network or scientific endorsement.',
                            {'draft_json': {'type': 'string', 'maxLength': 300000}}))
    return [{'name':name,'description':desc,'inputSchema':{'type':'object','properties':props,'required':['id'] if name=='view_supplement' else list(props),'additionalProperties':False},
             'annotations':{'readOnlyHint':False,'destructiveHint':False,'idempotentHint':False,'openWorldHint':name in ('check_citation','search_literature','lookup_source')}} for name,desc,props in definitions]


def main(config_path):
    import sys
    server=Investigation(read(config_path))
    for line in sys.stdin:
        request=json.loads(line)
        if 'id' not in request:continue
        try:
            method=request['method']
            if method=='initialize':
                server_info={'name':'paper-workspace','version':'0.1.0'}
                result={'protocolVersion':request['params']['protocolVersion'],'capabilities':{'tools':{}},'serverInfo':server_info}
                server.audit_tool_discovery('initialize', server_info)
            elif method=='tools/list':
                available=specs(server.synthesis_preflight is not None)
                server.audit_tool_discovery('tools/list', {'name':'paper-workspace','version':'0.1.0'},
                                            [tool['name'] for tool in available])
                result={'tools':available}
            elif method=='ping':result={}
            elif method=='tools/call':
                params=request['params']
                body=server.call(params['name'],params.get('arguments',{}))
                result={'content':[{'type':'image','data':body['image'],'mimeType':'image/png'},
                                   {'type':'text','text':json.dumps({k:v for k,v in body.items() if k!='image'},ensure_ascii=False)}] if 'image' in body else [{'type':'text','text':json.dumps(body,ensure_ascii=False)}]}
            else:raise ValueError('Unsupported method')
            response={'jsonrpc':'2.0','id':request['id'],'result':result}
        except (ValueError,KeyError,TypeError,StopIteration) as exc:
            response={'jsonrpc':'2.0','id':request['id'],'error':{'code':-32602,'message':str(exc)}}
        print(json.dumps(response),flush=True)
