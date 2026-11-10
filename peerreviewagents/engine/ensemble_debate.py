"""Bounded, independently anchored additions to a debate ledger."""
import copy
import json
import re

from .debate_revision import DEBATE_REVISION, revision_prompt, validate_revision
from .external_evidence import validate_references
from .review_schema import FINDING, STRING, obj


NEW_ISSUE = r"^new-[1-5]$"
NEW_FINDING = copy.deepcopy(FINDING)
NEW_FINDING['properties']['source_evidence'] = {
    'type': 'array', 'maxItems': 4,
    'items': obj({'evidence_id': STRING, 'quote': STRING}),
}
NEW_FINDING['required'].append('source_evidence')
SECONDARY_FINDING = copy.deepcopy(NEW_FINDING)
SECONDARY_FINDING_OR_NULL = {'anyOf': [copy.deepcopy(SECONDARY_FINDING), {'type': 'null'}]}
NEW_COMPONENT = copy.deepcopy(DEBATE_REVISION['properties']['decisions']['items']['properties']['components']['items'])
NEW_COMPONENT['properties'].pop('component_id')
NEW_COMPONENT['required'].remove('component_id')
NEW_DECISION = copy.deepcopy(DEBATE_REVISION['properties']['decisions']['items'])
NEW_DECISION['properties'].pop('issue_id')
NEW_DECISION['required'].remove('issue_id')
NEW_DECISION['properties'].pop('components')
NEW_DECISION['required'].remove('components')
NEW_DECISION['properties']['secondary_finding'] = copy.deepcopy(SECONDARY_FINDING_OR_NULL)
NEW_DECISION['required'].append('secondary_finding')
NEW_DECISION['properties']['component'] = NEW_COMPONENT
NEW_DECISION['required'].append('component')
NEW_CANDIDATE = obj({
    'issue_id': {'type': 'string', 'pattern': NEW_ISSUE},
    'finding': NEW_FINDING,
    'decision': NEW_DECISION,
})
ENSEMBLE_DEBATE = copy.deepcopy(DEBATE_REVISION)
ENSEMBLE_DEBATE['properties']['decisions']['items']['properties']['secondary_finding'] = copy.deepcopy(SECONDARY_FINDING_OR_NULL)
ENSEMBLE_DEBATE['properties']['decisions']['items']['required'].append('secondary_finding')
ENSEMBLE_DEBATE['properties']['new_candidates'] = {
    'type': 'array', 'items': NEW_CANDIDATE, 'maxItems': 5,
}
ENSEMBLE_DEBATE['required'].append('new_candidates')


def ensemble_schema():
    """Return the debate response schema with up to five new candidates."""
    return copy.deepcopy(ENSEMBLE_DEBATE)


def _identifier_choice(values):
    # Mirror the manuscript-handle constraint: a finite pattern replaces an oversized enum.
    return ({'type': 'string', 'enum': list(values)} if len(values) <= 950 else
            {'type': 'string', 'pattern': '^(?:' + '|'.join(re.escape(v) for v in values) + ')$'})


def constrain_ledger_identifiers(schema, ledger):
    """Require one outer decision per known issue and only ledger component identifiers."""
    result = copy.deepcopy(schema)
    issues = ledger['issues']
    decisions = result['properties']['decisions']
    decisions.update(minItems=len(issues), maxItems=len(issues))
    if not issues:
        return result
    decision = decisions['items']['properties']
    decision['issue_id'] = _identifier_choice([issue['issue_id'] for issue in issues])
    components = [component['component_id'] for issue in issues for component in issue['components']]
    if components:
        decision['components']['items']['properties']['component_id'] = _identifier_choice(components)
    if all(issue['components'] for issue in issues):
        decision['components']['minItems'] = 1
    return result


