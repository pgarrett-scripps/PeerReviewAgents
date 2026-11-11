"""MCP tools that run the evidence-linked review engine.

One tool call is one engine review: the PDF is imported into
``<output>/manuscript``, reviewed in ``<output>/review`` and reported as
``REVIEW.md`` and ``REVIEW.txt`` in ``<output>``, which is exactly what
``peerreview review manuscript.pdf <output>`` does. The job state, the path
confinement and the report-on-failure behaviour are
:mod:`peerreviewagents.review_service`, which the web UI runs on too, so this
module is only the MCP transport over it.

The review runs on a subscription transport: Claude Code by default, Codex when
``provider="codex"`` is named. Target journals, article types, strictness
levels, debate rounds and the research agent were parameters of the legacy
LangGraph panel. The engine has no equivalent for any of them, so they are
gone rather than accepted and ignored.
"""

from __future__ import annotations

import os
from typing import Any

from ..review_service import ReviewJob, ReviewService

__all__ = ["ReviewJob", "ReviewService", "create_server", "run"]


def create_server(service: ReviewService | None = None):
    """Create the FastMCP server, importing the optional SDK only when needed."""
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:
        raise RuntimeError(
            "The MCP server requires the optional dependency. "
            "Install with: pip install 'peerreviewagents[mcp]'"
        ) from exc

    reviews = service or ReviewService()
    server = FastMCP("Peer Review Agents")

    @server.tool()
    def start_peer_review(
        manuscript_path: str,
        output_name: str | None = None,
        mode: str = "ensemble",
        reviewers: int = 2,
        provider: str | None = None,
        model: str | None = None,
        policy: str | None = None,
        token_budget: int | None = None,
        literature_cutoff: str | None = None,
    ) -> dict[str, Any]:
        """Start an evidence-linked review of a manuscript PDF and return its job ID.

        The PDF is imported, reviewed by independent reviewers and an audited
        debate, and reported as REVIEW.md. ``mode`` is ensemble, independent or
        panel; ``provider="codex"`` runs on a ChatGPT subscription instead of
        Claude Code; ``policy="standard-v1"`` is the bounded study budget.
        """
        return reviews.start_review(
            manuscript_path,
            output_name=output_name,
            mode=mode,
            reviewers=reviewers,
            provider=provider,
            model=model,
            policy=policy,
            token_budget=token_budget,
            literature_cutoff=literature_cutoff,
        )

    @server.tool()
    def get_peer_review_status(job_id: str) -> dict[str, Any]:
        """Get the current stage, status and reports for a review job."""
        return reviews.get_status(job_id)

    @server.tool()
    def cancel_peer_review(job_id: str) -> dict[str, Any]:
        """Cancel a queued review. A running model call cannot be interrupted."""
        return reviews.cancel(job_id)

    @server.tool()
    def list_peer_review_artifacts(job_id: str) -> list[dict[str, Any]]:
        """List the reports and saved run files a review produced."""
        return reviews.list_artifacts(job_id)

    @server.tool()
    def read_peer_review_artifact(job_id: str, name: str) -> str:
        """Read one artifact by the name the listing gave, such as REVIEW.md."""
        return reviews.read_artifact(job_id, name)

    @server.tool()
    def peer_review_capabilities() -> dict[str, Any]:
        """The engine's modes, models, policies and defaults for this server."""
        return reviews.capabilities()

    return server


def run() -> None:
    """Run the MCP server over standard input and output."""
    configured_roots = os.environ.get("PEERREVIEW_MCP_INPUT_ROOTS", "")
    input_roots = [root for root in configured_roots.split(os.pathsep) if root]
    output_root = os.environ.get("PEERREVIEW_MCP_OUTPUT_ROOT") or None
    service = ReviewService(
        input_roots=input_roots or None,
        output_root=output_root,
    )
    create_server(service).run(transport="stdio")
