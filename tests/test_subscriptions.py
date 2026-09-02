from __future__ import annotations

import json
import subprocess
from pathlib import Path

from langchain_core.messages import HumanMessage
from langchain_core.tools import tool
from pydantic import BaseModel

from peerreviewagents.runtime.providers import make_chat_model, spec_for_llm
from peerreviewagents.runtime.subscriptions import (
    SUBSCRIPTION_PROVIDERS,
    CLIResponse,
    SubscriptionChatModel,
    _run_claude,
    _run_codex,
    _run_droid,
    _run_pi,
)


class Answer(BaseModel):
    verdict: str
    confidence: int


def test_plain_subscription_turn_becomes_an_ai_message():
    calls = []

    def runner(provider, prompt, schema, model, effort, timeout):
        calls.append((provider, prompt, schema, model, effort, timeout))
        return CLIResponse({"content": "A complete review"}, 120, 30, 20)

    llm = SubscriptionChatModel("codex", model="default", runner=runner)
    response = llm.invoke([HumanMessage(content="Review this")])

    assert response.content == "A complete review"
    assert response.usage_metadata["input_tokens"] == 120
    assert response.usage_metadata["input_token_details"]["cache_read"] == 20
    assert response.response_metadata["token_usage"]["cost"] == 0.0
    assert calls[0][0] == "codex"
    assert "Review this" in calls[0][1]
    assert calls[0][2]["required"] == ["content"]


def test_structured_subscription_turn_validates_pydantic():
    def runner(*_args):
        return CLIResponse({"verdict": "major", "confidence": 4})

    llm = SubscriptionChatModel("claude-code", runner=runner)
    result = llm.with_structured_output(Answer, include_raw=True).invoke("Decide")

    assert result["parsed"] == Answer(verdict="major", confidence=4)
    assert result["parsing_error"] is None
    assert json.loads(result["raw"].content)["verdict"] == "major"


def test_bound_research_tools_round_trip_as_langchain_tool_calls():
    @tool
    def find_paper(query: str) -> str:
        """Find a paper."""
        return query

    def runner(_provider, prompt, schema, *_rest):
        assert "find_paper" in prompt
        assert "tool_calls" in schema["properties"]
        return CLIResponse({
            "content": "",
            "tool_calls": [{"name": "find_paper", "arguments": {"query": "topic"}}],
        })

    llm = SubscriptionChatModel("codex", runner=runner).bind_tools([find_paper])
    response = llm.invoke("Research the novelty")

    assert response.tool_calls[0]["name"] == "find_paper"
    assert response.tool_calls[0]["args"] == {"query": "topic"}
    assert response.tool_calls[0]["id"].startswith("call_")


def test_provider_factory_builds_all_subscription_models():
    for provider in SUBSCRIPTION_PROVIDERS:
        llm = make_chat_model({
            "provider": provider,
            "reasoning_model": "default",
            "models": {},
            "agent_models": {},
        })
        assert isinstance(llm, SubscriptionChatModel)
        assert spec_for_llm(llm).name == provider


def test_claude_command_uses_subscription_safe_mode(monkeypatch):
    seen = {}

    def fake_run(command, **kwargs):
        seen["command"] = command
        seen["prompt"] = kwargs["input"]
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps({
                "is_error": False,
                "structured_output": {"content": "ok"},
                "usage": {"input_tokens": 8, "output_tokens": 2},
            }),
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    response = _run_claude("prompt", {"type": "object"}, "sonnet", "high", 30)

    assert response.value == {"content": "ok"}
    assert "--safe-mode" in seen["command"]
    assert seen["command"][seen["command"].index("--tools") + 1] == ""
    assert seen["prompt"] == "prompt"


def test_codex_command_uses_read_only_ephemeral_execution(monkeypatch):
    seen = {}

    def fake_run(command, **kwargs):
        seen["command"] = command
        output_path = Path(command[command.index("--output-last-message") + 1])
        output_path.write_text('{"content":"ok"}', encoding="utf-8")
        event = {
            "type": "turn.completed",
            "usage": {"input_tokens": 9, "cached_input_tokens": 3, "output_tokens": 2},
        }
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(event) + "\n",
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    response = _run_codex("prompt", {"type": "object"}, "default", None, 30)

    assert response.value == {"content": "ok"}
    assert response.cached_input_tokens == 3
    assert "--ephemeral" in seen["command"]
    assert seen["command"][seen["command"].index("--sandbox") + 1] == "read-only"
    assert seen["command"][-1] == "-"


def test_droid_command_uses_read_only_headless_execution(monkeypatch):
    seen = {}

    def fake_run(command, **kwargs):
        seen["command"] = command
        seen["prompt"] = kwargs["input"]
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps({
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "result": '{"content":"ok"}',
            }),
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(
        "peerreviewagents.runtime.subscriptions._require_executable",
        lambda name: name,
    )
    response = _run_droid("prompt", {"type": "object"}, "default", "high", 30)

    assert response.value == {"content": "ok"}
    assert seen["command"][:2] == ["droid", "exec"]
    assert "--output-format" in seen["command"]
    assert "--disable-builtin-skills" in seen["command"]
    assert "JSON Schema" in seen["prompt"]


def test_pi_command_disables_tools_and_reads_final_message(monkeypatch):
    seen = {}

    def fake_run(command, **kwargs):
        seen["command"] = command
        seen["prompt"] = kwargs["input"]
        events = [
            {"type": "session", "id": "test"},
            {
                "type": "message_end",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": '{"content":"ok"}'}],
                    "stopReason": "stop",
                    "usage": {
                        "input": 11,
                        "output": 3,
                        "cacheRead": 2,
                        "cacheWrite": 1,
                    },
                },
            },
        ]
        return subprocess.CompletedProcess(
            command,
            0,
            stdout="\n".join(json.dumps(event) for event in events),
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(
        "peerreviewagents.runtime.subscriptions._require_executable",
        lambda name: name,
    )
    response = _run_pi("prompt", {"type": "object"}, "default", "high", 30)

    assert response.value == {"content": "ok"}
    assert response.input_tokens == 11
    assert response.cached_input_tokens == 2
    assert "--no-tools" in seen["command"]
    assert "--no-context-files" in seen["command"]
    assert "JSON Schema" in seen["prompt"]