def normalize_new_decisions(value):
    """Materialize v2 nested new decisions without modifying the model response."""
    if not isinstance(value, dict):
        raise ValueError('Ensemble response must be a structured object')
    result = copy.deepcopy(value)
    candidates, decisions = result.get('new_candidates', []), result.get('decisions', [])
    if not isinstance(candidates, list) or not isinstance(decisions, list):
        raise ValueError('Ensemble response has invalid candidate or decision records')
    identifiers = []
    for candidate in candidates:
        if not isinstance(candidate, dict) or not isinstance(candidate.get('issue_id'), str):
            raise ValueError('New candidate needs an issue ID')
        issue_id = candidate['issue_id']
        if not re.fullmatch(NEW_ISSUE, issue_id):
            raise ValueError('New candidate must use a reserved new-N issue identifier')
        if issue_id in identifiers:
            raise ValueError('New candidate issue identifiers must be unique')
        identifiers.append(issue_id)
        decision = candidate.get('decision')
        if not isinstance(decision, dict):
            raise ValueError('New candidate requires an embedded decision')
        component = decision.get('component')
        if not isinstance(component, dict):
            raise ValueError('New candidate decision requires one embedded component')
        if 'issue_id' in decision or 'components' in decision or 'component_id' in component:
            raise ValueError('Embedded new decisions must not repeat generated identifiers')
    top_new = [decision.get('issue_id') for decision in decisions if isinstance(decision, dict)
               and isinstance(decision.get('issue_id'), str) and re.fullmatch(NEW_ISSUE, decision['issue_id'])]
    if top_new:
        raise ValueError('Top-level decisions may not contain new candidate IDs')
    materialized = []
    for candidate in candidates:
        issue_id = candidate['issue_id']
        embedded = candidate.pop('decision')
        component = embedded.pop('component')
        materialized.append({**embedded, 'issue_id': issue_id,
                             'components': [{**component, 'component_id': issue_id + '-part-1'}]})
    result['decisions'] = decisions + materialized
    return result


def ensemble_prompt(paper, manuscript, ledger, source_cards=None):
    """Add a narrowly scoped new-finding procedure to the v3 debate prompt."""
    known = [issue['issue_id'] for issue in ledger['issues']]
    procedure = '''\nENSEMBLE NEW-FINDING PROCEDURE:
The known issue IDs are: ''' + json.dumps(known, ensure_ascii=False) + '''.
Preserve every known issue in the outer decisions array. You may add at most five genuinely new scientific candidates in new_candidates. Use only the reserved IDs new-1 through new-5, each at most once. A new candidate must be independently anchored to one exact, unique manuscript quote, include valid manuscript evidence blocks, and use one component for its entire comment. Do not use a new candidate to restate, rename, or evade a known issue.
Each new candidate must embed its own decision object and one component object. Do not place a new-N decision in the outer decisions array. Do not provide issue_id or component_id inside embedded objects because the runtime assigns new-N and new-N-part-1. Its scientific status may be supported_concern, supported_clarification, or unresolved. A supported new candidate needs decision evidence from the manuscript or an eligible external source. External source cards are optional. If supplied, they must be quoted exactly. If a supported or unresolved new candidate is not selected among final findings, retain it as supporting with its component deferred. Do not invent source records.\n'''
    procedure += '''For every decision, provide secondary_finding. Set it to null unless the issue is a supported or unresolved supporting secondary concern with no preserved component and at least one deferred component. For each such retained concern, provide a complete, author-facing finding with an exact unique manuscript anchor, valid manuscript evidence blocks, and any eligible external evidence used. Its wording must reflect the adjudicated scope, including uncertainty where applicable. Do not include a replacement in a secondary finding. An unresolved discarded hypothesis with no original manuscript finding may instead use omitted presentation with deferred components and a null secondary_finding. That is audit-only, not an author-facing comment. Do not copy an original broad allegation into secondary_finding merely because it was retained in the audit trail. The original candidate remains audit evidence only.\n'''
    procedure+='Before finalizing, briefly check the central claims against the combined review for a consequential gap. Consider design-appropriate issues such as the unit of inference, outcome selection and error control, measurement, and whether conclusions exceed the evidence. These are prompts for investigation, not mandatory criticisms. Add a new candidate only when the manuscript supports a specific concern or actionable uncertainty. Do not manufacture an omission to fill a category.\n'
    return revision_prompt(paper, manuscript, ledger, source_cards) + procedure


