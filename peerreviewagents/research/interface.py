"""Vendor-routing dispatcher for research operations.

Reviewers call *logical operations* via :func:`route` (e.g.
``route("find_related_work", config, query="X-ray diffraction")``).
The router resolves the operation to a category, looks up the configured
vendor list (``data_vendors[category]``), and tries each vendor in
order. A :class:`peerreviewagents.research.RateLimitError` from a
vendor triggers fall-through to the next vendor; any other exception
the vendor either swallows (returning graceful-degrade text) or raises
back to the caller.

Per-method override: ``tool_vendors[method]`` wins over the
category-level default. Vendors are listed comma-separated:
``"semantic_scholar,arxiv"`` means "Semantic Scholar primary, arXiv on
rate-limit."

This mirrors the shape of TradingAgents'
``tradingagents/dataflows/interface.py``.
"""

from __future__ import annotations

import threading
from typing import Any, Callable

from . import RateLimitError, arxiv, biorxiv, pubmed, semantic_scholar

# --- Category & method registry --------------------------------------------

# Each method has a category (which gets a default vendor list in
# ``data_vendors``) and a per-vendor implementation in ``_VENDOR_IMPL``.
_CATEGORY_FOR_METHOD: dict[str, str] = {
    "find_related_work": "paper_search",
    "search_biomedical_literature": "biomedical",
    "search_preprints": "preprints",
}

_VENDOR_IMPL: dict[str, dict[str, Callable[..., str]]] = {
    "find_related_work": {
        "semantic_scholar": semantic_scholar.search,
        "arxiv": arxiv.search,
    },
    "search_biomedical_literature": {
        "pubmed": pubmed.search,
        "biorxiv": biorxiv.search,
    },
    "search_preprints": {
        "biorxiv": biorxiv.search,
        "arxiv": arxiv.search,
    },
}


_DEFAULT_VENDORS: dict[str, str] = {
    "paper_search": "semantic_scholar,arxiv",
    "biomedical": "pubmed,biorxiv",
    "preprints": "biorxiv,arxiv",
}


def available_methods() -> list[str]:
    return sorted(_CATEGORY_FOR_METHOD)


def category_for(method: str) -> str:
    cat = _CATEGORY_FOR_METHOD.get(method)
    if cat is None:
        raise ValueError(f"unknown research method {method!r}; "
                         f"available: {available_methods()}")
    return cat


def resolve_vendors(method: str, config: dict | None = None) -> list[str]:
    """Return the ordered vendor preference for ``method``.

    Tool-level override (``tool_vendors[method]``) wins over the
    category-level default (``data_vendors[category]``), which wins
    over the built-in default in :data:`_DEFAULT_VENDORS`.
    Vendors not present in the per-method implementation table are
    silently dropped (so a stale config can't break the call).
    """
    config = config or {}
    method_overrides = (config.get("tool_vendors") or {})
    cat_defaults = (config.get("data_vendors") or {})
    category = category_for(method)
    raw = (
        method_overrides.get(method)
        or cat_defaults.get(category)
        or _DEFAULT_VENDORS.get(category, "")
    )
    requested = [v.strip() for v in raw.split(",") if v.strip()]
    available = _VENDOR_IMPL.get(method, {})
    # Preserve user order; append any unlisted vendors so a misconfigured
    # primary still has a fallback.
    chain: list[str] = []
    for v in requested:
        if v in available and v not in chain:
            chain.append(v)
    for v in available:
        if v not in chain:
            chain.append(v)
    return chain


# Which vendor actually answered the most recent lookup on this thread.
#
# The router falls through on rate limits, so the vendor that serves a query is
# often not the one configured first — and the answer changes what the result
# is worth. `find_related_work` prefers Semantic Scholar and falls back to
# arXiv; for a biology manuscript that fallback is the wrong corpus, and it
# returns confidently irrelevant papers rather than nothing. Read by the tool
# tracing so a published search says who answered it, not just how many hits
# came back.
#
# Thread-local for the same reason the current node is: reviewers fan out in
# parallel, and each executes its own tools on its own thread.
_LAST: threading.local = threading.local()


def last_vendor() -> str:
    """Vendor that served the last :func:`route` call on this thread."""
    return getattr(_LAST, "vendor", "")


def route(method: str, config: dict | None = None, **kwargs: Any) -> str:
    """Dispatch ``method`` to the first non-rate-limited vendor.

    ``kwargs`` are forwarded verbatim to the vendor function; every
    vendor function in this layer takes the same positional / keyword
    args (``query``, ``max_results``).
    """
    # Defense-in-depth: in offline mode the reviewer/auditor nodes never bind
    # research tools, so this is normally unreachable — but if anything does
    # call route() while research is disabled, refuse before touching a vendor.
    _LAST.vendor = ""
    if config is not None and not config.get("research_enabled", True):
        return f"[research disabled: offline mode — no vendor called for {method!r}]"

    vendors = resolve_vendors(method, config)
    impls = _VENDOR_IMPL.get(method, {})
    if not vendors:
        return f"[no vendor configured for {method!r}]"

    last_rate_limit: RateLimitError | None = None
    for vendor in vendors:
        fn = impls.get(vendor)
        if fn is None:
            continue
        try:
            result = fn(**kwargs)
        except RateLimitError as exc:
            last_rate_limit = exc
            continue
        _LAST.vendor = vendor
        return result
    # Every vendor in the chain rate-limited.
    return (
        f"[{method}: all configured vendors rate-limited "
        f"({last_rate_limit})]"
    )
