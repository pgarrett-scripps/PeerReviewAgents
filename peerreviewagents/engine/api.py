"""Versioned facade over the existing engine, with no duplicated pipeline."""
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from .config import (default_literature_cutoff, get_review_preset, preset_synthesis_policy,
                     resolved_review_config, review_presets)
from .models import DEFAULT_MODEL, DEFAULT_PROVIDER
from .review_policy import (APPROVED_EFFORTS, DEFAULT_POLICY, DEFAULT_POLICY_NAME,
                            MAX_ATTEMPT_CEILING, STUDY_POLICY_NAME, get_review_policy,
                            review_policies, single_session_refusal)

# v3: a request that names no policy runs the agentic allowance rather than the
# bounded study budget, so a v2 artifact was produced under different defaults.
API_VERSION = 'paper-workbench-core-v3'


def capabilities():
    """Advertise supported execution, without starting providers or the UI."""
    from .models import DEFAULT_MODELS, PROVIDER_MODELS, SUBSCRIPTION_MODELS
    policies = review_policies()
    efforts_by_policy = {name: record['effort'] for name, record in policies.items()}
    retries_by_policy = {name: record['max_attempts'] > 1 for name, record in policies.items()}
    return {'api_version': API_VERSION, 'artifact_version': 'paper-workbench-run-v1',
            'workflows': ['manuscript-only', 'independent', 'ensemble'],
            'provider': DEFAULT_PROVIDER, 'default_provider': DEFAULT_PROVIDER,
            'default_model': DEFAULT_MODEL,
            'providers': list(PROVIDER_MODELS),
            'models_by_provider': {name: list(models) for name, models in PROVIDER_MODELS.items()},
            'default_models': dict(DEFAULT_MODELS),
            'configuration_version': 'paper-workbench-config-v1',
            'presets': list(review_presets()),
            'policies': list(review_policies()),
            'default_policy': DEFAULT_POLICY_NAME,
            'study_policy': STUDY_POLICY_NAME,
            'review_policies': policies,
            'evidence_policies': ['none', 'live'],
            'models': list(SUBSCRIPTION_MODELS), 'reasoning_effort': DEFAULT_POLICY.effort,
            'reasoning_efforts': list(APPROVED_EFFORTS),
            'reasoning_effort_by_policy': efforts_by_policy,
            'usage_policy': 'operator_owned_subscription_usage_recorded_not_gated',
            'tool_access': 'workflow-specific',
            'tool_access_by_workflow': {'manuscript-only': 'none',
                                        'independent': 'bounded_manuscript_tools',
                                        'ensemble': 'bounded_manuscript_tools'},
            'automatic_retries': retries_by_policy[DEFAULT_POLICY_NAME],
            'automatic_retries_by_policy': retries_by_policy,
            'retry_policy': {'default_attempts': DEFAULT_POLICY.max_attempts,
                             'max_attempts_ceiling': MAX_ATTEMPT_CEILING,
                             'attempts_by_policy': {name: record['max_attempts']
                                                    for name, record in policies.items()},
                             'retried': 'a launched call that timed out, crashed or lost its transport',
                             'never_retried': 'any refusal or guard stop, including safety, '
                                              'the call ceiling and the token stop',
                             'per_attempt_receipts': True},
            'invalid_finding_policy': 'quarantined-with-reason-not-run-ending',
            'review_guard_statuses': ['clean', 'partially-quarantined', 'all-findings-quarantined'],
            'zero_surviving_findings_status': 'complete-without-findings',
            'saved_draft_synthesis': {'policy': 'ordinary', 'generation_calls': 1,
                                      'discovery_calls': 0,
                                      'tool_allowance': 'the original run review policy allowance'},
            'limitations': ['Manuscript-only mode has no review tools or external access.',
                           'Independent mode includes tools and is not a tool-free baseline.',
                           'Claude Code and Codex subscription sessions are supported. API keys, other providers and arbitrary models are not.',
                           'The provider key names the default Claude Code transport. Codex runs only when it is selected by name.',
                           'Codex and Claude Code are separate conditions. Results from one do not reproduce the other.',
                           'Evidence validation establishes provenance, not scientific correctness.']}


def load_manuscript(workspace):
    from .annotations import verify_workspace
    return verify_workspace(Path(workspace))


