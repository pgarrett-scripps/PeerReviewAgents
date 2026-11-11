"""Trace targeted investigation to actual tool results without grading truth."""
import copy
from .review_schema import obj, STRING

VERSION = 'targeted-check-v2'
INSTRUCTION = '''TARGETED INVESTIGATION: Before finalizing, identify one or two consequential scientific questions and use at least one manuscript tool to test a question. Prefer evidence not already visible, such as an original table or figure, a cross-section numerical comparison, or a focused literature or exact-record check. Choose tools according to the question and selected domain guidance. Do not perform an irrelevant database lookup to satisfy this requirement. Stay within the existing tool allowance. Do not rerun analyses or inspect public review history.
Try to disprove a suspected problem as well as support it. A check may confirm the authors or narrow or remove your concern. Report tool_checks with the question, the returned tool_receipt call_index and tool, outcome, and what the returned evidence changes or leaves unresolved. Never invent receipts. save_note alone is not an evidence check. If tools fail or are unavailable, preserve useful review content and state that limitation. An empty tool_checks array is permitted only with an explicit explanation in investigation_summary. It is recorded as an incomplete investigation, not silently accepted as a tools-enabled review.'''


def with_tool_checks(schema, cap=4):
    """Add the reported tool checks. ``cap`` is the policy's allowance, None for no cap."""
    result = copy.deepcopy(schema)
    checks = {'type': 'array',
        'items': obj({'question': STRING, 'call_index': {'type': 'integer', 'minimum': 1},
                      'tool': STRING, 'outcome': {'type': 'string',
                      'enum': ['supported', 'narrowed', 'rejected', 'unresolved']},
                      'interpretation': STRING})}
    if cap is not None:
        checks = {'type': 'array', 'maxItems': cap, 'items': checks['items']}
    result['properties']['tool_checks'] = checks
    result['required'].append('tool_checks')
    return result


def audit_tool_checks(review, rows, supplied_blocks=None, version=VERSION):
    """Check receipt provenance only, never infer scientific value from a call."""
    if version not in ('targeted-check-v1',VERSION):
        raise ValueError('Unknown investigation audit version')
    receipts = {}
    for row in rows:
        result = row.get('result', {})
        receipt = result.get('tool_receipt', {})
        index = receipt.get('call_index')
        if type(index) is int and receipt.get('tool') == row.get('tool'):
            receipts.setdefault(index, []).append(row)
    checks = []
    for check in review.get('tool_checks', []):
        rows_for_index = receipts.get(check.get('call_index'), [])
        row = rows_for_index[0] if len(rows_for_index) == 1 else {}
        result = row.get('result', {})
        linked = bool(row) and row.get('tool') == check.get('tool')
        available = linked and not any(k in result for k in
            ('request_rejected', 'unavailable', 'notice', 'error'))
        if 'eligible' in result and not result['eligible']:
            available = False
        if version==VERSION and row.get('tool') in ('search','read_blocks'):
            available=available and bool(result.get('matches',result.get('blocks',[])))
        substantive = bool(str(check.get('question', '')).strip() and
                           str(check.get('interpretation', '')).strip())
        eligible = available and check.get('tool') != 'save_note' and substantive
        operation = evidence_operation(row, supplied_blocks) if available else 'no_usable_result'
        checks.append({**check, 'receipt_verified': linked,
                       'evidence_returned': available, 'counts_as_check': eligible,
                       'evidence_operation': operation})
    return {'version': version, 'complete': any(c['counts_as_check'] for c in checks),
            'actual_tool_calls': len(rows), 'checks': checks,
            'limitation': 'Receipts verify execution and reported interpretation, not correctness, novelty or improvement in review quality.'}


def evidence_operation(row, supplied_blocks=None):
    """Describe the operation, without pretending to measure scientific novelty."""
    name = row.get('tool')
    if name in ('view_page', 'view_supplement'):
        return 'visual_inspection'
    if name in ('lookup_source', 'search_literature', 'check_citation', 'resolve_identifier'):
        return 'external_retrieval'
    if name in ('calculate', 'record_comparison'):
        return 'derived_check'
    if name == 'inspect_results':
        return 'reorganized_manuscript'
    if name in ('read_blocks', 'search'):
        result = row.get('result', {})
        blocks = result.get('blocks', result.get('matches', []))
        if supplied_blocks is not None and any(b.get('id') not in supplied_blocks for b in blocks):
            return 'expanded_manuscript_context'
        return 'manuscript_reread'
    return 'note_or_unknown'
