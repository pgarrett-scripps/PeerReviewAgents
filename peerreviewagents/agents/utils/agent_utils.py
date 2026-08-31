"""Helpers shared by all agent nodes: tool loop, prompt-cache markup,
manuscript truncation, score aggregation, cost capture.
"""

from __future__ import annotations

from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from ...observability import AgentEvent, current_node, emit, estimate_cost
from ..schemas import NO_SCORE_NO_REASON

if TYPE_CHECKING:
    from .agent_states import ReviewState

# Rounds of tool calling, not tool calls: one round is one model turn, and a
# model may ask for several lookups in it. At four rounds every tool-using
# agent hit the ceiling — a citation audit that wanted sixteen lookups spent
# them in four turns of four — which meant the forced-final path below was not
# an exception, it was how every researched review in the corpus got written.
_MAX_TOOL_STEPS = 8
# Rounds alone do not bound the work: a model batching six lookups a round
# would run 48. This caps the lookups themselves.
_MAX_TOOL_CALLS = 24

# How many of one round's lookups run at once. Small deliberately: these are
# third-party APIs with their own rate limits, and firing a whole round at
# them earns 429s that cost more than the concurrency saves.
_TOOL_FANOUT = 4

# What to say when the budget runs out.
#
# It used to say "Now produce your final answer." Asked that, DeepSeek replied
# "Let me verify a few more key citations before finalizing my audit." — 68
# characters, non-empty, and so accepted as the finished audit. The extraction
# step turned it into "no findings could be extracted" and the bundle published
# HARD gaps (blocking): 0 for a manuscript nothing had audited.
#
# The model was not confused. It was interrupted, told something that reads
# like an aside, and answered it like one. So this says the three things that
# settle it: no further tools will run, this turn is the whole output, and
# here is the task again.
_FORCED_FINAL = (
    "The research budget for this task is now spent. No further tool calls "
    "will be executed — ask for one and it is discarded, with nothing put in "
    "its place.\n\n"
    "Write your complete final answer now, from the manuscript and the "
    "results already gathered above. This turn is the entirety of your "
    "output: there is no later turn to finish in, and a reply saying you "
    "intend to continue will be published as though it were your findings.\n\n"
    "The task, restated:\n\n{task}"
)

# How long a prompt-cache entry should live. The provider's default is 5
# minutes; a full review takes ten to twenty, and every stage after the panel
# runs sequentially, so the gap between one agent's request and the next
# routinely exceeds five minutes on a long paper.
#
# When that happens the entry is gone and the next agent rewrites the whole
# manuscript. Measured on C-01: 479,205 tokens of cache writes against 32,795
# of reads — about fifteen manuscripts written and one read, on a run whose
# generations had just got substantially longer.
#
# A 1h write is billed at 2x base against the 5m write's 1.25x, so this is a
# real trade and not free. It is still overwhelmingly right here: the downside
# is 0.75 of one manuscript when nothing would have expired, and the upside is
# the fourteen rewrites above. A workload with ~20 reads per write wants the
# entry to outlive the run.
DEFAULT_CACHE_TTL = "1h"


# ---------------------------------------------------------------------------
# Run result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RunResult:
    """What every agent gets back from ``run_agent``.

    `text` is the model's final assistant content (raw string, markdown
    by convention). `cost` is the OpenRouter-reported USD cost summed
    across every invocation in the tool loop (best-effort — 0.0 if the
    provider didn't surface a cost field).
    """

    text: str
    cost: float


class EmptyModelResponse(RuntimeError):
    """The provider completed a model turn without publishable content."""

    def __init__(self, message: str, *, cost: float = 0.0):
        super().__init__(message)
        self.cost = cost


# ---------------------------------------------------------------------------
# Core tool loop
# ---------------------------------------------------------------------------


