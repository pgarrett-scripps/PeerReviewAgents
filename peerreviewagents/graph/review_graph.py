"""Assemble the peer-review LangGraph and provide the top-level runner."""

from __future__ import annotations

import os
import uuid
from functools import lru_cache
from typing import Annotated, Any, Callable, get_args, get_origin, get_type_hints

from langgraph.graph import END, START, StateGraph

from .. import rounds as rounds_mod
from ..agents.auditors import get_auditor_nodes
from ..agents.author import rebuttal as author_rebuttal
from ..agents.author import response_verifier
from ..agents.debate import advocate, skeptic
from ..agents.editor import desk_screen, editor_in_chief
from ..agents.journal_recommender import recommender as journal_recommender
from ..agents.reviewers import get_reviewer_nodes
from ..agents.synthesis import gap_finder, meta_reviewer
from ..agents.utils.agent_states import ReviewState
from ..article_types import article_type_block, normalize_article_type
from ..default_config import get_config
from ..ingest import cache as ingest_cache
from ..ingest import diff as ingest_diff
from ..ingest.loader import load_manuscript, load_manuscript_record
from ..journals import load_journal
from ..strictness import DEFAULT_LEVEL, normalize_strictness, strictness_block
from .conditional_logic import make_desk_route, should_continue_debate


def is_revision(config: dict) -> bool:
    """Whether this run is a revision round (a prior round was named)."""
    return bool(config.get("revision_of"))


