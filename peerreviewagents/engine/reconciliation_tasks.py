"""Locate explicit reconciliation targets before a model chooses its concerns."""
import argparse
import json
import re
from pathlib import Path

from .annotations import verify_workspace
from .storage import digest, read, write


FIGURE = re.compile(r'\b(?:(S\d+)\s+Fig(?:ure)?\.?|Fig(?:ure)?s?\.?\s*(S?\d+))', re.I)
TABLE = re.compile(r'\bS\s*(\d+)\s*Table\b', re.I)
STATISTICAL = re.compile(r'\bsignifican\w*\b|\bp\s*[<=>]', re.I)
STAGE = re.compile(r'\b(?:postembryonic|embryonic|larval|pupal|pupation|eclosion|hatching|mortality|lethality|survival)\b', re.I)
EXCLUSIVE = re.compile(r'\b(?:exclusively|only|specifically)\b', re.I)
MEASUREMENT = re.compile(r'\b(?:scored|measured|counted|recorded|quantified|assayed)\b', re.I)


def sentences(text):
    """Return exact sentence spans, without claiming atomic scientific parsing."""
    return [s for s in re.split(r'(?<=[.!?])\s+(?=[A-Z(])', text) if s.strip()]


def figure_ids(text):
    return {next(part for part in match if part).upper() for match in FIGURE.findall(text)}


def stage_claim(text):
    return any(max(stage.start(), qualifier.start()) - min(stage.end(), qualifier.end()) <= 30
               for stage in STAGE.finditer(text) for qualifier in EXCLUSIVE.finditer(text))


def build_tasks(paper, catalog):
    blocks = [b for b in paper['blocks'] if b['kind'] not in ('reference','table')]
    books = {re.fullmatch(r'S(\d+)_Table\.xlsx', b['file'], re.I).group(1):b
             for b in catalog['books'] if re.fullmatch(r'S(\d+)_Table\.xlsx', b['file'], re.I)}
    tasks = []
    for block in blocks:
        for quote in sentences(block['text']):
            kind = None
            figures = figure_ids(block['text'][:100])
            if block['kind'] == 'caption' and figures and STATISTICAL.search(quote):
                kind = 'caption_results_table'
            elif block['kind'] != 'caption' and stage_claim(quote):
                kind = 'stage_endpoint'
            if not kind:
                continue
            if kind == 'caption_results_table':
                linked = [b for b in blocks if b['kind'] != 'caption'
                          and figures.intersection(figure_ids(b['text']))]
            else:
                linked = [b for b in blocks if b['id'] != block['id']
                          and MEASUREMENT.search(b['text']) and STAGE.search(b['text'])]
            table_numbers = set(TABLE.findall(block['text']))
            table_numbers.update(n for b in linked for n in TABLE.findall(b['text']))
            tasks.append({
                'id':'reconcile-' + digest(block['id'] + '\n' + quote)[:12],
                'kind':kind, 'target':{'block_id':block['id'], 'quote':quote},
                'related_block_ids':[b['id'] for b in linked],
                'reported_table_ids':[books[n]['id'] for n in sorted(table_numbers) if n in books],
                'unavailable_referenced_tables':['S' + n + ' Table' for n in sorted(table_numbers) if n not in books],
                'task':'Compare this exact statement with the linked source candidates. Match the scientific outcome, comparison, and measurement before deciding whether they agree. Record an unresolved outcome if decisive evidence is unavailable.',
            })
    return {'version':'reconciliation-targets-v1', 'tasks':tasks,
            'limitations':[
                'Targets come from explicit language patterns, not a complete inventory of scientific claims.',
                'Figure mentions and measurement words identify candidate links, not verified semantic relationships.',
                'A target is a request to check evidence, not an allegation of an error.',
                'Every target needs a disposition. Unresolved targets remain open and must not be described as verified.']}


def audit_tasks(packet, response, paper, catalog):
    """Reject self-reported completion without target-specific source coverage."""
    by = {b['id']:b['text'] for b in paper['blocks'] + catalog['blocks']}
    table_books = {b['id']:b['id'].split('-')[0] for b in catalog['blocks']}
    rows = response.get('reconciliation_results', [])
    records = []
    for task in packet['tasks']:
        matches = [r for r in rows if isinstance(r, dict) and r.get('task_id') == task['id']]
        problems = []
        row = matches[0] if len(matches) == 1 else {}
        if len(matches) != 1:
            problems.append('Missing or duplicate target disposition')
        status = row.get('status')
        if status not in ('compared','unresolved','not_applicable'):
            problems.append('Unknown comparison status')
        if not isinstance(row.get('reason'), str) or not row['reason'].strip():
            problems.append('Missing source-based explanation')
        refs = row.get('evidence', [])
        if not isinstance(refs, list):
            refs = []
            problems.append('Evidence must be a list')
        verified = []
        for ref in refs:
            if (isinstance(ref, dict) and isinstance(ref.get('quote'), str) and ref['quote']
                    and ref['quote'] in by.get(ref.get('block_id'), '')):
                verified.append(ref)
            else:
                problems.append('Evidence quotation does not match its source')
        if status == 'compared':
            if not any(ref['block_id'] == task['target']['block_id']
                       and len(ref['quote']) >= min(20,len(task['target']['quote']))
                       and ref['quote'] in task['target']['quote'] for ref in verified):
                problems.append('Comparison did not cite its exact target statement')
            if not any(ref['block_id'] in task['related_block_ids'] for ref in verified):
                problems.append('Comparison lacks linked manuscript evidence')
            if task['kind'] == 'caption_results_table' and not any(
                    table_books.get(ref['block_id']) in task['reported_table_ids'] for ref in verified):
                problems.append('Caption comparison lacks linked reported table cells')
            for key in ('outcome','comparison','measurement_or_test','strongest_author_defense'):
                if not isinstance(row.get(key),str) or not row[key].strip():
                    problems.append('Missing matched context: ' + key)
        records.append({'task_id':task['id'], 'status':status if not problems else 'unresolved',
                        'valid_record':not problems, 'problems':problems})
    expected = {t['id'] for t in packet['tasks']}
    unexpected = [r.get('task_id') if isinstance(r,dict) else None for r in rows
                  if not isinstance(r,dict) or r.get('task_id') not in expected]
    return {'records':records, 'unexpected_task_ids':unexpected,
            'accounting_valid':all(r['valid_record'] for r in records) and not unexpected,
            'all_targets_compared':bool(records) and not unexpected and all(
                r['valid_record'] and r['status'] == 'compared' for r in records),
            'limitation':'Passing verifies explicit source coverage and accounting. It does not verify reasoning, table-header interpretation, or scientific correctness.'}


def task_prompt(packet):
    return '''Before closing an evidence check, return one reconciliation_results entry per target ID below. Each entry needs task_id, status (compared, unresolved, or not_applicable), reason, outcome, comparison, measurement_or_test, strongest_author_defense, and evidence entries with block_id and exact quote. A compared caption task must cite the target caption statement, a linked manuscript passage, and linked table cells. A compared stage task must cite the specific stage claim and linked measurement evidence. Read actual source content before deciding whether candidate links concern the same experiment. Unavailable or inapplicable evidence may leave a target unresolved without becoming a review finding. Never substitute a different consistent claim for the requested target.\n''' + json.dumps(packet, ensure_ascii=False)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('workspace', type=Path)
    parser.add_argument('--catalog', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    packet = build_tasks(verify_workspace(args.workspace), read(args.catalog))
    write(args.output, packet)
    print(json.dumps({'tasks':len(packet['tasks']), 'output':str(args.output)}))