def run_agent(
    llm,
    system_prompt: str,
    user_prompt: str,
    tools: list | None = None,
    *,
    cached_prefix: str | Sequence[str] | None = None,
    cache_ttl: str = DEFAULT_CACHE_TTL,
) -> RunResult:
    """Run a chat turn, executing any tool calls (bounded), return final text.

    Tool calls are looped locally up to ``_MAX_TOOL_STEPS`` rounds; if the
    model still wants to call another tool after that, we force a final
    answer.

    Args:
        cached_prefix: optional text block placed at the start of the user
            message. On providers that honor ``cache_control: ephemeral``
            markers (Anthropic direct, OpenRouter-routed Anthropic) this
            block is sent as a separate cacheable content block so the
            manuscript text is served from the provider cache across the
            parallel reviewer fan-out. On other providers (OpenAI direct)
            the prefix is concatenated with the user prompt as plain text.
    """
    tools = tools or []
    tool_map = {t.name: t for t in tools}

    if tools:
        # bind_tools() converts to the active provider's tool format
        # (Anthropic native vs OpenAI `function`); passing pre-converted
        # OpenAI-format tools via bind() 400s on the Anthropic API.
        model = llm.bind_tools(tools)
    else:
        model = llm

    messages = _build_messages(
        system_prompt, user_prompt, cached_prefix,
        cache_supported=_cache_control_supported(llm),
        cache_ttl=cache_ttl,
    )
    cost_total = 0.0
    final_resp: AIMessage | None = None
    calls_made = 0
    for _ in range(_MAX_TOOL_STEPS):
        resp: AIMessage = model.invoke(messages)
        cost_total += _call_cost(resp, cache_ttl)
        messages.append(resp)
        calls = getattr(resp, "tool_calls", None) or []
        if not calls:
            final_resp = resp
            break
        if calls_made >= _MAX_TOOL_CALLS:
            # The assistant turn asking for these calls is already in the
            # history. Breaking with its tool_calls unanswered left a
            # transcript ending [tool_use, human], which both Anthropic and
            # OpenAI reject outright ("tool_use without tool_result", 400) —
            # so the forced-final request below always failed, the caller
            # fell back to a tools-free rerun, and every lookup this loop had
            # gathered was discarded at the exact moment the budget declared
            # the research finished. Answer each pending call with a stub so
            # the transcript stays valid and the results already in it are
            # what the final answer is written from.
            for call in calls:
                messages.append(ToolMessage(
                    content=(
                        "[not executed: the research call budget for this "
                        "task was exhausted before this call ran]"
                    ),
                    tool_call_id=call["id"],
                ))
            break
        calls_made += len(calls)
        for call, (result, failure) in zip(calls, _run_round(calls, tool_map)):
            _record_tool_call(call, result, failure)
            messages.append(ToolMessage(content=result, tool_call_id=call["id"]))
    if final_resp is None:
        # Tool budget exhausted: ask for a final answer; keep tools bound
        # so the API accepts the tool-call / tool-result history.
        emit(AgentEvent(
            kind="log",
            node=current_node(),
            text=f"tool budget spent after {calls_made} lookups; forcing a final answer",
        ))
        final_resp = model.invoke(
            messages + [HumanMessage(content=_FORCED_FINAL.format(task=user_prompt))]
        )
        cost_total += _call_cost(final_resp, cache_ttl)

    text = _text(final_resp.content)
    if not text.strip():
        raise EmptyModelResponse(
            "model returned empty content; " + _empty_response_diagnostics(final_resp),
            cost=cost_total,
        )
    return RunResult(text=text, cost=cost_total)


def _empty_response_diagnostics(resp: AIMessage) -> str:
    """Describe an empty response without revealing hidden reasoning text."""
    meta = getattr(resp, "response_metadata", None) or {}
    extra = getattr(resp, "additional_kwargs", None) or {}
    usage = getattr(resp, "usage_metadata", None) or {}
    raw_usage = meta.get("token_usage") or meta.get("usage") or {}
    reasoning = extra.get("openrouter_reasoning") or ""
    reasoning_details = extra.get("openrouter_reasoning_details") or []
    output_details = usage.get("output_token_details") or {}
    reasoning_tokens = (
        output_details.get("reasoning")
        or (raw_usage.get("completion_tokens_details") or {}).get("reasoning_tokens")
        or 0
    )
    facts = [
        f"finish_reason={meta.get('finish_reason') or meta.get('stop_reason') or 'unknown'}",
        f"model={meta.get('model_name') or meta.get('model') or 'unknown'}",
        f"output_tokens={usage.get('output_tokens') or raw_usage.get('completion_tokens') or 0}",
        f"reasoning_tokens={reasoning_tokens}",
        f"reasoning_chars={len(str(reasoning))}",
        f"reasoning_details={len(reasoning_details) if isinstance(reasoning_details, list) else 1}",
    ]
    return ", ".join(facts)


def _run_one(call: dict, tool_map: dict) -> tuple[str, str]:
    """One lookup. Returns (result, failure) — never raises.

    A vendor being down must not take the reviewer down with it: the failure
    is handed back to the model as a tool result and it carries on without
    that lookup, as it would an empty search.
    """
    fn = tool_map.get(call["name"])
    if fn is None:
        return f"[unknown tool {call['name']}]", "unknown tool"
    try:
        return str(fn.invoke(call["args"])), ""
    except Exception as exc:  # noqa: BLE001
        return f"[tool error: {type(exc).__name__}]", f"{type(exc).__name__}: {exc}"


