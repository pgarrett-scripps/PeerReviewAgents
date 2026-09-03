"""Tests for the research vendor router and tool registry."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from peerreviewagents.research import (
    RateLimitError,
    ResearchUnavailableError,
    VendorUnavailableError,
    interface,
)
from peerreviewagents.research import tools as research_tools

# --- resolve_vendors --------------------------------------------------------


def test_resolve_vendors_default_primary_first():
    chain = interface.resolve_vendors("find_related_work", config=None)
    assert chain[0] == "semantic_scholar"
    assert "arxiv" in chain


def test_resolve_vendors_config_override_wins():
    chain = interface.resolve_vendors(
        "find_related_work",
        config={"data_vendors": {"paper_search": "arxiv,semantic_scholar"}},
    )
    assert chain[0] == "arxiv"
    assert chain[1] == "semantic_scholar"


def test_resolve_vendors_tool_level_override_wins_over_category():
    chain = interface.resolve_vendors(
        "find_related_work",
        config={
            "data_vendors": {"paper_search": "semantic_scholar,arxiv"},
            "tool_vendors": {"find_related_work": "arxiv"},
        },
    )
    assert chain[0] == "arxiv"


def test_resolve_vendors_unknown_vendor_dropped_but_others_kept():
    chain = interface.resolve_vendors(
        "find_related_work",
        config={"data_vendors": {"paper_search": "made_up,semantic_scholar"}},
    )
    assert "made_up" not in chain
    assert "semantic_scholar" in chain


def test_unknown_method_raises():
    with pytest.raises(ValueError, match="unknown research method"):
        interface.category_for("does_not_exist")


# --- route() fallback semantics --------------------------------------------


def test_route_rate_limit_falls_through(monkeypatch):
    def boom(**_):
        raise RateLimitError("primary RL")

    def ok(**_):
        return "OK from fallback"

    monkeypatch.setitem(interface._VENDOR_IMPL["find_related_work"], "semantic_scholar", boom)
    monkeypatch.setitem(interface._VENDOR_IMPL["find_related_work"], "arxiv", ok)
    out = interface.route("find_related_work", config=None, query="x", max_results=3)
    assert out == "OK from fallback"


def test_route_outage_falls_through_like_a_rate_limit(monkeypatch):
    """A primary that is down (connect error / timeout / 5xx) must not be
    recorded as a served answer — the healthy fallback vendor answers."""
    def down(**_):
        raise VendorUnavailableError("connection refused")

    def ok(**_):
        return "OK from fallback"

    monkeypatch.setitem(interface._VENDOR_IMPL["find_related_work"], "semantic_scholar", down)
    monkeypatch.setitem(interface._VENDOR_IMPL["find_related_work"], "arxiv", ok)
    out = interface.route("find_related_work", config=None, query="x", max_results=3)
    assert out == "OK from fallback"
    assert interface.last_vendor() == "arxiv"


def test_route_all_rate_limited_raises_with_the_reason(monkeypatch):
    """Exhausting the chain surfaces the rate limiting — as an exception, so
    the tool loop records an error instead of a clean zero-hit search."""
    def boom(**_):
        raise RateLimitError("RL")

    monkeypatch.setitem(interface._VENDOR_IMPL["find_related_work"], "semantic_scholar", boom)
    monkeypatch.setitem(interface._VENDOR_IMPL["find_related_work"], "arxiv", boom)
    with pytest.raises(ResearchUnavailableError, match="rate-limited"):
        interface.route("find_related_work", config=None, query="x", max_results=3)


def test_route_all_vendors_down_raises_and_credits_no_vendor(monkeypatch):
    """An outage across the chain is a failure, never 'searched clean, zero
    hits' — and no vendor gets credited with having served it."""
    def down(**_):
        raise VendorUnavailableError("HTTP 503")

    monkeypatch.setitem(interface._VENDOR_IMPL["find_related_work"], "semantic_scholar", down)
    monkeypatch.setitem(interface._VENDOR_IMPL["find_related_work"], "arxiv", down)
    with pytest.raises(ResearchUnavailableError, match="every configured vendor failed"):
        interface.route("find_related_work", config=None, query="x", max_results=3)
    assert interface.last_vendor() == ""


def test_route_non_recoverable_error_surfaces(monkeypatch):
    """Only RateLimitError / VendorUnavailableError trigger fallback; an
    unexpected exception is a bug and propagates unchanged."""
    def explode(**_):
        raise RuntimeError("unexpected")

    monkeypatch.setitem(interface._VENDOR_IMPL["find_related_work"], "semantic_scholar", explode)
    with pytest.raises(RuntimeError, match="unexpected"):
        interface.route("find_related_work", config=None, query="x", max_results=3)


def test_route_4xx_degrade_text_is_a_served_answer(monkeypatch):
    """A non-429 4xx means the API judged the query; that verdict is returned
    as text (credited to the vendor) rather than shopped to the fallback."""
    def judged(**_):
        return "[semantic_scholar HTTP error: 400 Bad Request]"

    def never(**_):  # pragma: no cover - reaching this is the failure
        raise AssertionError("fallback must not run for a judged query")

    monkeypatch.setitem(interface._VENDOR_IMPL["find_related_work"], "semantic_scholar", judged)
    monkeypatch.setitem(interface._VENDOR_IMPL["find_related_work"], "arxiv", never)
    out = interface.route("find_related_work", config=None, query="x", max_results=3)
    assert "400" in out
    assert interface.last_vendor() == "semantic_scholar"


