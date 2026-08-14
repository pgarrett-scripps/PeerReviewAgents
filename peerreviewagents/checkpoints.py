"""Atomic per-node checkpoints for resumable review runs."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Callable

from .observability import AgentEvent, emit


_NON_SEMANTIC_CONFIG = {
    "run_id", "output_dir", "cache_dir", "checkpoint_dir", "resume",
}


def _run_key(state: dict) -> str:
    manuscript = str(state.get("manuscript_md") or "")
    config = {
        key: value for key, value in (state.get("config") or {}).items()
        if key not in _NON_SEMANTIC_CONFIG
    }
    payload = manuscript.encode("utf-8") + b"\0" + json.dumps(
        config, sort_keys=True, default=str, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:20]


def checkpointed(name: str, fn: Callable, config: dict) -> Callable:
    """Return a node that reuses a successful atomic checkpoint when present."""
    root = str(config.get("checkpoint_dir") or "").strip()
    if not root:
        return fn

    def run(state: dict) -> dict:
        # Debate roles execute repeatedly. A static filename would replay
        # round one forever, so make the phase part of their checkpoint ID.
        phase = ""
        if name in {"advocate", "skeptic"}:
            phase = f"-round-{int(state.get('debate_round') or 0) + 1}"
        path = Path(root) / _run_key(state) / f"{name}{phase}.json"
        if config.get("resume", True) and path.is_file():
            try:
                saved = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(saved, dict) and not saved.get("errors"):
                    emit(AgentEvent(kind="log", node=name, text="resumed from checkpoint"))
                    return saved
            except (OSError, json.JSONDecodeError):
                pass

        result = fn(state)
        if not isinstance(result, dict) or result.get("errors"):
            return result
        cost_limit = float(config.get("max_node_cost_usd") or 0)
        node_cost = float(result.get("total_cost") or 0)
        if cost_limit and node_cost > cost_limit:
            return {
                "errors": [
                    f"{name} exceeded its ${cost_limit:.2f} node budget "
                    f"(${node_cost:.2f})"
                ],
                "total_cost": node_cost,
            }

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(f".tmp-{os.getpid()}")
            temporary.write_text(
                json.dumps(result, ensure_ascii=False, sort_keys=True), encoding="utf-8"
            )
            os.replace(temporary, path)
        except OSError:
            # A read-only cache location must not turn a completed agent into
            # a failed review. Resume is an optimization, not correctness.
            pass
        return result

    run.__name__ = getattr(fn, "__name__", name)
    return run
