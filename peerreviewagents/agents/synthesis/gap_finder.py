"""Gap finder: what the three technical reviewers missed.

Runs once, after the specialist fan-out and before the debate. It reads the
data_analysis, methodology and rigor reports together with the manuscript, and
reports what none of them caught.

Those three are the panel's evidence-facing lane and they overlap in subject
without overlapping in remit — one asks whether the numbers are right, one
whether the design earns the conclusion, one whether the wording outruns the
evidence. A weakness in the paper can fall between all three, and nothing else
in the pipeline is looking: the reviewers run in parallel and never read each
other, and every stage downstream summarises the panel rather than checking it.

Grounding is the whole design problem. An agent told to find what the reviewers
missed has every incentive to manufacture something, and the obvious guard —
make it cite another report — is exactly wrong here, because a gap by
definition appears in no report. So the citation requirement points at the
manuscript instead: name the passage, figure or value, and name the lane that
should have caught it. That keeps the finding checkable and routes it to a
specialist rather than leaving it as a floating ninth opinion.

Reporting nothing is a first-class answer, and on a well-reviewed paper it is
the correct one.
"""

from __future__ import annotations

from ...observability import node_context
from ..schemas import PanelGapOutput
from ..utils.agent_states import ReviewState
from ..utils.agent_utils import context_block
from ..utils.llm import make_llm
from ..utils.structured import invoke_structured

# The evidence-facing lane. Deliberately not the whole panel: clarity, ethics,
# literature, novelty and reproducibility answer different questions, and
# folding them in turns "what did the technical read miss" into "what did
# anyone miss", which is a different and much vaguer job.
TECHNICAL_LANES = ("data_analysis", "methodology", "rigor")

_SYS = (
    "You are a senior referee auditing three specialist reports on a "
    "manuscript you have also read: the data-analysis reviewer, the "
    "methodology reviewer and the rigor reviewer.\n\n"
    "Your question is what they missed. Not whether they were right — do not "
    "grade them, do not re-argue their points, and do not restate a finding "
    "any of them already made in different words. You are looking for the "
    "weakness that fell between three people who each read for something "
    "else.\n\n"
    "Where to look:\n"
    "  - A claim in the paper none of the three examined at all. They read for "
    "statistics, for design and for overclaiming; a claim can be untouched by "
    "all three.\n"
    "  - A weakness one of them noticed in one place and did not carry to the "
    "other places it applies.\n"
    "  - Something that follows from two of their findings together that "
    "neither stated. Mark these 'joined' and name the reports.\n"
    "  - An assumption all three accepted without examining, often because it "
    "is stated so early in the paper that it reads as background.\n"
    "  - A control, comparison or piece of evidence a competent referee would "
    "expect for this kind of claim, that the paper does not have and no "
    "reviewer asked for.\n\n"
    "Every finding must name the specific sentence, figure, table or value in "
    "the manuscript it concerns — quoted or located precisely enough that "
    "someone can look it up — and must name which of the three reviewers' "
    "remits it belongs in. A finding you cannot tie to a specific place in the "
    "paper is one you invented; leave it out. If a report already covers it, "
    "even loosely, it is not a gap.\n\n"
    "Most well-reviewed papers will leave one or two real gaps, and some none "
    "at all. Reporting nothing, with a sentence on what you checked, is a "
    "better answer than padding. Return the structured PanelGapOutput schema."
)


def node(state: ReviewState) -> dict:
    with node_context("gap_finder", run_id=state["config"].get("run_id", "")):
        return _run(state)


def _technical_digest(state: ReviewState) -> str:
    """The three technical reports, in full, or '' if none of them reported."""
    out = []
    for r in state.get("reports", []):
        if r["reviewer"] not in TECHNICAL_LANES:
            continue
        score = r.get("score")
        head = (
            f"(score {score}, confidence {r.get('confidence')})"
            if isinstance(score, (int, float))
            else "(no score given)"
        )
        out.append(f"### {r['reviewer']} reviewer {head}\n{r['body'].strip()}")
    return "\n\n".join(out)


def _run(state: ReviewState) -> dict:
    config = state["config"]
    digest = _technical_digest(state)
    if not digest:
        # None of the three lanes reported — there is nothing to audit, and
        # asking anyway would produce a review of the manuscript from an agent
        # that is not a reviewer.
        return {"panel_gaps": ""}

    user = (
        f"The three technical reports:\n\n{digest}\n\n"
        "Report what they missed. For each finding, name the place in the "
        "manuscript it concerns and the reviewer whose remit it falls in."
    )
    try:
        # Model construction sits inside the try with the call. This stage is
        # additive: a run without it is the run the pipeline produced before
        # it existed, so nothing here should be able to take a review down.
        llm = make_llm(config, agent="gap_finder", default_tag="synthesis")
        result = invoke_structured(
            llm,
            PanelGapOutput,
            config,
            _SYS,
            user,
            # The same cached blocks the panel read, in the same order, so
            # this call shares their cache entry rather than writing a second
            # copy of the manuscript.
            cached_prefix=context_block(state),
        )
    except Exception as exc:  # noqa: BLE001
        return {"errors": [f"gap_finder failed: {exc}"], "panel_gaps": ""}

    output: PanelGapOutput = result.instance  # type: ignore[assignment]
    return {
        "panel_gaps": output.to_markdown(),
        "total_cost": result.cost,
    }
