"""Assemble the peer-review LangGraph and provide the top-level runner."""

from __future__ import annotations

import uuid
from functools import lru_cache
from typing import Annotated, Any, Callable, get_args, get_origin, get_type_hints

from langgraph.graph import END, START, StateGraph

from ..agents.auditors import get_auditor_nodes
from ..agents.author import rebuttal as author_rebuttal
from ..agents.debate import advocate, skeptic
from ..agents.editor import desk_screen, editor_in_chief
from ..agents.journal_recommender import recommender as journal_recommender
from ..agents.reviewers import get_reviewer_nodes
from ..agents.synthesis import meta_reviewer
from ..agents.utils.agent_states import ReviewState
from ..article_types import article_type_block, normalize_article_type
from ..default_config import get_config
from ..ingest.loader import load_manuscript
from ..journals import load_journal
from ..strictness import DEFAULT_LEVEL, normalize_strictness, strictness_block
from .conditional_logic import route_after_desk_screen, should_continue_debate


def build_graph(config: dict):
    g = StateGraph(ReviewState)

    reviewer_nodes = get_reviewer_nodes()
    for name, fn in reviewer_nodes:
        g.add_node(f"reviewer_{name}", fn)

    # Editorial audit lane: factual-checklist auditors that fan out alongside
    # the reviewers but route their reports straight to the editor (not into
    # the scored panel, debate, or meta-review).
    auditor_nodes = get_auditor_nodes()
    for name, fn in auditor_nodes:
        g.add_node(f"audit_{name}", fn)

    # The advocate/skeptic debate is on by default; `enable_debate=False`
    # ablates it (reviewers fan straight into the meta-reviewer), which is
    # how the eval harness measures the debate's contribution.
    debate_enabled = bool(config.get("enable_debate", True))
    if debate_enabled:
        g.add_node("advocate", advocate.node)
        g.add_node("skeptic", skeptic.node)
    g.add_node("meta_reviewer", meta_reviewer.node)
    # Author rebuttal sits between meta-reviewer and editor so the
    # editor sees both the panel's verdict and the author's defense.
    g.add_node("author_rebuttal", author_rebuttal.node)
    # `defer=True` is load-bearing: the editor joins two lanes of different
    # depths — the short audit lane (START -> audit -> editor) and the long
    # rebuttal chain (reviewers -> debate -> meta -> rebuttal -> editor).
    # LangGraph only barriers edges that settle in the SAME superstep, so a
    # plain node would fire once when the auditors finish (meta-review,
    # rebuttal, and scores still empty -> a junk decision letter) and again
    # after the rebuttal chain. Deferring makes the editor run once, after
    # every upstream task has drained.
    g.add_node("editor", editor_in_chief.node, defer=True)
    # Journal recommender runs after the editor so it can condition its
    # venue suggestions on the final accept/minor/major/reject verdict
    # and the required-revisions list in the decision letter.
    g.add_node("journal_recommender", journal_recommender.node)

    # Optional desk-screen node: a single triage node ahead of the fan-out.
    # Modes (see desk_screen.screen_mode): "gate" enforces desk-reject
    # (START -> desk_screen -> END on reject | fan out); "warm" runs it only to
    # prime the shared manuscript prompt cache before the parallel fan-out
    # reads it, always proceeding; "off" skips it (START fans out directly).
    # In warm mode the node returns desk_rejected=False, so the same
    # route_after_desk_screen fans out unconditionally.
    desk_screen_enabled = desk_screen.screen_mode(config) != "off"
    if desk_screen_enabled:
        g.add_node("desk_screen", desk_screen.node)
        g.add_edge(START, "desk_screen")
        g.add_conditional_edges(
            "desk_screen",
            route_after_desk_screen,
            [
                END,
                *[f"reviewer_{name}" for name, _ in reviewer_nodes],
                *[f"audit_{name}" for name, _ in auditor_nodes],
            ],
        )
    # With debate on, reviewers feed the advocate; with debate ablated, they
    # fan straight into the meta-reviewer.
    reviewer_sink = "advocate" if debate_enabled else "meta_reviewer"
    for name, _ in reviewer_nodes:
        if not desk_screen_enabled:
            g.add_edge(START, f"reviewer_{name}")
        g.add_edge(f"reviewer_{name}", reviewer_sink)

    # Audit lane fans out in parallel and converges on the editor, which is a
    # deferred node (see above) so it waits for both the rebuttal chain and
    # every auditor before it runs. On a desk-reject, the audits never fire.
    for name, _ in auditor_nodes:
        if not desk_screen_enabled:
            g.add_edge(START, f"audit_{name}")
        g.add_edge(f"audit_{name}", "editor")

    # Debate loop: advocate -> skeptic -> (loop | meta_reviewer). Skipped
    # entirely when debate is ablated.
    if debate_enabled:
        g.add_edge("advocate", "skeptic")
        g.add_conditional_edges("skeptic", should_continue_debate, ["advocate", "meta_reviewer"])

    # meta_reviewer -> author_rebuttal -> editor -> journal_recommender (linear).
    g.add_edge("meta_reviewer", "author_rebuttal")
    g.add_edge("author_rebuttal", "editor")
    g.add_edge("editor", "journal_recommender")
    g.add_edge("journal_recommender", END)

    return g.compile()


