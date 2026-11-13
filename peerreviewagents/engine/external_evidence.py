"""Bounded public-source retrieval and durable manuscript-linked evidence cards."""
import datetime
import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .file_lock import exclusive_lock
from .source_adapters import PROVIDERS, request, parse
from .storage import digest, read, write

HOSTS = {'api.crossref.org', 'api.datacite.org', 'www.ebi.ac.uk', 'rest.uniprot.org',
         'data.rcsb.org', 'pubchem.ncbi.nlm.nih.gov', 'clinicaltrials.gov', 'api.osf.io',
         'api.gbif.org', 'string-db.org'}
HOSTS.update({'rest.ensembl.org', 'reactome.org', 'cmr.earthdata.nasa.gov', 'api.worldbank.org'})

#: How this software names itself to a public API. Generic on purpose: a request
#: a reviewer's tool makes says what the software is and nothing about whose
#: manuscript it is, which rules out the mailto address these APIs invite.
USER_AGENT = 'PRA/0.2 (bounded scientific record verification)'
#: An HTTP code as the reason a lookup did not answer. None of these mean a
#: cited work does not exist; a caller that cannot tell must say it cannot tell.
HTTP_STATUS = {404: 'not_found', 401: 'auth_required', 403: 'access_denied',
               429: 'rate_limited'}
#: The largest response any public-source lookup will read.
RESPONSE_LIMIT = 2_000_000


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def check_url(url):
    """Refuse anything but plain https to an allowlisted public API host."""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != 'https' or parsed.hostname not in HOSTS or parsed.username or parsed.port:
        raise ValueError('Source endpoint is outside the public provider allowlist')
    return url


def retrieve(url, opener=None, headers=None, timeout=20, limit=RESPONSE_LIMIT):
    """One allowlisted HTTPS GET as a status and, when it worked, raw bytes.

    The transport only: no cache, no budget, no parsing. Shared by the agents'
    rationed :class:`SourceClient` and by the citation ledger, which resolves
    every reference with its own allowance — so the allowlist, the redirect
    policy and the client name stay in one place, and bytes that are not JSON
    (an open-access full text, say) can be read by a caller that wants them.
    """
    check_url(url)
    call = opener or urllib.request.build_opener(NoRedirect()).open
    req = urllib.request.Request(url, headers={'User-Agent': USER_AGENT, **(headers or {})})
    try:
        with call(req, timeout=timeout) as response:
            raw = response.read(limit + 1)
    except urllib.error.HTTPError as exc:
        return {'status': HTTP_STATUS.get(exc.code, 'http_error'), 'source_url': url,
                'http_status': exc.code}
    except (OSError, ValueError) as exc:
        return {'status': 'unavailable', 'source_url': url, 'error_type': type(exc).__name__}
    if len(raw) > limit:
        return {'status': 'response_too_large', 'source_url': url}
    return {'status': 'ok', 'source_url': url, 'raw': raw}


