"""Derive redundant presentation labels from validated component destinations."""
import copy

from .debate_revision import validate_revision


CONFLICT = "Priority conflicts with component destinations"
SUPPORTED = {"supported_concern", "supported_clarification"}


def recover(value, ledger, paper, source_cards=None):
    """Return a revalidated presentation-only recovery when the exact conflict permits it."""
    try:
        audit = validate_revision(value, ledger, paper, source_cards)
    except ValueError as error:
        original = {"status": "failure", "error": str(error)}
    else:
        return {"original": {"status": "valid"}, "recovered": None, "audit": audit,
                "changed_fields": []}
    if original["error"] != CONFLICT:
        return {"original": original, "recovered": None, "audit": None, "changed_fields": []}
    revised = copy.deepcopy(value)
    receipts = []
    for decision in revised["decisions"]:
        preserved = [component["final_finding_number"] for component in decision["components"]
                     if component["treatment"] == "preserved"]
        before = decision["presentation"]
        if before in ("supporting", "omitted") and preserved:
            after = "merged"
        elif before in ("primary", "merged") and not preserved:
            after = "supporting" if decision["scientific_status"] in SUPPORTED else "omitted"
        else:
            continue
        decision["presentation"] = after
        receipts.append({"issue_id": decision["issue_id"], "field": "presentation",
                         "before": before, "after": after,
                         "preserved_destinations": preserved})
    try:
        audit = validate_revision(revised, ledger, paper, source_cards)
    except ValueError as error:
        return {"original": original, "recovered": None,
                "audit": {"status": "failure", "error": str(error)}, "changed_fields": receipts}
    return {"original": original, "recovered": revised, "audit": audit,
            "changed_fields": receipts,
            "limitation": "Only presentation labels were derived. Source validation does not establish scientific correctness."}
