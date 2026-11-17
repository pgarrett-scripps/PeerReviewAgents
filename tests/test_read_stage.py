"""The Read stage: a model repairs the draft, and the seal writes the manuscript.

Every test here runs offline and no model reads anything: the reader's tools are
called directly, which is the whole point of putting them in the server. What
they hold in place is the order of the stage — nothing claims a slot before the
manuscript exists, a repair is taken only from a page the reader has opened, and
the seal is refused while a single equation, table or figure is unaccounted for
— and what the sealed base then says about where each line came from.

The drafts are the fixture PDF's own pieces with a few more put in: the fixture
is prose, and the converter's failures this stage exists for are equations,
tables and figures. A piece added here has the shape the converter writes, so
the reader sees what it would see on a paper that had them.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from manuscripts import build_base, seal_converted

from peerreviewagents import names
from peerreviewagents.engine import paper_base
from peerreviewagents.engine import reader as read_stage
from peerreviewagents.engine.ingest import DRAFT_FILE, convert_paper
from peerreviewagents.engine.storage import digest, write
from peerreviewagents.plugin import models as model_plan
from peerreviewagents.plugin import server as srv

FIXTURE = Path(__file__).parent / 'fixtures' / 'single-column.pdf'

PLUGIN = Path(srv.__file__).parent / 'pra'
READER_FILE = PLUGIN / 'agents' / 'reader.md'
WORKFLOW = PLUGIN / 'workflows' / 'review.js'

#: The converter's reading of equation (1): a brace it never closed, with its
#: own confidence at 1.0 beside it, which is the case this stage exists for.
MANGLED = 'E = m c^{2'

#: Slot claim codes from the last ``ingest``.
CODES = {}


@pytest.fixture(autouse=True)
def fresh_state():
    """A run with a Read stage, as every run of a PDF has, and no state left over."""
    srv.STATE['run'] = None
    CODES.clear()
    yield
    srv.STATE['run'] = None


def piece(kind, text='', *, page=1, section='3 Results', **extra):
    """One draft piece in the shape ``convert_paper`` writes them."""
    return {'text': text, 'section': section, 'kind': kind, 'page': page, 'bbox': None,
            'citations': [], 'table': None, 'source': 'text', 'page_mapping': 'converter',
            'document': paper_base.MAIN_DOCUMENT, **extra}


#: What the converter made of a page holding two equations, a figure and a
#: table: equation (1) mangled, equation (2) split in half across two pieces,
#: the figure as a caption with nothing behind it, and a table whose caption it
#: found and whose cells it did not.
CONVERTED = [
    piece('equation', MANGLED, latex=MANGLED, number='(1)', confidence=1.0),
    piece('equation', 'a^{2} + b^{2}', latex='a^{2} + b^{2}', number='', confidence=0.4),
    piece('equation', '= c^{2}', latex='= c^{2}', number='(2)', confidence=0.4),
    piece('caption', 'Figure 1: The bench, the instrument and the widgets.'),
    piece('caption', 'Table 1: Readings of the blue widgets, by day.'),
]

#: The four pieces of that page the stage has to account for: three equations
#: and the figure. ``Table 1``'s caption is a caption — the table itself is
#: what the converter dropped, and the reader adds it.
TO_ACCOUNT_FOR = 4

DESCRIPTION = ('A photograph of the bench with the instrument clamped at the left and three '
               'widgets in a row; the scale divisions are 0.5 mm apart.')


def drafted(tmp_path, pieces=CONVERTED, supplement=None, **kwargs):
    """A run over a draft holding the fixture's pieces and the ones given.

    ``ingest`` picks up a conversion the folder already holds rather than
    converting twice, which is how a run killed during the Read stage is
    resumed — and how a test hands the server a draft of its own.
    """
    root = tmp_path / 'run'
    workspace = root / names.MANUSCRIPT_DIRECTORY
    draft = convert_paper(str(FIXTURE), workspace)
    main = draft['documents'][paper_base.MAIN_DOCUMENT]
    at = next(index for index, held in enumerate(main)
              if str(held.get('section')) == 'References')
    draft['documents'][paper_base.MAIN_DOCUMENT] = [*main[:at], *pieces, *main[at:]]
    if supplement:
        draft['documents'][supplement] = [
            {**piece('title', 'Supplementary material', section='Supplement S1'),
             'document': supplement},
            {**piece('equation', 'x = y', latex='x = y', number='(S1)', section='Supplement S1'),
             'document': supplement}]
    write(workspace / DRAFT_FILE, draft)
    kwargs.setdefault('reviewers', 1)
    kwargs.setdefault('arm', 'single')
    opened = srv.ingest(str(FIXTURE), str(root), **kwargs)
    assert 'request_rejected' not in opened, opened
    CODES.update({row['slot']: row['claim_code'] for row in opened.get('claim_codes', [])})
    CODES['recorder_key'] = opened.get('recorder_key', '')
    return opened


#: A table the converter emitted as one paragraph of run-together cells, which
#: is what it did to two tables of the first paper a reader was run on. Nothing
#: in the listing calls it a table, so nothing asks for it to be repaired.
GARBLED_TABLE = 'Hour Current (mA) 1 4.5 2 9.0'

#: A paragraph holding the debris of the displayed equation beside it — the
#: converter emitted "$R =$ log2(1 + SNR) (9)" as a paragraph and left the next
#: equation's symbols in the prose that followed.
DEBRIS_PARAGRAPH = ('At high signal-to-noise the logarithm is well approximated by a linear '
                    'function of the bandwidth. We have SNR ln 2 In that regime the gain is '
                    'linear in the bandwidth and nothing else.')

#: A bar chart's axis text, read as a paragraph above its caption: a figure's
#: content that reached a sealed manuscript as a line of prose, which the
#: reader's own note named and no tool could reach.
AXIS_TEXT = '120 100 80 60 40 20 0 Control 10 µl/ml 20 µl/ml Concentration(µl/ml)'

#: A paragraph of the authors' own prose, on the page the debris is on. It is
#: longer than any debris a converter leaves, and that length is how `absorb`
#: tells the paper from the leavings.
AUTHORS_PROSE = ('The readings were taken at the same place on the bench each morning, with the '
                 'instrument clamped and the widgets left in the order they were received, so '
                 'that the only difference between one day and the next was the widget under '
                 'the probe rather than the position of the stage.')

#: What the converter made of the same page when it strewed the equations
#: instead of only mangling them: equation (2) in two pieces with a heading of
#: its debris on each side, and the figure's axis text above its caption. Every
#: debris piece here is prose by kind, so nothing asks for it to be accounted
#: for and nothing could take it out — the case `absorb` is for. The page-two
#: heading is there to be refused: debris is absorbed off its own page.
STREWN = [
    piece('equation', MANGLED, latex=MANGLED, number='(1)', confidence=1.0),
    piece('heading', 'with $\\{$'),
    piece('equation', 'a^{2} + b^{2}', latex='a^{2} + b^{2}', number='', confidence=0.4),
    piece('equation', '= c^{2}', latex='= c^{2}', number='(2)', confidence=0.4),
    piece('heading', '$qn,s,d$ else'),
    piece('paragraph', AXIS_TEXT),
    piece('caption', 'Figure 1: The bench, the instrument and the widgets.'),
    piece('paragraph', DEBRIS_PARAGRAPH),
    piece('caption', 'Table 1: Readings of the blue widgets, by day.'),
    piece('paragraph', AUTHORS_PROSE),
    piece('heading', '$|A|2 d=1$', page=2),
]


def reading(tmp_path, **kwargs):
    """``(ingest's answer, the reader's token)``: the stage, claimed and ready."""
    opened = drafted(tmp_path, **kwargs)
    taken = srv.claim_agent('reader', CODES['reader'])
    assert 'agent_token' in taken, taken
    return opened, taken['agent_token']


def listed(token, document=''):
    """``{piece id: row}`` of one document of the draft, as the reader sees it."""
    answer = srv.read_draft(token, document=document)
    assert 'request_rejected' not in answer, answer
    return {row['piece']: row for row in answer['pieces']}


def ids_of(token):
    """The ids of the pieces this stage works on, by what each one is."""
    rows = listed(token)
    equations = [key for key, row in rows.items() if row['kind'] == 'equation']
    figures = [key for key, row in rows.items() if row.get('account') == 'figure']
    tables = [key for key, row in rows.items()
              if str(row['text']).startswith('Caption: Table 1')]
    return {'mangled': equations[0], 'split': equations[1:3],
            'figure': figures[0], 'table_caption': tables[0]}


def strewn(tmp_path, **kwargs):
    """The stage over a draft whose displayed pieces came back strewn about."""
    opened, token = reading(tmp_path, pieces=STREWN, **kwargs)
    rows = listed(token)

    def named(text):
        return next(key for key, row in rows.items() if text in str(row['text']))

    return token, {**ids_of(token), 'before': named('with $\\{$'),
                   'after': named('$qn,s,d$ else'), 'axis': named('Concentration'),
                   'prose': named('SNR ln 2'), 'next_page': named('|A|2 d=1'),
                   'long': named('readings were taken')}


def garbled(tmp_path, **kwargs):
    """The same stage over a draft that also holds two pieces the converter mislabelled."""
    return reading(tmp_path, pieces=[*CONVERTED, piece('paragraph', GARBLED_TABLE),
                                     piece('paragraph', DEBRIS_PARAGRAPH)], **kwargs)


def paragraph_ids(token):
    """The ids of the two paragraphs that are not paragraphs."""
    rows = listed(token)
    return {'garbled': next(key for key, row in rows.items()
                            if 'Hour Current' in str(row['text'])),
            'debris': next(key for key, row in rows.items() if 'SNR ln 2' in str(row['text']))}


def account_for_everything(token, marks):
    """Do the stage's work: view both pages, repair what is wrong, add what is missing."""
    for page in (1, 2):
        assert 'request_rejected' not in srv.view_page(token, page)
    assert 'request_rejected' not in srv.replace_equation(
        token, marks['mangled'], 1, 'E = m c^{2}', number='(1)')
    assert 'request_rejected' not in srv.merge_equations(
        token, marks['split'], 1, 'a^{2} + b^{2} = c^{2}', number='(2)')
    assert 'request_rejected' not in srv.describe_figure(
        token, marks['figure'], 1, DESCRIPTION)
    added = srv.add_piece(token, marks['table_caption'], 'table', 1,
                          rows=[['Day', 'Reading (mm)'], ['1', '12.5'], ['2', '12.5']])
    assert 'request_rejected' not in added, added
    return added


