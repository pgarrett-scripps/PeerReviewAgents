"""Single-LLM baseline: the degenerate one-call ablation of the full board.

The whole thesis of PeerReviewAgents is that *structure* (a fan-out panel,
a debate, a rebuttal, an editor) beats a single "review this paper" call.
This module makes that comparison measurable: it reviews each corpus paper
with exactly one LLM call that collapses the panel + editor into a single
holistic verdict — one reviewer, zero debate rounds, no rebuttal — and
writes the same :class:`RunRecord` shape the full pipeline does, so the
existing ``metrics`` / ``figure`` harness scores it unchanged.

It is *not* a strawman: the single call sees the identical manuscript and
the identical venue / article-type / strictness conditioning the panel
gets (via :func:`context_block` over the same initial state), so the only
variable removed is the multi-agent structure itself.

Runs land in a separate, model-namespaced file (``runs_baseline_<model>.jsonl``)
so several models can be benchmarked side by side without colliding on the
``(paper_id, repeat)`` resume key.
"""

from __future__ import annotations

import re
import time
from typing import Any

from pydantic import BaseModel, Field

from ..agents.schemas import Verdict
from ..agents.utils.agent_utils import context_block
from ..agents.utils.llm import make_llm
from ..agents.utils.structured import invoke_structured
from ..graph.review_graph import PeerReviewGraph
from .corpus import load_corpus
from .runner import existing_keys
from .schema import RunRecord, append_jsonl, build_manifest

_VALID_VERDICTS = ("accept", "minor", "major", "reject")


class BaselineReviewOutput(BaseModel):
    """One holistic single-LLM verdict — the panel and editor collapsed into one."""

    score: int = Field(
        ..., ge=1, le=5,
        description="1=reject, 2=major-reject, 3=major-revision, "
                    "4=minor-revision, 5=accept.",
    )
    decision: Verdict = Field(
        ..., description="Final verdict: accept | minor | major | reject.",
    )
    rationale: str = Field(
        ..., description="One-paragraph holistic justification for the score and verdict.",
    )


_SYS = (
    "You are a single expert peer reviewer acting alone. There is no review "
    "panel, no debate, no author rebuttal, and no separate editor: you read "
    "the manuscript once and render the final verdict yourself. If a target "
    "venue, manuscript type, or review-strictness standard is described in the "
    "context above, judge the manuscript against it. Return the structured "
    "BaselineReviewOutput schema (an integer 1-5 score, a final verdict, and a "
    "brief rationale)."
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


def single_llm_review(item, rep: int, config: dict[str, Any], leakage_note: str) -> RunRecord:
    """One LLM call over one paper, returned as a pipeline-comparable RunRecord."""
    manifest = build_manifest(config, venue=item.venue, leakage_note=leakage_note)
    md = manifest.to_dict()
    md["mode"] = "single-llm"  # so a run file is never mistaken for a full-system one
    t0 = time.time()
    try:
        # Reuse the pipeline's own ingestion + conditioning so the single call
        # sees exactly what the panel would: same parsed manuscript, same
        # journal / article-type / strictness prompt blocks.
        state = PeerReviewGraph(config).initial_state(item.pdf_path)
        llm = make_llm(config, agent="baseline", default_tag="synthesis", reasoning_effort="high")
        result = invoke_structured(
            llm,
            BaselineReviewOutput,
            config,
            _SYS,
            _USER.format(title=state.get("manuscript_title", "Untitled")),
            cached_prefix=context_block(state),
        )
    except Exception as exc:  # noqa: BLE001
        return RunRecord(
            paper_id=item.id, repeat=rep, ok=False,
            system_decision=None, system_weighted_score=None,
            errors=[f"single-llm failed: {exc}"],
            latency_s=round(time.time() - t0, 1), manifest=md,
        )

    out: BaselineReviewOutput = result.instance  # type: ignore[assignment]
    decision = out.decision if out.decision in _VALID_VERDICTS else None
    return RunRecord(
        paper_id=item.id, repeat=rep,
        ok=bool(decision),
        system_decision=decision,
        # The "weighted score" of a one-reviewer panel is just its score; this
        # keeps the field identical in meaning to the full system's so metrics
        # (Spearman/Pearson vs human ratings) compares like with like.
        system_weighted_score=float(out.score),
        per_reviewer=[{"name": "single_llm", "score": out.score, "confidence": 5}],
        n_reviewers=1,
        cost_usd=round(float(result.cost), 4),
        latency_s=round(time.time() - t0, 1),
        errors=[],
        manifest=md,
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
        print(f"All {len(corpus)} paper(s) already have {repeats} baseline run(s). Nothing to do.")
        return 0

    print(f"[single-llm] Running {len(todo)} new run(s) across {len(corpus)} paper(s) "
          f"(repeats={repeats}); {len(done)} already on disk.\n"
          f"  -> {runs_path}")

    performed = 0
    for item, rep in todo:
        if verbose:
            print(f"\n→ {item.id} repeat {rep}: {item.title[:70]}")
        record = single_llm_review(item, rep, config, leakage_note)
        append_jsonl(runs_path, record.to_json())
        performed += 1
        if verbose:
            status = "ok" if record.ok else f"FAILED ({'; '.join(record.errors) or 'no decision'})"
            print(f"  {status} — decision={record.system_decision} "
                  f"score={record.system_weighted_score} "
                  f"cost=${record.cost_usd:.4f} {record.latency_s:.0f}s")
    return performed
