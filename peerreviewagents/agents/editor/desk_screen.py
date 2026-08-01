"""Optional desk-screen: an editorial triage gate before the full review.

Runs once, ahead of the reviewer fan-out, only when ``desk_screen`` is
enabled in config. It makes a fast scope / completeness / fatal-flaw
judgment against the target venue and the configured strictness, and either
desk-rejects the manuscript (short-circuiting the pipeline to a reject) or
lets it proceed to the panel unchanged.

The node is deliberately fail-open: any error in the screen degrades to
"proceed to full review" rather than blocking a manuscript on an
infrastructure hiccup.
"""

from __future__ import annotations

from ...observability import node_context
from ..schemas import DeskScreenOutput
from ..utils.agent_states import ReviewState
from ..utils.agent_utils import context_block
from ..utils.llm import make_llm
from ..utils.structured import invoke_structured

_SYS = (
    "You are the handling Editor performing an initial desk screen, before "
    "any reviewers are assigned. Decide ONLY whether the manuscript should be "
    "desk-rejected without full review. Desk-reject sparingly and only for "
    "clear, threshold problems: out of scope for the target venue, an "
    "incomplete or unintelligible submission, a fundamental and unfixable "
    "flaw evident on its face, or work plainly far below the venue's bar. "
    "When in doubt, do NOT desk-reject — send it to the panel. If a target "
    "journal is described in the context above, screen against that venue's "
    "scope and bar; if a review strictness standard is described above, apply "
    "it to how readily you desk-reject. Return the structured DeskScreenOutput "
    "schema."
)

_USER = (
    "Perform the desk screen on the manuscript above. Set desk_reject=true "
    "only if it should not be sent for full review, and give the authors a "
    "brief, professional rationale. If it should proceed, set "
    "desk_reject=false with an empty reasons list."
)


def screen_mode(config: dict) -> str:
    """Resolve the desk-screen mode: ``"off"`` | ``"warm"`` | ``"gate"``.

    - ``gate`` — run triage and enforce a desk-reject (short-circuit the run).
    - ``warm`` — run triage to prime the manuscript prompt cache for the
      parallel reviewer fan-out, but *ignore* the reject verdict (always
      proceed to the full panel). The screen's opinion is still recorded.
    - ``off`` — don't run the node at all.

    Back-compat: the legacy boolean ``desk_screen`` maps ``True`` → ``gate``,
    ``False`` → ``off``. An explicit ``desk_screen_mode`` overrides it.
    """
    m = str(config.get("desk_screen_mode") or "").lower().strip()
    if m in ("off", "warm", "gate"):
        return m
    return "gate" if config.get("desk_screen") else "off"


def node(state: ReviewState) -> dict:
    with node_context("desk_screen", run_id=state["config"].get("run_id", "")):
        return _run(state)


def _run(state: ReviewState) -> dict:
    config = state["config"]
    try:
        # Use the reviewers' model/tag, not a separate "screen" model, so the
        # cache this warms is the one the panel reads (caches are per-model).
        llm = make_llm(config, agent="desk_screen", default_tag="reviewer")
        result = invoke_structured(
            llm,
            DeskScreenOutput,
            config,
            _SYS,
            _USER,
            cached_prefix=context_block(state),
        )
    except Exception as exc:  # noqa: BLE001
        # Fail open: never block a manuscript at the desk on an error.
        return {"errors": [f"desk_screen failed: {exc}"], "desk_rejected": False}

    output: DeskScreenOutput = result.instance  # type: ignore[assignment]
    body = output.to_markdown()
    # In "warm" mode we ran only to prime the cache — never short-circuit,
    # regardless of the verdict.
    if output.desk_reject and screen_mode(config) == "gate":
        return {
            "desk_rejected": True,
            "decision": "reject",
            "decision_letter": body,
            "desk_screen": body,
            "total_cost": result.cost,
        }
    return {
        "desk_rejected": False,
        "desk_screen": body,
        "total_cost": result.cost,
    }
