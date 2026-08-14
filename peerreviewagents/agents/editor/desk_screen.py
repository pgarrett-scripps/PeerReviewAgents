"""The desk: what an editor settles before a single reviewer is assigned.

Two screens live in this one node:

1. **Conversion health** (``conversion_gate``, ``"degraded"`` by default) — a
   deterministic verdict on how the PDF converted, measured at ingest by
   :mod:`peerreviewagents.ingest.prose`. Text that arrived as
   ``well-definedsitecanbeengaged`` stops the run rather than being reviewed
   by seventeen agents at full price.
2. **Editorial triage** (``desk_screen``, off by default) — a fast LLM
   scope / completeness / fatal-flaw judgment against the target venue and
   the configured strictness, which either desk-rejects or passes the
   manuscript to the panel.

The two differ in what they produce, and that difference is the point. Triage
desk-rejects: a verdict, a letter, a published bundle. Conversion health raises
:class:`~peerreviewagents.ingest.loader.ManuscriptUnreadable` and produces
nothing at all — a desk rejection is a judgment about a manuscript, and a
converter failure is a fact about a file. Recording the second as the first
would attach a rejection to work no model ever read.

The LLM path is fail-open: any error degrades to "proceed to full review"
rather than blocking a manuscript on an infrastructure hiccup.

This node used to also run a deterministic scan for text concealed in the
submitted file — white fill, invisible render mode, off-page placement —
and desk-reject when it carried instructions aimed at an automated reviewer.
It was removed in full. The concealment test assumed a white page, so white
labels drawn on a dark figure read as hidden text, and on a real submission
that produced a published claim that the authors had concealed 10,976
characters. See the commit that removed it for the reasoning.
"""

from __future__ import annotations

from ...ingest.loader import conversion_gate, require_readable
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
    - ``off`` — no LLM triage. The node itself still runs when the conversion
      gate is on (see :func:`node_enabled`), but it makes no model call and
      records no screening note.

    Back-compat: the legacy boolean ``desk_screen`` maps ``True`` → ``gate``,
    ``False`` → ``off``. An explicit ``desk_screen_mode`` overrides it.
    """
    m = str(config.get("desk_screen_mode") or "").lower().strip()
    if m in ("off", "warm", "gate"):
        return m
    return "gate" if config.get("desk_screen") else "off"


def node_enabled(config: dict) -> bool:
    """Whether the desk node belongs in the graph at all.

    Either screen is enough. The conversion gate counts because it is the one
    that stops a run from being paid for, and a config that turned off triage
    too would otherwise send an unreadable file to the full panel.
    """
    return screen_mode(config) != "off" or conversion_gate(config) != "off"


def node(state: ReviewState) -> dict:
    with node_context("desk_screen", run_id=state["config"].get("run_id", "")):
        return _run(state)


def _run(state: ReviewState) -> dict:
    config = state["config"]

    # Is this file readable at all? This raises rather than returning a
    # verdict — see the module docstring on why an unreadable file must not
    # look like a rejection.
    require_readable(state.get("ingest"), config)

    if screen_mode(config) == "off":
        # Conversion-gate-only pass: nothing else to do at the desk, and no
        # LLM call to make. Leave `desk_screen` unset so a run with the triage
        # gate off looks exactly as it did before.
        return {"desk_rejected": False}

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
            # The manuscript block the whole panel shares. Perturbing it would
            # miss the cache for every later agent.
            cached_prefix=context_block(state),
        )
    except Exception as exc:  # noqa: BLE001
        # Fail open: never block a manuscript at the desk on an error.
        return {
            "errors": [f"desk_screen failed: {exc}"],
            "desk_rejected": False,
        }

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