def is_correction(config: dict) -> bool:
    """Whether this run challenges the review rather than the manuscript.

    A correction is still anchored to a prior round — it needs the reviewers'
    own earlier points to rule on — but the manuscript has not changed, so the
    two things that only make sense against a changed draft (the diff and the
    compliance auditor) are not run. See ``revision_mode`` in default_config.
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
    from ..agents.reviewers import REVIEWER_NAMES

    chosen = [str(n).strip() for n in (config.get("only_reviewers") or []) if str(n).strip()]
    if not chosen:
        return list(REVIEWER_NAMES)
    unknown = [n for n in chosen if n not in REVIEWER_NAMES]
    if unknown:
        raise ValueError(
            f"only_reviewers names no such reviewer: {', '.join(sorted(unknown))}. "
            f"Available: {', '.join(REVIEWER_NAMES)}."
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

    revision = is_revision(config)
    # The author's response letter is adjudicated BEFORE the fan-out, because
    # its only sanctioned route to a reviewer is the verifier's pointer list.
    # Wiring it as a gate rather than a parallel branch is what makes that
    # ordering structural instead of a convention someone can forget.
    verify_response = revision and bool(config.get("author_statement_path"))

    chosen = set(selected_reviewers(config))
    reviewer_nodes = [(n, fn) for n, fn in get_reviewer_nodes() if n in chosen]
    for name, fn in reviewer_nodes:
        g.add_node(f"reviewer_{name}", fn)

    # Editorial audit lane: factual-checklist auditors that fan out alongside
    # the reviewers but route their reports straight to the editor (not into
    # the scored panel, debate, or meta-review). A revision round adds the
    # compliance auditor to this lane — it checks the previous letter's
    # required revisions against the new draft, which is a factual checklist
    # of exactly the kind the lane exists for, and like the others it feeds
    # only the editor and carries no score.
    # A correction gets the standing auditors but not the compliance one:
    # the draft is identical, so it would report every required revision as
    # undone and drive the verdict down for a submission whose complaint is
    # that the *review* was wrong.
    auditor_nodes = get_auditor_nodes(revision=revision and not is_correction(config))
    for name, fn in auditor_nodes:
        g.add_node(f"audit_{name}", fn)

    # The advocate/skeptic debate is on by default; `enable_debate=False`
    # ablates it (reviewers fan straight into the meta-reviewer), which is
    # how the eval harness measures the debate's contribution.
    debate_enabled = bool(config.get("enable_debate", True))
    if debate_enabled:
        g.add_node("advocate", advocate.node)
        g.add_node("skeptic", skeptic.node)
    # The gap finder sits between the panel and whatever consumes it. The
    # three technical reviewers read the manuscript independently and never
    # see each other, so a weakness can fall between all of them with
    # nothing in the run looking for it. This stage is the only one that
    # audits their reports against the manuscript for what they missed. On
    # by default; `enable_gap_finder=False` ablates it.
    gap_finder_enabled = bool(config.get("enable_gap_finder", True))
    if gap_finder_enabled:
        g.add_node("gap_finder", gap_finder.node)
    g.add_node("meta_reviewer", meta_reviewer.node)
    # Author rebuttal sits between meta-reviewer and editor so the editor sees
    # both the panel's verdict and the author's defense. It is SKIPPED when
    # the real authors supplied a response letter: simulating their defense
    # while holding the genuine article would be strictly worse, and the
    # verifier has already turned that letter into checked claims.
    if not verify_response:
        g.add_node("author_rebuttal", author_rebuttal.node)
    # `defer=True` is load-bearing: the editor joins two lanes of different
    # depths — the short audit lane (START -> audit -> editor) and the long
    # rebuttal chain (reviewers -> debate -> meta -> rebuttal -> editor).
    # LangGraph only barriers edges that settle in the SAME superstep, so a
    # plain node would fire once when the auditors finish (meta-review,
    # rebuttal, and scores still empty -> a junk decision letter) and again
    # after the rebuttal chain. Deferring makes the editor run once, after
    # every upstream task has drained.
    g.add_node("editor", editor_in_chief.node, defer=True)
    # Journal recommender runs after the editor so it can condition its
    # venue suggestions on the final accept/minor/major/reject verdict
    # and the required-revisions list in the decision letter.
    g.add_node("journal_recommender", journal_recommender.node)

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

    # Gates that must run, in order, before the fan-out. Each one's output is
    # something every downstream agent reads, so they are serial by nature.
    if verify_response:
        g.add_node("response_verifier", response_verifier.node)

    # The desk node's successor is the verifier when there is one, else the
    # fan-out itself.
    after_desk = ["response_verifier"] if verify_response else fan_out

    if desk_screen_enabled:
        g.add_node("desk_screen", desk_screen.node)
        g.add_edge(START, "desk_screen")
        g.add_conditional_edges(
            "desk_screen", make_desk_route(after_desk), [END, *after_desk],
        )
    elif verify_response:
        g.add_edge(START, "response_verifier")

    if verify_response:
        for target in fan_out:
            g.add_edge("response_verifier", target)

    # With debate on, reviewers feed the advocate; with debate ablated, they
    # fan straight into the meta-reviewer.
    consumer = "advocate" if debate_enabled else "meta_reviewer"
    # Reviewers converge on the cross-examiner, which then feeds the consumer,
    # so everything downstream sees the joined findings alongside the reports
    # they were drawn from.
    reviewer_sink = "gap_finder" if gap_finder_enabled else consumer
    # True when nothing precedes the fan-out, so START feeds it directly.
    fan_out_from_start = not desk_screen_enabled and not verify_response
    for name, _ in reviewer_nodes:
        if fan_out_from_start:
            g.add_edge(START, f"reviewer_{name}")
        g.add_edge(f"reviewer_{name}", reviewer_sink)

    # Audit lane fans out in parallel and converges on the editor, which is a
    # deferred node (see above) so it waits for both the rebuttal chain and
    # every auditor before it runs. On a desk-reject, the audits never fire.
    for name, _ in auditor_nodes:
        if fan_out_from_start:
            g.add_edge(START, f"audit_{name}")
        g.add_edge(f"audit_{name}", "editor")

    if gap_finder_enabled:
        g.add_edge("gap_finder", consumer)

    # Debate loop: advocate -> skeptic -> (loop | meta_reviewer). Skipped
    # entirely when debate is ablated.
    if debate_enabled:
        g.add_edge("advocate", "skeptic")
        g.add_conditional_edges("skeptic", should_continue_debate, ["advocate", "meta_reviewer"])

    # meta_reviewer -> author_rebuttal -> editor -> journal_recommender (linear),
    # with the rebuttal hop dropped when the real letter replaced it.
    if verify_response:
        g.add_edge("meta_reviewer", "editor")
    else:
        g.add_edge("meta_reviewer", "author_rebuttal")
        g.add_edge("author_rebuttal", "editor")
    g.add_edge("editor", "journal_recommender")
    g.add_edge("journal_recommender", END)

    return g.compile()


class PeerReviewGraph:
    """High-level entry point, analogous to TradingAgentsGraph."""

    def __init__(self, config: dict | None = None):
        self.config = dict(config or get_config())
        # Tag every event this run emits so a consumer can watch one review
        # without seeing another's. Callers wanting that isolation pass
        # `graph.run_id` to register_observer; registering without one still
        # receives everything.
        self.config.setdefault("run_id", uuid.uuid4().hex[:12])
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
            ingest=parsed.ingest,
            supplement_md=sup_md,
            supplement_sections=sup_sections,
            config=self.config,
            journal_block=self._journal_block(),
            article_type_block=self._article_type_block(),
            strictness_block=self._strictness_block(),
            prior_round=prior,
            manuscript_diff=self._manuscript_diff(prior, sections),
            author_statement=self._load_author_statement(),
            response_verification="",
            verified_claims_block="",
            desk_rejected=False,
            reports=self._carried_reports(prior),
            audits=[],
            debate=[],
            debate_round=0,
            errors=[],
            total_cost=0.0,
        )

    def _carried_reports(self, prior) -> list:
        """Prior reports for the reviewers that are not re-running this round.

        With ``only_reviewers`` set, the panel that runs is a subset — but the
        panel that was *assessed* is still all eight. Seeding the state with
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
                    "new_issues": [],
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

    def _manuscript_diff(self, prior, sections: dict[str, str]):
        """Compare this draft against the one the prior round reviewed.

        The previous text comes from the ingest cache by key, so no second
        copy is kept on disk. A cleared cache costs the diff, not the run.
        """
        if prior is None:
            return None
        if is_correction(self.config):
            # Nothing changed by definition — the complaint is about the
            # review, not the draft. Reporting "no changes" as a finding would
            # read as the authors having ignored the letter.
            return ingest_diff.unavailable(
                "this is a correction to the previous review, not a revised "
                "draft: the manuscript is unchanged and was not re-compared"
            )
        if not prior.manuscript_cache_key:
            return ingest_diff.unavailable(
                "the previous round did not record a manuscript cache key"
            )
        cached = ingest_cache.get(prior.manuscript_cache_key, self.config)
        if cached is None:
            return ingest_diff.unavailable(
                "the previous draft is no longer in the manuscript cache"
            )
        return ingest_diff.diff_sections(cached.sections, sections)

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
            _title, text, _sections = load_manuscript(str(path), self.config)
        except Exception as exc:  # noqa: BLE001
            raise ValueError(
                f"Could not read the author statement at {path}: {exc}"
            ) from exc
        return text

    def _load_supplement(self) -> tuple[str, dict[str, str]]:
        """Parse the optional supplementary-information file, or ('', {}) if none.

        The SI is optional: no ``supplement_path`` means an unchanged run. A
        provided-but-unparseable SI must never crash the review, so any parse
        failure degrades to no-SI rather than propagating.
        """
        path = self.config.get("supplement_path")
        if not path:
            return "", {}
        try:
            _title, md, sections = load_manuscript(path, self.config)
        except Exception:  # noqa: BLE001 — optional input, never fail the run
            return "", {}
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
