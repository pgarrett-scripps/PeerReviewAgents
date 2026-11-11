"""Reusable bounded review and evidence-focused debate."""
import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from .annotations import verify_workspace,save_review,quarantine_findings
from .model_runtime import model_call,require_subscription,source_manifest,jsonl,prepare_retry
from .review_schema import CLARIFICATION_POLICY,number_word
from .review_policy import get_review_policy
from .storage import read,write,digest
from .discovery import AREAS,DESIGN_CHECKS,coverage_record,discovery_instruction,investigation_schema
from .debate_revision import debate_revision_schema,issue_ledger,revision_prompt,revision_package
from .evidence_handles import (build_registry, source_card_handles, with_evidence_handles,
                              constrain_manuscript_handles, hydrate_response, hydrate_review_response)


HANDLE_INSTRUCTION = '''SOURCE REFERENCE FORMAT: Select the supplied immutable handle IDs instead of copying source quotations or writing block IDs in output reference fields. This instruction replaces earlier output directions about copying quotes. Use a manuscript handle with anchor_eligible true for anchor_handle. Use only manuscript handles in manuscript_evidence_handles. Put external handles in source_evidence entries as source_handle. For coverage and decision evidence use evidence_handle. Coverage evidence must use manuscript handles. Source card tools return external handles with exact source text. The system inserts the exact original text and source identifiers and records the span. Unknown handles or the wrong source type fail validation. final_quote in a debate component is different: it must still exactly quote your own final finding comment to demonstrate retention. An edit replaces exactly the selected anchor span, so do not write a replacement for a larger passage. Handle validity establishes source location, not scientific truth.'''

SELF_REVISION_HEAD = '''SECOND PASS: Reconsider and improve your own draft review against the entire supplied manuscript and figures. Check scientific validity, specificity and proportionality. Resolve misunderstandings or unsupported premises. Look for important omissions as well as mistakes in the draft. Keep useful concerns, refine or remove weak ones, and add issues only when the available evidence supports them. Return your best final scientific review, with '''
SELF_REVISION_BOUND = 'at most five findings'
SELF_REVISION_OPEN = 'every finding the evidence supports'
SELF_REVISION_TAIL = ''' under the same overall word budget. Use ordinary self-revision, without simulating multiple reviewers or a debate. Your prior draft is fallible and is not source evidence. Reselect source handles for the final output. You have at most two tool calls in this pass.'''


COMPLETE_WITHOUT_FINDINGS = 'complete-without-findings'
GUARD_STATUS_POLICY = ('A finding that fails evidence validation is quarantined with a reason instead of '
    'ending the run, so a run can finish having delivered nothing. That is not a clean success: '
    'progress.json and the editor package carry review_guard_status, findings_delivered and '
    'quarantined_findings_count, a run whose findings were all quarantined finishes with status '
    'complete-without-findings, and the command line exits non-zero. Before this policy such a run raised, '
    'so a caller that read a completed run as a delivered review must now check the status.')


def guard_record(review):
    """What one stage delivered, and what the evidence guard removed from it."""
    return {'review_guard_status':review.get('review_guard_status','clean'),
            'findings_delivered':len(review.get('findings') or []),
            'quarantined_findings_count':len(review.get('quarantined_findings') or [])}


def combined_guard(stages):
    """One delivered record for a run: the last stage, with every quarantine counted."""
    record={k:v for k,v in dict(stages[-1]).items() if k!='stage'}
    record['quarantined_findings_count']=sum(stage['quarantined_findings_count'] for stage in stages)
    if record['quarantined_findings_count']:
        if not record['findings_delivered']:
            record['review_guard_status']='all-findings-quarantined'
        elif record['review_guard_status']=='clean':
            record['review_guard_status']='partially-quarantined'
    return record


def self_revision_instruction(finding_cap=5, tool_calls=2):
    """The second-pass instruction under one policy's finding and tool budget."""
    if finding_cap is None:
        bound = SELF_REVISION_OPEN
    elif finding_cap == 5:
        bound = SELF_REVISION_BOUND
    else:
        bound = f'at most {number_word(finding_cap)} findings'
    text = SELF_REVISION_HEAD + bound + SELF_REVISION_TAIL
    if tool_calls != 2:
        text = text.replace('at most two tool calls in this pass',
                            f'at most {number_word(tool_calls)} tool calls in this pass')
    return text + '\n' + CLARIFICATION_POLICY


SELF_REVISION = self_revision_instruction()

