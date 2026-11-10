"""Targeted identifier, literature, and curated historical registration access."""
import datetime
import argparse
import json
import re
from pathlib import Path
from urllib.parse import quote, urlencode, urlsplit

from .storage import digest
from .source_adapters import parse as parse_adapter


IDENTIFIER_KINDS = ('doi', 'pmid', 'pmcid', 'nct', 'osf')
REGISTRATION_HOSTS = {'clinicaltrials.gov', 'api.osf.io'}


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


class HistoricalRegistrationStore:
    """Read hash-manifested dated snapshots without changing source artifacts."""
    def __init__(self, root):
        self.root = Path(root)
        manifest_path = self.root / 'manifest.json'
        manifest_raw = manifest_path.read_bytes()
        if not manifest_raw or len(manifest_raw) > 1_000_000:
            raise ValueError('Historical registration manifest is empty or too large')
        self.manifest_sha256 = digest(manifest_raw)
        self.manifest = json.loads(manifest_raw)
        if self.manifest.get('schema_version') != 1 or not isinstance(self.manifest.get('files'), dict):
            raise ValueError('Historical registration manifest is invalid')

    def _records(self):
        manifest_path = self.root / 'manifest.json'
        manifest_raw = manifest_path.read_bytes()
        if digest(manifest_raw) != self.manifest_sha256 or json.loads(manifest_raw) != self.manifest:
            raise ValueError('Historical registration manifest changed')
        records = []
        for name, expected_hash in self.manifest['files'].items():
            path = (self.root / name).resolve()
            if not path.is_relative_to(self.root.resolve()) or not path.is_file():
                raise ValueError('Historical registration snapshot path is invalid')
            raw = path.read_bytes()
            if not raw or len(raw) > 2_000_000:
                raise ValueError('Historical registration source file is empty or too large')
            if digest(raw) != expected_hash:
                raise ValueError('Historical registration snapshot changed')
            if not name.endswith('.snapshot.json'):
                continue
            record = json.loads(raw)
            _registration_url(record.get('registry'), record.get('identifier'),
                              record.get('source_url', ''))
            parsed = urlsplit(record.get('source_url', ''))
            if (record.get('schema_version') != 1 or parsed.scheme != 'https'
                    or parsed.hostname not in REGISTRATION_HOSTS):
                raise ValueError('Historical registration snapshot provenance is invalid')
            datetime.date.fromisoformat(record['effective_date'])
            datetime.datetime.fromisoformat(record['captured_at'].replace('Z', '+00:00'))
            if record.get('registry') not in ('clinicaltrials', 'osf'):
                raise ValueError('Historical registration registry is unsupported')
            raw_name = record.get('raw_file')
            raw_path = (self.root / str(raw_name)).resolve()
            if (not raw_name or not raw_path.is_relative_to(self.root.resolve())
                    or not raw_path.is_file() or raw_name not in self.manifest['files']):
                raise ValueError('Historical registration raw source is missing')
            official_raw = raw_path.read_bytes()
            if not official_raw or len(official_raw) > 2_000_000:
                raise ValueError('Historical registration raw source is empty or too large')
            if (digest(official_raw) != self.manifest['files'][raw_name]
                    or digest(official_raw) != record.get('raw_sha256')):
                raise ValueError('Historical registration raw source changed')
            if not isinstance(record.get('fields'), dict):
                raise ValueError('Historical registration snapshot is incomplete')
            try:
                official_data = json.loads(official_raw)
                provider = 'clinicaltrials' if record['registry'] == 'clinicaltrials' else 'osf'
                derived_fields, derived_version, _, derived_id = parse_adapter(
                    provider, record['identifier'], official_data)
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ValueError('Historical registration raw source cannot be parsed') from exc
            if (derived_id is None or derived_id.casefold() != record['identifier'].casefold()
                    or derived_fields != record['fields']
                    or derived_version != record.get('record_version')):
                raise ValueError('Historical registration metadata does not match the pinned raw source')
            captured_date = datetime.datetime.fromisoformat(
                record['captured_at'].replace('Z', '+00:00')).date().isoformat()
            if captured_date > record['effective_date'] and not (
                    isinstance(record.get('curator_attestation'), str)
                    and record['curator_attestation'].strip()
                    and isinstance(record.get('version_locator'), str)
                    and record['version_locator'].strip()):
                raise ValueError('Post-date historical capture lacks curator attestation and version locator')
            records.append(record)
        return records

    def lookup(self, registry, identifier, as_of):
        datetime.date.fromisoformat(as_of)
        matches = [row for row in self._records() if row['registry'] == registry
                   and row['identifier'].casefold() == identifier.casefold()
                   and row['effective_date'] <= as_of]
        if not matches:
            return {'status':'historical_snapshot_unavailable', 'eligible':False,
                    'registry':registry, 'identifier':identifier, 'as_of':as_of,
                    'limitation':'No curated dated snapshot at or before the requested date is in the immutable snapshot set.'}
        matches.sort(key=lambda row:(row['effective_date'], row['captured_at']))
        row = matches[-1]
        captured_date = datetime.datetime.fromisoformat(
            row['captured_at'].replace('Z', '+00:00')).date().isoformat()
        if captured_date > as_of and not (row.get('curator_attestation') and row.get('version_locator')):
            return {'status':'historical_snapshot_unavailable', 'eligible':False,
                    'registry':registry, 'identifier':identifier, 'as_of':as_of,
                    'limitation':'The record was captured after the requested date without a pinned historical version attestation.'}
        evidence_text = '\n'.join(key + ': ' + json.dumps(value, ensure_ascii=False, sort_keys=True)
                                  for key, value in row['fields'].items())
        identity = {'registry':registry, 'identifier':identifier, 'as_of':as_of,
                    'effective_date':row['effective_date'], 'raw_sha256':row['raw_sha256'],
                    'evidence_text':evidence_text}
        return {'status':'ok', 'eligible':True, 'provider':registry + '-historical',
            'identifier':identifier, 'as_of':as_of, 'record_version':row.get('record_version'),
            'effective_date':row['effective_date'], 'captured_at':row['captured_at'],
            'source_url':row['source_url'], 'raw_sha256':row['raw_sha256'],
            'version_locator':row.get('version_locator'),
            'curator_attestation':row.get('curator_attestation'),
            'fields':row['fields'], 'evidence_text':evidence_text,
            'source_id':'historical-' + digest(json.dumps(identity, sort_keys=True))[:20],
            'limitation':'Curated dated snapshot provenance only. It does not certify historical completeness. Coverage depends on the immutable snapshot set and does not reconstruct omitted attachments or every registry transition.'}


