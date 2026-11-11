"""Versioned review presets and resolved run configuration."""
import datetime
from dataclasses import asdict, dataclass
from types import MappingProxyType


CONFIG_VERSION = 'paper-workbench-config-v1'


def default_literature_cutoff():
    """Today's date, the cutoff a run takes when it names none.

    Current-source mode is the default, so the useful default cutoff is the day
    the review runs: everything published so far is eligible and nothing later
    can be cited. A historical run still states its own earlier cutoff.
    """
    return datetime.date.today().isoformat()


@dataclass(frozen=True)
class ReviewPreset:
    name: str
    workflow: str
    reviewers: int
    # A preset pins the protocol, not the transport. ``None`` means the run takes
    # the default model of whichever provider it runs on, so no preset can pull a
    # run onto Codex without ``--provider codex`` being asked for by name.
    model: str | None
    token_budget: int
    prompt_profile: str
    manuscript_access: str
    evidence_profile: str
    synthesis_policy: str
    revision_policy: str
    policy: str = 'standard-v1'

    def record(self):
        return {'configuration_version': CONFIG_VERSION, **asdict(self)}


_PRESETS = MappingProxyType({preset.name: preset for preset in (
    ReviewPreset(
        name='manuscript-only-single-v1', workflow='manuscript-only', reviewers=1,
        model=None, token_budget=750000,
        prompt_profile='scientific-review-v1', manuscript_access='complete-extracted-manuscript-and-permitted-images',
        evidence_profile='manuscript-only-no-tools', synthesis_policy='none',
        revision_policy='findings-only-no-automatic-revision',
    ),
    ReviewPreset(
        name='workspace-independent-v1', workflow='independent', reviewers=1,
        model=None, token_budget=750000,
        prompt_profile='independent-discovery-v1', manuscript_access='workspace-bounded',
        evidence_profile='workspace-and-bounded-evidence-tools', synthesis_policy='none',
        revision_policy='findings-only-no-automatic-revision',
    ),
    ReviewPreset(
        name='workspace-ensemble-v1', workflow='ensemble', reviewers=2,
        model=None, token_budget=750000,
        prompt_profile='independent-discovery-v1', manuscript_access='workspace-bounded',
        evidence_profile='workspace-and-bounded-evidence-tools', synthesis_policy='audited-debate-v1',
        revision_policy='findings-only-no-automatic-revision',
    ),
    ReviewPreset(
        name='workspace-ensemble-ordinary-v1', workflow='ensemble', reviewers=2,
        model=None, token_budget=750000,
        prompt_profile='independent-discovery-v1', manuscript_access='workspace-bounded',
        evidence_profile='workspace-and-bounded-evidence-tools', synthesis_policy='ordinary-synthesis-v1',
        revision_policy='findings-only-no-automatic-revision',
    ),
    ReviewPreset(
        name='workspace-agentic-v1', workflow='independent', reviewers=1,
        model=None, token_budget=1500000,
        prompt_profile='agentic-session-v1', manuscript_access='workspace-bounded',
        evidence_profile='workspace-and-bounded-evidence-tools', synthesis_policy='none',
        revision_policy='findings-only-no-automatic-revision',
        policy='workspace-agentic-v1',
    ),
)})


def review_presets():
    """Return serializable preset records without exposing mutable shared state."""
    return {name: preset.record() for name, preset in _PRESETS.items()}


def get_review_preset(name):
    try:
        return _PRESETS[name]
    except KeyError as error:
        raise ValueError('Unknown Paper Workbench review preset') from error


def resolved_review_config(request):
    """Resolve one request into a durable, versioned configuration record."""
    if request.preset is None:
        base = {
            'configuration_version': CONFIG_VERSION,
            'preset': None,
            'prompt_profile': ('scientific-review-v1' if request.workflow == 'manuscript-only'
                               else 'existing-engine-default'),
            'manuscript_access': ('complete-extracted-manuscript-and-permitted-images'
                                  if request.workflow == 'manuscript-only' else 'workspace-bounded'),
            'evidence_profile': ('manuscript-only-no-tools' if request.workflow == 'manuscript-only'
                                 else 'workspace-and-bounded-evidence-tools'),
            'synthesis_policy': 'audited-debate-v1' if request.workflow == 'ensemble' else 'none',
            'revision_policy': 'findings-only-no-automatic-revision',
            'policy': 'standard-v1',
        }
    else:
        preset = get_review_preset(request.preset)
        expected_synthesis = 'ordinary' if preset.synthesis_policy == 'ordinary-synthesis-v1' else 'debate'
        if ((request.workflow, request.reviewers) != (preset.workflow, preset.reviewers)
                or request.synthesis_policy != expected_synthesis):
            raise ValueError('Request workflow, reviewer count, and synthesis policy do not match its preset')
        base = preset.record()
        base['preset'] = base.pop('name')
    requested_policy = getattr(request, 'policy', None)
    if requested_policy is not None:
        # An explicit policy is a deliberate runtime override, like model and
        # token budget. The resolved record states the policy that actually ran.
        base['policy'] = requested_policy
    packet = dict(request.source_packet_hashes)
    from .storage import digest
    import json
    return {**base, 'workflow': request.workflow, 'reviewers': request.reviewers,
            'model': request.model, 'token_budget': request.token_budget,
            'evidence_mode': request.evidence_mode,
            'evidence_policy': ('none' if request.workflow == 'manuscript-only' else
                                'frozen' if request.source_packet is not None else 'live'),
            'source_packet_manifest': packet or None,
            'source_packet_manifest_sha256': (digest(json.dumps(packet, sort_keys=True))
                                              if packet else None),
            'synthesis_policy': (request.synthesis_policy if request.workflow == 'ensemble' else 'none')}
