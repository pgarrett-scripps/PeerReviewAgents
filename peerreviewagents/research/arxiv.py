"""arXiv vendor — keyword search via the ``arxiv`` Python client."""

from __future__ import annotations

from . import RateLimitError


def search(query: str, max_results: int = 5) -> str:
    """Return a formatted plaintext block of arXiv hits for ``query``.

    Graceful-degrade: on any error the function returns a short note
    rather than raising, except for explicit rate-limit signals which
    propagate as :class:`RateLimitError` so the router can fall through.
    """
    try:
        import arxiv  # type: ignore
    except ImportError:
        return "[arxiv unavailable: install with `pip install -e .[research]`]"

    try:
        client = arxiv.Client()
        search_obj = arxiv.Search(query=query, max_results=max_results)
        items = []
        for r in client.results(search_obj):
            items.append(
                f"- {r.title} ({r.published.year}) — {r.entry_id}\n"
                f"  {r.summary[:300]}"
            )
        return "\n".join(items) if items else "No arXiv results."
    except Exception as exc:  # noqa: BLE001
        msg = str(exc).lower()
        if "429" in msg or "rate" in msg:
            raise RateLimitError(f"arxiv rate-limited: {exc}") from exc
        return f"[arxiv unavailable: {exc}]"