INDEPENDENT_HEAD = 'Review the entire paper independently. Identify consequential, source-supported scientific concerns and actionable uncertainties. Consider strengths and author explanations. '
INDEPENDENT_BOUND = 'Return up to ten concise candidate findings, overriding the shared five-finding instruction for this discovery stage.'
INDEPENDENT_TAIL = ' Preserve useful secondary concerns. Do not manufacture issues to reach a count. Your candidates will later be assessed alongside other independently produced reviews.'


def independent_instruction(candidate_cap=10, finding_cap=5):
    """The independent discovery instruction under one policy's caps."""
    if candidate_cap == 10 and finding_cap == 5:
        bound = INDEPENDENT_BOUND
    elif candidate_cap is None:
        bound = ('Return every concise candidate finding your investigation supports, overriding any shared '
                 'finding limit for this discovery stage.')
    else:
        bound = (f'Return up to {number_word(candidate_cap)} concise candidate findings, overriding any '
                 'shared finding limit for this discovery stage.')
    return INDEPENDENT_HEAD + bound + INDEPENDENT_TAIL

QUARANTINE_POLICY = 'A finding whose quote could not be anchored in its source block, or that cites unknown block IDs, is moved to quarantined_findings with a reason instead of ending the run. Quarantined findings are preserved verbatim and are not endorsed. The remaining findings passed source location checks only, which does not establish scientific correctness.'

MAX_SOURCE_IMAGES = 10


def shared_source_images(paths):
    """Resolve the complete bounded image set without truncation."""
    images = [Path(path).resolve() for path in (paths or [])]
    if len(images) > MAX_SOURCE_IMAGES or any(not path.is_file() for path in images):
        raise ValueError(f'Supply at most {MAX_SOURCE_IMAGES} existing shared source images')
    return images


def handle_manuscript(paper):
    registry = build_registry(paper)
    by_block = {}
    for handle in registry.handles:
        by_block.setdefault(handle.source_id, []).append(handle)
    lines = []
    for block in paper['blocks']:
        lines.append(f"[{block['id']} | {block.get('section','')} | page {block.get('page') or '?'}]")
        for handle in by_block[block['id']]:
            eligibility = '' if handle.anchor_eligible else ' (evidence only, not unique anchor)'
            lines.append(f'[{handle.handle_id}{eligibility}] {handle.text}')
    return '\n'.join(lines)


def save_initial_checkpoint(out, prompt, schema, first, images):
    """Preserve the investigator state before either second-pass strategy runs."""
    target=out/'initial-checkpoint'
    if target.exists():
        raise ValueError('Initial checkpoint already exists')
    target.mkdir()
    write(target/'input.json',{'prompt':prompt,'schema':schema,'review':first,
        'images':[{'path':str(p),'label':label,'sha256':digest(p.read_bytes())} for p,label in images],
        'tool_config':read(out/'execution/workspace/tool-config.json')})
    for name in ('sources','source-cache'):
        if (out/name).exists():
            shutil.copytree(out/name,target/name)
    write(target/'manifest.json',{'files':source_manifest(target),
        'scope':'Investigator output and evidence state captured before debate. No debate output is included.'})


def evidence_packet(paper,reviews):
    ids={f['block_id'] for review in reviews for f in review['findings']}
    ids.update(b for review in reviews for f in review['findings'] for b in f.get('evidence_blocks',[]))
    return {'title':paper['title'],'source_blocks':[b for b in paper['blocks'] if b['id'] in ids],
            'section_index':paper['sections'],'assets':paper['assets'],
            'limitation':'This packet includes cited evidence, not the complete manuscript. Use tools for omitted context. A missing item in this packet is not proof of absence in the full paper.'}


def debate_prompt(paper,reviews,policy=None):
    """The retention cap and the tool allowance come from the named policy."""
    policy=get_review_policy(policy)
    tagged=json.loads(json.dumps(reviews))
    for i,review in enumerate(tagged,1):
        for j,finding in enumerate(review['findings'],1):
            finding['source_finding_id']=f'r{i}-f{j}'
    retention=('Retain every distinct finding the debate leaves standing' if policy.finding_cap is None
               else f'Retain at most {number_word(policy.finding_cap)} distinct findings')
    tools=f'You have {number_word(policy.debate_tool_calls)} tool calls for missing evidence.'
    return policy.shared_instructions()+'''\nDebate the supplied reviews. For each consequential allegation, articulate the strongest defense of the authors and decide whether the concern survives, needs narrowing, or should be dropped. Preserve independent useful findings and identify duplicates. Do not assume agreement makes a claim true. Preserve substantive counterarguments and dropped hypotheses in the output. '''+retention+''', and do not add unsupported replacement facts. '''+tools+''' An image-based finding cannot be rejected merely because its evidence is absent from extracted text. Inspect the attached source image or mark it as needs_verification. Reconcile the reviews into a final report for an editor. Give one explicit source_dispositions entry for every source_finding_id. A shared source anchor does not make findings duplicates. Use final finding numbers as one-based positions in your findings array.\nREVIEWS:\n'''+json.dumps(tagged,ensure_ascii=False)+'\nSOURCE EVIDENCE:\n'+json.dumps(evidence_packet(paper,reviews),ensure_ascii=False)


