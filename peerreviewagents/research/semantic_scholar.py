"""Semantic Scholar vendor — keyword search via the Graph API.

Sends an optional ``x-api-key`` header if ``SEMANTIC_SCHOLAR_API_KEY`` is
set in the environment. Without a key the public tier rate-limits
aggressively, so the router will often fall through to arXiv.
"""

from __future__ import annotations

import os

from . import RateLimitError

_BASE = "https://api.semanticscholar.org/graph/v1/paper/search"


def search(query: str, max_results: int = 5) -> str:
    """Return a formatted plaintext block of Semantic Scholar hits for ``query``."""
    try:
        import requests
    except ImportError:
        return "[semantic_scholar unavailable: install `requests`]"

    headers: dict[str, str] = {}
    api_key = os.environ.get("SEMANTIC_SCHOLAR_API_KEY")
    if api_key:
        headers["x-api-key"] = api_key
    try:
        resp = requests.get(
            _BASE,
            params={
                "query": query,
                "limit": max_results,
                "fields": "title,year,citationCount,abstract,authors",
            },
            headers=headers,
            timeout=20,
        )
    except Exception as exc:  # noqa: BLE001
        return f"[semantic_scholar unavailable: {exc}]"

    if resp.status_code == 429:
        raise RateLimitError("semantic_scholar HTTP 429")
    try:
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        return f"[semantic_scholar HTTP error: {exc}]"

    data = resp.json().get("data", []) or []
    if not data:
        return "No Semantic Scholar results."
    out: list[str] = []
    for p in data:
        title = p.get("title") or "(untitled)"
        year = p.get("year")
        cites = p.get("citationCount")
        abstract = (p.get("abstract") or "")[:300]
        out.append(
            f"- {title} ({year}, cites={cites})\n  {abstract}"
        )
    return "\n".join(out)
