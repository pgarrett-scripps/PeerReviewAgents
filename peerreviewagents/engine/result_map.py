"""Inspectable result links and bounded comparisons of reported quantities.

The map is built over the manuscript base: one node per piece that names a
figure or a table or carries a table's numbers, found by the line range it
occupies. A node has no id — it is its document and its lines, which is what a
finding cites and what ``read_paper`` printed — and a reviewer asking what a
claim is linked to asks with a line number.
"""
import json
import re
from decimal import Decimal, InvalidOperation

from . import paper_base, quotes
from .storage import digest

VERSION = 'result-map-v2'
REF = re.compile(r'\b(?:(?P<kind>Fig(?:ure)?s?\.?|Tables?)\s*(?P<number>S?\d+)(?P<panel>[a-z])?|(?P<reverse_number>S\d+)\s*(?P<reverse_kind>Fig(?:ure)?s?\.?|Tables?))\b', re.I)
NUMBER = re.compile(r'(?<![\w.])[-−+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?(?:\s*%)?')
MODEL_PATTERNS = {
    'simple_linear_regression': r'\bSLR\b|\bsimple linear regression',
    'multiple_regression': r'\bMR\b|\bmultiple regression',
    'generalized_least_squares': r'\bGLS\b|\bgeneralized least squares',
    'log_binomial': r'\blog.binomial',
}
DIMENSIONS = ('metric', 'outcome', 'model', 'population', 'denominator', 'contrast', 'timepoint', 'units', 'scale')
#: What the base prints before a caption's own first word: ``Figure:`` or
#: ``Caption:`` on a caption line, and the pipe and padding of the first cell of
#: a Markdown table. A display declares itself in the words after that, so the
#: file's own markup is stripped before the label is read — the label is the
#: paper's, the prefix is the format's, and matching ``Table 2`` against
#: ``| Table 2 …`` would otherwise find no declaration anywhere in the base.
CAPTION_HEAD = re.compile(r'^(?:\s*\|\s*|\s*#{1,6}\s*|\s*(?:Figure|Caption):\s*)+')
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


def _where(node):
    """A node's identity: the document it is in and the line it starts on."""
    return (node['document'], node['line'])


def build_result_map(base):
    nodes, definitions = [], {}
    for row in base.rows:
        if row.get('kind') == 'reference' or str(row.get('section') or '').lower() in ('references', 'bibliography'):
            continue
        document = row.get('document') or paper_base.MAIN_DOCUMENT
        start, end = row['lines']
        text = base.range_text(start, end, document)
        refs = references(text)
        numeric = list(NUMBER.finditer(text))
        head = CAPTION_HEAD.sub('', text.strip())
        caption = REF.match(head) if row.get('kind') in ('caption','table','figure',None) else None
        if not refs and not (numeric and row.get('kind') == 'table'):
            continue
        declared = None
        node = {'document': document, 'line': start, 'end_line': end, 'lines': [start, end],
                'fingerprint': row.get('fingerprint'), 'kind': row.get('kind', 'paragraph'),
                'section': row.get('section'), 'page': row.get('page'), 'bbox': row.get('bbox'),
                'declares': None, 'references': refs, 'text': text,
                'model_labels': [name for name, pattern in MODEL_PATTERNS.items() if re.search(pattern, text, re.I)],
                'numbers': [{'text': m[0], 'start': m.start(), 'end': m.end()} for m in numeric],
                'visual_check': 'not_assessed'}
        if caption:
            declared = references(head)[0]['target']
            definitions.setdefault(declared, []).append(_where(node))
            node['declares'] = declared
            if declared.startswith('figure-'):
                node['visual_check'] = 'required_for_plotted_values'
        nodes.append(node)
    edges = []
    warnings = []
    by = {_where(node): node for node in nodes}
    for node in nodes:
        seen = set()
        for ref in node['references']:
            if ref['target'] in seen:
                continue
            seen.add(ref['target'])
            targets = [where for where in definitions.get(ref['target'], []) if where != _where(node)]
            if ref['target'] == node['declares']:
                continue
            edges.append({'document': node['document'], 'from': node['line'],
                          'target': ref['target'],
                          'to': [{'document': name, 'line': line} for name, line in targets],
                          'relation': 'explicit_reference', 'quote': ref['quote'],
                          'status': 'linked' if targets else 'target_not_extracted'})
            for where in targets:
                other = by[where]
                left, right = set(node['model_labels']), set(other['model_labels'])
                if node['declares'] and left and right and left.isdisjoint(right):
                    warnings.append({'document': node['document'],
                        'lines': [node['line'], other['line']], 'status': 'needs_comparability_check',
                        'reason': 'Linked display descriptions name different model classes. Check model identity before comparing values.',
                        'scientifically_verified': False})
    return {'version': VERSION, 'paper_id': base.paper_id,
            'source_sha256': (base.manifest.get('source') or {}).get('sha256'),
            'nodes': nodes, 'edges': edges, 'checks': warnings,
            'coverage': {'pieces_total': len(base.rows), 'mapped_pieces': len(nodes),
                         'unresolved_links': sum(not e['to'] for e in edges),
                         'ambiguous_definitions': [k for k,v in definitions.items() if len(v)>1],
                         'figure_pixels_transcribed': False},
            'limitation': 'Literal links and numbers are extracted, not scientifically interpreted. Unlinked claims, display ranges and image-only values can be missed. A missing target means extraction is incomplete, not that the manuscript omitted evidence.'}


