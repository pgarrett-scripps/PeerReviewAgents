"""Local synthesis preflight using the same production validation contract."""
import json
from collections import Counter

from .storage import digest, read, write

VERSION = 'synthesis-preflight-v2'
MAX_CHECKS = 2
MAX_DRAFT_CHARACTERS = 300000
INSTRUCTION = '''
SYNTHESIS PREFLIGHT:
Write the final findings first, then map components to their actual text. Do not map to a planned sentence that you removed or paraphrased. Empty findings are valid only if no component is preserved. For a rejected discarded hypothesis, every component must be resolved with source evidence. If a scientific question remains open, use an honest unresolved or supported status and defer the component as appropriate.
Before submitting, call validate_synthesis with draft_json containing your complete proposed response in the response schema, including evidence handles. It performs local structural checks without external requests. You have at most two checks, separate from your source tool allowance. Fix any reported errors and check once more if needed. Submit the checked draft without further edits. Each check reports component_ledger counts and names every missing, duplicated, misassigned or unknown component ID. When component_ledger is complete, keep every component record unchanged while fixing other errors. Never shorten or drop the component ledger to isolate an error. Every retained secondary finding must include all finding fields, including source_urls and source_evidence arrays. Use empty source arrays only when no external sources were used. Do not null or drop required secondary wording to avoid a shape error. Do not change scientific conclusions merely to satisfy the validator or fabricate missing evidence. Unresolved failures remain failures. A passing check establishes structural validity, not scientific correctness or semantic preservation.
'''


def required_components(ledger):
    return {issue['issue_id']: [component['component_id'] for component in issue['components']]
            for issue in ledger['issues']}


def component_contract(ledger):
    """State the exact component identifiers each known issue decision must account for."""
    return ('\nREQUIRED COMPONENT LEDGER:\nReturn exactly one outer decision for each issue below. In that decision, '
            'give each listed component ID exactly one treatment. Do not move a component to another issue, '
            'invent a component ID, or omit a component to shorten a draft. New candidates are separate and '
            'receive runtime IDs.\n' + json.dumps(required_components(ledger), ensure_ascii=False) + '\n')


def _identifier(value):
    return value if isinstance(value, str) else None


def component_diagnostics(value, ledger):
    """Name every absent or misplaced component without repairing the draft."""
    required = required_components(ledger)
    owner = {component: issue_id for issue_id, components in required.items() for component in components}
    decisions = value.get('decisions') if isinstance(value, dict) else None
    decisions = [d for d in decisions if isinstance(d, dict)] if isinstance(decisions, list) else []
    decided = Counter(_identifier(d.get('issue_id')) for d in decisions)
    problems = []
    accounted = 0
    for issue_id, expected in required.items():
        if not decided[issue_id]:
            problems.append({'issue_id': issue_id, 'code': 'missing_decision', 'component_ids': expected,
                             'message': 'Add one outer decision for this issue with a treatment for each listed component.'})
        elif decided[issue_id] > 1:
            problems.append({'issue_id': issue_id, 'code': 'duplicate_decision', 'count': decided[issue_id],
                             'message': 'Merge these into exactly one outer decision for this issue.'})
    for decision in decisions:
        issue_id = _identifier(decision.get('issue_id'))
        if issue_id not in required:
            problems.append({'issue_id': issue_id, 'code': 'unknown_decision',
                             'message': 'Use only known issue IDs in outer decisions. New candidates belong in new_candidates.'})
            continue
        components = decision.get('components')
        ids = Counter(_identifier(c.get('component_id')) for c in components if isinstance(c, dict)) if isinstance(components, list) else Counter()
        expected = required[issue_id]
        missing = [c for c in expected if not ids[c]]
        duplicate = [c for c in expected if ids[c] > 1]
        moved = [{'component_id': c, 'belongs_to': owner[c]} for c in ids if c in owner and owner[c] != issue_id]
        unknown = [c for c in ids if c not in owner]
        if missing:
            problems.append({'issue_id': issue_id, 'code': 'missing_components', 'component_ids': missing,
                             'message': 'Add one treatment for each listed component. Keep the components already present.'})
        if duplicate:
            problems.append({'issue_id': issue_id, 'code': 'duplicate_components', 'component_ids': duplicate,
                             'message': 'Keep exactly one treatment for each listed component.'})
        if moved:
            problems.append({'issue_id': issue_id, 'code': 'misassigned_components', 'components': moved,
                             'message': 'Move each listed component to the decision for the issue that owns it.'})
        if unknown:
            problems.append({'issue_id': issue_id, 'code': 'unknown_components', 'component_ids': unknown,
                             'message': 'Remove component IDs that are not in the required component ledger.'})
        if decided[issue_id] == 1:
            accounted += sum(ids[c] == 1 for c in expected)
    expected_total = sum(len(components) for components in required.values())
    status = {'issues': len(required), 'decided_issues': sum(decided[i] == 1 for i in required),
              'expected_components': expected_total, 'accounted_components': accounted,
              'complete': not problems and accounted == expected_total}
    return problems, status


