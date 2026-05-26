"""Live-research tools for grounding reviews in external evidence.

Each tool degrades gracefully: if a dependency or network is unavailable it
returns a short note rather than raising, so a review run never hard-fails on
the research layer.

The web search slot is backed by Tavily (real search API with retry/cache),
not by the prior DuckDuckGo scraper which was unreliable and gated behind a
package that wasn't even installed by default.
"""

from __future__ import annotations

import os
from typing import Iterable

from langchain_core.tools import tool

from .tavily_client import TavilyResearchClient, get_tavily_client


# ---------------------------------------------------------------------------
# Scientific literature tools — keep their original behavior.
# ---------------------------------------------------------------------------


@tool
def arxiv_search(query: str, max_results: int = 5) -> str:
    """Search arXiv for related papers. Use to check novelty and prior art."""
    try:
        import arxiv  # type: ignore

        client = arxiv.Client()
        search = arxiv.Search(query=query, max_results=max_results)
        items = []
        for r in client.results(search):
            items.append(f"- {r.title} ({r.published.year}) — {r.entry_id}\n  {r.summary[:300]}")
        return "\n".join(items) if items else "No arXiv results."
    except Exception as exc:  # noqa: BLE001
        return f"[arxiv_search unavailable: {exc}]"


@tool
def semantic_scholar_search(query: str, limit: int = 5) -> str:
    """Search Semantic Scholar for related work and citation counts."""
    try:
        import requests

        headers = {}
        api_key = os.environ.get("SEMANTIC_SCHOLAR_API_KEY")
        if api_key:
            headers["x-api-key"] = api_key
        resp = requests.get(
            "https://api.semanticscholar.org/graph/v1/paper/search",
            params={"query": query, "limit": limit, "fields": "title,year,citationCount,abstract"},
            headers=headers,
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json().get("data", [])
        if not data:
            return "No Semantic Scholar results."
        out = []
        for p in data:
            out.append(
                f"- {p.get('title')} ({p.get('year')}, cites={p.get('citationCount')})\n"
                f"  {(p.get('abstract') or '')[:300]}"
            )
        return "\n".join(out)
    except Exception as exc:  # noqa: BLE001
        return f"[semantic_scholar_search unavailable: {exc}]"


# ---------------------------------------------------------------------------
# Tavily-backed web search + extract. Built via a factory so each tool is
# bound to a specific shared client (per-process, per-config fingerprint).
# ---------------------------------------------------------------------------


def _make_tavily_tools(client: TavilyResearchClient) -> list:
    @tool
    def tavily_search(query: str) -> str:
        """Web search for claim verification, definitions, and supporting
        evidence outside the scientific-paper APIs. Returns a list of
        title / snippet / url for the top hits, ranked by relevance.
        Follow up with `tavily_extract` on a specific URL to read its
        full clean text. Prefer the arxiv / semantic_scholar tools for
        purely academic queries."""
        return client.search(query)

    @tool
    def tavily_extract(url: str) -> str:
        """Fetch the clean, readable full text of a single URL found via
        `tavily_search`. Use when you need to verify a specific claim
        against the source rather than relying on a snippet. The URL must
        be http(s). Output is truncated to keep context manageable."""
        return client.extract(url)

    return [tavily_search, tavily_extract]


# ---------------------------------------------------------------------------
# Tool resolution.
# ---------------------------------------------------------------------------

# Aliases for legacy config values. Older `peerreview.toml` files used
# `research_tools = ["web", ...]`; map that to the new Tavily slot so
# users don't have to edit their configs to keep working.
_LEGACY_ALIASES = {"web": "tavily"}

# Names that map to the static (non-Tavily) tools.
_STATIC_TOOLS = {
    "arxiv": arxiv_search,
    "scholar": semantic_scholar_search,
}


def _resolve_names(requested: Iterable[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in requested:
        name = _LEGACY_ALIASES.get(raw, raw)
        if name in seen:
            continue
        seen.add(name)
        out.append(name)
    return out


def get_research_tools(config: dict) -> list:
    """Return the LangChain tools available to research-enabled agents.

    Honors `research_enabled`. Filters to the subset in `research_tools`.
    Drops Tavily silently when `TAVILY_API_KEY` is not set (logged once
    per process). Listing `"tavily"` also enables `tavily_extract` so the
    agent can read full text of URLs it finds.
    """
    if not config.get("research_enabled"):
        return []

    requested = _resolve_names(config.get("research_tools") or [])
    tools: list = []

    for name in requested:
        if name == "tavily":
            client = get_tavily_client(config, os.environ.get("TAVILY_API_KEY"))
            if client is not None:
                search_tool, extract_tool = _make_tavily_tools(client)
                tools.append(search_tool)
                tools.append(extract_tool)
        elif name == "tavily_extract":
            client = get_tavily_client(config, os.environ.get("TAVILY_API_KEY"))
            if client is not None:
                _, extract_tool = _make_tavily_tools(client)
                if not any(getattr(t, "name", "") == extract_tool.name for t in tools):
                    tools.append(extract_tool)
        elif name in _STATIC_TOOLS:
            tools.append(_STATIC_TOOLS[name])

    return tools
