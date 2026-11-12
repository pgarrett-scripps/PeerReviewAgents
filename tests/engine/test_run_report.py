"""Offline checks for the Markdown and plain-text reports of a plugin run.

The header is the part of the report a reader trusts without reading further,
so these tests pin what it may say. A run whose session has ended knows what it
spent — the stream's own token total, the wall time, the model each agent ran
as, the server tool calls the ledger counted — and the header has to state those
instead of the zeroes a report rendered mid-session would see, and instead of
the protocol's ``model`` field, which for a plugin run is the sentence "per-agent,
pinned in the plugin agent files" rather than a model name.

Nothing here makes a model call: every number comes from JSON the run wrote.
"""

from __future__ import annotations

import json
import re

import pytest

from peerreviewagents.engine.run_report import NOTICE, WIDTH, report_blocks, write_reports
from peerreviewagents.engine.storage import write

# What a table row may be as wide as, which is a terminal rather than a
# paragraph: the agent table carries two full model ids per row.
TABLE_WIDTH = 104

EDIT = {'title': 'Soften the prerequisite claim', 'severity': 'major', 'kind': 'edit',
        'block_id': 'b1', 'placement': 'anchored', 'quote': 'These results confirm the model.',
        'comment': 'The curves are consistent with the model but do not confirm it. ' * 3,
        'replacement': 'These results are consistent with the model.',
        'counterevidence': 'The mutant stays inactive.'}
QUESTION = {'title': 'Report biological replicates', 'severity': 'moderate', 'kind': 'question',
            'block_id': 'b2', 'placement': 'anchored', 'quote': 'n = 3', 'replacement': '',
            'comment': 'How many biological replicates were used?'}

# The slots and the per-slot models of a real two-track run, which is where the
# header's model rows and the agent table have to come from. ``assigned_model``
# is what the run asked that slot to run and ``model`` what the slot reported
# running: two facts, and the page shows them as two.
AGENTS = {
    'core-1': {'role': 'core', 'track': 'core', 'attempt': 1, 'tool_calls': 8,
               'assigned_model': 'claude-sonnet-5-5', 'model': 'claude-sonnet-5-5'},
    'core-2': {'role': 'core', 'track': 'core', 'attempt': 1, 'tool_calls': 9,
               'assigned_model': 'claude-sonnet-5-5', 'model': 'claude-sonnet-5-5'},
    'spec-stats': {'role': 'specialist', 'track': 'stats', 'attempt': 1, 'tool_calls': 12,
                   'assigned_model': 'claude-sonnet-5-5', 'model': 'claude-sonnet-5-5'},
    'editor': {'role': 'editor', 'track': 'editor', 'attempt': 1, 'tool_calls': 3,
               'assigned_model': 'claude-opus-5-5', 'model': 'claude-opus-5-5'},
    'recorder': {'role': 'recorder', 'track': 'recorder', 'attempt': 1, 'tool_calls': 2,
                 'assigned_model': 'claude-haiku-5-5', 'model': 'claude-haiku-5-5'},
}

STREAM = [
    {'label': 'core-1', 'state': 'done', 'attempt': 1, 'agentType': 'pra:core-reviewer',
     'model': 'claude-sonnet-5-5', 'tokens': 61794, 'toolCalls': 17, 'durationMs': 95647},
    {'label': 'core-2', 'state': 'done', 'attempt': 1, 'model': 'claude-sonnet-5-5',
     'tokens': 58206, 'toolCalls': 19},
    {'label': 'spec-stats', 'state': 'done', 'attempt': 1, 'model': 'claude-sonnet-5-5',
     'tokens': 40000, 'toolCalls': 21},
    {'label': 'editor', 'state': 'done', 'attempt': 1, 'model': 'claude-opus-5-5',
     'tokens': 20000, 'toolCalls': 7},
    {'label': 'recorder', 'state': 'done', 'attempt': 1, 'model': 'claude-haiku-5-5',
     'tokens': 4000, 'toolCalls': 2},
]


