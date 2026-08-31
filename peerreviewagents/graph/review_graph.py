"""Assemble the peer-review LangGraph and provide the top-level runner."""

from __future__ import annotations

import math
import os
import uuid
from functools import lru_cache
from typing import Annotated, Any, Callable, get_args, get_origin, get_type_hints

from langgraph.graph import END, START, StateGraph

from .. import rounds as rounds_mod
from ..agents.auditors import get_auditor_nodes
from ..agents.author import response_verifier
from ..agents.debate import advocate, skeptic
from ..agents.editor import desk_screen, editor_in_chief
from ..agents.journal_recommender import recommender as journal_recommender
from ..agents.reviewers import get_reviewer_nodes
from ..agents.synthesis import debate_synthesizer
from ..agents.utils.agent_states import ReviewState
from ..article_types import article_type_block, normalize_article_type
from ..checkpoints import checkpointed
from ..default_config import get_config
from ..ingest.loader import load_manuscript, load_manuscript_record
from ..journals import load_journal
from ..strictness import DEFAULT_LEVEL, normalize_strictness, strictness_block
from .conditional_logic import make_desk_route, should_continue_debate


def is_revision(config: dict) -> bool:
    """Whether this run is a revision round (a prior round was named)."""
    return bool(config.get("revision_of"))


def is_correction(config: dict) -> bool:
    """Whether this run challenges the review rather than the manuscript.

    A correction is still anchored to a prior round — it carries the panel's
    earlier reports forward for the reviewers it is not re-running — but the
    manuscript has not changed, so the compliance auditor does not run: over
    an identical draft it would report every required revision as undone and
    push the verdict down over a complaint that the *review* was wrong. See
    ``revision_mode`` in default_config.
    """
    return is_revision(config) and str(
        config.get("revision_mode") or "revision"
    ).lower().strip() == "correction"


def selected_reviewers(config: dict) -> list[str]:
    """Names of the specialists that will actually run this round.

    An unknown name is an error rather than a silent omission: a typo in
    ``only_reviewers`` would otherwise quietly shrink the panel, and a review
    that ran five reviewers when it was asked for six is not something the
    output makes obvious.
    """
    from ..agents.reviewers import get_reviewer_nodes

    registry = get_reviewer_nodes(config)
    available = [name for name, _ in registry]
    chosen = [str(n).strip() for n in (config.get("only_reviewers") or []) if str(n).strip()]
    if not chosen:
        return available
    unknown = [n for n in chosen if n not in available]
    if unknown:
        raise ValueError(
            f"only_reviewers names no such reviewer: {', '.join(sorted(unknown))}. "
            f"Available: {', '.join(available)}."
        )
    if not config.get("revision_of"):
        raise ValueError(
            "only_reviewers requires revision_of: the reviewers left out have "
            "their prior reports carried forward from that round, and without "
            "one the panel would be scored on a subset without saying so."
        )
    return chosen