def inspected_images(paper,workspace,investigation,limit=3):
    if type(limit) is not int or not 1<=limit<=10:
        raise ValueError('Image handoff limit must be between one and ten')
    root=Path(workspace).resolve()
    verified=verify_workspace(root)
    if paper.get('id')!=verified.get('id'):
        raise ValueError('Paper does not match the verified manuscript workspace')
    paper=verified
    manifest=read(root/'manifest.json')['files']
    log=Path(investigation)/'tool-log.jsonl'
    images=[]
    for row in jsonl(log):
        if not isinstance(row,dict) or not isinstance(row.get('result'),dict):continue
        if 'label' not in row['result'] or 'request_rejected' in row['result']:continue
        arguments=row.get('arguments')
        if not isinstance(arguments,dict):continue
        path=None
        label=None
        if row.get('tool')=='view_supplement':
            asset_id=arguments.get('id')
            if row['result'].get('source_sha256'):
                from .supplement_view import supplement_page
                try:
                    with tempfile.TemporaryDirectory() as temporary:
                        rendered, metadata = supplement_page(root,paper,asset_id,temporary,arguments.get('page',1))
                        if any(row['result'].get(k)!=metadata[k] for k in ('source_sha256','page','asset_id')):
                            continue
                        destination=Path(investigation)/rendered.name
                        if rendered.is_relative_to(root):
                            destination=rendered
                        if destination.is_file() and digest(destination.read_bytes())==digest(rendered.read_bytes()):
                            if destination not in [p for p,label in images]:
                                images.append((destination,metadata['label']))
                except (ValueError,OSError,subprocess.SubprocessError):
                    continue
                continue
            asset=next((a for a in paper['assets'] if a['id']==asset_id and a.get('preview')),None)
            if asset:
                relative=asset['preview']
                path=(root/relative).resolve()
                if not path.is_relative_to(root):raise ValueError('Image path escapes manuscript workspace')
                expected=manifest.get(relative)
                if not expected or not path.is_file() or digest(path.read_bytes())!=expected:
                    path=None
                else:
                    label=asset['label']
        elif row.get('tool')=='view_page':
            page=arguments.get('page')
            if type(page) is not int or not 1<=page<=paper['statistics']['pdf_pages']:continue
            candidate=(Path(investigation)/f'page_{page:03d}.png').resolve()
            original=root/'original.pdf'
            if not candidate.is_file():continue
            with tempfile.TemporaryDirectory() as temporary:
                rendered=Path(temporary)/'page.png'
                try:
                    subprocess.run(['pdftoppm','-f',str(page),'-l',str(page),'-scale-to','1500',
                                    '-singlefile','-png',str(original),str(rendered.with_suffix(''))],
                                   check=True,capture_output=True,timeout=30)
                except (OSError,subprocess.SubprocessError):
                    continue
                if digest(candidate.read_bytes())==digest(rendered.read_bytes()):
                    path=candidate
                    label=f'Original PDF page {page}'
        if path and path not in [p for p,label in images]:images.append((path,label))
    return images[:limit]