@pytest.fixture
def run(tmp_path):
    """A finished two-track run: a manuscript folder, a review folder, both reports' input."""
    write(tmp_path / 'manuscript' / 'paper.json',
          {'id': 'paper-1', 'title': 'Dimerization regulates enzyme activity',
           'source_url': 'https://example.org/preprint'})
    review = tmp_path / 'review'
    write(review / 'protocol.json', {
        'provider': 'claude-code', 'model': 'per-agent, pinned in the plugin agent files',
        'effort': 'per-agent, pinned in the plugin agent files', 'arm': 'full-two-track',
        'reviewers': 2, 'paper_id': 'paper-1', 'literature_cutoff': '2024-04-07',
        'agents': AGENTS,
        'plan': {'arm': 'full-two-track', 'reviewers': 2, 'specialists': ['stats'] * 6,
                 'editor': True, 'revision': False},
    })
    write(review / 'manifest.json', {'paper_id': 'paper-1'})
    write(review / 'progress.json', {'status': 'complete'})
    write(review / 'runtime.json', {
        'status': 'complete', 'blocked_reason': None, 'wall_seconds': 247.0,
        'tokens_total': 184000, 'claude_code_version': '2.1.322', 'agents': STREAM,
        'rate_limits': [], 'result': {'num_turns': 41, 'session_id': 'abc'},
    })
    write(review / 'editor-package.json', {
        'summary': 'Two findings remain.', 'findings': [EDIT, QUESTION],
        'limitations': ['Supplementary data were unavailable.']})
    for index in (1, 2):
        write(review / f'reviewer-{index}' / 'editor-package.json',
              {'summary': f'Reviewer {index} summary.', 'findings': [QUESTION],
               'limitations': ['No raw data.']})
    usage = review / 'usage' / 'core-1'
    write(usage / 'core-1-1.started.json', {'label': 'core-1'})
    (usage / 'usage.jsonl').write_text(
        json.dumps({'label': 'core-1', 'request_sha256': 'r1', 'attempt': 1,
                    'observed_tokens': 61794, 'usage_unknown': False}) + '\n',
        encoding='utf-8')
    return review


def reports(run, **options):
    paths = write_reports(run, **options)
    # The report is written as UTF-8 and the header uses an em dash for a value
    # the run never recorded; read in the platform encoding it comes back
    # mangled, which is how this read failed on Windows and nowhere else.
    return (paths['markdown'].read_text(encoding='utf-8'),
            paths['text'].read_text(encoding='utf-8'))


def rows(markdown):
    """The header table as a dict, so a test can name a row without its spacing."""
    found = {}
    for line in markdown.splitlines():
        match = re.fullmatch(r'\| ([^|]+?) \| (.*?) \|', line)
        if match and match.group(1) not in ('Detail', '---'):
            found.setdefault(match.group(1), match.group(2))
    return found


# -- the header -------------------------------------------------------------


def test_the_header_states_what_the_run_really_spent(run):
    """The 0.9 header read 0 calls and 0 tokens because it rendered mid-session."""
    markdown, _ = reports(run)
    header = rows(markdown)

    assert markdown.startswith('# Review: Dimerization regulates enzyme activity')
    assert header['Tokens'] == '184,000 (session stream)'
    assert header['Wall time'] == '4 min 7 s'
    assert header['Claude Code'] == '2.1.322'
    assert header['Agents'] == '5'
    assert header['Server tool calls'] == '34'      # the ledger's own count: 8+9+12+3+2
    assert header['Session tool calls'] == '66'     # what the stream saw: 17+19+21+7+2
    assert header['Workflow'] == ('full-two-track: 2 core reviewer(s), 6 specialist track(s), '
                                  'an editor')
    assert header['Models asked for'] == 'claude-haiku-5-5, claude-opus-5-5, claude-sonnet-5-5'
    assert header['Models that ran'] == 'claude-haiku-5-5, claude-opus-5-5, claude-sonnet-5-5'
    assert header['Literature cutoff'] == '2024-04-07'
    assert header['Paper ID'] == 'paper-1'


def test_the_header_never_prints_the_protocols_model_sentence(run):
    """``protocol.model`` is a sentence about where models are pinned, not a model."""
    markdown, text = reports(run)

    assert 'pinned in the plugin agent files' not in markdown + text
    assert 'Model' not in rows(markdown)      # the header row is `Models`, from the agents
    assert 'Model calls' not in markdown + text
    assert 'Observed tokens' not in markdown + text
    for line in markdown.splitlines():
        assert not re.fullmatch(r'\| (Tokens|Server tool calls|Agents) \| 0 \|', line), line


def test_the_agent_table_names_the_model_each_agent_ran_as(run):
    markdown, text = reports(run)

    assert '## Agents' in markdown
    assert ('| Agent | Model asked | Model ran | Server calls | Session calls | Tokens | State |'
            in markdown)
    assert ('| core-1 | claude-sonnet-5-5 | claude-sonnet-5-5 | 8 | 17 | 61,794 | done |'
            in markdown)
    assert '| editor | claude-opus-5-5 | claude-opus-5-5 | 3 | 7 | 20,000 | done |' in markdown
    assert 'claude-haiku-5-5' in text


