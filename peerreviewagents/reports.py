"""Write per-run review artifacts to disk."""

from __future__ import annotations

import datetime as _dt
import os
import re

from .agents.utils.agent_states import ReviewState

_VERDICT_LABEL = {
    "accept": "Accept",
    "minor": "Minor Revision",
    "major": "Major Revision",
    "reject": "Reject",
}


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (s or "manuscript")[:50]


def write_reports(state: ReviewState) -> str:
    config = state["config"]
    ts = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = os.path.join(config["output_dir"], f"{ts}-{_slug(state.get('manuscript_title', ''))}")
    os.makedirs(run_dir, exist_ok=True)

    for r in state.get("reports", []):
        _write(run_dir, f"review_{r['reviewer']}.md", r["body"])

    if state.get("debate"):
        transcript = "\n\n".join(
            f"## {t['role'].title()} — round {t['round']}\n\n{t['content']}" for t in state["debate"]
        )
        _write(run_dir, "debate_transcript.md", f"# Debate Transcript\n\n{transcript}")

    if state.get("meta_review"):
        _write(run_dir, "meta_review.md", state["meta_review"])
    if state.get("author_rebuttal"):
        _write(run_dir, "author_rebuttal.md", state["author_rebuttal"])
    if state.get("decision_letter"):
        _write(run_dir, "decision_letter.md", state["decision_letter"])

    _write(run_dir, "summary.md", _summary(state))

    return run_dir


def _summary(state: ReviewState) -> str:
    decision = state.get("decision", "n/a")
    label = _VERDICT_LABEL.get(decision, decision)
    lines = [
        f"# Review Summary — {state.get('manuscript_title', 'Untitled')}",
        "",
        f"**Decision:** {label}",
        "",
        "## Reviewer Scores",
    ]
    for r in state.get("reports", []):
        lines.append(f"- **{r['reviewer']}** — score {r['score']}/5 (confidence {r['confidence']}/5)")
    avg = _avg(state)
    if avg is not None:
        lines.append("")
        lines.append(f"**Average reviewer score:** {avg:.2f}/5")
    cost = state.get("total_cost")
    if cost is not None and cost > 0:
        lines.append("")
        lines.append(f"**OpenRouter cost:** ${cost:.4f}")
    if state.get("errors"):
        lines += ["", "## Run Warnings"] + [f"- {e}" for e in state["errors"]]
    return "\n".join(lines)


def _avg(state: ReviewState):
    scores = [r["score"] for r in state.get("reports", [])]
    return sum(scores) / len(scores) if scores else None


def _write(run_dir: str, name: str, content: str) -> None:
    with open(os.path.join(run_dir, name), "w", encoding="utf-8") as fh:
        fh.write(content)