def build_graph(config: dict):
    g = StateGraph(ReviewState)

    def add_node(name: str, fn, **kwargs) -> None:
        g.add_node(name, checkpointed(name, fn, config), **kwargs)

    revision = is_revision(config)
    # The author's response letter is adjudicated BEFORE the fan-out, because
    # its only sanctioned route to a reviewer is the verifier's pointer list.
    # Wiring it as a gate rather than a parallel branch is what makes that
    # ordering structural instead of a convention someone can forget.
    verify_response = revision and bool(config.get("author_statement_path"))

    chosen = set(selected_reviewers(config))
    registry = get_reviewer_nodes(config)
    reviewer_nodes = [(n, fn) for n, fn in registry if n in chosen]
    for name, fn in reviewer_nodes:
        add_node(f"reviewer_{name}", fn)

    # Editorial audit lane: factual-checklist auditors that fan out alongside
    # the reviewers but route their reports straight to the editor (not into
    # the scored panel or debate). A revision round adds the
    # compliance auditor to this lane — it checks the previous letter's
    # required revisions against the new draft, which is a factual checklist
    # of exactly the kind the lane exists for, and like the others it feeds
    # only the editor and carries no score.
    # A correction gets the standing auditors but not the compliance one:
    # the draft is identical, so it would report every required revision as
    # undone and drive the verdict down for a submission whose complaint is
    # that the *review* was wrong. The reviewer panel is unaffected either
    # way — it is blind to the round in both modes.
    auditor_nodes = get_auditor_nodes(revision=revision and not is_correction(config))
    for name, fn in auditor_nodes:
        add_node(f"audit_{name}", fn)

    # The advocate/skeptic debate is on by default; `enable_debate=False`
    # sends the completed panel straight to the editor (and skips the debate
    # synthesizer, which would have nothing to condense). The two debaters
    # run in PARALLEL each round and meet at a join node that advances the
    # round counter — see agents/debate/base.py for why neither debater may
    # advance it itself.
    debate_enabled = bool(config.get("enable_debate", True))
    if debate_enabled:
        add_node("advocate", advocate.node)
        add_node("skeptic", skeptic.node)
        add_node("debate_synthesizer", debate_synthesizer.node)
    # The editor reads the full specialist reports and audits directly, plus
    # the synthesizer's condensed account of the debate — deliberately not
    # the raw multi-round transcript, which is published but would grow the
    # editor's context with every round.
    add_node("editor", editor_in_chief.node)
    # Journal recommender runs after the editor so it can condition its
    # venue suggestions on the final accept/minor/major/reject verdict
    # and the required-revisions list in the decision letter.
    add_node("journal_recommender", journal_recommender.node)

    # The desk node: conversion gate + optional triage gate.
    # Triage modes (see desk_screen.screen_mode): "gate" enforces desk-reject
    # (START -> desk_screen -> END on reject | fan out); "warm" runs it only to
    # prime the shared manuscript prompt cache before the parallel fan-out
    # reads it, always proceeding; "off" skips the LLM triage.
    # In warm mode the node returns desk_rejected=False, so the same
    # route_after_desk_screen fans out unconditionally.
    # The node is still wired in when triage is "off" as long as the
    # conversion gate is on (the default) — it then costs no tokens but is
    # what stops an unreadable file from being reviewed at full price. Both
    # screens off means START fans out directly, as before.
    desk_screen_enabled = desk_screen.node_enabled(config)

    # Everything the fan-out reaches: the scored panel plus the audit lane.
    fan_out = [
        *[f"reviewer_{name}" for name, _ in reviewer_nodes],
        *[f"audit_{name}" for name, _ in auditor_nodes],
    ]

    expected_reviewers = {name for name, _ in reviewer_nodes}
    expected_auditors = {name for name, _ in auditor_nodes}
    try:
        quorum_fraction = float(config.get("panel_quorum_fraction", 1.0))
    except (TypeError, ValueError) as exc:
        raise ValueError("panel_quorum_fraction must be a number in (0, 1]") from exc
    if not 0 < quorum_fraction <= 1:
        raise ValueError("panel_quorum_fraction must be in (0, 1]")
    reviewer_quorum = math.ceil(len(expected_reviewers) * quorum_fraction)

    def panel_gate(state: ReviewState) -> dict:
        """Stop an incomplete fan-out before synthesis can invent a verdict."""
        got_reviewers = {r.get("reviewer") for r in state.get("reports", [])}
        got_auditors = {a.get("auditor") for a in state.get("audits", [])}
        missing_reviewers = sorted(expected_reviewers - got_reviewers)
        missing_auditors = sorted(expected_auditors - got_auditors)
        if not missing_reviewers and not missing_auditors:
            return {
                "panel_complete": True,
                "panel_degraded": False,
                "run_status": "panel_complete",
            }
        missing = []
        if missing_reviewers:
            missing.append("reviewers: " + ", ".join(missing_reviewers))
        if missing_auditors:
            missing.append("auditors: " + ", ".join(missing_auditors))
        # An auditor is a load-bearing factual gate and remains mandatory. A
        # partial reviewer panel proceeds only when the caller explicitly
        # opted into a quorum below 1.0; score_summary() prints the missing
        # specialty into the editor's evidence and the result stays degraded.
        has_quorum = len(got_reviewers & expected_reviewers) >= reviewer_quorum
        if has_quorum and not missing_auditors:
            return {
                "panel_complete": True,
                "panel_degraded": True,
                "run_status": "panel_degraded",
                "publication_ready": False,
                "errors": ["degraded panel (" + "; ".join(missing) + ")"],
            }
        return {
            "panel_complete": False,
            "panel_degraded": False,
            "run_status": "panel_incomplete",
            "publication_ready": False,
            "errors": ["incomplete panel (" + "; ".join(missing) + ")"],
        }

    g.add_node("panel_gate", panel_gate)

    # Gates that must run, in order, before the fan-out. Each one's output is
    # something every downstream agent reads, so they are serial by nature.
    if verify_response:
        add_node("response_verifier", response_verifier.node)

    # The desk node's successor is the verifier when there is one, else the
    # fan-out itself.
    after_desk = ["response_verifier"] if verify_response else fan_out

    if desk_screen_enabled:
        add_node("desk_screen", desk_screen.node)
        g.add_edge(START, "desk_screen")
        g.add_conditional_edges(
            "desk_screen", make_desk_route(after_desk), [END, *after_desk],
        )
    elif verify_response:
        g.add_edge(START, "response_verifier")

    if verify_response:
        for target in fan_out:
            g.add_edge("response_verifier", target)

    debate_entry = ["advocate", "skeptic"] if debate_enabled else ["editor"]
    # True when nothing precedes the fan-out, so START feeds it directly.
    fan_out_from_start = not desk_screen_enabled and not verify_response
    for name, _ in reviewer_nodes:
        if fan_out_from_start:
            g.add_edge(START, f"reviewer_{name}")
        g.add_edge(f"reviewer_{name}", "panel_gate")

    # Audit lane joins the reviewers at the deterministic panel gate. On a
    # desk-reject, neither lane fires.
    for name, _ in auditor_nodes:
        if fan_out_from_start:
            g.add_edge(START, f"audit_{name}")
        g.add_edge(f"audit_{name}", "panel_gate")

    g.add_conditional_edges(
        "panel_gate",
        lambda state: list(debate_entry) if state.get("panel_complete") else END,
        [*debate_entry, END],
    )

    # Debate rounds: (advocate ∥ skeptic) -> join -> (next round | synthesizer).
    # The join is the barrier where the round closes: it runs once after both
    # debaters return, advances the counter, and either fans the next round
    # out or hands the transcript to the synthesizer. Skipped entirely when
    # debate is ablated.
    if debate_enabled:

        def debate_join(state: ReviewState) -> dict:
            return {"debate_round": state.get("debate_round", 0) + 1}

        g.add_node("debate_join", debate_join)
        g.add_edge("advocate", "debate_join")
        g.add_edge("skeptic", "debate_join")
        g.add_conditional_edges(
            "debate_join",
            lambda state: should_continue_debate(state, "debate_synthesizer"),
            ["advocate", "skeptic", "debate_synthesizer"],
        )
        g.add_edge("debate_synthesizer", "editor")

    def finalize(state: ReviewState) -> dict:
        ready = bool(state.get("panel_complete") and state.get("decision"))
        degraded = bool(state.get("panel_degraded"))
        return {
            "publication_ready": ready and not degraded,
            "run_status": (
                "degraded" if ready and degraded
                else "publishable" if ready
                else "synthesis_incomplete"
            ),
        }

    g.add_node("finalize", finalize)
    if config.get("enable_journal_recommender", True):
        g.add_edge("editor", "journal_recommender")
        g.add_edge("journal_recommender", "finalize")
    else:
        g.add_edge("editor", "finalize")
    g.add_edge("finalize", END)

    return g.compile()


