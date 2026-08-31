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
from dataclasses import replace
from typing import Any

from ..graph.review_graph import PeerReviewGraph
from .corpus import load_corpus
from .integrity import inspect_run_artifacts
from .schema import (
    RunRecord,
    append_jsonl,
    build_manifest,
    config_digest,
    read_jsonl,
    require_source_fingerprint,
    source_fingerprint,
    verify_protocol,
)


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


def existing_keys(
    runs_path: str,
    *,
    config: dict[str, Any] | None = None,
    mode: str | None = None,
    corpus_sha256: str | None = None,
) -> set[tuple[str, int]]:
    """(paper_id, repeat) pairs that already SUCCEEDED.

    Only ``ok`` runs count as done, so a transient failure (provider error,
    crash) is retried on the next invocation rather than permanently blocking
    that slot. Failed records stay in the file as a trail but are ignored here.
    """
    keys: set[tuple[str, int]] = set()
    expected_digest = config_digest(config) if config is not None else None
    expected_source = source_fingerprint() if config is not None else None
    for d in read_jsonl(runs_path):
        manifest = d.get("manifest") or {}
        found_digest = manifest.get("config_digest")
        found_mode = manifest.get("mode")
        found_corpus = manifest.get("corpus_sha256")
        found_source = manifest.get("source_fingerprint")
        if expected_digest is not None and found_digest != expected_digest:
            raise ValueError(
                f"{runs_path} already contains records for config "
                f"{found_digest or 'unknown'}, not requested config {expected_digest}; "
                "use a separate --runs-out file"
            )
        if mode is not None and found_mode != mode:
            raise ValueError(
                f"{runs_path} contains mode {found_mode or 'unknown'}, not {mode}; "
                "use a separate --runs-out file"
            )
        if corpus_sha256 is not None and found_corpus != corpus_sha256:
            raise ValueError(
                f"{runs_path} contains corpus {found_corpus or 'unknown'}, not the "
                f"currently frozen corpus {corpus_sha256}; use a separate --runs-out file"
            )
        if expected_source is not None and found_source != expected_source:
            raise ValueError(
                f"{runs_path} contains source {found_source or 'unknown'}, not current "
                f"source {expected_source}; use a separate --runs-out file"
            )
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
    from .corpus import verify_corpus_manifest

    corpus_manifest = verify_corpus_manifest(corpus_path, warn_missing=True)
    verify_protocol(corpus_path, config)
    batch_source = source_fingerprint()
    corpus_sha256 = corpus_manifest.get("corpus_sha256") if corpus_manifest else ""
    corpus = load_corpus(corpus_path)
    if only:
        wanted = set(only)
        corpus = [c for c in corpus if c.id in wanted]
    if not corpus:
        print("No corpus papers selected; nothing to do.")
        return 0

    done = existing_keys(
        runs_path, config=config, mode="system", corpus_sha256=corpus_sha256 or None,
    )
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
        require_source_fingerprint(
            batch_source, context=f"before {item.id} repeat {rep}",
        )
        if verbose:
            print(f"\n→ {item.id} repeat {rep}: {item.title[:70]}")
        record = _run_one(item, rep, config, leakage_note, corpus_sha256=corpus_sha256)
        require_source_fingerprint(
            batch_source, context=f"after {item.id} repeat {rep}",
        )
        append_jsonl(runs_path, record.to_json())
        performed += 1
        if verbose:
            failures = [*record.errors, *record.artifact_integrity_errors]
            status = "ok" if record.ok else f"FAILED ({'; '.join(failures) or 'no decision'})"
            print(f"  {status} — decision={record.system_decision} "
                  f"score={record.system_weighted_score} "
                  f"cost=${record.cost_usd:.4f} {record.latency_s:.0f}s")
    return performed


def _run_one(
    item,
    rep: int,
    config: dict[str, Any],
    leakage_note: str,
    *,
    corpus_sha256: str = "",
) -> RunRecord:
    manifest = build_manifest(config, venue=item.venue, leakage_note=leakage_note)
    manifest_dict = manifest.to_dict()
    manifest_dict["mode"] = "system"
    manifest_dict["corpus_sha256"] = corpus_sha256
    t0 = time.time()
    try:
        state = PeerReviewGraph(config).review(item.pdf_path)
    except Exception as exc:  # noqa: BLE001
        return RunRecord(
            paper_id=item.id, repeat=rep, ok=False,
            system_decision=None, system_weighted_score=None,
            errors=[f"pipeline crashed: {exc}"],
            latency_s=round(time.time() - t0, 1),
            manifest=manifest_dict,
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
                "score_source": r.get("score_source") or "legacy",
                # Source of truth for qualitative comparison. Empty derived
                # weakness metadata must never be mistaken for an empty review.
                "markdown": r.get("body") or "",
            }
            for r in reports
        ]
        decision = state.get("decision") or None
        record = RunRecord(
            paper_id=item.id, repeat=rep,
            # A decision produced under an explicitly enabled quorum policy is
            # still useful operational output, but it is not a successful
            # experimental completion. Counting it as ``ok`` inflated the
            # reliability endpoint by treating absent reviewers as success.
            ok=bool(decision) and bool(state.get("panel_complete", True))
            and not bool(state.get("panel_degraded")),
            system_decision=decision,
            system_weighted_score=weighted_score(reports),
            per_reviewer=per_reviewer,
            decision_letter=state.get("decision_letter") or "",
            debate_markdown=[dict(turn) for turn in (state.get("debate") or [])],
            debate_synthesis=state.get("debate_synthesis") or "",
            audit_markdown=[
                {
                    "auditor": audit.get("auditor"),
                    "title": audit.get("title"),
                    "markdown": audit.get("body") or "",
                }
                for audit in (state.get("audits") or [])
            ],
            n_reviewers=len(reports),
            cost_usd=round(float(state.get("total_cost") or 0.0), 4),
            latency_s=round(time.time() - t0, 1),
            errors=list(state.get("errors") or []),
            manifest=manifest_dict,
        )
        integrity_errors = inspect_run_artifacts(record)
        return replace(
            record,
            ok=record.ok and not integrity_errors,
            artifact_integrity_ok=not integrity_errors,
            artifact_integrity_errors=integrity_errors,
        )
    except Exception as exc:  # noqa: BLE001
        return RunRecord(
            paper_id=item.id, repeat=rep, ok=False,
            system_decision=None, system_weighted_score=None,
            errors=[f"post-run bookkeeping failed: {exc}"],
            latency_s=round(time.time() - t0, 1),
            manifest=manifest_dict,
        )
