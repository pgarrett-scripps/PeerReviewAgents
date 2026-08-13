"""The surface the In Silico overlay journal actually imports and reads.

In Silico (the `insilico` repo, scripts/run_review.py) drives this pipeline
from GitHub Actions: it builds a config with get_config(**overrides), runs
PeerReviewGraph.review over a downloaded PDF, drains cost and research
telemetry off the observability bus, and publishes fields it reads straight
off the final state and the loader's ingest record. None of that shows up in
our own call graph, so a refactor that renames one of these symbols or keys
passes this repo's suite and breaks every published review downstream —
silently, on an ephemeral runner, days later.

This file pins exactly what that script touches. If a test here fails, the
change is not wrong, but it is a breaking change for the journal: either
keep the old surface or update the consumer in the same breath.

No network, no LLM: everything is checked statically or through the FakeLLM
pipeline harness from test_pipeline.
"""

from __future__ import annotations

import os
from queue import Empty, Queue

from test_pipeline import SAMPLE, _patch_llms

from peerreviewagents.default_config import get_config

HEX64 = set("0123456789abcdef")


# --- desk-screen policy (published per review in REVIEW_SCREENS) -------------


def test_screen_mode_answers_with_a_known_mode():
    from peerreviewagents.agents.editor.desk_screen import screen_mode

    # The consumer records screen_mode(config) verbatim in provenance.json
    # and the site renders it; a fourth value would publish as gibberish.
    assert screen_mode(get_config()) in ("gate", "warm", "off")
    assert screen_mode(get_config(desk_screen=True)) == "gate"
    assert screen_mode(get_config(desk_screen_mode="warm")) == "warm"


# --- ingest surface (restore_prior_draft / _same_draft) -----------------------


def test_cache_key_takes_a_path_and_config_and_returns_a_string():
    from peerreviewagents.ingest.cache import cache_key

    key = cache_key(SAMPLE, get_config())
    assert isinstance(key, str) and key
    # The consumer compares it to a hex string stored in round.json.
    assert set(key) <= HEX64


def test_loader_surface_and_the_ingest_record_keys(tmp_path):
    from peerreviewagents.ingest.loader import (
        ManuscriptUnreadable,
        load_manuscript,
        load_manuscript_record,
    )

    cfg = get_config(cache_dir=str(tmp_path / "cache"))
    title, text, sections = load_manuscript(SAMPLE, cfg)
    assert title and text and isinstance(sections, dict)

    # _same_draft reads exactly these two keys off `.ingest` to decide
    # whether a re-fetched PDF is the draft a published round reviewed.
    record = load_manuscript_record(SAMPLE, cfg)
    assert set(record.ingest["text_sha256"]) <= HEX64
    assert record.ingest["chars"] == len(record.text)

    # Caught by name around graph.review(); it is what separates "fix the
    # file" (exit 3, no bundle) from "the run broke".
    assert issubclass(ManuscriptUnreadable, Exception)


# --- observability bus (the telemetry recorder) -------------------------------


def test_observer_queue_receives_events_with_the_read_fields():
    from peerreviewagents import observability
    from peerreviewagents.observability import (
        AgentEvent,
        clear_observer,
        register_observer,
    )

    queue: Queue = Queue()
    run_id = "downstream-contract"
    register_observer(queue, run_id)
    try:
        observability.emit(AgentEvent(kind="usage", node="editor",
                                      cost_usd=0.01, run_id=run_id))
        observability.emit(AgentEvent(kind="tool", node="reviewer_novelty",
                                      tool_name="find_related_work",
                                      tool_query="widgets", tool_hits=3,
                                      tool_error="", run_id=run_id))
        got = [queue.get_nowait(), queue.get_nowait()]
    finally:
        clear_observer(run_id)

    # The consumer's drain loop dispatches on `kind` and reads these
    # attributes off every event object — attributes, not dict keys.
    usage, tool = got
    assert (usage.kind, usage.node, usage.cost_usd) == ("usage", "editor", 0.01)
    assert (tool.tool_name, tool.tool_query, tool.tool_hits, tool.tool_error) == (
        "find_related_work", "widgets", 3, ""
    )


def test_cleared_observer_stops_receiving():
    from peerreviewagents import observability
    from peerreviewagents.observability import (
        AgentEvent,
        clear_observer,
        register_observer,
    )

    queue: Queue = Queue()
    register_observer(queue, "gone")
    clear_observer("gone")
    observability.emit(AgentEvent(kind="usage", run_id="gone"))
    try:
        queue.get_nowait()
        raise AssertionError("a cleared observer must receive nothing")
    except Empty:
        pass


# --- rounds (pointing --revision-of at a published bundle) --------------------


def test_resolve_run_dir_passes_an_existing_directory_through(tmp_path):
    from peerreviewagents.rounds import resolve_run_dir

    # The consumer hands a bundle *path*, never a job id under output_dir,
    # so the identity branch is the one it lives on.
    bundle = tmp_path / "v1"
    bundle.mkdir()
    assert resolve_run_dir(str(bundle), get_config()) == str(bundle)


# --- config construction ------------------------------------------------------


