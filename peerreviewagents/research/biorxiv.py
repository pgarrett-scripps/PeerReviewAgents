"""bioRxiv / medRxiv preprint search via EuropePMC.

bioRxiv's own REST API at ``api.biorxiv.org`` doesn't expose a public
keyword-search endpoint (only date-range listings and DOI lookups).
EuropePMC indexes bioRxiv + medRxiv preprints alongside published
papers and exposes a free, keyless search API with full text queries,
so we use it as the preprint backend.

Filter ``SRC:PPR`` restricts results to preprints.
"""

from __future__ import annotations

from . import RateLimitError

_EPMC = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"


def search(query: str, max_results: int = 5) -> str:
    """Return a formatted plaintext block of preprint hits for ``query``."""
    try:
        import requests
    except ImportError:
        return "[biorxiv unavailable: install `requests`]"

    try:
        r = requests.get(
            _EPMC,
            params={
                "query": f"({query}) AND SRC:PPR",
                "format": "json",
                "pageSize": str(max_results),
                "resultType": "core",
            },
            timeout=20,
        )
    except Exception as exc:  # noqa: BLE001
        return f"[biorxiv unavailable: {exc}]"

    if r.status_code == 429:
        raise RateLimitError("europepmc HTTP 429")
    try:
        r.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        return f"[biorxiv HTTP error: {exc}]"

    results = (r.json().get("resultList") or {}).get("result") or []
    if not results:
        return "No bioRxiv/medRxiv results."

    out: list[str] = []
    for p in results:
        title = p.get("title") or "(untitled)"
        year = (p.get("firstPublicationDate") or "")[:4]
        first_author = p.get("authorString") or ""
        doi = p.get("doi") or ""
        # EuropePMC stores the preprint server name (BIORXIV / MEDRXIV)
        # under bookOrReportDetails / source; surface whichever is set.
        source = p.get("source") or "PPR"
        abstract = (p.get("abstractText") or "")[:300]
        link = f"https://doi.org/{doi}" if doi else ""
        out.append(
            f"- {title} ({year}, {source})\n"
            f"  {first_author}\n"
            f"  {abstract}"
            + (f"\n  {link}" if link else "")
        )
    return "\n".join(out)
