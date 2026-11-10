"""A bounded coverage record for manuscript investigation, without extra inference."""
import copy
from .review_schema import REVIEW, STRING, obj


MEASUREMENT_PATH = 'For empirical claims based on measured outcomes, trace a central outcome from its operational definition through aggregation, thresholds, missingness, exclusions, and the observations included in analysis. Check whether inclusion or exclusion depends on the outcome signal itself and whether selection could differ among comparison groups. Distinguish a possible selection effect from demonstrated misclassification. Inspect reported validation and controls before raising a concern. An established proxy does not by itself require a new validation experiment. Prefer a proportionate request for existing checks, exclusion counts, sensitivity, or narrower interpretation when warranted.'


DESIGN_CHECKS = '''Apply the following compact checks only when the manuscript design matches. Do not force these checks onto other work.
For an intervention, mixture, substitution experiment, or bundled treatment, identify the actual manipulated contrast. What changes between groups, what necessarily changes alongside it, and what is held constant? Compare the causal attribution with that contrast. A mixture substitution can establish an effect of the specified substitution without isolating an intrinsic effect of one component. Do not call this confounding merely because a composition sums to a fixed total. Check the formulation, controls, factorial contrasts and stated estimand before raising a concern. Prefer a narrower supported interpretation or a precise clarification over demanding a new experiment.
For a clinical trial, reconcile when covariates were measured with treatment assignment and outcome timing. Reconcile every important count with its denominator, arm, time point, and missing-data path. Distinguish enrolled, randomized, treated, analyzed, per-protocol, and safety populations.
For an ecology field study, identify the treatment or exposure assignment unit separately from subsamples. Check whether interactions match the stated biological question. Reconcile model names, variables, coefficients, uncertainty, and fit statistics across prose, tables, captions, and plotted figures.'''


AREAS = {
    'claims': 'Which observations, sources or arguments support the central claim, and which alternative explanations remain?',
    'comparators': 'Where applicable, do comparison groups, cases or contrasting interpretations support the claim and its scope?',
    'measurement': 'For quantitative work, check outcome definitions, aggregation, exclusions, missingness and validation. For interpretive work, check source selection, analytic development and evidence for interpretations.',
    'interventions': 'Where applicable, are treatment identity, specificity, timing, and exposure described?',
    'replication': 'For empirical comparisons, are units, pairing, replication and uncertainty interpretable? For qualitative work, assess sampling rationale and methodological coherence without imposing statistical replication.',
    'consistency': 'Do reported counts, units, time scales, and conclusions agree across the manuscript?',
}
QUOTE = obj({'block_id': STRING, 'quote': {'type': 'string', 'maxLength': 350}})
CHECK = obj({
    'area': {'type': 'string', 'enum': list(AREAS)},
    'status': {'type': 'string', 'enum': ['examined', 'unresolved', 'not_applicable']},
    'reason': {'type': 'string', 'maxLength': 450},
    'evidence': {'type': 'array', 'items': QUOTE, 'maxItems': 3},
})
INVESTIGATION_REVIEW = copy.deepcopy(REVIEW)
INVESTIGATION_REVIEW['properties']['findings']['maxItems'] = 10
INVESTIGATION_REVIEW['properties']['coverage_checks'] = {
    'type': 'array', 'items': CHECK, 'minItems': len(AREAS), 'maxItems': len(AREAS),
}
INVESTIGATION_REVIEW['required'].append('coverage_checks')

DISCOVERY = MEASUREMENT_PATH + '\n' + DESIGN_CHECKS + '\n' + '''First scan the supplied manuscript across the six investigation areas below before narrowing your attention. These are questions to assess, not a list of defects to manufacture. Investigate the most consequential uncertainties deeply, prioritizing specific design or measurement issues over generic criticism of causal language. Activate domain-specific checks only where the manuscript design warrants them. Do not request unavailable experiments simply to fill an area.
Use at most three tool calls for evidence you do not already have. The complete extracted text is supplied. Inspect original figure pixels when the concern depends on a visual feature. For an apparent omission, check relevant methods, captions, and available supplementary material before calling it missing. If access or budget prevents that check, describe the uncertainty and ask the authors instead of asserting absence.
For this investigator stage, return up to ten distinct, anchored candidate findings. This stage intentionally overrides the shared five-finding instruction. Include a supported or unresolved actionable concern even when it may be secondary to the likely final five. Fewer candidates are preferable to padding. Keep all finding comments together within the shared concise word budget. Use discarded_hypotheses only for ideas you investigated and rejected or could not formulate as an anchored actionable question. Do not put a supported concern there merely because it has lower editorial priority.
Return one coverage_checks entry for every area. Keep the record concise. Cite exact short quotations and block IDs for examined areas. An examined status means you considered the area, not that the paper passed a validity check. Use unresolved for incomplete investigation and not_applicable with a reason when appropriate. Save this record in the final response rather than spending a tool call on save_note. The record is not a finding and does not count toward the ten-candidate limit. Retain competing explanations in counterevidence and discarded hypotheses.\nINVESTIGATION AREAS:\n'''


def coverage_record(paper, response):
    """Check accounting and quotation identity, without endorsing scientific reasoning."""
    blocks = {block['id']: block['text'] for block in paper['blocks']}
    rows = response.get('coverage_checks', [])
    if not isinstance(rows, list):
        rows = []
    records = []
    for area in AREAS:
        matches = [row for row in rows if isinstance(row, dict) and row.get('area') == area]
        problems = []
        row = matches[0] if len(matches) == 1 else {}
        if row.get('reference_validation_error'):
            problems.append('Evidence handle validation failed: '+str(row['reference_validation_error']))
        if len(matches) != 1:
            problems.append('Missing or duplicate area')
        status = row.get('status')
        if status not in ('examined', 'unresolved', 'not_applicable'):
            problems.append('Unknown status')
        reason = row.get('reason')
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 450:
            problems.append('Missing or oversized explanation')
        evidence = row.get('evidence', [])
        if not isinstance(evidence, list) or len(evidence) > 3:
            problems.append('Invalid evidence list')
            evidence = []
        verified = []
        for item in evidence:
            if (isinstance(item, dict) and isinstance(item.get('quote'), str)
                    and 0 < len(item['quote']) <= 350
                    and isinstance(item.get('block_id'), str)
                    and item['quote'] in blocks.get(item['block_id'], '')):
                verified.append({'block_id': item['block_id'], 'quote': item['quote']})
            else:
                problems.append('Evidence quotation does not match the source')
        if status == 'examined' and not verified:
            problems.append('Examined area lacks a source quotation')
        records.append({'area': area, 'status': 'unresolved' if problems else status,
                        'reason': reason[:450] if isinstance(reason, str) else '',
                        'evidence': verified, 'validation': 'invalid' if problems else 'valid',
                        'problems': problems})
    unexpected = [row.get('area') if isinstance(row, dict) else None for row in rows
                  if not isinstance(row, dict) or not isinstance(row.get('area'), str)
                  or row['area'] not in AREAS]
    return {'areas': records, 'unexpected_areas': unexpected,
            'limitation': 'Coverage is model-reported. Validation checks accounting and exact source quotations only. It does not establish review completeness or scientific correctness.'}
