"""Research layer: per-source clients + a vendor-routing dispatcher.

Reviewers consume *logical operations* (``find_related_work``,
``search_biomedical_literature``, ``search_preprints``); the router in
:mod:`peerreviewagents.research.interface` picks the configured vendor
and falls through to the next on rate-limit.

Shape mirrors TradingAgents' ``tradingagents/dataflows`` layout. See
:mod:`peerreviewagents.research.interface` for the category map and
fallback semantics.
"""

from __future__ import annotations


class RateLimitError(Exception):
    """Raised by a vendor when the upstream API rate-limits the caller.

    The router treats this as a recoverable signal and falls through to
    the next vendor in the fallback chain. All other exceptions are
    returned to the caller as graceful-degrade text by the vendor itself.
    """
