"""arXiv vendor — keyword search via the ``arxiv`` Python client."""

from __future__ import annotations

from . import RateLimitError, VendorUnavailableError


def search(query: str, max_results: int = 5) -> str:
    """Return a formatted plaintext block of arXiv hits for ``query``.

    Rate limits propagate as :class:`RateLimitError` and every other client
    failure as :class:`VendorUnavailableError`, so the router can fall
    through. The client gives no clean "the API judged your query" channel
    (bad queries come back as empty feeds, not errors), so unlike the
    HTTP vendors there is no 4xx degrade-text path here: an exception from
    the client means the lookup did not happen.
    """
    try:
        import arxiv  # type: ignore
    except ImportError as exc:
        raise VendorUnavailableError(
            "arxiv unavailable: install with `pip install -e .[research]`"
        ) from exc

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
        raise VendorUnavailableError(f"arxiv unavailable: {exc}") from exc
