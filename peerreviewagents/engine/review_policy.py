"""Named review policies: every limit a review runs under, in one object.

The engine used to hard-code the shape of a review in the middle of
``reviewer.review``: medium effort, one or two model calls, three to six tool
calls, a 900 second timeout, at most five findings in about 900 words. Those
numbers suited the bounded study the engine grew out of and they are the reason
a run reads like a form to fill in rather than an investigation.

A ``ReviewPolicy`` makes them data. ``standard-v1`` holds exactly the previous
numbers, so existing presets produce byte-identical prompts, schemas and
protocol records and existing run directories stay resumable.
``workspace-agentic-v1`` is the forward direction: one long high-effort
session, a thirty call tool allowance, no finding cap, a two hour limit, and
one bounded retry if the call fails or times out. Evidence handling does not
change: every finding is still quoted, anchored and validated, and a finding
that fails validation is quarantined with a reason instead of ending the run.
"""
from dataclasses import asdict, dataclass
from types import MappingProxyType

POLICY_VERSION = 'review-policy-v1'

APPROVED_EFFORTS = ('low', 'medium', 'high')
MAX_ATTEMPT_CEILING = 2

AGENTIC_SESSION = '''AGENTIC SESSION: This is one long investigative session, not a single quick pass. Work through the manuscript the way a careful reviewer with time would. Read it once for the argument, then list the claims the conclusions actually rest on, then check them one at a time against the methods, the tables, the figure pixels and the prior work. Use as many of your permitted tool calls as the manuscript genuinely requires, and prefer a tool call over an assumption whenever a check is available. Try to break your own suspicions before you report them.
Report every consequential finding you have established, at the length the evidence needs. There is no finding quota in either direction: do not pad the review to look thorough, and do not drop a supported, consequential concern to look concise. Keep quoting, anchoring and validating every finding exactly as instructed. Record what you checked and discarded so the investigation is auditable.'''


@dataclass(frozen=True)
class ReviewPolicy:
    """One named set of review limits. Values are data, never behaviour."""

    name: str
    effort: str = 'medium'
    observed_token_stop: int = 250000
    call_timeout_seconds: int = 900
    finding_cap: int | None = 5
    candidate_finding_cap: int | None = 10
    word_budget: int = 900
    tool_calls: int | None = None
    revision_tool_calls: int = 2
    debate_tool_calls: int = 2
    external_requests: int = 6
    debate_external_requests: int = 2
    tool_checks: int | None = 4
    generation_calls: int | None = None
    max_attempts: int = 1
    session: str = 'bounded-passes'
    session_instruction: str = ''

    def __post_init__(self):
        if self.effort not in APPROVED_EFFORTS:
            raise ValueError('Review policy effort must be low, medium or high')
        if not 1 <= self.max_attempts <= MAX_ATTEMPT_CEILING:
            raise ValueError('A review policy allows one call or one bounded retry')
        for value in (self.observed_token_stop, self.call_timeout_seconds, self.word_budget,
                      self.revision_tool_calls, self.debate_tool_calls, self.external_requests,
                      self.debate_external_requests):
            if type(value) is not int or value <= 0:
                raise ValueError('Review policy limits must be positive integers')
        for value in (self.finding_cap, self.candidate_finding_cap, self.tool_calls,
                      self.tool_checks, self.generation_calls):
            if value is not None and (type(value) is not int or value <= 0):
                raise ValueError('Optional review policy limits must be positive integers or None')

    @property
    def single_session(self):
        """One long investigative session rather than bounded short passes."""
        return self.session == 'single-long-agentic-session'

    def record(self):
        return {'policy_version': POLICY_VERSION, **asdict(self)}

    def investigator_tool_limit(self, mode, domain_support=True):
        """Keep the historical mode-derived allowance unless the policy pins one."""
        if self.tool_calls is not None:
            return self.tool_calls
        if not domain_support:
            return 3
        return 4 if mode in ('panel', 'self-revision', 'independent') else 6

    def generation_call_limit(self, mode):
        """Generation calls one run directory may spend, retries included."""
        if self.generation_calls is not None:
            return self.generation_calls
        return 2 if mode in ('panel', 'self-revision') else 1

    def shared_instructions(self):
        """The COMMON review instructions, with this policy's budget sentence."""
        from .review_schema import common_instructions
        return common_instructions(self.finding_cap, self.word_budget)

    def final_review_schema(self, base=None):
        from .review_schema import capped_findings
        return capped_findings(base, self.finding_cap)

    def candidate_review_schema(self, base=None):
        from .review_schema import capped_findings
        return capped_findings(base, self.candidate_finding_cap)

    def protocol_overlay(self):
        """Protocol keys this policy adds.

        The default policy adds none, so an existing run's ``protocol.json``
        and the identity hash derived from it are unchanged and the run stays
        resumable. A non-default policy records itself in full, because the
        protocol is the durable statement of what a run was allowed to do.
        """
        if self.name == DEFAULT_POLICY_NAME:
            return {}
        return {'review_policy': self.name, 'review_policy_version': POLICY_VERSION,
                'max_attempts': self.max_attempts, 'review_policy_record': self.record()}


STANDARD = ReviewPolicy(name='standard-v1')

AGENTIC = ReviewPolicy(
    name='workspace-agentic-v1',
    effort='high',
    observed_token_stop=1500000,
    call_timeout_seconds=7200,
    finding_cap=None,
    candidate_finding_cap=None,
    word_budget=6000,
    tool_calls=30,
    revision_tool_calls=8,
    debate_tool_calls=8,
    external_requests=24,
    debate_external_requests=8,
    tool_checks=30,
    generation_calls=2,
    max_attempts=2,
    session='single-long-agentic-session',
    session_instruction=AGENTIC_SESSION,
)

DEFAULT_POLICY_NAME = STANDARD.name
DEFAULT_POLICY = STANDARD

_POLICIES = MappingProxyType({policy.name: policy for policy in (STANDARD, AGENTIC)})


def review_policies():
    """Serializable policy records, without exposing mutable shared state."""
    return {name: policy.record() for name, policy in _POLICIES.items()}


def get_review_policy(policy=None):
    """Resolve a policy name, an explicit policy, or None for the default."""
    if policy is None:
        return DEFAULT_POLICY
    if isinstance(policy, ReviewPolicy):
        return policy
    try:
        return _POLICIES[policy]
    except KeyError as error:
        raise ValueError('Unknown review policy') from error
