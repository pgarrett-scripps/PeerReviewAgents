"""Live-research tools for grounding reviews in external evidence.

Two structured paper-lookup tools — arXiv and Semantic Scholar — that
reviewers can call when they need to verify a citation or check for
prior art. Each degrades gracefully: if the dependency or network is
unavailable, the tool returns a short note instead of raising, so a
single research hiccup never sinks a review run.

General-purpose web search is provided separately by OpenRouter's
server-side `openrouter:web_search` tool (wired in from
:mod:`peerreviewagents.agents.utils.agent_utils`), so we don't ship a
client for it here.
"""

from __future__ import annotations

import os

from langchain_core.tools import tool


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


def get_research_tools(config: dict) -> list:
    """Return the structured paper-lookup tools available to reviewers.

    OpenRouter's server-side web search is attached separately by
    :func:`run_agent`, so it does not need to appear in this list.
    """
    return [arxiv_search, semantic_scholar_search]