def before(rows, row):
    """The map row above this one: the piece written on the lines just above it."""
    return rows[rows.index(row) - 1]


def base_of(run):
    """``(paper.md, map.json's rows, the manifest)`` of the sealed manuscript."""
    folder = run.workspace
    text = (folder / paper_base.MAIN_DOCUMENT).read_text(encoding='utf-8')
    rows = json.loads((folder / 'map.json').read_text(encoding='utf-8'))
    manifest = json.loads((folder / 'manifest.json').read_text(encoding='utf-8'))
    return text, rows, manifest


def test_no_slot_but_the_reader_can_claim_before_the_manuscript_is_sealed(tmp_path):
    """A draft is not a manuscript, and a reviewer is not handed one.

    The reviewers, the specialists, the citation workers and the editor all
    quote line numbers, and before the seal there are none. So the gate is on
    the claim rather than on each tool: a slot that cannot be served the
    manuscript is not opened at all.
    """
    opened = drafted(tmp_path)
    assert opened['sealed'] is False
    assert opened['statistics'] is None
    assert opened['sections'] == [] and opened['documents'] == []
    assert opened['draft']['pages'] == 2
    assert opened['draft']['documents'] == [paper_base.MAIN_DOCUMENT]
    assert len(opened['draft']['to_account_for']['pieces']) == TO_ACCOUNT_FOR
    assert opened['draft']['to_account_for']['pages'] == [1, 2]
    for slot in ('core-1', 'recorder'):
        refused = srv.claim_agent(slot, CODES[slot])
        assert 'still a draft' in refused['request_rejected'], slot
        assert 'Read stage' in refused['request_rejected']
    # The reader is the one slot that may: it is the stage that writes the
    # thing the others are waiting for.
    taken = srv.claim_agent('reader', CODES['reader'])
    assert taken['role'] == 'reader' and taken['slot'] == 'reader'
    assert 'read_draft' in taken['allowed_tools'] and 'read_paper' not in taken['allowed_tools']
    assert taken['statistics'] is None and taken['sections'] == []
    assert taken['paper_id'] == opened['paper_id']
    assert taken['draft']['pages'] == 2
    assert len(taken['draft']['to_account_for']['pieces']) == TO_ACCOUNT_FOR
    assert 'never correct' in taken['reminder']
    assert 'seal_manuscript' in taken['read_first']


def test_the_seal_is_refused_until_every_piece_and_page_is_accounted_for(tmp_path):
    """Refused by name, and when nothing is left it is the run's one writer."""
    opened, token = reading(tmp_path)
    marks = ids_of(token)
    refused = srv.seal_manuscript(token)
    assert 'request_rejected' in refused
    assert marks['mangled'] in refused['request_rejected']
    assert 'have not been viewed' in refused['request_rejected']
    assert refused['outstanding']['pages'] == [1, 2]
    assert len(refused['outstanding']['pieces']) == TO_ACCOUNT_FOR
    assert not (srv.current().workspace / paper_base.MAIN_DOCUMENT).exists()

    account_for_everything(token, marks)
    sealed = srv.seal_manuscript(token)
    assert 'request_rejected' not in sealed, sealed
    assert sealed['sealed'] is True
    assert sealed['paper_id'] == opened['paper_id']
    assert sealed['ai_read']['read'] is True
    assert sealed['ai_read']['pages_viewed'] == 2
    assert sealed['ai_read']['reader_model'] == 'claude-sonnet-5-5'
    # One call of each, counted once each: `added` is `add_piece` and nothing
    # else. `describe_figure` settled the description it wrote as well as the
    # figure it read, so a run that described nine figures and added nothing
    # reported nine added.
    assert sealed['ai_read']['actions'] == {'repaired': 1, 'merged': 1,
                                            'described': 1, 'added': 1}
    # What ingest used to hand back and could not while the base did not exist,
    # handed back here instead, so the workflow reads the manuscript's numbers
    # in one place and nothing downstream asks twice.
    assert sealed['statistics']['lines'][paper_base.MAIN_DOCUMENT] > 0
    assert sealed['statistics']['pieces'] > len(CONVERTED)
    assert sealed['sections'] and sealed['documents']
    assert 'citations' in sealed and 'citation_ledger' in sealed
    # Sealed once. A second call is not a second manuscript.
    again = srv.seal_manuscript(token)
    assert 'already sealed' in again['request_rejected']


