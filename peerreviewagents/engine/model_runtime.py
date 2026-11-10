"""Subscription-only model transport with bounded calls and durable receipts."""
from __future__ import annotations

import base64

import hashlib

import json

import os

import re

import shutil

import subprocess

import sys

import threading

import time

from pathlib import Path
from .models import PROVIDER_MODELS

class StudyBlocked(RuntimeError):
    pass

APPROVED_MANUSCRIPT_TOOLS = (
    "read_blocks", "search", "calculate", "save_note", "view_page",
    "view_supplement", "check_citation", "search_literature",
)

def digest(value):
    data = value if isinstance(value, bytes) else json.dumps(value, sort_keys=True).encode()
    return hashlib.sha256(data).hexdigest()

def read_json(path):
    return json.loads(Path(path).read_text())

def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(f".tmp-{os.getpid()}")
    temp.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")
    temp.replace(path)

def claim_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x") as stream:
        stream.write(json.dumps(value, indent=2, ensure_ascii=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())

def jsonl(path):
    path = Path(path)
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []

def append_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as stream:
        stream.write(json.dumps(value, ensure_ascii=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())

def clean_env():
    env = dict(os.environ)
    for key in list(env):
        if key.startswith(("ANTHROPIC_", "PEERREVIEW_", "OPENAI_")) or key in {
            "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
            "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT",
            "CODEX_API_KEY", "CODEX_ACCESS_TOKEN",
        }:
            del env[key]
    env["CLAUDE_CODE_DISABLE_FAST_MODE"] = "1"
    # Claude Code loads no instruction files, memory or attachments and never substitutes a model.
    env.update(CLAUDE_CODE_DISABLE_CLAUDE_MDS="1", CLAUDE_CODE_DISABLE_AUTO_MEMORY="1",
               CLAUDE_CODE_DISABLE_ATTACHMENTS="1", CLAUDE_CODE_NO_MODEL_FALLBACK="1")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env

def auth_status(provider="claude-code"):
    if provider == "codex":
        result = subprocess.run([shutil.which("codex") or "codex", "login", "status"],
                                capture_output=True, text=True, env=clean_env(), timeout=30)
        logged_in = result.returncode == 0 and "Logged in using ChatGPT" in result.stdout + result.stderr
        return {"loggedIn": logged_in, "authMethod": "chatgpt" if logged_in else "unverified"}
    executable = shutil.which("claude")
    if not executable:
        return {"loggedIn": False, "error": "Claude Code CLI is missing"}
    result = subprocess.run([executable, "auth", "status"], capture_output=True,
                            text=True, env=clean_env(), timeout=30)
    try:
        status = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {"loggedIn": False, "error": "Cannot read Claude authentication status"}
    return {key: status.get(key) for key in ("loggedIn", "authMethod", "apiProvider", "subscriptionType")}

def require_subscription(root):
    protocol_path = Path(root) / "protocol.json"
    provider = read_json(protocol_path)["provider"] if protocol_path.exists() else "claude-code"
    status = auth_status(provider)
    if not status.get("loggedIn"):
        raise StudyBlocked(f"This process cannot access a {provider} subscription login.")
    accepted = {"chatgpt"} if provider == "codex" else {"claude.ai", "oauth"}
    if status.get("authMethod") not in accepted:
        raise StudyBlocked("Subscription authentication was not verified. Refusing API-backed execution.")
    billing_path = Path(root) / "billing_confirmation.json"
    from .billing import verify_billing
    if provider == "codex":
        if not billing_path.exists():
            raise StudyBlocked("Record current Codex subscription usage before execution.")
        try:
            verify_billing(read_json(billing_path))
        except ValueError as error:
            raise StudyBlocked(str(error)) from error
        return status
    if status.get("apiProvider") not in (None, "firstParty"):
        raise StudyBlocked("Claude Code must use the Anthropic subscription login, not a cloud provider.")
    if not billing_path.exists():
        raise StudyBlocked("Confirm paid extra usage is disabled in Claude settings, then record it with confirm-billing.")
    try:
        verify_billing(read_json(billing_path), provider="claude-code")
    except ValueError as error:
        raise StudyBlocked(str(error)) from error
    return status

# The approved efforts and the one-retry ceiling are owned by review_policy, so
# the transport and the named policies can never disagree about them.
from .review_policy import APPROVED_EFFORTS, MAX_ATTEMPT_CEILING  # noqa: E402

def validate_protocol(protocol):
    pinned = PROVIDER_MODELS
    if protocol.get("provider") not in pinned or protocol.get("model") not in pinned[protocol["provider"]]:
        raise StudyBlocked("Unapproved provider or model. No automatic fallback is permitted.")
    if protocol.get("held_out_runs_authorized"):
        raise StudyBlocked("This runner is limited to one development paper.")
    if protocol.get("effort") not in APPROVED_EFFORTS:
        raise StudyBlocked("Unapproved reasoning effort. Approved values are low, medium and high.")
    for key in ("max_calls_per_condition", "observed_token_stop", "call_timeout_seconds"):
        value = protocol.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise StudyBlocked(f"Protocol field {key} must be a positive integer.")
    attempts = protocol.get("max_attempts", 1)
    if isinstance(attempts, bool) or not isinstance(attempts, int) or not 1 <= attempts <= MAX_ATTEMPT_CEILING:
        raise StudyBlocked("Protocol field max_attempts allows one call or one bounded retry.")

def source_manifest(path):
    return {str(p.relative_to(path)): digest(p.read_bytes()) for p in sorted(Path(path).rglob("*"))
            if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"}

def usage_tokens(envelope):
    usage = envelope.get("usage") or {}
    keys = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
    if not any(key in usage for key in keys):
        return None
    return sum(int(usage.get(k) or 0) for k in keys)

def usage_row(record):
    return {k: v for k, v in record.items()
            if k not in {"value", "envelope", "stdout", "stderr", "events", "command"}}

def attempt_key(record):
    """Identity of one attempt. A retry of the same request is a separate row."""
    identity = record.get("request_sha256")
    return f"{identity}#{record.get('attempt') or 1}" if identity else None

def attempt_stem(identity, attempt):
    """File stem for an attempt. Attempt one keeps the plain identity for compatibility."""
    return identity if attempt <= 1 else f"{identity}.attempt{attempt}"

def account_once(path, record):
    key = attempt_key(record)
    if key and any(attempt_key(row) == key for row in jsonl(path)):
        return
    append_json(path, usage_row(record))

def reconcile_receipts(out):
    calls = Path(out) / "calls"
    if not calls.exists():
        return
    for receipt in sorted(calls.glob("*.json")):
        if not re.fullmatch(r"[0-9a-f]{64}(\.attempt[0-9]+)?\.json", receipt.name):
            continue
        account_once(Path(out) / "usage.jsonl", read_json(receipt))

def tool_identity(work, tools):
    if not tools:
        return None
    config_path = Path(work) / "tool-config.json"
    if not config_path.exists():
        raise StudyBlocked("Bounded manuscript tools require tool-config.json before inference.")
    config = read_json(config_path)
    source = Path(config.get("paper", "")) / "manifest.json"
    return {
        "approved_tools": tool_profile(config)[1],
        "config": config,
        "source_manifest_sha256": digest(source.read_bytes()) if source.is_file() else None,
    }

def tool_profile(config):
    profile = config.get('tool_profile', 'legacy')
    preflight = ('validate_synthesis',) if config.get('synthesis_ledger') else ()
    if profile == 'legacy':
        return 'manuscript_tools.py', (*APPROVED_MANUSCRIPT_TOOLS, *preflight)
    if profile == 'domains-v1':
        return 'manuscript_tools.py', (*APPROVED_MANUSCRIPT_TOOLS, 'lookup_source', 'inspect_results', 'record_comparison', 'resolve_identifier', 'historical_registration', *preflight)
    if profile == 'debate-v2':
        from .debate_evidence import TOOLS
        return 'debate_tools.py', TOOLS
    raise StudyBlocked('Unknown manuscript tool profile')

def request_identity(protocol, prompt, schema, tools, images, work):
    request = {
        "prompt": prompt,
        "schema": schema,
        "tools": tool_identity(work, tools),
        "protocol": protocol,
        "images": [{"path": str(path), "sha256": digest(path.read_bytes())} for path in images],
    }
    if protocol.get("provider") == "claude-code":
        # Claude Code runs replace the default system prompt, so the replacement is part of the request.
        request["system_prompt"] = CLAUDE_SYSTEM_PROMPT
    return digest(request)

def observed_codex_tokens(events):
    completed = [event for event in events if event.get("type") == "turn.completed"]
    keys = ("input_tokens", "output_tokens")
    if not completed or not any(key in event.get("usage", {}) for event in completed for key in keys):
        return None
    return sum(int(event.get("usage", {}).get(key) or 0)
               for event in completed for key in keys)

def terminal_codex_failures(events):
    """Return terminal failures, retaining non-reconnect errors fail closed."""
    completed_positions = [index for index, event in enumerate(events)
                           if event.get("type") == "turn.completed"]
    last_completion = completed_positions[-1] if completed_positions else -1
    return [event for index, event in enumerate(events)
            if event.get("type") == "turn.failed"
            or (event.get("type") == "error" and (
                index > last_completion
                or not str(event.get("message", "")).startswith("Reconnecting... ")
            ))]

def recover_codex_call(out, identity, started):
    calls = Path(out) / "calls"
    events_path = calls / f"{identity}.events.jsonl"
    answer_path = calls / f"{identity}.answer.json"
    if not events_path.exists() or not answer_path.exists():
        return None
    events = []
    for line in events_path.read_text().splitlines():
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    completed = [event for event in events if event.get("type") == "turn.completed"]
    failures = terminal_codex_failures(events)
    tool_calls = [event.get("item", {}) for event in events
                  if event.get("type") == "item.completed"
                  and event.get("item", {}).get("type") == "mcp_tool_call"]
    if not completed or failures or any(item.get("status") != "completed" for item in tool_calls):
        return None
    try:
        value = read_json(answer_path)
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(value, dict):
        return None
    record = dict(started)
    record.update(ok=True, value=value, recovered_after_interruption=True,
                  recovered_at=time.time(), events=events)
    tokens = observed_codex_tokens(events)
    record["usage_unknown"] = tokens is None
    if tokens is not None:
        record["observed_tokens"] = tokens
    return record

def unresolved_attempts(out):
    calls = Path(out) / "calls"
    if not calls.exists():
        return []
    unresolved = []
    for started in calls.glob("*.started.json"):
        identity = started.name.removesuffix(".started.json")
        if not (calls / f"{identity}.json").exists():
            unresolved.append(started)
    return unresolved

CLAUDE_SYSTEM_PROMPT = ("You are a careful scientific manuscript reviewer in a bounded review pipeline. "
                        "Follow the instructions in the user message exactly. "
                        "Return the final answer through the structured output tool.")
CLAUDE_TOOL_PREFIX = "mcp__manuscript__"
CLAUDE_IMAGE_TYPES = ((b"\x89PNG\r\n\x1a\n", "image/png"), (b"\xff\xd8\xff", "image/jpeg"),
                      (b"GIF87a", "image/gif"), (b"GIF89a", "image/gif"))
CLAUDE_IMAGE_LIMIT = 5 * 1024 * 1024
CLAUDE_SCHEMA_LIMIT = 120000
JS_SYNTAX_CHARACTERS = set("^$\\.*+?()[]{}|/")

def unicode_pattern(pattern):
    """Drop escapes that Python regexes allow but JavaScript Unicode regexes reject, keeping the meaning.

    re.escape writes identity escapes such as \\- or \\# that Codex accepts. Claude Code validates
    patterns with the JavaScript u flag, which allows escaping only syntax characters, and a
    hyphen inside a character class.
    """
    result = []
    in_class = False
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if char == "\\" and index + 1 < len(pattern):
            following = pattern[index + 1]
            keep = (following.isascii() and following.isalnum()) or following in JS_SYNTAX_CHARACTERS
            result.append(char + following if keep or (in_class and following == "-") else following)
            index += 2
            continue
        if char == "[" and not in_class:
            in_class = True
        elif char == "]" and in_class:
            in_class = False
        result.append(char)
        index += 1
    return "".join(result)

def claude_schema(value):
    """A copy of the response schema whose regular expressions Claude Code can compile."""
    if isinstance(value, list):
        return [claude_schema(item) for item in value]
    if not isinstance(value, dict):
        return value
    converted = {key: claude_schema(item) for key, item in value.items()}
    if isinstance(value.get("pattern"), str):
        converted["pattern"] = unicode_pattern(value["pattern"])
    if isinstance(value.get("patternProperties"), dict):
        converted["patternProperties"] = {unicode_pattern(key): claude_schema(item)
                                          for key, item in value["patternProperties"].items()}
    return converted

def claude_command(protocol, schema, tools, work):
    """Print mode with no built-in tools, settings files, skills or other MCP servers."""
    schema = claude_schema(schema)
    if len(json.dumps(schema)) > CLAUDE_SCHEMA_LIMIT:
        raise StudyBlocked("The response schema is too large for the Claude Code command line.")
    command = [shutil.which("claude") or "claude", "-p", "--model", protocol["model"],
               "--effort", protocol["effort"], "--input-format", "stream-json",
               "--output-format", "stream-json", "--verbose",
               "--json-schema", json.dumps(schema), "--system-prompt", CLAUDE_SYSTEM_PROMPT,
               "--no-session-persistence", "--restricted", "--strict-mcp-config",
               "--disable-slash-commands", "--permission-mode", "dontAsk", "--tools", ""]
    if tools:
        entry_point, approved_tools = tool_profile(read_json(work / "tool-config.json"))
        server = {"type": "stdio", "command": sys.executable,
                  "args": [str(HERE / entry_point), str(work / "tool-config.json")]}
        command.extend(["--mcp-config", json.dumps({"mcpServers": {"manuscript": server}}),
                        "--allowedTools", ",".join(CLAUDE_TOOL_PREFIX + name for name in approved_tools)])
    return command

def claude_input(prompt, images):
    """One stream-json user message with the prompt, then the images in their stated order."""
    content = [{"type": "text", "text": prompt}]
    for path in images:
        data = path.read_bytes()
        media = next((kind for magic, kind in CLAUDE_IMAGE_TYPES if data.startswith(magic)), None)
        if media is None and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            media = "image/webp"
        if media is None:
            raise StudyBlocked(f"Claude Code accepts PNG, JPEG, GIF or WebP source images. Unsupported: {path.name}")
        encoded = base64.b64encode(data).decode()
        if len(encoded) > CLAUDE_IMAGE_LIMIT:
            raise StudyBlocked(f"Source image exceeds the 5 MB Claude image limit: {path.name}")
        content.append({"type": "image", "source": {"type": "base64", "media_type": media, "data": encoded}})
    return json.dumps({"type": "user", "message": {"role": "user", "content": content}}) + "\n"

def claude_stream_problem(event, tools):
    """Name an event that shows API-key billing, paid extra usage or tools outside the manuscript server."""
    if event.get("type") == "system" and event.get("subtype") == "init":
        if event.get("apiKeySource") != "none":
            return "Claude Code did not use the subscription login. Refusing API-backed execution."
        if any(name != "StructuredOutput" and not (tools and name.startswith(CLAUDE_TOOL_PREFIX))
               for name in event.get("tools") or []):
            return "Claude Code exposed tools outside the approved manuscript server."
        if tools and not any(server.get("name") == "manuscript" and server.get("status") == "connected"
                             for server in event.get("mcp_servers") or []):
            return "The manuscript tool server did not connect."
    if event.get("type") == "rate_limit_event":
        info = event.get("rate_limit_info") or {}
        if info.get("isUsingOverage") or info.get("overageStatus") in ("allowed", "allowed_warning"):
            return "Claude reports that paid extra usage is enabled or in use. Disable it before running reviews."
    return None

def claude_tool_failures(events):
    names = {block.get("id"): block.get("name") for event in events if event.get("type") == "assistant"
             for block in (event.get("message") or {}).get("content") or []
             if isinstance(block, dict) and block.get("type") == "tool_use"}
    return [block for event in events if event.get("type") == "user"
            for block in (event.get("message") or {}).get("content") or []
            if isinstance(block, dict) and block.get("type") == "tool_result" and block.get("is_error")
            and str(names.get(block.get("tool_use_id"))).startswith(CLAUDE_TOOL_PREFIX)]

def same_model(name, requested):
    return isinstance(name, str) and (name == requested or name.startswith((requested + "-", requested + "[")))

def claude_failure(events, init, result, limits, model, tools, returncode):
    problems = [problem for problem in (claude_stream_problem(event, tools) for event in events) if problem]
    if problems:
        return problems[0]
    if init is None:
        return "Claude Code reported no session configuration. Inspect the saved attempt."
    if returncode or not result or result.get("is_error") or result.get("subtype") != "success":
        detail = result.get("result") or result.get("subtype") or f"exit status {returncode}"
        return f"Claude Code failed. No automatic retry. {detail}"
    if not limits:
        return "Claude Code reported no subscription usage limits. Refusing unverified execution."
    if claude_tool_failures(events) or result.get("permission_denials"):
        return "A manuscript tool call failed. Do not score this incomplete run."
    models = result.get("modelUsage") or {}
    if (not same_model(init.get("model"), model) or not models
            or any(not same_model(name, model) and not same_model(usage.get("canonicalModel"), model)
                   for name, usage in models.items())):
        return f"Unexpected model returned: {sorted(models) or init.get('model')}. No fallback is permitted."
    if not isinstance(result.get("structured_output"), dict):
        return "Claude returned no structured object."
    if usage_tokens(result) is None:
        return "Claude completed but did not report usage. Do not continue this run."
    return None

def assess_claude_stream(events, model, tools, returncode=None):
    """Verify one Claude Code event stream. Observed usage is kept even when the call failed."""
    init = next((event for event in events
                 if event.get("type") == "system" and event.get("subtype") == "init"), None)
    results = [event for event in events if event.get("type") == "result"]
    result = results[-1] if results else {}
    limits = [event.get("rate_limit_info") or {} for event in events if event.get("type") == "rate_limit_event"]
    tokens = usage_tokens(result)
    outcome = {"claude_code_version": (init or {}).get("claude_code_version"),
               "subscription_limits": limits,
               "models_observed": sorted((result.get("modelUsage") or {}).keys()),
               "model_verification": "Claude Code session model and per-model usage report.",
               "usage_unknown": tokens is None,
               "error": claude_failure(events, init, result, limits, model, tools, returncode)}
    if tokens is not None:
        outcome["observed_tokens"] = tokens
    if outcome["error"] is None:
        outcome["value"] = result["structured_output"]
    return outcome

def read_events(path):
    events = []
    for line in Path(path).read_text().splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events

def watch_claude_stream(path, process, finished, tools):
    """Stop a running call as soon as its stream shows API-key billing or paid usage."""
    pending = ""
    with Path(path).open() as stream:
        while True:
            pending += stream.readline()
            if pending.endswith("\n"):
                line, pending = pending, ""
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict) and claude_stream_problem(event, tools):
                    process.kill()
                    return
            elif finished.is_set():
                return
            else:
                time.sleep(0.05)

def run_claude(command, payload, stdout_path, stderr_path, cwd, timeout, tools):
    """Run one call. The stream goes straight to disk so a completed answer survives the launcher."""
    with Path(stdout_path).open("w") as stdout, Path(stderr_path).open("w") as stderr:
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=stdout, stderr=stderr,
                                   text=True, cwd=cwd, env=clean_env())
    finished = threading.Event()
    watcher = threading.Thread(target=watch_claude_stream, args=(stdout_path, process, finished, tools),
                               daemon=True)
    watcher.start()
    try:
        process.communicate(payload, timeout=timeout)
    except BaseException:
        process.kill()
        process.wait()
        raise
    finally:
        finished.set()
        watcher.join()
    return process.returncode

def recover_claude_call(out, identity, started):
    events_path = Path(out) / "calls" / f"{identity}.events.jsonl"
    if not events_path.exists():
        return None
    events = read_events(events_path)
    outcome = assess_claude_stream(events, started.get("model_requested"), started.get("tools"))
    if outcome.pop("error"):
        return None
    record = dict(started)
    record.update(outcome, ok=True, recovered_after_interruption=True, recovered_at=time.time(), events=events)
    return record

def model_call(protocol, out, prompt, schema, *, tools=False, label="call", images=None, recover_only=False):
    """One request, with at most ``protocol['max_attempts']`` attempts at it.

    A retry happens only when an attempt was actually launched and failed or
    timed out. Every guard refusal, including the call and token stops, is
    returned to the caller unchanged and never retried. The failed attempt
    keeps its own receipt and its incurred usage, so nothing is hidden.
    """
    limit = 1 if recover_only else protocol.get("max_attempts", 1)
    while True:
        try:
            return _model_call(protocol, out, prompt, schema, tools=tools, label=label,
                               images=images, recover_only=recover_only)
        except Exception as error:
            launched = getattr(error, "attempt_launched", None)
            if launched is None or launched >= limit:
                raise

def _model_call(protocol, out, prompt, schema, *, tools=False, label="call", images=None, recover_only=False):
    """A single fresh session, with a durable content-addressed response cache."""
    validate_protocol(protocol)
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    images = [Path(p).resolve() for p in (images or [])]
    image_limit=protocol.get('max_source_images',3)
    image_ceiling=10 if protocol.get('image_budget_version')=='evidence-v1' else 4
    if type(image_limit) is not int or not 1<=image_limit<=image_ceiling or len(images)>image_limit:
        raise StudyBlocked("Attached source images exceed the bounded protocol allowance")
    work = out / "workspace"
    work.mkdir(exist_ok=True)
    identity = request_identity(protocol, prompt, schema, tools, images, work)
    # One request may own up to max_attempts attempt stems. Attempt one keeps the
    # bare identity, so an existing run directory is untouched when retries are off.
    attempt = 1
    limit = protocol.get("max_attempts", 1)
    while True:
        stem = attempt_stem(identity, attempt)
        cache = out / "calls" / f"{stem}.json"
        if not cache.exists():
            break
        saved = read_json(cache)
        account_once(out / "usage.jsonl", saved)
        unknown = saved.get("usage_unknown") or saved.get("observed_tokens") is None
        if saved.get("ok") and not unknown:
            return saved["value"]
        if not saved.get("ok") and attempt < limit:
            # A failed or timed-out attempt is retried once, in its own stem, so the
            # failed receipt and its incurred usage both stay on disk.
            attempt += 1
            continue
        if unknown:
            raise StudyBlocked("Cached output has unknown usage. Inspect its receipt before continuing.")
        raise StudyBlocked("A prior attempt failed. Inspect it and use a new attempt directory to retry.")
    started_path = out / "calls" / f"{stem}.started.json"
    if started_path.exists():
        started = read_json(started_path)
        recover = recover_codex_call if protocol["provider"] == "codex" else recover_claude_call
        recovered = recover(out, stem, started)
        if recovered:
            write_json(cache, recovered)
            account_once(out / "usage.jsonl", recovered)
            if recovered.get("usage_unknown"):
                raise StudyBlocked("Recovered output has unknown usage. Inspect its receipt before continuing.")
            return recovered["value"]
        raise StudyBlocked("An interrupted attempt has no complete recoverable receipt. Inspect it before any new model call.")
    if recover_only:
        raise StudyBlocked("No matching completed request is recoverable. Recovery never launches a model call.")
    reconcile_receipts(out)
    if unresolved_attempts(out):
        raise StudyBlocked("An earlier interrupted attempt is unresolved. Inspect it before any new model call.")
    prior = jsonl(out / "usage.jsonl")
    # An attempt this retry replaces is superseded: its failure is recorded, its
    # tokens still count, but it no longer blocks the request that replaces it.
    superseded = [index for index, row in enumerate(prior)
                  if row.get("request_sha256") == identity and (row.get("attempt") or 1) < attempt]
    effective = [row for index, row in enumerate(prior) if index not in set(superseded)]
    if any(row.get("usage_unknown") or row.get("observed_tokens") is None for row in effective):
        raise StudyBlocked("Prior model usage is unknown. No further model calls are permitted in this attempt directory.")
    if len(effective) >= protocol["max_calls_per_condition"]:
        raise StudyBlocked("Pilot call limit reached. Outputs have been saved.")
    if sum(row.get("observed_tokens", 0) for row in prior) >= protocol["observed_token_stop"]:
        raise StudyBlocked("Pilot observed-token stop reached. Outputs have been saved.")
    if protocol["provider"] == "claude-code":
        command = claude_command(protocol, schema, tools, work)
        payload = claude_input(prompt, images)
    record = {"label": label, "request_sha256": identity, "attempt": attempt, "started_at": time.time(),
              "model_requested": protocol["model"], "tools": tools, "ok": False,
              "usage_unknown": True}
    write_json(out / "calls" / f"{stem}.request.json",
               {"prompt": prompt, "schema": schema, "identity": identity, "attempt": attempt})
    if protocol["provider"] == "codex":
        schema_path = out / "calls" / f"{stem}.schema.json"
        answer_path = out / "calls" / f"{stem}.answer.json"
        write_json(schema_path, schema)
        command = [shutil.which("codex") or "codex", "exec", "--ephemeral",
                   "--sandbox", "read-only", "--ignore-user-config", "--ignore-rules",
                   "--skip-git-repo-check", "--color", "never", "--json",
                   "--output-schema", str(schema_path), "--output-last-message", str(answer_path),
                   "--cd", str(work), "--model", protocol["model"],
                   "-c", f'model_reasoning_effort="{protocol["effort"]}"',
                   "-c", 'forced_login_method="chatgpt"', "-c", 'web_search="disabled"']
        for feature in ("shell_tool", "apps", "plugins", "browser_use", "computer_use", "multi_agent"):
            command.extend(["-c", f"features.{feature}=false"])
        if tools:
            entry_point, approved_tools = tool_profile(read_json(work / "tool-config.json"))
            command.extend(["-c", f"mcp_servers.manuscript.command={json.dumps(sys.executable)}",
                            "-c", "mcp_servers.manuscript.args=" + json.dumps([
                                str(HERE / entry_point), str(work / "tool-config.json")]),
                            "-c", "mcp_servers.manuscript.required=true"])
            for tool_name in approved_tools:
                command.extend(["-c", f'mcp_servers.manuscript.tools.{tool_name}.approval_mode="approve"'])
        for path in images:
            command.extend(["--image", str(path)])
        command.append("-")
    record["command"] = command
    record["provider"] = protocol["provider"]
    try:
        claim_json(started_path, record)
    except FileExistsError as exc:
        raise StudyBlocked("This request was started by another launcher. Inspect it before continuing.") from exc
    try:
        stdout_path = out / "calls" / f"{stem}.events.jsonl"
        stderr_path = out / "calls" / f"{stem}.stderr.txt"
        if protocol["provider"] == "claude-code":
            record["returncode"] = run_claude(command, payload, stdout_path, stderr_path, work,
                                              protocol["call_timeout_seconds"], tools)
            record["stderr"] = stderr_path.read_text()
            record["events"] = read_events(stdout_path)
            outcome = assess_claude_stream(record["events"], protocol["model"], tools, record["returncode"])
            error = outcome.pop("error")
            record.update(outcome)
            if error:
                raise StudyBlocked(error)
            record["ok"] = True
            return record["value"]
        with stdout_path.open("w") as stdout, stderr_path.open("w") as stderr:
            result = subprocess.run(command, input=prompt, stdout=stdout, stderr=stderr, text=True,
                                    cwd=work, env=clean_env(), timeout=protocol["call_timeout_seconds"])
        result.stdout = stdout_path.read_text()
        result.stderr = stderr_path.read_text()
        record["returncode"] = result.returncode
        record["stderr"] = result.stderr
        if protocol["provider"] == "codex":
            events = []
            record["stdout"] = result.stdout
            for line in result.stdout.splitlines():
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
            record["events"] = events
            completed = [event for event in events if event.get("type") == "turn.completed"]
            tokens = observed_codex_tokens(events)
            if tokens is not None:
                record["observed_tokens"] = tokens
                record["usage_unknown"] = False
            failures = terminal_codex_failures(events)
            if result.returncode or not completed or failures:
                raise StudyBlocked(f"Codex failed. No automatic retry. {failures[-1:]}")
            tool_calls = [e.get("item", {}) for e in events if e.get("type") == "item.completed"
                          and e.get("item", {}).get("type") == "mcp_tool_call"]
            if any(item.get("status") != "completed" for item in tool_calls):
                raise StudyBlocked("A manuscript tool call failed. Do not score this incomplete run.")
            # Complete manuscript text is in every generation prompt. Tools are
            # optional targeted rereads, PDF page inspection, and calculations.
            # This CLI does not expose the resolved model in JSON events.
            record["models_observed"] = []
            record["model_verification"] = "Explicit CLI model selection. Resolved model unavailable in event stream."
            value = read_json(answer_path)
            if not isinstance(value, dict):
                raise StudyBlocked("Codex returned no structured object")
            if record["usage_unknown"]:
                raise StudyBlocked("Codex completed but did not report usage. Do not continue this run.")
            record.update(ok=True, value=value)
            return value
    except subprocess.TimeoutExpired as exc:
        record["error"] = "Call timed out. Incurred usage is unknown."
        blocked = StudyBlocked(record["error"])
        # The attempt number travels on the exception so the caller can tell a
        # launched failure, which one bounded retry may replace, from a guard
        # refusal raised before anything was spent.
        blocked.attempt_launched = attempt
        raise blocked from exc
    except Exception as exc:
        record["error"] = str(exc)
        exc.attempt_launched = attempt
        raise
    finally:
        record["elapsed_seconds"] = time.time() - record["started_at"]
        write_json(cache, record)
        account_once(out / "usage.jsonl", record)
    return record["value"]

HERE = Path(__file__).resolve().parent
CODEX_MODEL = "gpt-5.6-sol"
