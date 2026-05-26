"""Helpers shared by all agent nodes: a small tool loop and report parsing."""

from __future__ import annotations

import re

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from .agent_states import ReviewReport

_MAX_TOOL_STEPS = 4


def run_agent(llm, system_prompt: str, user_prompt: str, tools: list | None = None) -> str:
    """Run a chat turn, executing any tool calls (bounded), return final text."""
    tools = tools or []
    tool_map = {t.name: t for t in tools}
    model = llm.bind_tools(tools) if tools else llm

    messages = [SystemMessage(content=system_prompt), HumanMessage(content=user_prompt)]
    for _ in range(_MAX_TOOL_STEPS):
        resp: AIMessage = model.invoke(messages)
        messages.append(resp)
        calls = getattr(resp, "tool_calls", None) or []
        if not calls:
            return _text(resp.content)
        for call in calls:
            fn = tool_map.get(call["name"])
            result = fn.invoke(call["args"]) if fn else f"[unknown tool {call['name']}]"
            messages.append(ToolMessage(content=str(result), tool_call_id=call["id"]))
    # Tool budget exhausted: ask for a final answer with no tools.
    final = llm.invoke(messages + [HumanMessage(content="Now write your final report.")])
    return _text(final.content)


def _text(content) -> str:
    if isinstance(content, list):
        return "".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in content)
    return str(content)


def _grab(pattern: str, text: str, default: float) -> float:
    m = re.search(pattern, text, re.IGNORECASE)
    if not m:
        return default
    try:
        return float(m.group(1))
    except ValueError:
        return default


def _bullets(text: str, header: str) -> list[str]:
    """Extract bullet lines under a markdown header until the next header."""
    m = re.search(rf"#+\s*{header}.*?\n(.*?)(?=\n#+\s|\Z)", text, re.IGNORECASE | re.DOTALL)
    if not m:
        return []
    return [ln.strip(" -*\t") for ln in m.group(1).splitlines() if ln.strip().startswith(("-", "*"))]


def parse_report(text: str, reviewer: str) -> ReviewReport:
    """Parse a reviewer's markdown into a structured ReviewReport."""
    summary = ""
    sm = re.search(r"#+\s*Summary\s*\n(.*?)(?=\n#+\s|\Z)", text, re.IGNORECASE | re.DOTALL)
    if sm:
        summary = sm.group(1).strip()[:1000]
    return ReviewReport(
        reviewer=reviewer,
        summary=summary,
        strengths=_bullets(text, "Strengths"),
        weaknesses=_bullets(text, "Weaknesses"),
        questions=_bullets(text, "Questions"),
        score=_grab(r"score\s*[:=]?\s*([1-5](?:\.\d)?)", text, 3.0),
        confidence=_grab(r"confidence\s*[:=]?\s*([0-5](?:\.\d)?)", text, 3.0),
        body=text,
    )
