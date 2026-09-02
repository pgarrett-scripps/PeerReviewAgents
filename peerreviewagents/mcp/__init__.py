"""MCP integration for PeerReviewAgents."""

from .server import ReviewService, create_server, run

__all__ = ["ReviewService", "create_server", "run"]
