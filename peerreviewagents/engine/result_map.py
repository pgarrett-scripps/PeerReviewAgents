"""Inspectable result links and bounded comparisons of reported quantities."""
import json
import re
from decimal import Decimal, InvalidOperation
from .storage import digest

VERSION = 'result-map-v1'
REF = re.compile(r'\b(?:(?P<kind>Fig(?:ure)?s?\.?|Tables?)\s*(?P<number>S?\d+)(?P<panel>[a-z])?|(?P<reverse_number>S\d+)\s*(?P<reverse_kind>Fig(?:ure)?s?\.?|Tables?))\b', re.I)
NUMBER = re.compile(r'(?<![\w.])[-−+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?(?:\s*%)?')
MODEL_PATTERNS = {
    'simple_linear_regression': r'\bSLR\b|\bsimple linear regression',
    'multiple_regression': r'\bMR\b|\bmultiple regression',
    'generalized_least_squares': r'\bGLS\b|\bgeneralized least squares',
    'log_binomial': r'\blog.binomial',
}
DIMENSIONS = ('metric', 'outcome', 'model', 'population', 'denominator', 'contrast', 'timepoint', 'units', 'scale')
INSTRUCTION = '''RESULT COMPARISON WORKFLOW:
Use the result map to trace a central claim to its cited tables and figures. Compare model identity, outcome, population or denominator, timepoint, units, scale and effect label before comparing values. Different fitted models or transformations can legitimately yield different coefficients. Check figure pixels when a value or label is only in the image. A caption is not a transcription of plotted values. Inspect_results returns linked source context. Record_comparison saves two exact textual observations and their comparability limits, not a confirmed defect. If figures are unavailable, mark the visual check incomplete. Select important issues rather than filling the review with every discrepancy candidate. No raw-data rerun is required.
'''


def references(text):
    result = []
    for match in REF.finditer(text):
        kind = match['kind'] or match['reverse_kind']
        number = match['number'] or match['reverse_number']
        result.append({'target': ('figure-' if kind.lower().startswith('fig') else 'table-')+number.upper(),
                       'quote':match[0], 'start':match.start(), 'end':match.end(), 'panel':(match['panel'] or '').lower()})
    return result


def build_result_map(paper):
    nodes, definitions = [], {}
    for block in paper['blocks']:
        if block.get('kind') == 'reference' or str(block.get('section', '')).lower() in ('references', 'bibliography'):
            continue
        text = block['text']
        refs = references(text)
        numeric = list(NUMBER.finditer(text))
        caption = REF.match(text.strip()) if block.get('kind') in ('caption','table','figure',None) else None
        if not refs and not (numeric and block.get('kind') == 'table'):
            continue
        declared = None
        if caption:
            declared = references(text.strip())[0]['target']
            definitions.setdefault(declared, []).append(block['id'])
        node = {'block_id': block['id'], 'text_sha256': digest(text), 'kind': block.get('kind', 'paragraph'),
                'section': block.get('section'), 'page': block.get('page'), 'declares': declared,
                'references': refs, 'text': text,
                'model_labels': [name for name, pattern in MODEL_PATTERNS.items() if re.search(pattern, text, re.I)],
                'numbers': [{'text': m[0], 'start': m.start(), 'end': m.end()} for m in numeric],
                'visual_check': 'required_for_plotted_values' if declared and declared.startswith('figure-') else 'not_assessed'}
        nodes.append(node)
    edges = []
    warnings = []
    by = {n['block_id']: n for n in nodes}
    for node in nodes:
        seen = set()
        for ref in node['references']:
            if ref['target'] in seen:
                continue
            seen.add(ref['target'])
            targets = [bid for bid in definitions.get(ref['target'], []) if bid != node['block_id']]
            if ref['target'] == node['declares']:
                continue
            edges.append({'from': node['block_id'], 'target': ref['target'], 'to': targets,
                          'relation': 'explicit_reference', 'quote': ref['quote'],
                          'status': 'linked' if targets else 'target_not_extracted'})
            for bid in targets:
                other = by[bid]
                left, right = set(node['model_labels']), set(other['model_labels'])
                if node['declares'] and left and right and left.isdisjoint(right):
                    warnings.append({'blocks': [node['block_id'], bid], 'status': 'needs_comparability_check',
                        'reason': 'Linked display descriptions name different model classes. Check model identity before comparing values.',
                        'scientifically_verified': False})
    return {'version': VERSION, 'paper_id': paper['id'], 'source_sha256': paper.get('source_sha256'),
            'nodes': nodes, 'edges': edges, 'checks': warnings,
            'coverage': {'blocks_total': len(paper['blocks']), 'mapped_blocks': len(nodes),
                         'unresolved_links': sum(not e['to'] for e in edges),
                         'ambiguous_definitions': [k for k,v in definitions.items() if len(v)>1],
                         'figure_pixels_transcribed': False},
            'limitation': 'Literal links and numbers are extracted, not scientifically interpreted. Unlinked claims, display ranges and image-only values can be missed. A missing target means extraction is incomplete, not that the manuscript omitted evidence.'}


