"""Bounded, source-preserving retrieval for the versioned debate workflow."""
import json
import sys

from .investigation import Investigation
from .storage import read
from .source_payload import text_block


TOOLS = ('search_source', 'claim_context', 'experiment_context', 'view_page')


def search_source(paper, terms, mode='any', limit=12):
    if (not isinstance(terms, list) or not 1 <= len(terms) <= 6
            or any(not isinstance(t, str) or not t.strip() or len(t) > 120 for t in terms)):
        raise ValueError('Supply one to six nonempty literal terms, each at most 120 characters')
    if mode not in ('any', 'all'):
        raise ValueError('Mode must be any or all')
    if type(limit) is not int or not 1 <= limit <= 24:
        raise ValueError('Return at most 24 blocks')
    terms = [t.strip().casefold() for t in terms]
    matches = [b for b in paper['blocks'] if (any if mode == 'any' else all)(
        t in b['text'].casefold() for t in terms)]
    return {'blocks': [text_block(b) for b in matches[:limit]], 'total_matches': len(matches),
            'truncated': len(matches) > limit, 'scanned_blocks': len(paper['blocks']),
            'scope': 'All imported text blocks only. Image pixels and unavailable assets are not searched.',
            'limitation': 'Literal matches are retrieval aids. No match does not establish absence of evidence.'}


def context_bundle(paper, ids, terms, mode='claim'):
    if mode not in ('claim', 'experiment'):
        raise ValueError('Unknown context mode')
    by = {b['id']: b for b in paper['blocks']}
    if (not isinstance(ids, list) or len(ids) > 8 or len(set(ids)) != len(ids)
            or any(i not in by for i in ids)):
        raise ValueError('Supply up to eight unique existing block IDs')
    found = search_source(paper, terms, limit=24)
    selected = set(ids)
    neighbors = set()
    for i, block in enumerate(paper['blocks']):
        if block['id'] in selected:
            neighbors.update(b['id'] for b in paper['blocks'][max(0, i-1):i+2])
    # Keep guaranteed source context separate from capped search matches.
    abstract = [b for b in paper['blocks'] if b.get('section', '').casefold() == 'abstract']
    pages = sorted({by[i]['page'] for i in selected if type(by[i].get('page')) is int})
    return {'title': paper['title'], 'source_sha256': paper['source_sha256'],
            'mode': mode, 'requested_blocks': [text_block(by[i]) for i in ids],
            'neighbor_blocks': [text_block(b) for b in paper['blocks'] if b['id'] in neighbors - selected],
            'abstract_blocks': abstract if mode == 'claim' else [],
            'matching_blocks': found['blocks'], 'search_coverage': {k:v for k,v in found.items() if k != 'blocks'},
            'page_locations': pages, 'section_index': paper.get('sections', {}),
            'assets': paper.get('assets', []),
            'limitation': 'Retrieved context is not an adjudication. Section and page mappings may be provisional. '
                          'Use original pages for visual claims. No automatic figure-to-method linkage is asserted.'}


class DebateInvestigation(Investigation):
    def dispatch(self, name, args):
        if name == 'search_source':
            return search_source(self.paper, args['terms'], args['mode'], args['limit'])
        if name in ('claim_context', 'experiment_context'):
            return context_bundle(self.paper, args['ids'], args['terms'],
                                  'claim' if name == 'claim_context' else 'experiment')
        if name == 'view_page':
            return super().dispatch(name, args)
        raise ValueError('Tool is not in the debate-v2 source-only profile')


def specs():
    terms = {'type':'array', 'items':{'type':'string'}, 'minItems':1, 'maxItems':6}
    ids = {'type':'array', 'items':{'type':'string'}, 'maxItems':8}
    definitions = [
        ('search_source', 'Search all imported text with explicit any/all literal terms. Reports truncation and scope.',
         {'terms':terms, 'mode':{'type':'string','enum':['any','all']}, 'limit':{'type':'integer','minimum':1,'maximum':24}}),
        ('claim_context', 'Read claim blocks, neighbors, abstract, and matching passages across the manuscript. Check title and conclusion wording.',
         {'ids':ids, 'terms':terms}),
        ('experiment_context', 'Read experimental blocks, neighbors, matching legends and methods, and original page locations. Linkage remains provisional.',
         {'ids':ids, 'terms':terms}),
        ('view_page', 'Inspect an original PDF page for decisive visual evidence.', {'page':{'type':'integer'}}),
    ]
    return [{'name':n, 'description':d, 'inputSchema':{'type':'object','properties':p,
             'required':list(p), 'additionalProperties':False}} for n,d,p in definitions]


def main(config_path):
    server = DebateInvestigation(read(config_path))
    for line in sys.stdin:
        request = json.loads(line)
        if 'id' not in request:
            continue
        try:
            method = request['method']
            if method == 'initialize':
                result = {'protocolVersion':request['params']['protocolVersion'], 'capabilities':{'tools':{}},
                          'serverInfo':{'name':'paper-debate-v2','version':'0.1.0'}}
            elif method == 'tools/list':
                result = {'tools':specs()}
            elif method == 'ping':
                result = {}
            elif method == 'tools/call':
                p = request['params']
                body = server.call(p['name'], p.get('arguments', {}))
                result = {'content':[{'type':'image','data':body['image'],'mimeType':'image/png'},
                                     {'type':'text','text':body['label']}] if 'image' in body else
                                    [{'type':'text','text':json.dumps(body, ensure_ascii=False)}]}
            else:
                raise ValueError('Unsupported method')
            response = {'jsonrpc':'2.0','id':request['id'],'result':result}
        except (ValueError, KeyError, TypeError) as exc:
            response = {'jsonrpc':'2.0','id':request['id'],'error':{'code':-32602,'message':str(exc)}}
        print(json.dumps(response), flush=True)
