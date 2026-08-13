"""PubMed vendor — NCBI E-utilities (esearch + esummary).

Two-call sequence per query: ``esearch`` returns PubMed IDs for the
query, ``esummary`` resolves those to titles, authors, dates, journal
names. Free, no key required, but rate-limited to 3 req/s without one
and 10 req/s with ``NCBI_API_KEY`` set.

Reference: https://www.ncbi.nlm.nih.gov/books/NBK25500/
"""

from __future__ import annotations

import os

from . import RateLimitError, VendorUnavailableError

_ESEARCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
_ESUMMARY = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"


def search(query: str, max_results: int = 5) -> str:
    """Return a formatted plaintext block of PubMed hits for ``query``."""
    try:
        import requests
    except ImportError as exc:
        raise VendorUnavailableError("pubmed unavailable: install `requests`") from exc

    api_key = os.environ.get("NCBI_API_KEY")
    common: dict[str, str] = {"db": "pubmed", "retmode": "json"}
    if api_key:
        common["api_key"] = api_key

    # 1) Find PMIDs.
    try:
        r = requests.get(
            _ESEARCH,
            params={**common, "term": query, "retmax": str(max_results)},
            timeout=20,
        )
    except Exception as exc:  # noqa: BLE001
        # Outage, not an answer: raised so the router can try the fallback
        # vendor rather than filing a clean zero-hit search.
        raise VendorUnavailableError(f"pubmed unreachable: {exc}") from exc

    if r.status_code == 429:
        raise RateLimitError("pubmed HTTP 429")
    if r.status_code >= 500:
        raise VendorUnavailableError(f"pubmed HTTP {r.status_code}")
    try:
        r.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        # Remaining 4xx: the API judged the query; that verdict is an answer.
        return f"[pubmed HTTP error: {exc}]"

    pmids = (r.json().get("esearchresult") or {}).get("idlist") or []
    if not pmids:
        return "No PubMed results."

    # 2) Resolve to titles + authors + journal.
    try:
        r2 = requests.get(
            _ESUMMARY,
            params={**common, "id": ",".join(pmids)},
            timeout=20,
        )
    except Exception as exc:  # noqa: BLE001
        raise VendorUnavailableError(f"pubmed esummary unreachable: {exc}") from exc
    if r2.status_code == 429:
        raise RateLimitError("pubmed HTTP 429")
    if r2.status_code >= 500:
        raise VendorUnavailableError(f"pubmed esummary HTTP {r2.status_code}")
    try:
        r2.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        return f"[pubmed esummary error: {exc}]"

    docs = (r2.json().get("result") or {})
    out: list[str] = []
    for pmid in pmids:
        d = docs.get(pmid)
        if not d:
            continue
        title = d.get("title") or "(untitled)"
        journal = d.get("fulljournalname") or d.get("source") or ""
        year = (d.get("pubdate") or "")[:4]
        authors = d.get("authors") or []
        first_author = authors[0].get("name") if authors else ""
        et_al = " et al." if len(authors) > 1 else ""
        out.append(
            f"- {title}\n"
            f"  {first_author}{et_al} — {journal} ({year}) "
            f"PMID:{pmid} — https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
        )
    return "\n".join(out) if out else "No PubMed results."
