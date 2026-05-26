"""Tests for the Tavily research client and tool registration.

These tests do not hit the network. The Tavily SDK is patched at the
seam (the lazy `_client()` accessor) so we can simulate every behavior:
success, transient failure with retry, permanent failure, cache hit,
empty result, malformed response.
"""

from __future__ import annotations

import threading
import time

import pytest

from peerreviewagents.research import tavily_client as tc
from peerreviewagents.research.tavily_client import (
    TavilyConfig,
    TavilyResearchClient,
    _PermanentFailure,
    _ExhaustedRetries,
    _TTLCache,
    get_tavily_client,
)
from peerreviewagents.research.tools import get_research_tools


# --- Fake SDK ---------------------------------------------------------------


class _FakeTavilySDK:
    """Stand-in for tavily.TavilyClient. Records calls; returns canned responses."""

    def __init__(self, *, search_responses=None, extract_responses=None, search_raises=None, extract_raises=None) -> None:
        # Each can be either a list (consumed in order) or a single value used forever.
        self._search_responses = list(search_responses) if isinstance(search_responses, list) else search_responses
        self._extract_responses = list(extract_responses) if isinstance(extract_responses, list) else extract_responses
        self._search_raises = list(search_raises) if isinstance(search_raises, list) else search_raises
        self._extract_raises = list(extract_raises) if isinstance(extract_raises, list) else extract_raises
        self.search_calls: list[dict] = []
        self.extract_calls: list[dict] = []

    def _maybe_raise(self, slot: str):
        attr = f"_{slot}_raises"
        val = getattr(self, attr)
        if val is None:
            return
        if isinstance(val, list):
            if val:
                exc = val.pop(0)
                if exc is not None:
                    raise exc
        else:
            raise val

    def _pop(self, slot: str, default: dict) -> dict:
        attr = f"_{slot}_responses"
        val = getattr(self, attr)
        if val is None:
            return default
        if isinstance(val, list):
            if val:
                return val.pop(0)
            return default
        return val

    def search(self, **params):
        self.search_calls.append(params)
        self._maybe_raise("search")
        return self._pop("search", {"results": []})

    def extract(self, **params):
        self.extract_calls.append(params)
        self._maybe_raise("extract")
        return self._pop("extract", {"results": [], "failed_results": []})


def _attach_fake(client: TavilyResearchClient, fake: _FakeTavilySDK) -> None:
    """Wire a fake SDK into a TavilyResearchClient, bypassing the lazy import."""
    client._sdk_client = fake


def _client(monkeypatch=None, **cfg_overrides) -> tuple[TavilyResearchClient, _FakeTavilySDK]:
    fake = _FakeTavilySDK()
    cfg = TavilyConfig(
        search_depth="advanced",
        max_results=3,
        topic="general",
        include_domains=(),
        exclude_domains=(),
        timeout=10.0,
        max_retries=3,
        backoff_base=0.0,         # zero sleeps so retry tests run fast
        backoff_cap=0.0,
        cache_ttl=3600.0,
        cache_maxsize=128,
    )
    # Apply per-test overrides via dataclass replace
    from dataclasses import replace
    cfg = replace(cfg, **cfg_overrides)
    client = TavilyResearchClient(api_key="test-key", config=cfg)
    _attach_fake(client, fake)
    return client, fake


# --- Permanent-error stand-ins --------------------------------------------
# We match by class NAME, not identity, so we can fake the SDK exceptions.


class InvalidAPIKeyError(Exception):
    pass


class BadRequestError(Exception):
    pass


class UsageLimitExceededError(Exception):
    pass


# --- Tests ------------------------------------------------------------------


def test_search_happy_path_formats_results():
    client, fake = _client()
    fake._search_responses = {
        "results": [
            {"title": "Attention is all you need", "url": "https://arxiv.org/abs/1706.03762",
             "content": "Transformer architecture.", "score": 0.94},
            {"title": "BERT", "url": "https://arxiv.org/abs/1810.04805",
             "content": "Bidirectional encoders.", "score": 0.82},
        ]
    }
    out = client.search("transformers")
    assert "[0.94] Attention is all you need" in out
    assert "https://arxiv.org/abs/1706.03762" in out
    assert "Transformer architecture." in out
    assert "[0.82] BERT" in out
    assert len(fake.search_calls) == 1
    assert fake.search_calls[0]["search_depth"] == "advanced"
    assert fake.search_calls[0]["max_results"] == 3
    assert fake.search_calls[0]["include_answer"] is False
    assert fake.search_calls[0]["include_raw_content"] is False
    assert fake.search_calls[0]["timeout"] == 10.0


def test_search_empty_results():
    client, fake = _client()
    fake._search_responses = {"results": []}
    assert client.search("nothing matches this") == "No web results."


def test_search_empty_query_short_circuits():
    client, fake = _client()
    out = client.search("   ")
    assert "empty query" in out
    assert fake.search_calls == []