def test_a_repair_is_refused_from_a_reader_that_has_not_viewed_the_page(tmp_path):
    """The gate is the page image: a transcription nobody read is not one."""
    opened, token = reading(tmp_path)
    marks = ids_of(token)
    for call in (lambda: srv.replace_equation(token, marks['mangled'], 1, 'E = m c^{2}'),
                 lambda: srv.describe_figure(token, marks['figure'], 1, DESCRIPTION),
                 lambda: srv.confirm_piece(token, marks['split'][0], 1),
                 lambda: srv.add_piece(token, marks['table_caption'], 'table', 1,
                                       rows=[['Day', 'Reading']])):
        refused = call()
        assert 'not viewed page 1' in refused['request_rejected'], refused
    left = srv.read_draft(token)['outstanding']['pieces']
    assert len(left) == TO_ACCOUNT_FOR, 'and nothing was settled by a refusal'

    # Viewing it is what the refusal asked for, and then the same call lands.
    assert 'request_rejected' not in srv.view_page(token, 1)
    assert 'request_rejected' not in srv.replace_equation(
        token, marks['mangled'], 1, 'E = m c^{2}', number='(1)')
    # A page the PDF does not have is refused before anything is rendered.
    assert 'Unknown page' in srv.view_page(token, 9)['request_rejected']
    # And a repair read off page 2 is still refused while only page 1 is open.
    assert 'not viewed page 2' in srv.confirm_piece(
        token, marks['split'][0], 2)['request_rejected']


def test_a_sealed_row_says_where_its_line_came_from_and_keeps_what_it_replaced(tmp_path):
    """Provenance, piece by piece: the three sources and the converter's own text."""
    opened, token = reading(tmp_path)
    marks = ids_of(token)
    account_for_everything(token, marks)
    assert 'request_rejected' not in srv.seal_manuscript(token)
    run = srv.current()
    text, rows, manifest = base_of(run)

    repaired = [row for row in rows if row['source'] == 'ai-reconstructed']
    assert len(repaired) == 2, 'the mangled equation and the merged one'
    assert all(row['kind'] == 'equation' for row in repaired)
    assert all(row['original']['latex'] for row in repaired)
    assert MANGLED in [row['original']['latex'] for row in repaired]
    # The repair is on the line; the converter's reading is only on the row.
    assert '$$ E = m c^{2} \\tag{(1)} $$' in text.splitlines()
    assert '$$ ' + MANGLED + ' \\tag{(1)} $$' not in text.splitlines()

    described = [row for row in rows if row['kind'] == 'figure-description']
    assert len(described) == 1 and described[0]['source'] == 'ai'
    assert described[0]['original'] == {}, 'a piece the converter never wrote'
    assert 'Figure description: A photograph of the bench' in text
    # The caption above it is still the authors' words, and still says so.
    caption = before(rows, described[0])
    assert caption['source'] == 'text' and 'original' not in caption
    assert caption['kind'] == 'caption'

    added = [row for row in rows if row['kind'] == 'table']
    assert len(added) == 1 and added[0]['source'] == 'ai'
    assert '| Day | Reading (mm) |' in text

    assert manifest['pieces_per_source']['ai-reconstructed'] == 2
    assert manifest['pieces_per_source']['ai'] == 2, 'the description and the table'
    assert manifest['pieces_per_source']['text'] == len(rows) - 4
    assert manifest['ai_read']['read'] is True
    assert manifest['ai_read']['reader_model'] == 'claude-sonnet-5-5'
    assert manifest['ai_read']['pages'] == 2
    assert 'claude-sonnet-5-5' in manifest['known_gaps']['figures']
    assert 'page image' in manifest['known_gaps']['equation_confidence']
    # The draft is gone: the folder holds the manuscript and nothing that could
    # be mistaken for it.
    assert not (run.workspace / DRAFT_FILE).exists()


def test_an_equation_the_converter_split_becomes_one_line(tmp_path):
    """Two pieces that are one equation, merged, with both halves kept on the row."""
    opened, token = reading(tmp_path)
    marks = ids_of(token)
    assert 'request_rejected' not in srv.view_page(token, 1)
    merged = srv.merge_equations(token, marks['split'], 1, 'a^{2} + b^{2} = c^{2}', number='(2)')
    assert 'request_rejected' not in merged, merged
    assert merged['piece'] == marks['split'][0]
    assert merged['removed'] == [marks['split'][1]]
    assert merged['replaced']['merged'][0]['latex'] == '= c^{2}'
    # The dropped half is gone from the draft, so it is not waiting to be
    # accounted for and will not be a line of its own.
    rows = listed(token)
    assert marks['split'][1] not in rows
    assert rows[marks['split'][0]]['settled'] == 'merged'

    assert 'request_rejected' not in srv.replace_equation(
        token, marks['mangled'], 1, 'E = m c^{2}', number='(1)')
    assert 'request_rejected' not in srv.describe_figure(token, marks['figure'], 1, DESCRIPTION)
    assert 'request_rejected' not in srv.add_piece(
        token, marks['table_caption'], 'table', 1, rows=[['Day', 'Reading'], ['1', '12.5']])
    assert 'request_rejected' not in srv.view_page(token, 2)
    assert 'request_rejected' not in srv.seal_manuscript(token)

    text, rows, _ = base_of(srv.current())
    whole = [row for row in rows if row.get('original', {}).get('merged')]
    assert len(whole) == 1
    start, end = whole[0]['lines']
    assert start == end, 'one equation is one line'
    lines = text.splitlines()
    assert lines[start - 1] == '$$ a^{2} + b^{2} = c^{2} \\tag{(2)} $$'
    assert '$$ = c^{2} \\tag{(2)} $$' not in lines, 'and not a line of its own'
    assert len([line for line in lines if line.startswith('$$')]) == 2


def test_an_equations_debris_is_folded_into_the_equation(tmp_path):
    """The headings the converter made of an equation's edges go into it.

    Three of them came off one real paper — ``with {``, ``qn,s,d else`` and
    ``|A|2 d=1`` — labelled headings, so nothing asked for them to be
    accounted for and no tool could reach them: a converter piece is the
    authors' text and this stage deletes none on its own. They reached the
    sealed manuscript as `####` lines. They are not prose and they are not
    pieces: they are the equation beside them, so they are merged into it.
    """
    token, marks = strewn(tmp_path)
    assert 'request_rejected' not in srv.view_page(token, 1)
    merged = srv.merge_equations(token, [*marks['split'], marks['before'], marks['after']],
                                 1, 'a^{2} + b^{2} = c^{2}', number='(2)')
    assert 'request_rejected' not in merged, merged
    assert merged['piece'] == marks['split'][0]
    assert merged['removed'] == sorted([marks['split'][1], marks['before'], marks['after']])
    # The halves are kept as halves and the debris as debris, each with the
    # converter's whole reading of it: the fold can be read back, which is what
    # makes it a fold and not a deletion.
    assert merged['replaced']['merged'][0]['latex'] == '= c^{2}'
    folded = {row['piece']: row for row in merged['replaced']['absorbed']}
    assert sorted(folded) == sorted([marks['before'], marks['after']])
    assert folded[marks['before']] == {'piece': marks['before'], 'kind': 'heading',
                                       'text': 'with $\\{$'}
    rows = listed(token)
    assert marks['before'] not in rows and marks['after'] not in rows

    assert 'request_rejected' not in srv.replace_equation(
        token, marks['mangled'], 1, 'E = m c^{2}', number='(1)')
    assert 'request_rejected' not in srv.describe_figure(token, marks['figure'], 1, DESCRIPTION)
    assert 'request_rejected' not in srv.view_page(token, 2)
    assert 'request_rejected' not in srv.seal_manuscript(token)

    text, rows, manifest = base_of(srv.current())
    assert 'qn,s,d' not in text and 'with $\\{$' not in text
    assert '$$ a^{2} + b^{2} = c^{2} \\tag{(2)} $$' in text.splitlines()
    # Three pieces left the manuscript here — the equation's other half and the
    # two headings of its edges — and the per-piece tally walks the pieces the
    # documents still hold, so none of them is in it. Their count is kept as
    # they go, and `actions` and `gone` do not add up: a fold moves a piece
    # from the first to the second.
    assert manifest['ai_read']['gone'] == {'merged': 1, 'absorbed': 2}
    assert manifest['ai_read']['actions']['merged'] == 1, 'the piece that holds them all'
    held = [row for row in rows if row.get('original', {}).get('absorbed')]
    assert len(held) == 1 and held[0]['kind'] == 'equation'
    assert [row['text'] for row in held[0]['original']['absorbed']] == ['with $\\{$',
                                                                        '$qn,s,d$ else']