def ordinary_synthesis_prompt(paper, manuscript, ledger, source_cards=None):
    """Reconcile saved reviews directly, without an adversarial reasoning frame."""
    known = [issue['issue_id'] for issue in ledger['issues']]
    cards = '' if source_cards is None else '\nSOURCE CARDS:\n' + json.dumps(source_cards, ensure_ascii=False)
    return '''Synthesize the supplied independent manuscript reviews into one concise editor report.
Assess each candidate against the manuscript and supplied evidence. Merge duplicates, narrow overbroad wording, retain distinct supported concerns, and omit claims that the available source material does not support. Do not treat reviewer agreement as proof. Use at most two optional source tool calls when missing context could change a consequential decision.
Preserve every known issue in the decisions array. The known issue IDs are: ''' + json.dumps(known, ensure_ascii=False) + '''.
For every issue, record its scientific status, editorial presentation, evidence, explanation, and the treatment of every supplied component. A preserved component must map to a selected final finding. A resolved component requires source evidence. A deferred component remains available as a secondary concern. Editorial omission does not make a concern scientifically contradicted.
Select at most five distinct consequential findings and keep final comments within 900 words. Every finding needs an exact unique manuscript anchor and valid evidence blocks. Phrase unresolved matters as specific questions. Do not invent methods, results, numerical values, citations, or author intent.
You may add at most five genuinely new manuscript-supported candidates using reserved IDs new-1 through new-5. Do not use a new candidate to restate a known issue. Each new candidate needs an exact unique manuscript anchor, evidence blocks, one embedded decision, and one whole-comment component. Keep supported secondary concerns with adjudicated author-facing wording in secondary_finding. Set secondary_finding to null otherwise.
Return one decision for every known issue and every new candidate. Preserve concrete requested actions when merging. Keep decision explanations brief. The component ledger is an audit record.
TITLE:
''' + paper['title'] + '\nCANDIDATE LEDGER:\n' + json.dumps(ledger, ensure_ascii=False) + cards + '\nFULL MANUSCRIPT:\n' + manuscript


def _new_issues(value, ledger, paper, source_cards):
    candidates = value.get('new_candidates', [])
    if not isinstance(candidates, list) or len(candidates) > 5:
        raise ValueError('New candidates must be an array of at most five records')
    existing = {issue.get('issue_id') for issue in ledger.get('issues', [])}
    identifiers = [candidate.get('issue_id') if isinstance(candidate, dict) else None
                   for candidate in candidates]
    if len(set(identifiers)) != len(identifiers):
        raise ValueError('New candidate issue identifiers must be unique')
    blocks = {block.get('id'): block.get('text') for block in paper.get('blocks', [])
              if isinstance(block, dict) and isinstance(block.get('id'), str)
              and isinstance(block.get('text'), str)}
    issues = []
    for index, candidate in enumerate(candidates):
        if not isinstance(candidate, dict):
            raise ValueError('New candidate must be a structured record')
        issue_id = candidate.get('issue_id')
        if not isinstance(issue_id, str) or not re.fullmatch(NEW_ISSUE, issue_id):
            raise ValueError('New candidate must use a reserved new-N issue identifier')
        if issue_id in existing:
            raise ValueError('New candidate identifier conflicts with an existing issue')
        finding = candidate.get('finding')
        if not isinstance(finding, dict):
            raise ValueError('New candidate finding must be a structured record')
        if not isinstance(finding.get('comment'), str) or not finding['comment'].strip():
            raise ValueError('New candidate needs a nonempty comment')
        block_id, quote = finding.get('block_id'), finding.get('quote')
        if not isinstance(block_id, str) or not isinstance(quote, str) or not quote:
            raise ValueError('New candidate needs an independently anchored finding')
        source = blocks.get(block_id, '')
        if len(list(re.finditer(r'(?=' + re.escape(quote) + r')', source))) != 1:
            raise ValueError('New candidate quote is absent or ambiguous')
        evidence_blocks = finding.get('evidence_blocks')
        if not isinstance(evidence_blocks, list) or any(not isinstance(item, str) for item in evidence_blocks):
            raise ValueError('New candidate evidence blocks must be manuscript block identifiers')
        if any(item not in blocks for item in evidence_blocks):
            raise ValueError('New candidate has an unknown manuscript evidence block')
        references = finding.get('source_evidence', [])
        if not isinstance(references, list):
            raise ValueError('New candidate source evidence must be an array')
        if references:
            validate_references({'findings': [{'source_evidence': references}]}, source_cards or [])
        issues.append({
            'issue_id': issue_id,
            'source_kind': 'debate_new_candidate',
            'original_finding': copy.deepcopy(finding),
            'components': [{'component_id': issue_id + '-part-1', 'text': finding['comment']}],
            'provenance': {
                'procedure': 'ensemble_debate_new_candidate_v1',
                'candidate_index': index + 1,
                'anchor': {'block_id': block_id, 'quote': quote},
                'evidence_blocks': copy.deepcopy(evidence_blocks),
                'source_evidence': copy.deepcopy(references),
            },
        })
    return issues