def _registration_url(registry, identifier, source_url):
    parsed = urlsplit(source_url)
    if parsed.scheme != 'https' or parsed.hostname not in REGISTRATION_HOSTS:
        raise ValueError('Registration source URL is not an allowed official endpoint')
    if registry == 'clinicaltrials':
        if parsed.hostname != 'clinicaltrials.gov' or identifier.upper() not in parsed.path.upper():
            raise ValueError('ClinicalTrials.gov source URL does not match the registration ID')
    elif registry == 'osf':
        expected = '/v2/registrations/' + identifier.lower() + '/'
        if parsed.hostname != 'api.osf.io' or parsed.path.lower() != expected:
            raise ValueError('OSF source URL does not match the registration ID')
    else:
        raise ValueError('Unsupported registration registry')


def curate_registration_snapshot(raw_path, output_root, registry, identifier, captured_at,
                                  source_url, archive_effective_date=None,
                                  version_locator=None, curator_attestation=None,
                                  maximum_bytes=2_000_000):
    """Create and recheck one immutable snapshot set from official raw JSON."""
    raw_path, output_root = Path(raw_path), Path(output_root)
    if output_root.exists() and any(output_root.iterdir()):
        raise ValueError('Snapshot output directory must be new or empty')
    raw = raw_path.read_bytes()
    if not raw or len(raw) > maximum_bytes:
        raise ValueError('Registration raw response is empty or exceeds the bounded size')
    _registration_url(registry, identifier, source_url)
    captured = datetime.datetime.fromisoformat(captured_at.replace('Z', '+00:00'))
    if captured.tzinfo is None:
        raise ValueError('captured_at must include a timezone')
    data = json.loads(raw)
    provider = 'clinicaltrials' if registry == 'clinicaltrials' else 'osf'
    fields, version, _, resolved = parse_adapter(provider, identifier, data)
    if resolved is None or resolved.casefold() != identifier.casefold():
        raise ValueError('Official raw response does not match the registration ID')
    normalized = json.dumps(fields, ensure_ascii=False, sort_keys=True)
    if len(normalized.encode()) > 14_000:
        raise ValueError('Selected registration fields exceed the model-facing bound')
    capture_date = captured.date().isoformat()
    effective_date = archive_effective_date or capture_date
    datetime.date.fromisoformat(effective_date)
    if effective_date != capture_date and not (
            isinstance(version_locator, str) and version_locator.strip()
            and isinstance(curator_attestation, str) and curator_attestation.strip()):
        raise ValueError('Historical archive capture requires a version locator and curator attestation')
    output_root.mkdir(parents=True, exist_ok=True)
    safe = registry + '-' + re.sub(r'[^A-Za-z0-9_-]', '_', identifier)
    raw_name, record_name = safe + '.raw.json', safe + '.snapshot.json'
    raw_out = output_root / raw_name
    raw_out.write_bytes(raw)
    record = {'schema_version':1, 'registry':registry, 'identifier':identifier,
        'effective_date':effective_date, 'captured_at':captured_at,
        'source_url':source_url, 'record_version':version,
        'version_locator':version_locator, 'curator_attestation':curator_attestation,
        'raw_file':raw_name, 'raw_sha256':digest(raw), 'fields':fields,
        'provenance_status':'curated_snapshot_not_verified_historical_truth'}
    (output_root / record_name).write_text(json.dumps(record, indent=2, ensure_ascii=False) + '\n')
    manifest = {'schema_version':1, 'files':{
        raw_name:digest(raw_out.read_bytes()),
        record_name:digest((output_root / record_name).read_bytes())}}
    (output_root / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    store = HistoricalRegistrationStore(output_root)
    store._records()
    return {'snapshot_directory':str(output_root),
        'manifest_sha256':digest((output_root / 'manifest.json').read_bytes()),
        'record_file':record_name, 'raw_file':raw_name,
        'provenance_status':record['provenance_status']}


class TargetedSources:
    def __init__(self, client, historical_store=None):
        self.client = client
        self.historical_store = historical_store

    def _card(self, provider, identifier, block_id, record, fields, eligible, limitation):
        text = '\n'.join(key + ': ' + json.dumps(value, ensure_ascii=False, sort_keys=True)
                         for key, value in fields.items())
        card = {key:value for key, value in record.items() if key != 'data'}
        card.update(provider=provider, identifier=identifier, manuscript_block_id=block_id,
            evidence_mode=self.client.mode, cutoff=self.client.cutoff, record_version=None,
            source_publication_date=None, fields=fields, evidence_text=text,
            eligible=eligible, limitation=limitation)
        if self.client.mode == 'historical' and eligible:
            card.update(status='version_unavailable', eligible=False,
                        temporal_status='historical_record_not_established')
            card['limitation'] += ' This live response is not an archived historical record.'
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

    def historical_registration(self, registry, identifier, as_of, block_id):
        as_of_date = datetime.date.fromisoformat(as_of)
        if self.client.cutoff is not None and as_of_date > datetime.date.fromisoformat(self.client.cutoff):
            raise ValueError('Historical registration date exceeds the configured evidence cutoff')
        denied = self.client.reserve_target(block_id, 'historical-registration')
        if denied:
            return denied
        if self.historical_store is None:
            return {'status':'historical_snapshot_unavailable', 'eligible':False,
                    'provider':'historical-registration', 'identifier':identifier,
                    'manuscript_block_id':block_id,
                    'limitation':'No curated immutable historical snapshot set was configured.'}
        result = self.historical_store.lookup(registry, identifier, as_of)
        result.update(manuscript_block_id=block_id, evidence_mode='historical', cutoff=as_of)
        if not result.get('eligible'):
            return result
        result['provider'] = 'historical-registration'
        result['identifier'] = registry + ':' + identifier
        result['record_version'] = result.get('record_version') or result.get('effective_date')
        return self.client.store_card(result)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Curate one bounded registration snapshot set')
    parser.add_argument('--raw', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--registry', required=True, choices=('clinicaltrials','osf'))
    parser.add_argument('--identifier', required=True)
    parser.add_argument('--captured-at', required=True)
    parser.add_argument('--source-url', required=True)
    parser.add_argument('--archive-effective-date')
    parser.add_argument('--version-locator')
    parser.add_argument('--curator-attestation')
    args = parser.parse_args(argv)
    result = curate_registration_snapshot(args.raw, args.output, args.registry,
        args.identifier, args.captured_at, args.source_url,
        args.archive_effective_date, args.version_locator, args.curator_attestation)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
