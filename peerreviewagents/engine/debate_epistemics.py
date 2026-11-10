"""Conservative epistemic audit records for additive manuscript debate."""
from __future__ import annotations

import copy
import json

from .storage import digest


AUDIT_VERSION = "debate-epistemic-audit-v1"
_HISTORY_TEXT = (
    "decision_id", "declared_scientific_status", "presentation",
    "strongest_criticism", "strongest_author_defense", "decisive_question",
    "decision_reason",
)


def _decision_id(decision: dict) -> str:
    material = {key: decision.get(key) for key in (
        "issue_id", "scientific_status", "presentation", "strongest_criticism",
        "strongest_author_defense", "decisive_question", "decision_reason",
        "evidence", "components",
    )}
    return "decision-" + digest(json.dumps(material, ensure_ascii=False, sort_keys=True))[:16]


def decision_snapshot(decision: dict, *, inherited: bool) -> dict:
    """Flatten one decision so later rounds do not recursively embed packages."""
    return {
        "decision_id": _decision_id(decision),
        "inherited": inherited,
        "declared_scientific_status": decision.get("scientific_status", "unresolved"),
        "presentation": decision.get("presentation", "omitted"),
        "strongest_criticism": decision.get("strongest_criticism", ""),
        "strongest_author_defense": decision.get("strongest_author_defense", ""),
        "decisive_question": decision.get("decisive_question", ""),
        "decision_reason": decision.get("decision_reason", ""),
    }


def inherited_history(payload: dict) -> dict[str, list[dict]]:
    """Read prior flat audit history from a package, accepting legacy absence."""
    audit = payload.get("epistemic_audit")
    if not isinstance(audit, dict) or not isinstance(audit.get("issues"), list):
        return {}
    result = {}
    for issue in audit["issues"]:
        if not isinstance(issue, dict) or not isinstance(issue.get("issue_id"), str):
            continue
        history = issue.get("decision_history", [])
        if isinstance(history, list) and all(isinstance(row, dict) for row in history):
            flat = []
            for row in history:
                normalized = {key: row.get(key, "") for key in _HISTORY_TEXT}
                if not all(isinstance(value, str) for value in normalized.values()):
                    continue
                normalized["inherited"] = True
                flat.append(normalized)
            result[issue["issue_id"]] = flat
    return result


def _assertion_status(issue: dict, decision: dict) -> str:
    """Classify the assertion without using its kept or dropped disposition."""
    status = decision.get("scientific_status")
    if issue.get("source_kind") == "discarded_hypothesis":
        if status == "contradicted":
            return "hypothesis_with_recorded_counterevidence"
        return "unverified_hypothesis"
    return {
        "supported_concern": "source_linked_concern",
        "supported_clarification": "source_linked_clarification",
        "contradicted": "concern_with_recorded_counterevidence",
        "unresolved": "unresolved_concern",
    }.get(status, "unresolved_concern")


def _selection_status(decision: dict) -> str:
    selected = any(component.get("treatment") == "preserved"
                   for component in decision.get("components", []))
    return "selected" if selected else "not_selected"


def _unique_text(values) -> list[str]:
    result = []
    for value in values:
        if isinstance(value, str) and value.strip() and value.strip() not in result:
            result.append(value.strip())
    return result


def build_epistemic_audit(value: dict, ledger: dict) -> dict:
    """Build an inspectable audit after structural source checks have passed.

    Exact quotation checks establish traceability only. They do not establish
    that a criticism, defense, or interpretation is scientifically correct.
    """
    issues = {issue["issue_id"]: issue for issue in ledger["issues"]}
    records = []
    for decision in value["decisions"]:
        issue = issues[decision["issue_id"]]
        history = copy.deepcopy(issue.get("decision_history", []))
        if not history and isinstance(issue.get("prior_decision"), dict):
            history.append(decision_snapshot(issue["prior_decision"], inherited=True))
        current = decision_snapshot(decision, inherited=False)
        known = {row.get("decision_id") for row in history}
        if current["decision_id"] not in known:
            history.append(current)

        decision_refs = len(decision.get("evidence", []))
        component_refs = sum(len(component.get("evidence", []))
                             for component in decision.get("components", []))
        selected_numbers = {component.get("final_finding_number")
                            for component in decision.get("components", [])
                            if component.get("treatment") == "preserved"}
        finding_anchors = len({number for number in selected_numbers
                               if type(number) is int and 1 <= number <= len(value["findings"])})
        finding = issue.get("original_finding") or {}
        qualifications = _unique_text([
            finding.get("counterevidence"),
            *(row.get("strongest_author_defense") for row in history),
        ])
        records.append({
            "issue_id": decision["issue_id"],
            "origin": ("discarded_hypothesis" if issue.get("source_kind") == "discarded_hypothesis"
                       else "candidate_finding"),
            "assertion_status": _assertion_status(issue, decision),
            "selection_status": _selection_status(decision),
            "declared_scientific_status": decision["scientific_status"],
            "presentation": decision["presentation"],
            "source_link_status": ("exact_quotes_validated"
                                   if finding_anchors + decision_refs + component_refs
                                   else "no_exact_quote"),
            "source_reference_counts": {
                "finding_anchors": finding_anchors,
                "decision": decision_refs,
                "components": component_refs,
            },
            "scientific_truth_status": "not_mechanically_determined",
            "restored_from_hypothesis": (
                issue.get("source_kind") == "discarded_hypothesis"
                and _selection_status(decision) == "selected"
            ),
            "qualifications": qualifications,
            "decision_history": history,
        })
    return {
        "version": AUDIT_VERSION,
        "issues": records,
        "limitations": [
            "Exact quotations establish traceability, not scientific truth.",
            "Selection records editorial inclusion, not confirmation of a defect.",
            "A restored hypothesis remains unverified unless independent scientific assessment establishes more.",
            "Statuses summarize model decisions and deterministic checks. They are not expert adjudication.",
        ],
    }
