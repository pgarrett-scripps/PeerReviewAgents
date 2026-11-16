"""Bounded manuscript investigation with cached literature provenance."""
import base64
import json
import re
import subprocess
from pathlib import Path
from .annotations import verify_workspace
from .calculator import calculate
from .storage import digest, read
from . import paper_base, paper_view

#: Tools that do not spend the allowance. Reading the manuscript is not an
#: investigative step a reviewer should be rationing: a budget that counts it
#: buys the reviewer's attention away from the paper and into deciding which
#: parts of it to skip, which is the opposite of what the allowance is for.
FREE_TOOLS = ('read_paper',)


class Investigation:
    def __init__(self, config):
        self.root=Path(config['paper']).resolve()
        self.out=Path(config['output']).resolve()
        self.out.mkdir(parents=True,exist_ok=True)
        self.base=verify_workspace(self.root)
        self.references=read(self.root/'references.json')
        from .result_map import build_result_map
        self.result_map = build_result_map(self.base)
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
        if name not in FREE_TOOLS:
            self.calls+=1
            if self.calls>self.limit:
                return {'notice':'Tool allowance exhausted. Finish from existing evidence.'}
        try:
            result=self.dispatch(name,args)
        except (ValueError,KeyError,TypeError,ArithmeticError,SyntaxError,StopIteration,subprocess.SubprocessError) as exc:
            result={'request_rejected':str(exc)}
        result['tool_receipt']={'call_index':self.calls,'tool':name,
                                'spent':name not in FREE_TOOLS}
        # The manuscript itself is not written into the log: the log is the
        # record of what was asked for, and read_paper's answer is a copy of a
        # file the folder already holds. What is kept is which part was served,
        # which is what a reader of the log wants to know.
        log={ 'tool':name,'arguments':args,
              'result':{k:v for k,v in result.items() if k not in ('image','text')}}
        with (self.out/'tool-log.jsonl').open('a',encoding='utf-8') as stream:
            stream.write(json.dumps(log,ensure_ascii=False)+'\n')
        return result

    def _line(self, args, what):
        """One line of one document of this manuscript, or a refusal saying so."""
        document = str(args.get('document') or paper_base.MAIN_DOCUMENT)
        if document not in self.base.documents:
            raise ValueError(what + ' names a document this manuscript does not hold')
        total = self.base.line_count(document)
        line = args.get('line')
        if type(line) is not int or isinstance(line, bool) or not 1 <= line <= total:
            raise ValueError(what + ' names a line of ' + document + ', 1 to ' + str(total))
        return document, line

    def _anchor(self, args, what):
        """Where a source investigation came from, as ``paper.md:118``."""
        document, line = self._line(args, what)
        return document + ':' + str(line)

    def dispatch(self,name,args):
        if name == 'resolve_identifier':
            anchor = self._anchor(args, 'A source investigation')
            return self.targeted.resolve_identifier(args['kind'],args['identifier'],anchor)
        if name == 'inspect_results':
            from .result_map import inspect_results
            if args.get('line'):
                document, line = self._line(args, 'A result inspection')
            else:
                document, line = str(args.get('document') or paper_base.MAIN_DOCUMENT), None
            return inspect_results(self.result_map, line, document)
        if name == 'record_comparison':
            from .result_map import compare_observations
            result = compare_observations(self.base, args['observations'])
            target = self.out/'comparisons.jsonl'
            with target.open('a',encoding='utf-8') as stream:
                stream.write(json.dumps(result, ensure_ascii=False)+'\n')
            return result
        if name == 'lookup_source':
            anchor = self._anchor(args, 'A source lookup')
            return self.sources.lookup(args['provider'], args['identifier'], anchor)
        if name=='read_paper':
            document=str(args.get('document') or paper_base.MAIN_DOCUMENT)
            part=args.get('part',1)
            if part in (None,''):part=1
            text=paper_view.render(self.base,document,part)
            return {'document':document,'part':part,
                    'parts':paper_view.served(self.base,document),
                    'lines':self.base.line_count(document),'text':text}
        if name=='search':
            query=args['query']
            if not query or len(query)>200:raise ValueError('Use a short literal query')
            matches,more=paper_view.search(self.base,query)
            result={'matches':matches}
            if more:
                result['search_guidance']=('More lines than this match. The phrase is in the '
                                           'manuscript; read the paper for the rest.')
            elif not matches:
                result['search_guidance']=('No exact contiguous phrase matched. This does not '
                                           'establish absence of the concept: the reader may have '
                                           'hyphenated or broken the words differently. Use a '
                                           'shorter phrase, or read the paper.')
            return result
        if name=='calculate':
            result={'expression':args['expression'],'result':calculate(args['expression']),'limitation':'Arithmetic only. Applicability requires source evidence.'}
            result['receipt']=digest(json.dumps(result,sort_keys=True))[:16]
            return result
        if name=='save_note':
            if len(args['text'])>4000:raise ValueError('Note exceeds 4000 characters')
            total=self.base.line_count()
            if any(type(n) is not int or not 1<=n<=total for n in args['evidence_lines']):
                raise ValueError('Evidence lines are line numbers of paper.md, 1 to '+str(total))
            with (self.out/'notes.jsonl').open('a',encoding='utf-8') as stream:
                stream.write(json.dumps(args,ensure_ascii=False)+'\n')
            return {'saved':True,'limitation':'A saved hypothesis is not a verified finding.'}
        if name=='view_page':
            page=args['page']
            pages=(self.base.manifest.get('statistics') or {}).get('pdf_pages')
            if type(page) is not int or not 1<=page<=(pages or 0):raise ValueError('Unknown page')
            target=self.out/f'page_{page:03d}.png'
            if not target.exists():
                subprocess.run(['pdftoppm','-f',str(page),'-l',str(page),'-scale-to','1500','-singlefile','-png',str(self.root/'original'/'original.pdf'),str(target.with_suffix(''))],check=True,capture_output=True,timeout=30)
            return {'label':f'Original PDF page {page}','image':base64.b64encode(target.read_bytes()).decode()}
        if name=='view_supplement':
            from .supplement_view import supplement_page
            path, metadata = supplement_page(self.root,self.base.manifest,args['id'],self.out,args.get('page',1))
            return {**metadata,'image':base64.b64encode(path.read_bytes()).decode()}
        if name=='check_citation':
            ref=next((r for r in self.references if r['id']==args['reference_id']),None)
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
            anchor = self._anchor(args, 'A literature investigation')
            return self.targeted.search_literature(args['query'],anchor)
        raise ValueError('Unknown investigation tool')