def test_a_figures_axis_text_is_folded_into_its_description(tmp_path):
    """A bar chart's axis numbers, read as a paragraph, go into the figure.

    They are the figure's own content: the converter read the numbers printed
    along the axes as prose above the caption, and a sealed manuscript carried
    "120 100 80 … Concentration(µl/ml)" as a line of the paper. The
    description is what the figure's content is in the manuscript, so the
    paragraph is folded into it.
    """
    token, marks = strewn(tmp_path)
    assert 'request_rejected' not in srv.view_page(token, 1)
    described = srv.describe_figure(token, marks['figure'], 1, DESCRIPTION,
                                    absorb=[marks['axis']])
    assert 'request_rejected' not in described, described
    assert described['removed'] == [marks['axis']]
    assert described['absorbed'] == [{'piece': marks['axis'], 'kind': 'paragraph',
                                      'text': AXIS_TEXT}]
    assert marks['axis'] not in listed(token)

    assert 'request_rejected' not in srv.replace_equation(
        token, marks['mangled'], 1, 'E = m c^{2}', number='(1)')
    assert 'request_rejected' not in srv.merge_equations(
        token, marks['split'], 1, 'a^{2} + b^{2} = c^{2}', number='(2)')
    assert 'request_rejected' not in srv.view_page(token, 2)
    assert 'request_rejected' not in srv.seal_manuscript(token)

    text, rows, _ = base_of(srv.current())
    assert 'Concentration(µl/ml)' not in text
    assert DESCRIPTION in text
    held = [row for row in rows if row.get('original', {}).get('absorbed')]
    assert len(held) == 1 and held[0]['kind'] == 'figure-description'
    assert held[0]['original']['absorbed'][0]['text'] == AXIS_TEXT


def test_what_may_be_absorbed_is_the_debris_printed_beside_the_piece(tmp_path):
    """Every way of absorbing something that is the paper rather than debris."""
    token, marks = strewn(tmp_path)
    assert 'request_rejected' not in srv.view_page(token, 1)
    table = [{'kind': 'table', 'rows': [['Hour', 'Current'], ['1', '4.5']]}]

    def refused(answer):
        assert 'request_rejected' in answer, answer
        return answer['request_rejected']

    # A figure, an equation or a table is accounted for in its own right.
    said = refused(srv.reread_piece(token, marks['prose'], 1, table, absorb=[marks['figure']]))
    assert 'is a figure' in said and 'in its own right' in said
    said = refused(srv.reread_piece(token, marks['prose'], 1, table, absorb=[marks['mangled']]))
    assert 'is an equation' in said and 'merge_equations' in said
    # A paragraph of the paper is not debris, and its length is how that shows.
    assert len(AUTHORS_PROSE) > read_stage.MOST_ABSORBED_CHARACTERS
    said = refused(srv.describe_figure(token, marks['figure'], 1, DESCRIPTION,
                                       absorb=[marks['long']]))
    assert 'characters' in said and 'reread_piece' in said and '`remove`' in said
    # Debris is absorbed off the page it is printed on, not across a page.
    said = refused(srv.describe_figure(token, marks['figure'], 1, DESCRIPTION,
                                       absorb=[marks['next_page']]))
    assert 'page 2 and not on page 1' in said
    # And out of the run: the axis paragraph between them is the paper too.
    said = refused(srv.describe_figure(token, marks['figure'], 1, DESCRIPTION,
                                       absorb=[marks['after']]))
    assert 'other pieces between these' in said
    # A re-read that writes prose alone has nothing for the debris to be part of.
    said = refused(srv.reread_piece(token, marks['prose'], 1,
                                    [{'kind': 'paragraph', 'remove': ['We have SNR ln 2']}],
                                    absorb=[marks['after']]))
    assert 'nothing here for the debris to be part of' in said
    # Three at most, and never the piece doing the absorbing.
    said = refused(srv.describe_figure(token, marks['figure'], 1, DESCRIPTION,
                                       absorb=[marks['before'], marks['after'], marks['axis'],
                                               marks['next_page']]))
    assert 'at most 3 pieces' in said
    said = refused(srv.describe_figure(token, marks['figure'], 1, DESCRIPTION,
                                       absorb=[marks['figure']]))
    assert 'the piece doing the absorbing' in said
    # None of that moved anything: every piece named is still in the draft.
    standing = listed(token)
    assert all(marks[name] in standing for name in ('before', 'after', 'axis', 'prose',
                                                    'figure', 'mangled', 'next_page', 'long'))


def test_a_table_the_converter_missed_is_added_after_its_caption(tmp_path):
    """A table only a caption names, written from the page and marked as written."""
    opened, token = reading(tmp_path)
    marks = ids_of(token)
    assert 'request_rejected' not in srv.view_page(token, 1)
    added = srv.add_piece(token, marks['table_caption'], 'table', 1,
                          rows=[['Day', 'Reading (mm)'], ['1', '12.5'], ['2', '13.0']])
    assert 'request_rejected' not in added, added
    assert added['after'] == marks['table_caption'] and added['source'] == 'ai'
    assert '| 1 | 12.5 |' in added['text']
    # It is a table, so it is accountable — and adding it accounted for it.
    assert listed(token)[added['piece']]['settled'] == 'added'
    assert added['piece'] not in [row['piece'] for row in added['outstanding']['pieces']]
    # A kind the stage does not add is refused: the rest of the manuscript is
    # the authors' and stays as converted.
    refused = srv.add_piece(token, marks['table_caption'], 'paragraph', 1, text='Not theirs.')
    assert 'A reader adds' in refused['request_rejected']

    assert 'request_rejected' not in srv.replace_equation(
        token, marks['mangled'], 1, 'E = m c^{2}', number='(1)')
    assert 'request_rejected' not in srv.merge_equations(
        token, marks['split'], 1, 'a^{2} + b^{2} = c^{2}', number='(2)')
    assert 'request_rejected' not in srv.describe_figure(token, marks['figure'], 1, DESCRIPTION)
    assert 'request_rejected' not in srv.view_page(token, 2)
    assert 'request_rejected' not in srv.seal_manuscript(token)

    text, rows, _ = base_of(srv.current())
    table = next(row for row in rows if row['kind'] == 'table')
    caption = before(rows, table)
    assert caption['kind'] == 'caption' and caption['source'] == 'text'
    assert table['source'] == 'ai' and table['page'] == 1
    start, end = table['lines']
    assert end - start == 3, 'a header, its rule and two rows'
    assert text.splitlines()[start - 1] == '| Day | Reading (mm) |'


