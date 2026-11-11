"""A single-pass manuscript-only review with enforced tool isolation."""
import json
from pathlib import Path


def run_manuscript_only(request, paper, resolved_config):
    from .annotations import quarantine_findings, save_review
    from .ensemble import _prepare, observed_usage
    from .evidence_handles import build_registry, constrain_manuscript_handles, hydrate_review_response, with_evidence_handles
    from .model_runtime import model_call, source_manifest
    from .models import provider_for
    from .review_policy import get_review_policy
    from .reviewer import HANDLE_INSTRUCTION, MAX_SOURCE_IMAGES, QUARANTINE_POLICY, handle_manuscript
    from .storage import digest, read, write

    policy = get_review_policy(resolved_config.get('policy') or getattr(request, 'policy', None))

    root = Path(request.workspace).resolve()
    output = Path(request.output).resolve()
    workspace_manifest = read(root / 'manifest.json')['files']
    if paper.get('statistics', {}).get('total_words', 0) > 35000:
        raise ValueError('Manuscript-only review input exceeds 35,000 extracted words')
    images = []
    seen = set()
    for candidate in request.source_images:
        path = Path(candidate).resolve()
        if not path.is_file():
            raise ValueError('Every manuscript-only source image must be an existing file')
        identity = digest(path.read_bytes())
        if identity not in seen:
            images.append(path)
            seen.add(identity)
    attached_assets = []
    unavailable_assets = []
    for asset in paper.get('assets', []):
        preview = asset.get('preview')
        path = (root / preview).resolve() if preview else None
        expected = workspace_manifest.get(preview) if preview else None
        if (path is not None and path.is_relative_to(root) and path.is_file() and expected
                and digest(path.read_bytes()) == expected):
            attached_assets.append(asset.get('id'))
            identity = digest(path.read_bytes())
            if identity not in seen:
                images.append(path)
                seen.add(identity)
        elif asset.get('available') == 'file':
            unavailable_assets.append({'id': asset.get('id'), 'label': asset.get('label'),
                                       'reason': 'no attachable preview'})
    if len(images) > MAX_SOURCE_IMAGES:
        raise ValueError(f'Manuscript-only review has more than {MAX_SOURCE_IMAGES} unique source images. Prepare a complete bounded image set explicitly.')
    protocol = {
        'provider': provider_for(request.model), 'model': request.model, 'effort': policy.effort,
        'observed_token_stop': request.token_budget, 'call_timeout_seconds': policy.call_timeout_seconds,
        'max_source_images': MAX_SOURCE_IMAGES, 'image_budget_version': 'evidence-v1',
        'mode': 'manuscript-only', 'version': 'manuscript-only-v1',
        'tool_access': 'none',
        **policy.protocol_overlay(),
    }
    _prepare(output, protocol)
    prompt = policy.shared_instructions() + '''\nReview the complete supplied manuscript once. Base every finding only on the manuscript and attached source figures. You have no review tools, external sources, other reviews, or hidden evaluation material. Do not claim that an omitted external search establishes an error. The full extracted text is supplied below.\n''' + HANDLE_INSTRUCTION
    prompt += '\nMANUSCRIPT INVENTORY:\n' + json.dumps({
        'title': paper.get('title', ''), 'assets': paper.get('assets', []),
        'references': paper.get('references', []),
    }, ensure_ascii=False)
    prompt += '\nCOMPLETE EXTRACTED MANUSCRIPT:\n' + handle_manuscript(paper)
    if images:
        prompt += '\nATTACHED SOURCE FIGURES:\n' + '\n'.join(path.name for path in images)
    if unavailable_assets:
        prompt += '\nSOURCE ASSETS WITHOUT DELIVERED PIXELS:\n' + json.dumps(unavailable_assets, ensure_ascii=False)
        prompt += '\nDo not infer visual content for these assets. State the resulting review limitation when it affects a finding.'
    prompt += '\n' + HANDLE_INSTRUCTION
    if policy.session_instruction:
        prompt = policy.session_instruction + '\n' + prompt
    registry = build_registry(paper)
    schema = constrain_manuscript_handles(with_evidence_handles(policy.final_review_schema()), registry)
    manifest = {
        'configuration': resolved_config,
        'source_manifest_sha256': digest((root / 'manifest.json').read_bytes()),
        'engine_source': source_manifest(Path(__file__).parent),
        'source_images': {str(path): digest(path.read_bytes()) for path in images},
        'attached_asset_ids': attached_assets,
        'unavailable_assets': unavailable_assets,
    }
    write(output / 'run-manifest.json', manifest)
    try:
        response = model_call(protocol, output / 'execution', prompt, schema, tools=False,
                              label='manuscript-only', images=images)
        write(output / 'responses' / 'manuscript-only-model.json', response)
        hydrated = hydrate_review_response(response, registry)
        guarded = {**hydrated['response'], 'evidence_handle_audit': hydrated['audit']}
        normalized = save_review(root, guarded, 'manuscript-only-' + digest(json.dumps(manifest, sort_keys=True))[:10])
        kept, quarantined = quarantine_findings(normalized['findings'])
        if quarantined:
            write(output / 'responses' / 'manuscript-only-quarantined.json',
                  {'label': 'manuscript-only', 'quarantined_findings': quarantined,
                   'quarantine_policy': QUARANTINE_POLICY, 'kept_findings': len(kept)})
        package = {**guarded, 'findings': kept, 'quarantined_findings': quarantined,
                   'quarantine_policy': QUARANTINE_POLICY,
                   'review_guard_status': 'clean' if not quarantined else
                   'all-findings-quarantined' if not kept else 'partially-quarantined'}
        write(output / 'editor-package.json', package)
        return package
    finally:
        write(output / 'usage-summary.json', observed_usage(output))