def evidence_registry(workspace, source_cards=()):
    from .evidence_handles import build_registry
    return build_registry(load_manuscript(workspace), list(source_cards))


def validate_review(workspace, response, review_id='external-review'):
    """Normalize a source-bound review without saving it or executing a model."""
    from .annotations import normalize_review
    return normalize_review(load_manuscript(workspace), response, review_id)


@dataclass(frozen=True)
class ReviewRequest:
    workspace: Path
    output: Path
    # A run that names no cutoff reviews against everything published so far.
    literature_cutoff: str = field(default_factory=default_literature_cutoff)
    workflow: Literal['manuscript-only', 'independent', 'ensemble'] = 'ensemble'
    model: str = DEFAULT_MODEL
    reviewers: int = 2
    # None takes the ceiling this request's policy needs for its stages, which
    # is what every surface means by naming no budget.
    token_budget: int | None = None
    source_images: tuple[Path, ...] = field(default_factory=tuple)
    preset: str | None = None
    synthesis_policy: Literal['debate', 'ordinary'] = 'debate'
    # None is the engine default policy, the same one the command line takes.
    policy: str | None = None

    def __post_init__(self):
        from datetime import date
        if self.workflow not in capabilities()['workflows']:
            raise ValueError('Unsupported Paper Workbench workflow')
        if self.model not in capabilities()['models']:
            raise ValueError('Unsupported Paper Workbench model. No fallback is permitted.')
        if type(self.reviewers) is not int or not 1 <= self.reviewers <= 16:
            raise ValueError('Reviewer count must be between one and sixteen')
        if self.token_budget is None:
            object.__setattr__(self, 'token_budget', self.resolved_token_budget())
        if type(self.token_budget) is not int or self.token_budget <= 0:
            raise ValueError('Token launch budget must be a positive integer')
        if self.workflow == 'manuscript-only' and self.reviewers != 1:
            raise ValueError('Manuscript-only review requires exactly one reviewer')
        if self.preset is not None:
            get_review_preset(self.preset)
        if self.policy is not None:
            # The CLI refuses this combination. The request object is the other
            # way in, so it refuses it too rather than spending three long
            # high-effort calls on a request that asked for one session.
            if get_review_policy(self.policy).single_session and self.workflow == 'ensemble':
                raise ValueError(single_session_refusal(
                    self.policy, option='policy', preset='preset', mode="workflow='independent'"))
        if self.synthesis_policy not in ('debate', 'ordinary'):
            raise ValueError('Synthesis policy must be debate or ordinary')
        if self.workflow != 'ensemble' and self.synthesis_policy != 'debate':
            raise ValueError('Ordinary synthesis is available only for ensemble review')
        date.fromisoformat(self.literature_cutoff)
        if Path(self.workspace).resolve() == Path(self.output).resolve():
            raise ValueError('Use a separate run output directory')

    def policy_name(self):
        """The policy this request runs under, named explicitly or by its preset."""
        if self.policy is not None:
            return self.policy
        if self.preset is not None:
            return get_review_preset(self.preset).policy
        return DEFAULT_POLICY_NAME

    def resolved_token_budget(self):
        """The observed-token ceiling this request starts under.

        One ceiling covers every generation call a run makes, so an ensemble
        asks the policy for its reviewers plus the synthesis and a single-call
        workflow asks for one stage. The study budget is the same fixed figure
        either way, so a study arm's ceiling is unchanged.
        """
        policy = get_review_policy(self.policy_name())
        stages = self.reviewers + 1 if self.workflow == 'ensemble' else 1
        return policy.launch_budget(stages)


