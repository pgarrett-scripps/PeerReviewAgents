"""The panel's agent definitions, written once and run by any runner.

Each ``<name>.md`` here is one agent: a small header that says which model it
runs on, how hard it may think, how many turns it gets and which of the
server's tools it may call, then the instructions it is given, which are the
whole of what it knows about the job.

Nothing in these files names a harness. The tools are the server's own bare
names — ``read_paper``, not ``mcp__plugin_pra_peerreview__read_paper`` — and a
runner is what turns them into whatever its transport calls them (see
:mod:`peerreviewagents.plugin.claude_code`). That is the point of keeping them
here rather than inside a plugin: the instruction text is the review's, and the
plugin was only ever one way to run it.

The scripted runner in :mod:`peerreviewagents.plugin.runner` reads the same
definitions, so the tool list a test confines an agent to is the tool list a
real run grants it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).parent

#: Keys a definition's header may set. A header key that is not here is a
#: typo or a leftover from a harness that no longer runs these agents, and
#: reading it as if it meant something is worse than refusing it.
HEADER = ('name', 'description', 'model', 'effort', 'maxTurns', 'tools')

FRONT = re.compile(r'\A---\n(.*?)\n---\n', re.S)


@dataclass(frozen=True)
class Definition:
    """One agent: what it is, what it may call, and what it is told."""

    name: str
    description: str
    model: str
    effort: str
    max_turns: int
    tools: tuple[str, ...]
    instructions: str

    def allowed(self, prefix=''):
        """This agent's tools under one transport's naming."""
        return tuple(f'{prefix}{tool}' for tool in self.tools)


def path(name):
    return HERE / f'{name}.md'


def names():
    """Every agent defined here, in alphabetical order."""
    return tuple(sorted(found.stem for found in HERE.glob('*.md')))


def read(name):
    """One definition, parsed from its file.

    The parse is deliberately strict about the header and indifferent to the
    body: a missing field or an unknown key raises, and everything after the
    second ``---`` is the instructions, exactly as written.
    """
    found = path(name)
    if not found.is_file():
        raise KeyError(f'no agent definition called {name!r} in {HERE}')
    text = found.read_text(encoding='utf-8')
    match = FRONT.match(text)
    if not match:
        raise ValueError(f'{found} does not start with a --- header')
    header = {}
    for line in match.group(1).splitlines():
        if not line.strip():
            continue
        key, _, value = line.partition(':')
        key = key.strip()
        if key not in HEADER:
            raise ValueError(f'{found} sets {key!r}, which is not one of {HEADER}')
        header[key] = value.strip()
    missing = [key for key in HEADER if key not in header]
    if missing:
        raise ValueError(f'{found} is missing {missing}')
    if header['name'] != name:
        raise ValueError(f'{found} calls itself {header["name"]!r}')
    tools = tuple(tool for tool in re.split(r'[,\s]+', header['tools']) if tool)
    return Definition(name=name, description=header['description'], model=header['model'],
                      effort=header['effort'], max_turns=int(header['maxTurns']), tools=tools,
                      instructions=text[match.end():].strip())


CACHE = {}


def definition(name):
    """One definition, read once per process."""
    if name not in CACHE:
        CACHE[name] = read(name)
    return CACHE[name]


def all_definitions():
    return tuple(definition(name) for name in names())
