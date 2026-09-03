"""LangChain-compatible chat models backed by signed-in coding agent CLIs.

These adapters let the review graph use an existing Claude Code, Codex,
Factory Droid, or Pi login. They run each model turn in a fresh,
non-interactive process. The manuscript is already present in the prompt, so
the child process never needs workspace access.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Sequence

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import BaseModel

from ..observability import AgentEvent, emit


class SubscriptionCLIError(RuntimeError):
    """A signed-in coding agent CLI could not complete a model turn."""


@dataclass(frozen=True)
class CLIResponse:
    """Normalized response from a coding agent CLI."""

    value: dict[str, Any]
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    cache_write_input_tokens: int = 0


Runner = Callable[[str, str, dict[str, Any], str, str | None, float], CLIResponse]

SUBSCRIPTION_PROVIDERS = ("claude-code", "codex", "droid", "pi")
_EXECUTABLES = {
    "claude-code": "claude",
    "codex": "codex",
    "droid": "droid",
    "pi": "pi",
}


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content)
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text") or ""))
        else:
            parts.append(json.dumps(block, ensure_ascii=False, default=str))
    return "\n".join(part for part in parts if part)


def _serialize_messages(messages: Sequence[BaseMessage] | str) -> str:
    if isinstance(messages, str):
        messages = [HumanMessage(content=messages)]
    rendered: list[str] = [
        "Complete the conversation task below.",
        "Return only the JSON object required by the supplied output schema.",
        "Do not use coding tools, inspect the workspace, or change files.",
    ]
    for message in messages:
        role = getattr(message, "type", "message").upper()
        body = _content_text(getattr(message, "content", ""))
        tool_calls = getattr(message, "tool_calls", None) or []
        if tool_calls:
            body += "\nTool calls: " + json.dumps(tool_calls, ensure_ascii=False, default=str)
        tool_call_id = getattr(message, "tool_call_id", None)
        if tool_call_id:
            role = f"TOOL RESULT {tool_call_id}"
        rendered.append(f"<{role}>\n{body}\n</{role}>")
    return "\n\n".join(rendered)


def _schema_dict(schema: type[BaseModel] | dict[str, Any]) -> dict[str, Any]:
    if isinstance(schema, type) and issubclass(schema, BaseModel):
        return _strict_schema(schema.model_json_schema())
    if isinstance(schema, dict):
        if schema.get("type") == "function" and isinstance(schema.get("function"), dict):
            return _strict_schema(dict(schema["function"].get("parameters") or {}))
        return _strict_schema(dict(schema))
    raise TypeError(f"unsupported structured output schema: {schema!r}")


def _strict_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Normalize a JSON schema for Codex and Claude strict output modes."""
    normalized = json.loads(json.dumps(schema))

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("type") == "object" or "properties" in node:
                properties = node.get("properties") or {}
                node["additionalProperties"] = False
                node["required"] = list(properties)
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            for value in node:
                visit(value)

    visit(normalized)
    return normalized


def _plain_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {"content": {"type": "string"}},
        "required": ["content"],
        "additionalProperties": False,
    }


def _tool_schema(tools: tuple[dict[str, Any], ...]) -> dict[str, Any]:
    variants: list[dict[str, Any]] = []
    for tool in tools:
        fn = tool["function"]
        variants.append({
            "type": "object",
            "properties": {
                "name": {"type": "string", "const": fn["name"]},
                "arguments": _strict_schema(fn.get("parameters") or {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                }),
            },
            "required": ["name", "arguments"],
            "additionalProperties": False,
        })
    item_schema: dict[str, Any]
    if len(variants) == 1:
        item_schema = variants[0]
    else:
        item_schema = {"anyOf": variants}
    return {
        "type": "object",
        "properties": {
            "content": {"type": "string"},
            "tool_calls": {"type": "array", "items": item_schema},
        },
        "required": ["content", "tool_calls"],
        "additionalProperties": False,
    }


def _tool_instruction(tools: tuple[dict[str, Any], ...]) -> str:
    names = ", ".join(tool["function"]["name"] for tool in tools)
    return (
        "The local review runner can execute these research tools: "
        f"{names}. If a tool is needed, return an empty content string and "
        "one or more tool_calls. Otherwise return the complete final answer "
        "in content and an empty tool_calls array."
    )


def _schema_prompt(prompt: str, schema: dict[str, Any]) -> str:
    """Attach a schema for clients without a native schema flag."""
    return (
        f"{prompt}\n\n"
        "Return one JSON object and no surrounding commentary. The object must "
        "match this JSON Schema exactly:\n"
        f"{json.dumps(schema, ensure_ascii=False, separators=(',', ':'))}"
    )