def destination_diagnostics(value, ledger, reference_changes=()):
    """Locate all broken preserved links and contradictory hypothesis treatments."""
    findings = value.get('findings', [])
    issues = {issue['issue_id']: issue for issue in ledger['issues']}
    problems = []
    for decision in value.get('decisions', []):
        issue_id = decision.get('issue_id')
        issue = issues.get(issue_id, {})
        for component in decision.get('components', []):
            context = {'issue_id': issue_id, 'component_id': component.get('component_id')}
            number, quote = component.get('final_finding_number'), component.get('final_quote')
            for change in reference_changes:
                if (change.get('field') == 'final_quote' and change.get('issue_id') == issue_id
                        and change.get('component_id') == component.get('component_id')
                        and change.get('before') == quote):
                    quote = change['after']
            if component.get('treatment') == 'preserved':
                if type(number) is not int or not 1 <= number <= len(findings):
                    problems.append({**context, 'code': 'missing_final_finding', 'final_finding_number': number})
                elif not isinstance(quote, str) or not quote or quote not in findings[number - 1].get('comment', ''):
                    problems.append({**context, 'code': 'missing_final_quote', 'final_finding_number': number,
                                     'message': 'Copy an exact substantive span from this final comment. Do not invent a destination.'})
            if (issue.get('source_kind') == 'discarded_hypothesis'
                    and decision.get('scientific_status') == 'contradicted'
                    and component.get('treatment') != 'resolved'):
                problems.append({**context, 'code': 'rejected_hypothesis_unresolved_component',
                                 'message': 'A rejected hypothesis must resolve every component with evidence. An open question is not disproven.'})
    return problems


def validate_draft(raw, ledger, paper, cards=None):
    """Check a model-format response without saving or promoting a review."""
    from .annotations import normalize_review
    from .ensemble_debate import normalize_new_decisions, augment_ledger, adjudicated_secondary
    from .ensemble_references import recover
    from .evidence_handles import hydrate_response, build_registry
    from .external_evidence import bind_sources
    result = {'version': VERSION, 'draft_sha256': digest(json.dumps(raw, sort_keys=True)),
              'valid': False, 'problems': [],
              'limitation': 'Structural check only. Scientific correctness and semantic preservation require assessment.'}
    # Component accounting needs no hydration, so it is reported even when another error stops validation.
    component_problems, result['component_ledger'] = component_diagnostics(raw, ledger)
    result['problems'] = list(component_problems)
    try:
        hydrated = hydrate_response(raw, build_registry(paper, cards or []))
        final = normalize_new_decisions(hydrated['response'])
        if cards is not None:
            final = bind_sources(final, cards)
        for index, candidate in enumerate(final.get('new_candidates', []), 1):
            checked = normalize_review(paper, {'summary': 'New candidate', 'findings': [candidate['finding']]}, 'new-' + str(index))
            candidate['finding'] = checked['findings'][0]
        augmented = augment_ledger(final, ledger, paper, cards, validate=False)
        secondary_problems = []
        for decision in final.get('decisions', []):
            try:
                adjudicated_secondary({'decisions': [decision]}, augmented, paper, cards)
            except ValueError as error:
                secondary_problems.append({'code': 'invalid_secondary_finding',
                                           'issue_id': decision.get('issue_id'), 'message': str(error)})
        checked = recover(final, augmented, paper, cards)
        accepted = checked['recovered'] if checked['recovered'] is not None else final
        result['valid'] = (not component_problems and not secondary_problems
                           and (checked['recovered'] is not None or checked['original']['status'] == 'valid'))
        result['reference_recovery'] = {key: value for key, value in checked.items() if key != 'recovered'}
        if not result['valid']:
            failure = checked.get('audit') or checked['original']
            result['error'] = (secondary_problems[0]['message'] if secondary_problems else
                               failure.get('error', 'Every original component must have exactly one treatment'))
            result['problems'] = component_problems + secondary_problems + destination_diagnostics(accepted, augmented, checked.get('changed_fields', []))
    except (ValueError, KeyError, TypeError, AttributeError, IndexError) as error:
        result['error'] = str(error)
    return result


def preflight_receipts(directory, raw):
    """Record whether a passing preflight checked the exact submitted response."""
    expected = digest(json.dumps(raw, sort_keys=True))
    rows = []
    for path in sorted(directory.glob('check-*.json')):
        saved = read(path)
        result = saved.get('result', {})
        rows.append({'file': path.name, 'status': saved.get('status'),
                     'valid': result.get('valid') is True,
                     'matches_submitted_draft': result.get('draft_sha256') == expected})
    return {'checks': rows, 'submitted_draft_sha256': expected,
            'submitted_draft_preflight_passed': any(row['valid'] and row['matches_submitted_draft'] for row in rows)}


class SynthesisPreflight:
    """Persist a two-check local allowance and every candidate validation receipt."""
    def __init__(self, ledger, paper, output, cards):
        self.ledger, self.paper, self.output, self.cards = ledger, paper, output, cards
        self.output.mkdir(parents=True, exist_ok=True)

    def call(self, draft_json):
        # Exclusive receipt reservation makes the allowance survive MCP restarts.
        for index in range(1, MAX_CHECKS + 1):
            path = self.output / f'check-{index}.json'
            try:
                with path.open('x') as stream:
                    json.dump({'status': 'started', 'check': index}, stream)
                break
            except FileExistsError:
                continue
        else:
            return {'valid': False, 'error': 'Synthesis preflight allowance exhausted', 'remaining_checks': 0}
        try:
            if not isinstance(draft_json, str) or len(draft_json) > MAX_DRAFT_CHARACTERS:
                raise ValueError('Supply a bounded JSON draft string')
            raw = json.loads(draft_json)
            result = validate_draft(raw, self.ledger, self.paper, self.cards())
        except (ValueError, TypeError, AttributeError, RecursionError) as error:
            result = {'valid': False, 'error': str(error)}
        result.update(check=index, remaining_checks=MAX_CHECKS-index,
                      tool_receipt={'tool': 'validate_synthesis', 'call_index': index, 'allowance': VERSION})
        write(path, {'status': 'complete', 'draft_json': draft_json if isinstance(draft_json, str) and len(draft_json) <= MAX_DRAFT_CHARACTERS else None,
                     'result': result})
        return result
