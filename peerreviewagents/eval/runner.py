"""Resumable batch runner: review every corpus paper ``--repeats`` times.

Each completed run is appended to ``runs.jsonl`` as a :class:`RunRecord`,
keyed by ``(paper_id, repeat)``. On restart we read back the existing keys and
skip them, so a crash (or a deliberate two-phase plan — all papers once for
agreement, then a subset at higher ``--repeats`` for consistency) never repeats
finished work. The pipeline's own 3x provider retry keeps a long batch from
dying on a single upstream blip.
"""

from __future__ import annotations

import time
from typing import Any

from ..graph.review_graph import PeerReviewGraph
from .corpus import load_corpus
from .schema import RunRecord, append_jsonl, build_manifest, read_jsonl


def weighted_score(reports: list[dict[str, Any]]) -> float | None:
    """Confidence-weighted mean reviewer score — the system's headline number.

    Mirrors :func:`peerreviewagents.agents.utils.agent_utils.score_summary`
    so the eval number matches what the pipeline shows — including its
    filter: a score is null by design when a dimension had nothing to judge
    in a manuscript, and an abstaining reviewer must drop out of the average
    rather than multiply ``None * confidence`` and take the batch down.
    Returns ``None`` when no reviewer could score at all.
    """
    scored = [r for r in reports if isinstance(r.get("score"), (int, float))]
    if not scored:
        return None
    total_w = sum(r["confidence"] for r in scored) or 1.0
    return round(sum(r["score"] * r["confidence"] for r in scored) / total_w, 4)


def existing_keys(runs_path: str) -> set[tuple[str, int]]:
    """(paper_id, repeat) pairs that already SUCCEEDED.

    Only ``ok`` runs count as done, so a transient failure (provider error,
    crash) is retried on the next invocation rather than permanently blocking
    that slot. Failed records stay in the file as a trail but are ignored here.
    """
    keys: set[tuple[str, int]] = set()
    for d in read_jsonl(runs_path):
        if d.get("ok"):
            keys.add((d.get("paper_id"), int(d.get("repeat", 0))))
    return keys


def run_batch(
    corpus_path: str,
    runs_path: str,
    config: dict[str, Any],
    *,
    repeats: int = 1,
    only: list[str] | None = None,
    leakage_note: str = "",
    verbose: bool = True,
) -> int:
    """Ensure each (selected) paper has ``repeats`` runs in ``runs_path``.

    Returns the number of new runs performed. ``only`` restricts to a subset of
    paper ids (used for the consistency phase).
    """
    corpus = load_corpus(corpus_path)
    if only:
        wanted = set(only)
        corpus = [c for c in corpus if c.id in wanted]
    if not corpus:
        print("No corpus papers selected; nothing to do.")
        return 0

    done = existing_keys(runs_path)
    todo = [
        (item, rep)
        for item in corpus
        for rep in range(repeats)
        if (item.id, rep) not in done
    ]
    if not todo:
        print(f"All {len(corpus)} paper(s) already have {repeats} run(s). Nothing to do.")
        return 0

    print(f"Running {len(todo)} new run(s) across {len(corpus)} paper(s) "
          f"(repeats={repeats}); {len(done)} already on disk.")

    performed = 0
    for item, rep in todo:
        if verbose:
            print(f"\n→ {item.id} repeat {rep}: {item.title[:70]}")
        record = _run_one(item, rep, config, leakage_note)
        append_jsonl(runs_path, record.to_json())
        performed += 1
        if verbose:
            status = "ok" if record.ok else f"FAILED ({'; '.join(record.errors) or 'no decision'})"
            print(f"  {status} — decision={record.system_decision} "
                  f"score={record.system_weighted_score} "
                  f"cost=${record.cost_usd:.4f} {record.latency_s:.0f}s")
    return performed


def _run_one(item, rep: int, config: dict[str, Any], leakage_note: str) -> RunRecord:
    manifest = build_manifest(config, venue=item.venue, leakage_note=leakage_note)
    t0 = time.time()
    try:
        state = PeerReviewGraph(config).review(item.pdf_path)
    except Exception as exc:  # noqa: BLE001
        return RunRecord(
            paper_id=item.id, repeat=rep, ok=False,
            system_decision=None, system_weighted_score=None,
            errors=[f"pipeline crashed: {exc}"],
            latency_s=round(time.time() - t0, 1),
            manifest=manifest.to_dict(),
        )

    # Bookkeeping failures are recorded like graph failures, not raised: one
    # paper's malformed state must not end a batch that other papers' runs
    # are still waiting on — the record keeps the trail and the slot retries.
    try:
        reports = state.get("reports") or []
        per_reviewer = [
            {
                "name": r.get("reviewer"),
                "score": r.get("score"),
                "confidence": r.get("confidence"),
                # Kept per reviewer, not just the aggregate: weakness-level
                # overlap against the human reviews is the study's endpoint,
                # and it cannot be recomputed from a score after the fact.
                "weaknesses": list(r.get("weaknesses") or []),
                "not_applicable_reason": r.get("not_applicable_reason") or "",
            }
            for r in reports
        ]
        decision = state.get("decision") or None
        return RunRecord(
            paper_id=item.id, repeat=rep,
            ok=bool(decision),
            system_decision=decision,
            system_weighted_score=weighted_score(reports),
            per_reviewer=per_reviewer,
            n_reviewers=len(reports),
            cost_usd=round(float(state.get("total_cost") or 0.0), 4),
            latency_s=round(time.time() - t0, 1),
            errors=list(state.get("errors") or []),
            manifest=manifest.to_dict(),
        )
    except Exception as exc:  # noqa: BLE001
        return RunRecord(
            paper_id=item.id, repeat=rep, ok=False,
            system_decision=None, system_weighted_score=None,
            errors=[f"post-run bookkeeping failed: {exc}"],
            latency_s=round(time.time() - t0, 1),
            manifest=manifest.to_dict(),
        )