def editor_package(final,reviews,paper=None):
    """Preserve unmatched findings as a queue, without promoting their validity."""
    result=json.loads(json.dumps(final))
    sources=[{'source_finding_id':f'r{i+1}-f{j+1}','review_number':i+1,
              'finding_number':j+1,'finding':f}
             for i,r in enumerate(reviews) for j,f in enumerate(r['findings'])]
    result['source_findings']=sources
    dispositions=final.get('source_dispositions')
    explicit=isinstance(dispositions,list)
    by_id={source['source_finding_id']:source for source in sources}
    known_blocks={block for source in sources
                  for block in [source['finding'].get('block_id'),*source['finding'].get('evidence_blocks',[])]
                  if isinstance(block,str)}
    if paper is not None:
        known_blocks={block['id'] for block in paper['blocks']}
    decisions={}
    invalid=set()
    unexpected=[]
    if explicit:
        for decision in dispositions:
            if not isinstance(decision,dict):
                continue
            if decision.get('source_finding_id') not in by_id:
                unexpected.append(decision.get('source_finding_id'))
                continue
            source_id=decision['source_finding_id']
            if source_id in decisions:
                invalid.add(source_id)
                continue
            refs=decision.get('final_finding_numbers')
            valid_refs=(isinstance(refs,list) and all(type(n) is int and 1<=n<=len(final['findings']) for n in refs))
            evidence=decision.get('evidence_blocks')
            valid_evidence=(isinstance(evidence,list) and
                            all(isinstance(block,str) and block in known_blocks for block in evidence))
            valid_reason=bool(str(decision.get('reason','')).strip())
            disposition=decision.get('disposition')
            valid=(disposition in ('retained','merged') and valid_refs and bool(refs))
            valid=valid or (disposition in ('rejected','needs_verification') and valid_refs and not refs)
            valid=valid and valid_reason and valid_evidence
            if valid:decisions[source_id]=decision
            else:invalid.add(source_id)
    final_signatures={json.dumps(f,sort_keys=True,ensure_ascii=False) for f in final['findings']}
    queued=[]
    for source in sources:
        source_id=source['source_finding_id']
        finding=source['finding']
        decision=decisions.get(source_id)
        if explicit and source_id not in invalid and decision and decision['disposition'] in ('retained','merged','rejected'):
            continue
        if not explicit and json.dumps(finding,sort_keys=True,ensure_ascii=False) in final_signatures:
            continue
        reason='The debate marked this earlier finding as requiring verification.'
        if not explicit:
            reason='The legacy debate did not retain this exact earlier finding or provide an explicit disposition.'
        elif source_id in invalid or decision is None:
            reason='The debate did not provide a valid disposition for this earlier finding.'
        queued.append({**finding,'kind':'question','replacement':'','disposition':'needs_verification',
                       'endorsed':False,'source_finding_id':source_id,
                       'title':'Needs verification: '+finding['title'],
                       'comment':finding['comment']+' '+reason+' It remains available for verification and is not endorsed by the editor.'})
    result['source_disposition_validation']=[
        {'source_finding_id':source['source_finding_id'],
         'status':('invalid' if source['source_finding_id'] in invalid else
                   'valid' if source['source_finding_id'] in decisions else
                   'legacy_exact_match' if not explicit and json.dumps(source['finding'],sort_keys=True,ensure_ascii=False) in final_signatures else
                   'legacy_unresolved' if not explicit else 'missing')}
        for source in sources]
    result['unexpected_source_disposition_ids']=unexpected
    result['findings'].extend(queued)
    result['editor_findings_count']=len(final['findings'])
    result['verification_queue_count']=len(queued)
    if queued:result['summary']+=f' {len(queued)} earlier finding(s) remain in the verification queue.'
    result['ledger_limitation']='Source anchors locate passages and do not adjudicate whether concerns are equivalent. Explicit dispositions govern the handoff. Missing or invalid dispositions remain unendorsed and all original findings are preserved separately.'
    return result


