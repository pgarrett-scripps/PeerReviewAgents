"""Write per-run review artifacts to disk."""

from __future__ import annotations

import datetime as _dt
import os
import re

from .agents.utils.agent_states import ReviewState
from .observability import cache_totals, node_usage

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
    usage = _usage_table(state)
    if usage:
        _write(run_dir, "usage.md", usage)
    stats = _prose_report(state)
    if stats:
        _write(run_dir, "manuscript_stats.md", stats)
    _write_round_record(state, run_dir)

    return run_dir


def _usage_table(state: ReviewState) -> str:
    """Per-agent tokens, cache split and spend — or '' when nothing was billed.

    The run total in summary.md answers "what did this cost"; this answers
    "which agent should I look at", and only the second one tells you what to
    change. Working out where C-09's bill went without this meant estimating
    each stage's share from prompt sizes by hand, which is guessing dressed up
    as arithmetic.

    `cached` is the share of that agent's input served from cache. A low
    figure on an agent that sends the manuscript means it is not sharing the
    common prefix — the thing most worth catching here, since a cache write
    costs 12.5x a read.
    """
    rows = node_usage(state["config"].get("run_id", ""))
    if not rows:
        return ""
    lines = [
        "# Per-agent usage",
        "",
        "`cached` is the fraction of that agent's input tokens served from the",
        "prompt cache. An agent that sends the manuscript and shows a low",
        "figure is writing its own cache entry instead of sharing the common",
        "one — a write costs 12.5x what a read costs, so that is where a",
        "review's cost goes when it goes somewhere surprising.",
        "",
        "| agent | in | out | cache read | cache write | cached | $ |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for node, (tin, tout, cread, cwrite, usd) in sorted(
        rows.items(), key=lambda kv: -kv[1][4]
    ):
        share = f"{cread / tin:.0%}" if tin else "—"
        lines.append(
            f"| {node} | {tin:,} | {tout:,} | {cread:,} | {cwrite:,} | {share} | "
            f"{usd:.4f} |"
        )
    tot = [sum(r[i] for r in rows.values()) for i in range(5)]
    share = f"{tot[2] / tot[0]:.0%}" if tot[0] else "—"
    lines.append(
        f"| **total** | {int(tot[0]):,} | {int(tot[1]):,} | {int(tot[2]):,} | "
        f"{int(tot[3]):,} | {share} | {tot[4]:.4f} |"
    )
    return "\n".join(lines) + "\n"


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
    # Placed with the other provenance, above the scores: a reader weighing a
    # verdict should learn that the panel read a damaged conversion before
    # they read the verdict, not after.
    conversion = _ingest_health_line(state)
    if conversion:
        lines.append(conversion)
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


def _prose_report(state: ReviewState) -> str:
    """``manuscript_stats.md`` — deterministic counts over the parsed text.

    Facts only, and no adjective anywhere in it. These numbers exist so a
    reader can check the panel against the manuscript, and the moment one of
    them reads as a judgment ("dense prose", "poor readability") it starts
    competing with the reviewers instead of grounding them.
    """
    ingest = state.get("ingest") or {}
    stats = ingest.get("prose") or {}
    health, counts = stats.get("health") or {}, stats.get("counts") or {}
    if not counts:
        return ""

    lines = [
        f"# Manuscript Statistics — {state.get('manuscript_title', 'Untitled')}",
        "",
        "Measured deterministically at ingest, with no model involved. Every "
        "figure describes the *converted* text the panel read, not the PDF.",
        "",
        "## How the file converted",
        "",
        f"- Format: {ingest.get('format', 'unknown')} via {ingest.get('tool', 'unknown')}",
        f"- Conversion health: **{health.get('verdict', 'unknown')}**",
        f"- Fused tokens: {health.get('fused_per_1k', 0)} per 1000 words",
        f"- Hyphenated line breaks: {health.get('hyphen_breaks_per_1k', 0)} per 1000 words",
        f"- Lost sentence spaces: {health.get('missing_space_per_1k', 0)} per 1000 words",
        f"- Markdown headings emitted by the converter: {health.get('markdown_headings', 0)}",
        # Named as blocks, not paragraphs, because that is what survives:
        # across a 16-paper corpus this ranged from 2.8 to 656 words per
        # block on comparable manuscripts.
        f"- Blank-line-separated blocks: {counts.get('blocks', 0)}",
    ]
    share = health.get("preamble_share")
    if share is not None:
        lines.append(f"- Text matching no known section: {share:.0%}")
    for note in health.get("notes") or []:
        lines.append(f"- ⚠ {note}")

    main = counts.get("main_text_words")
    lines += [
        "",
        "## Size",
        "",
        f"- Words: {counts.get('words', 0):,}",
        (
            f"- Main text (excluding references): {main:,}"
            if main is not None
            else "- Main text: unavailable — the reference list could not be "
            "located, and it is typically a fifth of a paper's words"
        ),
        f"- Reference list: {counts.get('reference_words', 0):,} words",
        f"- Sentences: {counts.get('sentences', 0):,}",
        f"- Display equations: {counts.get('display_math', 0)}",
        f"- Table rows: {counts.get('table_rows', 0)}",
    ]

    density = stats.get("density")
    if not density:
        lines += [
            "",
            "## Prose",
            "",
            f"Not measured: this run compressed the manuscript "
            f"(caveman = {stats.get('caveman')}), so sentence length, hedging "
            "and lexical diversity would describe the compressor rather than "
            "the authors.",
        ]
        return "\n".join(lines) + "\n"

    style = density.get("citation_style")
    citation_line = (
        f"- Citations: {density.get('citations', 0)} ({density.get('citations_per_1k', 0)} "
        f"per 1000 words), style {style}"
        if style != "undetected"
        else "- Citations: too few detected to count reliably — this venue most "
        "likely sets them as superscript numerals, which convert to bare digits"
    )
    lines += [
        "",
        "## Prose",
        "",
        f"- Sentence length: mean {density.get('sentence_len_mean', 0)}, "
        f"median {density.get('sentence_len_median', 0)}, "
        f"90th percentile {density.get('sentence_len_p90', 0)} words",
        f"- Sentences over 40 words: {density.get('long_sentence_share', 0):.0%}",
        f"- Lexical diversity (MATTR): {density.get('mattr', 0)}",
        f"- Passive constructions: {density.get('passive_per_sentence_approx', 0)} "
        "per sentence (regex approximation)",
        "",
        "## Claims and evidence",
        "",
        citation_line,
        f"- Numbers: {density.get('numbers_per_1k', 0)} per 1000 words",
        f"- Hedging language: {density.get('hedges_per_1k', 0)} per 1000 words",
        f"- Amplifying language: {density.get('boosters_per_1k', 0)} per 1000 words",
        f"- p-values: {density.get('p_values_exact', 0)} exact, "
        f"{density.get('p_values_threshold', 0)} reported only as a threshold",
    ]
    return "\n".join(lines) + "\n"


def _ingest_health_line(state: ReviewState) -> str:
    """One summary line, only when the conversion was not clean.

    Silent on a clean read: a line saying nothing went wrong on every run
    teaches the reader to skip the place the warning would appear.
    """
    health = ((state.get("ingest") or {}).get("prose") or {}).get("health") or {}
    verdict = health.get("verdict")
    if not verdict or verdict == "clean":
        return ""
    notes = "; ".join(health.get("notes") or []) or "see manuscript_stats.md"
    return f"**Manuscript conversion:** {verdict} — {notes}"


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
