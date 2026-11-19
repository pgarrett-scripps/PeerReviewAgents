"""Every tool a prompt tells an agent to call, that agent is granted.

The first real fix check judged all six problems and then could not write the
check: the fix-check script told the recorder to call ``write_fix_check``,
``recorder.md`` never granted it, and the one step that writes
``fix-check.json`` and ``FIXES.md`` answered *No such tool available*. The
verdicts stayed in ``fix-checks.jsonl`` and the run ended blocked over a
one-word omission in a tools list.

Nothing read the two sides against each other, so this does. The prompts come
from :func:`peerreviewagents.plugin.orchestrator.prompt_catalogue`, which is
every prompt the orchestrator can send paired with the agent that receives it,
and the grants come from that agent's own definition. A prompt that names a
tool its agent cannot call is the bug, whichever side of the pair is wrong.

It is still a text test, and still has to be: the grant is a header in a
Markdown file, the instruction is prose inside a Python string, and the only
thing that ties them together is the tool's name. What changed is that the
prompts are now read as the strings they are, rather than cut out of a
JavaScript file with a bracket counter.
"""

from __future__ import annotations

import re

from peerreviewagents import agents as pack
from peerreviewagents.plugin import orchestrator, server

TOOLS = tuple(sorted(tool.__name__ for tool in server.TOOLS))
CATALOGUE = orchestrator.prompt_catalogue()


def granted(agent):
    """The server tools ``<agent>.md`` grants, by their bare names."""
    return pack.definition(agent).tools


def demanded(said):
    """The server tools a prompt tells its agent to call."""
    return tuple(tool for tool in TOOLS if re.search(r'\b' + tool + r'\b', said))


def test_every_tool_a_prompt_names_is_one_that_agent_is_granted():
    missing = []
    for agent, said in CATALOGUE:
        have = granted(agent)
        for tool in demanded(said):
            if tool not in have:
                missing.append(f'a prompt tells {agent} to call {tool}')
    assert not sorted(set(missing)), 'a prompt names a tool its agent cannot call: ' + '; '.join(
        sorted(set(missing)))


def test_the_prompts_and_the_agents_are_both_really_read():
    """The test above would pass on an empty reading of either side.

    A catalogue that lost a stage, an agent whose header stopped parsing, a
    regex that matched nothing: each would make every assertion above
    vacuously true. So the reading itself is asserted — the prompts are there,
    they name tools, and the grants are read off the definitions.
    """
    assert len(CATALOGUE) >= 20
    assert all(len(said) > 200 for _, said in CATALOGUE)
    assert sum(len(demanded(said)) for _, said in CATALOGUE) >= 40
    # The stages a review cannot do without, each with its own agent.
    assert {'reader', 'core-reviewer', 'editor', 'fix-check'} <= {a for a, _ in CATALOGUE}
    assert 'claim_agent' in granted('core-reviewer')
    assert 'submit_finding' in granted('stats')
    assert 'write_fix_check' not in granted('fix-check')
    assert demanded('Then call seal_manuscript. It refuses') == ('seal_manuscript',)


def test_every_prompt_asks_for_a_tool_and_every_agent_is_told_to_claim():
    """A stage closes by calling a tool, and the prompt has to ask for one.

    The sharper half of the same seam: a prompt that names no tool at all would
    pass the test above, and an agent that is never told to claim its slot is
    refused by every tool it then calls. The claim can be asked for by the
    prompt or by the agent's own instructions — the revision prompt names the
    slot and the code, and ``core-reviewer.md`` names the tool — so both sides
    are read here.
    """
    for agent, said in CATALOGUE:
        called = demanded(said)
        assert called, f'a prompt for {agent} names no tool at all'
        told = said + pack.definition(agent).instructions
        assert 'claim_agent' in told, f'{agent} is never told to claim its slot'


def test_every_agent_grants_only_tools_the_server_has():
    """A tools header that names something the server does not serve.

    The other half of the same seam: a tool renamed in the server leaves a
    grant that grants nothing, and an agent whose header still names it reads
    as able to call it.
    """
    strays = {}
    for name in pack.names():
        unknown = [tool for tool in granted(name) if tool not in TOOLS]
        if unknown:
            strays[name] = unknown
    assert not strays, f'agents granting tools the server does not serve: {strays}'


def test_every_agent_in_the_catalogue_has_a_definition():
    """A prompt sent to an agent that does not exist is a run that cannot start."""
    for agent, _ in CATALOGUE:
        held = pack.definition(agent)
        assert held.instructions.strip(), agent
        assert held.tools, agent
        assert held.max_turns > 0, agent