def test_the_agent_table_shows_an_assigned_model_that_did_not_run(run):
    """A slot that ran another family is the record disagreeing with the plan.

    The page is where that is read, so the plan and the observation sit in
    their own columns and neither is ever printed as the other. A slot whose
    model was never reported says so rather than borrowing the plan's answer.
    """
    protocol = json.loads((run / 'protocol.json').read_text(encoding='utf-8'))
    protocol['agents']['core-1'].pop('model')      # core-1 never reported a model
    write(run / 'protocol.json', protocol)
    runtime = json.loads((run / 'runtime.json').read_text(encoding='utf-8'))
    runtime['agents'] = [{'label': 'editor', 'attempt': 1, 'tokens': 20000, 'toolCalls': 7,
                          'model': 'claude-sonnet-5-5', 'state': 'done'},
                         {'label': 'core-1', 'attempt': 1, 'tokens': 61794, 'toolCalls': 17,
                          'state': 'done'}]
    write(run / 'runtime.json', runtime)
    markdown, _ = reports(run)

    assert '| editor | claude-opus-5-5 | claude-sonnet-5-5 | 3 | 7 | 20,000 | done |' in markdown
    assert '| core-1 | claude-sonnet-5-5 | not recorded | 8 | 17 | 61,794 | done |' in markdown
    assert rows(markdown)['Models asked for'] == ('claude-haiku-5-5, claude-opus-5-5, '
                                                  'claude-sonnet-5-5')
    assert rows(markdown)['Models that ran'] == 'claude-haiku-5-5, claude-sonnet-5-5'


def test_a_slot_nothing_observed_is_called_unverified_in_both_reports(run):
    """A plan nobody checked is not the same as a plan that held.

    The run is still publishable — nothing was seen to disagree with the plan,
    and a missing observation is the ordinary state of a slot whose session
    reported no model — but a page showing `Model asked` with nothing beside it
    would read as agreement. Both reports say which slots those are.
    """
    protocol = json.loads((run / 'protocol.json').read_text(encoding='utf-8'))
    protocol['agents']['core-1'].pop('model')
    protocol['agents']['core-2'].pop('model')
    write(run / 'protocol.json', protocol)
    runtime = json.loads((run / 'runtime.json').read_text(encoding='utf-8'))
    runtime['agents'] = [row for row in runtime['agents']
                         if row['label'] not in ('core-1', 'core-2')]
    write(run / 'runtime.json', runtime)
    markdown, text = reports(run)

    line = ('the model that ran was not recorded for 2 slots (core-1, core-2), '
            'so the plan for them is unverified')
    assert '**Models unverified:** ' + line.capitalize() + '.' in markdown
    assert 'Models unverified' in text and 'core-1, core-2' in text
    assert '| core-1 | claude-sonnet-5-5 | not recorded |' in markdown


def test_a_report_whose_slots_all_reported_a_model_says_nothing_about_it(run):
    markdown, text = reports(run)
    assert 'Models unverified' not in markdown and 'Models unverified' not in text


def test_a_retry_is_labelled_as_one_and_an_unplanned_agent_is_still_shown(run):
    """A slot the protocol never recorded is a discrepancy worth seeing, not a tidy-up."""
    runtime = json.loads((run / 'runtime.json').read_text(encoding='utf-8'))
    runtime['agents'] = [{'label': 'core-1#2', 'attempt': 2, 'tokens': 1000, 'toolCalls': 3,
                          'model': 'claude-sonnet-5-5', 'state': 'done'},
                         {'label': 'ghost-1', 'tokens': 500, 'toolCalls': 1, 'state': 'failed'}]
    write(run / 'runtime.json', runtime)
    markdown, _ = reports(run)

    assert ('| core-1#2 | claude-sonnet-5-5 | claude-sonnet-5-5 | 8 | 3 | 1,000 | done |'
            in markdown)
    assert '| ghost-1 | not recorded | not recorded | — | 1 | 500 | failed |' in markdown
    assert rows(markdown)['Agents'] == '6'


def test_tokens_fall_back_to_the_usage_receipts_when_the_stream_wrote_none(run):
    """A session killed before the starter wrote its totals still has the receipts."""
    runtime = json.loads((run / 'runtime.json').read_text(encoding='utf-8'))
    del runtime['tokens_total']
    write(run / 'runtime.json', runtime)

    assert rows(reports(run)[0])['Tokens'] == '61,794 (usage receipts)'


def test_usage_the_run_cannot_account_for_is_said_so_in_the_header(run):
    """A started attempt with no receipt makes the token total a lower bound."""
    write(run / 'usage' / 'core-2' / 'core-2-1.started.json', {'label': 'core-2'})
    runtime = json.loads((run / 'runtime.json').read_text(encoding='utf-8'))
    del runtime['tokens_total']
    write(run / 'runtime.json', runtime)

    assert rows(reports(run)[0])['Tokens'].endswith('; some usage is unknown')


