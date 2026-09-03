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

import copy

from langchain_core.tools import tool

from .interface import available_methods, route

# --- Logical operations (these are what reviewers actually call) -----------


@tool
def find_related_work(query: str, max_results: int = 5) -> str:
    """Find related-work papers for a query. Use for novelty / prior-art checks.

    Routes through Semantic Scholar (primary) → arXiv (rate-limit fallback).
    """
    return route("find_related_work", {}, query=query, max_results=max_results)


@tool
def search_biomedical_literature(query: str, max_results: int = 5) -> str:
    """Search peer-reviewed biomedical literature. Use for clinical, biology,
    or medical claims.

    Routes through PubMed (primary) → bioRxiv/medRxiv preprints via
    EuropePMC (rate-limit fallback).
    """
    return route("search_biomedical_literature", {}, query=query, max_results=max_results)


@tool
def search_preprints(query: str, max_results: int = 5) -> str:
    """Search recent preprints (bioRxiv / medRxiv / arXiv). Use when the most
    recent work might not yet be in peer-reviewed venues.

    Routes through bioRxiv (primary, via EuropePMC) → arXiv (fallback).
    """
    return route("search_preprints", {}, query=query, max_results=max_results)


# --- Tool registry & lookup -------------------------------------------------


_TOOL_REGISTRY = {
    "find_related_work": find_related_work,
    "search_biomedical_literature": search_biomedical_literature,
    "search_preprints": search_preprints,
}


def available_tool_names() -> list[str]:
    return list(_TOOL_REGISTRY)


def get_tools_by_name(names: list[str], config: dict) -> list:
    """Resolve tool ``names`` to independently configured tool objects."""
    unknown = [n for n in names if n not in _TOOL_REGISTRY]
    if unknown:
        raise ValueError(
            f"unknown research tool(s): {unknown}; "
            f"available: {available_tool_names()}"
        )

    bound_config = copy.deepcopy(config)
    tools = []
    for name in names:
        template = _TOOL_REGISTRY[name]

        def invoke(query: str, max_results: int = 5, *, _name=name) -> str:
            return route(
                _name,
                bound_config,
                query=query,
                max_results=max_results,
            )

        invoke.__name__ = name
        invoke.__doc__ = template.description
        tools.append(tool(invoke))
    return tools


__all__ = [
    "find_related_work",
    "search_biomedical_literature",
    "search_preprints",
    "available_tool_names",
    "get_tools_by_name",
    "available_methods",
]