class PeerReviewGraph:
    """High-level entry point, analogous to TradingAgentsGraph."""

    def __init__(self, config: dict | None = None):
        self.config = dict(config or get_config())
        # Tag every event this run emits so a consumer can watch one review
        # without seeing another's. Callers wanting that isolation pass
        # `graph.run_id` to register_observer; registering without one still
        # receives everything.
        # Not setdefault: every config built by get_config() carries the
        # DEFAULT_CONFIG key run_id=None, which setdefault treats as "set" —
        # so no run built that way was ever tagged, and a consumer keying its
        # observer on graph.run_id fell through to the shared mailbox, where
        # two concurrent runs interleave.
        if not self.config.get("run_id"):
            self.config["run_id"] = uuid.uuid4().hex[:12]
        self.graph = build_graph(self.config)

    @property
    def run_id(self) -> str:
        return self.config["run_id"]

    def initial_state(self, manuscript_path: str) -> ReviewState:
        parsed = load_manuscript_record(manuscript_path, self.config)
        title, md, sections = parsed.as_triple()
        sup_md, sup_sections = self._load_supplement()
        prior = self._load_prior_round()
        return ReviewState(
            manuscript_path=manuscript_path,
            manuscript_title=title,
            manuscript_md=md,
            sections=sections,
            references=parsed.references,
            ingest=parsed.ingest,
            supplement_md=sup_md,
            supplement_sections=sup_sections,
            config=self.config,
            journal_block=self._journal_block(),
            article_type_block=self._article_type_block(),
            strictness_block=self._strictness_block(),
            prior_round=prior,
            author_statement=self._load_author_statement(),
            response_verification="",
            verified_claims_block="",
            desk_rejected=False,
            panel_complete=False,
            panel_degraded=False,
            run_status="running",
            publication_ready=False,
            reports=self._carried_reports(prior),
            audits=[],
            debate=[],
            debate_round=0,
            debate_synthesis="",
            errors=[],
            total_cost=0.0,
        )

    def _carried_reports(self, prior) -> list:
        """Prior reports for the reviewers that are not re-running this round.

        With ``only_reviewers`` set, the panel that runs is a subset — but the
        panel that was *assessed* is still the complete selected panel. Seeding the state with
        the untouched reviewers' earlier reports is what keeps the weighted
        score, the debate digest and the editor's view over the whole panel
        instead of over whichever agent happened to re-run. Without this a
        correction that re-ran one reviewer would publish a mean over one
        report and call it the panel's score.

        The rendered bodies come from the prior round's report directory,
        since the round record stores scalars and weaknesses but not prose. A
        body that cannot be read falls back to the record's own summary of
        that reviewer, so a missing file costs detail rather than the report.
        """
        chosen = set(selected_reviewers(self.config))
        if prior is None:
            return []
        # Membership, not size: a 7-name only_reviewers list and a 7-report
        # prior round (one reviewer errored out of it) match by count while
        # naming different panels, and the count comparison dropped the one
        # report that needed carrying.
        prior_names = {r.reviewer for r in prior.reviewer_reports}
        if prior_names <= chosen:
            return []

        carried = []
        for report in prior.reviewer_reports:
            if report.reviewer in chosen:
                continue  # this one is re-running; its fresh output replaces it
            carried.append(
                {
                    "reviewer": report.reviewer,
                    "score": report.score,
                    "confidence": report.confidence,
                    "weaknesses": [w.text for w in report.weaknesses],
                    "questions": list(report.questions),
                    "body": self._prior_body(report),
                }
            )
        return carried

    def _prior_body(self, report) -> str:
        """The rendered markdown of one carried-forward reviewer report."""
        job_id = str(self.config.get("revision_of") or "")
        header = (
            "*Carried forward unchanged from the previous round: this reviewer "
            "was not re-run, so its assessment stands as written.*\n\n"
        )
        try:
            run_dir = rounds_mod.resolve_run_dir(job_id, self.config)
            path = os.path.join(run_dir, f"review_{report.reviewer}.md")
            with open(path, "r", encoding="utf-8") as fh:
                return header + fh.read()
        except (OSError, FileNotFoundError, ValueError):
            pass
        lines = [f"### {report.reviewer} (carried forward)", ""]
        if report.weaknesses:
            lines += ["Weaknesses raised:"] + [f"- {w.text}" for w in report.weaknesses]
        return header + "\n".join(lines)

    def _load_prior_round(self):
        """Load the previous round's record, or None for a first-round run.

        A named-but-unloadable prior round is fatal on purpose. Everything
        else in this file degrades gracefully, but silently downgrading a
        revision round to a fresh review would give the authors a decision
        letter that ignores the revisions they were asked to make — a wrong
        answer delivered confidently, which is worse than a clear failure.
        """
        job_id = self.config.get("revision_of")
        if not job_id:
            return None
        return rounds_mod.load_prior(str(job_id), self.config)

    def _load_author_statement(self) -> str:
        """Parse the real authors' response letter, or '' when none was given.

        Parsed with the same loader as the manuscript, which means it also
        passes through the ingest cache. It is NOT screened here, and no
        node screens it later either: the defenses are structural. The
        letter reaches the panel only as the response verifier's
        corroborated pointers, and both the verifier and the compliance
        auditor read it fenced between neutralized quote markers — its
        prose has no route to a reviewer whatever it says.
        """
        path = self.config.get("author_statement_path")
        if not path:
            return ""
        try:
            # kind="letter": the converter's plausibility floor is calibrated
            # for manuscripts, and a normal one-page response letter is a
            # quarter of it — loaded as a manuscript, a readable letter was
            # refused as "scanned or image-only" and killed the revision run.
            _title, text, _sections = load_manuscript(
                str(path), self.config, kind="letter"
            )
        except Exception as exc:  # noqa: BLE001
            raise ValueError(
                f"Could not read the author response letter at {path}: {exc}"
            ) from exc
        return text

    def _load_supplement(self) -> tuple[str, dict[str, str]]:
        """Parse the optional supplementary-information file, or ('', {}) if none.

        The SI is optional: no ``supplement_path`` means an unchanged run. A
        provided-but-unparseable SI must never crash the review, so any parse
        failure degrades rather than propagating — but not to silence. The
        failure used to be swallowed whole, and the methods-completeness
        auditor then reported the SI missing when the operator had supplied
        it. The placeholder returned below rides where the SI text would
        have, so the auditor (and the report built on its output) is told the
        SI exists and why it could not be read.
        """
        path = self.config.get("supplement_path")
        if not path:
            return "", {}
        try:
            _title, md, sections = load_manuscript(
                path, self.config, kind="supplement"
            )
        except Exception as exc:  # noqa: BLE001 — optional input, never fail the run
            from ..observability import AgentEvent, emit

            reason = f"{exc.__class__.__name__}: {exc}"
            emit(AgentEvent(
                kind="log", node="ingest",
                text=f"supplement at {os.path.basename(str(path))} could not "
                     f"be read ({reason}); the review proceeds without it",
            ))
            note = (
                "A supplementary-information file was provided by the "
                f"operator ({os.path.basename(str(path))}) but could not be "
                f"read: {reason}. Its contents are unavailable to this "
                "review. Treat the SI as supplied but unreadable — do not "
                "report it as missing, and do not count anything as verified "
                "by it."
            )
            return note, {}
        return md, sections

    def _journal_block(self) -> str:
        """Render the target-journal prompt block once, or '' if none/missing.

        A bad slug should already have been caught at selection time
        (CLI/web); here we degrade gracefully so a run never crashes on
        venue context — the review just proceeds venue-agnostically.
        """
        try:
            profile = load_journal(self.config.get("target_journal"), self.config)
        except FileNotFoundError:
            return ""
        return profile.to_prompt_block() if profile else ""

    def _article_type_block(self) -> str:
        """Render the manuscript-type prompt block once, or '' if none/invalid.

        The general description/framing comes from the shared taxonomy; any
        per-type word caps and notes are pulled from the selected target
        journal's profile. A bad type or journal slug degrades to '' here so a
        run never crashes on manuscript-type context (the CLI/web layer
        validates and fails fast before this point).
        """
        try:
            key = normalize_article_type(self.config.get("article_type"))
        except ValueError:
            return ""
        if not key:
            return ""
        max_words = abstract_max_words = 0
        notes = ""
        try:
            profile = load_journal(self.config.get("target_journal"), self.config)
        except FileNotFoundError:
            profile = None
        limits = profile.article_type_limits(key) if profile else None
        if limits:
            max_words = limits.max_words
            abstract_max_words = limits.abstract_max_words
            notes = limits.notes
        return article_type_block(
            key,
            max_words=max_words,
            abstract_max_words=abstract_max_words,
            notes=notes,
        )

    def _strictness_block(self) -> str:
        """Render the review-strictness directive once, or '' at the balanced
        default. An out-of-range value (e.g. from a hand-edited TOML) degrades
        to the balanced default rather than crashing a run; the CLI/web layer
        validates and fails fast before reaching here.
        """
        try:
            level = normalize_strictness(self.config.get("review_strictness", DEFAULT_LEVEL))
        except ValueError:
            level = DEFAULT_LEVEL
        return strictness_block(level)

    def review(self, manuscript_path: str) -> ReviewState:
        state = self.initial_state(manuscript_path)
        return self.graph.invoke(state, {"recursion_limit": 50})

    def stream(self, manuscript_path: str):
        """Yield (node_name, accumulated_state) as the graph executes.

        We accumulate state ourselves because LangGraph's default stream mode
        emits per-node partials, and parallel writers to reducer fields
        (reports, debate, errors) would otherwise look like overwrites to
        a naive consumer doing dict.update.
        """
        # Emit a start event BEFORE parsing so the CLI/TUI shows activity
        # while the converter works through the document — a long PDF takes a
        # while and the UI would otherwise look hung.
        yield "_ingest_start", {"manuscript_path": manuscript_path}
        state = self.initial_state(manuscript_path)
        accumulated: dict = dict(state)
        yield "_ingest", dict(accumulated)
        for chunk in self.graph.stream(state, {"recursion_limit": 50}):
            for node_name, partial in chunk.items():
                _merge_partial(accumulated, partial)
                yield node_name, dict(accumulated)


