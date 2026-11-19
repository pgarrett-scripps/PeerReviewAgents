"""Every tool a workflow script tells an agent to call, that agent is granted.

The first real fix check judged all six problems and then could not write the
check: ``check-fixes.js`` tells the recorder to call ``write_fix_check``,
``recorder.md`` never granted it, and the one step that writes
``fix-check.json`` and ``FIXES.md`` answered *No such tool available*. The
verdicts stayed in ``fix-checks.jsonl`` and the run ended blocked over a
one-word omission in a tools list.

Nothing read the two sides against each other, so this does. It reads every
agent call in both scripts as text — the prompt the script hands that agent,
and the prompt helpers that prompt is built from — and asserts that every
server tool the text tells the agent to call is in that agent's own ``tools``
header. A prompt that names a tool its agent cannot call is the bug, whichever
side of the pair is wrong.

It is deliberately a text test. The grant and the instruction are written in
two different files in two different languages, and the only thing that ties
them together is the tool's name.
"""

from __future__ import annotations

import re
from pathlib import Path

from peerreviewagents.plugin import server
from peerreviewagents.plugin.starter import TOOL_PREFIX, plugin_dir

AGENTS = plugin_dir() / 'agents'
WORKFLOWS = plugin_dir() / 'workflows'
SCRIPTS = ('review.js', 'check-fixes.js')
TOOLS = tuple(sorted(tool.__name__ for tool in server.TOOLS))

#: The agent types ``review.js``'s ``agentTypeFor`` can name when it reopens a
#: stage the ledger never recorded: a core reviewer, or the specialist whose
#: track the slot holds — ``spec-stats`` is ``pra:stats``, ``spec-code`` is
#: ``pra:code``. A prompt handed to that call reaches any of them, so each one
#: has to be able to follow it.
CLOSERS = ('core-reviewer', *server.SPECIALIST_TRACKS, 'code')

#: `agent(` and `stage(` call sites. The lookbehind keeps `claim_agent(` out of
#: it, which is a tool name and not a call to an agent.
CALL = re.compile(r'(?<![\w$.])(?:agent|stage)\(')
NAMED_TYPE = re.compile(r"agentType:\s*'pra:([\w-]+)'")
LITERAL_TYPE = re.compile(r"'pra:([\w-]+)'")
HELPER_USE = re.compile(r'\b(\w+Prompt)\s*\(')
HELPER_DEFINITION = re.compile(r'^(?:const|function|async function)\s+(\w+Prompt)\b')
TOP_LEVEL = re.compile(r'^(?:const|let|function|async function|}|/\*)')


def granted(agent):
    """The server tools ``<agent>.md`` grants, by their bare names."""
    text = (AGENTS / f'{agent}.md').read_text(encoding='utf-8')
    header = text.split('---')[1]
    field = re.search(r'^tools:(.*?)(?=^\w+:|\Z)', header, re.M | re.S)
    return tuple(re.findall(re.escape(TOOL_PREFIX) + r'(\w+)', field.group(1) if field else ''))


def readable(script):
    """One script with its full-line comments taken out.

    A comment is written for the reader of the script and is not part of what
    any agent is told, so a note about ``write_review`` beside a stage that
    never calls it would otherwise read as an instruction to call it. Only
    whole-line comments are removed: a ``//`` inside a prompt is part of a URL.
    """
    text = re.sub(r'/\*.*?\*/', '', (WORKFLOWS / script).read_text(encoding='utf-8'), flags=re.S)
    return ''.join(line for line in text.splitlines(keepends=True)
                   if not line.lstrip().startswith('//'))


def helpers(text):
    """Every ``somethingPrompt`` helper in one script, as text."""
    lines, found = text.splitlines(keepends=True), {}
    for index, line in enumerate(lines):
        start = HELPER_DEFINITION.match(line)
        if not start:
            continue
        body = [line]
        for following in lines[index + 1:]:
            if TOP_LEVEL.match(following):
                break
            body.append(following)
        found[start.group(1)] = ''.join(body)
    return found


def instructions(text):
    """Every agent call in one script, as ``(agent types, what it is told)``.

    The call region is the text between the call's own brackets, which holds
    the prompt, the helper that builds it, the label and the schema. The agent
    types are the ``pra:`` names the call gives — or, where it names none
    because the type is worked out from the slot, every type it could reach.
    """
    helper = helpers(text)
    for match in CALL.finditer(text):
        depth, index = 1, match.end()
        while index < len(text) and depth:
            depth += {'(': 1, ')': -1}.get(text[index], 0)
            index += 1
        region = text[match.end():index - 1]
        said = region + ''.join(helper.get(name, '') for name in HELPER_USE.findall(region))
        named = NAMED_TYPE.findall(region) or LITERAL_TYPE.findall(region)
        if named:
            yield tuple(named), said
        elif HELPER_USE.search(region):
            # A prompt sent to an agent type the call works out from the slot.
            yield CLOSERS, said


def demanded(said):
    """The server tools a prompt tells its agent to call."""
    return tuple(tool for tool in TOOLS if re.search(r'\b' + tool + r'\b', said))


def test_every_tool_a_script_names_in_a_prompt_is_one_that_agent_is_granted():
    missing = []
    for script in SCRIPTS:
        text = readable(script)
        for agents, said in instructions(text):
            for agent in agents:
                have = granted(agent)
                for tool in demanded(said):
                    if tool not in have:
                        missing.append(f'{script} tells pra:{agent} to call {tool}')
    assert not sorted(set(missing)), 'a prompt names a tool its agent cannot call: ' + '; '.join(
        sorted(set(missing)))


def test_the_scripts_and_the_agents_are_both_really_read():
    """The test above would pass on an empty reading of either side.

    A regex that matched nothing, an agents folder somewhere else, a script
    renamed: each would make every assertion above vacuously true. So the
    reading itself is asserted — the calls are found, the agent types are
    named, and the tools are read off both sides.
    """
    for script in SCRIPTS:
        found = list(instructions(readable(script)))
        assert len(found) >= 4, script
        assert any(demanded(said) for _, said in found), script
        assert {'recorder'} <= {agent for agents, _ in found for agent in agents}, script
    assert len(granted('recorder')) >= 8
    assert 'claim_agent' in granted('core-reviewer')
    assert demanded('Then call seal_manuscript. It refuses') == ('seal_manuscript',)


def test_every_agent_grants_only_tools_the_server_has():
    """A tools header that names something the server does not serve.

    The other half of the same seam: a tool renamed in the server leaves a
    grant that grants nothing, and an agent whose header still names it reads
    as able to call it.
    """
    strays = {}
    for path in sorted(Path(AGENTS).glob('*.md')):
        unknown = [tool for tool in granted(path.stem) if tool not in TOOLS]
        if unknown:
            strays[path.stem] = unknown
    assert not strays, f'agents granting tools the server does not serve: {strays}'
