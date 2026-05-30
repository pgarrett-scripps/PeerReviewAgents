"""Helpers shared by all agent nodes: tool loop, prompt-cache markup,
frontmatter parsing, cost capture.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.utils.function_calling import convert_to_openai_tool

if TYPE_CHECKING:
    from .agent_states import ReviewState

_MAX_TOOL_STEPS = 4


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


# ---------------------------------------------------------------------------
# Core tool loop
# ---------------------------------------------------------------------------


def run_agent(
    llm,
    system_prompt: str,
    user_prompt: str,
    tools: list | None = None,
    *,
    cached_prefix: str | None = None,
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
        bound_tools = [convert_to_openai_tool(t) for t in tools]
        model = llm.bind(tools=bound_tools)
    else:
        model = llm

    use_cache_marker = _cache_control_supported(llm)
    messages = [
        SystemMessage(content=system_prompt),
        _user_message(user_prompt, cached_prefix, with_cache_marker=use_cache_marker),
    ]
    cost_total = 0.0
    final_resp: AIMessage | None = None
    for _ in range(_MAX_TOOL_STEPS):
        resp: AIMessage = model.invoke(messages)
        cost_total += _call_cost(resp)
        messages.append(resp)
        calls = getattr(resp, "tool_calls", None) or []
        if not calls:
            final_resp = resp
            break
        for call in calls:
            fn = tool_map.get(call["name"])
            result = fn.invoke(call["args"]) if fn else f"[unknown tool {call['name']}]"
            messages.append(ToolMessage(content=str(result), tool_call_id=call["id"]))
    if final_resp is None:
        # Tool budget exhausted: ask for a final answer; keep tools bound
        # so the API accepts the tool-call / tool-result history.
        final_resp = model.invoke(messages + [HumanMessage(content="Now produce your final answer.")])
        cost_total += _call_cost(final_resp)

    return RunResult(text=_text(final_resp.content), cost=cost_total)


def _cache_control_supported(llm) -> bool:
    """Whether ``llm`` accepts ``cache_control: ephemeral`` content-block markers.

    Anthropic direct (``ChatAnthropic``) supports it natively; OpenRouter
    forwards it to Anthropic-class providers. OpenAI direct does not
    accept unknown content-block keys, so we strip the marker there.
    """
    cls_name = type(llm).__name__
    if cls_name == "ChatAnthropic":
        return True
    if cls_name == "ChatOpenAI":
        base_url = str(
            getattr(llm, "openai_api_base", "")
            or getattr(llm, "base_url", "")
            or ""
        )
        return "openrouter" in base_url.lower()
    return False


def _user_message(
    user_prompt: str,
    cached_prefix: str | None,
    *,
    with_cache_marker: bool = True,
) -> HumanMessage:
    """Build the user message, optionally with a cache-controlled prefix block."""
    if cached_prefix is None:
        return HumanMessage(content=user_prompt)
    if with_cache_marker:
        return HumanMessage(
            content=[
                {"type": "text", "text": cached_prefix, "cache_control": {"type": "ephemeral"}},
                {"type": "text", "text": user_prompt},
            ]
        )
    # No provider-side cache markup available: just concatenate so we
    # don't pay the content-block overhead for nothing.
    return HumanMessage(content=f"{cached_prefix}\n\n{user_prompt}")


def _call_cost(resp: AIMessage) -> float:
    """Best-effort extraction of OpenRouter's reported USD cost."""
    meta = getattr(resp, "response_metadata", None) or {}
    usage = meta.get("token_usage") or meta.get("usage") or {}
    cost = usage.get("cost")
    if cost is None:
        return 0.0
    try:
        return float(cost)
    except (TypeError, ValueError):
        return 0.0


def _text(content) -> str:
    if isinstance(content, list):
        return "".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in content)
    return str(content)