def test_a_blocked_run_says_why_and_claims_no_review(run):
    (run / 'editor-package.json').unlink()
    for index in (1, 2):
        (run / f'reviewer-{index}' / 'editor-package.json').unlink()
    write(run / 'progress.json', {'status': 'blocked', 'error': 'the run passed its token ceiling'})
    runtime = json.loads((run / 'runtime.json').read_text(encoding='utf-8'))
    runtime.update(status='blocked', blocked_reason='the run passed its token ceiling')
    write(run / 'runtime.json', runtime)
    markdown, text = reports(run)

    assert rows(markdown)['Run status'] == 'blocked: the run passed its token ceiling'
    assert '## Review\n\nNo final review was produced.' in markdown
    assert 'Run error: the run passed its token ceiling' in text
    assert NOTICE not in markdown


def test_a_rate_limit_that_stopped_the_run_is_on_the_page(run):
    runtime = json.loads((run / 'runtime.json').read_text(encoding='utf-8'))
    runtime['rate_limits'] = [{'reason': 'the seven_day rate-limit window is at 99.4%'}]
    write(run / 'runtime.json', runtime)

    assert '**Rate limit:** the seven_day rate-limit window is at 99.4%' in reports(run)[0]


# -- the review -------------------------------------------------------------


def test_the_final_review_comes_before_each_independent_reviewer(run):
    markdown, text = reports(run)

    positions = [markdown.index(f'## {name}') for name in
                 ('Final review (editor)', 'Reviewer 1 (independent)', 'Reviewer 2 (independent)')]
    assert positions == sorted(positions)
    for expected in ('> These results confirm the model.',
                     '> These results are consistent with the model.',
                     '**Strongest counterpoint:** The mutant stays inactive.',
                     '- Supplementary data were unavailable.', NOTICE):
        assert expected in markdown
    assert 'Severity: major | Type: edit | Location: block b1' in text


def test_the_text_report_is_plain_text_within_the_width(run):
    """Prose keeps to the width; a table keeps to its data.

    ``WIDTH`` is a prose measure. A row of the agent table carries two full
    model ids — the one the slot was asked to run and the one it ran — at
    seventeen characters each, so it is wider than a paragraph may be, and
    shortening a model id to fit is the one thing those two columns exist to
    prevent. A table row is therefore allowed a terminal's width instead.
    """
    _, text = reports(run)
    lines = text.splitlines()
    tabular = set()
    for index, line in enumerate(lines):
        # A table's column rule: dashes in groups. A heading's rule has no gap.
        if not (line.strip() and '  ' in line and re.fullmatch(r'[- ]+', line)):
            continue
        start, stop = index, index + 1
        while start > 0 and lines[start - 1].strip():
            start -= 1
        while stop < len(lines) and lines[stop].strip():
            stop += 1
        tabular.update(range(start, stop))

    for index, line in enumerate(lines):
        assert not (line.startswith(('#', '>', '| ')) or '**' in line), line
        if str(run) not in line:
            assert len(line) <= (TABLE_WIDTH if index in tabular else WIDTH), line


# -- the inputs -------------------------------------------------------------


def test_the_manuscript_folder_is_found_beside_the_review_folder(run):
    """``peerreview report <run>/review`` is given no workspace and must find one."""
    blocks = report_blocks(run)
    fields = dict(next(content for kind, *content in blocks if kind == 'fields')[0])

    assert fields['Paper'] == 'Dimerization regulates enzyme activity'
    assert fields['Source'] == 'https://example.org/preprint'


def test_a_mismatched_workspace_or_a_missing_protocol_fails_before_writing(run, tmp_path):
    other = tmp_path / 'other'
    write(other / 'paper.json', {'id': 'paper-2', 'title': 'Another paper'})

    with pytest.raises(ValueError, match='does not match'):
        write_reports(run, workspace=other)
    with pytest.raises(ValueError, match='protocol.json'):
        write_reports(tmp_path / 'manuscript')
    assert not (run / 'REVIEW.md').exists()


def test_the_reports_can_be_written_somewhere_else_without_touching_the_run(run, tmp_path):
    target = tmp_path / 'reports'
    paths = write_reports(run, output_dir=target)

    assert paths['markdown'] == target / 'REVIEW.md'
    assert (target / 'REVIEW.md').is_file() and (target / 'REVIEW.txt').is_file()
    assert not (run / 'REVIEW.md').exists()
