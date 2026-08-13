from .base import make_auditor_node

node = make_auditor_node(
    "citation_integrity",
    title="Citation Integrity Auditor",
    mandate=(
        "Audit whether the manuscript's citations are resolvable and whether "
        "claims attributed to prior work are actually supported by it. This is "
        "an integrity check on references, not a judgment of related-work "
        "coverage (that is the literature reviewer's job). You may use the "
        "research tools to look up cited works and check that they exist and "
        "plausibly support the claim made about them.\n\n"
        "Categories to check (only where the trigger appears):\n"
        "  - Reference resolvability: every load-bearing in-text citation maps to "
        "a reference that is specific and resolvable (DOI/PMID/full citation). "
        "Flag '(data not shown)', '(unpublished)', '(in preparation)', and "
        "dead/unresolvable references supporting a central claim (HARD). "
        "Non-load-bearing dead refs are SOFT.\n"
        "  - Claim–citation support: a specific factual/quantitative claim "
        "attributed to a reference is plausibly contained in that reference. "
        "Flag citations that do not support the stated claim, or that you "
        "cannot confirm support it (mark unverifiable, raise as a question). A "
        "central claim resting on a misattributed or unsupported citation is "
        "HARD.\n"
        "  - Quotation/number fidelity: quoted text, statistics, or values "
        "attributed to a source match what the source says, where checkable.\n"
        "  - Self-citation / citation inflation: note conspicuous, non-germane "
        "self-citation or padding (SOFT).\n"
        "  - Retracted / predatory sources: flag any cited work you can identify "
        "as retracted or from a known predatory venue, especially if "
        "load-bearing (HARD when load-bearing).\n\n"
        "Be conservative: when you cannot verify a reference's contents from the "
        "manuscript or the tools, mark the finding unverifiable and raise it as a "
        "question rather than asserting it is wrong. Do not assume an "
        "unverifiable citation passes."
    ),
    tool_names=["find_related_work", "search_biomedical_literature"],
    # The reference list is this auditor's primary material, and reading it
    # out of the prose is what it used to do: the bibliography arrives as
    # entries, so "does [12] resolve" is a lookup rather than a re-segmenting
    # of two flattened columns. The manuscript text is still there and is
    # still where in-text citations are read from.
    needs_references=True,
)
