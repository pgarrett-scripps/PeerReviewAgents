"""Targeted identifier and literature access over the bounded source client."""
import datetime
import json
import re
from urllib.parse import quote, urlencode

from .storage import digest


IDENTIFIER_KINDS = ('doi', 'pmid', 'pmcid', 'nct', 'osf')


def literature_request(query, cutoff=None, limit=3):
    if not isinstance(query, str) or query != query.strip() or not 3 <= len(query) <= 300:
        raise ValueError('Literature search needs a focused query of 3 to 300 characters')
    if type(limit) is not int or not 1 <= limit <= 3:
        raise ValueError('Literature search returns at most three results')
    search = query
    if cutoff is not None:
        datetime.date.fromisoformat(cutoff)
        search = '(' + query + ') AND FIRST_PDATE:[1800-01-01 TO ' + cutoff + ']'
    return 'https://www.ebi.ac.uk/europepmc/webservices/rest/search?' + urlencode({
        'query':search, 'format':'json', 'resultType':'core', 'pageSize':limit,
    })


def parse_literature(query, data, cutoff=None, limit=3):
    rows = data.get('resultList', {}).get('result', [])
    if not isinstance(rows, list):
        raise TypeError('Europe PMC search results must be a list')
    hits = []
    excluded = {'unknown_publication_date':0,'after_cutoff':0}
    for row in rows[:limit]:
        published = row.get('firstPublicationDate')
        if cutoff:
            try:
                published_date = datetime.date.fromisoformat(published)
            except (ValueError,TypeError):
                excluded['unknown_publication_date'] += 1
                continue
            if published_date.isoformat() > cutoff:
                excluded['after_cutoff'] += 1
                continue
        identity = {'source':row.get('source'), 'id':row.get('id'),
                    'pmid':row.get('pmid'), 'pmcid':row.get('pmcid'), 'doi':row.get('doi')}
        source_id = 'literature-' + digest(json.dumps(identity, sort_keys=True))[:20]
        abstract = row.get('abstractText')
        omitted = 0
        if isinstance(abstract, str) and len(abstract) > 2_000:
            omitted = len(abstract) - 2_000
            abstract = abstract[:2_000]
        hits.append({'source_id':source_id, **identity,
            'title':row.get('title'), 'author_string':row.get('authorString'),
            'publication_date':published, 'indexed_date':row.get('firstIndexDate'), 'journal':row.get('journalTitle'),
            'publication_types':row.get('pubTypeList', {}).get('pubType', [])[:6],
            'abstract':abstract, 'abstract_truncated':bool(omitted),
            'abstract_omitted_characters':omitted, 'open_access':row.get('isOpenAccess')})
    total = data.get('hitCount')
    return {'query':query, 'cutoff':cutoff, 'requested_limit':limit,
            'excluded_from_returned_page':excluded,
            'returned_count':len(hits), 'reported_total_hits':total,
            'coverage':'First relevance-ranked Europe PMC page only. No synonym expansion audit, citation chasing, full-text search, or second page.',
            'truncated':bool(isinstance(total, int) and total > len(hits)), 'hits':hits}


def identifier_request(kind, identifier):
    if kind not in IDENTIFIER_KINDS:
        raise ValueError('Unsupported identifier kind')
    if not isinstance(identifier, str) or identifier != identifier.strip() or len(identifier) > 160:
        raise ValueError('Use a short exact identifier')
    patterns = {'doi':r'10\.\d{4,9}/\S+', 'pmid':r'\d{1,12}',
        'pmcid':r'PMC\d{1,12}', 'nct':r'NCT\d{8}', 'osf':r'[a-z0-9]{5,12}'}
    if not re.fullmatch(patterns[kind], identifier, re.IGNORECASE if kind in ('doi','pmcid') else 0):
        raise ValueError('Identifier format does not match ' + kind)
    encoded = quote(identifier, safe='')
    if kind == 'doi':
        return 'https://api.crossref.org/works/' + encoded
    if kind in ('pmid', 'pmcid'):
        query = ('EXT_ID:' + identifier + ' AND SRC:MED') if kind == 'pmid' else 'PMCID:' + identifier
        return 'https://www.ebi.ac.uk/europepmc/webservices/rest/search?' + urlencode({
            'query':query, 'format':'json', 'resultType':'core', 'pageSize':2})
    if kind == 'nct':
        return 'https://clinicaltrials.gov/api/v2/studies/' + encoded.upper()
    return 'https://api.osf.io/v2/registrations/' + encoded.lower() + '/'


