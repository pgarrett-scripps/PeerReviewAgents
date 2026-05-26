"""Append-only review memory for cross-run calibration (analog of trading_memory.md)."""

from __future__ import annotations

import datetime as _dt
import os

from .agent_states import ReviewState


def append_memory(state: ReviewState) -> None:
    path = state["config"]["memory_path"]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    scores = [r["score"] for r in state.get("reports", [])]
    avg = sum(scores) / len(scores) if scores else float("nan")
    entry = (
        f"\n## {_dt.datetime.now().isoformat(timespec='seconds')} — "
        f"{state.get('manuscript_title', 'Untitled')}\n"
        f"- Decision: {state.get('decision', 'n/a')}\n"
        f"- Avg reviewer score: {avg:.2f}\n"
        f"- Reviewers: {', '.join(r['reviewer'] for r in state.get('reports', []))}\n"
    )
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(entry)