# ---------------------------------------------------------------------------
# YAML frontmatter — single source of machine-readable scalars
# ---------------------------------------------------------------------------
#
# Every agent emits markdown with an optional YAML frontmatter block at
# the top, e.g.:
#
#     ---
#     score: 4
#     confidence: 4
#     ---
#     # Methodology Review
#     ...
#
# The body (markdown after the frontmatter) is the canonical report
# rendered on disk and in the webapp; the frontmatter only carries
# scalars that downstream code needs (score, confidence, decision).


def split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Parse YAML-ish frontmatter from the top of ``text``.

    Tolerant: only ``key: value`` lines are supported (string values, no
    nesting). Falls back to ``({}, text)`` when no frontmatter is found,
    so a model that forgets the block doesn't break the pipeline.
    """
    if not isinstance(text, str) or not text.startswith("---"):
        return {}, text
    # Allow the opening fence to be followed by \n or \r\n.
    rest = text[3:]
    if rest.startswith("\r\n"):
        rest = rest[2:]
    elif rest.startswith("\n"):
        rest = rest[1:]
    else:
        return {}, text
    # Find a closing fence on its own line.
    for sep in ("\n---\n", "\r\n---\r\n", "\n---\r\n", "\r\n---\n"):
        idx = rest.find(sep)
        if idx >= 0:
            block, body = rest[:idx], rest[idx + len(sep):]
            break
    else:
        # No closing fence — treat the whole text as body.
        return {}, text
    data: dict[str, str] = {}
    for line in block.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, _, value = line.partition(":")
        data[key.strip()] = value.strip().strip("'\"")
    return data, body


def body_only(text: str) -> str:
    """Return the markdown body with any frontmatter stripped."""
    _, body = split_frontmatter(text)
    return body


def coerce_int(value: Any, *, default: int, lo: int, hi: int) -> int:
    """Clamp ``value`` to ``[lo, hi]``; return ``default`` if not coercible."""
    try:
        n = int(float(value))
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


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
        budget = state["config"].get("manuscript_char_budget", 60000)

    if len(text) <= budget:
        return text

    sections: dict[str, str] = state.get("sections") or {}
    if sections:
        parts: list[str] = []
        total = 0
        for name in _PRIORITY_SECTIONS:
            content = sections.get(name, "")
            if not content:
                continue
            chunk = f"## {name.title()}\n\n{content}"
            if total + len(chunk) > budget:
                break
            parts.append(chunk)
            total += len(chunk)
        if len(parts) >= 4:
            return "\n\n".join(parts)

    return text[:budget] + "\n\n[...manuscript truncated...]"


def manuscript_block(state: ReviewState) -> str:
    """Return the cache-eligible manuscript block used by every agent that
    sends the manuscript text. Centralizing the wrapper format keeps the
    block byte-identical across reviewers, debaters, meta-reviewer,
    author rebuttal, and editor so they all share the same provider-side
    cache entry."""
    return f"=== MANUSCRIPT ===\n{fit_manuscript(state)}\n=== END MANUSCRIPT ==="


# ---------------------------------------------------------------------------
# Reviewer score aggregation (decision anchor)
# ---------------------------------------------------------------------------


def score_summary(state: ReviewState) -> str:
    """Confidence-weighted reviewer score + verdict distribution.

    Pipeline stages downstream of the reviewers (meta-reviewer, author
    rebuttal, editor) work entirely in prose, which lets their verdicts
    drift from the panel's actual numbers. Injecting this single line
    into their prompts anchors the decision in the aggregated signal —
    they can still argue against it, but they have to do so explicitly.
    """
    reports = state.get("reports") or []
    if not reports:
        return "(no reviewer scores yet)"

    total_w = sum(r["confidence"] for r in reports) or 1.0
    weighted = sum(r["score"] * r["confidence"] for r in reports) / total_w
    raw = sum(r["score"] for r in reports) / len(reports)

    # Per-reviewer line so the synthesizer can see who pushed where.
    per_reviewer = "; ".join(
        f"{r['reviewer']} {r['score']:.0f}/5@{r['confidence']:.0f}"
        for r in reports
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
        f"(unweighted: {raw:.2f}/5, n={len(reports)})\n"
        f"Verdict distribution: {distrib}\n"
        f"Per-reviewer: {per_reviewer}"
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
