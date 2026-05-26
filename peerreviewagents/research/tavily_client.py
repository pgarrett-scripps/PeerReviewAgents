"""Reliable Tavily client for the research layer.

Wraps `tavily-python` with the behaviors the agent layer needs:

  * Retries on transient failures (network errors, 429s, 5xx) with
    exponential backoff. Permanent failures (bad key, bad request, quota
    exhausted) short-circuit immediately.
  * A thread-safe TTL cache so the parallel reviewer fan-out doesn't
    pay multiple credits for the same query within a run.
  * Per-request timeout via the SDK's `timeout` kwarg.
  * Markdown-shaped string output, which is what the LangChain `@tool`
    layer expects to hand back to the LLM.
  * No exceptions escape the public methods: failures are returned to
    the agent as a short bracketed message so a single search hiccup
    never sinks an entire review run.

The client itself is constructed once per process (per config fingerprint)
via `get_tavily_client` in tools.py; the LangChain tool wrappers just call
its `search` / `extract` methods.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# Tavily SDK exception class names that indicate a permanent failure —
# retrying won't help. We match by name to avoid a hard import dependency
# on the SDK at module load time (so test environments without the SDK
# still import this file cleanly).
_PERMANENT_EXC_NAMES = frozenset(
    {
        "MissingAPIKeyError",
        "InvalidAPIKeyError",
        "UsageLimitExceededError",
        "BadRequestError",
        "ForbiddenError",
    }
)

# Cap on raw_content per extracted URL: keeps the reviewer's context
# window manageable while still giving it enough text to verify a claim.
_EXTRACT_CHAR_CAP = 12_000

# Cap on snippet length per search hit. Tavily's `content` field is
# already a short snippet (~300 chars), but cap defensively.
_SNIPPET_CHAR_CAP = 400


@dataclass(frozen=True)
class TavilyConfig:
    """Validated, hashable view of the Tavily-related config knobs."""

    search_depth: str = "advanced"           # "basic" | "advanced"
    max_results: int = 5
    topic: str = "general"                   # "general" | "news"
    include_domains: tuple[str, ...] = ()
    exclude_domains: tuple[str, ...] = ()
    timeout: float = 30.0                    # seconds, per HTTP call
    max_retries: int = 3                     # total attempts = max_retries
    backoff_base: float = 0.75               # seconds; doubles each retry
    backoff_cap: float = 8.0                 # max sleep between retries
    cache_ttl: float = 3600.0                # seconds; 0 disables cache
    cache_maxsize: int = 256

    @classmethod
    def from_dict(cls, cfg: dict) -> "TavilyConfig":
        return cls(
            search_depth=str(cfg.get("tavily_search_depth", "advanced")).lower(),
            max_results=int(cfg.get("tavily_max_results", 5)),
            topic=str(cfg.get("tavily_topic", "general")).lower(),
            include_domains=tuple(cfg.get("tavily_include_domains") or ()),
            exclude_domains=tuple(cfg.get("tavily_exclude_domains") or ()),
            timeout=float(cfg.get("tavily_timeout", 30.0)),
            max_retries=int(cfg.get("tavily_max_retries", 3)),
            backoff_base=float(cfg.get("tavily_backoff_base", 0.75)),
            backoff_cap=float(cfg.get("tavily_backoff_cap", 8.0)),
            cache_ttl=float(cfg.get("tavily_cache_ttl", 3600.0)),
            cache_maxsize=int(cfg.get("tavily_cache_maxsize", 256)),
        )


@dataclass
class _CacheEntry:
    value: str
    expires_at: float


class _TTLCache:
    """Tiny thread-safe LRU+TTL cache. Stdlib only."""

    def __init__(self, maxsize: int, ttl: float) -> None:
        self._maxsize = max(0, maxsize)
        self._ttl = max(0.0, ttl)
        self._data: dict[Any, _CacheEntry] = {}
        self._lock = threading.Lock()

    def get(self, key: Any) -> str | None:
        if self._maxsize == 0 or self._ttl == 0:
            return None
        now = time.monotonic()
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return None
            if entry.expires_at < now:
                self._data.pop(key, None)
                return None
            # Refresh LRU position.
            self._data.pop(key)
            self._data[key] = entry
            return entry.value

    def set(self, key: Any, value: str) -> None:
        if self._maxsize == 0 or self._ttl == 0:
            return
        expires_at = time.monotonic() + self._ttl
        with self._lock:
            if key in self._data:
                self._data.pop(key)
            self._data[key] = _CacheEntry(value=value, expires_at=expires_at)
            while len(self._data) > self._maxsize:
                # Pop oldest (first-inserted) item.
                self._data.pop(next(iter(self._data)))

    def clear(self) -> None:
        with self._lock:
            self._data.clear()


def _is_permanent(exc: BaseException) -> bool:
    return type(exc).__name__ in _PERMANENT_EXC_NAMES


class TavilyResearchClient:
    """Sync Tavily client wrapper with retry + cache + timeout.

    Methods return LLM-ready markdown strings. They do not raise.
    """

    def __init__(self, api_key: str, config: TavilyConfig) -> None:
        if not api_key:
            raise ValueError("TavilyResearchClient requires a non-empty api_key")
        self._api_key = api_key
        self._config = config
        self._cache = _TTLCache(maxsize=config.cache_maxsize, ttl=config.cache_ttl)
        self._sdk_client: Any | None = None
        self._sdk_init_lock = threading.Lock()
        # Track whether we've already warned about a permanent failure so
        # we don't spam logs across the parallel reviewer pass.
        self._disabled_reason: str | None = None
        self._disabled_lock = threading.Lock()

    # --- public API ---------------------------------------------------------

    def search(self, query: str) -> str:
        query = (query or "").strip()
        if not query:
            return "[tavily_search: empty query]"

        cfg = self._config
        cache_key = (
            "search",
            query,
            cfg.search_depth,
            cfg.max_results,
            cfg.topic,
            cfg.include_domains,
            cfg.exclude_domains,
        )
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        disabled = self._disabled()
        if disabled is not None:
            return disabled

        params: dict[str, Any] = {
            "query": query,
            "search_depth": cfg.search_depth,
            "max_results": cfg.max_results,
            "topic": cfg.topic,
            "include_answer": False,
            "include_raw_content": False,
            "include_images": False,
        }
        if cfg.include_domains:
            params["include_domains"] = list(cfg.include_domains)
        if cfg.exclude_domains:
            params["exclude_domains"] = list(cfg.exclude_domains)

        try:
            raw = self._call_with_retry("search", params)
        except _PermanentFailure as exc:
            return self._mark_disabled(f"[tavily_search unavailable: {exc}]")
        except _ExhaustedRetries as exc:
            return f"[tavily_search transient failure after retries: {exc}]"

        formatted = self._format_search(raw)
        self._cache.set(cache_key, formatted)
        return formatted

    def extract(self, url: str) -> str:
        url = (url or "").strip()
        if not url:
            return "[tavily_extract: empty url]"
        if not (url.startswith("http://") or url.startswith("https://")):
            return f"[tavily_extract: refusing non-http URL: {url}]"

        cache_key = ("extract", url)
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        disabled = self._disabled()
        if disabled is not None:
            return disabled

        params: dict[str, Any] = {
            "urls": [url],
            "extract_depth": "advanced",
            "include_images": False,
        }
        try:
            raw = self._call_with_retry("extract", params)
        except _PermanentFailure as exc:
            return self._mark_disabled(f"[tavily_extract unavailable: {exc}]")
        except _ExhaustedRetries as exc:
            return f"[tavily_extract transient failure after retries: {exc}]"

        formatted = self._format_extract(raw, url)
        self._cache.set(cache_key, formatted)
        return formatted

    # --- internals ----------------------------------------------------------

    def _client(self) -> Any:
        """Lazily construct the underlying SDK client (one per instance)."""
        if self._sdk_client is not None:
            return self._sdk_client
        with self._sdk_init_lock:
            if self._sdk_client is not None:
                return self._sdk_client
            from tavily import TavilyClient  # type: ignore

            self._sdk_client = TavilyClient(api_key=self._api_key)
            return self._sdk_client

    def _call_with_retry(self, method: str, params: dict) -> dict:
        cfg = self._config
        attempts = max(1, cfg.max_retries)
        last_exc: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                client = self._client()
                fn = getattr(client, method)
                # Tavily's SDK accepts `timeout` on both .search and .extract.
                return fn(timeout=cfg.timeout, **params)
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if _is_permanent(exc):
                    logger.warning(
                        "tavily.%s permanent failure: %s: %s",
                        method, type(exc).__name__, exc,
                    )
                    raise _PermanentFailure(str(exc)) from exc
                if attempt >= attempts:
                    logger.warning(
                        "tavily.%s exhausted %d retries: %s: %s",
                        method, attempts, type(exc).__name__, exc,
                    )
                    break
                sleep_for = min(
                    cfg.backoff_cap,
                    cfg.backoff_base * (2 ** (attempt - 1)),
                )
                logger.info(
                    "tavily.%s transient failure (attempt %d/%d), sleeping %.2fs: %s",
                    method, attempt, attempts, sleep_for, exc,
                )
                time.sleep(sleep_for)
        assert last_exc is not None
        raise _ExhaustedRetries(str(last_exc)) from last_exc

    def _disabled(self) -> str | None:
        with self._disabled_lock:
            return self._disabled_reason

    def _mark_disabled(self, message: str) -> str:
        with self._disabled_lock:
            if self._disabled_reason is None:
                self._disabled_reason = message
        return message

    @staticmethod
    def _format_search(raw: dict) -> str:
        hits = raw.get("results") or []
        if not hits:
            return "No web results."
        lines: list[str] = []
        for h in hits:
            title = (h.get("title") or "(untitled)").strip()
            content = (h.get("content") or "").strip().replace("\n", " ")
            content = content[:_SNIPPET_CHAR_CAP]
            url = h.get("url") or ""
            score = h.get("score")
            score_tag = f"[{score:.2f}] " if isinstance(score, (int, float)) else ""
            lines.append(f"- {score_tag}{title}\n  {content}\n  {url}")
        return "\n".join(lines)

    @staticmethod
    def _format_extract(raw: dict, url: str) -> str:
        results = raw.get("results") or []
        failed = raw.get("failed_results") or []
        if not results:
            reason = ""
            if failed:
                first = failed[0] if isinstance(failed[0], dict) else {}
                err = first.get("error") or first.get("reason") or failed[0]
                reason = f": {err}"
            return f"[tavily_extract: no content for {url}{reason}]"
        item = results[0]
        body = (item.get("raw_content") or "").strip()
        if not body:
            return f"[tavily_extract: empty content for {url}]"
        if len(body) > _EXTRACT_CHAR_CAP:
            body = body[:_EXTRACT_CHAR_CAP] + "\n\n[...extract truncated...]"
        return f"Source: {item.get('url') or url}\n\n{body}"


class _PermanentFailure(Exception):
    """Raised internally when retrying would never succeed."""


class _ExhaustedRetries(Exception):
    """Raised internally when transient retries are exhausted."""


# ---------------------------------------------------------------------------
# Process-wide client cache so the parallel reviewer pass + integrity panel
# share one underlying client (and therefore one TTL cache) per config.
# ---------------------------------------------------------------------------

_client_registry: dict[tuple, TavilyResearchClient] = {}
_client_registry_lock = threading.Lock()
_missing_key_warned = False
_missing_key_lock = threading.Lock()


def get_tavily_client(config: dict, api_key: str | None) -> TavilyResearchClient | None:
    """Return a shared TavilyResearchClient, or None if no API key is set.

    Multiple calls with the same (key, config fingerprint) return the same
    instance — important so all reviewers share the cache. A missing API
    key logs a single warning per process and then stays silent.
    """
    global _missing_key_warned
    if not api_key:
        with _missing_key_lock:
            if not _missing_key_warned:
                logger.warning(
                    "TAVILY_API_KEY is not set; tavily research tools will be omitted."
                )
                _missing_key_warned = True
        return None

    tcfg = TavilyConfig.from_dict(config)
    key = (api_key, tcfg)
    with _client_registry_lock:
        client = _client_registry.get(key)
        if client is None:
            client = TavilyResearchClient(api_key=api_key, config=tcfg)
            _client_registry[key] = client
        return client


def _reset_client_registry_for_tests() -> None:
    """Test hook: drop the cached clients and the missing-key flag."""
    global _missing_key_warned
    with _client_registry_lock:
        _client_registry.clear()
    with _missing_key_lock:
        _missing_key_warned = False
