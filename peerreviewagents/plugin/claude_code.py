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

* ``--setting-sources ''``, which is what keeps the owner's instructions out.
  Claude Code's documented load table makes every ``CLAUDE.md`` conditional on
  a setting source: ``~/.claude/CLAUDE.md`` and ``~/.claude/rules/*.md`` load
  only when the sources include ``user``, the project and parent-directory
  files only with ``project``, ``CLAUDE.local.md`` only with ``local``. An
  empty list names none of them, so none of them is read. A reviewer that read
  the owner's instructions is not reviewing the manuscript on its own terms,
  and the first fix check found an agent doing exactly that.
* ``CLAUDE_CODE_DISABLE_AUTO_MEMORY``, because auto memory is the one
  instruction file the documentation says is read *whatever* the setting
  sources are: ``~/.claude/projects/<project>/memory/`` goes into the system
  prompt at start-up. The throwaway working directory already makes that a
  folder no owner has ever written to, and the variable is the documented way
  to switch it off rather than rely on that.
* ``omitClaudeMd`` in the agent definition, which the documentation scopes to
  an agent *spawned as a subagent*; ours run as the main session of their own
  process, so it is belt to the ``--setting-sources`` braces and not the thing
  doing the work.
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

What that number *means* changed when the orchestrator took the panel over, and
the first real run found it the hard way. Each assistant message's ``usage`` is
one call's usage, so summing them is the session's **cumulative** spend — the
same total Claude Code's own ``result`` reports. The workflow harness this
engine used to read reported a per-agent figure that was the largest single
call instead: the context the agent was holding at the end. For a one-turn
agent those are the same number; for a reader that works through 29 pages they
are 592,106 and 144,072. Both are now written down, under their own names
(:func:`parts_of` for the four parts, ``peak_context`` for the largest call),
and ``Outcome.tokens`` — what the ceiling is measured against — is the
cumulative one. ``peak_context`` is the figure that compares with runs made
before the orchestrator existed.

Being stopped is not the same as having nothing to say. A session that has
already called ``StructuredOutput`` has answered, whatever the ceiling then
does to it: the first proving run killed a reader that had viewed every page,
repaired every piece and sealed the manuscript, and threw the answer away
because the kill landed before Claude Code's ``result`` event. So the last
``StructuredOutput`` in the stream is kept, and a stopped agent that answered
ends :data:`~peerreviewagents.plugin.runner.ANSWERED` with a note saying it was
stopped. Finished work is never thrown away.
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

from ..names import TOKEN_FIELDS, TOKEN_PEAK
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
    # Auto memory is read whatever the setting sources say, so it is turned
    # off by name. Every other CLAUDE.md is already gated on a source, and
    # this session asks for none.
    'CLAUDE_CODE_DISABLE_AUTO_MEMORY': '1',
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


def executable(binary=BINARY):
    """Claude Code's path, resolved the way a shell resolves it, or ``None``.

    The resolved path is what gets run, not the bare word. ``shutil.which``
    honours PATHEXT, so on Windows it finds the ``claude.cmd`` an install puts
    on PATH; naming the bare word there would find nothing, because the process
    that starts a child does not consult PATHEXT itself.
    """
    return shutil.which(binary)


def version(binary=BINARY):
    """``claude --version``'s first line, or ``None``. Asked once per process."""
    if binary not in VERSIONS:
        VERSIONS[binary] = None
        found = executable(binary)
        if found:
            try:
                done = subprocess.run([found, '--version'], capture_output=True, text=True,
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


#: The four things a usage block counts, each under the name it is written down
#: as — :data:`~peerreviewagents.names.TOKEN_FIELDS` is the whole vocabulary,
#: and these are its parts in its own order. Kept apart because they are not
#: the same token: cache reads are most of a long agent's total and the cheapest
#: part of it, and an agent whose total is nearly all cache read is not an agent
#: that is running away.
PARTS = tuple(zip(TOKEN_FIELDS[1:5], ('input_tokens', 'cache_creation_input_tokens',
                                      'cache_read_input_tokens', 'output_tokens')))


def tokens_of(usage):
    """Every token one usage block accounts for, cache included."""
    return sum(parts_of(usage).values())


def parts_of(usage):
    """One usage block as the four parts it is made of, zeros included."""
    held = usage if isinstance(usage, dict) else {}
    parts = {}
    for name, key in PARTS:
        value = held.get(key)
        parts[name] = int(value) if isinstance(value, (int, float)) else 0
    return parts


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
        if not executable(self.binary):
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
                # Scoped by Claude Code to an agent spawned as a subagent;
                # ours is the main session, and ``--setting-sources ''`` is
                # what keeps the owner's CLAUDE.md out of it.
                'omitClaudeMd': True,
                'tools': allowed,
            }
        }

    def command(self, task, directory):
        allowed, definition = self.definition(task)
        agents = Path(directory) / 'agents.json'
        agents.write_text(json.dumps(definition), encoding='utf-8')
        return [
            executable(self.binary) or self.binary, '-p', task.prompt,
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
        if not executable(self.binary):
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
        state = {'tokens': 0, 'parts': {}, 'peak': 0, 'calls': 0, 'models': set(),
                 'result': None, 'init': None, 'stop': '', 'last': '', 'said': None,
                 'limits': [], 'messages': {}, 'anonymous': 0, 'startup': ''}
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
                    self._read(task, event, state)
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

    def _read(self, task, event, state):
        limit = rate_limit_reason(event)
        if limit and limit not in state['limits']:
            state['limits'].append(limit)
        kind = event.get('type')
        if kind == 'system' and event.get('subtype') == 'init':
            state['init'] = event
            wrong = self._wrong_at_start(task, state)
            if wrong:
                # Nothing this session does from here is the review: an agent
                # without its tools reads as an agent that found nothing to
                # say, and the retry is cheaper than a whole silent run.
                state['startup'] = wrong
                state['stop'] = FAILED
            return
        if kind == 'assistant':
            message = event.get('message') or {}
            if message.get('model'):
                state['models'].add(str(message['model']))
            self._count(message, state)
            for block in message.get('content') or ():
                if not isinstance(block, dict):
                    continue
                if block.get('type') == 'text' and str(block.get('text') or '').strip():
                    state['last'] = str(block['text']).strip()
                # The agent's answer, as it gave it. Kept because a session can
                # be stopped, or die, between answering and the `result` event
                # that normally carries the answer out.
                if (block.get('type') == 'tool_use' and block.get('name') == ANSWER_TOOL
                        and isinstance(block.get('input'), dict)):
                    state['said'] = dict(block['input'])
            return
        if kind == 'result':
            state['result'] = event
            total = tokens_of(event.get('usage'))
            if total > state['tokens']:
                # The session's own total against our sum of the messages we
                # saw: the larger is the one to keep, and its parts come with
                # it so the parts and the total always add up.
                state['tokens'] = total
                state['parts'] = parts_of(event.get('usage'))

    def _count(self, message, state):
        """What one assistant message spent and called, counted once.

        The stream can send the same assistant message more than once — one
        event per content block, the same ``id``, the same ``usage`` on each.
        Summing every event counted a turn's whole input again per block,
        which is what the token ceiling is measured against: a long turn could
        be stopped as over budget on tokens it never spent. So a message id's
        usage is credited once, the largest figure it reports, in case a later
        event knows more — and its four parts are credited the same way, so
        the parts of the run add up to its total.

        The largest single message is kept as it goes past, because that is
        the figure a run made before the orchestrator existed reported per
        agent, and nothing else here can reconstruct it afterwards.

        The calls in such a turn are *not* the same block repeated: a real
        reader's stream sent one event per call, each carrying a different
        ``tool_use`` with its own ``id``, so counting a turn's blocks and
        keeping the largest count credited one call for a turn that made
        fifteen. Each block is therefore credited by its own identity, which
        is right whether the blocks repeat or arrive one at a time.
        """
        held = str(message.get('id') or '')
        if not held:
            state['anonymous'] += 1
            held = f'anonymous-{state["anonymous"]}'
        seen = state['messages'].setdefault(held, {'tokens': 0, 'parts': {}, 'tools': set()})
        tokens = tokens_of(message.get('usage'))
        if tokens > seen['tokens']:
            state['tokens'] += tokens - seen['tokens']
            seen['tokens'] = tokens
            state['peak'] = max(state['peak'], tokens)
        for name, value in parts_of(message.get('usage')).items():
            had = seen['parts'].get(name, 0)
            if value > had:
                state['parts'][name] = state['parts'].get(name, 0) + value - had
                seen['parts'][name] = value
        for block in message.get('content') or ():
            if not isinstance(block, dict) or block.get('type') != 'tool_use':
                continue
            # A block of our own making has an id; one without is told apart
            # by what it says, so a block that truly repeats is still one call.
            which = str(block.get('id') or json.dumps(block, sort_keys=True, default=str))
            if which not in seen['tools']:
                seen['tools'].add(which)
                state['calls'] += 1

    def _wrong_at_start(self, task, state):
        """Why this session is not the one we asked for, from its own init.

        Three ways a session can start up wrong, all of them silent: our tool
        server is not connected, a tool this agent was granted is not there,
        or a built-in is — anything that reads a file, runs a command or
        reaches the web on its own, which no agent of ours is allowed.
        """
        seen = self._connected(task, state)
        said = []
        if seen['server'] != 'connected':
            said.append(f'the run\'s tool server was {seen["server"] or "not there"}')
        if seen['missing']:
            said.append(f'these granted tools were not in the session: '
                        f'{", ".join(seen["missing"])}')
        if seen['built_ins']:
            said.append(f'the session had built-in tools of its own: '
                        f'{", ".join(seen["built_ins"])}')
        return '; '.join(said)

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
        # The answer the `result` event carried, or — when there was no result,
        # because the session was stopped or died first — the last answer the
        # agent gave in the stream. Work that is done is not thrown away for
        # the sake of the event that normally reports it.
        answer = answer_of(result)
        if answer is None and isinstance(state['said'], dict):
            answer = dict(state['said'])
        ending, note = stop or ANSWERED, ''
        if stop and answer is not None and not state['startup']:
            # A session that started up wrong is never answered, whatever it
            # then said: it is not the agent we asked for.
            ending, note = ANSWERED, (f'the agent answered and was then stopped: {stop}. '
                                      'The answer it gave is this stage\'s.')
        elif not stop and answer is None:
            ending = SILENT
        common = {'tokens': state['tokens'], 'usage': self._usage(state),
                  'model': ran_as(result, state['models']) or task.model,
                  'tool_calls': state['calls'], 'seconds': seconds, 'record': record}
        if ending == ANSWERED:
            if note:
                record['stopped'] = stop
            return Outcome(ending=ANSWERED, answer=answer, detail=note, **common)
        return Outcome(ending=ending, detail=self._detail(task, state, tail), **common)

    def _usage(self, state):
        """What this agent spent, in every unit the record keeps.

        ``tokens_cumulative`` is the session's whole spend and the number the
        ceiling is measured against; ``tokens_peak_context`` is its largest
        single call, which is what a run made before the orchestrator existed
        reported per agent. The four parts add up to the cumulative total.
        """
        return {TOKEN_FIELDS[0]: int(state['tokens']),
                **{name: int(state['parts'].get(name, 0)) for name, _ in PARTS},
                TOKEN_PEAK: int(state['peak'])}

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
            wrong = state.get('startup') or self._wrong_at_start(task, state)
            if wrong:
                said.append(wrong)
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