@dataclass(frozen=True)
class SavedDraftSynthesisRequest:
    """One ordinary synthesis pass over the immutable drafts from an ensemble run.

    The drafts are immutable; the evidence is not frozen with them. This pass
    retrieves live sources under the original run's cutoff, exactly as a fresh
    review does, so what it reuses is the discovery drafts and nothing else.
    """
    workspace: Path
    original_output: Path
    output: Path
    source_images: tuple[Path, ...]
    # The reused drafts were written under one cutoff. None inherits that cutoff
    # instead of synthesising saved drafts against a later date.
    literature_cutoff: str | None = None
    model: str = DEFAULT_MODEL
    # The matched pass must launch under the same ceiling the original synthesis
    # did, and that figure depends on the policy the original run chose. None
    # takes it from the original debate stage rather than from a fixed number.
    token_budget: int | None = None
    synthesis_policy: Literal['ordinary'] = 'ordinary'

    def __post_init__(self):
        from datetime import date
        from .storage import read
        original_protocol = Path(self.original_output) / 'reviewer-1' / 'protocol.json'
        if not original_protocol.is_file():
            raise ValueError('Saved draft synthesis needs the original run reviewer-1 protocol')
        if self.token_budget is None:
            debate_protocol = Path(self.original_output) / 'debate' / 'protocol.json'
            if not debate_protocol.is_file():
                raise ValueError('Saved draft synthesis needs the original run debate protocol')
            object.__setattr__(self, 'token_budget',
                               read(debate_protocol).get('token_launch_budget'))
        original_cutoff = read(original_protocol).get('literature_cutoff')
        date.fromisoformat(str(original_cutoff))
        if self.literature_cutoff is None:
            object.__setattr__(self, 'literature_cutoff', original_cutoff)
        elif self.literature_cutoff != original_cutoff:
            raise ValueError('Saved draft synthesis must use the original run literature cutoff, '
                             + str(original_cutoff))
        if self.model not in capabilities()['models']:
            raise ValueError('Unsupported Paper Workbench model. No fallback is permitted.')
        if self.synthesis_policy != 'ordinary':
            raise ValueError('Saved draft synthesis supports only the ordinary policy')
        if type(self.token_budget) is not int or self.token_budget <= 0:
            raise ValueError('Token launch budget must be a positive integer')
        if len(self.source_images) > 10 or any(not Path(path).is_file() for path in self.source_images):
            raise ValueError('Supply the complete set of at most ten existing source images')
        date.fromisoformat(self.literature_cutoff)


def _hash_files(paths):
    from .storage import digest
    return {str(Path(path).resolve()): digest(Path(path).read_bytes()) for path in paths}


