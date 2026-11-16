"""Versioned project skills with bounded, manuscript-grounded routing."""
import json
import re
from pathlib import Path

from . import paper_base
from .storage import digest

ROOT = Path(__file__).parent / 'domains'


def catalog(root=ROOT):
    packages = []
    ids, codes = set(), set()
    for path in sorted(Path(root).glob('*/manifest.json')):
        item = json.loads(path.read_text(encoding='utf-8'))
        ident = item.get('id', '')
        if (item.get('schema_version') != 1 or not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', ident)
                or path.parent.name != ident or ident in ids or item.get('catalog_id') in codes):
            raise ValueError('Invalid or duplicate domain manifest: ' + str(path))
        for key in ('name', 'description', 'catalog_id'):
            if not isinstance(item.get(key), str) or not item[key].strip():
                raise ValueError('Missing domain field: ' + key)
        for key in ('triggers', 'skills', 'providers', 'limitations'):
            if not isinstance(item.get(key), list) or any(not isinstance(v, str) or not v.strip() for v in item[key]):
                raise ValueError('Invalid domain list: ' + key)
        if not item['skills'] or not item['triggers'] or not isinstance(item.get('maturity'), dict):
            raise ValueError('Incomplete domain manifest')
        instructions = []
        for relative in item['skills']:
            skill = (path.parent / relative).resolve()
            if not skill.is_relative_to(path.parent.resolve()) or not skill.is_file():
                raise ValueError('Invalid domain skill path')
            text = skill.read_text(encoding='utf-8')
            if not text.startswith('---\n') or '\n---\n' not in text[4:]:
                raise ValueError('Skill frontmatter missing')
            header, body = text[4:].split('\n---\n', 1)
            if not re.search(r'^name: .+', header, re.M) or not re.search(r'^description: .+', header, re.M):
                raise ValueError('Skill identity missing')
            instructions.append({'path': relative, 'sha256': digest(text), 'text': body.strip()})
        ids.add(ident)
        codes.add(item['catalog_id'])
        packages.append({**item, 'manifest_sha256': digest(path.read_bytes()), 'instructions': instructions})
    return packages


def route(base, packages=None, limit=2):
    """Which domain packages this manuscript's own narrative asks for.

    The evidence is a line of the manuscript and the phrase found on it, so a
    routing decision can be read back against what ``read_paper`` printed.
    """
    if type(limit) is not int or not 1 <= limit <= 4:
        raise ValueError('Load between one and four domain packages')
    packages = catalog() if packages is None else packages
    narrative = []
    for row in base.rows:
        if row.get('kind') in ('reference', 'bibliography'):
            continue
        if re.search(r'\b(references|bibliography)\b', str(row.get('section') or ''), re.I):
            continue
        document = row.get('document') or paper_base.MAIN_DOCUMENT
        start, end = row['lines']
        narrative.append((document, start, base.range_text(start, end, document)))
    matches = []
    for item in packages:
        evidence = []
        for phrase in item['triggers']:
            expression = re.compile(r'(?<!\w)' + re.escape(phrase) + r'(?!\w)', re.I)
            for document, line, text in narrative:
                hit = expression.search(text)
                if hit:
                    evidence.append({'document': document, 'line': line,
                                     'quote': hit.group(), 'trigger': phrase})
                    break
        if evidence:
            matches.append({'id': item['id'], 'name': item['name'], 'evidence': evidence,
                            'score': len(evidence), 'manifest_sha256': item['manifest_sha256']})
    matches.sort(key=lambda m: (-m['score'], m['id']))
    return {'version': 'domain-routing-v1', 'selected': matches[:limit], 'other_matches': matches[limit:],
            'limitation': 'Lexical routing identifies candidate expertise, not a validated design classification. '
                          'Apply only methods supported by the manuscript. Unmatched papers use shared review guidance.'}


def context(profile, packages=None, max_chars=18000):
    packages = catalog() if packages is None else packages
    by = {p['id']: p for p in packages}
    selected = []
    for match in profile['selected']:
        item = by[match['id']]
        selected.append({'domain': item['name'], 'routing_evidence': match['evidence'],
                         'instructions': [i['text'] for i in item['instructions']],
                         'limitations': item['limitations'], 'maturity': item['maturity']})
    text = json.dumps(selected, ensure_ascii=False)
    if len(text) > max_chars:
        raise ValueError('Selected domain instructions exceed context allowance')
    return '\nSELECTED DOMAIN GUIDANCE:\n' + text + '\n' + profile['limitation']
