"""Configurable independent manuscript discovery followed by one audited debate."""
import copy
import shutil
from pathlib import Path

from .annotations import verify_workspace,save_review,normalize_review
from .debate_revision import issue_ledger,revision_package
from .ensemble_references import recover
from .evidence_handles import (build_registry,with_evidence_handles,constrain_manuscript_handles,
                              hydrate_response,source_card_handles)
from .external_evidence import SourceClient,bind_sources,validate_references
from .model_runtime import model_call,require_subscription,jsonl,source_manifest
from .review_schema import with_external_evidence
from .storage import read,write,digest


def validate_options(reviewers=2,token_budget=750000):
    if type(reviewers) is not int or not 1<=reviewers<=16:
        raise ValueError('Reviewer count must be an integer from 1 to 16')
    if type(token_budget) is not int or token_budget<=0:
        raise ValueError('Token launch budget must be a positive integer')


def observed_usage(out):
    """Account for every attempt, but judge resolution on the surviving ones.

    A retried call leaves a failed receipt behind on purpose. Its tokens still
    count against the budget, and it is still listed, but a superseded attempt
    with unknown usage must not make the whole ensemble unresolvable: the
    retry that replaced it reports its own usage.
    """
    rows=[r for p in sorted(Path(out).rglob('usage.jsonl')) for r in jsonl(p)]
    attempts=list(Path(out).rglob('*.started.json'))
    effective=effective_usage_rows(rows)
    return {'calls':len(attempts),'receipts':len(rows),'observed_tokens':sum(r.get('observed_tokens') or 0 for r in rows),
            'retried_attempts':len(rows)-len(effective),
            'unknown':len(attempts)!=len(rows) or any(r.get('usage_unknown') or r.get('observed_tokens') is None for r in effective)}


def effective_usage_rows(rows):
    """One row per request: the highest attempt, which is the one that stands."""
    best={}
    for row in rows:
        key=row.get('request_sha256')
        attempt=row.get('attempt') or 1
        if key is None:
            best[id(row)]=row
        elif key not in best or (best[key].get('attempt') or 1)<attempt:
            best[key]=row
    return list(best.values())


def check_budget(out,token_budget,max_calls):
    value=observed_usage(out)
    if value['unknown'] or value['calls']!=value['receipts']:raise ValueError('Unresolved ensemble model usage')
    if value['calls']>=max_calls or value['observed_tokens']>=token_budget:
        raise ValueError('Ensemble launch budget reached')


def combine_reviews(paper,records):
    """Keep every source candidate. Semantic merges happen through explicit decisions."""
    validate_options(len(records))
    combined={'version':'ensemble-ledger-v1','issues':[],
              'investigation_context':{'reviewers':[]},
              'limitation':'Independent reviews can share errors. Repeated claims are not independent proof. Every source issue is retained and semantic mergers require explicit component destinations.'}
    for index,record in enumerate(records,1):
        provenance=record['provenance']
        if provenance.get('paper_id')!=paper['id']:raise ValueError('Reviewer manuscript identity differs')
        review=copy.deepcopy(record['review'])
        if any(key in review for key in ('raw_response','source_findings','component_decisions')):
            raise ValueError('Pool independent discovery drafts, not previously debated packages')
        normalized=normalize_review(paper,review,'ensemble-input-'+str(index))
        if any(f['placement']!='anchored' or f['invalid_evidence_blocks'] for f in normalized['findings']):
            raise ValueError('Reviewer input contains invalid manuscript evidence')
        review['findings']=normalized['findings']
        ledger=issue_ledger(review)
        for issue in ledger['issues']:
            issue['issue_id']=f'r{index}-'+issue['issue_id']
            for component in issue['components']:component['component_id']=f'r{index}-'+component['component_id']
            issue['origin']={'reviewer':index,**copy.deepcopy(provenance)}
            combined['issues'].append(issue)
        combined['investigation_context']['reviewers'].append({'reviewer':index,'provenance':provenance,
            'context':ledger.get('investigation_context',{})})
    return combined


