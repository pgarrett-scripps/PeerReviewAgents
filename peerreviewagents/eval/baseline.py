"""Single-LLM practical baseline for comparison with the full board.

This module characterizes how the complete workflow differs from a common,
low-cost alternative: one holistic "review this paper" call. It reviews each corpus paper
with exactly one LLM call that collapses the panel + editor into a single
holistic verdict — one reviewer, zero debate rounds, no rebuttal — and
writes the same :class:`RunRecord` shape the full pipeline does, so the
existing ``metrics`` / ``figure`` harness scores it unchanged.

The single call sees the identical manuscript and
the identical venue / article-type / strictness conditioning the panel
gets (via :func:`context_block` over the same initial state). It is not a
compute-matched causal ablation: the full workflow uses more calls, tokens,
roles, and synthesis. Reports must therefore present quality, cost, and latency
together and describe this as a practical baseline comparison.

Runs land in a separate, model-namespaced file (``runs_baseline_<model>.jsonl``)
so several models can be benchmarked side by side without colliding on the
``(paper_id, repeat)`` resume key.
"""

from __future__ import annotations

import re
import time
from dataclasses import replace
from typing import Any

from pydantic import BaseModel

from ..agents.utils.agent_utils import context_block
from ..agents.utils.llm import make_llm
from ..agents.utils.structured import extract_structured_metadata, invoke_markdown
from ..graph.review_graph import PeerReviewGraph
from ..ingest.loader import require_readable
from .corpus import load_corpus
from .integrity import inspect_run_artifacts
from .runner import existing_keys
from .schema import RunRecord, append_jsonl, build_manifest, verify_protocol

_VALID_VERDICTS = ("accept", "minor", "major", "reject")
_DECISION_FOR_SCORE = {1: "reject", 2: "reject", 3: "major", 4: "minor", 5: "accept"}
_SCORE_FOR_DECISION = {"reject": 1, "major": 3, "minor": 4, "accept": 5}


class BaselineMetadata(BaseModel):
    """Tiny sidecar recovered only when explicit Markdown labels are absent."""

    score: int
    decision: str


_SYS = (
    "You are a single expert peer reviewer acting alone. There is no review "
    "panel, no debate, no author rebuttal, and no separate editor: you read "
    "the manuscript once and render the final verdict yourself. If a target "
    "venue, manuscript type, or review-strictness standard is described in the "
    "context above, judge the manuscript against it. Write a substantive "
    "Markdown review. Put `SCORE: <1-5>` and `VERDICT: "
    "accept|minor|major|reject` at the top when possible; ordinary Markdown "
    "is the source of truth and must never be replaced by JSON."
)

_SCORE_LINE = re.compile(r"(?im)^\s*(?:\*\*)?score(?:\*\*)?\s*:\s*([1-5])\b")
_VERDICT_LINE = re.compile(
    r"(?im)^\s*(?:\*\*)?(?:verdict|decision)(?:\*\*)?\s*:\s*"
    r"(accept|minor(?:\s+revision)?|major(?:\s+revision)?|reject)\b"
)


def _parse_metadata(llm, config: dict[str, Any], markdown: str) -> tuple[int, str, float, list[str]]:
    score_match = _SCORE_LINE.search(markdown)
    verdict_match = _VERDICT_LINE.search(markdown)
    if score_match:
        score = int(score_match.group(1))
        decision = _DECISION_FOR_SCORE[score]
        warnings = []
        if verdict_match and verdict_match.group(1).lower().split()[0] != decision:
            warnings.append("baseline verdict label conflicted with score; used frozen score mapping")
        return score, decision, 0.0, warnings
    if verdict_match:
        decision = verdict_match.group(1).lower().split()[0]
        return _SCORE_FOR_DECISION[decision], decision, 0.0, [
            "baseline score derived from explicit verdict using frozen mapping"
        ]

    normalized = extract_structured_metadata(llm, BaselineMetadata, config, markdown)
    if normalized is None:
        raise ValueError("baseline Markdown omitted a recoverable score or verdict")
    raw_score = getattr(normalized.instance, "score", None)
    raw_verdict = str(getattr(normalized.instance, "decision", "")).lower().split()[0]
    score = int(raw_score) if isinstance(raw_score, (int, float)) else None
    if score in range(1, 6):
        return score, _DECISION_FOR_SCORE[score], normalized.cost, [
            "baseline metadata normalized from Markdown; verdict derived from frozen score mapping"
        ]
    if raw_verdict in _VALID_VERDICTS:
        return _SCORE_FOR_DECISION[raw_verdict], raw_verdict, normalized.cost, [
            "baseline metadata normalized from Markdown; score derived from frozen verdict mapping"
        ]
    raise ValueError(
        f"baseline Markdown contained no recoverable score or verdict: "
        f"score={raw_score!r}, verdict={raw_verdict!r}"
    )


_USER = (
    "Manuscript title: {title}\n\n"
    "Review this manuscript and produce your final verdict as the single "
    "responsible reviewer. Weigh methodology, novelty, rigor, clarity, and "
    "reproducibility together into one holistic judgment. Map your verdict to "
    "the score scale: 5=accept, 4=minor revision, 3=major revision, "
    "1-2=reject."
)