def test_a_table_the_converter_called_a_paragraph_is_re_read_as_what_the_page_prints(tmp_path):
    """One piece out, the pieces the page prints in, and nothing lost in between.

    The two failures this is for, both off the first real run: a table that
    came out as a one-line paragraph of run-together cells, which no repair
    tool could reach because nothing had labelled it a table; and one
    converter table that held a table, the next table's caption and that table
    as well, where replacing it with one table dropped the rest of it with
    nothing in the manifest to say so.
    """
    opened, token = garbled(tmp_path)
    marks = ids_of(token)
    prose = paragraph_ids(token)
    # Nothing asks for it. The converter called it a paragraph, so it is not
    # outstanding, and the reader finds it by reading the page.
    assert 'account' not in listed(token)[prose['garbled']]
    assert len(srv.read_draft(token)['outstanding']['pieces']) == TO_ACCOUNT_FOR

    refused = srv.reread_piece(token, prose['garbled'], 1,
                               [{'kind': 'table', 'rows': [['Hour', 'Current (mA)']]}])
    assert 'not viewed page 1' in refused['request_rejected']

    assert 'request_rejected' not in srv.view_page(token, 1)
    answer = srv.reread_piece(token, prose['garbled'], 1, [
        {'kind': 'table', 'rows': [['Hour', 'Current (mA)'], ['1', '4.5'], ['2', '9.0']]},
        {'kind': 'caption', 'text': 'Table 2. Current drawn by the red widgets, by hour.'},
        {'kind': 'equation', 'latex': 'I = V / R', 'number': '(3)'}])
    assert 'request_rejected' not in answer, answer
    assert answer['replaced'] == prose['garbled']
    assert answer['kinds'] == ['table', 'caption', 'equation']
    assert answer['was']['text'] == GARBLED_TABLE
    assert answer['was']['kind'] == 'paragraph'

    rows = listed(token)
    assert prose['garbled'] not in rows, 'the piece it replaced is gone'
    pieces = answer['pieces']
    assert [rows[one]['kind'] for one in pieces] == ['table', 'caption', 'equation']
    # The two a reviewer reads as display objects were accounted for by the
    # writing of them; the caption is a caption and was never outstanding.
    assert rows[pieces[0]]['settled'] == 'reread'
    assert rows[pieces[2]]['settled'] == 'reread'
    assert 'account' not in rows[pieces[1]]
    order = [row['piece'] for row in srv.read_draft(token)['pieces']]
    at = order.index(pieces[0])
    assert order[at:at + 3] == pieces, 'in printed order, where the piece was'

    account_for_everything(token, marks)
    assert 'request_rejected' not in srv.seal_manuscript(token)
    text, sealed, manifest = base_of(srv.current())
    assert '| Hour | Current (mA) |' in text
    assert 'Caption: Table 2. Current drawn by the red widgets, by hour.' in text
    assert '$$ I = V / R \\tag{(3)} $$' in text.splitlines()
    assert GARBLED_TABLE not in text, 'and the line it was is not in the manuscript twice'
    # Every piece it became keeps the whole reading it came out of, and says
    # which part of it it is, so the re-read can be audited against it.
    parts = [row for row in sealed if row.get('original', {}).get('piece') == prose['garbled']]
    assert len(parts) == 3
    assert [row['original']['part'] for row in parts] == [1, 2, 3]
    assert all(row['original']['of'] == 3 for row in parts)
    assert all(row['source'] == 'ai-reconstructed' for row in parts)
    assert all(row['original']['text'] == GARBLED_TABLE for row in parts)
    assert manifest['ai_read']['actions']['reread'] == 3


def test_a_paragraph_re_read_names_the_debris_and_the_engine_cuts_it(tmp_path):
    """Debris of a displayed equation comes out; nothing a model types goes in.

    The converter half-swallows a displayed equation and leaves the rest of it
    inside the paragraph beside it, so the paragraph cannot be left as it is —
    and a model that may retype a paragraph is a model that may write the
    paper. So prose is not typed here at all: the reader names the span and the
    engine cuts it, and every character that comes out is a character of the
    converter's reading because it is a slice of it.
    """
    opened, token = garbled(tmp_path)
    marks = ids_of(token)
    prose = paragraph_ids(token)
    assert 'request_rejected' not in srv.view_page(token, 1)

    typed = srv.reread_piece(token, prose['debris'], 1, [
        {'kind': 'equation', 'latex': 'R = \\log_{2}(1 + \\mathrm{SNR})', 'number': '(9)'},
        {'kind': 'paragraph', 'text': 'At high signal-to-noise the logarithm is well '
                                      'approximated by a linear function of the bandwidth.'}])
    assert 'not typed here' in typed['request_rejected']
    assert DEBRIS_PARAGRAPH in typed['request_rejected'], \
        'the refusal carries the whole reading, which the listing truncates'
    assert prose['debris'] in listed(token), 'and nothing was replaced'

    nowhere = srv.reread_piece(token, prose['debris'], 1, [
        {'kind': 'paragraph', 'remove': ['We have SNR divided by ln 2']}])
    assert 'reads nowhere in this piece' in nowhere['request_rejected']
    twice = srv.reread_piece(token, prose['debris'], 1,
                             [{'kind': 'paragraph', 'remove': ['the bandwidth']}])
    assert 'reads 2 times' in twice['request_rejected']
    too_much = srv.reread_piece(token, prose['debris'], 1, [
        {'kind': 'paragraph',
         'remove': ['At high signal-to-noise the logarithm is well approximated by a linear '
                    'function of the bandwidth.']}])
    assert '% of what the converter read' in too_much['request_rejected']
    crowded = srv.reread_piece(token, prose['debris'], 1, [
        {'kind': 'paragraph', 'remove': ['We have', 'SNR', 'ln 2', 'nothing else']}])
    assert '4 spans is more than the 3' in crowded['request_rejected']

    answer = srv.reread_piece(token, prose['debris'], 1, [
        {'kind': 'equation', 'latex': 'R = \\log_{2}(1 + \\mathrm{SNR})', 'number': '(9)'},
        {'kind': 'paragraph', 'remove': ['We have SNR ln 2']}])
    assert 'request_rejected' not in answer, answer
    assert answer['kinds'] == ['equation', 'paragraph']

    # `keep` is the other way round: most of the block was a table, and one run
    # of prose is left over. It drops everything it does not name, so it is
    # allowed only where a table or an equation in the same re-read accounts
    # for the rest — prose alone, it would be a silent loss of a paragraph.
    alone = srv.reread_piece(token, prose['garbled'], 1,
                             [{'kind': 'paragraph', 'keep': 'Hour Current'}])
    assert '`keep` drops everything it does not name' in alone['request_rejected']
    assert 'Use `remove`' in alone['request_rejected']
    assert prose['garbled'] in listed(token), 'and nothing was replaced'

    held = srv.reread_piece(token, prose['garbled'], 1, [
        {'kind': 'table', 'rows': [['Hour', 'Current (mA)'], ['1', '4.5']]},
        {'kind': 'paragraph', 'keep': '2 9.0'}])
    assert 'request_rejected' not in held, held

    account_for_everything(token, marks)
    assert 'request_rejected' not in srv.seal_manuscript(token)
    text, sealed, _ = base_of(srv.current())
    assert 'SNR ln 2' not in text, 'the equation\'s debris left the paragraph'
    assert ('At high signal-to-noise the logarithm is well approximated by a linear function of '
            'the bandwidth. In that regime the gain is linear in the bandwidth and nothing '
            'else.') in text, 'and the authors\' own characters are what is left'
    assert '$$ R = \\log_{2}(1 + \\mathrm{SNR}) \\tag{(9)} $$' in text.splitlines()
    trimmed = next(row for row in sealed
                   if row.get('original', {}).get('piece') == prose['debris']
                   and row['kind'] == 'paragraph')
    assert trimmed['source'] == 'ai-reconstructed'
    assert trimmed['original']['text'] == DEBRIS_PARAGRAPH