def run_saved_draft_synthesis(request: SavedDraftSynthesisRequest):
    """Reuse a debate run's exact discovery drafts for one ordinary synthesis call."""
    from .ensemble import debate_reviews, observed_usage
    from .model_runtime import source_manifest
    from .storage import read, write, digest
    if not isinstance(request, SavedDraftSynthesisRequest):
        raise TypeError('Expected SavedDraftSynthesisRequest')
    workspace = Path(request.workspace).resolve()
    original = Path(request.original_output).resolve()
    output = Path(request.output).resolve()
    if output.exists():
        raise FileExistsError('Use a new run directory. The public API never retries an existing run.')
    paper = load_manuscript(workspace)
    source_manifest_sha = digest((workspace / 'manifest.json').read_bytes())
    artifact = read(original / 'core-artifact.json')
    current_engine = source_manifest(Path(__file__).parent)
    if (artifact.get('api_version') != API_VERSION or
            artifact.get('artifact_version') != capabilities()['artifact_version'] or
            artifact.get('engine_source') != current_engine):
        raise ValueError('Original ensemble engine changed')
    if (artifact.get('paper_id') != paper['id'] or artifact.get('source_manifest_sha256') != source_manifest_sha
            or artifact.get('workflow') != 'ensemble' or artifact.get('model_requested') != request.model):
        raise ValueError('Saved drafts do not match the requested manuscript, workflow, or model')
    if artifact.get('resolved_configuration', {}).get('synthesis_policy') != 'debate':
        raise ValueError('Saved drafts must originate from the debate condition')
    debate_root = original / 'debate'
    records_path = debate_root / 'input-reviews.json'
    stage_manifest = read(debate_root / 'manifest.json')
    if digest(records_path.read_bytes()) != stage_manifest.get('review_input_sha256'):
        raise ValueError('Saved input review hashes changed')
    records = read(records_path)
    expected_review_hashes = [row.get('provenance', {}).get('source_review_sha256') for row in records]
    if any(not value for value in expected_review_hashes):
        raise ValueError('Saved drafts lack original review hashes')
    # The reused row and the original reviewer file must still be the same
    # review: the hash alone would accept a rewritten row beside an intact file.
    for row, expected in zip(records, expected_review_hashes):
        source_path = Path(row['provenance'].get('source_review_path', ''))
        if (not source_path.is_file() or digest(source_path.read_bytes()) != expected
                or row['review'] != read(source_path)):
            raise ValueError('An original input review changed')
    expected_images = stage_manifest.get('images')
    supplied_images = _hash_files(request.source_images)
    if not isinstance(expected_images, dict) or supplied_images != expected_images:
        raise ValueError('Saved draft synthesis requires the complete original image set')
    protocol = read(debate_root / 'protocol.json')
    # The drafts were debated under one review policy, and the matched pass runs
    # under that same allowance: an agentic run's synthesis is not a bounded
    # study synthesis. The study policy records no overlay, so a run made before
    # policies were data reads as the study budget it actually ran under.
    original_policy = protocol.get('review_policy', STUDY_POLICY_NAME)
    policy = get_review_policy(original_policy)
    if (protocol.get('debate_tool_allowance') != policy.debate_tool_calls
            or protocol.get('token_launch_budget') != request.token_budget):
        raise ValueError('Saved draft synthesis must match the original synthesis tool and token budget')
    expected_discovery_calls = artifact.get('resolved_configuration', {}).get('reviewers')
    if type(expected_discovery_calls) is not int or len(records) != expected_discovery_calls:
        raise ValueError('Saved drafts do not match the original expected discovery count')
    cache_roots = [original / f'reviewer-{index}' / 'source-cache'
                   for index in range(1, expected_discovery_calls + 1)]
    discovery_usage = []
    discovery_receipts = {}
    for index in range(1, expected_discovery_calls + 1):
        reviewer_root = original / f'reviewer-{index}'
        usage = observed_usage(reviewer_root)
        if usage.get('unknown') or usage.get('calls') != 1 or usage.get('receipts') != 1:
            raise ValueError('Every reused discovery draft requires one resolved original model receipt')
        discovery_usage.append(usage)
        for path in reviewer_root.rglob('*'):
            if path.is_file() and (path.name == 'usage.jsonl' or path.name.endswith('.started.json')):
                discovery_receipts[str(path.relative_to(original))] = digest(path.read_bytes())
    reused_discovery_usage = {'calls': expected_discovery_calls, 'receipts': expected_discovery_calls,
        'observed_tokens': sum(row['observed_tokens'] for row in discovery_usage), 'unknown': False}
    cache_before = {str(path.resolve()): digest(path.read_bytes()) for root in cache_roots for path in root.glob('*') if path.is_file()}
    raw_hashes = sorted({digest(path.read_bytes()) for root in cache_roots
                         for path in root.glob('*.raw') if path.is_file()})
    package = debate_reviews(workspace, output, request.literature_cutoff,
        records, images=request.source_images, model=request.model, token_budget=request.token_budget,
        cache_roots=cache_roots, synthesis_policy='ordinary', reused_drafts=True,
        policy=original_policy)
    cache_after = {str(path.resolve()): digest(path.read_bytes()) for root in cache_roots for path in root.glob('*') if path.is_file()}
    if cache_after != cache_before:
        raise ValueError('Original reviewer evidence cache changed during synthesis')
    package['synthesis_provenance'].update(reused_drafts=True, discovery_calls=0,
        original_core_artifact_sha256=digest((original / 'core-artifact.json').read_bytes()),
        original_input_reviews_sha256=digest(records_path.read_bytes()),
        original_review_sha256=expected_review_hashes,
        original_evidence_raw_sha256=raw_hashes, shared_images=supplied_images,
        reused_discovery_receipts=discovery_receipts)
    write(output / 'editor-package.json', package)
    synthesis_usage = observed_usage(output)
    result = {'api_version': API_VERSION, 'artifact_version': 'paper-workbench-run-v1',
        'paper_id': paper['id'], 'source_manifest_sha256': source_manifest_sha,
        'workflow': 'ensemble', 'execution_scope': 'saved-draft-synthesis', 'model_requested': request.model,
        'resolved_configuration': {'synthesis_policy': 'ordinary', 'reused_drafts': True,
            'discovery_calls': 0, 'synthesis_calls': 1,
            'synthesis_tool_allowance': policy.debate_tool_calls, 'review_policy': policy.name,
            'token_launch_budget': request.token_budget, 'evidence_policy': 'live'},
        'engine_source': current_engine, 'editor_package': package,
        'usage': synthesis_usage, 'usage_scope': 'new_synthesis_stage_only',
        'reused_discovery_usage': reused_discovery_usage,
        'reused_discovery_receipts': discovery_receipts,
        'original_full_workflow_usage': artifact.get('usage'), 'output_directory': str(output),
        'limitation': 'Reused discovery usage is reported separately and is not counted as new synthesis usage.'}
    write(output / 'core-artifact.json', result)
    return result


