"""Top-level re-exports for convenience.

Library usage:
    from peerreviewagents import PeerReviewGraph, get_config, write_reports
"""

from __future__ import annotations

from .default_config import get_config
from .graph.review_graph import PeerReviewGraph
from .reports import write_reports

__all__ = ["PeerReviewGraph", "get_config", "write_reports"]
