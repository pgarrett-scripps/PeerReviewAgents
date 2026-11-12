"""The `peerreview` MCP server for the 0.9 spike: three tools, no model calls.

This is build step 1 of the 0.9 plan, cut to the smallest thing that answers
one question: does a plugin with a workflow script, a subagent and an MCP
server run headless on the subscription login? So it exposes only `ingest`,
`read_blocks` and `submit_finding`, and it is deliberately not the 0.9 server:
there are no agent identity tokens, no stage ledger, no run status, no
`basis` label and no review file.

What it does take from the engine is the read path a reviewer actually uses,
rather than a second copy of it:

- :func:`peerreviewagents.engine.ingest.import_paper` builds the immutable
  workspace, exactly as ``peerreview review`` does today.
- :func:`peerreviewagents.engine.annotations.verify_workspace` re-checks every
  file against ``manifest.json`` when a run reuses a workspace, so a changed
  manuscript is refused rather than reviewed.
- :func:`peerreviewagents.engine.source_payload.text_block` hands a block over
  in the same shape ``investigation.py`` hands it to a reviewer today.

The two rules the engine's tool server keeps are kept here too: at most twelve
blocks in one read, and a per-run tool allowance, after which the server
answers with a notice instead of more evidence.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..engine.annotations import verify_workspace
from ..engine.ingest import import_paper
from ..engine.source_payload import text_block
from ..engine.storage import digest

MAX_BLOCKS_PER_READ = 12
TOOL_ALLOWANCE = 24
MANUSCRIPT_DIRECTORY = "manuscript"
FINDINGS_FILE = "findings.jsonl"
SEVERITIES = ("major", "moderate", "minor")
EXHAUSTED = "Tool allowance exhausted. Finish from the evidence you already have."


@dataclass
class Run:
    """One spike run: the imported manuscript and the findings stored for it."""

    root: Path
    workspace: Path
    paper: dict[str, Any]
    blocks: dict[str, dict[str, Any]] = field(default_factory=dict)
    calls: int = 0

    @property
    def findings(self) -> Path:
        return self.root / FINDINGS_FILE


_run: Run | None = None


def _active() -> Run:
    if _run is None:
        raise ValueError("No manuscript has been ingested yet. Call ingest first.")
    return _run


def _spend() -> str | None:
    """Count a tool call against the run's allowance, as the engine's server does."""
    run = _active()
    run.calls += 1
    return EXHAUSTED if run.calls > TOOL_ALLOWANCE else None


def ingest(pdf: str, output: str) -> dict[str, Any]:
    """Import one PDF into an immutable workspace and return its block index.

    The index is what a reviewer navigates by: this server has no `search`
    tool, so the section-to-block map is the only way an agent can choose what
    to read.
    """
    global _run
    source = Path(pdf).expanduser().resolve()
    if not source.is_file():
        raise ValueError(f"manuscript not found: {source}")
    root = Path(output).expanduser().resolve()
    workspace = root / MANUSCRIPT_DIRECTORY
    if (workspace / "paper.json").exists():
        paper = verify_workspace(workspace)
    else:
        paper = import_paper(str(source), str(workspace))
    _run = Run(root=root, workspace=workspace, paper=paper,
               blocks={block["id"]: block for block in paper["blocks"]})
    return {
        "paper_id": paper["id"],
        "title": paper["title"],
        "block_count": len(paper["blocks"]),
        "sections": paper.get("sections", {}),
        "statistics": paper.get("statistics", {}),
        "warnings": paper.get("warnings", []),
        "tool_allowance": TOOL_ALLOWANCE,
        "max_blocks_per_read": MAX_BLOCKS_PER_READ,
    }


def read_blocks(ids: list[str]) -> dict[str, Any]:
    """Return up to twelve manuscript blocks by id, as the engine returns them."""
    run = _active()
    notice = _spend()
    if notice:
        return {"notice": notice}
    if not ids:
        raise ValueError("Name at least one block id, for example b0001")
    if len(ids) > MAX_BLOCKS_PER_READ:
        raise ValueError(f"Read at most {MAX_BLOCKS_PER_READ} blocks")
    unknown = [block_id for block_id in ids if block_id not in run.blocks]
    if unknown:
        raise ValueError("No such block: " + ", ".join(unknown) +
                         f". This manuscript has b0001 to b{len(run.blocks):04d}.")
    return {"blocks": [text_block(run.blocks[block_id]) for block_id in ids]}


def submit_finding(
    severity: str,
    claim: str,
    recommendation: str,
    blocks: list[str],
) -> dict[str, Any]:
    """Store one finding and return the receipt that proves it was stored.

    The receipt is the digest of the stored record. The workflow script cannot
    read the run folder, so the receipt is what it checks; the batch starter
    checks the file itself afterwards.
    """
    run = _active()
    notice = _spend()
    if notice:
        return {"notice": notice}
    if severity not in SEVERITIES:
        raise ValueError("severity must be one of: " + ", ".join(SEVERITIES))
    if len(claim.strip()) < 40:
        raise ValueError("Say what the problem is in a full sentence")
    if not recommendation.strip():
        raise ValueError("Say what the authors should do about it")
    unknown = [block_id for block_id in blocks if block_id not in run.blocks]
    if not blocks or unknown:
        raise ValueError("Cite the blocks the finding rests on, by the ids ingest listed")
    record = {
        "paper_id": run.paper["id"],
        "severity": severity,
        "claim": claim.strip(),
        "recommendation": recommendation.strip(),
        "blocks": list(blocks),
        "block_digests": {block_id: run.blocks[block_id]["sha256"] for block_id in blocks},
    }
    line = json.dumps(record, ensure_ascii=False, sort_keys=True)
    receipt = digest(line)[:16]
    record["receipt"] = receipt
    run.findings.parent.mkdir(parents=True, exist_ok=True)
    with run.findings.open("a", encoding="utf-8") as findings:
        findings.write(json.dumps(record, ensure_ascii=False) + "\n")
    return {
        "stored": True,
        "receipt": receipt,
        "findings_file": str(run.findings),
        "tool_calls_left": max(0, TOOL_ALLOWANCE - run.calls),
    }


def create_server():
    """The stdio MCP server, with the three tools and nothing else."""
    try:
        from mcp.server.fastmcp import FastMCP
    except ModuleNotFoundError as error:  # pragma: no cover - depends on the extra
        raise RuntimeError(
            "The MCP server needs the 'mcp' package: pip install 'peerreviewagents[mcp]'"
        ) from error
    server = FastMCP("peerreview")
    server.tool()(ingest)
    server.tool()(read_blocks)
    server.tool()(submit_finding)
    return server


def run() -> None:
    create_server().run()


if __name__ == "__main__":
    run()