def parse_identifier(kind, identifier, data):
    candidates = []
    if kind == 'doi':
        row = data.get('message', {})
        candidates = [{'resolved_id':row.get('DOI'), 'title':row.get('title', [])[:1],
            'type':row.get('type'), 'published':row.get('published'),
            'relation':row.get('relation'), 'updates':row.get('update-to') or row.get('updated-by')}]
    elif kind in ('pmid', 'pmcid'):
        for row in data.get('resultList', {}).get('result', [])[:2]:
            if kind == 'pmid' and row.get('source') != 'MED':
                continue
            resolved = row.get('id') if kind == 'pmid' else row.get('pmcid')
            candidates.append({'resolved_id':resolved, 'pmid':row.get('pmid'),
                'pmcid':row.get('pmcid'), 'doi':row.get('doi'), 'title':row.get('title'),
                'publication_date':row.get('firstPublicationDate')})
    elif kind == 'nct':
        protocol = data.get('protocolSection', {})
        identity = protocol.get('identificationModule', {})
        candidates = [{'resolved_id':identity.get('nctId'),
            'brief_title':identity.get('briefTitle'),
            'official_title':identity.get('officialTitle'),
            'last_update':protocol.get('statusModule', {}).get('lastUpdatePostDateStruct', {}).get('date')}]
    elif kind == 'osf':
        row = data.get('data', {})
        attrs = row.get('attributes', {})
        candidates = [{'resolved_id':row.get('id'), 'title':attrs.get('title'),
            'date_registered':attrs.get('date_registered'), 'withdrawn':attrs.get('withdrawn'),
            'embargoed':attrs.get('embargoed')}]
    else:
        raise ValueError('Unsupported identifier kind')
    expected = identifier.casefold()
    exact = [row for row in candidates if str(row.get('resolved_id', '')).casefold() == expected]
    status = 'resolved' if len(exact) == 1 else 'ambiguous' if len(candidates) > 1 else 'identity_mismatch'
    fields = exact[0] if len(exact) == 1 else {'candidates':candidates}
    source_id = 'identifier-' + digest(json.dumps({'kind':kind, 'identifier':identifier,
        'fields':fields}, sort_keys=True))[:20]
    return {'status':status, 'kind':kind, 'identifier':identifier,
            'source_id':source_id, 'fields':fields,
            'limitation':'Identifier resolution verifies record identity and selected metadata, not claim support or historical state.'}


class TargetedSources:
    def __init__(self, client):
        self.client = client

    def _card(self, provider, identifier, block_id, record, fields, eligible, limitation):
        text = '\n'.join(key + ': ' + json.dumps(value, ensure_ascii=False, sort_keys=True)
                         for key, value in fields.items())
        card = {key:value for key, value in record.items() if key != 'data'}
        card.update(provider=provider, identifier=identifier, manuscript_block_id=block_id,
            cutoff=self.client.cutoff, record_version=None,
            source_publication_date=None, fields=fields, evidence_text=text,
            eligible=eligible, limitation=limitation)
        return self.client.store_card(card)

    def search_literature(self, query, block_id, limit=3):
        denied = self.client.reserve_target(block_id, 'literature-search')
        if denied:
            return denied
        url = literature_request(query, self.client.cutoff, limit)
        record = self.client.fetch(url)
        data = record.get('data')
        if record.get('status') != 'ok' or data is None:
            return self._card('literature-search', query, block_id, record, {}, False,
                'Search failed before coverage could be established.')
        coverage = parse_literature(query, data, self.client.cutoff, limit)
        return self._card('literature-search', query, block_id, record, coverage, bool(coverage['hits']),
            'Search results are relevance-ranked leads. Coverage is explicit but not exhaustive, and abstracts do not replace full-text assessment.')

    def resolve_identifier(self, kind, identifier, block_id):
        denied = self.client.reserve_target(block_id, 'identifier-resolution')
        if denied:
            return denied
        record = self.client.fetch(identifier_request(kind, identifier))
        data = record.get('data')
        if record.get('status') != 'ok' or data is None:
            return self._card('identifier-resolution', kind + ':' + identifier, block_id,
                record, {}, False, 'Identifier request failed before identity could be established.')
        result = parse_identifier(kind, identifier, data)
        return self._card('identifier-resolution', kind + ':' + identifier, block_id,
            record, result, result['status'] == 'resolved', result['limitation'])
