"""Cross-examiner: findings that need more than one reviewer's report to see.

Runs once, after the specialist fan-out and before the debate. It reads the
reports the panel has written and the manuscript they were written about, and
reports only what no single reviewer said.

Why it exists, concretely. Benchmarking this panel against Nature's published
referee reports for one manuscript found the referees' central objection — that
the paper's headline sequence motifs were sample-preparation artefacts rather
than biology — reconstructed in full by one strong model in one report, and
scattered across three reports by weaker ones: the novelty reviewer noticed
substitutions clustering at cleavage sites, the reproducibility reviewer
noticed a detection bias that could manufacture the paper's stability
correlation, the rigor reviewer noticed low-frequency variants would be
invisible at the sequencing depth used. Each is a fragment of one argument.
Nothing in the pipeline was looking across them, because the specialists run in
parallel and never read each other, and every stage downstream summarises the
panel rather than reasoning over it.

It gets the manuscript as well as the reports, because a joined finding has to
be checkable against the paper before it is worth making. That is also the
danger — the area chair is deliberately denied the manuscript so it weighs the
panel instead of becoming a ninth reviewer — so the schema, not the prompt,
carries the constraint: a finding names the two or more reports it rests on and
quotes them, or it does not validate.
"""

from __future__ import annotations

from ...observability import node_context
from ..debate.base import _reports_digest
from ..schemas import CrossExamOutput
from ..utils.agent_states import ReviewState
from ..utils.agent_utils import context_block
from ..utils.llm import make_llm
from ..utils.structured import invoke_structured

_SYS = (
    "You are the cross-examiner on a peer-review panel. The specialists have "
    "filed their reports and never saw each other's. Your job is the one none "
    "of them could do: read the reports together and find what follows from "
    "combining them.\n\n"
    "You are not a ninth reviewer. Do not re-review the manuscript, do not "
    "restate a finding that is already in a report, and do not grade the "
    "reviewers. Every finding you report must be built from at least two "
    "reports, must quote the statement you took from each, and must say what "
    "it adds beyond what either said alone.\n\n"
    "What to look for:\n"
    "  - One reviewer names a mechanism and another names an observation that "
    "the mechanism would produce. Together they are an explanation neither "
    "stated.\n"
    "  - Two reviewers each report a limitation that is mild alone, and the "
    "same claim rests on both at once.\n"
    "  - One reviewer accepts a claim as supported and another reports "
    "evidence that undermines the support. Name the contradiction and say "
    "which side the manuscript actually bears out.\n"
    "  - Several reviewers flag separate instances of what is really one "
    "underlying problem, which none of them sized because each saw a part.\n\n"
    "Check every joined finding against the manuscript before reporting it. "
    "If the paper already addresses it, drop it. A finding you cannot ground "
    "in a quoted statement from each report is one you invented; leave it "
    "out.\n\n"
    "Most panels will yield one or two of these, and some none at all. "
    "Reporting nothing, with a sentence on what you looked for, is a better "
    "answer than reporting something thin. Return the structured "
    "CrossExamOutput schema."
)


def node(state: ReviewState) -> dict:
    with node_context("cross_examiner", run_id=state["config"].get("run_id", "")):
        return _run(state)


def _run(state: ReviewState) -> dict:
    config = state["config"]
    reports = state.get("reports", [])
    # Two reports is the floor for the question to be askable at all. Below it
    # there is nothing to cross-examine, and the call would be spent asking a
    # model to find connections in a single document.
    if len(reports) < 2:
        return {"cross_exam": ""}

    user = (
        f"The panel's reports:\n\n{_reports_digest(state)}\n\n"
        "Report only what needs more than one of these to see. Name the "
        "reports each finding is built from and quote the statement you took "
        "from each."
    )
    try:
        # Model construction is inside the try with the call. This stage is
        # additive: a run without it is the run the pipeline produced before
        # it existed, so nothing here — including a provider that will not
        # build — should be able to take the review down with it.
        llm = make_llm(config, agent="cross_examiner", default_tag="synthesis")
        result = invoke_structured(
            llm,
            CrossExamOutput,
            config,
            _SYS,
            user,
            # The same cached blocks the panel read, in the same order, so
            # this call shares their cache entry instead of writing a second
            # copy of the manuscript.
            cached_prefix=context_block(state),
        )
    except Exception as exc:  # noqa: BLE001
        # Never fatal. This stage adds findings; a run without it is the run
        # the pipeline produced before it existed.
        return {"errors": [f"cross_examiner failed: {exc}"], "cross_exam": ""}

    output: CrossExamOutput = result.instance  # type: ignore[assignment]
    return {
        "cross_exam": output.to_markdown(),
        "total_cost": result.cost,
    }