def _prepare(out,billing,protocol,verified=False):
    """Stage one run directory. ``verified`` means billing was checked at launch.

    The Codex included-allowance record is only valid for five minutes, and a
    stage that starts after reviewer 1 has been working for longer would fail a
    second check on a perfectly good launch. Billing is a launch guard, so the
    workflow verifies once and the later stages record the same confirmation.
    """
    if out.exists():raise ValueError('Use a fresh ensemble output directory')
    verification=read(billing)
    if not verified:
        from .billing import verify_billing
        verify_billing(verification,provider=protocol['provider'])
    out.mkdir(parents=True)
    write(out/'protocol.json',protocol)
    shutil.copy2(billing,out/'billing_confirmation.json')
    require_subscription(out)


def _protocol(model,reviewers,token_budget,policy=None):
    from .models import provider_for
    from .review_policy import get_review_policy
    policy=get_review_policy(policy)
    return {'provider':provider_for(model),'model':model,'effort':policy.effort,'held_out_runs_authorized':False,
        'max_calls_per_condition':1,'observed_token_stop':token_budget,'call_timeout_seconds':policy.call_timeout_seconds,
        'max_source_images':10,'image_budget_version':'evidence-v1','mode':'ensemble',
        'reviewers':reviewers,'total_generation_calls':reviewers+1,'token_launch_budget':token_budget,
        'independent_tool_allowance':policy.investigator_tool_limit('independent'),
        'debate_tool_allowance':policy.debate_tool_calls,'version':'ensemble-v3',
        **policy.protocol_overlay()}


