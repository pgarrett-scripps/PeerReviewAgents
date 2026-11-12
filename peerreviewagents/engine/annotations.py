"""Source-bound suggestions, with editorial decisions separate from findings."""
import copy
import re
from pathlib import Path
from .storage import digest, read, write


def verify_workspace(root):
    root=Path(root)
    manifest=read(root/'manifest.json')
    for name, expected in manifest['files'].items():
        path=(root/name).resolve()
        if not path.is_relative_to(root.resolve()) or digest(path.read_bytes())!=expected:
            raise ValueError('Manuscript source changed: '+name)
    paper=read(root/'paper.json')
    if paper.get('source_format')=='scaffold-typst':
        actual=set()
        for path in (root/'scaffold').rglob('*'):
            if path.is_symlink():
                raise ValueError('Manuscript source contains a new symlink')
            if path.is_file():actual.add(path.relative_to(root).as_posix())
        expected={name for name in manifest['files'] if name.startswith('scaffold/')}
        if actual!=expected:
            raise ValueError('Manuscript source inventory changed')
    return paper


def normalize_review(paper, response, review_id):
    for key in ('paper_id','source_sha256'):
        if key in response and response[key]!=paper['id' if key=='paper_id' else key]:
            raise ValueError('Review belongs to a different manuscript version')
    by={b['id']:b for b in paper['blocks']}
    notes=[]
    for i, raw in enumerate(response.get('findings',[]),1):
        a={**raw,'id':f'{review_id}-{i}','review_id':review_id,'issue_status':'open','edit_decision':'proposed'}
        a['kind']=raw.get('kind','question') if raw.get('kind') in ('edit','question','literature') else 'question'
        a['category']=raw.get('category') if raw.get('category') in ('scientific','copyedit') else 'scientific'
        a['evidence_blocks']=[bid for bid in raw.get('evidence_blocks',[]) if bid in by]
        a['invalid_evidence_blocks']=[bid for bid in raw.get('evidence_blocks',[]) if bid not in by]
        block=by.get(raw.get('block_id'))
        quote=raw.get('quote','')
        positions=[m.start() for m in re.finditer('(?='+re.escape(quote)+')',block['text'])] if block and quote else []
        if len(positions)==1:
            a.update(start=positions[0],end=positions[0]+len(quote),block_sha256=block['sha256'],placement='anchored')
        else:
            a.update(start=None,end=None,block_sha256=block['sha256'] if block else None,placement='unplaced',
                     placement_reason='Quote is absent or ambiguous in the selected source block')
        replacement=raw.get('replacement','')
        if a['kind']=='edit' and (not isinstance(replacement,str) or not replacement.strip() or a['placement']!='anchored'):
            a['kind']='question'
            a['edit_withheld']='A valid anchored replacement was not supplied'
        numeric=r'(?<![\w])[-+]?\d+(?:\.\d+)?(?![\w])'
        if a['kind']=='edit' and not set(re.findall(numeric,replacement)) <= set(re.findall(numeric,quote)):
            a['kind']='question'
            a['edit_withheld']='Replacement introduces a numerical literal. Keep it as an author question until checked.'
        a['replacement']=replacement if a['kind']=='edit' else None
        notes.append(a)
    return {'id':review_id,'paper_id':paper['id'],'summary':response.get('summary',''),
            'findings':notes,'limitations':response.get('limitations',[]),'raw_response':response,
            'annotation_counts':{'total':len(notes),'anchored':sum(a['placement']=='anchored' for a in notes)}}


def quarantine_findings(findings):
    """Split normalized findings into the ones that passed evidence validation and the ones that did not.

    A finding is quarantined when its quote could not be anchored in the
    source block it names, or when it cites block IDs the manuscript does not
    contain. Each quarantined finding keeps its content and gains a reason, so
    one unanchored quote costs that finding and nothing else. The caller writes
    both lists to the run output: the quarantine is evidence about the model,
    not something to discard quietly.
    """
    kept, quarantined = [], []
    for finding in findings:
        reasons = []
        if finding.get('placement') != 'anchored':
            reasons.append(finding.get('placement_reason')
                           or 'Finding is not anchored to an exact source quote')
        if finding.get('invalid_evidence_blocks'):
            reasons.append('Cites block IDs absent from the manuscript: '
                           + ', '.join(str(bid) for bid in finding['invalid_evidence_blocks']))
        if reasons:
            quarantined.append({**finding, 'quarantined': True, 'issue_status': 'quarantined',
                                'quarantine_reason': ' '.join(reasons)})
        else:
            kept.append(finding)
    return kept, quarantined