def _run_round(calls: list[dict], tool_map: dict) -> list[tuple[str, str]]:
    """Every lookup the model asked for in one turn, at the same time.

    These were sequential, and that was most of what a researched review cost
    in wall-clock. The model asks for several lookups in a single turn — they
    are independent by construction, since it had to name them all before
    seeing any result — and each one is an HTTP round trip to a vendor that
    may be slow, rate-limiting, or retrying. Measured on one manuscript: a
    citation audit spent 753s and a literature reviewer 3764s, against
    non-searching reviewers on the same panel that finished in 30-90s. The
    searching agents were the entire critical path of every run.

    Results stay in call order because the caller zips them back against
    `calls` to build the ToolMessages, and a tool_result carrying the wrong
    tool_call_id is a 400 from both Anthropic and OpenAI.

    The pool is small on purpose. These are third-party APIs with their own
    rate limits, and firing twenty at once earns 429s that cost more than the
    concurrency saves — the point is to stop paying for six round trips in
    series, not to hammer arXiv.
    """
    if len(calls) < 2:
        return [_run_one(call, tool_map) for call in calls]
    with ThreadPoolExecutor(max_workers=min(len(calls), _TOOL_FANOUT)) as pool:
        return list(pool.map(lambda c: _run_one(c, tool_map), calls))


def _record_tool_call(call: dict, result: str, failure: str) -> None:
    """Put one research lookup on the observability bus.

    Published, not just logged. A reviewer that cites prior work is making a
    different claim depending on whether it searched for that work or recalled
    it, and nothing else in the record distinguishes the two — the tool path
    costs more whether or not a tool is ever called, so cost cannot answer it.

    ``hits`` counts the entries in the rendered list the research router
    returns, which is a heuristic over its output format rather than a count
    the vendor supplied. Zero means the search ran and found nothing, which is
    itself a finding: it is the evidence behind "no prior art located".
    """
    args = call.get("args") or {}
    query = args.get("query") or next((str(v) for v in args.values() if v), "")
    hits = 0 if failure else sum(
        1 for line in result.splitlines() if line.lstrip().startswith("- ")
    )
    try:
        from ...research.interface import last_vendor
        vendor = last_vendor()
    except Exception:  # noqa: BLE001
        # Research extras not installed, or a non-research tool. Not knowing
        # the vendor must not cost us the rest of the record.
        vendor = ""
    emit(AgentEvent(
        kind="tool",
        node=current_node(),
        tool_name=str(call.get("name") or ""),
        tool_query=str(query)[:200],
        tool_hits=hits,
        tool_vendor=vendor,
        tool_error=failure,
    ))


def _cache_control_supported(llm) -> bool:
    """Whether ``llm`` accepts ``cache_control: ephemeral`` content-block markers.

    Anthropic direct (``ChatAnthropic``) supports it natively; OpenRouter
    forwards it to Anthropic-class providers. OpenAI direct does not
    accept unknown content-block keys, so we strip the marker there.
    """
    # MRO names, not the leaf class name: the OpenRouter factory builds a
    # ChatOpenAI subclass (providers._chat_openrouter_class), which must keep
    # its cache-control support.
    mro_names = {c.__name__ for c in type(llm).__mro__}
    if "ChatAnthropic" in mro_names:
        return True
    if "ChatOpenAI" in mro_names:
        base_url = str(
            getattr(llm, "openai_api_base", "")
            or getattr(llm, "base_url", "")
            or ""
        )
        return "openrouter" in base_url.lower()
    return False


