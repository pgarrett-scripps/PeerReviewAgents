"""What it takes to run one agent, and what comes back when it stops.

The orchestrator decides *what* the panel does; a runner decides *how* an agent
is run. Between them there is one call — :meth:`Runner.run` — and two plain
objects: a :class:`Task`, which is everything an agent needs to exist, and an
:class:`Outcome`, which is everything the orchestrator needs to know about what
happened. Nothing else crosses.

A task is deliberately small and complete: the definition the agent is (its
instructions and its tools), the stage's own prompt, the model and effort it
runs at, a turn cap, the address of this run's tool server, the schema its
final answer must satisfy, and the token ceiling past which it is not allowed
to continue. A runner that can honour those can run this panel; nothing in a
task names Claude Code, a process, or a transport.

An outcome is equally small: the final answer as a dict, the tokens it cost,
the model it actually ran as — not the one it was asked for — the number of
tool calls, how long it took, and how it ended. ``ending`` is the one field
with a closed vocabulary, because the orchestrator branches on it: a stage that
answered is a stage that closed, and every other ending is a stage to retry
once and then block over.

Two runners live in the tree. :class:`ScriptedRunner` is here: it runs a Python
function in place of a model, against the same tool list the real agent is
confined to, which is how the whole panel is tested without spending a token.
The Claude Code runner is in :mod:`peerreviewagents.plugin.claude_code`.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace

from ..agents import Definition, definition

#: The stage answered: its final answer is in ``Outcome.answer``.
ANSWERED = 'answered'
#: The agent stopped without a final answer — it ran out of turns, refused, or
#: ended its turn without calling the tool that answers.
SILENT = 'no answer'
#: The agent was stopped because it passed its token ceiling.
OVER_BUDGET = 'over the token ceiling'
#: The agent was stopped because the run's wall clock, or its own, ran out.
TIMED_OUT = 'out of time'
#: The agent could not be run at all, or died: ``Outcome.detail`` says how.
FAILED = 'failed'

ENDINGS = (ANSWERED, SILENT, OVER_BUDGET, TIMED_OUT, FAILED)


class NotGranted(Exception):
    """An agent reached for a tool its own definition does not grant.

    Only :class:`ScriptedRunner` can raise this, because only there do we hold
    the call before it happens. A real runner confines an agent at its own
    boundary — the tool is not there to call — and that is the behaviour this
    stands in for.
    """


@dataclass(frozen=True)
class Task:
    """One agent, ready to run."""

    agent: Definition
    #: What this stage tells this agent to do, on top of its definition.
    prompt: str
    #: The shape its final answer must have.
    schema: dict
    #: The label this agent is accounted under: its slot, plus ``#2`` on a
    #: retry. One agent, one row in the usage files.
    label: str
    model: str = ''
    effort: str = ''
    max_turns: int = 0
    #: The server tools it may call, by their bare names.
    tools: tuple = ()
    #: This run's MCP endpoint, or ``None`` for a runner that needs none.
    address: str = ''
    #: Tokens it may spend before it is stopped, or ``None`` for no ceiling.
    token_ceiling: int = 0
    #: Seconds it may take before it is stopped, or ``None``.
    timeout: float = 0.0

    @classmethod
    def of(cls, agent, prompt, schema, label='', **over):
        """A task for one agent, defaulting to that agent's own definition."""
        held = agent if isinstance(agent, Definition) else definition(agent)
        return cls(agent=held, prompt=prompt, schema=schema, label=label or held.name,
                   model=over.pop('model', '') or held.model,
                   effort=over.pop('effort', '') or held.effort,
                   max_turns=over.pop('max_turns', 0) or held.max_turns,
                   tools=tuple(over.pop('tools', ()) or held.tools), **over)

    def with_(self, **over):
        return replace(self, **over)

    @property
    def instructions(self):
        """The agent's standing instructions: who it is and how it works."""
        return self.agent.instructions

    def grants(self, tool):
        return tool in self.tools


@dataclass
class Outcome:
    """What one agent left behind."""

    ending: str
    answer: dict = None
    tokens: int = 0
    model: str = ''
    tool_calls: int = 0
    seconds: float = 0.0
    #: Everything a human needs to read about a stage that did not answer: the
    #: agent's own last word, the stop reason, the stderr tail.
    detail: str = ''
    #: The runner's raw record of the run, for the usage files. Never branched
    #: on, only written down.
    record: dict = field(default_factory=dict)

    @property
    def closed(self):
        return self.ending == ANSWERED and isinstance(self.answer, dict)

    def __post_init__(self):
        if self.ending not in ENDINGS:
            raise ValueError(f'{self.ending!r} is not one of {ENDINGS}')