def _parse_json_object(text: str, client: str) -> dict[str, Any]:
    """Parse a JSON object, tolerating a single fenced JSON response."""
    candidate = text.strip()
    if candidate.startswith("```") and candidate.endswith("```"):
        lines = candidate.splitlines()
        if len(lines) >= 3:
            candidate = "\n".join(lines[1:-1]).strip()
    try:
        value = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise SubscriptionCLIError(
            f"{client} did not return the requested JSON object"
        ) from exc
    if not isinstance(value, dict):
        raise SubscriptionCLIError(f"{client} did not return the requested JSON object")
    return value


@dataclass(frozen=True)
class SubscriptionChatModel:
    """Small chat-model facade that matches the graph's LangChain usage."""

    provider: str
    model: str = "default"
    reasoning_effort: str | None = None
    timeout_s: float = 120.0
    node: str | None = None
    run_id: str | None = None
    runner: Runner | None = None
    bound_tools: tuple[dict[str, Any], ...] = ()

    @property
    def _subscription_provider(self) -> str:
        return self.provider

    def invoke(self, messages: Sequence[BaseMessage] | str, **_kwargs: Any) -> AIMessage:
        schema = _tool_schema(self.bound_tools) if self.bound_tools else _plain_schema()
        prompt = _serialize_messages(messages)
        if self.bound_tools:
            prompt += "\n\n" + _tool_instruction(self.bound_tools)
        response = self._run(prompt, schema)
        if self.bound_tools:
            calls = [
                {
                    "name": call["name"],
                    "args": call.get("arguments") or {},
                    "id": f"call_{uuid.uuid4().hex}",
                    "type": "tool_call",
                }
                for call in response.value.get("tool_calls") or []
            ]
            content = str(response.value.get("content") or "")
        else:
            calls = []
            content = str(response.value.get("content") or "")
        return self._message(content, response, tool_calls=calls)

    def bind_tools(self, tools: Sequence[Any], **_kwargs: Any) -> SubscriptionChatModel:
        converted = tuple(convert_to_openai_tool(tool) for tool in tools)
        return replace(self, bound_tools=converted)

    def with_structured_output(
        self,
        schema: type[BaseModel] | dict[str, Any],
        *,
        include_raw: bool = False,
        **kwargs: Any,
    ) -> _StructuredSubscriptionCall:
        kwargs.pop("method", None)
        kwargs.pop("strict", None)
        if kwargs:
            raise ValueError(f"unsupported structured output arguments: {sorted(kwargs)}")
        return _StructuredSubscriptionCall(self, schema, include_raw)

    def _run(self, prompt: str, schema: dict[str, Any]) -> CLIResponse:
        runner = self.runner or run_subscription_cli
        return runner(
            self.provider,
            prompt,
            schema,
            self.model,
            self.reasoning_effort,
            self.timeout_s,
        )

    def _message(
        self,
        content: str,
        response: CLIResponse,
        *,
        tool_calls: list[dict[str, Any]] | None = None,
    ) -> AIMessage:
        usage = {
            "input_tokens": response.input_tokens,
            "output_tokens": response.output_tokens,
            "total_tokens": response.input_tokens + response.output_tokens,
        }
        if response.cached_input_tokens:
            usage["input_token_details"] = {"cache_read": response.cached_input_tokens}
        if response.cache_write_input_tokens:
            usage.setdefault("input_token_details", {})["cache_creation"] = (
                response.cache_write_input_tokens
            )
        emit(AgentEvent(
            kind="usage",
            node=self.node or "",
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            cache_read_tokens=response.cached_input_tokens,
            cache_write_tokens=response.cache_write_input_tokens,
            cost_usd=0.0,
            run_id=self.run_id or "",
        ))
        return AIMessage(
            content=content,
            tool_calls=tool_calls or [],
            usage_metadata=usage,
            response_metadata={
                "model_name": self.model,
                "provider": self.provider,
                "subscription_cli": True,
                "token_usage": {"cost": 0.0},
            },
        )