def _build_messages(
    system_prompt: str,
    user_prompt: str,
    cached_prefix: str | Sequence[str] | None,
    *,
    cache_supported: bool = True,
    cache_ttl: str = DEFAULT_CACHE_TTL,
) -> list:
    """Assemble ``[system, user]`` with the cached prefix as leading blocks.

    Putting the manuscript first — ahead of each agent's own system prompt —
    makes the cached region byte-identical across every agent that sends the
    same manuscript, so a single provider-side cache entry is shared instead
    of one being written per agent-specific prefix. The agent's system_prompt
    and the user_prompt sit *after* the last cache breakpoint, so they don't
    fragment the key. This is what lets the desk-screen warmer prime a cache
    the parallel reviewer fan-out then reads.

    ``cached_prefix`` may be several blocks, each of which gets its own
    breakpoint. Anthropic caches incrementally — a request matches the longest
    cached prefix and writes only the increment beyond it — so blocks ordered
    general-to-specific let agents that share a *prefix* of the blocks share
    that part of the cache even when their later blocks differ. The manuscript
    goes first for exactly this reason: agents that also send the venue
    directives read the manuscript entry and write only the directives on top,
    rather than writing a second copy of the whole manuscript.

    ``cache_ttl`` sets how long an entry survives — see
    :data:`DEFAULT_CACHE_TTL` for why the default is not the provider's.

    On providers that don't honor ``cache_control`` (OpenAI direct) the blocks
    are folded into the system prompt as plain text — same ordering, no marker.
    """
    blocks = [cached_prefix] if isinstance(cached_prefix, str) else list(cached_prefix or [])
    blocks = [b for b in blocks if b and b.strip()]
    if not blocks:
        return [SystemMessage(content=system_prompt), HumanMessage(content=user_prompt)]
    if cache_supported:
        control: dict[str, Any] = {"type": "ephemeral"}
        if cache_ttl and cache_ttl != "5m":
            control["ttl"] = cache_ttl
        system_content: Any = [
            {"type": "text", "text": b, "cache_control": dict(control)}
            for b in blocks
        ]
        system_content.append({"type": "text", "text": system_prompt})
    else:
        system_content = "\n\n".join([*blocks, system_prompt])
    return [SystemMessage(content=system_content), HumanMessage(content=user_prompt)]


def _call_cost(resp: AIMessage, cache_ttl: str | None = None) -> float:
    """Best-effort USD cost for a single model call.

    OpenRouter reports actual spend on the response (``usage.cost``, kept on
    ``response_metadata["token_usage"]`` by the streaming subclass in
    ``runtime.providers``), and that number is authoritative — it is what the
    account was actually billed, for any model OpenRouter serves, with no
    pricing-table row to go stale. Anthropic and OpenAI direct report no
    spend, only token counts, so those are priced from the static table.
    Without the fallback the whole ``total_cost`` chain reads 0.0 on every
    direct-API run, including the figure written into summary.md and any
    downstream provenance record.
    """
    meta = getattr(resp, "response_metadata", None) or {}
    usage = meta.get("token_usage") or meta.get("usage") or {}

    cost = usage.get("cost")
    if cost is not None:
        try:
            return float(cost)
        except (TypeError, ValueError):
            pass  # malformed vendor field — fall through to the estimate

    tokens = getattr(resp, "usage_metadata", None) or {}
    in_tok = tokens.get("input_tokens") or usage.get("prompt_tokens") or usage.get("input_tokens")
    out_tok = (
        tokens.get("output_tokens") or usage.get("completion_tokens") or usage.get("output_tokens")
    )
    if not in_tok and not out_tok:
        return 0.0

    model = meta.get("model_name") or meta.get("model") or ""
    read, written = cache_tokens(resp)
    return estimate_cost(
        model,
        int(in_tok or 0),
        int(out_tok or 0),
        cache_read_tokens=read,
        cache_write_tokens=written,
        # A 1h write bills at 2x and a 5m one at 1.25x, so the TTL this call
        # actually used is what prices it. Omitted, estimate_cost assumes 1h,
        # which is what this pipeline configures.
        cache_ttl=cache_ttl,
    )


def cache_tokens(resp: AIMessage) -> tuple[int, int]:
    """``(cache_read, cache_write)`` input tokens for one call, or ``(0, 0)``.

    Both are already counted inside ``usage_metadata["input_tokens"]`` — see
    :func:`peerreviewagents.observability.estimate_cost`, which subtracts them
    back out. This reads LangChain's normalized ``input_token_details`` and
    falls back to Anthropic's raw field names, because a response that came
    straight off the SDK rather than through the LangChain usage adapter
    carries the raw spelling and would otherwise report a cache-free call.
    """
    details = (getattr(resp, "usage_metadata", None) or {}).get("input_token_details") or {}
    raw = (getattr(resp, "response_metadata", None) or {}).get("usage") or {}
    read = details.get("cache_read") or raw.get("cache_read_input_tokens") or 0
    written = details.get("cache_creation") or raw.get("cache_creation_input_tokens") or 0
    return int(read or 0), int(written or 0)


def _text(content) -> str:
    if content is None:
        return ""
    if isinstance(content, list):
        return "".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in content)
    return str(content)


# ---------------------------------------------------------------------------
# Section-aware manuscript truncation
# ---------------------------------------------------------------------------


_PRIORITY_SECTIONS: list[str] = [
    "abstract",
    "introduction",
    "methods",
    "materials and methods",
    "methodology",
    "results",
    "discussion",
    "conclusion",
    "conclusions",
]


