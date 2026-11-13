"""The six specialist lanes, and the boundaries between them.

Each specialist audits one dimension and is told in its own brief which
dimensions belong to the other five, by name, so two of them cannot file the
same finding against the same sentence and leave the editor to reconcile it.
These tests read the briefs as files, check that the ten ownership claims
between them agree, and check the numbers lane's page-image rule in both
halves: the tool is in its slot, and its brief is what forbids reading a value
off a plot.

Nothing here starts Claude Code, Codex or any provider, and no model runs.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from peerreviewagents.plugin import server as srv
from peerreviewagents.plugin import starter

FIXTURE = Path(__file__).parent / 'fixtures' / 'single-column.pdf'
PLUGIN = Path(srv.__file__).resolve().parent / 'pra'
AGENTS = PLUGIN / 'agents'
CODES = {}


@pytest.fixture(autouse=True)
def fresh_state():
    srv.STATE['run'] = None
    srv.STATE['plugin_root'] = None
    CODES.clear()
    yield
    srv.STATE['run'] = None


def open_run(tmp_path, **kwargs):
    opened = srv.ingest(str(FIXTURE), str(tmp_path / 'run'), **kwargs)
    assert 'request_rejected' not in opened, opened
    CODES.update({row['slot']: row['claim_code'] for row in opened.get('claim_codes', [])})
    return opened


def brief(track):
    return (AGENTS / f'{track}.md').read_text(encoding='utf-8')


def handed_away(text):
    """The brief's ``Not yours`` section, which ends at the next heading."""
    found = re.search(r'\n## Not yours\n(.*?)(?=\n## )', text, re.S)
    assert found, 'the brief hands no lane away, so two specialists own the same finding'
    return found.group(1)


def test_every_specialist_track_has_a_brief_that_names_itself():
    for track in srv.SPECIALIST_TRACKS:
        path = AGENTS / f'{track}.md'
        assert path.exists(), f'{track} is a planned slot with no brief to spawn'
        assert re.search(rf'^name:\s*{re.escape(track)}\s*$', brief(track), re.M), \
            f'{path.name} declares a name the server never asks for'


def test_every_brief_hands_the_other_five_lanes_away_by_name():
    """A lane boundary is only real if both sides of it are written down.

    The brief has to name the other five tracks as not its own: an agent told
    only what to audit will audit whatever it notices, and two specialists
    filing the same finding is the duplicate the editor then has to merge.
    """
    for track in srv.SPECIALIST_TRACKS:
        section = handed_away(brief(track))
        for other in srv.SPECIALIST_TRACKS:
            if other == track:
                assert f'`{other}`' not in section, \
                    f'{track} hands its own lane away'
            else:
                assert f'`{other}`' in section, \
                    f'{track} is not told that {other} owns its own lane'


def test_every_brief_says_it_is_auditing_one_dimension_and_reports_the_rest():
    for track in srv.SPECIALIST_TRACKS:
        # The briefs are wrapped prose, so a sentence is matched against the
        # text with its line breaks flattened rather than as it is laid out.
        text = re.sub(r'\s+', ' ', brief(track))
        assert 'auditing one dimension' in text, \
            f'{track} is not told it is auditing rather than reviewing'
        assert 'Seen, not mine' in text, \
            f'{track} is given nowhere to put what belongs to another lane'


def test_the_core_reviewer_is_still_not_told_the_specialists_exist():
    """The blinding the `reviewers-only` arm measures against.

    The core reviewer's brief says what a review is not — a list of surface
    defects — without naming a specialist or a specialist track. Naming them
    would tell a reviewer in `single`, `reviewers-only` or `manuscript-only`
    about auditors that are not running in its arm, and those arms exist to
    measure what the specialists add.
    """
    text = brief('core-reviewer').lower()
    assert 'specialist' not in text and 'spec-' not in text
    for track in srv.SPECIALIST_TRACKS:
        assert f'`{track}`' not in text
    assert 'what a review is not' in text, \
        'the reviewer is not told where its judgement ends'


