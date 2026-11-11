"""FastAPI web UI for the evidence-linked review engine.

A manuscript PDF is uploaded, reviewed in a background thread by
:mod:`peerreviewagents.review_service` — the same service the MCP server runs
on — and reported as ``REVIEW.md``, which the page renders. Progress is the
stage the run records; there is no token stream and no per-agent sprite,
because the engine runs each stage as one long model call.
"""

from .server import create_app

__all__ = ["create_app"]