def inspect_results(mapping, block_id=None, max_chars=16000):
    nodes = mapping['nodes']
    by = {n['block_id']: n for n in nodes}
    if block_id is not None and block_id not in by:
        return {'nodes': [], 'notice': 'No mapped display reference at this block. Read the source block directly.',
                'coverage': mapping['coverage'], 'limitation': mapping['limitation']}
    if block_id:
        ids = {block_id}
        for edge in mapping['edges']:
            if edge['from'] == block_id or block_id in edge['to']:
                ids.add(edge['from'])
                ids.update(edge['to'])
        ordered = [by[block_id]] + [n for n in nodes if n['block_id'] in ids and n['block_id'] != block_id]
    else:
        priority = {bid for c in mapping['checks'] for bid in c['blocks']}
        ordered = sorted(nodes, key=lambda n: (n['block_id'] not in priority, not bool(n['declares']), -len(n['references'])))
    selected, used = [], 0
    for node in ordered[:12]:
        excerpt = node['text'][:1800]
        item = {k:v for k,v in node.items() if k not in ('text', 'numbers')}
        item.update(text=excerpt, text_truncated=len(excerpt)<len(node['text']))
        length = len(json.dumps(item, ensure_ascii=False))
        if used + length > max_chars:
            break
        selected.append(item)
        used += length
    selected_ids = {n['block_id'] for n in selected}
    return {'version': VERSION, 'nodes': selected,
            'edges': [e for e in mapping['edges'] if e['from'] in selected_ids],
            'checks': [c for c in mapping['checks'] if any(b in selected_ids for b in c['blocks'])],
            'coverage': mapping['coverage'], 'nodes_omitted_from_view': len(nodes)-len(selected),
            'limitation': mapping['limitation']}


def compare_observations(paper, observations):
    """Check quoted quantities without treating investigator labels as verified facts."""
    if not isinstance(observations, list) or len(observations) != 2:
        raise ValueError('Supply exactly two textual observations')
    by = {b['id']: b['text'] for b in paper['blocks']}
    checked, values = [], []
    for obs in observations:
        if not isinstance(obs, dict) or set(obs) != set(DIMENSIONS) | {'block_id','quote','value'}:
            raise ValueError('Each observation requires an anchor, value and all comparison dimensions')
        if any(not isinstance(v,str) or not v.strip() or len(v)>350 for v in obs.values()):
            raise ValueError('Observation fields must be short nonempty strings, using unknown when unresolved')
        if obs['quote'] not in by.get(obs['block_id'],''):
            raise ValueError('Observation quotation does not match its manuscript block')
        tokens = [m[0].strip() for m in NUMBER.finditer(obs['quote'])]
        if obs['value'] not in tokens:
            raise ValueError('Observation value must be a complete numeric token in the exact quote')
        try:
            values.append(Decimal(obs['value'].replace('−','-').replace('%','').strip()))
        except InvalidOperation as exc:
            raise ValueError('Observation is not a finite reported number') from exc
        if not values[-1].is_finite():
            raise ValueError('Observation is not finite')
        checked.append(dict(obs))
    unknown = [d for d in DIMENSIONS if any(o[d].casefold() in ('unknown','unresolved','not reported') for o in checked)]
    different = [d for d in DIMENSIONS if checked[0][d].casefold()!=checked[1][d].casefold()]
    status = 'incomplete_context' if unknown else 'different_context' if different else 'same_reported_value' if values[0]==values[1] else 'needs_reconciliation'
    result = {'observations': checked, 'status': status, 'unknown_dimensions': unknown,
              'different_dimensions': different, 'scientifically_verified': False,
              'dimension_provenance': 'Investigator labels, not independently validated',
              'limitation': 'Exact text and numeric tokens are checked. Comparability, image values and scientific correctness are not established. Different contexts need not yield equal values.'}
    result['comparison_id'] = 'comparison-' + digest(json.dumps(result,sort_keys=True))[:16]
    return result
