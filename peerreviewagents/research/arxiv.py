"""arXiv vendor — keyword search via the ``arxiv`` Python client."""

from __future__ import annotations

from typing import Any

from . import RateLimitError, VendorUnavailableError

# What the other vendors pass to their own requests. semantic_scholar, biorxiv
# and pubmed all name `timeout=20`; this one had no way to.
_TIMEOUT_S = 20


def _bounded(session: Any) -> Any:
    """Give the client's session the default timeout it does not have.

    The `arxiv` client issues ``self._session.get(url, headers=...)`` with no
    timeout, and `requests` defaults to None — which blocks forever. Its
    constructor takes page_size, delay_seconds and num_retries, and offers no
    way to say otherwise, so the session is reached for directly.

    That gap cost a whole run. A search agent's lookup hung, its worker future
    never completed, and LangGraph's teardown waits on pending futures with no
    timeout of its own — so a review that had already finished its panel sat
    with one open socket and nothing running until it was killed. The model
    deadline added for the same symptom did not help here, because the stuck
    call was never a model call.

    ``Session.get`` routes through ``Session.request``, so wrapping that one
    method covers every call the client makes. ``setdefault`` leaves an
    explicit timeout alone, should the library ever start passing one.
    """
    original = session.request

    def request(method: str, url: str, **kwargs: Any) -> Any:
        kwargs.setdefault("timeout", _TIMEOUT_S)
        return original(method, url, **kwargs)

    session.request = request
    return session


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
        _bounded(client._session)
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
