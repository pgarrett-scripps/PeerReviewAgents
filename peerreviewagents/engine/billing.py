"""Validate recorded subscription evidence before launching a bounded workflow."""
import math
import time


CLAUDE_POLICY = 'claude-subscription-v1'
CLAUDE_AUTH_METHODS = ('claude.ai', 'oauth')


def claude_billing_record(status, now=None):
    """Record an operator's confirmation that a Claude subscription cannot buy extra usage."""
    if (not status.get('loggedIn') or status.get('authMethod') not in CLAUDE_AUTH_METHODS
            or status.get('apiProvider') not in (None, 'firstParty')):
        raise ValueError('Log in to Claude Code with a Claude subscription first (claude auth login)')
    return {'policy': CLAUDE_POLICY, 'provider': 'claude-code',
            'checked_at': time.time() if now is None else now,
            'auth_method': status['authMethod'], 'subscription_type': status.get('subscriptionType'),
            'extra_usage_disabled': True, 'purchases_authorized': False,
            'attestation': 'The operator confirmed that paid extra usage is disabled in Claude settings.'}


def verify_claude_billing(verification, now):
    """Accept a recent operator confirmation. Each call also verifies the live overage state."""
    if verification.get('policy') != CLAUDE_POLICY or verification.get('provider') != 'claude-code':
        raise ValueError('A Claude subscription billing record is required. Create one with confirm-billing.')
    checked = verification.get('checked_at', 0)
    if isinstance(checked, bool) or not isinstance(checked, (int, float)) or not math.isfinite(checked):
        raise ValueError('Invalid billing evidence timestamp')
    if not 0 <= now - checked < 86400:
        raise ValueError('Claude billing confirmation must be refreshed within 24 hours')
    if verification.get('auth_method') not in CLAUDE_AUTH_METHODS:
        raise ValueError('The billing record must come from a Claude subscription login')
    if verification.get('extra_usage_disabled') is not True:
        raise ValueError('Confirm that paid extra usage is disabled in Claude settings')
    if verification.get('purchases_authorized') is not False:
        raise ValueError('This policy does not authorize purchases')
    return CLAUDE_POLICY


def verify_billing(verification, now=None, provider='codex'):
    """Accept legacy zero-credit evidence or fresh included-allowance evidence.

    This is a launch guard, not a provider-side spending limit. The coordinator
    must refresh the authoritative usage snapshot before every workflow and
    retain a separate cumulative study token budget. No account setting is
    changed, and this policy never authorizes purchases or resets. Claude Code
    runs use their own policy, and neither provider accepts the other's record.
    """
    now = time.time() if now is None else now
    if provider == 'claude-code':
        return verify_claude_billing(verification, now)
    if provider != 'codex':
        raise ValueError('Unsupported subscription provider')
    checked = verification.get('checked_at', 0)
    if isinstance(checked, bool) or not isinstance(checked, (int, float)) or not math.isfinite(checked):
        raise ValueError('Invalid billing evidence timestamp')
    age = now - checked
    if verification.get('policy') != 'included-allowance-v1':
        if verification.get('no_paid_credits_verified') is True and 0 <= age < 86400:
            return 'legacy-zero-credit'
        raise ValueError('Recent subscription billing verification required')
    if not 0 <= age <= 300:
        raise ValueError('Included allowance evidence must be refreshed within five minutes')
    if verification.get('source') != 'Codex app get_usage_limits':
        raise ValueError('An authoritative account usage snapshot is required')
    if verification.get('purchases_authorized') is not False or verification.get('resets_authorized') is not False:
        raise ValueError('This policy does not authorize purchases or resets')
    snapshot = verification.get('usage_snapshot', {})
    buckets = snapshot.get('rateLimitsByLimitId') or {'codex': snapshot.get('rateLimits')}
    if not isinstance(buckets, dict) or not buckets:
        raise ValueError('Account usage limits are unavailable')
    windows = 0
    for bucket in buckets.values():
        if not isinstance(bucket, dict) or bucket.get('planType') not in ('pro', 'plus'):
            raise ValueError('Included allowance policy requires a verified Pro or Plus plan')
        if bucket.get('spendControlReached') is not False or bucket.get('rateLimitReachedType'):
            raise ValueError('Account has reached a usage or spending limit')
        for name in ('primary', 'secondary'):
            window = bucket.get(name)
            if window is None:
                continue
            used = window.get('usedPercent')
            if isinstance(used, bool) or not isinstance(used, (int, float)) or not math.isfinite(used):
                raise ValueError('Account usage percentage is unavailable')
            if not 0 <= used < 25:
                raise ValueError('Conservative included-allowance launch limit reached')
            reset = window.get('resetsAt')
            if isinstance(reset, bool) or not isinstance(reset, (int, float)) or reset <= now:
                raise ValueError('Account usage window must be refreshed')
            windows += 1
        if bucket.get('individualLimit') is not None:
            raise ValueError('Individual account limits require explicit inspection')
    if windows == 0:
        raise ValueError('No included usage window is available')
    return 'included-allowance-v1'
