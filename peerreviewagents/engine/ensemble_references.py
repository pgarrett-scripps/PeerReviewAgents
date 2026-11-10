"""Restricted repair for formatting-only debate component references."""
import copy
import re

from .debate_presentation import recover as recover_presentation


DESTINATION_ERROR = 'Preserved component lacks an exact final-review destination'
TRAILING = '.!?,:'


def _error(result):
    original = result.get('original', {})
    if original.get('error') == DESTINATION_ERROR:
        return DESTINATION_ERROR
    audit = result.get('audit') or {}
    return audit.get('error')


def _apply_presentation_receipts(value, receipts):
    revised = copy.deepcopy(value)
    decisions = {decision.get('issue_id'): decision for decision in revised.get('decisions', [])
                 if isinstance(decision, dict)}
    for receipt in receipts:
        if receipt.get('field') != 'presentation':
            return None
        decision = decisions.get(receipt.get('issue_id'))
        if decision is None or decision.get('presentation') != receipt.get('before'):
            return None
        decision['presentation'] = receipt.get('after')
    return revised


def _variants(before):
    """Return only the permitted formatting variants, excluding the original."""
    variants = set()
    first = next((index for index, char in enumerate(before) if char.isascii() and char.isalpha()), None)
    stems = [before]
    if first is not None:
        toggled = before[:first] + before[first].swapcase() + before[first + 1:]
        stems.append(toggled)
    for stem in stems:
        variants.add(stem)
        if stem and stem[-1] in TRAILING:
            variants.add(stem[:-1])
    variants.discard(before)
    return variants


def _repair_quotes(value):
    revised = copy.deepcopy(value)
    receipts = []
    findings = revised.get('findings', [])
    for decision in revised.get('decisions', []):
        if not isinstance(decision, dict):
            continue
        for component in decision.get('components', []):
            if not isinstance(component, dict) or component.get('treatment') != 'preserved':
                continue
            before = component.get('final_quote')
            number = component.get('final_finding_number')
            if (not isinstance(before, str) or len(re.findall(r'\b\w+\b', before)) < 3
                    or type(number) is not int or not 1 <= number <= len(findings)):
                continue
            comment = findings[number - 1].get('comment') if isinstance(findings[number - 1], dict) else None
            if not isinstance(comment, str):
                continue
            if before in comment:
                continue
            matches = {}
            for candidate in _variants(before):
                for match in re.finditer(r'(?<!\w)' + re.escape(candidate) + r'(?!\w)', comment):
                    current = matches.get(match.start())
                    record = (candidate, match.start(), match.end())
                    if current is None or record[2] > current[2]:
                        matches[match.start()] = record
            if len(matches) != 1:
                continue
            after, start, end = next(iter(matches.values()))
            component['final_quote'] = after
            receipts.append({
                'issue_id': decision.get('issue_id'),
                'component_id': component.get('component_id'),
                'field': 'final_quote', 'before': before, 'after': after,
                'final_finding_number': number, 'start': start, 'end': end,
            })
    return revised, receipts


def recover(value, ledger, paper, source_cards=None):
    """Recover only uniquely provable final-quote formatting differences."""
    initial = recover_presentation(value, ledger, paper, source_cards)
    if _error(initial) != DESTINATION_ERROR:
        return initial
    if initial.get('original', {}).get('error') == DESTINATION_ERROR:
        working = copy.deepcopy(value)
        prior_receipts = []
    else:
        prior_receipts = copy.deepcopy(initial.get('changed_fields', []))
        working = _apply_presentation_receipts(value, prior_receipts)
        if working is None:
            return initial
    repaired, reference_receipts = _repair_quotes(working)
    if not reference_receipts:
        return initial
    checked = recover_presentation(repaired, ledger, paper, source_cards)
    all_receipts = prior_receipts + reference_receipts + checked.get('changed_fields', [])
    if checked.get('recovered') is not None:
        recovered = checked['recovered']
    elif checked.get('original', {}).get('status') == 'valid':
        recovered = repaired
    else:
        return {**initial, 'recovered': None, 'audit': checked.get('audit') or checked.get('original'),
                'changed_fields': all_receipts}
    return {'original': initial['original'], 'recovered': recovered,
            'audit': checked.get('audit'), 'changed_fields': all_receipts,
            'limitation': 'Only a uniquely located final-quote formatting difference was repaired.'}