def test_a_sentence_a_model_invents_cannot_reach_a_paragraph(tmp_path):
    """The lead's probe of the first design, which that design accepted.

    A character-subsequence test was the first guard, and almost any short
    sentence is a character subsequence of a long paragraph: re-reading the
    exosome abstract as its own text plus "This result is highly significant."
    passed, and the sentence reached the sealed line. The same probe also
    retyped a truncated preview of a long paragraph and silently dropped most
    of it. Both are refused here because prose is never typed.
    """
    opened, token = garbled(tmp_path)
    marks = ids_of(token)
    prose = paragraph_ids(token)
    assert 'request_rejected' not in srv.view_page(token, 1)
    whole = DEBRIS_PARAGRAPH

    for written in (whole + ' This result is highly significant.',
                    'This result is highly significant.',
                    whole[:60],
                    whole.replace('linear', 'lineer')):
        answer = srv.reread_piece(token, prose['debris'], 1,
                                  [{'kind': 'paragraph', 'text': written}])
        assert 'request_rejected' in answer, written
        assert 'not typed here' in answer['request_rejected']

    # The page's own pieces come back whole, so naming a span does not depend
    # on a preview that is cut off at CONTEXT_CHARACTERS. A page is an image
    # and a JSON payload beside it, which is where the pieces are.
    page = srv.view_page(token, 1)
    payload = json.loads(page[1]) if isinstance(page, list) else page
    shown = {row['piece']: row['text'] for row in payload['pieces']}
    assert shown[prose['debris']] == whole
    assert '…' not in shown[prose['debris']]

    # And `keep` is not the way round it: three words of an abstract kept and
    # the rest dropped was the hole the lead found in the span design itself.
    three = srv.reread_piece(token, prose['debris'], 1,
                             [{'kind': 'paragraph', 'keep': 'At high signal-to-noise'}])
    assert '`keep` drops everything it does not name' in three['request_rejected']

    assert 'request_rejected' not in srv.reread_piece(
        token, prose['debris'], 1, [{'kind': 'paragraph', 'remove': ['We have SNR ln 2']}])
    account_for_everything(token, marks)
    assert 'request_rejected' not in srv.seal_manuscript(token)
    text, _, _ = base_of(srv.current())
    assert 'highly significant' not in text
    assert 'SNR ln 2' not in text
    assert 'In that regime the gain is linear in the bandwidth' in text, \
        'the paragraph is still the paragraph, not three words of it'


def test_an_empty_piece_is_refused_and_one_the_stage_added_can_be_withdrawn(tmp_path):
    """The caption that got in through the wrong argument, and the way back out.

    ``add_piece`` with the text in ``rows`` wrote a caption whose text was
    empty: the rendered line is ``Caption:`` and not empty, so the check that
    was on the rendered line passed it, and the stage had nothing that could
    take a piece out again. It reached a sealed manuscript.
    """
    opened, token = reading(tmp_path)
    marks = ids_of(token)
    assert 'request_rejected' not in srv.view_page(token, 1)
    before_count = len(listed(token))

    refused = srv.add_piece(token, marks['table_caption'], 'caption', 1,
                            rows=[['Table 2. Readings of the red widgets.']])
    assert 'a caption is its text' in refused['request_rejected']
    assert len(listed(token)) == before_count, 'and nothing was written'

    added = srv.add_piece(token, marks['table_caption'], 'caption', 1,
                          text='Table 2. Readings of the red widgets.')
    assert 'request_rejected' not in added, added
    gone = srv.withdraw_piece(token, added['piece'])
    assert 'request_rejected' not in gone, gone
    assert gone['kind'] == 'caption' and gone['unsettled'] == ''
    assert added['piece'] not in listed(token)
    assert len(listed(token)) == before_count

    # A converter piece is the authors' text, and leaving one out is not a
    # repair of it.
    held = srv.withdraw_piece(token, marks['mangled'])
    assert 'not a piece this stage added' in held['request_rejected']
    assert 'reread_piece' in held['request_rejected']
    assert marks['mangled'] in listed(token)

    # A figure's description is this stage's own, and withdrawing it leaves the
    # figure unaccounted for rather than sealing a figure with no description.
    assert 'request_rejected' not in srv.describe_figure(token, marks['figure'], 1, DESCRIPTION)
    assert listed(token)[marks['figure']]['settled'] == 'described'
    description = next(key for key, row in listed(token).items()
                       if row['kind'] == 'figure-description')
    dropped = srv.withdraw_piece(token, description)
    assert 'request_rejected' not in dropped, dropped
    assert dropped['unsettled'] == marks['figure']
    assert listed(token)[marks['figure']]['settled'] is None
    assert marks['figure'] in [row['piece']
                               for row in srv.read_draft(token)['outstanding']['pieces']]

    # A withdrawn piece is in no document, so nothing the seal walks counts it.
    # The count is kept at the act: two pieces taken back out, and the one
    # equation half folded into its other half by the work below.
    account_for_everything(token, marks)
    assert 'request_rejected' not in srv.seal_manuscript(token)
    assert base_of(srv.current())[2]['ai_read']['gone'] == {'withdrawn': 2, 'merged': 1}


def test_a_paragraph_the_converter_mislabelled_can_become_an_equation(tmp_path):
    """The kind gate was on the converter's guess, which is what was wrong.

    ``replace_equation`` and ``replace_table`` checked that the piece was
    already an equation or a table, so the one case they were needed for —
    the converter having emitted a displayed piece as a paragraph — was the
    one case they refused.
    """
    opened, token = garbled(tmp_path)
    marks = ids_of(token)
    prose = paragraph_ids(token)
    assert 'request_rejected' not in srv.view_page(token, 1)

    answer = srv.replace_table(token, prose['garbled'], 1,
                               [['Hour', 'Current (mA)'], ['1', '4.5'], ['2', '9.0']])
    assert 'request_rejected' not in answer, answer
    assert answer['kind'] == 'table' and answer['source'] == 'ai-reconstructed'
    assert answer['replaced']['kind'] == 'paragraph'
    assert answer['replaced']['text'] == GARBLED_TABLE
    assert listed(token)[prose['garbled']]['settled'] == 'repaired'

    # A figure's caption is not retyped into something else: its image is what
    # is missing. Nor is a caption written over, because the piece it names
    # would be left with none.
    assert 'is the caption of a figure' in srv.replace_table(
        token, marks['figure'], 1, [['a', 'b']])['request_rejected']
    kept = srv.replace_table(token, marks['table_caption'], 1, [['a', 'b']])
    assert 'reread_piece' in kept['request_rejected']
    assert 'no caption' in kept['request_rejected']


def test_the_reader_leaves_a_note_about_the_document_for_whoever_reads_it(tmp_path):
    """What the reader knew and no reviewer could work out, where they will see it.

    On the first real run the reader's own report said a table had been
    dropped by the converter and two more were garbled. None of it reached a
    file: the reviewers read the manuscript with nothing saying any of that,
    and an application reading the run could not find it either.
    """
    opened, token = reading(tmp_path)
    marks = ids_of(token)
    account_for_everything(token, marks)

    assert 'says nothing' in srv.seal_manuscript(token, note='garbled')['request_rejected']
    long = srv.seal_manuscript(token, note='The converter dropped a table. ' * 100)
    assert 'the manifest keeps' in long['request_rejected']
    assert srv.current().base is None, 'and a refused note sealed nothing'

    note = ('Table 1 is the converter\'s; its cells ran together and I re-read it off page 1. '
            'Figure 1 is a photograph and its scale bar is too small to read in the image.')
    sealed = srv.seal_manuscript(token, note=note)
    assert 'request_rejected' not in sealed, sealed
    assert sealed['ai_read']['note'] == note
    _, _, manifest = base_of(srv.current())
    assert manifest['ai_read']['note'] == note

    # Where a reviewer reads it: above the manuscript, as the reader's account
    # and not as the paper's.
    reviewer = srv.claim_agent('core-1', CODES['core-1'])['agent_token']
    served = srv.read_paper(reviewer)
    text = served if type(served) is str else json.dumps(served)
    assert note in text
    assert 'not the authors\' text and not a finding' in text