def test_get_config_accepts_every_override_the_consumer_passes(tmp_path):
    # run_review.py builds its overrides dict from exactly these keys; an
    # upstream rename would raise nothing (get_config takes **kwargs), so the
    # pin is that each one actually lands in the resolved config.
    cfg = get_config(
        provider="anthropic",
        reasoning_model="claude-opus-5",
        models={},
        agent_models={},
        max_debate_rounds=1,
        output_dir=str(tmp_path / "reports"),
        revision_of=str(tmp_path),
    )
    assert cfg["provider"] == "anthropic"
    assert cfg["reasoning_model"] == "claude-opus-5"
    assert cfg["models"] == {} and cfg["agent_models"] == {}
    assert cfg["max_debate_rounds"] == 1
    assert cfg["output_dir"] == str(tmp_path / "reports")
    assert cfg["revision_of"] == str(tmp_path)


def test_the_retired_revision_baseline_key_is_inert_not_fatal(tmp_path):
    """`revision_baseline_path` is gone; the consumer still sets it.

    run_review.py restores the prior draft from a versioned preprint URL and
    assigns `config["revision_baseline_path"] = path` after get_config has
    returned. It fed the section diff between drafts, and that diff is gone —
    it was informative only in a narrow band (a trivial revision read as
    "nothing changed", which the file hash now says for free; a real one read
    as "everything changed", which says nothing), and with the panel blinded
    it had no consumers left.

    What matters for the journal is that setting the key costs nothing. It is
    a plain dict assignment onto a resolved config, so it neither raises nor
    warns — nothing validates the final dict against DEFAULT_CONFIG — and no
    code reads it. Passing it through get_config is equally inert. The
    consumer can drop it whenever convenient; nothing breaks until then.
    """
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        cfg = get_config(revision_baseline_path=str(tmp_path / "prior.pdf"))
    cfg["revision_baseline_path"] = str(tmp_path / "prior.pdf")

    assert "revision_baseline_path" not in _default_keys()
    # And a run built from that config still resolves, unread key and all.
    assert cfg["revision_of"] is None


def _default_keys() -> set[str]:
    from peerreviewagents.default_config import DEFAULT_CONFIG

    return set(DEFAULT_CONFIG)


# --- the run itself and what the bundle writer reads off it -------------------


def test_final_state_carries_the_published_fields(monkeypatch, tmp_path):
    from peerreviewagents.graph.review_graph import PeerReviewGraph
    from peerreviewagents.reports import write_reports

    _patch_llms(monkeypatch)
    graph = PeerReviewGraph(get_config(
        max_debate_rounds=1,
        output_dir=str(tmp_path / "reports"),
        cache_dir=str(tmp_path / "cache"),
    ))
    # run_id is read before the run (to register the telemetry queue) and
    # must therefore exist on the graph object, not only in the state.
    assert isinstance(graph.run_id, str) and graph.run_id

    # The consumer's whole telemetry story is: register a queue under
    # graph.run_id, run, drain. A run whose events stop carrying that id
    # would leave every published cost/research breakdown silently empty.
    from peerreviewagents.observability import clear_observer, register_observer

    events: Queue = Queue()
    register_observer(events, graph.run_id)
    try:
        state = graph.review(SAMPLE)
    finally:
        clear_observer(graph.run_id)
    assert not events.empty()
    first = events.get_nowait()
    assert first.kind and hasattr(first, "node")

    # provenance.json is built from these keys; a missing one publishes as
    # null on every review page.
    assert state["decision"] in ("accept", "minor", "major", "reject")
    assert state["desk_rejected"] is False
    assert isinstance(state["errors"], list)
    assert isinstance(state["total_cost"], float)
    assert state["ingest"]["chars"] > 0 and state["ingest"]["text_sha256"]

    # panel_scores() reads these off every report dict, and keeps a null
    # score null — the not_applicable_reason key is how the page explains
    # an abstention instead of averaging over an invented number.
    for report in state["reports"]:
        assert isinstance(report["reviewer"], str)
        assert isinstance(report["score"], (int, float, type(None)))
        assert isinstance(report["confidence"], (int, float))
        assert "not_applicable_reason" in report

    # The bundle is copied out of write_reports' run directory.
    run_dir = write_reports(state)
    assert os.path.isfile(os.path.join(run_dir, "summary.md"))
    assert os.path.isfile(os.path.join(run_dir, "round.json"))


def test_a_null_score_report_carries_its_reason():
    # Statically, off the schema the reviewer nodes promote into the report
    # dict: a null score without a reason is rejected at validation time, so
    # the consumer can rely on the pair arriving together.
    import pytest
    from pydantic import ValidationError

    from peerreviewagents.agents.schemas import ReviewerOutput

    out = ReviewerOutput(
        score=None,
        not_applicable_reason="No quantitative analysis in this paper.",
        confidence=5,
        summary="Nothing in my dimension to judge.",
        strengths=[],
        weaknesses=[],
        questions=[],
    )
    assert out.score is None and out.not_applicable_reason
    with pytest.raises(ValidationError):
        ReviewerOutput(
            score=None, not_applicable_reason="", confidence=5,
            summary="x", strengths=[], weaknesses=[], questions=[],
        )