def fit_manuscript(state: ReviewState, budget: int | None = None) -> str:
    """Return the manuscript text fitted to `budget` chars.

    If the full markdown fits, return it unchanged. Otherwise prefer
    keeping abstract + methods + results + discussion + conclusion;
    drop supplementary/appendix content first; fall back to a tail
    truncation if section parsing didn't yield enough structure.
    """
    text: str = state["manuscript_md"]
    if budget is None:
        budget = state["config"].get("manuscript_char_budget")

    # No budget set (None/0) -> send the full manuscript, no truncation.
    if not budget or len(text) <= budget:
        return text

    sections: dict[str, str] = state.get("sections") or {}
    if sections:
        parts: list[str] = []
        kept: set[str] = set()
        total = 0
        for name in _PRIORITY_SECTIONS:
            content = sections.get(name, "")
            if not content:
                continue
            chunk = f"## {name.title()}\n\n{content}"
            if total + len(chunk) > budget:
                # Skip, don't stop: this used to `break`, so one oversized
                # methods section ended the packing and took results,
                # discussion and conclusion down with it — sections that
                # would each have fit on their own.
                continue
            parts.append(chunk)
            kept.add(name)
            total += len(chunk)
        if len(parts) >= 4:
            # Every notice in this file names its own damage — compression,
            # PDF conversion — but the fitted manuscript said nothing, so a
            # reviewer faulted the authors for having no limitations section
            # when one exists in the PDF and was dropped here. Named in the
            # section dict's own (document) order, which is fixed for a given
            # manuscript + budget, so the manuscript block built on this text
            # stays byte-identical across every agent in a run and the shared
            # prompt-cache entry is not fragmented.
            dropped = [n for n, c in sections.items() if c and n not in kept]
            if dropped:
                parts.append(
                    "[sections omitted to fit the length budget: "
                    + ", ".join(dropped)
                    + "]"
                )
            return "\n\n".join(parts)

    return text[:budget] + "\n\n[...manuscript truncated...]"


# Telegraphic compression strips articles and copulas, which leaves every
# content word standing and not every sentence grammatical. Without this
# notice the clarity reviewer criticises the authors for it — measured, three
# times on a paper whose uncompressed run reported no such problem. It sits
# inside the manuscript block so it stays part of the one shared cached
# prefix rather than forming a second.
_COMPRESSED_NOTICE = (
    "NOTE: this manuscript has been machine-compressed for length — "
    "grammatical scaffolding (articles, copulas{hard}) was stripped "
    "automatically. Missing words are an artifact of that process, not of "
    "the authors' writing. Do not treat the telegraphic style, dropped "
    "function words or broken grammar as a defect of the paper, and quote "
    "from it only as paraphrase: the original wording is not recoverable "
    "from this text."
)

# Same failure as the compression notice above, from a different cause. PDF
# conversion loses spaces between words and leaves hyphens sitting where a
# line used to break, and a reviewer reading that without being told writes up
# the authors for typography no human reader of the PDF would ever see.
#
# Only the *measured* damage is named, and only the kinds that were actually
# found, so the notice never invites a reviewer to discount a real writing
# problem it has no evidence for. A conversion worse than this does not reach
# an agent at all — see ingest.loader.require_readable.
_DEGRADED_NOTICE = (
    "NOTE: this manuscript was converted from PDF and the conversion is "
    "imperfect: {damage}. That damage is the converter's, not the authors' — "
    "the PDF a human reader opens does not contain it. Do not report spacing, "
    "hyphenation, run-together words or broken formatting as a defect of the "
    "paper. Judge the science; where a passage is too mangled to judge, say "
    "you could not read it rather than guessing at what it said."
)

# Health fields, in the order they read best in a sentence, paired with how to
# say each one to a reviewer rather than to an engineer.
_DAMAGE_PHRASES = (
    ("fused_per_1k", "words have run together where spaces were lost"),
    ("hyphen_breaks_per_1k", "hyphens survive from line breaks mid-word"),
    ("missing_space_per_1k", "some sentences run into the next without a space"),
)


def _conversion_notice(state: ReviewState) -> str:
    """The degraded-conversion advisory, or '' when the file converted cleanly.

    Silent on a clean read. A warning that appears on every run is one every
    reader — human or model — learns to skip.
    """
    from ...ingest import prose

    ingest = state.get("ingest") or {}
    if prose.verdict_of(ingest) == prose.CLEAN:
        return ""
    health = prose.health_of(ingest)
    damage = [
        phrase for key, phrase in _DAMAGE_PHRASES
        if float(health.get(key) or 0) > 0
    ]
    if not damage:
        # Degraded on section coverage alone: nothing here is about the words
        # themselves, so there is nothing to warn a reviewer about.
        return ""
    return _DEGRADED_NOTICE.format(damage="; ".join(damage)) + "\n\n"


