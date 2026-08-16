"""Structured-output helper for agents.

Wraps ``llm.with_structured_output(schema)`` to:

  * pick the provider's preferred extraction method
    (``ProviderSpec.structured_method`` — ``json_schema`` for the direct
    OpenAI and Anthropic APIs, ``function_calling`` for OpenRouter);
  * capture cost via ``include_raw=True`` so the per-call cost line in
    the TUI / summary still works;
  * retry once with a sharpened prompt on schema-validation failure
    before letting the exception bubble up to the agent's existing
    try/except.

Two entry points:

  * :func:`invoke_structured` — one shot, no tool loop. Used by agents
    that don't call external tools (every agent today except novelty +
    literature reviewers).
  * :func:`invoke_structured_after_tools` — run the existing tool loop
    to free text, then a second structured call extracts the schema.
    Two LLM calls; cost is summed.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from pydantic import BaseModel, Field

from ...observability import AgentEvent, current_node, emit
from ...runtime.providers import spec_for_llm
from ..schemas import NO_SCORE_NO_REASON
from .agent_utils import (
    DEFAULT_CACHE_TTL,
    _build_messages,
    _cache_control_supported,
    _call_cost,
    _text,
    run_agent,
)

# Transient provider/transport failures (e.g. OpenRouter "Provider returned
# error", rate limits, dropped connections) surface as exceptions from
# ``structured.invoke``. Retry the call a few times with linear backoff
# before letting it bubble up to the agent's node-level error handler, so a
# single upstream blip doesn't silently drop an agent from the run.
_MAX_PROVIDER_ATTEMPTS = 3
_RETRY_BACKOFF_S = 2.0

# How many times to ask a model to fix an answer the schema rejected. This is
# a different failure from the transport retries above: the call succeeded and
# the content was wrong.
#
# Three, because the failure is stochastic rather than deterministic — the same
# manuscript on the same model lost two reviewers on one run and none on the
# next — and because the cost is lopsided. A repair round is a fraction of a
# cent; the alternative is a verdict decided by seven reviewers with no note of
# which one is missing.
_MAX_REPAIR_ROUNDS = 3


@dataclass(frozen=True)
class StructuredResult:
    """What every structured call returns: parsed instance + summed cost."""

    instance: BaseModel
    cost: float
    warnings: tuple[str, ...] = ()
    raw_text: str = ""
    score_source: str = ""


@dataclass(frozen=True)
class MarkdownResult:
    """A durable prose response and the cost of every attempt that produced it."""

    text: str
    cost: float
    warnings: tuple[str, ...] = ()


# Boundaries and orchestration labels occur in agent inputs, but have no place
# in an authored review. Their reappearance means the model copied its prompt
# or the manuscript wrapper instead of completing the task. This deliberately
# checks content integrity, not headings or review style.
_INTERNAL_ECHO_MARKERS = (
    "=== MANUSCRIPT ===",
    "=== END MANUSCRIPT ===",
    "=== BEGIN OUTPUT ===",
    "=== BEGIN CRITIQUE ===",
    "### FORM REQUIREMENTS:",
)


def _markdown_problem(text: str, _user_prompt: str, min_chars: int) -> str:
    """Return why prose is unusable, or ``""`` when it can be retained."""
    if len(text) < min_chars:
        return "returned no text" if not text else f"returned only {len(text)} characters"
    # These are private orchestration boundaries and are never legitimate
    # authored output. Some live echoes copied them from `cached_prefix`, not
    # from `user_prompt`, so conditioning the check on one prompt component
    # let a 60k-character manuscript dump through the generation boundary.
    echoed = [marker for marker in _INTERNAL_ECHO_MARKERS if marker in text]
    if echoed:
        return "echoed internal prompt boundary " + repr(echoed[0])
    return ""


def invoke_markdown(
    llm,
    config: dict,
    system_prompt: str,
    user_prompt: str,
    *,
    tools: list | None = None,
    cached_prefix: str | Sequence[str] | None = None,
    min_chars: int = 80,
) -> MarkdownResult:
    """Generate ordinary Markdown with bounded retries and no output schema.

    This is the default boundary for substantive agents. The first complete
    prose response is the artifact; no parser or validator can replace it.
    A blank, refusal-like, or interrupted response gets fresh prose retries.
    If every attempt is unusable, the node fails honestly instead of asking
    for a large structured object whose formatting can erase the work.
    """
    total = 0.0
    warnings: list[str] = []
    last_text = ""
    last_error = ""
    try:
        attempts = max(1, int(config.get("markdown_attempts") or 3))
    except (TypeError, ValueError):
        attempts = 3
    for attempt in range(attempts):
        retry_user = user_prompt
        retry_tools = tools or []
        if attempt:
            retry_tools = []
            retry_user = (
                user_prompt
                + "\n\nYour previous response was missing, interrupted, or too short "
                "to contain the requested work. Write the complete substantive "
                "answer now as ordinary Markdown. Do not return JSON or call a tool."
            )
        try:
            result = run_agent(
                llm,
                system_prompt,
                retry_user,
                retry_tools,
                cached_prefix=cached_prefix,
                cache_ttl=config.get("cache_ttl") or DEFAULT_CACHE_TTL,
            )
            total += result.cost
            last_text = (result.text or "").strip()
            problem = _markdown_problem(last_text, user_prompt, min_chars)
            if not problem:
                if attempt:
                    warnings.append(
                        "initial Markdown generation was unusable; retained the "
                        "successful schema-free prose retry"
                    )
                return MarkdownResult(last_text, total, tuple(warnings))
            last_error = problem
        except Exception as exc:  # noqa: BLE001
            total += float(getattr(exc, "cost", 0.0) or 0.0)
            last_error = f"failed with {type(exc).__name__}: {exc}"
        if not attempt:
            warnings.append(f"initial Markdown generation {last_error}")
    raise ValueError(
        f"Markdown generation failed after {attempts} prose attempts; " + last_error
    )


def invoke_structured(
    llm,
    schema: type[BaseModel],
    config: dict,
    system_prompt: str,
    user_prompt: str,
    *,
    cached_prefix: str | Sequence[str] | None = None,
) -> StructuredResult:
    """One-shot structured call. No tool loop.

    Raises ``ValueError`` if the schema can't be filled after a retry —
    agents catch this in their existing ``except`` block and report a
    node-level error.
    """
    messages = _build_messages(
        system_prompt, user_prompt, cached_prefix,
        cache_supported=_cache_control_supported(llm),
        cache_ttl=config.get("cache_ttl") or DEFAULT_CACHE_TTL,
    )
    return _try_structured(llm, schema, messages, config=config)


def extract_structured_metadata(
    llm,
    schema: type[BaseModel],
    config: dict,
    markdown: str,
) -> StructuredResult | None:
    """Best-effort sidecar extraction from an already-durable prose artifact.

    Failure returns ``None`` rather than invalidating ``markdown``. Use this
    only for machine indexes or security-filtered derivatives, never as the
    source of the substantive report.
    """
    messages = [
        SystemMessage(content=(
            "Extract the requested metadata exactly from the supplied Markdown. "
            "Do not reassess, add, soften, or remove substantive claims."
        )),
        HumanMessage(content=markdown),
    ]
    try:
        return _try_structured(llm, schema, messages, config=config)
    except Exception:  # noqa: BLE001
        return None


# Shortest plausible agent answer. A real review runs to thousands of
# characters; the shortest legitimate one in the corpus — an ethics review of
# a computational reanalysis with no human subjects — is over a thousand. The
# floor is set well below that, because the cost of being wrong is one
# tools-free retry and the cost of being right is not publishing a fabricated
# verdict.
MIN_AGENT_TEXT_CHARS = 400


def invoke_structured_after_tools(
    llm,
    schema: type[BaseModel],
    config: dict,
    system_prompt: str,
    user_prompt: str,
    tools: list,
    *,
    cached_prefix: str | Sequence[str] | None = None,
) -> StructuredResult:
    """Tool-loop reviewers: run :func:`run_agent` for free text, then a
    second call extracts the schema from that text.

    Two LLM calls; cost is summed. This avoids the awkward interaction
    between tool-calling and structured-output binding (LangChain wraps
    both as tool calls, and combining them gets brittle across providers).

    If the tool loop itself fails (a provider rejecting the tool request —
    e.g. the Sonnet-only ``tools.0 function`` 400 — a tool vendor being down,
    or a rate limit), we fall back to a tools-free structured pass so this
    reviewer still contributes a verdict from the manuscript alone rather than
    dropping out of the panel. The fallback is logged so the lost web-search
    grounding is visible.

    **An empty first call takes the same route, and must.** The extraction
    prompt asks a model to convert "the assistant text below"; hand it nothing
    and a well-behaved model answers the prompt it was actually given. Observed
    on nvidia/nemotron-3-ultra, which is a reasoning model and returned its
    whole response as reasoning tokens with empty content:

        # Novelty & Contribution Reviewer
        ## Summary
        The user requested conversion of assistant text to JSON per a schema,
        but neither the source text nor the schema were included.
        ## Weaknesses
        - Missing required inputs: assistant text and schema

    That validated against ReviewerOutput, scored the paper 1/5, and flowed
    into the panel mean and the editor's verdict. Nothing caught it: no
    exception, no schema error, ``scored_count`` 8 of 8. Two of the three
    tool-using agents failed this way in one run and the review published a
    fabricated score about real authors' work.

    So a blank response is a failed call, not a response. It is the same
    failure as the tool loop raising, and it takes the same fallback.
    """
    def _without_tools(reason: str, sunk_cost: float = 0.0) -> StructuredResult:
        emit(AgentEvent(
            kind="log",
            node=current_node(),
            text=f"{reason}; reviewing without research tools",
        ))
        # Also on the tool channel, so the published bundle records it. A
        # review that lost its literature grounding is otherwise
        # indistinguishable from one that kept it: same verdict shape, same
        # cost band, no missing file, nothing for a reader to notice.
        emit(AgentEvent(kind="tool", node=current_node(), tool_error=reason))
        if schema.__name__ == "ReviewerOutput":
            fallback = invoke_markdown(
                llm,
                config,
                system_prompt,
                user_prompt,
                tools=[],
                cached_prefix=cached_prefix,
                min_chars=MIN_AGENT_TEXT_CHARS,
            )
            parsed = _reviewer_from_markdown(llm, schema, fallback.text)
            return StructuredResult(
                instance=parsed.instance,
                cost=sunk_cost + fallback.cost + parsed.cost,
                warnings=tuple(dict.fromkeys((
                    reason,
                    *fallback.warnings,
                    *parsed.warnings,
                ))),
                raw_text=fallback.text,
                score_source=parsed.score_source,
            )
        fallback = invoke_structured(
            llm, schema, config, system_prompt, user_prompt, cached_prefix=cached_prefix
        )
        # ``sunk_cost`` is what the discarded tool loop was billed. A too-short
        # answer still ran the whole research phase — every lookup invoiced —
        # and reporting only the fallback's cost undercounted the agent by
        # exactly the most expensive calls it made.
        return StructuredResult(
            instance=fallback.instance, cost=fallback.cost + sunk_cost
        )

    try:
        free = run_agent(
            llm, system_prompt, user_prompt, tools,
            cached_prefix=cached_prefix,
            cache_ttl=config.get("cache_ttl") or DEFAULT_CACHE_TTL,
        )
    except Exception as exc:  # noqa: BLE001
        return _without_tools(
            f"tool loop failed ({type(exc).__name__}: {exc})",
            sunk_cost=float(getattr(exc, "cost", 0.0) or 0.0),
        )

    text = (free.text or "").strip()
    problem = _markdown_problem(text, user_prompt, MIN_AGENT_TEXT_CHARS)
    if problem:
        # Not just empty. "Let me verify a few more key citations before
        # finalizing my audit." is 68 characters, and the emptiness test
        # accepted it as a completed audit — see the forced-final note in
        # agent_utils. Anything this short is a model that was interrupted or
        # refused, not a review, and it takes the same fallback as a blank.
        return _without_tools(
            "tool loop " + problem,
            sunk_cost=free.cost,
        )

    # Reviewer prose is the durable artifact. Do not send a complete review
    # through another large all-or-nothing schema merely to recover three
    # scalar fields: accept ordinary Markdown, parse explicit metadata
    # deterministically, and use a tiny normalizer only when necessary.
    if schema.__name__ == "ReviewerOutput":
        markdown = _reviewer_from_markdown(llm, schema, text)
        return StructuredResult(
            instance=markdown.instance,
            cost=free.cost + markdown.cost,
            warnings=markdown.warnings,
            raw_text=text,
            score_source=markdown.score_source,
        )

    extraction_sys = (
        "Convert the assistant text below into a structured JSON object "
        "matching the given schema. Preserve every concrete claim verbatim; "
        "do not invent new content."
    )
    messages = [
        SystemMessage(content=extraction_sys),
        HumanMessage(content=free.text),
    ]
    extracted = _try_structured(llm, schema, messages, config=config)
    return StructuredResult(
        instance=extracted.instance,
        cost=free.cost + extracted.cost,
    )


def _try_structured(
    llm,
    schema: type[BaseModel],
    messages: list,
    *,
    config: dict,
) -> StructuredResult:
    spec = spec_for_llm(llm)
    structured = _bind(llm, schema, spec.structured_method)
    result = _invoke_with_retries(structured, messages)
    parsed, cost = _unpack(result)
    if parsed is not None:
        return StructuredResult(instance=parsed, cost=cost)

    # Ask again quoting the validator's own complaint, and keep asking, up to
    # _MAX_REPAIR_ROUNDS times.
    #
    # Quoting the error is what makes the ask answerable. "That was not valid"
    # asks the model to guess which of a dozen fields was wrong, and on a
    # semantic constraint it cannot guess: a reviewer that returns a null score
    # with no reason reads "strictly matching the schema" as a formatting note,
    # returns the same null, and its entire review — summary, strengths,
    # weaknesses, questions, already written and paid for — is thrown away.
    # Observed live on three of eight reviewers in one run.
    #
    # Asking more than once is what makes it reliable. This failure is
    # stochastic, not deterministic: the same manuscript, on the same model,
    # on the same code, lost data_analysis and methodology on one run and kept
    # all eight reviewers on the next. Meeting a coin flip with a single
    # re-ask leaves a quarter of the bad flips unrecovered, and the cost is
    # asymmetric — a repair round is a fraction of a cent, while the failure
    # decides the paper on seven reviewers and never says which one is absent
    # from its reasoning.
    #
    # Bounded, because a model that cannot satisfy a constraint in three tries
    # will not satisfy it in ten; the abstention repair and the salvage below
    # are the honest fallbacks for that case.
    total = cost
    for _ in range(_MAX_REPAIR_ROUNDS):
        retry_msg = HumanMessage(content=(
            f"Your previous response did not produce a valid {schema.__name__}. "
            f"The validation error was:\n\n{_parsing_error(result)}\n\n"
            "Respond again with the whole object, fixing exactly that. Keep the "
            "rest of your answer as it was. No prose, no extra fields."
        ))
        # "Keep the rest of your answer as it was" is only satisfiable if the
        # model can see what it was. The retry used to send the original
        # conversation with the correction alone appended — no rejected turn in
        # it — so "your previous response" pointed at nothing and every retry
        # was a blind full regeneration of an answer already written and paid
        # for. Each round replays the most recent rejection, not the first.
        result = _invoke_with_retries(
            structured, messages + _rejected_turn(result) + [retry_msg]
        )
        parsed, round_cost = _unpack(result)
        total += round_cost
        if parsed is not None:
            return StructuredResult(instance=parsed, cost=total)

    repaired = _repair_abstention(llm, schema, result)
    if repaired is not None:
        return StructuredResult(instance=repaired.instance, cost=total + repaired.cost)

    repaired = _repair_editor_summary(llm, schema, result, messages)
    if repaired is not None:
        return StructuredResult(instance=repaired.instance, cost=total + repaired.cost)

    salvaged = _salvage(schema, result)
    if salvaged is not None:
        return StructuredResult(instance=salvaged, cost=total)

    salvaged_editor = _salvage_editor_decision(schema, result)
    if salvaged_editor is not None:
        return StructuredResult(
            instance=salvaged_editor,
            cost=total,
            warnings=(
                "editor summary was reconstructed from the editor's verdict "
                "and required revisions after structured-output repair failed",
            ),
        )

    prose = _via_prose(llm, schema, messages, spec)
    if prose is not None:
        return StructuredResult(instance=prose.instance, cost=total + prose.cost)

    err = _parsing_error(result)
    raise ValueError(
        f"structured-output validation failed after {_MAX_REPAIR_ROUNDS} "
        f"repair attempts and a prose fallback: {err}"
    )


def _via_prose(llm, schema: type[BaseModel], messages: list, spec) -> StructuredResult | None:
    """Last resort: drop the schema, ask for the review, then extract it.

    Everything above this asks the same failing question again in a firmer
    voice. This asks a different one. A model that cannot fill a twelve-field
    object under a tool-call constraint can usually still write the review in
    prose — the difficulty is the formatting contract, not the manuscript —
    and a second, much smaller call turns that prose into the object.

    The alternative is what this replaces: the agent raises, its node records
    an error, and the panel is one reviewer short. On the manuscript that
    prompted this, data_analysis was lost on two runs out of three, and it is
    the reviewer that owns most of what the human referees raised. A degraded
    review from the right specialist beats a silent absence.

    Two calls, both cheap, and it never raises: if the prose is too thin to be
    a review, or the extraction fails in its turn, this returns None and the
    caller reports the original validation error. The extraction is given only
    the prose, not the manuscript, so it cannot invent content the reviewer
    did not write.
    """
    ask = HumanMessage(content=(
        "Ignore the required output format. Write your review as plain prose: "
        "an overall assessment, then the specific weaknesses you found, then "
        "any questions for the authors. Do not return JSON or a tool call."
    ))
    try:
        drafted = llm.invoke(messages + [ask])
    except Exception:  # noqa: BLE001 - a failed fallback must cost nothing
        return None
    text = _text(getattr(drafted, "content", ""))
    if len(text.strip()) < MIN_AGENT_TEXT_CHARS:
        return None
    cost = _call_cost(drafted)

    extractor = _bind(llm, schema, spec.structured_method)
    extract_ask = [
        SystemMessage(content=(
            "Convert the review below into the required object. Use only what "
            "it says — do not add findings, soften them, or invent a score the "
            "reviewer did not imply."
        )),
        HumanMessage(content=text),
    ]
    try:
        extracted = _invoke_with_retries(extractor, extract_ask)
    except Exception:  # noqa: BLE001
        return None
    instance, extract_cost = _unpack(extracted)
    if instance is None:
        return None
    return StructuredResult(instance=instance, cost=cost + extract_cost)


def _rejected_turn(result: Any) -> list:
    """The invalid response, replayed as history the retry can refer back to.

    A structured answer usually arrives as a tool call, and a replayed tool
    call left unanswered is itself an invalid transcript — both Anthropic and
    OpenAI 400 on a tool_use with no tool_result — so each call the rejected
    turn made is answered with a stub before the human correction follows.

    An empty raw (no content, no tool calls) is not replayed: there is
    nothing in it to keep, and Anthropic rejects an assistant turn with
    empty content.
    """
    raw = result.get("raw") if isinstance(result, dict) else None
    if raw is None:
        return []
    calls = getattr(raw, "tool_calls", None) or []
    content = getattr(raw, "content", None)
    if not calls and not content:
        return []
    turn: list = [raw]
    for call in calls:
        turn.append(ToolMessage(
            content="[this response failed schema validation — see the next message]",
            tool_call_id=call["id"],
        ))
    return turn


# What the model filled in, when the object it filled in was rejected.
#
# A schema failure is not the same event as an agent failing. The reviewer
# below wrote a summary, three strengths, seven weaknesses and five questions,
# then left one field blank — and the whole review was discarded, unread,
# after the model had been paid to write it. Twice, on two different runs. The
# editor was then told the panel had returned no review on that dimension.
#
# So: rescue the answer when the only thing wrong with it is a rule about
# abstaining. A reviewer that gives no score and no reason gets the marker
# below written into the field it left empty, which is what the report then
# prints — the abstention is published as unexplained rather than dressed up
# as a considered "nothing here to judge".
#
# Deliberately narrow. A response truncated mid-JSON is not salvaged: half a
# review is not a review, and the fix for that one is the token cap.
NO_REASON_GIVEN = NO_SCORE_NO_REASON  # the renderers in schemas.py key on it
_NO_REASON_GIVEN = NO_REASON_GIVEN


class _ScoreRepair(BaseModel):
    """The two fields the abstention rule is about, and nothing else.

    The full schema is where the failure happens: a model that has just
    written a long review omits the score at the end of a large object. Asked
    the same question through a two-field schema, the same model answers
    reliably — measured 4/4 recoveries on the model that produced 4/8
    unexplained abstentions through the full schema on one live run.
    """

    score: int | None = Field(None, ge=1, le=5)
    not_applicable_reason: str = ""


class _EditorSummaryRepair(BaseModel):
    """The one editor field whose failure must not discard a real verdict."""

    summary_of_evaluation: str


class _ReviewMetadata(BaseModel):
    """Small normalization boundary for an already-written prose review."""

    score: int | None = Field(None, ge=1, le=5)
    confidence: int | None = Field(None, ge=1, le=5)
    not_applicable_reason: str = ""


_SCORE_LINE = re.compile(
    r"(?im)^\s*(?:[-*#>]\s*)*(?:\*\*)?"
    r"(?:score|rating|recommendation)(?:\*\*)?\s*[:=-]?\s*(?:\*\*)?\s*"
    r"(n\s*/?\s*a|null|[1-5](?:\.0)?(?:\s*/\s*5)?|"
    r"accept|minor(?:\s+revision)?|major(?:\s+revision)?|reject)\b"
)
_CONFIDENCE_LINE = re.compile(
    r"(?im)^\s*(?:[-*#>]\s*)*(?:\*\*)?confidence(?:\*\*)?"
    r"\s*[:=-]?\s*(?:\*\*)?\s*([1-5](?:\.0)?)(?:\s*/\s*5)?\b"
)
_NA_REASON_LINE = re.compile(
    r"(?im)^\s*(?:[-*#>]\s*)*(?:\*\*)?"
    r"(?:not[_ -]?applicable[_ -]?reason|n\s*/?\s*a\s+reason)"
    r"(?:\*\*)?\s*[:=-]\s*(?:\*\*)?\s*(.+)$"
)


def _reviewer_from_markdown(llm, schema: type[BaseModel], text: str) -> StructuredResult:
    """Build reviewer metadata without making Markdown formatting load-bearing."""
    score, explicit_na = _explicit_score(text)
    confidence = _explicit_confidence(text)
    reason_match = _NA_REASON_LINE.search(text)
    reason = reason_match.group(1).strip() if reason_match else ""
    source = "explicit" if score is not None or explicit_na else ""
    cost = 0.0

    score_was_missing = score is None and not explicit_na
    if score_was_missing or confidence is None:
        normalized = _normalize_review_metadata(llm, text)
        if normalized is not None:
            fixed, cost = normalized
            if score_was_missing:
                score = fixed.score
                reason = reason or fixed.not_applicable_reason.strip()
            if confidence is None:
                confidence = fixed.confidence
            if score_was_missing and fixed.score is not None:
                source = "normalized"

    warnings: list[str] = []
    if score is None:
        reason = reason or NO_SCORE_NO_REASON
        source = source or "unavailable"
        if reason == NO_SCORE_NO_REASON:
            warnings.append(
                "review Markdown was preserved, but no score could be extracted"
            )
    if confidence is None:
        # Confidence only weights a numeric score. One is conservative and
        # prevents missing metadata from amplifying an inferred judgment.
        confidence = 1
        warnings.append(
            "review Markdown was preserved, but no confidence could be extracted; using 1"
        )

    sections = _markdown_review_sections(text)
    summary = sections["summary"] or _markdown_summary(text)
    if _review_item_is_placeholder(summary) or len(summary.strip()) < 25:
        summary = _markdown_summary(text)
    payload = {
        "score": score,
        "not_applicable_reason": reason,
        "confidence": confidence,
        "summary": summary,
        "strengths": _clean_review_items(sections["strengths"]),
        "weaknesses": _clean_review_items(sections["weaknesses"]),
        "questions": _clean_review_items(sections["questions"]),
    }
    # The complete Markdown has already cleared the substantive artifact
    # checks. These fields are a lossy convenience index, not an agent output
    # contract, so ReviewerOutput's structured-generation validators must not
    # be allowed to discard the prose. In particular, a perfectly ordinary
    # `Questions: None.` used to validate as the placeholder list ["None."],
    # erase an otherwise complete ethics review, and fail the whole panel.
    # Constructing the typed carrier without validation keeps downstream field
    # access while making the saved Markdown—not a section parser—authoritative.
    instance = schema.model_construct(**payload)
    return StructuredResult(
        instance=instance,
        cost=cost,
        warnings=tuple(dict.fromkeys(warnings)),
        raw_text=text,
        score_source=source,
    )


def _review_item_is_placeholder(value: str) -> bool:
    stripped = re.sub(r"^[\s>*#`_\-]+|[\s.*`_\-]+$", "", str(value or "")).lower()
    return not stripped or stripped in {
        "n/a", "na", "none", "nil", "no questions", "not applicable", "tbd", "todo",
    }


def _clean_review_items(items: list[str]) -> list[str]:
    """Drop empty/placeholder section bullets from the lossy metadata index."""
    return [str(item).strip() for item in items if not _review_item_is_placeholder(item)]


def _normalize_review_metadata(llm, text: str) -> tuple[_ReviewMetadata, float] | None:
    """Extract three scalars from prose; never regenerate or rejudge the review."""
    ask = [
        SystemMessage(content=(
            "Extract metadata from the review exactly as written. Do not "
            "reassess the manuscript. If the reviewer states no score, leave "
            "score null; infer a score only when an explicit categorical "
            "recommendation (accept/minor/major/reject) is present."
        )),
        HumanMessage(content=text),
    ]
    spec = spec_for_llm(llm)
    try:
        result = _invoke_with_retries(
            _bind(llm, _ReviewMetadata, spec.structured_method), ask
        )
    except Exception:  # noqa: BLE001
        return None
    parsed, cost = _unpack(result)
    return (parsed, cost) if isinstance(parsed, _ReviewMetadata) else None


def _explicit_score(text: str) -> tuple[int | None, bool]:
    match = _SCORE_LINE.search(text)
    if not match:
        numeric = re.search(
            r"(?i)\b(?:score|rating)\s+(?:of\s+|is\s+)?([1-5])"
            r"(?:\.0)?(?:\s*/\s*5|\s+out\s+of\s+5)?\b",
            text,
        )
        if numeric:
            return int(numeric.group(1)), False
        categorical = re.search(
            r"(?i)\b(?:recommend|recommendation\s+is|verdict\s+is)\s+"
            r"(accept|minor\s+revision|major\s+revision|reject)\b",
            text,
        )
        if not categorical:
            return None, False
        match_value = categorical.group(1).lower()
        return {
            "accept": 5,
            "minor revision": 4,
            "major revision": 3,
            "reject": 1,
        }[match_value], False
    value = re.sub(r"\s+", " ", match.group(1).strip().lower())
    if value in {"n/a", "n / a", "na", "null"}:
        return None, True
    mapping = {
        "accept": 5,
        "minor": 4,
        "minor revision": 4,
        "major": 3,
        "major revision": 3,
        "reject": 1,
    }
    if value in mapping:
        return mapping[value], False
    number = re.match(r"[1-5]", value)
    return (int(number.group(0)), False) if number else (None, False)


def _explicit_confidence(text: str) -> int | None:
    match = _CONFIDENCE_LINE.search(text)
    if match:
        return int(float(match.group(1)))
    inline = re.search(
        r"(?i)\bconfidence\s+(?:of\s+|is\s+)?([1-5])"
        r"(?:\.0)?(?:\s*/\s*5|\s+out\s+of\s+5)?\b",
        text,
    )
    return int(inline.group(1)) if inline else None


def _markdown_review_sections(text: str) -> dict[str, Any]:
    """Best-effort section extraction; absent headings are fully acceptable."""
    aliases = {
        "summary": {"summary", "assessment", "overall assessment"},
        "strengths": {"strength", "strengths"},
        "weaknesses": {"weakness", "weaknesses", "concerns", "major concerns"},
        "questions": {"question", "questions", "questions for the authors"},
    }
    found: dict[str, list[str]] = {key: [] for key in aliases}
    current = ""
    for raw in text.splitlines():
        stripped = raw.strip()
        label = re.sub(r"^[#>*\s-]+|[*:#\s-]+$", "", stripped).lower()
        new = next((key for key, names in aliases.items() if label in names), "")
        if new:
            current = new
            continue
        if stripped.startswith("#"):
            current = ""
            continue
        if current and stripped and not _SCORE_LINE.match(raw) and not _CONFIDENCE_LINE.match(raw):
            found[current].append(stripped)

    def items(lines: list[str]) -> list[str]:
        if not lines:
            return []
        bullets = [
            re.sub(r"^\s*(?:[-*+] |\d+[.)]\s*)", "", line).strip()
            for line in lines
            if re.match(r"^\s*(?:[-*+] |\d+[.)]\s*)", line)
        ]
        if bullets:
            return [x for x in bullets if x]
        paragraph = " ".join(lines).strip()
        return [paragraph] if paragraph else []

    return {
        "summary": " ".join(found["summary"]).strip(),
        "strengths": items(found["strengths"]),
        "weaknesses": items(found["weaknesses"]),
        "questions": items(found["questions"]),
    }


def _markdown_summary(text: str) -> str:
    cleaned = _SCORE_LINE.sub("", text)
    cleaned = _CONFIDENCE_LINE.sub("", cleaned)
    cleaned = _NA_REASON_LINE.sub("", cleaned)
    cleaned = re.sub(r"(?m)^\s*#{1,6}\s+.*$", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:2000]


def _repair_abstention(llm, schema: type[BaseModel], result: Any) -> StructuredResult | None:
    """One targeted ask before an unexplained abstention is published.

    Observed live on a long manuscript: a reviewer writes the complete
    review — summary, strengths, seven weaknesses — and returns a null score
    with no reason, twice. Half the panel abstaining that way skews the mean
    over whoever happened to comply, so before the salvage path publishes
    "gave no score and did not say why", the model is shown its own review
    once and asked for the score it implies, or the missing reason.

    The number is still the model's own: a repair that fails, abstains again
    without a reason, or produces an object the schema rejects changes
    nothing, and the salvage below publishes the abstention as unexplained.
    """
    payload = _tool_args(result)
    if not isinstance(payload, dict) or not payload:
        return None
    if "not_applicable_reason" not in schema.model_fields:
        return None
    if payload.get("score") is not None or str(payload.get("not_applicable_reason") or "").strip():
        return None  # rejected for some other reason; not ours to second-guess
    quote = _review_quote(payload)
    if not quote:
        return None  # nothing of the review survived to quote back

    ask = HumanMessage(content=(
        "You wrote the review below and returned it without a score and "
        "without a reason for abstaining.\n\n"
        f"{quote}\n\n"
        "Either give the 1-5 score your own review implies (1=reject, "
        "3=major revision, 4=minor revision, 5=accept), or the one-sentence "
        "reason this dimension has nothing to judge in this manuscript. Do "
        "not soften or revisit the review itself."
    ))
    spec = spec_for_llm(llm)
    structured = _bind(llm, _ScoreRepair, spec.structured_method)
    try:
        repair_result = _invoke_with_retries(structured, [ask])
    except Exception:  # noqa: BLE001 - a failed repair must cost nothing
        return None
    fixed, cost = _unpack(repair_result)
    if fixed is None:
        return None

    merged = dict(payload)
    if fixed.score is not None:
        merged["score"] = fixed.score
    elif fixed.not_applicable_reason.strip():
        merged["not_applicable_reason"] = fixed.not_applicable_reason.strip()
    else:
        return None
    try:
        return StructuredResult(instance=schema.model_validate(merged), cost=cost)
    except Exception:  # noqa: BLE001 - fall through to the plain salvage
        return None


def _repair_editor_summary(
    llm,
    schema: type[BaseModel],
    result: Any,
    messages: list,
) -> StructuredResult | None:
    """Regenerate only a defective editor synthesis and keep its other fields.

    Some models repeatedly return a real verdict and actionable revision list
    with ``summary_of_evaluation='...'``. Re-running the whole editor object
    lets already-correct fields drift and still often reproduces the same
    placeholder. A one-field schema makes the actual missing task explicit.
    The original editor context remains in ``messages``, so the repair can
    synthesize the panel rather than inventing from the malformed payload.
    """
    payload = _tool_args(result)
    if not isinstance(payload, dict) or not payload:
        return None
    if "summary_of_evaluation" not in schema.model_fields:
        return None
    existing = str(payload.get("summary_of_evaluation") or "").strip()
    try:
        schema.model_validate(payload)
        return None
    except Exception as exc:  # noqa: BLE001
        if "summary_of_evaluation" not in str(exc):
            return None

    ask = HumanMessage(content=(
        "Keep the editor's decision, required revisions, and minor suggestions "
        "unchanged. Return only a replacement summary_of_evaluation that "
        "synthesizes the panel evidence, explains the tradeoff, and states why "
        f"it leads to the verdict. The prior summary was {existing!r} and was "
        "rejected as non-substantive. Write at least 200 characters."
    ))
    spec = spec_for_llm(llm)
    structured = _bind(llm, _EditorSummaryRepair, spec.structured_method)
    total = 0.0
    for _ in range(2):
        try:
            repair_result = _invoke_with_retries(structured, messages + [ask])
        except Exception:  # noqa: BLE001
            return None
        fixed, cost = _unpack(repair_result)
        total += cost
        if fixed is None:
            continue
        try:
            merged = {**payload, "summary_of_evaluation": fixed.summary_of_evaluation}
            return StructuredResult(instance=schema.model_validate(merged), cost=total)
        except Exception:  # noqa: BLE001
            continue
    return None


def _review_quote(payload: dict) -> str:
    """The review the model wrote, compact enough to quote back to it."""
    parts: list[str] = []
    summary = str(payload.get("summary") or "").strip()
    if summary:
        parts.append(summary)
    for w in (payload.get("weaknesses") or [])[:6]:
        text = w.get("text") if isinstance(w, dict) else w
        if str(text or "").strip():
            parts.append(f"- {str(text).strip()}")
    return "\n".join(parts)[:2000]


def _salvage(schema: type[BaseModel], result: Any) -> BaseModel | None:
    payload = _tool_args(result)
    if not isinstance(payload, dict) or not payload:
        return None
    if "not_applicable_reason" not in schema.model_fields:
        return None
    if payload.get("score") is not None or str(payload.get("not_applicable_reason") or "").strip():
        # Rejected for some other reason; not ours to second-guess.
        return None
    try:
        return schema.model_validate({**payload, "not_applicable_reason": _NO_REASON_GIVEN})
    except Exception:  # noqa: BLE001
        return None


def _salvage_editor_decision(schema: type[BaseModel], result: Any) -> BaseModel | None:
    """Keep a revision verdict when only its prose synthesis is unusable.

    This is deliberately narrower than inventing an editorial opinion. It
    works only for minor/major payloads that already contain concrete required
    revisions, and states transparently that the original synthesis was not
    usable. Every substantive item in the replacement is copied from the
    editor's own structured payload.
    """
    payload = _tool_args(result)
    if not isinstance(payload, dict) or "summary_of_evaluation" not in schema.model_fields:
        return None
    decision = str(payload.get("decision") or "").strip()
    revisions = [
        str(x).strip()
        for x in (payload.get("required_revisions") or [])
        if str(x).strip()
    ]
    if decision not in {"minor", "major"} or not revisions:
        return None
    joined = "; ".join(revisions)
    summary = (
        f"The editor returned a {decision} revision decision and identified "
        f"the following requirements as load-bearing: {joined}. These items "
        "must be resolved before the manuscript meets the stated review "
        "standard. The editor's original narrative synthesis was unusable, "
        "so this transparent fallback preserves only its verdict and explicit "
        "requirements; it adds no new scientific findings."
    )
    try:
        return schema.model_validate({**payload, "summary_of_evaluation": summary})
    except Exception:  # noqa: BLE001
        return None


def _tool_args(result: Any) -> Any:
    """The object the model sent, when it parsed as JSON but not as the schema.

    Two places to look, because a model answers a schema in whichever of the
    two ways it likes and the choice is not ours: as a tool call, or as a JSON
    object written into ordinary content. The first version of this looked
    only at tool calls, which is why a literature reviewer that wrote its JSON
    into content was still discarded after the salvage path existed.

    Returns None for a response cut off mid-object — there is nothing whole to
    recover there.
    """
    if not isinstance(result, dict):
        return None
    raw = result.get("raw")
    candidates: list[Any] = []

    candidates += [c.get("args") for c in (getattr(raw, "tool_calls", None) or [])]
    candidates += [
        c.get("args") for c in (getattr(raw, "invalid_tool_calls", None) or [])
    ]

    extra = getattr(raw, "additional_kwargs", None) or {}
    for call in extra.get("tool_calls") or []:
        function = call.get("function") or {}
        candidates.append(function.get("arguments"))

    candidates.append(getattr(raw, "content", None))

    error = result.get("parsing_error")
    try:
        candidates += [e.get("input") for e in error.errors(include_url=False)]
    except (AttributeError, TypeError, ValueError):
        pass

    for candidate in candidates:
        if isinstance(candidate, dict) and candidate:
            return candidate
        parsed = _json_object(candidate)
        if parsed:
            return parsed
    return None


def _json_object(content: Any) -> dict | None:
    """The outermost JSON object in a content payload, or None."""
    if isinstance(content, list):
        # Anthropic-style content blocks.
        content = "".join(
            b.get("text", "") for b in content if isinstance(b, dict)
        )
    if not isinstance(content, str):
        return None
    start, end = content.find("{"), content.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        parsed = json.loads(content[start : end + 1])
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _invoke_with_retries(structured, messages: list) -> Any:
    """Invoke a structured-output chain, retrying transient provider errors.

    Retries up to ``_MAX_PROVIDER_ATTEMPTS`` times on any exception raised by
    ``invoke`` (provider/transport failures), with linear backoff between
    attempts. The final exception is re-raised if every attempt fails, so the
    agent's existing try/except still records a node-level error. Schema
    *validation* failures don't raise here (they return ``parsed=None``) and
    are handled separately by the caller's sharpened-prompt retry.
    """
    last_exc: Exception | None = None
    for attempt in range(_MAX_PROVIDER_ATTEMPTS):
        try:
            return structured.invoke(messages)
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt < _MAX_PROVIDER_ATTEMPTS - 1:
                time.sleep(_RETRY_BACKOFF_S * (attempt + 1))
    assert last_exc is not None
    raise last_exc


def _bind(llm, schema: type[BaseModel], method: str):
    """Wrap llm in a structured-output chain. Falls back gracefully if
    the provider doesn't support the requested method or ``include_raw``
    (older LangChain versions / non-standard providers)."""
    try:
        return llm.with_structured_output(schema, method=method, include_raw=True)
    except (TypeError, ValueError) as exc:
        # Logged, because falling back silently is how a dead flag hides:
        # the registry shipped ``method="tool_call"`` — not a LangChain
        # method name at all — and every bind raised here and quietly
        # rebound with the library default, so the ProviderSpec's declared
        # preference read as honored while never once being used.
        emit(AgentEvent(
            kind="log",
            node=current_node(),
            text=(
                f"structured method {method!r} rejected by "
                f"{type(llm).__name__} ({exc}); falling back to the library default"
            ),
        ))
    try:
        return llm.with_structured_output(schema, include_raw=True)
    except (TypeError, ValueError):
        pass
    return llm.with_structured_output(schema)


def _unpack(result: Any) -> tuple[BaseModel | None, float]:
    """Extract (parsed_instance, cost_usd) from either include_raw dict
    or a bare parsed instance."""
    if isinstance(result, dict):
        parsed = result.get("parsed")
        raw = result.get("raw")
        cost = _call_cost(raw) if raw is not None else 0.0
        return parsed, cost
    if isinstance(result, BaseModel):
        return result, 0.0
    return None, 0.0


def _parsing_error(result: Any) -> Any:
    if not isinstance(result, dict):
        return repr(result)
    # A response cut off at max_tokens arrives here as an empty or half-written
    # tool call, and the parser reports it as "Invalid json output:" with
    # nothing after the colon — which reads like the model ignored the schema
    # when in fact it ran out of room mid-answer. Naming it costs nothing and
    # is the difference between a fixable finding and a mystery.
    if _stop_reason(result.get("raw")) == "max_tokens":
        return (
            "response hit the max_tokens cap before the schema was complete "
            "(raise max_tokens for this agent, or ask it for less)"
        )
    return result.get("parsing_error") or "unknown"


def _stop_reason(raw: Any) -> str:
    meta = getattr(raw, "response_metadata", None) or {}
    return str(meta.get("stop_reason") or meta.get("finish_reason") or "")