def _validate_secondary_finding(finding, blocks, source_cards):
    """Validate model-supplied author wording without deriving scientific content."""
    if not isinstance(finding, dict):
        raise ValueError('Secondary concern requires an adjudicated finding')
    required = set(SECONDARY_FINDING['required'])
    if set(finding) != required:
        raise ValueError('Adjudicated secondary finding has an invalid shape. Missing fields: '
                         + ', '.join(sorted(required - set(finding)))
                         + '. Unexpected fields: ' + ', '.join(sorted(set(finding) - required))
                         + '. Supply the required fields. Use empty source arrays only when no external sources were used. Do not drop required secondary wording.')
    if not isinstance(finding.get('comment'), str) or not finding['comment'].strip():
        raise ValueError('Adjudicated secondary finding needs a nonempty comment')
    if finding.get('replacement'):
        raise ValueError('Adjudicated secondary finding cannot include an unguarded replacement')
    block_id, quote = finding.get('block_id'), finding.get('quote')
    if not isinstance(block_id, str) or not isinstance(quote, str) or not quote:
        raise ValueError('Adjudicated secondary finding needs an exact anchor')
    source = blocks.get(block_id, '')
    if len(list(re.finditer(r'(?=' + re.escape(quote) + r')', source))) != 1:
        raise ValueError('Adjudicated secondary finding quote is absent or ambiguous')
    evidence_blocks = finding.get('evidence_blocks')
    if (not isinstance(evidence_blocks, list)
            or any(not isinstance(item, str) or item not in blocks for item in evidence_blocks)):
        raise ValueError('Adjudicated secondary finding has invalid manuscript evidence')
    references = finding.get('source_evidence')
    if not isinstance(references, list):
        raise ValueError('Adjudicated secondary finding source evidence must be an array')
    if references:
        validate_references({'findings': [{'source_evidence': references}]}, source_cards or [])


def adjudicated_secondary(value, ledger, paper, source_cards=None):
    """Return only validated, model-authored secondary comments, keyed for presentation."""
    if not isinstance(value, dict) or not isinstance(ledger, dict):
        raise ValueError('Ensemble debate requires structured response and ledger records')
    blocks = {block.get('id'): block.get('text') for block in paper.get('blocks', [])
              if isinstance(block, dict) and isinstance(block.get('id'), str)
              and isinstance(block.get('text'), str)}
    issues = {issue.get('issue_id'): issue for issue in ledger.get('issues', [])
              if isinstance(issue, dict)}
    result = []
    for decision in value.get('decisions', []):
        if not isinstance(decision, dict):
            raise ValueError('Ensemble decision must be a structured record')
        components = decision.get('components')
        deferred_candidate = (decision.get('scientific_status') in
                              ('supported_concern', 'supported_clarification', 'unresolved')
                              and isinstance(components, list) and components
                              and any(isinstance(component, dict) and component.get('treatment') == 'deferred'
                                      for component in components)
                              and not any(isinstance(component, dict)
                                          and component.get('treatment') == 'preserved'
                                          for component in components))
        issue = issues.get(decision.get('issue_id'))
        audit_only = (deferred_candidate and decision.get('scientific_status') == 'unresolved'
                      and decision.get('presentation') == 'omitted' and isinstance(issue, dict)
                      and issue.get('original_finding') is None
                      and isinstance(issue.get('original_hypothesis'), str))
        if audit_only:
            if decision.get('secondary_finding') is not None:
                raise ValueError('Audit-only hypothesis cannot have author-facing secondary wording')
            continue
        if deferred_candidate and decision.get('presentation') != 'supporting':
            raise ValueError('Retained secondary concern must use supporting presentation')
        secondary = deferred_candidate and decision.get('presentation') == 'supporting'
        finding = decision.get('secondary_finding')
        if secondary:
            if decision.get('issue_id') not in issues:
                raise ValueError('Secondary concern refers to an unknown issue')
            try:
                _validate_secondary_finding(finding, blocks, source_cards)
            except ValueError as error:
                raise ValueError(str(error) + ' [issue_id=' + str(decision.get('issue_id')) + ']') from None
            result.append({'issue_id': decision['issue_id'], 'finding': copy.deepcopy(finding)})
        elif finding is not None:
            raise ValueError('Only retained secondary concerns may have adjudicated wording')
    return result


def audit_only_secondary(value, ledger):
    """Identify unresolved, unanchored hypotheses that remain only in the audit trail."""
    issues = {issue.get('issue_id'): issue for issue in ledger.get('issues', [])
              if isinstance(issue, dict)}
    records = []
    for decision in value.get('decisions', []):
        if not isinstance(decision, dict):
            continue
        components = decision.get('components')
        if (decision.get('scientific_status') != 'unresolved'
                or decision.get('presentation') != 'omitted'
                or not isinstance(components, list) or not components
                or not any(isinstance(component, dict) and component.get('treatment') == 'deferred'
                           for component in components)
                or any(isinstance(component, dict) and component.get('treatment') == 'preserved'
                       for component in components)):
            continue
        issue = issues.get(decision.get('issue_id'))
        if (isinstance(issue, dict) and issue.get('original_finding') is None
                and isinstance(issue.get('original_hypothesis'), str)):
            records.append({'issue_id': decision['issue_id'], 'audience': 'audit_only'})
    return records