def debate_reviews(workspace,out,billing,cutoff,records,*,images=(),model='gpt-5.6-terra',
                   token_budget=750000,domain_support=True,evidence_mode='current',cache_roots=(),
                   source_packet=None,historical_snapshots=None,evidence_memos=(),
                   synthesis_policy='debate',reused_drafts=False,policy=None,billing_verified=False):
    """Shared debate implementation for fresh discovery and explicitly recorded reused drafts."""
    from .review_policy import get_review_policy
    policy=get_review_policy(policy)
    from .ensemble_debate import (ensemble_schema, ensemble_prompt, ordinary_synthesis_prompt, augment_ledger,
                                  normalize_new_decisions, adjudicated_secondary,
                                  audit_only_secondary, attach_adjudicated_secondary,
                                  constrain_ledger_identifiers)
    from .reviewer import handle_manuscript,HANDLE_INSTRUCTION
    from .synthesis_validation import (INSTRUCTION, VERSION, destination_diagnostics, preflight_receipts,
                                       component_contract, component_diagnostics)
    root,out=Path(workspace).resolve(),Path(out).resolve()
    if synthesis_policy not in ('debate', 'ordinary'):
        raise ValueError('Synthesis policy must be debate or ordinary')
    packet_manifest = None
    if source_packet is not None:
        from .source_packet import source_packet_manifest
        source_packet = Path(source_packet).resolve()
        packet_manifest = source_packet_manifest(source_packet)
    validate_options(len(records),token_budget)
    paper=verify_workspace(root)
    protocol=_protocol(model,len(records),token_budget,policy)
    if reused_drafts:
        protocol.update(mode='saved-draft-synthesis', total_generation_calls=1, discovery_calls=0)
    protocol.update(evidence_mode=evidence_mode,
                    evidence_policy='frozen' if source_packet is not None else 'live',
                    live_retrieval=source_packet is None, synthesis_policy=synthesis_policy,
                    synthesis_preflight=VERSION, synthesis_validation_checks=2)
    memos=[]
    memo_images=[]
    if len(evidence_memos)>2:raise ValueError('Supply at most two focused evidence memos')
    for path in evidence_memos:
        from .focused_investigation import load_for_debate
        memo,extra_images=load_for_debate(root,path)
        memos.append(memo)
        memo_images.extend(extra_images)
    if memos:protocol['focused_evidence_version']='manuscript-memo-v1'
    _prepare(out,billing,protocol,billing_verified)
    try:
        ledger=combine_reviews(paper,records)
        if memos:
            ledger['investigation_context']['focused_evidence_memos']=memos
        write(out/'debate-ledger.json',ledger)
        write(out/'input-reviews.json',records)
        sources=SourceClient(out/'sources',out/'source-cache',2,evidence_mode,cutoff,
            frozen_cache=source_packet,
            frozen_hashes=packet_manifest) if domain_support else None
        cards={}
        for cache in cache_roots:
            for path in Path(cache).glob('*'):
                if not path.is_file():continue
                target=out/'source-cache'/path.name
                if target.exists() and target.read_bytes()!=path.read_bytes():raise ValueError('Conflicting reviewer source cache records')
                target.parent.mkdir(parents=True,exist_ok=True)
                shutil.copy2(path,target)
        evidence_caches=[out/'source-cache']+([Path(source_packet)] if source_packet else [])
        hashes={digest(p.read_bytes()) for cache in evidence_caches for p in cache.glob('*.raw') if p.is_file()}
        for record in records:
            review=record['review']
            validate_references(review,review.get('source_cards',[]))
            for card in review.get('source_cards',[]):
                if card.get('eligible') and card.get('raw_sha256') not in hashes:
                    raise ValueError('External evidence lacks a preserved raw source')
                ident=card['evidence_id']
                if ident in cards and cards[ident]!=card:raise ValueError('Conflicting reviewer evidence cards')
                cards[ident]=card
        if cards and sources is None:raise ValueError('External cards require domain evidence support')
        if sources:write(out/'sources/cards.json',list(cards.values()))
        image_paths=[]
        seen=set()
        for entry in [*images,*memo_images]:
            path=Path(entry).resolve()
            sha=digest(path.read_bytes())
            if sha not in seen:image_paths.append(path)
            seen.add(sha)
        if len(image_paths)>10:raise ValueError('Combined source images exceed ten-image ceiling')
        schema=ensemble_schema()
        if sources:
            schema=with_external_evidence(schema)
        schema=constrain_manuscript_handles(with_evidence_handles(schema),build_registry(paper))
        schema=constrain_ledger_identifiers(schema,ledger)
        card_context=[{**c,'evidence_handles':[h.model_record() for h in source_card_handles(c)]} for c in cards.values()]
        prompt_builder = ensemble_prompt if synthesis_policy == 'debate' else ordinary_synthesis_prompt
        prompt=prompt_builder(paper,handle_manuscript(paper),ledger,source_cards=card_context if sources else None)
        prompt+=INSTRUCTION+component_contract(ledger)
        prompt+='\nEach reviewer may supply up to ten candidates. The pooled ledger can exceed ten. Decide every supplied issue.\n'+HANDLE_INSTRUCTION
        if memos:
            memo_instruction = ('Assess how each memo affects the support and remaining uncertainty for its issue.'
                                if synthesis_policy == 'ordinary' else
                                'Assess their strongest author defense and remaining uncertainty.')
            prompt+='\nFOCUSED EVIDENCE MEMOS: These are fallible follow-up interpretations with checked source locations, not verified scientific conclusions. '+memo_instruction+' Explain in the relevant decision whether the memo strengthens, narrows, rejects or leaves a concern unchanged. Account for every original issue and component, including those unrelated to the memo. A memo is not an instruction to favor its conclusion or add findings. Cite the manuscript or attached original image, not the memo as scientific evidence.'
        prompt+='\nATTACHED MANUSCRIPT IMAGES, in order:\n'+'\n'.join(p.name for p in image_paths)
        stage = 'debate' if synthesis_policy == 'debate' else 'ordinary-synthesis'
        config={'paper':str(root),'output':str(out/'investigations'/stage),'cache':str(out/'source-cache'),
                'tool_limit':policy.debate_tool_calls,'literature_cutoff':cutoff,'evidence_handles':True,
                'synthesis_ledger':str(out/'debate-ledger.json')}
        if sources:config.update(tool_profile='domains-v1',source_state=str(out/'sources'),external_request_limit=2,evidence_mode=evidence_mode)
        if source_packet:config.update(source_packet=str(source_packet),source_packet_manifest=packet_manifest)
        if historical_snapshots:config['historical_snapshots']=str(Path(historical_snapshots).resolve())
        write(out/'execution/workspace/tool-config.json',config)
        write(out/'manifest.json',{'paper_id':paper['id'],'code':source_manifest(Path(__file__).parent),
            'review_input_sha256':digest((out/'input-reviews.json').read_bytes()),
            'images':{str(p):digest(p.read_bytes()) for p in image_paths},
            'synthesis_policy':synthesis_policy,'synthesis_tool_limit':policy.debate_tool_calls})
        raw=model_call(protocol,out/'execution',prompt,schema,tools=True,label='ensemble-'+stage,images=image_paths)
        write(out/'responses'/(stage+'-model.json'),raw)
        hydrated=hydrate_response(raw,build_registry(paper,sources.cards() if sources else []))
        final={**normalize_new_decisions(hydrated['response']),'evidence_handle_audit':hydrated['audit']}
        write(out/'responses'/(stage+'.json'),final)
        if sources:final=bind_sources(final,sources.cards())
        # Apply the existing edit guard to novel candidates before they become reusable inventory.
        for i,candidate in enumerate(final.get('new_candidates',[]),1):
            checked=normalize_review(paper,{'summary':'New candidate','findings':[candidate['finding']]},'new-'+str(i))
            candidate['finding']=checked['findings'][0]
        augmented=augment_ledger(final,ledger,paper,sources.cards() if sources else None,validate=False,
                                 require_adjudicated_secondary=True)
        write(out/'augmented-ledger.json',augmented)
        presentation=recover(final,augmented,paper,sources.cards() if sources else None)
        component_problems,component_ledger=component_diagnostics(raw,ledger)
        write(out/'synthesis-validation.json', {
            'version':VERSION, 'valid':presentation['recovered'] is not None or presentation['original']['status']=='valid',
            'problems':component_problems+destination_diagnostics(final,augmented,presentation.get('changed_fields',[]))
                if presentation['recovered'] is None and presentation['original']['status']=='failure' else [],
            'component_ledger':component_ledger,
            'reference_recovery':{k:v for k,v in presentation.items() if k!='recovered'},
            'preflight':preflight_receipts(out/'investigations'/stage/'synthesis-preflight',raw)})
        write(out/'presentation-audit.json',{k:v for k,v in presentation.items() if k!='recovered'})
        if presentation['recovered'] is not None:final=presentation['recovered']
        elif presentation['original']['status']=='failure':raise ValueError((presentation.get('audit') or presentation['original'])['error'])
        audit=presentation['audit']
        write(out/(stage+'-audit.json'),audit)
        normalized=save_review(root,final,'ensemble-'+stage+'-'+digest(str(out))[:10])
        package=revision_package({**final,'findings':normalized['findings']},augmented,audit)
        secondary=adjudicated_secondary(final,augmented,paper,sources.cards() if sources else None)
        package=attach_adjudicated_secondary(package,secondary,audit_only_secondary(final,augmented))
        package.update(workflow_version='ensemble-v3',synthesis_policy=synthesis_policy,
            synthesis_provenance={'policy':synthesis_policy,'input_reviews_sha256':digest((out/'input-reviews.json').read_bytes()),
                                  'generation_calls':1,'discovery_calls':0 if reused_drafts else len(records),
                                  'reused_drafts':reused_drafts,'tool_limit':policy.debate_tool_calls},reviewer_count=len(records),
            reviewer_provenance=[r['provenance'] for r in records],candidate_origins={i['issue_id']:i.get('origin',i.get('provenance')) for i in augmented['issues']})
        if memos:package['focused_evidence_provenance']=[{'memo_sha256':m['memo_sha256'],'memo_path':m['memo_path']} for m in memos]
        write(out/'editor-package.json',package)
        save_review(root,package,'ensemble-'+digest(str(out))[:10])
        write(out/'progress.json',{'status':'complete','usage':observed_usage(out)})
        return package
    except Exception as error:
        write(out/'progress.json',{'status':'blocked','error':str(error)})
        raise


