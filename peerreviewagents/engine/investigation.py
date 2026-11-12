"""Bounded manuscript investigation with cached literature provenance."""
import base64
import json
import re
import subprocess
from pathlib import Path
from .annotations import verify_workspace
from .calculator import calculate
from .storage import digest
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
        from .external_evidence import SourceClient
        self.sources = SourceClient(config.get('source_state') or str(self.out/'sources'), config['cache'],
                                    config.get('external_request_limit', 6), self.cutoff)
        from .targeted_sources import TargetedSources
        self.targeted = TargetedSources(self.sources)

    def fetch(self, url):
        """Retrieve one allowlisted public record through the shared source client."""
        record = self.sources.fetch(url)
        if record['status'] != 'ok':
            record['unavailable'] = record['status']
        return record

    def call(self,name,args):
        self.calls+=1
        if self.calls>self.limit:
            return {'notice':'Tool allowance exhausted. Finish from existing evidence.'}
        try:
            result=self.dispatch(name,args)
        except (ValueError,KeyError,TypeError,ArithmeticError,SyntaxError,StopIteration,subprocess.SubprocessError) as exc:
            result={'request_rejected':str(exc)}
        result['tool_receipt']={'call_index':self.calls,'tool':name}
        log={ 'tool':name,'arguments':args,'result':{k:v for k,v in result.items() if k!='image'}}
        with (self.out/'tool-log.jsonl').open('a',encoding='utf-8') as stream:
            stream.write(json.dumps(log,ensure_ascii=False)+'\n')
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
            with target.open('a',encoding='utf-8') as stream:
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
            with (self.out/'notes.jsonl').open('a',encoding='utf-8') as stream:
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
