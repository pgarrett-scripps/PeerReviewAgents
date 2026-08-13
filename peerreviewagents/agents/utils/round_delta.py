"""Round-over-round delta: the numbers a revision decision must not drift from.

:func:`~.agent_utils.score_summary` exists because prose agents downstream of
the reviewers quietly lose the panel's arithmetic. A revision round makes that
worse, not better: the editor is now asked to compare two rounds, and "the
manuscript improved" is exactly the kind of judgment a model will assert from
the *tone* of a response letter while the scores sat still. So the comparison
is computed here, deterministically and without an LLM, and injected as one
compact block the editor has to argue against rather than around.

Everything in it comes from state the pipeline already carries: the previous
:class:`~peerreviewagents.rounds.RoundRecord`, this round's reports, and the
revision-compliance audit. Nothing is inferred.

One thing changed shape when the panel was blinded. The per-reviewer scores
here are now a *fresh independent sample* — eight specialists who were not
told there was a previous round — compared against what the same eight said
last time. Movement between them is real information about the manuscript
plus ordinary sampling noise, and the editor's prompt says so. What it is
not, any more, is a reviewer's own account of whether it got what it asked
for; that account lives on the compliance line, which is the pipeline's
whole round-over-round memory.

The block is deliberately tolerant of missing pieces. Its inputs are written
by three sibling tracks (reviewers, compliance auditor, response verifier),
each of which can legitimately produce less than the full picture — a
fail-open verifier, an auditor that errored out, a reviewer that dropped from
the panel. A line whose signal is absent is omitted; the block never guesses
and never raises, because an editor prompt that crashes is strictly worse than
one that is a line shorter.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from ..schemas import OPEN_COMPLIANCE_STATUSES

if TYPE_CHECKING:
    from .agent_states import ReviewState

# Compliance statuses in the order a reader wants them: best outcome first,
# "we could not tell" last, and the code-assigned demotion after it. Matches
# RevisionComplianceOutput.ComplianceStatus.
_STATUS_ORDER = (
    "addressed", "partial", "not_addressed", "rebutted", "unverifiable",
    "unsubstantiated",
)

# `RevisionComplianceOutput.to_markdown()` renders each finding as
# `- **[R1-03] partial** **[blocking]**`. Reading it back is the fallback path,
# not the plan: we prefer structured findings promoted onto the audit entry and
# only parse the rendered body when the auditor didn't promote any.
_FINDING_LINE = re.compile(
    r"^\s*-\s+\*\*\[(?P<id>[^\]]+)\]\s+(?P<status>[a-z_]+)\*\*(?P<rest>.*)$"
)
_BLOCKING_FLAG = "[blocking]"

# The audit-lane entry this module reads; the other auditors are the editor's
# business, not the delta's.
_COMPLIANCE_AUDITOR = "revision_compliance"


def round_delta(state: ReviewState) -> str:
    """Render the previous round vs. this one, or '' on a first-round run.

    A first round has nothing to compare against, so callers can splice the
    result in unconditionally and get the untouched first-round prompt back.
    """
    prior = state.get("prior_round")
    if prior is None:
        return ""

    lines = [
        _round_line(prior, state),
        _score_line(prior, state),
    ]
    for optional in (
        _resubmission_line(prior, state),
        _per_reviewer_line(prior, state),
        _compliance_line(prior, state),
    ):
        if optional:
            lines.append(optional)
    return "\n".join(lines)


# --- individual lines -------------------------------------------------------


def _round_line(prior: Any, state: ReviewState) -> str:
    """Which round this is, how many remain, and what the last one decided.

    The budget half matters as much as the count: without being told that no
    further round is available, an editor will keep writing "revise and
    resubmit" forever, which is the failure mode a round cap exists to make
    visible rather than the neutral outcome it looks like from inside round N.
    """
    round_no = _int(_get(prior, "round", 1), 1) + 1
    parts = [f"This is round {round_no}"]

    max_rounds = _int((state.get("config") or {}).get("max_rounds"), 0)
    if max_rounds:
        remaining = max_rounds - round_no
        if remaining > 0:
            noun = "round" if remaining == 1 else "rounds"
            parts.append(
                f"of at most {max_rounds}; {remaining} further revision {noun} "
                f"remain{'s' if remaining == 1 else ''} after this one."
            )
        else:
            parts.append(
                f"of at most {max_rounds}; no further revision round is available, "
                "so this decision is the last one this review can make."
            )
    else:
        parts.append("of this manuscript's review.")

    line = " ".join(parts)
    decision = str(_get(prior, "decision", "") or "")
    if decision:
        line += f" The round-{round_no - 1} decision was '{decision}'."
    return line


def _score_line(prior: Any, state: ReviewState) -> str:
    """Weighted panel score then vs. now — the headline the prose must match."""
    now = _weighted(state.get("reports") or [])
    then = _float_or_none(_get(prior, "weighted_score", None))

    if now is None:
        return "Weighted panel score: no reviewer scores were produced this round."
    if then is None:
        return f"Weighted panel score: {now:.2f}/5 this round (the previous round recorded none)."
    delta = now - then
    return (
        f"Weighted panel score: {then:.2f}/5 -> {now:.2f}/5 ({delta:+.2f})."
    )


def _resubmission_line(prior: Any, state: ReviewState) -> str:
    """Says so when this round's file is byte-for-byte the previous round's.

    The one fact about "what changed" that needs no conversion, no second
    parse and no model: two sha256s of two files. It replaced a section-aware
    diff, which was informative only in a narrow band — a trivial revision
    reads as "nothing changed", which the hash says for free, and a real
    revision reads as "everything changed", which says nothing at all.

    Emitted only on a match. A file that differs proves very little: a
    re-export of an unedited document differs in every byte, so announcing
    "the file changed" would put a change claim in front of the editor that
    nothing checked. Equality is the only direction that carries a fact.

    The wording is careful about what the fact means. An identical
    resubmission means no ask can have been met by a change to the text; it
    does NOT mean the authors defied anybody. This pipeline reviews whatever
    draft an archive serves it, and an editor told "nothing changed" without
    that caveat has previously rejected a paper for "disregard for the review
    process" that no human had ever resubmitted.
    """
    now = str((state.get("ingest") or {}).get("file_sha256") or "")
    then = str(_get(prior, "manuscript_file_sha256", "") or "")
    if not now or not then or now != then:
        return ""
    return (
        "Manuscript file: byte-identical to the draft the previous round "
        "reviewed (same sha256). No required revision can have been met by a "
        "change to the text, because the text is the same text. This is a "
        "fact about the file and NOT evidence of bad faith — the pipeline "
        "reviews the draft it is given, and an unchanged resubmission is not "
        "defiance of an editor. Judge the paper, and do not escalate the "
        "verdict over it."
    )


def _per_reviewer_line(prior: Any, state: ReviewState) -> str:
    """Per-reviewer movement, so a flat average that hides a split is visible.

    Reviewers are matched by name across rounds. A name on only one side is
    reported as such rather than dropped: a reviewer who joined this round
    has no earlier verdict to compare, and one that is missing this round
    left a point of comparison unfilled — both change how much the average
    means.

    Both sides are independent blind assessments of a manuscript, not a
    reviewer's own before-and-after. The numbers are reported without
    interpretation for that reason; the editor's prompt is where the panel's
    blindness, and therefore the sampling noise in any single row, is
    explained.
    """
    then = {
        str(_get(r, "reviewer", "")): r
        for r in (_get(prior, "reviewer_reports", []) or [])
    }
    now = {str(r.get("reviewer", "")): r for r in (state.get("reports") or [])}
    if not then and not now:
        return ""

    parts: list[str] = []
    for name, report in now.items():
        score = _float_or_none(report.get("score"))
        before = _float_or_none(_get(then.get(name), "score", None)) if name in then else None
        if score is None:
            # A reviewer that produced no score this round is a fact about the
            # panel, not a row to drop — omitting the name here left the
            # editor counting reviewers against a line that was one short.
            if before is not None:
                parts.append(f"{name} {before:g} -> N/A (no score this round)")
            else:
                parts.append(f"{name} N/A (no score this round)")
        elif before is None:
            parts.append(f"{name} {score:g} (new this round)")
        else:
            parts.append(f"{name} {before:g} -> {score:g} ({score - before:+g})")
    parts += [f"{name} (no report this round)" for name in then if name not in now]
    if not parts:
        return ""
    return "Per-reviewer: " + "; ".join(parts) + "."


def _compliance_line(prior: Any, state: ReviewState) -> str:
    """How the previous round's numbered asks actually fared.

    Omitted entirely when the compliance auditor produced nothing readable —
    reporting "0 addressed" in that case would read as a damning finding when
    it only means the audit is missing.
    """
    findings = _compliance_findings(state)
    if not findings:
        return ""

    counts: dict[str, int] = {}
    for status, _blocking in findings:
        counts[status] = counts.get(status, 0) + 1
    shown = [s for s in _STATUS_ORDER if counts.get(s)]
    shown += sorted(s for s in counts if s not in _STATUS_ORDER)
    breakdown = ", ".join(f"{counts[s]} {s.replace('_', ' ')}" for s in shown)

    asked = len(_get(prior, "required_revisions", []) or []) or len(findings)
    line = (
        f"Required revisions from round {_int(_get(prior, 'round', 1), 1)} "
        f"({asked} item{'s' if asked != 1 else ''}): {breakdown}."
    )

    blocking = sum(
        1 for status, is_blocking in findings
        if is_blocking and status in OPEN_COMPLIANCE_STATUSES
    )
    if blocking:
        line += (
            f" {blocking} still-open item{'s are' if blocking != 1 else ' is'} "
            "marked blocking."
        )
    else:
        line += " No still-open item is marked blocking."

    # `unsubstantiated` is a demotion the pipeline applied, not a verdict the
    # auditor reached, so the editor is told what it means rather than left to
    # read it as one more shade of "we could not tell".
    demoted = sum(1 for status, _blocking in findings if status == "unsubstantiated")
    if demoted:
        line += (
            f" {demoted} item{'s were' if demoted != 1 else ' was'} reported "
            "addressed or partially addressed on manuscript text that could "
            "not be found in the manuscript, and "
            f"{'are' if demoted != 1 else 'is'} recorded as unsubstantiated. "
            "That is not progress; treat those items as open."
        )
    return line


# --- reading the sibling tracks' output --------------------------------------


def _compliance_findings(state: ReviewState) -> list[tuple[str, bool]]:
    """(status, blocking) for every compliance finding we can recover.

    ``AuditReport.findings`` is the source of truth — the compliance auditor
    promotes per-item outcomes precisely so this does not have to read them
    back out of prose. The body parse below is a backstop for an audit entry
    that somehow lacks them; it must never become the normal path, because a
    verdict recovered by string matching is one rendering change away from
    silently reporting zero.
    """
    for audit in state.get("audits") or []:
        if str(_get(audit, "auditor", "")) != _COMPLIANCE_AUDITOR:
            continue
        structured = _get(audit, "findings", None)
        if structured:
            return [
                (str(_get(f, "status", "") or ""), bool(_get(f, "blocking", False)))
                for f in structured
                if _get(f, "status", None)
            ]
        return _parse_findings(str(_get(audit, "body", "") or ""))
    return []


def _parse_findings(body: str) -> list[tuple[str, bool]]:
    out: list[tuple[str, bool]] = []
    for line in body.splitlines():
        match = _FINDING_LINE.match(line)
        if match:
            out.append((match.group("status"), _BLOCKING_FLAG in match.group("rest")))
    return out


# --- scalar helpers ----------------------------------------------------------


def _weighted(reports: list) -> float | None:
    """Confidence-weighted panel score.

    Must stay identical to ``rounds._weighted_score``, which produced the
    number on the other side of the comparison — a then/now pair computed two
    different ways would show movement that never happened. That includes how
    unscored dimensions are treated: a null score leaves the numerator and the
    denominator both, so a reviewer moving to N/A between rounds does not read
    as the panel changing its mind.
    """
    reports = [r for r in reports if isinstance(r.get("score"), (int, float))]
    if not reports:
        return None
    total_w = sum(_float(r.get("confidence"), 0.0) for r in reports) or 1.0
    return sum(
        _float(r.get("score"), 0.0) * _float(r.get("confidence"), 0.0) for r in reports
    ) / total_w


def _get(obj: Any, name: str, default: Any = None) -> Any:
    """Attribute or key lookup — round records are dataclasses, reports dicts."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