def test_a_run_sealed_as_converted_keeps_the_converter_s_reading_and_says_so(tmp_path):
    """What the recorder's seal leaves behind: no setting reaches this, only a failure."""
    opened = drafted(tmp_path)
    sealed = srv.seal_as_converted(CODES['recorder_key'], reason='nothing read the pages')
    assert sealed['sealed'] is True and sealed['ai_read']['read'] is False
    assert sealed['statistics']['lines'][paper_base.MAIN_DOCUMENT] > 0
    assert sealed['sections']
    # Every slot claims now: there is a manuscript to read.
    taken = srv.claim_agent('core-1', CODES['core-1'])
    assert 'agent_token' in taken, taken
    assert 'read_draft' not in taken['allowed_tools']

    text, rows, manifest = base_of(srv.current())
    assert {row['source'] for row in rows} == {'text'}
    assert manifest['ai_read']['read'] is False
    assert manifest['ai_read']['reason']
    assert MANGLED in text, "the converter's reading, as it stood"
    assert 'Figure description:' not in text
    # And the gaps say what they said before this stage existed.
    assert 'claude' not in manifest['known_gaps']['figures']
    assert not (srv.current().workspace / DRAFT_FILE).exists()
    assert opened['reader'] is not None, 'the run planned a reader, as every run does'


def test_a_reader_that_never_sealed_leaves_the_recorder_to_seal_as_converted(tmp_path):
    """The failure path: no manuscript is no review, so the recorder writes one."""
    opened, token = reading(tmp_path)
    # The reader got part way — one repair — and never sealed.
    marks = ids_of(token)
    assert 'request_rejected' not in srv.view_page(token, 1)
    assert 'request_rejected' not in srv.replace_equation(token, marks['mangled'], 1,
                                                          'E = m c^{2}', '(1)')
    assert 'request_rejected' in srv.seal_manuscript(token), 'there is work left'
    # No reviewer can work until there is a manuscript, so the run is not
    # blocked on the reader: it is sealed as the converter read it.
    assert 'request_rejected' in srv.claim_agent('core-1', CODES['core-1'])
    refused = srv.seal_as_converted('not-the-key')
    assert 'recorder' in refused['request_rejected']
    sealed = srv.seal_as_converted(CODES['recorder_key'], reason='the reader stopped after one '
                                   'repair and reported nothing')
    assert sealed['sealed'] is True and sealed['ai_read']['read'] is False
    assert 'did not seal' in sealed['ai_read']['reason']
    assert 'stopped after one repair' in sealed['ai_read']['reason']
    assert sealed['statistics']['lines'][paper_base.MAIN_DOCUMENT] > 0
    assert 'citations' in sealed and sealed['sections']
    # The repair the reader did finish is in the manuscript — it is the draft
    # that was sealed, not the converter's output a second time — and the run
    # still says the paper was not AI-read, which is the claim that matters.
    _, rows, manifest = base_of(srv.current())
    assert any(row['source'] == 'ai-reconstructed' for row in rows)
    assert manifest['ai_read']['read'] is False
    assert 'claude' not in manifest['known_gaps']['figures'], 'no figure was described'
    # And now the reviewers can work, while the reader cannot.
    assert 'agent_token' in srv.claim_agent('core-1', CODES['core-1'])
    assert 'request_rejected' in srv.read_draft(token)
    assert 'already sealed' in srv.seal_as_converted(CODES['recorder_key'])['request_rejected']


def test_a_finding_cites_the_lines_the_seal_wrote(tmp_path):
    """A reviewer quotes a repaired equation, and the line it cites is the sealed one."""
    opened, token = reading(tmp_path)
    marks = ids_of(token)
    account_for_everything(token, marks)
    assert 'request_rejected' not in srv.seal_manuscript(token)
    run = srv.current()
    _, rows, _ = base_of(run)
    repaired = next(row for row in rows if row['source'] == 'ai-reconstructed'
                    and row['original']['latex'] == MANGLED)
    line = repaired['lines'][0]

    reviewer = srv.claim_agent('core-1', CODES['core-1'])
    assert 'agent_token' in reviewer, reviewer
    reviewer = reviewer['agent_token']
    served = srv.read_paper(reviewer)
    assert 'request_rejected' not in served
    # What the reviewer is told about the lines it is reading: the repair names
    # the page and the model, and the description is not offered as the paper's
    # own words. Neither marker is in the file.
    text = served if type(served) is str else json.dumps(served)
    assert 'read from the page image of page 1 by ' + model_plan.DEFAULT_READER_MODEL in text
    assert 'AI description of the figure image' in text
    assert "not the authors' words" in text
    quote = run.base.line_text(line)
    assert quote == '$$ E = m c^{2} \\tag{(1)} $$'
    # The gate on an equation finding stands: the reviewer opens the page image
    # before filing against a line a model transcribed.
    early = srv.submit_finding(reviewer, title='The exponent reads oddly', quote=quote,
                               comment='Worth checking against the page.', severity='minor',
                               kind='question', line=line)
    assert 'Call view_page on page 1' in early['request_rejected']
    assert 'request_rejected' not in srv.view_page(reviewer, 1)
    stored = srv.submit_finding(reviewer, title='The exponent reads oddly', quote=quote,
                                comment='The page prints a square, as the equation now reads.',
                                severity='minor', kind='question', line=line)
    assert 'request_rejected' not in stored, stored
    filed = next(held for held in run.findings
                 if held['finding_uid'] == stored['finding_uid'])
    assert filed['line'] == line and filed['end_line'] == line
    assert filed['piece_kind'] == 'equation'
    assert filed['quoted_exactly'] is True
    # The line the finding cites is the line the map says is a repair, which is
    # the only reason the two readings can be put side by side at all.
    assert repaired['lines'] == filed['piece_lines']


def test_the_reader_cannot_work_after_the_seal(tmp_path):
    """The stage is over when the manuscript exists, whoever still holds the token."""
    opened, token = reading(tmp_path)
    marks = ids_of(token)
    account_for_everything(token, marks)
    assert 'request_rejected' not in srv.seal_manuscript(token)
    refused = srv.read_draft(token)
    assert 'sealed' in refused['request_rejected']
    assert 'Read stage is over' in refused['request_rejected']
    assert 'request_rejected' in srv.confirm_piece(token, marks['mangled'], 1)
    # And the run's own progress says the stage is done rather than leaving the
    # reading's accounting behind to be read as outstanding work.
    progress = json.loads((srv.current().review / 'progress.json').read_text(encoding='utf-8'))
    assert progress['read'] == {'sealed': True, 'outstanding': None}


def test_a_reader_claiming_after_the_seal_is_refused_in_words(tmp_path):
    """The claim is answered. A slot that cannot be served says why, not how it broke."""
    opened = drafted(tmp_path)
    assert opened['reader'] is not None, 'this run opened on a draft, so it planned a reader'
    seal_converted(opened)
    # The reader that was planned arrives late — a slow spawn, a retry, a
    # reader the recorder gave up waiting for and sealed around — and finds the
    # manuscript written. Its claim is refused in words a model can act on
    # rather than raising out of a desk built over a draft that is gone.
    refused = srv.claim_agent('reader', CODES['reader'])
    assert 'request_rejected' in refused, refused
    assert 'sealed' in refused['request_rejected']
    assert 'nothing to read' in refused['request_rejected']
    assert 'Read stage is over' in refused['request_rejected']
    # And it is the state that refuses, not the code: a reader holding the
    # right code is told the same thing, and the run is untouched either way.
    assert 'request_rejected' in srv.claim_agent('reader', 'not-the-code')
    assert srv.current().agents.get('reader') is None
    assert 'agent_token' in srv.claim_agent('core-1', CODES['core-1'])