class SourceClient:
    def __init__(self, state, cache, limit=6, cutoff=None, opener=None):
        if type(limit) is not int or not 0 <= limit <= 30:
            raise ValueError('External request allowance must be between zero and thirty')
        if cutoff is not None:
            datetime.date.fromisoformat(cutoff)
        self.state, self.cache = Path(state), Path(cache)
        self.state.mkdir(parents=True, exist_ok=True)
        self.cache.mkdir(parents=True, exist_ok=True)
        self.limit, self.cutoff = limit, cutoff
        self.opener = opener or urllib.request.build_opener(NoRedirect()).open

    def reserve_target(self, block_id, provider='targeted-source'):
        if not isinstance(block_id, str) or not block_id.strip() or len(block_id) > 160:
            raise ValueError('Source investigation requires a short manuscript block ID')
        with (self.state / 'targets.lock').open('a', encoding='utf-8') as lock:
            exclusive_lock(lock)
            target_path = self.state / 'targets.json'
            targets = read(target_path) if target_path.exists() else []
            if block_id not in targets:
                if len(targets) >= 2:
                    return {'status': 'target_limit', 'provider': provider,
                            'limitation': 'Two source investigation locations have already been selected for this condition.'}
                targets.append(block_id)
                write(target_path, targets)
        return None

    def store_card(self, card):
        card = dict(card)
        evidence_text = card.get('evidence_text', '')
        if not isinstance(evidence_text, str):
            raise ValueError('Source card evidence text must be a string')
        if len(evidence_text) > 14_000:
            card.update(status='normalized_record_too_large', eligible=False,
                        fields={}, evidence_text='',
                        limitation=(card.get('limitation', '') +
                            ' Normalized selected fields exceeded the 14000-character model-facing limit.').strip())
        identity = {k: card.get(k) for k in ('provider', 'identifier', 'manuscript_block_id',
                    'cutoff', 'raw_sha256', 'record_version', 'status', 'evidence_text')}
        stored = dict(card)
        stored['evidence_id'] = 'source-' + digest(json.dumps(identity, sort_keys=True))[:20]
        with (self.state / 'cards.lock').open('a', encoding='utf-8') as lock:
            exclusive_lock(lock)
            path = self.state / 'cards.json'
            cards = read(path) if path.exists() else []
            if not any(c['evidence_id'] == stored['evidence_id'] for c in cards):
                cards.append(stored)
                write(path, cards)
        return stored

    def fetch(self, url):
        check_url(url)
        key = digest(url)
        meta, raw_path = self.cache / (key + '.json'), self.cache / (key + '.raw')
        with (self.state / 'requests.lock').open('a', encoding='utf-8') as lock:
            exclusive_lock(lock)
            budget_path = self.state / 'requests.json'
            budget = read(budget_path) if budget_path.exists() else {'limit': self.limit, 'requests': []}
            if budget['limit'] != self.limit:
                raise ValueError('Shared source request allowance changed during a run')
            if meta.exists() and raw_path.exists():
                record, raw = read(meta), raw_path.read_bytes()
                if record.get('source_url') != url or digest(raw) != record.get('raw_sha256'):
                    return {'status': 'cache_invalid', 'source_url': url}
                return {**record, 'data': json.loads(raw), 'cache_hit': True}
            if len(budget['requests']) >= self.limit:
                return {'status': 'budget_exhausted', 'source_url': url}
            now = datetime.datetime.now(datetime.timezone.utc).isoformat()
            receipt = {'source_url': url, 'requested_at': now, 'status': 'started'}
            budget['requests'].append(receipt)
            write(budget_path, budget)
            try:
                answer = retrieve(url, opener=self.opener,
                                  headers={'Accept': 'application/json'})
                if answer['status'] != 'ok':
                    receipt['status'] = answer['status']
                    if 'http_status' in answer:
                        receipt['http_status'] = answer['http_status']
                    return {name: value for name, value in answer.items() if name != 'raw'}
                raw = answer['raw']
                data = json.loads(raw)
                record = {'status': 'ok', 'source_url': url, 'retrieved_at': now, 'raw_sha256': digest(raw)}
                raw_path.write_bytes(raw)
                write(meta, record)
                receipt.update(status='ok', raw_sha256=record['raw_sha256'])
                return {**record, 'data': data, 'cache_hit': False}
            except (OSError, ValueError) as exc:
                receipt['status'] = 'unavailable'
                return {'status': 'unavailable', 'source_url': url, 'error_type': type(exc).__name__}
            finally:
                write(budget_path, budget)

    def lookup(self, provider, identifier, block_id):
        url = request(provider, identifier)
        if url is None:
            return {'status': 'not_implemented', 'provider': provider,
                    'limitation': 'This catalog resource has no active PRA adapter.'}
        denied = self.reserve_target(block_id, provider)
        if denied:
            return denied
        record = self.fetch(url)
        data = record.pop('data', None)
        card = {**record, 'provider': provider, 'identifier': identifier, 'manuscript_block_id': block_id,
                'cutoff': self.cutoff, 'record_version': None,
                'source_publication_date': None, 'fields': {}, 'evidence_text': '',
                'limitation': PROVIDERS[provider][1], 'eligible': False}
        if card['status'] == 'ok':
            try:
                fields, version, published, resolved = parse(provider, identifier, data)
                card.update(fields=fields, record_version=version, source_publication_date=published)
                if resolved is None:
                    card['status'] = 'ambiguous' if fields else 'not_found'
                elif str(resolved).casefold() != identifier.casefold():
                    card['status'] = 'identity_mismatch'
                else:
                    card['eligible'] = True
                text = '\n'.join(k + ': ' + json.dumps(v, ensure_ascii=False, sort_keys=True) for k, v in fields.items())
                if len(text) > 14000:
                    card.update(status='normalized_record_too_large', eligible=False, fields={})
                else:
                    card['evidence_text'] = text
            except (KeyError, IndexError, TypeError, ValueError, AttributeError):
                card.update(status='schema_error', eligible=False, fields={}, evidence_text='')
        return self.store_card(card)

    def cards(self):
        path = self.state / 'cards.json'
        return read(path) if path.exists() else []


def validate_references(review, cards):
    by = {c['evidence_id']: c for c in cards}
    checked = 0
    for finding in review.get('findings', []):
        for ref in finding.get('source_evidence', []):
            card = by.get(ref.get('evidence_id'))
            if (not card or not card.get('eligible') or not ref.get('quote')
                    or ref['quote'] not in card['evidence_text']):
                raise ValueError('External finding evidence is unavailable, ineligible or misquoted')
            checked += 1
    return {'checked_references': checked, 'limitation': 'Source identity and exact quotations are checked, not scientific interpretation.'}


def bind_sources(review, cards):
    """Carry verified source records with findings through revision and Word export."""
    import copy
    validate_references(review, cards)
    result = copy.deepcopy(review)
    by = {c['evidence_id']: c for c in cards}
    for finding in result.get('findings', []):
        ids = dict.fromkeys(r['evidence_id'] for r in finding.get('source_evidence', []))
        finding['external_sources'] = [copy.deepcopy(by[evidence_id]) for evidence_id in ids]
    result['source_cards'] = copy.deepcopy(cards)
    return result