@lru_cache(maxsize=1)
def _state_reducers() -> dict[str, Callable[[Any, Any], Any]]:
    """Read the reducers straight off ``ReviewState``'s annotations.

    Fields written by parallel nodes are declared as
    ``Annotated[list[X], operator.add]`` so LangGraph combines rather than
    overwrites them. ``stream()`` has to apply the same rule while
    accumulating, and that used to be a hand-maintained list of key names —
    so adding a reducer field and forgetting the list silently downgraded
    streaming to overwrite. Only ``review()`` would look right, while the
    CLI, TUI, and web UI all consume ``stream()``. Deriving it from the one
    declaration removes the chance to forget.
    """
    reducers: dict[str, Callable[[Any, Any], Any]] = {}
    for key, hint in get_type_hints(ReviewState, include_extras=True).items():
        if get_origin(hint) is not Annotated:
            continue
        for meta in get_args(hint)[1:]:
            if callable(meta):
                reducers[key] = meta
                break
    return reducers


def _merge_partial(accumulated: dict, partial: dict) -> None:
    reducers = _state_reducers()
    for key, value in partial.items():
        reduce = reducers.get(key)
        if reduce is not None and key in accumulated:
            accumulated[key] = reduce(accumulated[key], value)
        else:
            accumulated[key] = value