def test_the_numbers_lane_may_confirm_a_printed_digit_but_not_read_a_plot(tmp_path):
    """The lane's boundary is a plot, not a page, and the brief is what draws it.

    A number that exists only as a position in a picture belongs to
    `figures-tables`. A number printed in a table cell is this lane's, and the
    reader garbles table cells — escaped tabs, a two-column page linearised
    into interleaved rows — so a lane that could only see the extracted text
    would either miss a real cell disagreement or file one it could have
    cleared by looking at the page. `view_page` is therefore in the slot and
    the prohibition is in the brief; `view_supplement` is not, which is the
    difference from the lanes that audit what a panel shows.
    """
    assert 'view_page' in srv.TRACK_TOOLS['numbers']
    assert 'view_supplement' not in srv.TRACK_TOOLS['numbers']
    assert set(srv.TRACK_TOOLS['numbers']) == {*srv.OFFLINE_TOOLS, 'view_page'}

    # The brief has to carry both halves: what the page image is for, and the
    # plot it is never for. A tool granted with no boundary in the prompt is
    # the lane overlap this PR exists to remove.
    text = re.sub(r'\s+', ' ', brief('numbers'))
    assert 'view_page' in text, 'the lane is granted a tool its brief never mentions'
    assert 'garbled' in text and 'table cell' in text, \
        'the brief does not say what the page image is for'
    assert 'never for reading a value off a plot' in text, \
        'the brief does not hand plot reading back to figures-tables'
    assert '`figures-tables`' in handed_away(brief('numbers'))

    open_run(tmp_path)
    taken = srv.claim_agent('spec-numbers', CODES['spec-numbers'])
    assert 'view_page' in taken['allowed_tools']
    assert 'view_supplement' not in taken['allowed_tools']
    assert 'inspect_results' in taken['allowed_tools'], \
        'captions and table values are how the lane compares across the paper'
    refusal = srv.view_supplement(taken['agent_token'], 1)['request_rejected']
    assert 'may not call view_supplement' in refusal


def test_the_workflow_briefs_the_tracks_the_server_plans():
    source = (PLUGIN / 'workflows' / 'review.js').read_text(encoding='utf-8')
    block = re.search(r'const SPECIALIST_BRIEF = \{(.*?)\n\}', source, re.S)
    assert block, 'the workflow has no per-track brief to spawn a specialist with'
    keys = re.findall(r"^\s*'([a-z-]+)':", block.group(1), re.M)
    assert set(keys) == set(srv.SPECIALIST_TRACKS), \
        'a planned track with no brief line is spawned with an empty lane'
    assert 'consistency' not in source


def test_the_draft_check_default_names_tracks_that_exist():
    assert set(starter.DRAFT_CHECK_TRACKS) <= set(srv.SPECIALIST_TRACKS)
    assert 'numbers' in starter.DRAFT_CHECK_TRACKS, \
        "a draft of your own is usually wrong about its own numbers"


def test_no_surface_still_calls_the_numbers_lane_consistency():
    """The rename left no half-renamed path behind.

    ``consistency`` was the lane's name before 0.9.1 and `--track consistency`
    is now refused by the parser, so a command, prompt or default still
    carrying it would fail at the point of use rather than at review time.
    Historical records — ``docs/stage-a-results.md`` and the run the plugin
    README reports — keep the name they ran under and are not scanned.
    """
    root = Path(srv.__file__).resolve().parent.parent.parent
    scanned = [path for path in (PLUGIN.rglob('*.md'))]
    scanned += [root / 'peerreviewagents' / 'plugin' / 'server.py',
                root / 'peerreviewagents' / 'plugin' / 'starter.py',
                root / 'docs' / 'study-arms.md',
                root / 'docs' / 'paper-scaffold.md']
    for path in scanned:
        if path.name == 'README.md' and path.parent == PLUGIN:
            continue  # the 0.9.0 run it reports is a record, not a plan
        text = path.read_text(encoding='utf-8')
        for stale in ('spec-consistency', 'track consistency', "'consistency'",
                      '`consistency`', 'audit_consistency'):
            assert stale not in text, f'{path.name} still names the lane {stale}'
