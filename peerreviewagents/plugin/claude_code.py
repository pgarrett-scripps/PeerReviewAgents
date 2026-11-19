"""Running one agent as a headless Claude Code session.

One agent, one process, one turn of the wheel: the session is started with
``claude -p``, given this run's tool server and nothing else, and read to its
end. It is not a panel and it does not spawn anything — the panel is the
orchestrator's, in Python, and this file knows about exactly one agent at a
time.

What it takes to confine an agent is four flags that do four different things,
and the step-A spike paid for finding that out:

* ``--agents`` with a definition that lists ``tools`` decides what the agent
  can **see**. An agent whose definition lists no tools sees all 47 the server
  offers.
* ``--allowed-tools`` decides what it is allowed to **call**. Without it every
  call was denied under ``dontAsk`` while the tools stayed visible.
* ``--tools ''`` takes away the built-ins, so there is no file, shell, edit or
  web tool in the session at all. A reviewer that could read files could read
  the run's own folder, another reviewer's notes, or a benchmark answer key.
* ``StructuredOutput`` has to be in both lists, because it is how a
  ``--json-schema`` answer is given and a definition that lists tools hides
  every tool it does not list.

What it takes to keep the owner's machine out of the review is the environment
and the settings:

* ``--setting-sources ''`` so no user, project or local settings file is read,
  and ``omitClaudeMd`` plus ``CLAUDE_CODE_DISABLE_MEMORY`` so no ``CLAUDE.md``
  is either. A reviewer that read the owner's instructions is not reviewing the
  manuscript on its own terms, and the first fix check found an agent doing
  exactly that.
* a **built** environment rather than an inherited one (see
  :func:`environment`). The engine used to pass its own ``CLAUDECODE`` and
  ``CLAUDE_CODE_*`` through to the child, which made a session believe it was
  already inside one; a tester had to reach for ``env -i`` to get a clean run.
  Nothing is inherited here that was not named.
* no API key, ever. Both key variables are left out of the built environment,
  so the session can only be the subscription login this machine is signed in
  to, and ``--bare`` — which skips plugin sync and reads credentials *only*
  from a key — is never passed.

The token ceiling is enforced from the stream as it arrives: the orchestrator
says how many tokens this agent may spend, the usage on each assistant message
is added up, and the process is stopped the moment the total passes it. That is
the only way an agent can be stopped mid-thought, and it is the orchestrator's
number, not this file's.
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from . import models as model_plan
from .runner import ANSWERED, FAILED, OVER_BUDGET, SILENT, TIMED_OUT, Outcome, Runner
from .service import TOOL_PREFIX

BINARY = 'claude'
#: The tool that answers a ``--json-schema`` prompt. Not one of ours, and the
#: one built-in every agent needs.
ANSWER_TOOL = 'StructuredOutput'
#: Environment variables a session is allowed to inherit, and nothing else.
#: ``HOME`` is here because the subscription login lives under it; the proxy
#: and certificate variables because a machine behind either cannot reach the
#: API without them. Deliberately absent: ``CLAUDECODE`` and ``CLAUDE_CODE_*``,
#: which belong to whatever session started us, and the two API key variables.
INHERITED = ('HOME', 'PATH', 'LANG', 'LC_ALL', 'LC_CTYPE', 'TZ', 'TMPDIR', 'SHELL', 'USER',
             'LOGNAME', 'XDG_RUNTIME_DIR', 'XDG_CACHE_HOME', 'SSL_CERT_FILE', 'SSL_CERT_DIR',
             'REQUESTS_CA_BUNDLE', 'NODE_EXTRA_CA_CERTS', 'HTTP_PROXY', 'HTTPS_PROXY',
             'NO_PROXY', 'http_proxy', 'https_proxy', 'no_proxy')
#: Variables every session gets, whatever the parent's environment says.
IMPOSED = {
    # No CLAUDE.md from this machine, this repo or the owner's home.
    'CLAUDE_CODE_DISABLE_MEMORY': '1',
    # The server reads this to know it is serving a review rather than being
    # poked at by hand.
    'PEERREVIEW_PLUGIN_RUN': '1',
    # Claude Code asks the terminal about itself; a session with no terminal
    # should not be guessing at one.
    'TERM': 'dumb',
    'CI': '1',
}
MISSING_CLAUDE = ('claude is not installed or not on this PATH, so no agent can be run: '
                  'install Claude Code and check that `claude --version` runs in this shell')
#: How long a stopped process is given to go away before it is killed.
#: A rate-limit window this full stops the run rather than starting another
#: agent into it: past this the next agent is the one that dies mid-stage.
RATE_LIMIT_CEILING = 0.99
#: Statuses a healthy stream reports. Anything else is a refusal to serve.
ALLOWED_LIMIT_STATUS = ('allowed', 'allowed_warning', 'warning', 'ok')

GRACE = 5.0
#: How long the reader waits on the stream before looking at the clock again.
TICK = 0.5
VERSIONS = {}


def version(binary=BINARY):
    """``claude --version``'s first line, or ``None``. Asked once per process."""
    if binary not in VERSIONS:
        VERSIONS[binary] = None
        if shutil.which(binary):
            try:
                done = subprocess.run([binary, '--version'], capture_output=True, text=True,
                                      timeout=60, encoding='utf-8', errors='replace')
                first = ((done.stdout or '') + (done.stderr or '')).strip().splitlines()
                VERSIONS[binary] = first[0][:120] if first else None
            except (OSError, subprocess.SubprocessError):
                VERSIONS[binary] = None
    return VERSIONS[binary]


