"""Evidence reconciliation and matched scientific review output contracts."""
import copy
import re

from .review_schema import REVIEW, STRING, obj
from .debate_revision import DEBATE_REVISION
from .discovery import MEASUREMENT_PATH


def questions_only(schema):
    result = copy.deepcopy(schema)
    fields = result['properties']['findings']['items']['properties']
    fields['kind'] = {'type':'string', 'enum':['question']}
    fields['replacement'] = {'type':'string', 'enum':['']}
    return result


REFERENCE = obj({'block_id':STRING, 'quote':STRING})
CHECK = obj({
    'area':{'type':'string', 'enum':['reported_results','endpoint_scope','design_scope','measurement_selection']},
    'claim':STRING, 'measured_or_tested':STRING, 'comparison_and_unit':STRING,
    'observation':STRING, 'strongest_author_defense':STRING,
    'unresolved':STRING,
    'status':{'type':'string', 'enum':['consistent','concern','unresolved','not_applicable']},
    'evidence':{'type':'array', 'items':REFERENCE, 'maxItems':6},
})
CANDIDATE_REVIEW = questions_only(REVIEW)
CANDIDATE_REVIEW['properties']['findings']['maxItems'] = 8
CANDIDATE_REVIEW['properties']['selected_finding_numbers'] = {
    'type':'array', 'items':{'type':'integer', 'minimum':1, 'maximum':8}, 'maxItems':5}
CANDIDATE_REVIEW['properties']['claim_checks'] = {'type':'array', 'items':CHECK, 'maxItems':4}
CANDIDATE_REVIEW['required'] += ['selected_finding_numbers','claim_checks']
QUESTION_DEBATE = questions_only(DEBATE_REVISION)

INVESTIGATE = '''Build four concise evidence checks before selecting findings. These checks are investigative tasks, not required defects.
1. reported_results: Reconcile consequential numerical or significance claims across prose, captions, and corresponding reported table cells. Match outcome, cohort, comparison, interaction order, unit, and model before comparing values. Read header and footnote rows separately. Never fill down labels and present the reconstructed row as a quotation. Different tests are not a contradiction. Where no comparable table exists record the limit.
2. endpoint_scope: Connect a central biological process, stage, mechanism, or specificity claim to the observations actually collected and reported. Distinguish measurements taken, results shown, and conclusions inferred. Inspect methods and supplements for intermediate observations and counterevidence. Ask whether the available observations distinguish plausible alternatives, without assuming an unreported measurement was never made.
3. design_scope: Trace a consequential comparison through allocation, treatment sequence, pairing, observation unit, biological replication, and stated analysis. Separate within-group claims from between-group claims. Do not mistake design combinations or individual measurements for independent replication.
4. measurement_selection: ''' + MEASUREMENT_PATH + '''
Return one claim_checks entry for each area, with exact source quotations, matched evidence, the strongest author defense, and remaining uncertainty. Limit each check's prose to 160 words. A consistent check is useful and need not produce a finding. Seek other consequential issues as ordinary peer review requires. Do not repeat acknowledged caveats unless a specific stronger conclusion remains. Prioritize concrete discrepancies and unresolved scientific consequences over generic cautions.
If an investigated concern remains useful after narrowing its scope, preserve it as an anchored candidate even when it is not among your selected five. Use discarded_hypotheses for premises the evidence defeats or for questions with no remaining consequential request. A limitation that affects one comparison but not another needs a scoped decision, not automatic dismissal. Do not preserve a defeated allegation merely to fill the candidate allowance.
'''

FOCUSED_DEBATE = '''Keep the debate centered on decisions the evidence could change. For each candidate state the opposing scientific arguments and the decisive source fact. Spend detail on contradicted premises, different tests being conflated, missing counterevidence, excessive requests, and loss of distinct scientific questions during selection. A narrow clarification can be better than an allegation. Do not improve wording at the expense of a supported concrete issue. Do not infer that an endpoint was never measured just because its result is not displayed.
Every original component still requires an explicit disposition. For straightforward preserved components keep the reason brief. Resolved components need actual source evidence. Deferred components remain in the ledger with a reason. Preserve distinct useful questions where possible within five selected findings. No tools are available in this bounded run, use the complete delivered prose and reported cell register. All selected findings must be questions with empty replacements. No wording edits are allowed in any condition.
'''


def validate_review_contract(response, paper, max_findings=8):
    by = {b['id']:b['text'] for b in paper['blocks']}
    findings = response.get('findings', [])
    if len(findings) > max_findings or sum(len(f['comment'].split()) for f in findings) > 900:
        raise ValueError('Scientific review exceeds the shared output allowance')
    for f in findings:
        if f['kind'] != 'question' or f['replacement'] != '':
            raise ValueError('All comparison findings must be questions without replacements')
        if not f['quote'] or len(re.findall('(?=' + re.escape(f['quote']) + ')', by.get(f['block_id'], ''))) != 1:
            raise ValueError('Finding requires an exact unique manuscript quote')
        if any(bid not in by for bid in f['evidence_blocks']):
            raise ValueError('Finding evidence must identify original manuscript blocks')
    if 'selected_finding_numbers' in response:
        numbers = response['selected_finding_numbers']
        if (len(numbers) > 5 or len(set(numbers)) != len(numbers)
                or any(type(n) is not int or not 1 <= n <= len(findings) for n in numbers)):
            raise ValueError('Invalid selected finding numbers')


def audit_checks(response, paper, required=False):
    by = {b['id']:b['text'] for b in paper['blocks']}
    rows = response.get('claim_checks', [])
    required_areas = {'reported_results','endpoint_scope','design_scope','measurement_selection'}
    records = []
    for row in rows:
        errors = []
        if row['area'] not in required_areas:
            errors.append('Unknown investigation area')
        if row['status'] != 'not_applicable' and not row['evidence']:
            errors.append('Check lacks source evidence')
        for ref in row['evidence']:
            if not ref['quote'] or ref['quote'] not in by.get(ref['block_id'], ''):
                errors.append('Source quotation does not match')
        records.append({'area':row['area'], 'valid_references':not errors, 'errors':errors})
    areas = [r['area'] for r in rows]
    complete = all(areas.count(area) == 1 for area in required_areas)
    return {'version':'claim-checks-v2', 'checks':records, 'required_coverage_complete':complete if required else None,
            'valid':all(r['valid_references'] for r in records) and (complete or not required),
            'limitation':'These checks validate source identity and task accounting, not scientific correctness.'}
