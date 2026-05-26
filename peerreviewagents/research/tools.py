"""Live-research tools for grounding reviews in external evidence.

Each tool degrades gracefully: if a dependency or network is unavailable it
returns a short note rather than raising, so a review run never hard-fails on
the research layer.
"""

from __future__ import annotations

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

        resp = requests.get(
            "https://api.semanticscholar.org/graph/v1/paper/search",
            params={"query": query, "limit": limit, "fields": "title,year,citationCount,abstract"},
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


@tool
def web_search(query: str) -> str:
    """General web search for facts, definitions, and claim verification."""
    try:
        from ddgs import DDGS  # optional dependency

        with DDGS() as ddgs:
            hits = list(ddgs.text(query, max_results=5))
        if not hits:
            return "No web results."
        return "\n".join(f"- {h.get('title')}: {h.get('body', '')[:200]} ({h.get('href')})" for h in hits)
    except Exception as exc:  # noqa: BLE001
        return f"[web_search unavailable: {exc}]"


_ALL = {"arxiv": arxiv_search, "scholar": semantic_scholar_search, "web": web_search}


def get_research_tools(config: dict) -> list:
    if not config.get("research_enabled"):
        return []
    return [_ALL[name] for name in config.get("research_tools", []) if name in _ALL]
