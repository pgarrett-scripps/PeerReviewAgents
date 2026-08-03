"""Write per-run review artifacts to disk."""

from __future__ import annotations

import datetime as _dt
import os
import re

from .agents.utils.agent_states import ReviewState
from .observability import cache_totals

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

    for a in state.get("audits", []):
        _write(run_dir, f"audit_{a['auditor']}.md", a["body"])

    if state.get("debate"):
        transcript = "\n\n".join(
            f"## {t['role'].title()} — round {t['round']}\n\n{t['content']}" for t in state["debate"]
        )
        _write(run_dir, "debate_transcript.md", f"# Debate Transcript\n\n{transcript}")

    if state.get("integrity"):
        _write(run_dir, "integrity.md", state["integrity"])
    if state.get("response_verification"):
        _write(run_dir, "author_response_verification.md", state["response_verification"])
    if state.get("desk_screen"):
        _write(run_dir, "desk_screen.md", state["desk_screen"])
    if state.get("meta_review"):
        _write(run_dir, "meta_review.md", state["meta_review"])
    if state.get("author_rebuttal"):
        _write(run_dir, "author_rebuttal.md", state["author_rebuttal"])
    if state.get("decision_letter"):
        _write(run_dir, "decision_letter.md", state["decision_letter"])
    if state.get("journal_recommendations"):
        _write(run_dir, "journal_recommendations.md", state["journal_recommendations"])

    _write(run_dir, "summary.md", _summary(state))
    _write_round_record(state, run_dir)

    return run_dir


def _write_round_record(state: ReviewState, run_dir: str) -> None:
    """Write ``round.json`` — what makes this run revisable.

    Best-effort: a review whose artifacts are on disk should not fail at the
    last step because the record could not be built. The cost of skipping it
    is that this run cannot be used as the basis of a revision round, which
    the next run reports clearly.
    """
    from . import rounds

    try:
        record = rounds.build_from_state(
            state,
            job_id=os.path.basename(run_dir.rstrip(os.sep)),
            cache_key=_manuscript_cache_key(state),
        )
        rounds.save(record, run_dir)
    except Exception:  # noqa: BLE001
        pass


def _manuscript_cache_key(state: ReviewState) -> str:
    """Ingest cache key of the reviewed manuscript, for the next round's diff."""
    from .ingest import cache

    path = state.get("manuscript_path") or ""
    if not path:
        return ""
    try:
        return cache.cache_key(path, state.get("config") or {})
    except OSError:
        return ""


def _summary(state: ReviewState) -> str:
    decision = state.get("decision", "n/a")
    label = _VERDICT_LABEL.get(decision, decision)
    lines = [
        f"# Review Summary — {state.get('manuscript_title', 'Untitled')}",
        "",
        f"**Decision:** {label}",
    ]
    venue = _target_venue(state)
    if venue:
        lines.append(f"**Target venue:** {venue}")
    article_type = _article_type_line(state)
    if article_type:
        lines.append(f"**Manuscript type:** {article_type}")
    strictness = _strictness_line(state)
    if strictness:
        lines.append(f"**Review strictness:** {strictness}")
    prior = state.get("prior_round")
    if prior is not None:
        lines.append(f"**Round:** {prior.round + 1} (revision of {prior.job_id})")
        if prior.weighted_score is not None:
            lines.append(f"**Previous decision:** {_VERDICT_LABEL.get(prior.decision, prior.decision)}")
    integrity = _integrity_line(state)
    if integrity:
        lines.append(f"**Submission integrity:** {integrity}")
    if state.get("desk_rejected"):
        lines.append("**Outcome:** Desk reject (screened before full review)")
        return "\n".join(lines)
    lines += [
        "",
        "## Reviewer Scores",
    ]
    for r in state.get("reports", []):
        if isinstance(r.get("score"), (int, float)):
            lines.append(
                f"- **{r['reviewer']}** — score {r['score']}/5 "
                f"(confidence {r['confidence']}/5)"
            )
        else:
            # Named rather than dropped: that a dimension did not apply is a
            # fact about the paper, and omitting the row would leave a reader
            # counting seven reports and wondering which one failed.
            reason = str(r.get("not_applicable_reason") or "").strip()
            lines.append(
                f"- **{r['reviewer']}** — not applicable"
                + (f" ({reason})" if reason else "")
                + ", excluded from the mean"
            )
    avg = _avg(state)
    if avg is not None:
        lines.append("")
        lines.append(f"**Average reviewer score:** {avg:.2f}/5")
    audits = state.get("audits", [])
    if audits:
        lines += ["", "## Editorial Audits"]
        for a in audits:
            lines.append(
                f"- **{a.get('title', a['auditor'])}** — "
                f"{a.get('hard_gaps', 0)} HARD gap(s), {a.get('soft_gaps', 0)} SOFT gap(s)"
            )
    cost = state.get("total_cost")
    if cost is not None and cost > 0:
        lines.append("")
        # Not "OpenRouter cost": the default provider is Anthropic direct, and
        # on that route this is an estimate from the pricing table rather than
        # a figure the vendor reported.
        lines.append(f"**Estimated cost:** ${cost:.4f}")
    cache_line = _cache_line(state)
    if cache_line:
        lines.append(cache_line)
    if state.get("errors"):
        lines += ["", "## Run Warnings"] + [f"- {e}" for e in state["errors"]]
    return "\n".join(lines)


