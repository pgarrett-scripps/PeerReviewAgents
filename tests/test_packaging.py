"""What an installed copy carries.

0.9 runs the review through the plugin, so a wheel without the plugin files is
a package that cannot review anything: ``peerreview review`` would resolve a
plugin folder that only exists in a checkout. These tests build the real wheel
and look inside it, because the only honest check of package data is the
artifact itself.
"""

from __future__ import annotations

import json
import os
import shutil
import zipfile
from pathlib import Path

import pytest

from peerreviewagents.plugin import starter

ROOT = Path(__file__).resolve().parent.parent
PLUGIN_FILES = (
    'peerreviewagents/plugin/pra/.claude-plugin/plugin.json',
    'peerreviewagents/plugin/pra/.mcp.json',
    'peerreviewagents/plugin/pra/README.md',
    'peerreviewagents/plugin/pra/workflows/review.js',
    'peerreviewagents/plugin/pra/skills/peer-review-manuscript/SKILL.md',
    'peerreviewagents/plugin/pra/skills/check-draft/SKILL.md',
    'peerreviewagents/plugin/pra/skills/follow-up/SKILL.md',
)
AGENTS = ('citation-worker', 'citations', 'core-reviewer', 'editor', 'field',
          'figures-tables', 'methods-reporting', 'numbers', 'recorder', 'stats')


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
    for agent in AGENTS:
        assert f'peerreviewagents/plugin/pra/agents/{agent}.md' in inside
    assert len([name for name in inside
                if name.startswith('peerreviewagents/plugin/pra/agents/')]) == len(AGENTS)


def test_the_installed_plugin_is_the_one_the_starter_looks_for(wheel):
    """The starter resolves the plugin inside the package, not beside the repo."""
    relative = starter.plugin_dir().relative_to(ROOT).as_posix()
    assert relative == 'peerreviewagents/plugin/pra'
    assert f'{relative}/.claude-plugin/plugin.json' in set(wheel.namelist())


def test_the_packaged_mcp_server_is_launched_by_an_installed_entry_point(wheel):
    """Nothing in the shipped plugin may point at a virtualenv in a checkout."""
    config = json.loads(
        wheel.read('peerreviewagents/plugin/pra/.mcp.json').decode('utf-8'))
    server = config['mcpServers']['peerreview']
    assert 'CLAUDE_PROJECT_DIR' not in json.dumps(config)
    assert '.venv' not in json.dumps(config)
    assert server['command'] == '${PEERREVIEW_MCP_COMMAND:-peerreview-plugin-server}'
    metadata = [name for name in wheel.namelist() if name.endswith('entry_points.txt')]
    assert metadata, 'the wheel declares no console scripts'
    entry_points = wheel.read(metadata[0]).decode('utf-8')
    assert 'peerreview-plugin-server = peerreviewagents.plugin.server:main' in entry_points
    assert 'peerreview = peerreviewagents.cli.main:run' in entry_points


def test_the_engine_data_the_review_reads_is_still_packed(wheel):
    inside = set(wheel.namelist())
    assert any(name.endswith('/manifest.json') and '/engine/domains/' in name
               for name in inside)
    assert any(name.endswith('SKILL.md') and '/engine/domains/' in name for name in inside)