def test_search_caches_repeat_queries():
    client, fake = _client()
    fake._search_responses = [
        {"results": [{"title": "T", "url": "u", "content": "c", "score": 0.5}]},
    ]
    first = client.search("same query")
    second = client.search("same query")
    assert first == second
    assert len(fake.search_calls) == 1, "cache hit should skip the SDK call"


def test_search_cache_disabled_when_ttl_zero():
    client, fake = _client(cache_ttl=0.0)
    fake._search_responses = [
        {"results": [{"title": "A", "url": "u", "content": "c"}]},
        {"results": [{"title": "B", "url": "u", "content": "c"}]},
    ]
    client.search("q")
    client.search("q")
    assert len(fake.search_calls) == 2


def test_search_retries_on_transient_failure():
    client, fake = _client(max_retries=3)
    # First two attempts blow up with a generic transient error; third succeeds.
    fake._search_raises = [ConnectionError("flaky"), TimeoutError("slow"), None]
    # Only the third call reaches _pop (the first two raise before returning).
    fake._search_responses = [
        {"results": [{"title": "ok", "url": "u", "content": "c", "score": 0.9}]},
    ]
    out = client.search("flaky query")
    assert "ok" in out
    assert len(fake.search_calls) == 3


def test_search_gives_up_after_max_retries():
    client, fake = _client(max_retries=2)
    fake._search_raises = [ConnectionError("nope"), ConnectionError("still nope")]
    out = client.search("dead service")
    assert "transient failure after retries" in out
    assert len(fake.search_calls) == 2


def test_search_does_not_retry_on_permanent_error():
    client, fake = _client(max_retries=5)
    fake._search_raises = InvalidAPIKeyError("bad key")
    out = client.search("any")
    assert "tavily_search unavailable" in out
    assert "InvalidAPIKeyError" not in out  # we only include the message, not class
    assert "bad key" in out
    assert len(fake.search_calls) == 1, "permanent error must not retry"


def test_search_disables_client_after_permanent_error():
    """Once we know the key is bad / quota gone, stop hitting the API for the
    rest of the run — subsequent calls return the cached failure message."""
    client, fake = _client()
    fake._search_raises = [UsageLimitExceededError("out of credits")]
    first = client.search("a")
    second = client.search("b")
    assert first == second
    assert "unavailable" in second
    assert len(fake.search_calls) == 1


def test_extract_rejects_non_http_url():
    client, fake = _client()
    out = client.extract("file:///etc/passwd")
    assert "non-http URL" in out
    assert fake.extract_calls == []


def test_extract_happy_path_truncates_long_content():
    client, fake = _client()
    body = "lorem ipsum " * 5000
    fake._extract_responses = {
        "results": [{"url": "https://example.com/x", "raw_content": body}],
        "failed_results": [],
    }
    out = client.extract("https://example.com/x")
    assert out.startswith("Source: https://example.com/x")
    assert "[...extract truncated...]" in out
    assert len(out) < len(body) + 200


def test_extract_reports_failure_details():
    client, fake = _client()
    fake._extract_responses = {
        "results": [],
        "failed_results": [{"url": "https://example.com/x", "error": "404 Not Found"}],
    }
    out = client.extract("https://example.com/x")
    assert "404 Not Found" in out


def test_extract_caches():
    client, fake = _client()
    fake._extract_responses = [
        {"results": [{"url": "u", "raw_content": "body"}], "failed_results": []}
    ]
    a = client.extract("https://example.com")
    b = client.extract("https://example.com")
    assert a == b
    assert len(fake.extract_calls) == 1


# --- _TTLCache --------------------------------------------------------------


def test_ttl_cache_expires():
    cache = _TTLCache(maxsize=8, ttl=0.05)
    cache.set("k", "v")
    assert cache.get("k") == "v"
    time.sleep(0.08)
    assert cache.get("k") is None


def test_ttl_cache_evicts_oldest_at_capacity():
    cache = _TTLCache(maxsize=2, ttl=60.0)
    cache.set("a", "1")
    cache.set("b", "2")
    cache.set("c", "3")
    assert cache.get("a") is None
    assert cache.get("b") == "2"
    assert cache.get("c") == "3"


def test_ttl_cache_disabled_when_maxsize_zero():
    cache = _TTLCache(maxsize=0, ttl=60.0)
    cache.set("k", "v")
    assert cache.get("k") is None


def test_ttl_cache_concurrent_access():
    """Hammer the cache from multiple threads; assert no errors and no
    data corruption (every set value is later readable until evicted)."""
    cache = _TTLCache(maxsize=1024, ttl=60.0)
    errors: list[Exception] = []

    def worker(start: int) -> None:
        try:
            for i in range(start, start + 100):
                cache.set(f"k{i}", f"v{i}")
                got = cache.get(f"k{i}")
                assert got == f"v{i}"
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n * 100,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []


# --- get_tavily_client + tool registration ---------------------------------


def test_get_tavily_client_returns_none_without_key(monkeypatch):
    tc._reset_client_registry_for_tests()
    client = get_tavily_client({"tavily_search_depth": "basic"}, api_key=None)
    assert client is None