class Runner:
    """What the orchestrator needs from whatever runs its agents."""

    #: True when an agent is something other than this process — then the run
    #: has to open its tool server on the wire before any agent starts.
    remote = True

    def run(self, task):
        """Run one agent to its end and account for it."""
        raise NotImplementedError

    def opened(self, service):
        """Told which tools this run serves, before any agent starts.

        A remote runner needs nothing from this: its agents reach the tools
        over the address on their task. A runner that calls them in this
        process has no other way to find them — the orchestrator owns the
        service, and this is where it hands it over.
        """
        return self

    def unavailable(self, models):
        """Why these models cannot be run here, one sentence each.

        Asked once, before anything is written, so a run that cannot have the
        panel it was asked for fails at the start instead of silently running
        something else. An empty list means every model is available.
        """
        return []

    def describe(self):
        """One line for the run's provenance: what ran these agents."""
        return type(self).__name__


class Tools:
    """The tool list one scripted agent is confined to.

    Every call goes through :meth:`Service.call`, so a scripted panel exercises
    the real server and the real state. What it adds is the boundary: a tool
    this agent's definition does not grant raises :class:`NotGranted` instead
    of quietly working, which is the one thing a scripted run has to get right
    if it is to catch a prompt that tells an agent to call a tool it does not
    have.
    """

    def __init__(self, service, task):
        self.service = service
        self.task = task
        self.calls = []

    def __getattr__(self, name):
        if name.startswith('_'):
            raise AttributeError(name)
        if name not in TOOL_NAMES:
            raise AttributeError(f'the server has no tool called {name!r}')
        if not self.task.grants(name):
            raise NotGranted(f'{self.task.agent.name} ({self.task.label}) is not granted '
                             f'{name}; it has {", ".join(self.task.tools)}')

        def call(*args, **keywords):
            self.calls.append(name)
            return self.service.call(name, *args, **keywords)

        return call

    def claim(self, slot, code):
        """The first call of every agent's life, by the name every one uses."""
        return self.claim_agent(slot, code)


class ScriptedRunner(Runner):
    """A panel with Python where the models go.

    A script is a callable taking ``(tools, task)`` and returning the final
    answer as a dict — ``None`` for an agent that stops without answering, or
    raising to stand in for one that dies. Scripts are looked up by the task's
    label first and the agent's name second, so a test can script the panel by
    role and then say something different about one slot.
    """

    remote = False

    def __init__(self, scripts=None, service=None, tokens=1000):
        self.scripts = dict(scripts or {})
        self.service = service
        self.tokens = tokens
        self.ran = []

    def opened(self, service):
        self.service = service
        return self

    def script_for(self, task):
        for key in (task.label, task.label.split('#')[0], task.agent.name):
            if key in self.scripts:
                return self.scripts[key]
        return self.scripts.get('*')

    def run(self, task):
        if self.service is None:
            raise RuntimeError('a scripted runner needs the run\'s service to call tools')
        script = self.script_for(task)
        self.ran.append(task)
        tools = Tools(self.service, task)
        started = time.monotonic()
        if script is None:
            return Outcome(ending=SILENT, model=task.model, seconds=0.0,
                           detail=f'nothing is scripted for {task.label}')
        try:
            answer = script(tools, task)
        except NotGranted:
            raise
        except Exception as problem:  # noqa: BLE001 - a scripted agent that dies
            return Outcome(ending=FAILED, model=task.model, tool_calls=len(tools.calls),
                           seconds=time.monotonic() - started,
                           detail=f'{type(problem).__name__}: {problem}')
        seconds = time.monotonic() - started
        if answer is None:
            return Outcome(ending=SILENT, model=task.model, tool_calls=len(tools.calls),
                           seconds=seconds, detail='the script answered nothing')
        return Outcome(ending=ANSWERED, answer=dict(answer), tokens=self.tokens,
                       model=task.model, tool_calls=len(tools.calls), seconds=seconds,
                       record={'scripted': True, 'tools': list(tools.calls)})

    def describe(self):
        return 'scripted'


def _tool_names():
    from .service import TOOLS

    return frozenset(TOOLS)


TOOL_NAMES = _tool_names()
