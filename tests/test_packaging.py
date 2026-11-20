"""What an installed copy carries.

0.9 runs the review through the plugin, so a wheel without the plugin files is
a package that cannot review anything: ``peerreview review`` would resolve a
plugin folder that only exists in a checkout. These tests build the real wheel
and look inside it, because the only honest check of package data is the
artifact itself.
"""

from __future__ import annotations

import os
import shutil
import zipfile
from pathlib import Path

import pytest

from peerreviewagents.plugin import starter

ROOT = Path(__file__).resolve().parent.parent
PLUGIN_FILES = (
    'peerreviewagents/plugin/pra/.claude-plugin/plugin.json',
    'peerreviewagents/plugin/pra/README.md',
    'peerreviewagents/plugin/pra/skills/peer-review-manuscript/SKILL.md',
    'peerreviewagents/plugin/pra/skills/check-draft/SKILL.md',
    'peerreviewagents/plugin/pra/skills/check-fixes/SKILL.md',
)
#: Every slot an agent runs. The recorder is not one: that slot is held by the
#: orchestrator in this process and no model runs it, so it has no definition.
AGENTS = ('citation-worker', 'citations', 'code', 'code-check', 'core-reviewer', 'editor',
          'field', 'figures-tables', 'fix-check', 'methods-reporting', 'numbers', 'reader',
          'stats')


@pytest.fixture(scope='module')
def wheel(tmp_path_factory):
    """The wheel this source tree builds, built away from the checkout.

    The build runs in a copy so it cannot leave ``build/`` or an egg-info
    directory in the repository, and so what it packs is only what is declared
    rather than whatever a previous build left lying around.
    """
    build = pytest.importorskip('setuptools.build_meta')
    work = tmp_path_factory.mktemp('wheel')
    source = work / 'src'
    source.mkdir()
    for name in ('pyproject.toml', 'README.md', 'LICENSE'):
        shutil.copy2(ROOT / name, source / name)
    shutil.copytree(ROOT / 'peerreviewagents', source / 'peerreviewagents')
    here = os.getcwd()
    os.chdir(source)
    try:
        name = build.build_wheel(str(work))
    finally:
        os.chdir(here)
    return zipfile.ZipFile(work / name)


def test_the_wheel_ships_the_plugin_that_runs_the_review(wheel):
    inside = set(wheel.namelist())
    missing = [name for name in PLUGIN_FILES if name not in inside]
    assert not missing, 'the wheel cannot run a review without these: ' + ', '.join(missing)
    # The instruction text ships beside the engine rather than inside the
    # plugin: the orchestrator reads it from the installed package, and the
    # runner it hands a definition to need not be Claude Code.
    for agent in AGENTS:
        assert f'peerreviewagents/agents/{agent}.md' in inside
    packed = [name for name in inside
              if name.startswith('peerreviewagents/agents/') and name.endswith('.md')]
    assert len(packed) == len(AGENTS), packed
    gone = [name for name in inside
            if name.endswith(('.js', '.mcp.json'))
            or '/plugin/pra/agents/' in name]
    assert not gone, 'the wheel still carries the plugin the orchestrator replaced: ' + str(gone)


def test_the_installed_plugin_is_the_one_the_starter_looks_for(wheel):
    """The starter resolves the plugin inside the package, not beside the repo."""
    relative = starter.plugin_dir().relative_to(ROOT).as_posix()
    assert relative == 'peerreviewagents/plugin/pra'
    assert f'{relative}/.claude-plugin/plugin.json' in set(wheel.namelist())


def test_the_shipped_plugin_is_three_skills_that_shell_out_to_the_command(wheel):
    """Nothing in the shipped plugin may point at a virtualenv in a checkout.

    The tools are served in the orchestrator's own process now, over a server
    it starts itself, so there is no ``.mcp.json`` for a session to launch and
    no stdio entry point to launch it with. What ships is the skills, and they
    reach the review the way a person would: ``peerreview review``.
    """
    inside = set(wheel.namelist())
    skills = [name for name in inside if name.endswith('SKILL.md') and '/pra/skills/' in name]
    assert len(skills) == 3, skills
    for name in skills:
        said = wheel.read(name).decode('utf-8')
        assert 'peerreview ' in said, f'{name} names no command to run'
        for stale in ('CLAUDE_PROJECT_DIR', '.venv', 'peerreview-plugin-server',
                      'workflows/review.js', '.mcp.json'):
            assert stale not in said, f'{name} still names {stale}'
    metadata = [name for name in inside if name.endswith('entry_points.txt')]
    assert metadata, 'the wheel declares no console scripts'
    entry_points = wheel.read(metadata[0]).decode('utf-8')
    assert 'peerreview = peerreviewagents.cli.main:run' in entry_points
    assert 'peerreview-plugin-server' not in entry_points, 'nothing launches a server by stdio'


def test_the_engine_data_the_review_reads_is_still_packed(wheel):
    inside = set(wheel.namelist())
    assert any(name.endswith('/manifest.json') and '/engine/domains/' in name
               for name in inside)
    assert any(name.endswith('SKILL.md') and '/engine/domains/' in name for name in inside)