def review(workspace,output,cutoff,mode='panel',scaffold_root=None,domain_support=True,evidence_mode='current',source_packet=None,shared_images=None,historical_snapshots=None,model=None,reviewers=2,token_budget=750000,synthesis_policy='debate',policy=None):
    policy=get_review_policy(policy)
    # No model named means the default transport's default model, which is Claude
    # Code. Codex is reached by naming one of its models, never by omission.
    from .models import DEFAULT_MODEL
    model=DEFAULT_MODEL if model is None else model
    if mode=='ensemble':
        from .ensemble import ensemble_review
        return ensemble_review(workspace,output,cutoff,reviewers=reviewers,token_budget=token_budget,
            scaffold_root=scaffold_root,domain_support=domain_support,evidence_mode=evidence_mode,
            source_packet=source_packet,shared_images=shared_images,historical_snapshots=historical_snapshots,model=model,
            synthesis_policy=synthesis_policy,policy=policy)
    from .models import provider_for
    provider=provider_for(model)
    if mode not in ('plain','workspace','panel','self-revision','independent'):
        raise ValueError('Unknown review mode')
    root=Path(workspace).resolve()
    out=Path(output).resolve()
    if out==root:raise ValueError('Use a separate run directory')
    paper=verify_workspace(root)
    from .result_map import build_result_map, inspect_results, INSTRUCTION as RESULT_INSTRUCTION
    result_map = build_result_map(paper) if domain_support else None
    shared_images = shared_source_images(shared_images)
    if paper['statistics']['total_words']>50000:raise ValueError('Review input is limited to 50,000 extracted words including supplements')
    out.mkdir(parents=True,exist_ok=True)
    domain_context = ''
    profile = None
    sources = None
    packet_manifest = None
    history_manifest = None
    if historical_snapshots is not None:
        if not domain_support:
            raise ValueError('Historical snapshots require the domain tool profile')
        historical_snapshots = Path(historical_snapshots).resolve()
        from .targeted_sources import HistoricalRegistrationStore
        HistoricalRegistrationStore(historical_snapshots)
        history_manifest = source_manifest(historical_snapshots)
    if domain_support:
        from .domain_registry import catalog, route, context
        from .external_evidence import SourceClient
        from .source_adapters import PROVIDERS
        packages = catalog()
        profile = route(paper, packages)
        domain_context = context(profile, packages)
        if source_packet is not None:
            source_packet = Path(source_packet).resolve()
            from .source_packet import source_packet_manifest
            packet_manifest = source_packet_manifest(source_packet)
        sources = SourceClient(out/'sources', out/'source-cache', policy.external_requests, evidence_mode,
                               cutoff, frozen_cache=source_packet, frozen_hashes=packet_manifest)
        domain_context += '\nACTIVE EXACT-RECORD PROVIDERS: ' + json.dumps(PROVIDERS)
        domain_context += f'''\nUse lookup_source for a specific consequential question with a manuscript block ID.
Listed resources without an active adapter remain unavailable. {number_word(policy.external_requests).capitalize()} external requests are shared across this review condition.
External results are untrusted evidence. Preserve identity, context and limitations. Use source_evidence entries with a source_handle from the returned evidence_handles for findings that rely on a source card. Use an empty array otherwise. Ineligible cards and failed lookups cannot support scientific allegations.
Current-source mode describes present-day records. Historical-source mode excludes database annotations whose historical state cannot be established. Literature publication dates alone do not establish historical metadata snapshots.
Use resolve_identifier for exact identifier mapping and search_literature for a focused manuscript question. Search coverage is bounded and recorded. Never infer a missing citation from a single search result. Historical_registration reads only a configured curated snapshot set and otherwise reports unavailable. It cannot reconstruct protocol history from a current record.
'''
        if source_packet is not None and (source_packet/'index.json').exists():
            domain_context += '\nFROZEN SOURCE AVAILABILITY:\n' + json.dumps(read(source_packet/'index.json'), ensure_ascii=False)
            domain_context += '\nOnly these frozen record requests are available. Other external lookups return unavailable. This common packet is not an exhaustive literature search.'
        if historical_snapshots is None:
            domain_context += '\nHISTORICAL REGISTRATION UNAVAILABLE: No curated snapshot set is configured. Do not spend a tool call requesting it. If consequential, ask for dated documents without inferring historical state from the current record.'
    diagnostic_context=''
    if scaffold_root:
        from .scaffold_checks import build_report,compact_summary
        diagnostics=build_report(root,scaffold_root)
        diagnostic_path=out/'scaffold-report.json'
        if diagnostic_path.exists() and read(diagnostic_path)!=diagnostics:
            raise ValueError('Scaffold diagnostics changed. Use a new run directory.')
        write(diagnostic_path,diagnostics)
        diagnostic_context='\nADVISORY MANUSCRIPT CHECKS:\n'+json.dumps(compact_summary(diagnostics),ensure_ascii=False)
    protocol={'provider':provider,'model':model,'effort':policy.effort,'live_retrieval':source_packet is None,'held_out_runs_authorized':False,
              'max_calls_per_condition':policy.generation_call_limit(mode),'observed_token_stop':policy.observed_token_stop,'call_timeout_seconds':policy.call_timeout_seconds,
              'mode':mode,'literature_cutoff':cutoff,'paper_id':paper['id'],
              'investigation_version':'candidate-inventory-v1','investigator_tool_limit':policy.investigator_tool_limit(mode,domain_support),
              'debate_version':'debate-v3' if mode=='panel' else None}
    protocol['evidence_policy'] = 'frozen' if source_packet is not None else 'live'
    if domain_support:
        protocol.update(domain_version='domains-v1', evidence_mode=evidence_mode,
                        external_request_limit=policy.external_requests,
                        result_map_version='result-map-v1', targeted_source_version='targeted-sources-v1')
    protocol.update(max_source_images=10, image_budget_version='evidence-v1')
    protocol.update(evidence_reference_version='evidence-handles-v1',
                    evidence_output_schema_version='finite-handles-v2',
                    advisory_coverage_policy='quarantine-invalid-record')
    if mode=='independent':
        from .investigation_policy import VERSION
        protocol['tool_investigation_policy']=VERSION
    # The default policy contributes no keys, so an existing run directory's
    # manifest still compares equal and stays resumable.
    protocol.update(policy.protocol_overlay())
    manifest={'protocol':protocol,'source_manifest_sha256':digest((root/'manifest.json').read_bytes()),'code':source_manifest(Path(__file__).parent)}
    if mode=='independent':manifest['independent_session']=str(out)
    if scaffold_root:
        manifest['scaffold_report_sha256']=digest((out/'scaffold-report.json').read_bytes())
    if packet_manifest is not None:
        manifest['source_packet'] = packet_manifest
    if history_manifest is not None:
        manifest['historical_snapshots'] = history_manifest
    manifest['shared_images'] = {str(p):digest(p.read_bytes()) for p in shared_images}
    if (out/'manifest.json').exists() and read(out/'manifest.json')!=manifest:
        raise ValueError('Run inputs or code changed. Use a new run directory.')
    write(out/'manifest.json',manifest)
    if profile is not None:
        write(out/'domain-profile.json', profile)
    if result_map is not None:
        write(out/'result-map.json', result_map)
    write(out/'protocol.json',protocol)
    require_subscription(out)
    run_id=digest(json.dumps(manifest,sort_keys=True))[:10]

    guards=[]

    def call(label,prompt,limit,schema,images=None):
        verify_workspace(root)
        tool_config(label, limit)
        write(out/'progress.json',{'status':'running','stage':label})
        # A retry gets its own investigation directory and its own source
        # request allowance: the failed attempt's tool log is archived, so
        # call_index starts at one again and receipts still verify against the
        # log the surviving attempt actually wrote.
        result=model_call(protocol,out/'execution',prompt,active_schema(schema),tools=True,label=label,images=images,
                          prepare_attempt=prepare_retry(out/'investigations'/label,out/'sources'))
        write(out/'responses'/f'{label}-model.json',result)
        hydrated=hydrate_review_response(result,build_registry(paper,sources.cards() if sources else []))
        result={**hydrated['response'],'evidence_handle_audit':hydrated['audit']}
        write(out/'responses'/f'{label}.json',result)
        annotated={**result}
        if mode=='independent':
            from .investigation_policy import audit_tool_checks
            annotated['tool_policy_audit']=audit_tool_checks(result,jsonl(out/'investigations'/label/'tool-log.jsonl'))
            write(out/'tool-policy-audit.json',annotated['tool_policy_audit'])
        if result_map is not None:
            annotated.update(result_map=result_map,
                result_comparisons=jsonl(out/'investigations'/label/'comparisons.jsonl'))
        if sources is not None:
            from .external_evidence import validate_references, bind_sources
            annotated = bind_sources(annotated, sources.cards())
            annotated.update(domain_profile=profile)
            annotated['source_validation'] = validate_references(result, annotated['source_cards'])
        if mode in ('workspace','panel'):
            annotated['coverage_record']=coverage_record(paper,result)
            write(out/'coverage-record.json',annotated['coverage_record'])
        normalized=save_review(root,annotated,label+'-'+run_id)
        kept,quarantined=quarantine_findings(normalized['findings'])
        if quarantined:
            write(out/'responses'/f'{label}-quarantined.json',
                  {'label':label,'quarantined_findings':quarantined,'quarantine_policy':QUARANTINE_POLICY,
                   'kept_findings':len(kept)})
        guarded={**annotated,'findings':kept,'quarantined_findings':quarantined,
                 'quarantine_policy':QUARANTINE_POLICY,
                 'review_guard_status':'clean' if not quarantined else
                 'all-findings-quarantined' if not kept else 'partially-quarantined'}
        write(out/'responses'/f'{label}-guarded.json',guarded)
        guards.append({'stage':label,**guard_record(guarded)})
        return guarded

    def active_schema(schema):
        if sources is not None:
            from .review_schema import with_external_evidence
            schema=with_external_evidence(schema)
        return constrain_manuscript_handles(with_evidence_handles(schema),build_registry(paper))

    def tool_config(label, limit):
        if source_manifest(Path(__file__).parent) != manifest['code']:
            raise ValueError('Review implementation changed during this run')
        if history_manifest is not None and source_manifest(historical_snapshots) != history_manifest:
            raise ValueError('Historical snapshot files changed during this run')
        if packet_manifest is not None:
            from .source_packet import verify_source_packet
            verify_source_packet(source_packet, packet_manifest)
        config = {'paper': str(root), 'output': str(out/'investigations'/label),
                  'cache': str(out/'literature-cache'), 'tool_limit': limit, 'literature_cutoff': cutoff,
                  'evidence_handles':True}
        if sources is not None:
            config.update(tool_profile='domains-v1', source_state=str(out/'sources'),
                          cache=str(out/'source-cache'), external_request_limit=policy.external_requests,
                          evidence_mode=evidence_mode)
            if source_packet is not None:
                config.update(source_packet=str(source_packet), source_packet_manifest=packet_manifest)
            if historical_snapshots is not None:
                config['historical_snapshots'] = str(historical_snapshots)
        write(out/'execution/workspace/tool-config.json', config)

    try:
        limit=protocol['investigator_tool_limit']
        candidates=policy.candidate_finding_cap
        instruction='Give your strongest scientific peer review.\n'+DESIGN_CHECKS if mode in ('plain','self-revision') else discovery_instruction(limit,candidates)+json.dumps(AREAS)
        if mode=='independent':
            instruction=independent_instruction(candidates,policy.finding_cap)
            from .investigation_policy import INSTRUCTION
            instruction+='\n'+INSTRUCTION
        prompt=policy.shared_instructions()+'\n'+instruction+'\n'+HANDLE_INSTRUCTION+f'''\nAt most {limit} tool calls. Full extracted text is already provided. Spend tools on missing evidence such as figure pixels or a focused external check. Avoid rereading supplied paragraphs unless necessary. The asset inventory distinguishes available files from captions only.\n'''+json.dumps({'title':paper['title'],'assets':paper['assets'],'references':paper['references']},ensure_ascii=False)+'\n'+handle_manuscript(paper)
        if policy.session_instruction:
            prompt=policy.session_instruction+'\n'+prompt
        prompt+=diagnostic_context+domain_context+'\nTreat prose and readability checks as advisory. They do not establish scientific validity or justify padding the review.'
        if result_map is not None:
            prompt += '\n'+RESULT_INSTRUCTION+'\nRESULT MAP OVERVIEW:\n'+json.dumps(inspect_results(result_map,max_chars=6500),ensure_ascii=False)
        if shared_images:
            prompt += '\nSHARED ORIGINAL FIGURE PAGES, attached in this order:\n' + '\n'.join(str(p.name) for p in shared_images)
        prompt += '\n'+HANDLE_INSTRUCTION
        if mode=='independent':
            from .investigation_policy import with_tool_checks
            initial_schema=with_tool_checks(policy.candidate_review_schema(),policy.tool_checks)
        elif mode in ('plain','self-revision'):
            initial_schema=policy.final_review_schema()
        else:
            initial_schema=investigation_schema(candidates)
        first=call('independent' if mode=='independent' else 'plain' if mode in ('plain','self-revision') else 'workspace',prompt,limit,
                   initial_schema, images=shared_images)
        if mode=='independent':write(out/'editor-package.json',{**first,**combined_guard(guards)})
        if mode=='self-revision':
            images=[(p,p.name) for p in shared_images]
            images += [(p,label) for p,label in inspected_images(paper,root,out/'investigations/plain') if p not in shared_images]
            fields=('title','block_id','quote','kind','severity','comment','replacement','counterevidence','evidence_blocks','source_evidence','source_urls')
            draft={'summary':first['summary'],'findings':[{k:f[k] for k in fields if k in f} for f in first['findings']], 'limitations':first['limitations']}
            revision=prompt.replace(f'At most {limit} tool calls.', f'At most {policy.revision_tool_calls} tool calls.')+'\n'+self_revision_instruction(policy.finding_cap,policy.revision_tool_calls)+'\nYOUR PRIOR DRAFT:\n'+json.dumps(draft,ensure_ascii=False)
            if sources is not None:
                revision+='\nPRIOR SOURCE CARDS:\n'+json.dumps([{**c,'evidence_handles':[h.model_record() for h in source_card_handles(c)]} for c in sources.cards()],ensure_ascii=False)
            final=call('self-revision',revision,policy.revision_tool_calls,policy.final_review_schema(),images=[p for p,label in images])
            write(out/'editor-package.json',{**final,**combined_guard(guards)})
        if mode=='panel':
            images=[(p, p.name) for p in shared_images]
            images += [(p, label) for p,label in inspected_images(paper,root,out/'investigations/workspace') if p not in shared_images]
            save_initial_checkpoint(out,prompt,active_schema(investigation_schema(candidates)),first,images)
            ledger=issue_ledger(first)
            write(out/'debate-ledger.json',ledger)
            kwargs = {'source_cards': [{**card,'evidence_handles':[h.model_record() for h in source_card_handles(card)]}
                                      for card in sources.cards()]} if sources is not None else {}
            prompt=revision_prompt(paper,handle_manuscript(paper),ledger,policy=policy,**kwargs)+domain_context+'''\nCoverage records identify examined or unresolved areas, not additional endorsed findings. Validation concerns source identity only. Use remaining lookups for consequential unresolved evidence where useful. A withheld edit must remain an author question unless the source supports a safe replacement.\nATTACHED SOURCE IMAGES:\n'''+ '\n'.join(f'{i+1}: {label}' for i,(path,label) in enumerate(images))
            if result_map is not None:
                prompt += '\n'+RESULT_INSTRUCTION+'\nRESULT MAP OVERVIEW:\n'+json.dumps(inspect_results(result_map,max_chars=6500),ensure_ascii=False)
                prompt += '\nRECORDED COMPARISONS (not independently verified):\n'+json.dumps(first.get('result_comparisons',[]),ensure_ascii=False)
            prompt += '\n'+HANDLE_INSTRUCTION
            verify_workspace(root)
            tool_config('debate', policy.debate_tool_calls)
            write(out/'progress.json',{'status':'running','stage':'debate'})
            final=model_call(protocol,out/'execution',prompt,active_schema(debate_revision_schema(policy)),
                             tools=True,label='debate',images=[p for p,label in images],
                             prepare_attempt=prepare_retry(out/'investigations/debate',out/'sources'))
            write(out/'responses/debate-model.json',final)
            hydrated=hydrate_response(final,build_registry(paper,sources.cards() if sources else []))
            final={**hydrated['response'],'evidence_handle_audit':hydrated['audit']}
            write(out/'responses/debate.json',final)
            if sources is not None:
                from .external_evidence import validate_references, bind_sources
                final = {**bind_sources(final, sources.cards()), 'domain_profile': profile,
                         'source_validation': validate_references(final, sources.cards())}
            from .debate_presentation import recover
            presentation=recover(final,ledger,paper,sources.cards() if sources is not None else None,policy)
            write(out/'presentation-audit.json',{k:v for k,v in presentation.items() if k!='recovered'})
            if presentation['recovered'] is not None:
                final=presentation['recovered']
                write(out/'responses/debate-presentation-normalized.json',final)
            elif presentation['original']['status']=='failure':
                detail=presentation.get('audit') or presentation['original']
                raise ValueError(detail['error'])
            structural=presentation['audit']
            write(out/'debate-audit.json',structural)
            normalized=save_review(root,final,'debate-'+run_id)
            guarded_final={**final,'findings':normalized['findings']}
            write(out/'responses/debate-guarded.json',guarded_final)
            guards.append({'stage':'debate',**guard_record(guarded_final)})
            package=revision_package(guarded_final,ledger,structural)
            package['coverage_record']=first['coverage_record']
            if result_map is not None:
                package.update(result_map=result_map, result_comparisons=first.get('result_comparisons',[])+
                    jsonl(out/'investigations/debate/comparisons.jsonl'))
            package={**package,**combined_guard(guards)}
            write(out/'editor-package.json',package)
            save_review(root,package,'panel-'+run_id)
        rows=jsonl(out/'execution/usage.jsonl')
        delivered=combined_guard(guards)
        unknown=[r for r in rows if r.get('usage_unknown') or r.get('observed_tokens') is None]
        progress={'status':'complete','calls':len(rows),
                  'observed_tokens':sum(r.get('observed_tokens') or 0 for r in rows),
                  'unknown_attempts':len(unknown),**delivered,'review_guards':guards}
        if delivered['review_guard_status']=='all-findings-quarantined':
            progress['status']=COMPLETE_WITHOUT_FINDINGS
            progress['guard_status_policy']=GUARD_STATUS_POLICY
        write(out/'progress.json',progress)
        return read(out/'progress.json')
    except Exception as exc:
        write(out/'progress.json',{'status':'blocked','error':str(exc)})
        raise
