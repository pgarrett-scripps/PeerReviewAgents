"""Tests for the research vendor router and tool registry."""

from __future__ import annotations

import pytest

from peerreviewagents.research import RateLimitError, interface
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


def test_route_all_rate_limited_returns_message(monkeypatch):
    def boom(**_):
        raise RateLimitError("RL")

    monkeypatch.setitem(interface._VENDOR_IMPL["find_related_work"], "semantic_scholar", boom)
    monkeypatch.setitem(interface._VENDOR_IMPL["find_related_work"], "arxiv", boom)
    out = interface.route("find_related_work", config=None, query="x", max_results=3)
    assert "all configured vendors rate-limited" in out


def test_route_non_rate_limit_surfaces(monkeypatch):
    """Non-RateLimitError must NOT trigger fallback — the vendor is
    expected to swallow other errors and return graceful-degrade text.
    If it raises something else, that propagates."""
    def explode(**_):
        raise RuntimeError("unexpected")

    monkeypatch.setitem(interface._VENDOR_IMPL["find_related_work"], "semantic_scholar", explode)
    with pytest.raises(RuntimeError, match="unexpected"):
        interface.route("find_related_work", config=None, query="x", max_results=3)


# --- tool registry ---------------------------------------------------------


def test_tool_registry_lists_logical_operations():
    names = research_tools.available_tool_names()
    assert "find_related_work" in names
    assert "search_biomedical_literature" in names
    assert "search_preprints" in names


def test_get_tools_by_name_returns_bound_tools():
    cfg = {"data_vendors": {"paper_search": "arxiv,semantic_scholar"}}
    tools = research_tools.get_tools_by_name(["find_related_work"], cfg)
    assert len(tools) == 1
    # The shared active-config holder picked up the override.
    assert research_tools._ACTIVE_CONFIG["data_vendors"]["paper_search"] \
        == "arxiv,semantic_scholar"


def test_get_tools_by_name_rejects_unknown():
    with pytest.raises(ValueError, match="unknown research tool"):
        research_tools.get_tools_by_name(["bogus_tool"], {})
