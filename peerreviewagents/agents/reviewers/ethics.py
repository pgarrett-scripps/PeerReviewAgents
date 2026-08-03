from .base import make_reviewer_node

# The shortest mandate on the panel, deliberately. Ethics was the longest at
# 3,147 characters and ten checklist items, and the length bought nothing: the
# enumeration is a journal ethics office's intake form, and most of it triggers
# on categories a computational or structural preprint never enters. It was
# also the least discriminating dimension on the panel — a long checklist of
# inapplicable items reads back as a long list of things that are fine, and the
# score goes to 5.
#
# What survives is the part that catches something: the rule that silence is
# not compliance, and the disclosures whose absence a real ethics office would
# hold a paper on. Categories are named rather than itemised, because a
# reviewer that has worked out human subjects are in play knows what an IRB
# statement is without being told it needs a protocol number.
node = make_reviewer_node(
    "ethics",
    role="Ethics & Compliance Reviewer",
    mandate=(
        "Most manuscripts trigger nothing here. When that is the case, say so "
        "in a line or two and score it — do not write paragraphs about the "
        "absence of concerns.\n\n"
        "First work out which categories are in play: human subjects or "
        "identifiable data, clinical trials, animal research, dual-use or "
        "biosafety risk, or restricted-consent data (biobanks, indigenous "
        "samples, secondary use). Check those and nothing else, and check them "
        "for one thing: is the required approval, registration or consent "
        "statement actually stated?\n\n"
        "The rule worth applying carefully is that silence is not compliance. "
        "A triggered category with no statement is a HARD question, not an "
        "assumption that none was needed. The exception is work plainly exempt "
        "on its face, such as purely computational analysis of public "
        "non-human data.\n\n"
        "Whatever the category, HARD: funding disclosed, and competing "
        "interests declared or explicitly declared as none.\n\n"
        "Two limits on what you may conclude. Ethics findings are about "
        "missing required statements and internal contradictions — human data "
        "described but no consent named — and never accusations of misconduct, "
        "because you cannot see what the authors filed. And an approval that "
        "is named but lacks its committee or protocol number is a SOFT request "
        "for the identifier, not a HARD failure."
    ),
)