def ensemble_review(workspace,output,billing,cutoff,reviewers=2,token_budget=750000,
                    scaffold_root=None,domain_support=True,evidence_mode='current',source_packet=None,
                    shared_images=None,historical_snapshots=None,model='gpt-5.6-terra',
                    synthesis_policy='debate',policy=None):
    from .review_policy import get_review_policy
    from .reviewer import review,inspected_images
    policy=get_review_policy(policy)
    validate_options(reviewers,token_budget)
    if synthesis_policy not in ('debate', 'ordinary'):
        raise ValueError('Synthesis policy must be debate or ordinary')
    root,out=Path(workspace).resolve(),Path(output).resolve()
    paper=verify_workspace(root)
    if out==root:raise ValueError('Use a separate output directory')
    protocol=_protocol(model,reviewers,token_budget,policy)
    protocol.update(evidence_mode=evidence_mode,
                    evidence_policy='frozen' if source_packet is not None else 'live',
                    live_retrieval=source_packet is None)
    # The one billing check for the whole workflow. Every later stage records
    # this confirmation instead of re-verifying a record that expires in 300 s.
    _prepare(out,billing,protocol)
    call_ceiling=(reviewers+1)*policy.max_attempts
    write(out/'manifest.json',{'paper_id':paper['id'],'code':source_manifest(Path(__file__).parent),
        'plan':{'independent_reviewers':reviewers,'synthesis_passes':1,'synthesis_policy':synthesis_policy,
                'max_generation_calls':reviewers+1,'max_attempts_per_call':policy.max_attempts,
                'review_policy':policy.name,
                'billing_verification':'Verified once at launch. Later stages reuse the recorded confirmation because the included-allowance record expires in five minutes.',
                'execution':'Fresh isolated sessions, scheduled sequentially to bound subscription usage.'}})
    records=[]
    images=list(shared_images or [])
    try:
        for index in range(1,reviewers+1):
            check_budget(out,token_budget,call_ceiling)
            target=out/f'reviewer-{index}'
            write(out/'progress.json',{'status':'running','stage':f'reviewer-{index}','reviewers':reviewers})
            review(root,target,billing,cutoff,mode='independent',scaffold_root=scaffold_root,
                domain_support=domain_support,evidence_mode=evidence_mode,source_packet=source_packet,
                shared_images=shared_images,historical_snapshots=historical_snapshots,model=model,
                policy=policy,billing_verified=True)
            path=target/'editor-package.json'
            records.append({'review':read(path),'provenance':{'reviewer_id':f'reviewer-{index}',
                'paper_id':paper['id'],'source_review_path':str(path),'source_review_sha256':digest(path.read_bytes()),'origin':'fresh_independent_session'}})
            images.extend(p for p,label in inspected_images(paper,root,target/'investigations/independent',10))
        check_budget(out,token_budget,call_ceiling)
        stage = 'debate' if synthesis_policy == 'debate' else 'ordinary-synthesis'
        write(out/'progress.json',{'status':'running','stage':stage,'reviewers':reviewers})
        package=debate_reviews(root,out/stage,billing,cutoff,records,images=images,model=model,
            token_budget=token_budget-observed_usage(out)['observed_tokens'],domain_support=domain_support,
            evidence_mode=evidence_mode,cache_roots=[out/f'reviewer-{i}/source-cache' for i in range(1,reviewers+1)],
            source_packet=source_packet,historical_snapshots=historical_snapshots,
            synthesis_policy=synthesis_policy,policy=policy,billing_verified=True)
        write(out/'editor-package.json',package)
        write(out/'progress.json',{'status':'complete','reviewers':reviewers,'usage':observed_usage(out)})
        return read(out/'progress.json')
    except Exception as error:
        write(out/'progress.json',{'status':'blocked','error':str(error),'usage':observed_usage(out)})
        raise