def test_a_manuscript_sealed_before_the_run_opened_plans_no_reader_at_all(tmp_path):
    """An application's own source: nothing here converted it, so there is no stage.

    ``write_manuscript`` is the call an application writes its own manuscript
    with, and a folder that already holds one arrives with paper.md, map.json
    and citable lines. The run planned a reader slot there anyway — a code
    handed back for a stage that could not run, and a claim that raised rather
    than answered. A slot nobody can claim is not planned.
    """
    root = tmp_path / 'run'
    build_base(root / names.MANUSCRIPT_DIRECTORY,
               [{'kind': 'title', 'text': 'A manuscript the application wrote',
                 'section': 'Title'},
                {'kind': 'paragraph', 'text': 'These lines were written by the application '
                 'that holds the paper, from its own source, and they are citable already.',
                 'section': '1 Introduction'}],
               # The source the application names is the one being reviewed, so
               # the folder guard reads this as the same manuscript rather than
               # a different paper in a reused folder.
               manifest={'source': {'file': FIXTURE.name, 'pages': 1,
                                    'sha256': digest(FIXTURE.read_bytes())}})
    opened = srv.ingest(str(FIXTURE), str(root), reviewers=1, arm='single')
    assert 'request_rejected' not in opened, opened
    assert opened['sealed'] is True
    assert opened['reader'] is None, 'there is no Read stage, so there is no reader to spawn'
    codes = {row['slot']: row['claim_code'] for row in opened['claim_codes']}
    assert 'reader' not in codes, 'no code is handed back for a slot nobody can claim'
    assert 'core-1' in codes and 'recorder' in codes
    assert 'reader' not in srv.current().models, 'and no model is planned to run it'
    # Asked for anyway, the slot is refused in words that name the seal — the
    # same answer whatever code is offered, because the stage is what is
    # missing and not the identity.
    refused = srv.claim_agent('reader', 'any-code-at-all')
    assert 'request_rejected' in refused, refused
    assert 'already sealed' in refused['request_rejected']
    assert 'nothing to read' in refused['request_rejected']
    # The stages this run does have work: the lines are written, so a reviewer
    # reads the manuscript the application handed in.
    taken = srv.claim_agent('core-1', codes['core-1'])
    assert 'agent_token' in taken, taken
    assert 'request_rejected' not in srv.read_paper(taken['agent_token'])


def test_the_readers_allowance_is_the_size_of_what_it_has_to_read(tmp_path):
    """Two pages and four accountable pieces, not a number someone picked."""
    opened, token = reading(tmp_path)
    assert opened['reader']['allowance'] == srv.reader_call_budget(2, TO_ACCOUNT_FOR)
    assert opened['reader']['model'] == 'claude-sonnet-5-5'
    # read_draft costs nothing: the stage does not begin by rationing the list
    # of what it has to do.
    assert srv.read_draft(token)['calls_left'] == opened['reader']['allowance']
    assert 'request_rejected' not in srv.view_page(token, 1)
    assert srv.read_draft(token)['calls_left'] == opened['reader']['allowance'] - 1
    log = (srv.current().review / 'reader' / 'tool-log.jsonl').read_text(encoding='utf-8')
    written = [json.loads(line) for line in log.splitlines()]
    assert [one['tool'] for one in written] == ['read_draft', 'view_page', 'read_draft']
    assert all('image' not in one['result'] for one in written), 'the image is in views/'
    assert all('pieces' not in one['result'] for one in written), 'and the listing is the draft'


def test_a_supplement_is_read_and_accounted_for_as_a_document_of_its_own(tmp_path):
    """A supplement goes through the same stage under the same rules."""
    opened, token = reading(tmp_path, supplement='supplement-s1.md')
    served = srv.read_draft(token)
    assert served['documents'] == [paper_base.MAIN_DOCUMENT, 'supplement-s1.md']
    supplementary = srv.read_draft(token, document='supplement-s1.md')
    assert 'request_rejected' not in supplementary, supplementary
    assert supplementary['document'] == 'supplement-s1.md'
    equation = next(row for row in supplementary['pieces'] if row.get('account') == 'equation')
    assert equation['settled'] is None
    # The supplement's equation is one of the pieces the seal is waiting for.
    left = served['outstanding']['pieces']
    assert len(left) == TO_ACCOUNT_FOR + 1
    assert equation['piece'] in [row['piece'] for row in left]
    assert 'supplement-s1.md' in {row['document'] for row in left}

    marks = ids_of(token)
    account_for_everything(token, marks)
    refused = srv.seal_manuscript(token)
    assert equation['piece'] in refused['request_rejected']
    assert 'request_rejected' not in srv.confirm_piece(token, equation['piece'], 1)
    assert 'request_rejected' not in srv.seal_manuscript(token)
    run = srv.current()
    assert 'supplement-s1.md' in run.base.document_names()
    assert '$$ x = y \\tag{(S1)} $$' in (
        run.workspace / 'supplement-s1.md').read_text(encoding='utf-8')


# -- the stage as the plugin ships it --------------------------------------
#
# The server holds the rules; these hold the three files that have to agree
# with it, because a reader whose agent file stops it short of the seal leaves
# a draft nothing can write, and that is a run with no manuscript at all.


def test_the_readers_turn_cap_is_the_one_its_allowance_is_built_on():
    front = READER_FILE.read_text(encoding='utf-8').split('---')[1]
    stated = re.search(r'^maxTurns:\s*(\d+)\s*$', front, re.MULTILINE)
    assert stated, front
    assert int(stated.group(1)) == srv.READER_MAX_TURNS
    assert srv.READER_CALL_CEILING == srv.READER_MAX_TURNS - srv.READER_TURN_RESERVE
    # The allowance for a big paper still fits inside the cap it is spent under.
    assert srv.reader_call_budget(40, 60) <= srv.READER_CALL_CEILING < srv.READER_MAX_TURNS


def test_the_reader_is_given_every_tool_the_stage_needs_and_nothing_else():
    """A tool the agent file omits is a piece that can never be accounted for."""
    front = READER_FILE.read_text(encoding='utf-8').split('---')[1]
    listed = re.search(r'^tools: (.+)$', front, re.MULTILINE).group(1)
    names_listed = [name.strip().split('__')[-1] for name in listed.split(',')]
    assert names_listed == ['claim_agent', *srv.READER_TOOLS]
    # It reads and describes; it files nothing and judges nothing.
    for forbidden in ('submit_finding', 'write_review', 'view_paper', 'seal_as_converted'):
        assert forbidden not in listed
    model = re.search(r'^model: (.+)$', front, re.MULTILINE).group(1).strip()
    assert model == model_plan.DEFAULT_READER_MODEL


def test_the_workflow_runs_the_read_stage_before_anything_claims_a_slot():
    source = WORKFLOW.read_text(encoding='utf-8')
    read_at = source.index("phase('Read')")
    assert read_at < source.index("phase('Review')")
    # The reader is claimed like every other slot, with its own code and type.
    assert "agentType: 'pra:reader'" in source
    assert 'codeFor(readerSlot)' in source and "reissue(readerSlot, 'Read')" in source
    # Its model has to arrive, the same as every other slot's.
    assert 'concat(readerSlot ? [readerSlot] : [])' in source
    # The failure path, which is the only way a run reaches a manuscript
    # nothing read: there is no setting that skips the stage.
    assert 'seal_as_converted' in source
    assert 'ai_read=false' not in source
    # The counts come from the seal, because the ledger's rows cite its lines.
    assert 'opened.citations &&' not in source
    for sized in ('const referenceCount = (counts && counts.references)',
                  'const uses = (counts && counts.uses)'):
        assert sized in source, sized
    assert source.index('let counts = ') < source.index('const referenceCount = ')
