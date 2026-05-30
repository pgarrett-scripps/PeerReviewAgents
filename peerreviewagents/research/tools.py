"""LangChain ``@tool`` wrappers exposed to reviewer agents.

Each tool delegates to :func:`peerreviewagents.research.interface.route`,
which picks the configured vendor and falls through to the next on
rate-limit. Tools are intentionally *logical operations* (not
vendor-specific endpoints) so the agent prompt doesn't have to know
which backend served the result.

Reviewers declare the tool *names* they want in
:func:`peerreviewagents.agents.reviewers.base.make_reviewer_node`;
:func:`get_tools_by_name` resolves those names to bound tools at graph
build time.
"""

from __future__ import annotations

from langchain_core.tools import tool

from .interface import available_methods, route


# Module-level config holder — set by ``get_tools_by_name(names, config)``
# before agent execution so the @tool functions (which can't take a
# config arg per LangChain's tool schema rules) can read it.
_ACTIVE_CONFIG: dict = {}


def _cfg() -> dict:
    return _ACTIVE_CONFIG


# --- Logical operations (these are what reviewers actually call) -----------


@tool
def find_related_work(query: str, max_results: int = 5) -> str:
    """Find related-work papers for a query. Use for novelty / prior-art checks.

    Routes through Semantic Scholar (primary) → arXiv (rate-limit fallback).
    """
    return route("find_related_work", _cfg(), query=query, max_results=max_results)


@tool
def search_biomedical_literature(query: str, max_results: int = 5) -> str:
    """Search peer-reviewed biomedical literature. Use for clinical, biology,
    or medical claims.

    Routes through PubMed (primary) → bioRxiv/medRxiv preprints via
    EuropePMC (rate-limit fallback).
    """
    return route("search_biomedical_literature", _cfg(), query=query, max_results=max_results)


@tool
def search_preprints(query: str, max_results: int = 5) -> str:
    """Search recent preprints (bioRxiv / medRxiv / arXiv). Use when the most
    recent work might not yet be in peer-reviewed venues.

    Routes through bioRxiv (primary, via EuropePMC) → arXiv (fallback).
    """
    return route("search_preprints", _cfg(), query=query, max_results=max_results)


# --- Tool registry & lookup -------------------------------------------------


_TOOL_REGISTRY = {
    "find_related_work": find_related_work,
    "search_biomedical_literature": search_biomedical_literature,
    "search_preprints": search_preprints,
}


def available_tool_names() -> list[str]:
    return list(_TOOL_REGISTRY)


def get_tools_by_name(names: list[str], config: dict) -> list:
    """Resolve tool ``names`` to bound tool objects, installing ``config``
    so the underlying router can read ``data_vendors`` / ``tool_vendors``.
    """
    # Mutate in place so existing tool refs (already returned by an
    # earlier call) pick up the latest config too. The pipeline is
    # single-job-per-process today, so a global is fine here.
    _ACTIVE_CONFIG.clear()
    _ACTIVE_CONFIG.update(config)

    unknown = [n for n in names if n not in _TOOL_REGISTRY]
    if unknown:
        raise ValueError(
            f"unknown research tool(s): {unknown}; "
            f"available: {available_tool_names()}"
        )
    return [_TOOL_REGISTRY[n] for n in names]


__all__ = [
    "find_related_work",
    "search_biomedical_literature",
    "search_preprints",
    "available_tool_names",
    "get_tools_by_name",
    "available_methods",
]