def manuscript_block(state: ReviewState) -> str:
    """Return the cache-eligible manuscript block used by every agent that
    sends the manuscript text. Centralizing the wrapper format keeps the
    block byte-identical across reviewers, debaters, the synthesizer,
    author rebuttal, and editor so they all share the same provider-side
    cache entry.

    Both notices are constant for a whole run, so prepending them costs one
    cache write of a few dozen tokens and nothing per agent after that.
    """
    caveman = (state.get("ingest") or {}).get("caveman")
    notice = ""
    if caveman:
        hard = ", prepositions and connectives" if caveman == "hard" else ""
        notice = _COMPRESSED_NOTICE.format(hard=hard) + "\n\n"
    notice += _conversion_notice(state)
    return f"=== MANUSCRIPT ===\n{notice}{fit_manuscript(state)}\n=== END MANUSCRIPT ==="


def supplement_block(state: ReviewState) -> str:
    """Full supplementary-information block, or '' when no SI was provided.

    Unlike :func:`manuscript_block`, this is deliberately NOT truncated: the
    SI is passed in full to the agents that opt in (currently only the
    methods_completeness auditor), because the reagent / key-resources tables
    and detailed protocols there are exactly what those agents check. Appended
    after the manuscript in an opt-in agent's cached prefix, so it forms its
    own provider-side cache entry without touching the shared fan-out prefix.
    """
    sup = (state.get("supplement_md") or "").strip()
    if not sup:
        return ""
    return (
        "=== SUPPLEMENTARY INFORMATION ===\n"
        f"{sup}\n"
        "=== END SUPPLEMENTARY INFORMATION ==="
    )


# A cap no real bibliography reaches — the longest in the calibration corpus
# is 341 entries — but a survey's can, and a prompt that does not fit is not a
# review. What is dropped is named rather than silently missing, because the
# agent reading this block is the one auditing the list for completeness.
_MAX_RENDERED_REFERENCES = 1000

# Told to the agents that receive the block, because both facts change how it
# should be read: an entry the converter could not type is not in the list,
# and a field it could not parse is absent rather than wrong.
REFERENCES_NOTE = (
    "\n\nA REFERENCE LIST block is included above. It is the bibliography as "
    "the PDF converter typed it — one entry per line, in the manuscript's own "
    "order, with the manuscript's own labels — not a rendering of the prose, "
    "so it survives even where the reference section was truncated out of the "
    "manuscript block or ran together in conversion. Two limits, and both "
    "matter for what you may conclude from it: an entry the converter failed "
    "to type is simply absent from the list, so a citation with no matching "
    "entry is a lead to check against the manuscript text and never on its "
    "own evidence that the reference does not exist; and a field it could not "
    "read confidently is left out rather than guessed, so a missing year or "
    "DOI is a gap in the parse unless the entry as printed also lacks it."
)


def references_block(state: ReviewState) -> str:
    """The typed bibliography as a cached block, or '' when there is none.

    Given only to the agents whose remit is the reference list — the citation
    auditor and the literature reviewer — and appended after the shared
    prefix, so the manuscript and directives blocks every other agent sends
    stay byte-identical and these two write only this block on top of the
    shared entry.

    Worth sending even though the same text is in the manuscript. The
    reference list is not a priority section, so it is the first thing
    :func:`fit_manuscript` drops when a char budget is set — which would take
    the bibliography away from the two agents that exist to check it, on
    exactly the long papers where a budget gets set. And as entries it is a
    list with the manuscript's own labels on it, rather than a two-column
    bibliography flattened into prose that an agent has to re-segment before
    it can check anything.
    """
    entries = state.get("references") or []
    if not entries:
        return ""
    shown = entries[:_MAX_RENDERED_REFERENCES]
    lines = []
    for entry in shown:
        label = str(entry.get("label") or "").strip()
        raw = str(entry.get("raw") or "").strip()
        lines.append(f"[{label}] {raw}" if label else raw)
    if len(entries) > len(shown):
        lines.append(
            f"[... {len(entries) - len(shown)} further entries omitted to fit "
            "the prompt; the manuscript block carries them]"
        )
    return (
        f"=== REFERENCE LIST ({len(entries)} entries) ===\n"
        + "\n".join(lines)
        + "\n=== END REFERENCE LIST ==="
    )