def _model_slug(config: dict[str, Any]) -> str:
    model = str(
        config.get("reasoning_model")
        or config.get("model")
        or config.get("fast_model")
        or "unknown"
    )
    return re.sub(r"[^a-z0-9]+", "-", model.lower()).strip("-") or "unknown"


def baseline_runs_path(eval_dir: str, config: dict[str, Any]) -> str:
    """Model-namespaced baseline runs file, e.g. runs_baseline_openrouter-owl-alpha.jsonl."""
    import os

    return os.path.join(eval_dir, f"runs_baseline_{_model_slug(config)}.jsonl")


def single_llm_review(
    item,
    rep: int,
    config: dict[str, Any],
    leakage_note: str,
    *,
    corpus_sha256: str = "",
) -> RunRecord:
    """One LLM call over one paper, returned as a pipeline-comparable RunRecord."""
    manifest = build_manifest(config, venue=item.venue, leakage_note=leakage_note)
    md = manifest.to_dict()
    md["mode"] = "single-llm"  # so a run file is never mistaken for a full-system one
    md["corpus_sha256"] = corpus_sha256
    t0 = time.time()
    try:
        # Reuse the pipeline's own ingestion + conditioning so the single call
        # sees exactly what the panel would: same parsed manuscript, same
        # journal / article-type / strictness prompt blocks.
        state = PeerReviewGraph(config).initial_state(item.pdf_path)
        require_readable(state.get("ingest"), config)
        llm = make_llm(config, agent="baseline", default_tag="synthesis")
        result = invoke_markdown(
            llm,
            config,
            _SYS,
            _USER.format(title=state.get("manuscript_title", "Untitled")),
            cached_prefix=context_block(state),
            min_chars=200,
        )
        score, decision, metadata_cost, warnings = _parse_metadata(
            llm, config, result.text,
        )
    except Exception as exc:  # noqa: BLE001
        return RunRecord(
            paper_id=item.id, repeat=rep, ok=False,
            system_decision=None, system_weighted_score=None,
            errors=[f"single-llm failed: {exc}"],
            latency_s=round(time.time() - t0, 1), manifest=md,
        )

    record = RunRecord(
        paper_id=item.id, repeat=rep,
        ok=bool(decision),
        system_decision=decision,
        # The "weighted score" of a one-reviewer panel is just its score; this
        # keeps the field identical in meaning to the full system's so metrics
        # (Spearman/Pearson vs human ratings) compares like with like.
        system_weighted_score=float(score),
        per_reviewer=[{
            "name": "single_llm",
            "score": score,
            "confidence": 5,
            "markdown": result.text,
            "score_source": "explicit" if not warnings else "normalized",
        }],
        decision_letter=result.text,
        n_reviewers=1,
        cost_usd=round(float(result.cost + metadata_cost), 4),
        latency_s=round(time.time() - t0, 1),
        errors=warnings,
        manifest=md,
    )
    integrity_errors = inspect_run_artifacts(record)
    return replace(
        record,
        ok=record.ok and not integrity_errors,
        artifact_integrity_ok=not integrity_errors,
        artifact_integrity_errors=integrity_errors,
    )


def run_baseline_batch(
    corpus_path: str,
    runs_path: str,
    config: dict[str, Any],
    *,
    repeats: int = 1,
    only: list[str] | None = None,
    leakage_note: str = "",
    verbose: bool = True,
) -> int:
    """Ensure each selected paper has ``repeats`` single-LLM runs in ``runs_path``.

    Mirrors :func:`peerreviewagents.eval.runner.run_batch` (same resume
    semantics) but swaps the full graph for one LLM call.
    """
    from .corpus import verify_corpus_manifest

    corpus_manifest = verify_corpus_manifest(corpus_path, warn_missing=True)
    verify_protocol(corpus_path, config)
    corpus_sha256 = corpus_manifest.get("corpus_sha256") if corpus_manifest else ""
    corpus = load_corpus(corpus_path)
    if only:
        wanted = set(only)
        corpus = [c for c in corpus if c.id in wanted]
    if not corpus:
        print("No corpus papers selected; nothing to do.")
        return 0

    done = existing_keys(
        runs_path,
        config=config,
        mode="single-llm",
        corpus_sha256=corpus_sha256 or None,
    )
    todo = [
        (item, rep)
        for item in corpus
        for rep in range(repeats)
        if (item.id, rep) not in done
    ]
    if not todo:
        print(f"All {len(corpus)} paper(s) already have {repeats} baseline run(s). Nothing to do.")
        return 0

    print(f"[single-llm] Running {len(todo)} new run(s) across {len(corpus)} paper(s) "
          f"(repeats={repeats}); {len(done)} already on disk.\n"
          f"  -> {runs_path}")

    performed = 0
    for item, rep in todo:
        if verbose:
            print(f"\n→ {item.id} repeat {rep}: {item.title[:70]}")
        record = single_llm_review(
            item, rep, config, leakage_note, corpus_sha256=corpus_sha256,
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