class PeerReviewGraph:
    """High-level entry point, analogous to TradingAgentsGraph."""

    def __init__(self, config: dict | None = None):
        self.config = dict(config or get_config())
        # Tag every event this run emits so a consumer can watch one review
        # without seeing another's. Callers wanting that isolation pass
        # `graph.run_id` to register_observer; registering without one still
        # receives everything.
        self.config.setdefault("run_id", uuid.uuid4().hex[:12])
        self.graph = build_graph(self.config)

    @property
    def run_id(self) -> str:
        return self.config["run_id"]

    def initial_state(self, manuscript_path: str) -> ReviewState:
        title, md, sections = load_manuscript(manuscript_path, self.config)
        sup_md, sup_sections = self._load_supplement()
        return ReviewState(
            manuscript_path=manuscript_path,
            manuscript_title=title,
            manuscript_md=md,
            sections=sections,
            supplement_md=sup_md,
            supplement_sections=sup_sections,
            config=self.config,
            journal_block=self._journal_block(),
            article_type_block=self._article_type_block(),
            strictness_block=self._strictness_block(),
            desk_rejected=False,
            reports=[],
            audits=[],
            debate=[],
            debate_round=0,
            errors=[],
            total_cost=0.0,
        )

    def _load_supplement(self) -> tuple[str, dict[str, str]]:
        """Parse the optional supplementary-information file, or ('', {}) if none.

        The SI is optional: no ``supplement_path`` means an unchanged run. A
        provided-but-unparseable SI must never crash the review, so any parse
        failure degrades to no-SI rather than propagating.
        """
        path = self.config.get("supplement_path")
        if not path:
            return "", {}
        try:
            _title, md, sections = load_manuscript(path, self.config)
        except Exception:  # noqa: BLE001 — optional input, never fail the run
            return "", {}
        return md, sections

    def _journal_block(self) -> str:
        """Render the target-journal prompt block once, or '' if none/missing.

        A bad slug should already have been caught at selection time
        (CLI/web); here we degrade gracefully so a run never crashes on
        venue context — the review just proceeds venue-agnostically.
        """
        try:
            profile = load_journal(self.config.get("target_journal"), self.config)
        except FileNotFoundError:
            return ""
        return profile.to_prompt_block() if profile else ""

    def _article_type_block(self) -> str:
        """Render the manuscript-type prompt block once, or '' if none/invalid.

        The general description/framing comes from the shared taxonomy; any
        per-type word caps and notes are pulled from the selected target
        journal's profile. A bad type or journal slug degrades to '' here so a
        run never crashes on manuscript-type context (the CLI/web layer
        validates and fails fast before this point).
        """
        try:
            key = normalize_article_type(self.config.get("article_type"))
        except ValueError:
            return ""
        if not key:
            return ""
        max_words = abstract_max_words = 0
        notes = ""
        try:
            profile = load_journal(self.config.get("target_journal"), self.config)
        except FileNotFoundError:
            profile = None
        limits = profile.article_type_limits(key) if profile else None
        if limits:
            max_words = limits.max_words
            abstract_max_words = limits.abstract_max_words
            notes = limits.notes
        return article_type_block(
            key,
            max_words=max_words,
            abstract_max_words=abstract_max_words,
            notes=notes,
        )

    def _strictness_block(self) -> str:
        """Render the review-strictness directive once, or '' at the balanced
        default. An out-of-range value (e.g. from a hand-edited TOML) degrades
        to the balanced default rather than crashing a run; the CLI/web layer
        validates and fails fast before reaching here.
        """
        try:
            level = normalize_strictness(self.config.get("review_strictness", DEFAULT_LEVEL))
        except ValueError:
            level = DEFAULT_LEVEL
        return strictness_block(level)

    def review(self, manuscript_path: str) -> ReviewState:
        state = self.initial_state(manuscript_path)
        return self.graph.invoke(state, {"recursion_limit": 50})

    def stream(self, manuscript_path: str):
        """Yield (node_name, accumulated_state) as the graph executes.

        We accumulate state ourselves because LangGraph's default stream mode
        emits per-node partials, and parallel writers to reducer fields
        (reports, debate, errors) would otherwise look like overwrites to
        a naive consumer doing dict.update.
        """
        # Emit a start event BEFORE parsing so the CLI/TUI shows activity
        # while pypdf works through the document — a long PDF takes a
        # while and the UI would otherwise look hung.
        yield "_ingest_start", {"manuscript_path": manuscript_path}
        state = self.initial_state(manuscript_path)
        accumulated: dict = dict(state)
        yield "_ingest", dict(accumulated)
        for chunk in self.graph.stream(state, {"recursion_limit": 50}):
            for node_name, partial in chunk.items():
                _merge_partial(accumulated, partial)
                yield node_name, dict(accumulated)


@lru_cache(maxsize=1)
def _state_reducers() -> dict[str, Callable[[Any, Any], Any]]:
    """Read the reducers straight off ``ReviewState``'s annotations.

    Fields written by parallel nodes are declared as
    ``Annotated[list[X], operator.add]`` so LangGraph combines rather than
    overwrites them. ``stream()`` has to apply the same rule while
    accumulating, and that used to be a hand-maintained list of key names —
    so adding a reducer field and forgetting the list silently downgraded
    streaming to overwrite. Only ``review()`` would look right, while the
    CLI, TUI, and web UI all consume ``stream()``. Deriving it from the one
    declaration removes the chance to forget.
    """
    reducers: dict[str, Callable[[Any, Any], Any]] = {}
    for key, hint in get_type_hints(ReviewState, include_extras=True).items():
        if get_origin(hint) is not Annotated:
            continue
        for meta in get_args(hint)[1:]:
            if callable(meta):
                reducers[key] = meta
                break
    return reducers


def _merge_partial(accumulated: dict, partial: dict) -> None:
    reducers = _state_reducers()
    for key, value in partial.items():
        reduce = reducers.get(key)
        if reduce is not None and key in accumulated:
            accumulated[key] = reduce(accumulated[key], value)
        else:
            accumulated[key] = value