def unreliable_reference_parse(state: ReviewState) -> str:
    """Explain a demonstrably corrupt typed bibliography, or return ``""``.

    This is deliberately narrow. A missing typed list is not enough: Markdown,
    LaTeX and older converters legitimately provide no block model, while the
    bibliography may still be readable in the manuscript text. The signal here
    is a converter claiming to have isolated a substantial reference section
    while typing only a handful of enormous "entries". In the live Jets PDF it
    isolated 6,779 words as references but emitted four appendix-sized entries.
    That is evidence about conversion, never evidence that the authors omitted
    references.
    """
    ingest = state.get("ingest") or {}
    counts = ((ingest.get("prose") or {}).get("counts") or {})
    reference_words = counts.get("reference_words")
    if not isinstance(reference_words, (int, float)) or reference_words < 500:
        return ""

    entries = state.get("references") or []
    typed_count = len(entries)
    if typed_count == 0:
        return ""
    words_per_entry = float(reference_words) / typed_count
    if typed_count <= 10 and words_per_entry >= 250:
        return (
            "the PDF converter isolated approximately "
            f"{int(reference_words):,} reference-section words but typed only "
            f"{typed_count} entries (about {words_per_entry:,.0f} words per "
            "entry), indicating that appendix/bibliography blocks were merged"
        )
    return ""


def directives_block(state: ReviewState) -> str | None:
    """Run-wide framing directives only: journal + article-type + strictness,
    WITHOUT the manuscript text.

    Used by synthesis-stage agents (debate synthesizer, editor) that judge the
    distilled review/debate signal rather than re-reading the primary text:
    they still need to know the venue's bar and the configured strictness
    ("the context above" their system prompts reference), but feeding them
    the full manuscript invites them to re-review instead of synthesize.

    Returns ``None`` when no directives are set so the caller sends a plain
    user message with no cached prefix.
    """
    parts = [
        p
        for p in (
            (state.get("journal_block") or "").strip(),
            (state.get("article_type_block") or "").strip(),
            (state.get("strictness_block") or "").strip(),
        )
        if p
    ]
    return "\n\n".join(parts) if parts else None


def context_block(state: ReviewState) -> list[str]:
    """Cached blocks for an agent that reads the primary text: manuscript,
    then the run's journal / article-type / strictness directives.

    Returned as separate blocks, and in this order, because the order is worth
    real money. Both are constant for a whole run, so either arrangement
    caches — but putting the directives first, as this did, gave the agents
    that read them a prefix sharing nothing with the bare
    :func:`manuscript_block` the debate, rebuttal and scout send. The same
    manuscript was then cached twice, once per group.

    Manuscript first, with a breakpoint after it, makes the two groups share
    one entry: the debaters read it, and the reviewers read it and write only
    the few hundred tokens of directives on top. Measured on a 61,700-token
    paper, the split was writing about 3.6 manuscripts' worth of cache per
    run, and a cache write costs 12.5x what reading one does.
    """
    directives = directives_block(state)
    return [manuscript_block(state), directives] if directives else [manuscript_block(state)]


# ---------------------------------------------------------------------------
# Reviewer score aggregation (decision anchor)
# ---------------------------------------------------------------------------