def environment(extra=None):
    """The environment one agent runs in, built rather than inherited.

    Only :data:`INHERITED` is carried over from this process, so nothing about
    the session that started the run reaches the session being started — in
    particular not ``CLAUDECODE`` or any ``CLAUDE_CODE_*``, which told a child
    it was already inside a session and made its behaviour depend on ours.
    :data:`IMPOSED` is then set unconditionally.
    """
    built = {key: os.environ[key] for key in INHERITED if os.environ.get(key)}
    built.update(IMPOSED)
    built.update(extra or {})
    for key in ('ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN'):
        built.pop(key, None)  # subscription login only, never a key
    return built


def rate_limit_reason(event):
    """A rate limit, read from the shape the real stream sends.

    The event is ``{'type': 'rate_limit_event', 'rate_limit_info': {...}}``, and
    a healthy one says ``status: 'allowed'`` — a subscription with no overage
    credit reports ``overageStatus: 'rejected'`` on every call it serves
    perfectly well, so only ``status`` itself stops a run. Any nested object
    that reports a numeric ``utilization`` is also honoured, without this code
    asserting what those windows are called.
    """
    if not isinstance(event, dict):
        return None
    info = event.get('rate_limit_info')
    if not isinstance(info, dict):
        info = event.get('rate_limit')
    if not isinstance(info, dict):
        return None
    status = str(info.get('status') or '').strip().lower()
    if status and status not in ALLOWED_LIMIT_STATUS:
        detail = info.get('overageDisabledReason') or info.get('reason') or 'no reason given'
        return f'the subscription rate limit reported status {status} ({detail})'
    for name, window in info.items():
        levels = [(name, window)]
        if isinstance(window, dict):
            levels += list(window.items())
        for label, level in levels:
            used = level.get('utilization') if isinstance(level, dict) else None
            if isinstance(used, (int, float)) and not isinstance(used, bool):
                if used >= RATE_LIMIT_CEILING:
                    return f'the {label} rate-limit window is at {used:.0%}'
    return None


def tokens_of(usage):
    """Every token one usage block accounts for, cache included."""
    if not isinstance(usage, dict):
        return 0
    total = 0
    for key in ('input_tokens', 'output_tokens', 'cache_creation_input_tokens',
                'cache_read_input_tokens'):
        value = usage.get(key)
        if isinstance(value, (int, float)):
            total += int(value)
    return total


def events_of(line):
    try:
        event = json.loads(line)
    except ValueError:
        return None
    return event if isinstance(event, dict) else None


def answer_of(result):
    """The final answer in a ``result`` event, as a dict, or ``None``."""
    for key in ('structured_output', 'structuredOutput'):
        held = result.get(key)
        if isinstance(held, dict):
            return held
    text = result.get('result')
    if isinstance(text, str) and text.strip().startswith('{'):
        try:
            held = json.loads(text)
        except ValueError:
            return None
        return held if isinstance(held, dict) else None
    return None


def ran_as(result, seen):
    """The model the session actually ran, by its own account.

    Asked of the result's ``modelUsage`` first, which names the canonical model
    per model actually billed, and of the assistant messages second. Never
    taken from what we asked for: a substituted model that nobody noticed is
    the failure this reads around.
    """
    usage = result.get('modelUsage') or result.get('model_usage') or {}
    if isinstance(usage, dict):
        for name, row in usage.items():
            if isinstance(row, dict) and row.get('canonicalModel'):
                return str(row['canonicalModel'])
            if name:
                return str(name)
    return sorted(seen)[0] if seen else ''


def pump(stream, lines):
    try:
        for line in stream:
            lines.put(line)
    except (OSError, ValueError):
        pass
    finally:
        lines.put(None)


