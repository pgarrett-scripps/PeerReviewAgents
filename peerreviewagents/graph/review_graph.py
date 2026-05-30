"""Assemble the peer-review LangGraph and provide the top-level runner."""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from ..agents.author import rebuttal as author_rebuttal
from ..agents.debate import advocate, skeptic
from ..agents.editor import editor_in_chief
from ..agents.journal_recommender import recommender as journal_recommender
from ..agents.reviewers import get_reviewer_nodes
from ..agents.synthesis import meta_reviewer
from ..agents.utils.agent_states import ReviewState
from ..default_config import get_config
from ..ingest.loader import load_manuscript
from .conditional_logic import should_continue_debate


def build_graph(config: dict):
    g = StateGraph(ReviewState)

    reviewer_nodes = get_reviewer_nodes()
    for name, fn in reviewer_nodes:
        g.add_node(f"reviewer_{name}", fn)

    g.add_node("advocate", advocate.node)
    g.add_node("skeptic", skeptic.node)
    g.add_node("meta_reviewer", meta_reviewer.node)
    # Author rebuttal sits between meta-reviewer and editor so the
    # editor sees both the panel's verdict and the author's defense.
    g.add_node("author_rebuttal", author_rebuttal.node)
    g.add_node("editor", editor_in_chief.node)
    # Journal recommender runs after the editor so it can condition its
    # venue suggestions on the final accept/minor/major/reject verdict
    # and the required-revisions list in the decision letter.
    g.add_node("journal_recommender", journal_recommender.node)

    # Fan out to reviewers from START.
    for name, _ in reviewer_nodes:
        g.add_edge(START, f"reviewer_{name}")
        g.add_edge(f"reviewer_{name}", "advocate")

    # Debate loop: advocate -> skeptic -> (loop | meta_reviewer)
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
        self.config = config or get_config()
        self.graph = build_graph(self.config)

    def initial_state(self, manuscript_path: str) -> ReviewState:
        title, md, sections = load_manuscript(manuscript_path, self.config)
        return ReviewState(
            manuscript_path=manuscript_path,
            manuscript_title=title,
            manuscript_md=md,
            sections=sections,
            config=self.config,
            reports=[],
            debate=[],
            debate_round=0,
            errors=[],
            total_cost=0.0,
        )

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
        # while the Datalab API is parsing the PDF.
        yield "_ingest_start", {"manuscript_path": manuscript_path}
        state = self.initial_state(manuscript_path)
        accumulated: dict = dict(state)
        yield "_ingest", dict(accumulated)
        for chunk in self.graph.stream(state, {"recursion_limit": 50}):
            for node_name, partial in chunk.items():
                _merge_partial(accumulated, partial)
                yield node_name, dict(accumulated)


_LIST_MERGE_KEYS = ("reports", "debate", "errors")
_SUM_MERGE_KEYS = ("total_cost",)


def _merge_partial(accumulated: dict, partial: dict) -> None:
    for key, value in partial.items():
        if key in _LIST_MERGE_KEYS and isinstance(value, list):
            accumulated[key] = (accumulated.get(key) or []) + value
        elif key in _SUM_MERGE_KEYS and isinstance(value, (int, float)):
            accumulated[key] = (accumulated.get(key) or 0.0) + float(value)
        else:
            accumulated[key] = value