def test_get_tavily_client_shares_instance_per_config(monkeypatch):
    tc._reset_client_registry_for_tests()
    cfg = {"tavily_max_results": 5}
    a = get_tavily_client(cfg, api_key="k1")
    b = get_tavily_client(cfg, api_key="k1")
    assert a is b, "same key + same config must yield the same client (shared cache)"

    c = get_tavily_client({"tavily_max_results": 10}, api_key="k1")
    assert c is not a, "different config fingerprint must yield a new client"


def test_get_research_tools_omits_tavily_without_key(monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    tc._reset_client_registry_for_tests()
    cfg = {"research_enabled": True, "research_tools": ["tavily", "arxiv", "scholar"]}
    tools = get_research_tools(cfg)
    names = [t.name for t in tools]
    assert "tavily_search" not in names
    assert "tavily_extract" not in names
    assert "arxiv_search" in names
    assert "semantic_scholar_search" in names


def test_get_research_tools_includes_tavily_with_key(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "test-key")
    tc._reset_client_registry_for_tests()
    cfg = {"research_enabled": True, "research_tools": ["tavily"]}
    tools = get_research_tools(cfg)
    names = [t.name for t in tools]
    assert "tavily_search" in names
    assert "tavily_extract" in names, "tavily implies tavily_extract too"


def test_get_research_tools_accepts_legacy_web_alias(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "test-key")
    tc._reset_client_registry_for_tests()
    cfg = {"research_enabled": True, "research_tools": ["web", "arxiv"]}
    tools = get_research_tools(cfg)
    names = [t.name for t in tools]
    assert "tavily_search" in names, "'web' must map to tavily"
    assert "arxiv_search" in names


def test_get_research_tools_empty_when_research_disabled(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "test-key")
    tc._reset_client_registry_for_tests()
    cfg = {"research_enabled": False, "research_tools": ["tavily", "arxiv"]}
    assert get_research_tools(cfg) == []


def test_tavily_tools_invoke_through_shared_client(monkeypatch):
    """End-to-end: registering tools and invoking them routes through one
    shared TavilyResearchClient (proves the LangChain-tool wiring works)."""
    monkeypatch.setenv("TAVILY_API_KEY", "test-key")
    tc._reset_client_registry_for_tests()

    cfg = {"research_enabled": True, "research_tools": ["tavily"]}
    # Get the shared client first; attach a fake SDK to it.
    client = get_tavily_client(cfg, "test-key")
    fake = _FakeTavilySDK()
    fake._search_responses = {
        "results": [{"title": "Hit", "url": "https://x.example", "content": "snippet", "score": 0.7}]
    }
    fake._extract_responses = {
        "results": [{"url": "https://x.example", "raw_content": "full body text"}],
        "failed_results": [],
    }
    _attach_fake(client, fake)

    tools = get_research_tools(cfg)
    by_name = {t.name: t for t in tools}
    search_out = by_name["tavily_search"].invoke({"query": "hit"})
    extract_out = by_name["tavily_extract"].invoke({"url": "https://x.example"})

    assert "Hit" in search_out and "https://x.example" in search_out
    assert "full body text" in extract_out
    assert len(fake.search_calls) == 1
    assert len(fake.extract_calls) == 1


# --- TavilyConfig parsing ---------------------------------------------------


def test_tavily_config_from_dict_uses_defaults():
    cfg = TavilyConfig.from_dict({})
    assert cfg.search_depth == "advanced"
    assert cfg.max_results == 5
    assert cfg.include_domains == ()


def test_tavily_config_from_dict_normalizes_case():
    cfg = TavilyConfig.from_dict({"tavily_search_depth": "BASIC", "tavily_topic": "NEWS"})
    assert cfg.search_depth == "basic"
    assert cfg.topic == "news"


def test_tavily_config_is_hashable_for_client_registry():
    """The registry uses the config as part of its dict key — confirm
    TavilyConfig instances hash equal when their fields match."""
    a = TavilyConfig.from_dict({"tavily_max_results": 5})
    b = TavilyConfig.from_dict({"tavily_max_results": 5})
    c = TavilyConfig.from_dict({"tavily_max_results": 10})
    assert hash(a) == hash(b)
    assert a == b
    assert a != c


# --- Permanent error detection ----------------------------------------------


def test_is_permanent_matches_known_sdk_exception_names():
    from peerreviewagents.research.tavily_client import _is_permanent

    class MissingAPIKeyError(Exception):
        pass

    class SomethingElse(Exception):
        pass

    assert _is_permanent(MissingAPIKeyError("x"))
    assert _is_permanent(InvalidAPIKeyError("x"))
    assert _is_permanent(UsageLimitExceededError("x"))
    assert _is_permanent(BadRequestError("x"))
    assert not _is_permanent(SomethingElse("x"))
    assert not _is_permanent(ConnectionError("x"))
    assert not _is_permanent(TimeoutError("x"))
