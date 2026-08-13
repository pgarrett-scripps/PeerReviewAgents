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
    the next vendor in the fallback chain.
    """


class VendorUnavailableError(Exception):
    """Raised by a vendor for transport-level failures: connection refused,
    timeout, 5xx, or a missing client library.

    Recoverable exactly like :class:`RateLimitError` — the router falls
    through to the next vendor. Vendors used to swallow these into
    "[vendor unavailable: ...]" strings, which the router counted as served
    answers: an outage on the primary was recorded as a clean zero-hit
    search and the healthy fallback vendor was never tried.

    A non-429 4xx is deliberately NOT this: the vendor reached the API and
    the API judged the query, so that verdict is returned as text rather
    than shopped to a vendor that would judge it differently.
    """


class ResearchUnavailableError(Exception):
    """Raised by the router when every vendor in the chain failed.

    Deliberately an exception rather than degrade text: the tool loop
    records a raised error on the provenance line (``tool_error``), while
    returned text is counted as a served answer — an outage that came back
    as a string was indistinguishable from "searched clean, zero hits".
    """