@dataclass(frozen=True)
class _StructuredSubscriptionCall:
    model: SubscriptionChatModel
    schema: type[BaseModel] | dict[str, Any]
    include_raw: bool

    def invoke(self, messages: Sequence[BaseMessage] | str, **_kwargs: Any) -> Any:
        response = self.model._run(_serialize_messages(messages), _schema_dict(self.schema))
        raw = self.model._message(
            json.dumps(response.value, ensure_ascii=False),
            response,
        )
        try:
            if isinstance(self.schema, type) and issubclass(self.schema, BaseModel):
                parsed: Any = self.schema.model_validate(response.value)
            else:
                parsed = response.value
            error: Exception | None = None
        except Exception as exc:  # noqa: BLE001
            parsed = None
            error = exc
        if self.include_raw:
            return {"raw": raw, "parsed": parsed, "parsing_error": error}
        if error is not None:
            raise error
        return parsed


def run_subscription_cli(
    provider: str,
    prompt: str,
    schema: dict[str, Any],
    model: str,
    effort: str | None,
    timeout_s: float,
) -> CLIResponse:
    """Run one isolated turn through a supported local coding agent."""
    if provider == "claude-code":
        return _run_claude(prompt, schema, model, effort, timeout_s)
    if provider == "codex":
        return _run_codex(prompt, schema, model, effort, timeout_s)
    if provider == "droid":
        return _run_droid(prompt, schema, model, effort, timeout_s)
    if provider == "pi":
        return _run_pi(prompt, schema, model, effort, timeout_s)
    raise ValueError(f"unsupported subscription provider: {provider}")


def validate_subscription_cli(provider: str) -> str:
    """Return the executable path or raise a focused installation error."""
    executable = _EXECUTABLES.get(provider)
    if executable is None:
        raise ValueError(f"unsupported subscription provider: {provider}")
    return _require_executable(executable)


def _require_executable(name: str) -> str:
    path = shutil.which(name)
    if path:
        return path
    raise SubscriptionCLIError(
        f"{name!r} was not found on PATH. Install it and sign in before using this provider."
    )


def _completed(command: list[str], prompt: str, timeout_s: float) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            command,
            input=prompt,
            text=True,
            capture_output=True,
            timeout=timeout_s,
            check=False,
            env=os.environ.copy(),
        )
    except subprocess.TimeoutExpired as exc:
        raise SubscriptionCLIError(
            f"{Path(command[0]).name} exceeded the {timeout_s:g} second request timeout"
        ) from exc
    if result.returncode != 0:
        streams = [part.strip() for part in (result.stderr, result.stdout) if part.strip()]
        detail = "\n".join(streams)[-5000:] or "no diagnostic output"
        raise SubscriptionCLIError(
            f"{Path(command[0]).name} exited with status {result.returncode}: {detail}"
        )
    return result


def _run_claude(
    prompt: str,
    schema: dict[str, Any],
    model: str,
    effort: str | None,
    timeout_s: float,
) -> CLIResponse:
    command = [
        _require_executable("claude"),
        "-p",
        "--output-format",
        "json",
        "--json-schema",
        json.dumps(schema, separators=(",", ":")),
        "--no-session-persistence",
        "--safe-mode",
        "--tools",
        "",
        "--permission-mode",
        "dontAsk",
    ]
    if model and model != "default":
        command.extend(["--model", model])
    if effort:
        command.extend(["--effort", effort])
    result = _completed(command, prompt, timeout_s)
    try:
        envelope = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise SubscriptionCLIError("claude returned invalid JSON output") from exc
    if envelope.get("is_error"):
        raise SubscriptionCLIError(str(envelope.get("result") or "claude reported an error"))
    value = envelope.get("structured_output")
    if not isinstance(value, dict):
        candidate = envelope.get("result")
        try:
            value = json.loads(candidate) if isinstance(candidate, str) else candidate
        except json.JSONDecodeError as exc:
            raise SubscriptionCLIError("claude did not return the requested JSON object") from exc
    if not isinstance(value, dict):
        raise SubscriptionCLIError("claude did not return the requested JSON object")
    usage = envelope.get("usage") or {}
    return CLIResponse(
        value=value,
        input_tokens=int(usage.get("input_tokens") or 0),
        output_tokens=int(usage.get("output_tokens") or 0),
        cached_input_tokens=int(usage.get("cache_read_input_tokens") or 0),
        cache_write_input_tokens=int(usage.get("cache_creation_input_tokens") or 0),
    )