def attach_adjudicated_secondary(package, secondary, audit_only=()):
    """Expose validated secondary wording while retaining the source finding for audit."""
    result = copy.deepcopy(package)
    inventory = result.get('candidate_inventory', {})
    candidates = inventory.get('secondary_candidates', []) if isinstance(inventory, dict) else []
    by_issue = {item['issue_id']: item for item in secondary}
    audit_by_issue = {item['issue_id']: item for item in audit_only}
    source_by_issue = {item.get('source_finding_id'): item.get('finding', {})
                       for item in result.get('source_findings', []) if isinstance(item, dict)}
    candidate_ids = {item.get('issue_id') for item in candidates if isinstance(item, dict)}
    if candidate_ids != set(by_issue) | set(audit_by_issue):
        raise ValueError('Adjudicated secondary records do not match the candidate inventory')
    author_candidates = []
    audit_candidates = []
    for candidate in candidates:
        if candidate['issue_id'] in audit_by_issue:
            audit_candidates.append({**candidate, **audit_by_issue[candidate['issue_id']]})
            continue
        finding = by_issue[candidate['issue_id']]['finding']
        original = source_by_issue.get(candidate['issue_id'], {})
        candidate['original_title'] = original.get('title', candidate.get('title', ''))
        candidate['original_text'] = original.get('comment', candidate.get('text', ''))
        candidate['title'] = finding['title']
        candidate['text'] = finding['comment']
        candidate['adjudicated_finding'] = copy.deepcopy(finding)
        author_candidates.append(candidate)
    inventory['secondary_candidates'] = author_candidates
    inventory['audit_only_candidates'] = audit_candidates
    result['adjudicated_secondary'] = copy.deepcopy(secondary)
    return result


def _validate_new_decisions(value, new_issues):
    decisions = {row.get('issue_id'): row for row in value.get('decisions', [])
                 if isinstance(row, dict)}
    for issue in new_issues:
        decision = decisions.get(issue['issue_id'])
        if decision is None:
            raise ValueError('New candidate requires a decision')
        status = decision.get('scientific_status')
        if status not in ('supported_concern', 'supported_clarification', 'unresolved'):
            raise ValueError('New candidate decisions must be source-supported or unresolved')
        components = decision.get('components', [])
        if (not isinstance(components, list) or len(components) != 1
                or not isinstance(components[0], dict)
                or components[0].get('component_id') != issue['components'][0]['component_id']):
            raise ValueError('New candidate decision must account for its whole-comment component')
        preserved = components[0].get('treatment') == 'preserved'
        if status in ('supported_concern', 'supported_clarification') and not decision.get('evidence'):
            raise ValueError('Supported new candidate requires decision evidence')
        if not preserved and status in ('supported_concern', 'supported_clarification', 'unresolved'):
            if decision.get('presentation') != 'supporting':
                raise ValueError('Unselected new candidates must remain supporting secondary candidates')
            if any(component.get('treatment') != 'deferred'
                   for component in components if isinstance(component, dict)):
                raise ValueError('Unselected new candidates must defer their component')


def augment_ledger(value, ledger, paper, source_cards=None, *, validate=True,
                   require_adjudicated_secondary=False):
    """Copy and extend a ledger, with optional deferred v3 validation."""
    if not isinstance(value, dict) or not isinstance(ledger, dict):
        raise ValueError('Ensemble debate requires structured response and ledger records')
    original = copy.deepcopy(ledger)
    augmented = copy.deepcopy(ledger)
    additions = _new_issues(value, ledger, paper, source_cards)
    augmented.setdefault('issues', []).extend(additions)
    augmented['version'] = 'ensemble-debate-ledger-v1'
    _validate_new_decisions(value, additions)
    if require_adjudicated_secondary:
        adjudicated_secondary(value, augmented, paper, source_cards)
    if validate:
        revision_value = copy.deepcopy(value)
        revision_value.pop('new_candidates', None)
        validate_revision(revision_value, augmented, paper, source_cards)
    if ledger != original:
        raise RuntimeError('Ensemble debate must not modify the original ledger')
    return augmented
