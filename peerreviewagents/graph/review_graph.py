"""Assemble the peer-review LangGraph and provide the top-level runner."""

from __future__ import annotations

import os

from langgraph.graph import END, START, StateGraph

from ..agents.debate import advocate, skeptic
from ..agents.editor import editor_in_chief
from ..agents.integrity.panel import NODES as INTEGRITY_NODES
from ..agents.reviewers import get_reviewer_nodes
from ..agents.synthesis import meta_reviewer
from ..agents.utils.agent_states import ReviewState
from ..default_config import get_config
from ..ingest.loader import load_manuscript
from .conditional_logic import should_continue_debate


def build_graph(config: dict):
    g = StateGraph(ReviewState)

    reviewer_nodes = get_reviewer_nodes(config["reviewer_set"])
    for name, fn in reviewer_nodes:
        g.add_node(f"reviewer_{name}", fn)

    g.add_node("advocate", advocate.node)
    g.add_node("skeptic", skeptic.node)
    g.add_node("meta_reviewer", meta_reviewer.node)
    for name, fn in INTEGRITY_NODES:
        g.add_node(f"integrity_{name}", fn)
    g.add_node("editor", editor_in_chief.node)

    # Fan out to reviewers, fan in to the debate.
    for name, _ in reviewer_nodes:
        g.add_edge(START, f"reviewer_{name}")
        g.add_edge(f"reviewer_{name}", "advocate")

    # Debate loop: advocate -> skeptic -> (loop | meta_reviewer)
    g.add_edge("advocate", "skeptic")
    g.add_conditional_edges("skeptic", should_continue_debate, ["advocate", "meta_reviewer"])

    # Synthesis -> integrity panel (parallel) -> editor
    for name, _ in INTEGRITY_NODES:
        g.add_edge("meta_reviewer", f"integrity_{name}")
        g.add_edge(f"integrity_{name}", "editor")

    g.add_edge("editor", END)

    if config.get("checkpoint"):
        from langgraph.checkpoint.sqlite import SqliteSaver

        os.makedirs(os.path.dirname(config["checkpoint_path"]), exist_ok=True)
        return g.compile(checkpointer=SqliteSaver.from_conn_string(config["checkpoint_path"]))
    return g.compile()


class PeerReviewGraph:
    """High-level entry point, analogous to TradingAgentsGraph."""

    def __init__(self, config: dict | None = None):
        self.config = config or get_config()
        self.graph = build_graph(self.config)

    def initial_state(self, manuscript_path: str) -> ReviewState:
        title, md, sections = load_manuscript(manuscript_path)
        return ReviewState(
            manuscript_path=manuscript_path,
            manuscript_title=title,
            manuscript_md=md,
            sections=sections,
            config=self.config,
            reports=[],
            debate=[],
            debate_round=0,
            integrity_findings=[],
            errors=[],
        )

    def review(self, manuscript_path: str) -> ReviewState:
        state = self.initial_state(manuscript_path)
        cfg = {"recursion_limit": 50}
        if self.config.get("checkpoint"):
            cfg["configurable"] = {"thread_id": os.path.basename(manuscript_path)}
        return self.graph.invoke(state, cfg)

    def stream(self, manuscript_path: str):
        """Yield (node_name, partial_state) as the graph executes (for the TUI)."""
        state = self.initial_state(manuscript_path)
        cfg = {"recursion_limit": 50}
        if self.config.get("checkpoint"):
            cfg["configurable"] = {"thread_id": os.path.basename(manuscript_path)}
        yield "_ingest", state
        for chunk in self.graph.stream(state, cfg):
            for node_name, partial in chunk.items():
                yield node_name, partial