def _run_codex(
    prompt: str,
    schema: dict[str, Any],
    model: str,
    effort: str | None,
    timeout_s: float,
) -> CLIResponse:
    with tempfile.TemporaryDirectory(prefix="peerreview-codex-") as temp:
        schema_path = Path(temp) / "schema.json"
        output_path = Path(temp) / "answer.json"
        schema_path.write_text(json.dumps(schema), encoding="utf-8")
        command = [
            _require_executable("codex"),
            "exec",
            "--ephemeral",
            "--sandbox",
            "read-only",
            "--ignore-user-config",
            "--ignore-rules",
            "--skip-git-repo-check",
            "--color",
            "never",
            "--json",
            "--output-schema",
            str(schema_path),
            "--output-last-message",
            str(output_path),
            "--cd",
            temp,
        ]
        if model and model != "default":
            command.extend(["--model", model])
        if effort:
            command.extend(["--config", f'model_reasoning_effort="{effort}"'])
        command.append("-")
        result = _completed(command, prompt, timeout_s)
        if not output_path.is_file():
            raise SubscriptionCLIError("codex did not write its final response")
        try:
            value = json.loads(output_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise SubscriptionCLIError("codex did not return the requested JSON object") from exc
        if not isinstance(value, dict):
            raise SubscriptionCLIError("codex did not return the requested JSON object")

        usage: dict[str, Any] = {}
        for line in result.stdout.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "turn.completed" and isinstance(event.get("usage"), dict):
                usage = event["usage"]
        return CLIResponse(
            value=value,
            input_tokens=int(usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
            cached_input_tokens=int(usage.get("cached_input_tokens") or 0),
            cache_write_input_tokens=int(usage.get("cache_write_input_tokens") or 0),
        )


def _run_droid(
    prompt: str,
    schema: dict[str, Any],
    model: str,
    effort: str | None,
    timeout_s: float,
) -> CLIResponse:
    """Run one read-only Factory Droid turn through ``droid exec``."""
    with tempfile.TemporaryDirectory(prefix="peerreview-droid-") as temp:
        command = [
            _require_executable("droid"),
            "exec",
            "--output-format",
            "json",
            "--cwd",
            temp,
            "--disable-builtin-skills",
        ]
        if model and model != "default":
            command.extend(["--model", model])
        if effort:
            command.extend(["--reasoning-effort", effort])
        result = _completed(command, _schema_prompt(prompt, schema), timeout_s)
    try:
        envelope = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise SubscriptionCLIError("droid returned invalid JSON output") from exc
    if envelope.get("is_error") or envelope.get("subtype") not in (None, "success"):
        raise SubscriptionCLIError(str(envelope.get("result") or "droid reported an error"))
    raw = envelope.get("structured_output")
    if isinstance(raw, dict):
        value = raw
    else:
        value = _parse_json_object(str(envelope.get("result") or ""), "droid")
    usage = envelope.get("usage") or {}
    return CLIResponse(
        value=value,
        input_tokens=int(usage.get("input_tokens") or usage.get("input") or 0),
        output_tokens=int(usage.get("output_tokens") or usage.get("output") or 0),
        cached_input_tokens=int(
            usage.get("cached_input_tokens") or usage.get("cacheRead") or 0
        ),
        cache_write_input_tokens=int(
            usage.get("cache_write_input_tokens") or usage.get("cacheWrite") or 0
        ),
    )


def _run_pi(
    prompt: str,
    schema: dict[str, Any],
    model: str,
    effort: str | None,
    timeout_s: float,
) -> CLIResponse:
    """Run one tool-free Pi turn and parse its final JSON event."""
    command = [
        _require_executable("pi"),
        "--mode",
        "json",
        "--print",
        "--no-session",
        "--no-tools",
        "--no-extensions",
        "--no-skills",
        "--no-context-files",
        "--no-approve",
    ]
    if model and model != "default":
        command.extend(["--model", model])
    if effort:
        command.extend(["--thinking", effort])
    result = _completed(command, _schema_prompt(prompt, schema), timeout_s)

    message: dict[str, Any] | None = None
    for line in result.stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        candidate = event.get("message")
        if (
            event.get("type") == "message_end"
            and isinstance(candidate, dict)
            and candidate.get("role") == "assistant"
        ):
            message = candidate
    if message is None:
        raise SubscriptionCLIError("pi did not emit a final assistant message")
    if message.get("stopReason") == "error":
        raise SubscriptionCLIError(str(message.get("errorMessage") or "pi reported an error"))
    value = _parse_json_object(_content_text(message.get("content") or []), "pi")
    usage = message.get("usage") or {}
    return CLIResponse(
        value=value,
        input_tokens=int(usage.get("input") or 0),
        output_tokens=int(usage.get("output") or 0),
        cached_input_tokens=int(usage.get("cacheRead") or 0),
        cache_write_input_tokens=int(usage.get("cacheWrite") or 0),
    )