def drain(stream, into):
    try:
        for line in stream:
            into.append(line)
    except (OSError, ValueError):
        pass


class ClaudeCodeRunner(Runner):
    """Runs each agent as its own headless Claude Code session."""

    remote = True

    def __init__(self, binary=BINARY, keep_events=False):
        self.binary = binary
        #: Set when something wants the raw stream of every agent on disk.
        self.keep_events = keep_events
        self.events_root = None

    # -- what this runner can run ----------------------------------------

    def unavailable(self, models):
        """Why this login cannot run these models, one line each.

        Two refusals, both from :mod:`peerreviewagents.plugin.models`: a name
        no version of this package knows, and a known model an
        ``availableModels`` allowlist leaves out. Asked before anything is
        written, because Claude Code substituting a model of its own choosing
        would leave the run recording an arm it did not run.
        """
        if not shutil.which(self.binary):
            return [MISSING_CLAUDE]
        assignment = models if isinstance(models, dict) else {'model': models}
        return model_plan.problems(assignment, model_plan.allowlist())

    def describe(self):
        return f'claude-code {version(self.binary) or "(version unknown)"}'

    # -- running one agent -----------------------------------------------

    def definition(self, task):
        """The session agent definition for one task.

        The tool list is the agent's own, under the server's wire names, plus
        the one built-in that answers a schema. ``prompt`` is the definition's
        instructions: who this agent is, what it is for and how it works.
        """
        allowed = [f'{TOOL_PREFIX}{tool}' for tool in task.tools] + [ANSWER_TOOL]
        return allowed, {
            task.agent.name: {
                'description': task.agent.description,
                'prompt': task.instructions,
                'model': task.model,
                'effort': task.effort,
                'maxTurns': task.max_turns,
                'omitClaudeMd': True,
                'tools': allowed,
            }
        }

    def command(self, task, directory):
        allowed, definition = self.definition(task)
        agents = Path(directory) / 'agents.json'
        agents.write_text(json.dumps(definition), encoding='utf-8')
        return [
            self.binary, '-p', task.prompt,
            '--agents', str(agents),
            '--agent', task.agent.name,
            '--mcp-config', json.dumps({'mcpServers': {'peerreview': {
                'type': 'http', 'url': task.address}}}),
            '--strict-mcp-config',
            # No built-ins: nothing that reads a file, runs a command, edits
            # anything or reaches the web on its own.
            '--tools', '',
            '--allowed-tools', ','.join(allowed),
            # No user, project or local settings file.
            '--setting-sources', '',
            '--disable-slash-commands',
            '--model', task.model,
            '--effort', task.effort,
            '--permission-mode', 'dontAsk',
            '--permission-prompts', 'none',
            '--no-session-persistence',
            '--json-schema', json.dumps(task.schema),
            '--output-format', 'stream-json',
            '--verbose',
        ]

    def run(self, task):
        if not task.address:
            return Outcome(ending=FAILED, model=task.model,
                           detail='no tool server address was given to this agent')
        if not shutil.which(self.binary):
            return Outcome(ending=FAILED, model=task.model, detail=MISSING_CLAUDE)
        # The working directory is empty and thrown away: there is nothing in
        # it to read even if the agent found a way to look, and no project
        # settings or CLAUDE.md to be found above it that is ours.
        directory = tempfile.mkdtemp(prefix='peerreview-agent-')
        try:
            return self._session(task, directory)
        finally:
            shutil.rmtree(directory, ignore_errors=True)

    def _session(self, task, directory):
        command = self.command(task, directory)
        started = time.monotonic()
        deadline = started + float(task.timeout) if task.timeout else None
        ceiling = int(task.token_ceiling or 0)
        child = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True, cwd=directory, env=environment(),
                                 encoding='utf-8', errors='replace', bufsize=1)
        lines, tail, events = queue.Queue(), [], []
        threading.Thread(target=pump, args=(child.stdout, lines), daemon=True).start()
        threading.Thread(target=drain, args=(child.stderr, tail), daemon=True).start()
        state = {'tokens': 0, 'calls': 0, 'models': set(), 'result': None, 'init': None,
                 'stop': '', 'last': '', 'limits': []}
        open_stream = True
        while open_stream:
            try:
                line = lines.get(timeout=TICK)
            except queue.Empty:
                line = ''
            if line is None:
                open_stream = False
            elif line.strip():
                event = events_of(line)
                if event is not None:
                    self._read(event, state)
                    if self.keep_events:
                        events.append(event)
            if not state['stop'] and ceiling and state['tokens'] > ceiling:
                state['stop'] = OVER_BUDGET
            if not state['stop'] and deadline and time.monotonic() > deadline:
                state['stop'] = TIMED_OUT
            if state['stop'] and open_stream:
                break
        if state['stop']:
            self._end(child)
        else:
            try:
                child.wait(timeout=GRACE)
            except subprocess.TimeoutExpired:
                self._end(child)
        return self._outcome(task, state, started, tail, events, directory)

    def _read(self, event, state):
        limit = rate_limit_reason(event)
        if limit and limit not in state['limits']:
            state['limits'].append(limit)
        kind = event.get('type')
        if kind == 'system' and event.get('subtype') == 'init':
            state['init'] = event
            return
        if kind == 'assistant':
            message = event.get('message') or {}
            if message.get('model'):
                state['models'].add(str(message['model']))
            # Every request's whole input is paid for again, cache reads
            # included, so the sum over messages is what the session spends
            # and what the result event reports at the end.
            state['tokens'] += tokens_of(message.get('usage'))
            for block in message.get('content') or ():
                if isinstance(block, dict):
                    if block.get('type') == 'tool_use':
                        state['calls'] += 1
                    elif block.get('type') == 'text' and str(block.get('text') or '').strip():
                        state['last'] = str(block['text']).strip()
            return
        if kind == 'result':
            state['result'] = event
            total = tokens_of(event.get('usage'))
            if total > state['tokens']:
                state['tokens'] = total

    def _end(self, child):
        try:
            child.terminate()
            child.wait(timeout=GRACE)
        except (subprocess.TimeoutExpired, OSError):
            try:
                child.kill()
            except OSError:
                pass

    def _outcome(self, task, state, started, tail, events, directory):
        seconds = time.monotonic() - started
        result = state['result'] or {}
        record = {'init': self._connected(task, state), 'result': {
            key: result.get(key) for key in ('subtype', 'is_error', 'num_turns', 'duration_ms',
                                             'session_id')},
                  'rate_limits': list(state['limits'])}
        if self.keep_events and self.events_root:
            record['events'] = self._keep(task, events)
        stop = state['stop']
        if stop:
            return Outcome(ending=stop, tokens=state['tokens'], model=ran_as(result,
                           state['models']) or task.model, tool_calls=state['calls'],
                           seconds=seconds, record=record,
                           detail=self._detail(task, state, tail))
        answer = answer_of(result)
        if answer is None:
            return Outcome(ending=SILENT, tokens=state['tokens'],
                           model=ran_as(result, state['models']) or task.model,
                           tool_calls=state['calls'], seconds=seconds, record=record,
                           detail=self._detail(task, state, tail))
        return Outcome(ending=ANSWERED, answer=answer, tokens=state['tokens'],
                       model=ran_as(result, state['models']) or task.model,
                       tool_calls=state['calls'], seconds=seconds, record=record)

    def _connected(self, task, state):
        """What the session said about itself at start-up.

        The one check worth making on every agent: whether our server is
        connected and whether the tools this agent was granted are there. A
        silent misconfiguration would otherwise look like an agent that found
        nothing to say.
        """
        init = state['init'] or {}
        servers = {row.get('name'): row.get('status')
                   for row in init.get('mcp_servers') or () if isinstance(row, dict)}
        listed = [name for name in init.get('tools') or () if isinstance(name, str)]
        wanted = {f'{TOOL_PREFIX}{tool}' for tool in task.tools}
        return {'server': servers.get('peerreview'),
                'tools': len(listed),
                'missing': sorted(wanted - set(listed)),
                'built_ins': sorted(name for name in listed
                                    if not name.startswith(TOOL_PREFIX) and name != ANSWER_TOOL)}

    def _detail(self, task, state, tail):
        """Everything a reader needs about an agent that did not answer."""
        said = []
        init = state['init']
        if init is None:
            said.append('the session never reported starting up')
        else:
            seen = self._connected(task, state)
            if seen['server'] != 'connected':
                said.append(f'the run\'s tool server was {seen["server"] or "not there"}')
            if seen['missing']:
                said.append(f'these tools were not in the session: {", ".join(seen["missing"])}')
        result = state['result'] or {}
        if result.get('subtype') and result.get('subtype') != 'success':
            said.append(f'the session ended {result["subtype"]}')
        if state['last']:
            said.append(f'the agent signed off: {state["last"][:600]}')
        if not said and tail:
            said.append('stderr ended: ' + ''.join(tail[-6:]).strip()[:600])
        return '; '.join(said)

    def _keep(self, task, events):
        root = Path(self.events_root)
        root.mkdir(parents=True, exist_ok=True)
        path = root / f'{task.label.replace("#", "-")}.json'
        path.write_text(json.dumps(events, indent=1), encoding='utf-8')
        return str(path)