def _cache_line(state: ReviewState) -> str:
    """One line on how the prompt cache behaved, or '' when it did not engage.

    The manuscript is sent to every agent as a shared cached prefix, so this
    is the ratio that decides what a review costs — and it is not visible in
    the cost figure above, where a cache read and a full-price token look the
    same. Reported as tokens rather than dollars because the panel spans model
    tiers and a token count is the thing you can compare across runs.
    """
    read, written = cache_totals(state["config"].get("run_id", ""))
    if not read and not written:
        return ""
    total = read + written
    return (
        f"**Prompt cache:** {read:,} tokens read, {written:,} written "
        f"({read / total:.0%} served from cache)"
    )


def _integrity_line(state: ReviewState) -> str:
    """One-line integrity verdict for the summary, or '' when nothing was found.

    Read off the rendered report's Outcome line rather than re-scanning, so
    the summary can never disagree with ``integrity.md``.
    """
    body = state.get("integrity") or ""
    for line in body.splitlines():
        if line.startswith("**Outcome:**"):
            return f"{line[len('**Outcome:**'):].strip()} — see integrity.md"
    return ""


def _target_venue(state: ReviewState) -> str:
    """Human-readable name of the target journal, or '' if none/unresolvable."""
    slug = (state.get("config") or {}).get("target_journal")
    if not slug:
        return ""
    try:
        from .journals import load_journal

        profile = load_journal(slug, state.get("config"))
    except Exception:  # noqa: BLE001 — never let report writing fail on this
        return slug
    return profile.name if profile else slug


def _article_type_line(state: ReviewState) -> str:
    """Human-readable manuscript type for the run, or '' if none/unresolvable."""
    raw = (state.get("config") or {}).get("article_type")
    try:
        from .article_types import article_type_label, normalize_article_type

        key = normalize_article_type(raw)
    except Exception:  # noqa: BLE001 — never let report writing fail on this
        return ""
    return article_type_label(key) if key else ""


def _strictness_line(state: ReviewState) -> str:
    """'Label (N/5)' for the run's review strictness, or '' if unresolvable."""
    raw = (state.get("config") or {}).get("review_strictness")
    if raw is None:
        return ""
    try:
        from .strictness import MAX_LEVEL, normalize_strictness, strictness_label

        level = normalize_strictness(raw)
    except Exception:  # noqa: BLE001 — never let report writing fail on this
        return ""
    return f"{strictness_label(level)} ({level}/{MAX_LEVEL})"


def _avg(state: ReviewState):
    # Reviewers that found nothing in their remit return a null score. They
    # are left out rather than counted, so a paper is neither rewarded nor
    # penalised for not having, say, a statistics section at all.
    scores = [
        r["score"] for r in state.get("reports", [])
        if isinstance(r.get("score"), (int, float))
    ]
    return sum(scores) / len(scores) if scores else None


def _write(run_dir: str, name: str, content: str) -> None:
    with open(os.path.join(run_dir, name), "w", encoding="utf-8") as fh:
        fh.write(content)