def score_summary(state: ReviewState) -> str:
    """Confidence-weighted reviewer score + verdict distribution.

    Pipeline stages downstream of the reviewers (debate synthesizer, author
    rebuttal, editor) work entirely in prose, which lets their verdicts
    drift from the panel's actual numbers. Injecting this single line
    into their prompts anchors the decision in the aggregated signal —
    they can still argue against it, but they have to do so explicitly.
    """
    all_reports = state.get("reports") or []
    if not all_reports:
        return "(no reviewer scores yet)"

    # A reviewer that found nothing in its remit returns no score. It is left
    # out of every aggregate here — but still named, because "this paper has
    # no statistics to check" is something the editor should weigh, and a
    # dimension silently vanishing from the panel line would hide it.
    reports = [r for r in all_reports if isinstance(r.get("score"), (int, float))]
    unscored = [r for r in all_reports if not isinstance(r.get("score"), (int, float))]
    if not reports:
        return (
            "(no reviewer could score this manuscript: "
            + ", ".join(r["reviewer"] for r in unscored)
            + " all reported nothing in their dimension to judge)"
        )

    total_w = sum(r["confidence"] for r in reports) or 1.0
    weighted = sum(r["score"] * r["confidence"] for r in reports) / total_w
    raw = sum(r["score"] for r in reports) / len(reports)

    # Per-reviewer line so the synthesizer can see who pushed where.
    per_reviewer = "; ".join(
        f"{r['reviewer']} {r['score']:.0f}/5@{r['confidence']:.0f}"
        for r in reports
    )
    if unscored:
        # Two events share a null score and must not share a label here: a
        # reasoned abstention is a judgment the editor should weigh, while
        # the sentinel marks a reviewer that wrote its assessment and never
        # scored it. Rendering both as "n/a" invited the editor to read a
        # reporting failure as "nothing in my dimension to judge" — on
        # exactly the reports whose bodies argue hardest for a low number.
        per_reviewer += "; " + "; ".join(
            f"{r['reviewer']} no score returned (reporting failure — "
            "its written review still stands)"
            if str(r.get("not_applicable_reason") or "").strip() == NO_SCORE_NO_REASON
            else f"{r['reviewer']} n/a"
            for r in unscored
        )

    buckets: dict[str, int] = {}
    for r in reports:
        buckets[_score_to_verdict(r["score"])] = (
            buckets.get(_score_to_verdict(r["score"]), 0) + 1
        )
    # Stable display order: best-case verdict first.
    ordered = [b for b in ("accept", "minor", "major", "reject") if b in buckets]
    distrib = ", ".join(f"{buckets[b]} {b}" for b in ordered)

    return (
        f"Confidence-weighted reviewer score: {weighted:.2f}/5  "
        f"(unweighted: {raw:.2f}/5, n={len(reports)}"        + (f" of {len(all_reports)}; "
                 + ", ".join(r["reviewer"] for r in unscored)
                 + " not applicable" if unscored else "")
              + ")\n"
        f"Verdict distribution: {distrib}\n"
        f"Per-reviewer: {per_reviewer}"
        + _missing_reviewers_line(state, all_reports)
    )


def _missing_reviewers_line(state: ReviewState, reports: list) -> str:
    """Name any reviewer that was assigned and never reported, or ''.

    A reviewer whose call fails is not in ``reports`` at all — unlike one that
    returned "nothing in my dimension to judge", which is present with a null
    score and already named above. So the aggregate silently narrowed: on C-09
    the rigor reviewer was truncated at max_tokens, dropped out, and the panel
    line read n=7 with nothing to say the eighth had ever been assigned. The
    editor then issued a verdict on seven specialists believing it had the
    whole panel.

    An aggregate over a panel that lost a member is still usable — dropping
    the round would be worse — but the stage weighing it has to know, which is
    the difference between a smaller sample and a misrepresented one.
    """
    from ...agents.reviewers import get_reviewer_nodes

    expected = list(
        state["config"].get("only_reviewers")
        or (name for name, _node in get_reviewer_nodes(state["config"]))
    )
    got = {r["reviewer"] for r in reports}
    missing = [name for name in expected if name not in got]
    if not missing:
        return ""
    return (
        f"\nINCOMPLETE PANEL: {', '.join(missing)} was assigned but returned no "
        "review (the run log records why). The figures above are over the "
        "reviewers that did report; weigh them knowing the panel is short, and "
        "do not read a missing specialty as no concern in it."
    )


def _score_to_verdict(score: float) -> str:
    """Map a 1-5 reviewer score to a verdict bucket.

    Thresholds match the reviewer prompt's own scale:
    5 = accept, 4 = minor, 3 = major, 1-2 = reject.
    """
    if score >= 4.5:
        return "accept"
    if score >= 3.5:
        return "minor"
    if score >= 2.5:
        return "major"
    return "reject"


# ---------------------------------------------------------------------------
# Editorial audit digest (editor-only compliance dossier)
# ---------------------------------------------------------------------------


def audit_digest(state: ReviewState) -> str:
    """Render the audit lane's reports for the editor's prompt.

    Audits are factual compliance checklists routed only to the editor (see
    :class:`~peerreviewagents.agents.utils.agent_states.AuditReport`). This
    folds each auditor's HARD/SOFT gap counts and full report into one block
    so the editor can turn HARD gaps into required revisions. Returns a
    short placeholder when no audits ran.
    """
    audits = state.get("audits") or []
    if not audits:
        return "(no editorial audits were produced)"
    parts: list[str] = []
    for a in audits:
        heading = f"### {a.get('title', a.get('auditor', 'Audit'))}"
        hard, soft = a.get("hard_gaps"), a.get("soft_gaps")
        if isinstance(hard, int) and isinstance(soft, int):
            heading += f" — HARD gaps: {hard}, SOFT gaps: {soft}"
        parts.append(heading)
        parts.append((a.get("body") or "").strip())
    return "\n\n".join(parts)