# --- tool registry ---------------------------------------------------------


def test_tool_registry_lists_logical_operations():
    names = research_tools.available_tool_names()
    assert "find_related_work" in names
    assert "search_biomedical_literature" in names
    assert "search_preprints" in names


def test_get_tools_by_name_isolates_concurrent_configs(monkeypatch):
    barrier = Barrier(2)

    def routed(_method, config, **_kwargs):
        barrier.wait(timeout=2)
        return config["marker"]

    monkeypatch.setattr(research_tools, "route", routed)
    first = research_tools.get_tools_by_name(
        ["find_related_work"], {"marker": "first"}
    )[0]
    second = research_tools.get_tools_by_name(
        ["find_related_work"], {"marker": "second"}
    )[0]

    with ThreadPoolExecutor(max_workers=2) as pool:
        one = pool.submit(first.invoke, {"query": "topic"})
        two = pool.submit(second.invoke, {"query": "topic"})
    assert one.result() == "first"
    assert two.result() == "second"


def test_get_tools_by_name_rejects_unknown():
    with pytest.raises(ValueError, match="unknown research tool"):
        research_tools.get_tools_by_name(["bogus_tool"], {})


# ---------------------------------------------------------------------------
# The arXiv client's session must carry a timeout.
#
# `arxiv.Client` issues `self._session.get(url, headers=...)` with none, and
# `requests` defaults to None — blocking forever. A hung lookup left a worker
# future pending, and LangGraph's teardown waits on pending futures without a
# timeout of its own, so a finished review sat with one open socket until it
# was killed. The constructor exposes no timeout, so this is enforced here.
# ---------------------------------------------------------------------------


def test_the_arxiv_session_gets_a_default_timeout():
    from peerreviewagents.research.arxiv import _TIMEOUT_S, _bounded

    seen = {}

    class _Session:
        def request(self, method, url, **kwargs):
            seen.update(kwargs)
            return "ok"

        def get(self, url, **kwargs):
            # How the arxiv client actually calls it.
            return self.request("get", url, **kwargs)

    session = _bounded(_Session())
    session.get("https://export.arxiv.org/api/query", headers={"user-agent": "x"})
    assert seen["timeout"] == _TIMEOUT_S


def test_an_explicit_timeout_is_left_alone():
    from peerreviewagents.research.arxiv import _bounded

    seen = {}

    class _Session:
        def request(self, method, url, **kwargs):
            seen.update(kwargs)
            return "ok"

    session = _bounded(_Session())
    session.request("get", "https://example.invalid", timeout=3)
    assert seen["timeout"] == 3


# ---------------------------------------------------------------------------
# A round's lookups run at the same time.
#
# They were sequential, which was most of what a researched review cost in
# wall-clock: a citation audit spent 753s and a literature reviewer 3764s on
# one manuscript, against non-searching reviewers finishing in 30-90s. The
# model names every call in a round before seeing any result, so they are
# independent by construction.
# ---------------------------------------------------------------------------


def test_a_round_of_lookups_runs_concurrently():
    import time

    from peerreviewagents.agents.utils.agent_utils import _run_round

    class _Slow:
        def invoke(self, args):
            time.sleep(0.4)
            return f"hit {args['q']}"

    calls = [{"id": f"c{i}", "name": "s", "args": {"q": i}} for i in range(6)]
    started = time.monotonic()
    out = _run_round(calls, {"s": _Slow()})
    elapsed = time.monotonic() - started

    # Sequential would be 2.4s. Generous bound: the point is that it is not
    # the sum, not that it hits any particular speedup on a loaded machine.
    assert elapsed < 1.6, f"lookups still serialised ({elapsed:.2f}s)"
    assert [r for r, _ in out] == [f"hit {i}" for i in range(6)]


def test_call_order_survives_uneven_lookup_times():
    """Results are zipped back against `calls` to build ToolMessages, and a
    tool_result carrying the wrong tool_call_id is a 400 from every vendor."""
    import time

    from peerreviewagents.agents.utils.agent_utils import _run_round

    class _Uneven:
        def invoke(self, args):
            time.sleep(0.3 if args["q"] == 0 else 0.01)
            return f"hit {args['q']}"

    calls = [{"id": f"c{i}", "name": "s", "args": {"q": i}} for i in range(4)]
    out = _run_round(calls, {"s": _Uneven()})
    assert [r for r, _ in out] == [f"hit {i}" for i in range(4)]


def test_one_failing_vendor_does_not_take_down_the_round():
    from peerreviewagents.agents.utils.agent_utils import _run_round

    class _Ok:
        def invoke(self, args):
            return "fine"

    class _Boom:
        def invoke(self, args):
            raise RuntimeError("vendor down")

    calls = [
        {"id": "a", "name": "ok", "args": {}},
        {"id": "b", "name": "boom", "args": {}},
        {"id": "c", "name": "missing", "args": {}},
    ]
    out = _run_round(calls, {"ok": _Ok(), "boom": _Boom()})
    assert out[0] == ("fine", "")
    assert out[1][0].startswith("[tool error") and "RuntimeError" in out[1][1]
    assert out[2][1] == "unknown tool"
