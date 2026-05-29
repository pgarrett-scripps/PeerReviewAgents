"""FastAPI-based web UI for PeerReviewAgents.

A single-job, in-memory server that accepts a manuscript upload, runs it
through the LangGraph pipeline in a background thread, and streams agent
lifecycle / token events to a browser over WebSocket. The browser
renders a 2D "office" with one sprite per agent so the reviewing
process is legible to a human watcher.
"""

from .server import create_app

__all__ = ["create_app"]