def export_draft(paper, review, decisions):
    if review['paper_id']!=paper['id'] or decisions['paper_id']!=paper['id']:
        raise ValueError('Decisions belong to a different manuscript')
    blocks=copy.deepcopy(paper['blocks'])
    by={b['id']:b for b in blocks}
    edits={}
    for a in review['findings']:
        if decisions['items'].get(a['id'],'proposed')!='accepted':
            continue
        if a['kind']!='edit' or a['placement']!='anchored':
            raise ValueError('Only anchored edit suggestions can be accepted')
        b=by[a['block_id']]
        if digest(b['text'])!=a['block_sha256'] or b['text'][a['start']:a['end']]!=a['quote']:
            raise ValueError('Edit anchor no longer matches the immutable source')
        edits.setdefault(a['block_id'],[]).append(a)
    for bid, pending in edits.items():
        ordered=sorted(pending,key=lambda a:a['start'])
        for left,right in zip(ordered,ordered[1:]):
            if left['end']>right['start']:
                raise ValueError('Accepted suggestions overlap. Choose one or revise them before applying.')
        for a in reversed(ordered):
            b=by[bid]
            b['text']=b['text'][:a['start']]+a['replacement']+b['text'][a['end']:]
        by[bid]['sha256']=digest(by[bid]['text'])
    return {'paper_id':paper['id'],'source_sha256':paper['source_sha256'],'review_id':review['id'],
            'original_blocks':paper['blocks'],'proposed_blocks':blocks,'decisions':decisions,
            'findings':review['findings'],'source_findings':review.get('raw_response',{}).get('source_findings',[]),
            'source_hypotheses':review.get('raw_response',{}).get('source_hypotheses',[]),
            'candidate_inventory':review.get('raw_response',{}).get('candidate_inventory'),
            'evidence_handle_audit':review.get('raw_response',{}).get('evidence_handle_audit'),
            'source_cards':review.get('raw_response',{}).get('source_cards',[]),
            'result_map':review.get('raw_response',{}).get('result_map'),
            'result_comparisons':review.get('raw_response',{}).get('result_comparisons',[]),
            'domain_profile':review.get('raw_response',{}).get('domain_profile'),
            'source_validation':review.get('raw_response',{}).get('source_validation'),
            'investigation_context':review.get('raw_response',{}).get('investigation_context',{}),
            'source_dispositions':review.get('raw_response',{}).get('source_dispositions',[]),
            # `component_decisions` and `epistemic_audit` were the debate's own
            # keys. 0.9 has no debate, so every export carried two empty fields
            # a consumer had to decide what to do with; they are gone rather
            # than kept as a shape nothing fills.
            'component_validation':review.get('raw_response',{}).get('component_validation'),
            'workflow_version':review.get('raw_response',{}).get('workflow_version'),
            'coverage_record':review.get('raw_response',{}).get('coverage_record'),
            'experiment_map':review.get('raw_response',{}).get('experiment_map'),
            'task_results':review.get('raw_response',{}).get('task_results'),
            'task_validation':review.get('raw_response',{}).get('task_validation'),
            'evidence_provenance':review.get('raw_response',{}).get('evidence_provenance'),
            'source_disposition_validation':review.get('raw_response',{}).get('source_disposition_validation',[]),
            'unexpected_source_disposition_ids':review.get('raw_response',{}).get('unexpected_source_disposition_ids',[]),
            'handoff':{key:review.get('raw_response',{}).get(key) for key in
                       ('editor_findings_count','verification_queue_count','ledger_limitation')},
            'limitations':review.get('limitations',[]),'issue_status':'Acceptance edits wording only. Scientific issues remain open.',
            'format':'PRA review package. This is not native Word tracked changes or a PDF layout reproduction.'}


def save_review(root, response, review_id):
    if not re.fullmatch(r'[a-z0-9_-]{1,50}',review_id):
        raise ValueError('Invalid review ID')
    paper=verify_workspace(root)
    value=normalize_review(paper,response,review_id)
    path=Path(root)/'reviews'/f'{review_id}.json'
    if path.exists():
        if read(path)==value:
            return value
        raise ValueError('Review ID already exists with different content')
    write(path,value)
    return value