def manuscript_only_progress(package):
    """The same status vocabulary the reviewer reports, for the one-call baseline.

    ``run_manuscript_only`` returns the editor package rather than a progress
    record. The guard status is in it, so the zero-finding outcome is named
    here the way every other workflow names it.
    """
    from .reviewer import COMPLETE_WITHOUT_FINDINGS, GUARD_STATUS_POLICY
    package = package or {}
    guard = package.get('review_guard_status', 'clean')
    progress = {'status': 'complete', 'review_guard_status': guard,
                'findings_delivered': len(package.get('findings') or []),
                'quarantined_findings_count': len(package.get('quarantined_findings') or [])}
    if guard == 'all-findings-quarantined':
        progress.update(status=COMPLETE_WITHOUT_FINDINGS, guard_status_policy=GUARD_STATUS_POLICY)
    return progress


def run_review(request: ReviewRequest):
    """Explicit inference entry point. Importing or constructing requests is offline."""
    from .reviewer import review
    from .ensemble import observed_usage
    from .model_runtime import source_manifest
    from .storage import read, write, digest
    if not isinstance(request, ReviewRequest):
        raise TypeError('Expected ReviewRequest')
    workspace, output = Path(request.workspace).resolve(), Path(request.output).resolve()
    if output.exists():
        raise FileExistsError('Use a new run directory. The public API never retries an existing run.')
    paper = load_manuscript(workspace)
    source_hash = digest((workspace/'manifest.json').read_bytes())
    resolved_config = resolved_review_config(request)
    # A run that completes with every finding quarantined is not the same
    # outcome as a clean one. The engine says which it was; the artifact has to
    # carry it, because an API caller never reads progress.json.
    if request.workflow == 'manuscript-only':
        from .baseline import run_manuscript_only
        package = run_manuscript_only(request, paper, resolved_config)
        progress = manuscript_only_progress(package)
    else:
        progress = review(workspace, output, request.literature_cutoff,
                          mode=request.workflow, model=request.model, reviewers=request.reviewers,
                          token_budget=request.token_budget,
                          shared_images=request.source_images,
                          synthesis_policy=request.synthesis_policy,
                          policy=resolved_config.get('policy'))
    if not progress:
        # The engine writes the same record to the run directory. Read it back
        # rather than report an unknown status.
        progress = read(output/'progress.json') if (output/'progress.json').exists() else {}
    engine = source_manifest(Path(__file__).parent)
    artifact = {'api_version': API_VERSION, 'artifact_version': 'paper-workbench-run-v1',
                'paper_id': paper['id'], 'source_manifest_sha256': source_hash,
                'workflow': request.workflow, 'model_requested': request.model,
                'resolved_configuration': resolved_config,
                'resolved_configuration_sha256': digest(json.dumps(resolved_config, sort_keys=True)),
                'engine_source': engine,
                'status': progress.get('status'), 'run_progress': progress,
                'editor_package': read(output/'editor-package.json'),
                'usage': observed_usage(output), 'output_directory': str(output),
                'limitation': 'A run artifact records what the engine did. It is not a graded score.'}
    write(output/'core-artifact.json', artifact)
    return artifact


def review_request_from_preset(name, *, workspace, output,
                               literature_cutoff=None, model=None, token_budget=None,
                               source_images=(), synthesis_policy=None, policy=None):
    """Construct a request from one named preset, with explicit runtime overrides."""
    preset = get_review_preset(name)
    preset_synthesis = preset_synthesis_policy(preset)
    return ReviewRequest(
        workspace=Path(workspace), output=Path(output),
        literature_cutoff=(default_literature_cutoff() if literature_cutoff is None
                           else literature_cutoff),
        workflow=preset.workflow, model=model or preset.model or DEFAULT_MODEL,
        reviewers=preset.reviewers,
        token_budget=preset.token_budget if token_budget is None else token_budget,
        source_images=tuple(source_images), preset=name,
        synthesis_policy=preset_synthesis if synthesis_policy is None else synthesis_policy,
        policy=preset.policy if policy is None else policy,
    )