def _node_at(nodes, line, document):
    """The mapped node whose lines hold this line, or ``None``."""
    for node in nodes:
        if node['document'] == document and node['line'] <= int(line) <= node['end_line']:
            return node
    return None


def inspect_results(mapping, line=None, document=paper_base.MAIN_DOCUMENT, max_chars=16000):
    """What the map holds, or what one line of it is linked to.

    *line* is any line of a piece, not only its first: a reviewer citing the
    row of a table asks about the table.
    """
    nodes = mapping['nodes']
    chosen = _node_at(nodes, line, document) if line else None
    if line and chosen is None:
        return {'nodes': [], 'notice': 'No mapped display reference on this line. Read the lines themselves.',
                'coverage': mapping['coverage'], 'limitation': mapping['limitation']}
    if chosen is not None:
        here = _where(chosen)
        wanted = {here}
        for edge in mapping['edges']:
            ends = {(edge['document'], edge['from'])} | {(one['document'], one['line'])
                                                         for one in edge['to']}
            if here in ends:
                wanted |= ends
        ordered = [chosen] + [node for node in nodes
                              if _where(node) in wanted and _where(node) != here]
    else:
        priority = {(check['document'], number)
                    for check in mapping['checks'] for number in check['lines']}
        ordered = sorted(nodes, key=lambda node: (_where(node) not in priority,
                                                  not bool(node['declares']),
                                                  -len(node['references'])))
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
    chosen_where = {_where(node) for node in selected}
    return {'version': VERSION, 'nodes': selected,
            'edges': [e for e in mapping['edges'] if (e['document'], e['from']) in chosen_where],
            'checks': [c for c in mapping['checks']
                       if any((c['document'], number) in chosen_where for number in c['lines'])],
            'coverage': mapping['coverage'], 'nodes_omitted_from_view': len(nodes)-len(selected),
            'limitation': mapping['limitation']}


#: An observation's anchor and its value, beside the dimensions. ``document``
#: is optional and defaults to the main document, because most comparisons are
#: between two lines of the paper itself.
ANCHOR_FIELDS = {'line', 'quote', 'value'}


def compare_observations(base, observations):
    """Check quoted quantities without treating investigator labels as verified facts."""
    if not isinstance(observations, list) or len(observations) != 2:
        raise ValueError('Supply exactly two textual observations')
    checked, values = [], []
    for obs in observations:
        required = set(DIMENSIONS) | ANCHOR_FIELDS
        if not isinstance(obs, dict) or not (required <= set(obs) <= required | {'document'}):
            raise ValueError('Each observation requires an anchor, value and all comparison dimensions')
        line = obs['line']
        if not isinstance(line, int) or isinstance(line, bool) or line < 1:
            raise ValueError('Each observation is anchored on a line of the manuscript')
        document = str(obs.get('document') or paper_base.MAIN_DOCUMENT)
        if any(not isinstance(obs[field], str) or not obs[field].strip() or len(obs[field]) > 350
               for field in set(obs) - {'line'}):
            raise ValueError('Observation fields must be short nonempty strings, using unknown when unresolved')
        if document not in base.documents:
            raise ValueError('Observation names a document this manuscript does not hold')
        if not quotes.placements(base, obs['quote'], line, None, document):
            raise ValueError('Observation quotation is not on the manuscript line it names')
        tokens = [m[0].strip() for m in NUMBER.finditer(obs['quote'])]
        if obs['value'] not in tokens:
            raise ValueError('Observation value must be a complete numeric token in the exact quote')
        try:
            values.append(Decimal(obs['value'].replace('−','-').replace('%','').strip()))
        except InvalidOperation as exc:
            raise ValueError('Observation is not a finite reported number') from exc
        if not values[-1].is_finite():
            raise ValueError('Observation is not finite')
        checked.append({**obs, 'document': document})
    unknown = [d for d in DIMENSIONS if any(o[d].casefold() in ('unknown','unresolved','not reported') for o in checked)]
    different = [d for d in DIMENSIONS if checked[0][d].casefold()!=checked[1][d].casefold()]
    status = 'incomplete_context' if unknown else 'different_context' if different else 'same_reported_value' if values[0]==values[1] else 'needs_reconciliation'
    result = {'observations': checked, 'status': status, 'unknown_dimensions': unknown,
              'different_dimensions': different, 'scientifically_verified': False,
              'dimension_provenance': 'Investigator labels, not independently validated',
              'limitation': 'Exact text and numeric tokens are checked. Comparability, image values and scientific correctness are not established. Different contexts need not yield equal values.'}
    result['comparison_id'] = 'comparison-' + digest(json.dumps(result,sort_keys=True))[:16]
    return result
