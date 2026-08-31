"""Independent, schema-free integrity checks for saved evaluation artifacts.

These checks deliberately inspect the durable Markdown after a run.  A graph
that reached an editor decision is not a successful publication-study run when
one of its reviews is blank, duplicated, or contains an echoed orchestration
prompt.  Keeping this check outside the generation path also prevents the
model from grading its own output.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .schema import RunRecord


MIN_ARTIFACT_CHARS = 80
EXPECTED_REVIEWERS = {
    "clarity",
    "data_analysis",
    "ethics",
    "literature",
    "methodology",
    "novelty",
    "reproducibility",
    "rigor",
}
EXPECTED_CONDENSED_REVIEWERS = {
    "contribution_context",
    "data_analysis",
    "ethics",
    "reporting_reproducibility",
    "scientific_validity",
}
EXPECTED_REVIEWER_PANELS = {
    "full": EXPECTED_REVIEWERS,
    "condensed": EXPECTED_CONDENSED_REVIEWERS,
}
EXPECTED_AUDITORS = {"citation_integrity", "methods_completeness"}

# These strings are boundaries used by orchestration prompts.  Their presence
# in authored output is evidence of prompt echo, not a stylistic disagreement.
INTERNAL_ECHO_MARKERS = (
    "=== MANUSCRIPT ===",
    "=== END MANUSCRIPT ===",
    "=== BEGIN OUTPUT ===",
    "=== BEGIN CRITIQUE ===",
    "### FORM REQUIREMENTS:",
)


def _text_issues(label: str, text: str) -> list[str]:
    text = (text or "").strip()
    issues = []
    if len(text) < MIN_ARTIFACT_CHARS:
        issues.append(f"{label} has only {len(text)} characters")
    for marker in INTERNAL_ECHO_MARKERS:
        if marker in text:
            issues.append(f"{label} contains internal prompt marker {marker!r}")
            break
    return issues


def inspect_run_artifacts(record: RunRecord) -> list[str]:
    """Return deterministic integrity failures for one stored run.

    Each PRA panel has a frozen reviewer roster and a shared two-auditor
    contract.  The practical baseline has one holistic Markdown review.
    Failed calls may naturally have no artifacts; they remain failures without
    accumulating a second, redundant list of integrity complaints.
    """
    if not record.system_decision:
        return []

    mode = (record.manifest or {}).get("mode", "system")
    issues: list[str] = []
    reviews = record.per_reviewer or []
    names = [str(review.get("name") or "") for review in reviews]

    if mode == "system":
        found = set(names)
        panel = str((record.manifest or {}).get("reviewer_panel") or "").lower()
        if panel not in EXPECTED_REVIEWER_PANELS:
            # Backward compatibility for records written before panel identity
            # was included in the manifest.  A condensed-only role is an
            # unambiguous signature; otherwise preserve the historical full
            # panel contract rather than guessing from a merely incomplete run.
            panel = (
                "condensed"
                if found & (EXPECTED_CONDENSED_REVIEWERS - EXPECTED_REVIEWERS)
                else "full"
            )
        expected_reviewers = EXPECTED_REVIEWER_PANELS[panel]
        missing = sorted(expected_reviewers - found)
        extra = sorted(found - expected_reviewers)
        if missing:
            issues.append("missing reviewers: " + ", ".join(missing))
        if extra:
            issues.append("unexpected reviewers: " + ", ".join(extra))
        if len(names) != len(set(names)):
            issues.append("duplicate reviewer artifacts")
        if record.n_reviewers != len(expected_reviewers):
            issues.append(
                f"reported {record.n_reviewers} reviewers; expected "
                f"{len(expected_reviewers)} for {panel} panel"
            )

        audits = record.audit_markdown or []
        audit_names = {str(audit.get("auditor") or "") for audit in audits}
        missing_auditors = sorted(EXPECTED_AUDITORS - audit_names)
        if missing_auditors:
            issues.append("missing auditors: " + ", ".join(missing_auditors))
        for audit in audits:
            label = f"audit {audit.get('auditor') or '?'}"
            issues.extend(_text_issues(label, str(audit.get("markdown") or "")))

        issues.extend(_text_issues("decision letter", record.decision_letter))
    elif mode == "single-llm":
        if names != ["single_llm"]:
            issues.append("single-LLM condition must contain exactly one review artifact")

    for review in reviews:
        label = f"review {review.get('name') or '?'}"
        issues.extend(_text_issues(label, str(review.get("markdown") or "")))
    return issues
