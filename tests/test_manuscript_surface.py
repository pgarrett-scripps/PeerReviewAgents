"""A manuscript an application wrote, read back by the engine.

An application that keeps its manuscript in its own format — paper-workbench
holds a Typst source and parses it itself — writes the engine's workspace from
the pieces it already has rather than from a PDF this package converts. Every
write here goes through the public surface, ``peerreviewagents`` and nothing
below it, because that surface is the whole of what such an application may
use: a second implementation of the base living in an app is the thing these
exports exist to prevent.

A Typst manuscript has no PDF pages until it is built, so the pieces here carry
no ``page`` and no ``bbox``. That is the case the engine has to accept: the
citation is a line of ``paper.md``, and the page a line came from is a column
of ``map.json`` that may be empty.

``read_paper`` is the review server's own tool rather than a library call, so it
is reached here through :class:`Investigation`, the engine object the server
runs on. What it proves is that an agent handed this workspace reads the
manuscript the application wrote, numbered as the application's own viewer
numbers it.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import peerreviewagents
from peerreviewagents import scripted
from peerreviewagents import workspace as workspace_tools
from peerreviewagents.engine.investigation import Investigation

FIXTURE = Path(__file__).resolve().parent / 'fixtures' / 'single-column.pdf'

#: One short paper, in the shape an application hands the writer: a kind, the
#: text, the section it sits in, and nothing about a page.
PIECES = [
    {'kind': 'title', 'text': 'Replay during quiet rest', 'section': 'Title'},
    {'kind': 'heading', 'level': 1, 'text': 'Results', 'section': 'Results'},
    {'kind': 'paragraph', 'section': 'Results',
     'text': 'Sequences recurred in 12 of 14 sessions (86%).'},
    {'kind': 'equation', 'section': 'Results', 'latex': 'r = \\frac{a}{b}', 'number': 1},
    {'kind': 'table', 'section': 'Results',
     'table': [['Session', 'Events'], ['1', '12'], ['2', '14']]},
    {'kind': 'figure', 'section': 'Results',
     'text': 'Figure 1. Replay events per session.'},
]

SUPPLEMENT = [
    {'kind': 'title', 'text': 'Supplementary methods', 'section': 'Supplement'},
    {'kind': 'supplement', 'section': 'Supplement',
     'text': 'Spikes were sorted with the same template library.'},
]


def workspace(folder, pieces=None, scaffold=True, **extra):
    """A manuscript workspace, written the way an application writes one."""
    folder.mkdir(parents=True, exist_ok=True)
    if scaffold:
        # The source the application builds the manuscript from, kept beside
        # the base. It is written first because the manifest digests the folder
        # as it stands.
        (folder / 'scaffold').mkdir(exist_ok=True)
        (folder / 'scaffold' / 'main.typ').write_text('= Replay during quiet rest\n',
                                                      encoding='utf-8')
    return peerreviewagents.write_manuscript(
        folder, pieces if pieces is not None else PIECES,
        parser='paper-workbench 1.0', source={'file': 'scaffold/main.typ'}, **extra)


def test_a_manuscript_with_no_pages_is_written_read_and_served(tmp_path):
    folder = tmp_path / 'manuscript'
    manifest = workspace(folder, references=[{'id': 'r1', 'raw': 'Wilson, 1994.'}])

    # What the writer reports: the schema and the main document are the two
    # names the public surface carries, so a consumer checks them without
    # importing anything of the engine's.
    assert manifest['schema_version'] == peerreviewagents.BASE_SCHEMA_VERSION
    assert manifest['documents'] == [peerreviewagents.MAIN_DOCUMENT]
    assert manifest['title'] == 'Replay during quiet rest'
    assert manifest['parser'] == 'paper-workbench 1.0'
    # No PDF was involved, so there are no pages to count and none to view.
    assert manifest['source']['pages'] == 0
    assert manifest['statistics']['pdf_pages'] == 0
    assert manifest['statistics']['reference_entries'] == 1
    assert manifest['counts']['figure_captions'] == 1
    assert manifest['files'][peerreviewagents.MAIN_DOCUMENT]
    assert manifest['files']['scaffold/main.typ']
    # No pages means no page geometry either, and the empty list says so: a
    # viewer reads it as "nothing to draw a box on" rather than as a size it
    # has to guess.
    assert manifest['pages'] == []

    # What the loader returns: the lines, the map rows and the manifest.
    base = peerreviewagents.read_manuscript(folder)
    assert isinstance(base, peerreviewagents.PaperBase)
    assert base.title == manifest['title']
    assert base.line_text(1) == '# Replay during quiet rest'
    assert '86%' in base.text
    assert base.section_at(5) == 'Results'
    assert base.manifest['paper_id'] == manifest['paper_id']
    # Every row says what it is and where it sits; a page and a box are the two
    # columns a manuscript with no pages leaves empty.
    assert {row['page'] for row in base.rows} == {None}
    assert {row['bbox'] for row in base.rows} == {None}
    assert [row['kind'] for row in base.rows] == ['title', 'heading', 'paragraph', 'equation',
                                                  'table', 'figure']

    # verify_workspace re-digests every file the manifest lists and returns the
    # same base, which is what a run anchors its findings against.
    verified = peerreviewagents.verify_workspace(folder)
    assert isinstance(verified, peerreviewagents.PaperBase)
    assert verified.paper_id == manifest['paper_id']
    assert verified.documents == base.documents

    # And an agent reading it gets the manuscript, numbered as the file is.
    served = Investigation({'paper': str(folder), 'output': str(tmp_path / 'out'),
                            'cache': str(tmp_path / 'cache')}).call('read_paper', {})
    assert '    1| # Replay during quiet rest' in served['text']
    assert 'Sequences recurred in 12 of 14 sessions (86%).' in served['text']
    assert served['tool_receipt']['spent'] is False


def test_a_supplement_is_a_document_of_its_own(tmp_path):
    folder = tmp_path / 'manuscript'
    manifest = workspace(folder, {peerreviewagents.MAIN_DOCUMENT: PIECES,
                                  'supplement-methods.md': SUPPLEMENT})
    assert manifest['documents'] == [peerreviewagents.MAIN_DOCUMENT, 'supplement-methods.md']
    base = peerreviewagents.read_manuscript(folder)
    assert base.document_names() == [peerreviewagents.MAIN_DOCUMENT, 'supplement-methods.md']
    assert 'template library' in base.documents['supplement-methods.md']
    assert base.line_count('supplement-methods.md') == 3


def test_a_finding_cites_a_line_and_has_no_box_to_highlight(tmp_path):
    """What highlighting does on a manuscript with no pages: it highlights lines."""
    folder = tmp_path / 'manuscript'
    workspace(folder)
    base = peerreviewagents.verify_workspace(folder)
    review = workspace_tools.normalize_review(
        base, {'findings': [{'title': 'Check the percentage', 'kind': 'question',
                             'quote': '12 of 14 sessions (86%)',
                             'comment': '12/14 is 85.7%.'}]}, 'review')
    placed = review['findings'][0]
    assert placed['placement'] == 'anchored'
    assert placed['document'] == peerreviewagents.MAIN_DOCUMENT
    assert '12 of 14' in base.line_text(placed['line'])
    # The span inside the line is what a viewer highlights; the page and the box
    # a PDF viewer would need are empty, because there is no page to draw on.
    assert placed['end_line'] == placed['line']
    assert placed['anchor_text'] == '12 of 14 sessions (86%)'
    assert placed['page'] is None and placed['bbox'] is None
    assert base.line_text(placed['line'])[placed['start']:placed['end']] == placed['anchor_text']


def test_a_built_manuscript_carries_the_page_each_box_is_drawn_on(tmp_path):
    """Once the application has built its PDF, its boxes are placeable.

    An app that renders its Typst source to a PDF knows where each piece landed
    and passes the boxes through. What it also has to pass is the size of every
    page, in the same points, or the box cannot be scaled to the image of the
    page a viewer is showing. The convention is written into the manifest with
    them, so a reader of the folder is not left to infer which corner the
    numbers are measured from.
    """
    folder = tmp_path / 'built'
    pieces = [{'kind': 'title', 'text': 'Replay during quiet rest', 'section': 'Title',
               'page': 1, 'bbox': {'x0': 72.0, 'y0': 60.0, 'x1': 540.0, 'y1': 84.0}},
              {'kind': 'paragraph', 'text': 'Sequences recurred in 12 of 14 sessions.',
               'section': 'Results', 'page': 2, 'bbox': [72.0, 120.0, 540.0, 160.0]}]
    manifest = workspace(folder, pieces, pages=[
        {'page': 1, 'width': 595.276, 'height': 841.89},
        {'page': 2, 'width': 595.276, 'height': 841.89, 'rotation': 90}])

    assert manifest['pages'] == [
        {'page': 1, 'width': 595.276, 'height': 841.89, 'rotation': 0},
        {'page': 2, 'width': 595.276, 'height': 841.89, 'rotation': 90}]
    # The page count follows the geometry when the caller does not give one.
    assert manifest['source']['pages'] == 2
    assert 'top-left' in manifest['box_convention']

    base = peerreviewagents.read_manuscript(folder)
    sizes = {page['page']: page for page in manifest['pages']}
    for row in base.rows:
        page = sizes[row['page']]
        x0, y0, x1, y1 = row['bbox']
        assert 0 <= x0 < x1 <= page['width'] and 0 <= y0 < y1 <= page['height']


def test_the_source_tree_beside_the_base_is_part_of_the_manuscript(tmp_path):
    """A scaffold workspace: the sources are digested with everything else."""
    folder = tmp_path / 'manuscript'
    manifest = workspace(folder)
    assert sorted(name for name in manifest['files'] if name.startswith('scaffold/')) == [
        'scaffold/main.typ']
    peerreviewagents.verify_workspace(folder)

    # A source file added after the write is a manuscript that changed under
    # the review, and is refused although no listed file was touched.
    (folder / 'scaffold' / 'figure.typ').write_text('// later\n', encoding='utf-8')
    with pytest.raises(ValueError, match='inventory changed'):
        peerreviewagents.verify_workspace(folder)
    (folder / 'scaffold' / 'figure.typ').unlink()
    peerreviewagents.verify_workspace(folder)

    # And an edited document is refused by name, which is the whole point of
    # the digests: a line number is only worth having while the line is there.
    (folder / peerreviewagents.MAIN_DOCUMENT).write_text('Something else.\n', encoding='utf-8')
    with pytest.raises(ValueError, match='Manuscript source changed: paper.md'):
        peerreviewagents.verify_workspace(folder)


def test_a_manuscript_that_does_not_say_which_shape_it_is_is_refused(tmp_path):
    """An unstamped manifest was read as the current shape on no evidence.

    ``paper-base-v1`` is refused by name, but a manifest with no
    ``schema_version`` at all — an older folder, or one written by something
    that is not this format — passed straight through, and its rows were
    trusted to carry fields they may never have had. Both readers refuse it,
    and so does a folder with no manifest beside the base.
    """
    folder = tmp_path / 'manuscript'
    workspace(folder)
    unstamped = workspace_tools.read_json(folder / 'manifest.json')
    unstamped.pop('schema_version')
    workspace_tools.write_json(folder / 'manifest.json', unstamped)
    for read in (peerreviewagents.read_manuscript, peerreviewagents.verify_workspace):
        with pytest.raises(ValueError, match='no schema_version') as refusal:
            read(folder)
        assert peerreviewagents.BASE_SCHEMA_VERSION in str(refusal.value)

    # And the manifest is one of the three files a manuscript is, so a folder
    # holding only the other two is not one — it is a half-written workspace,
    # and reading it would produce line numbers nothing can be checked against.
    (folder / 'manifest.json').unlink()
    for read in (peerreviewagents.read_manuscript, peerreviewagents.verify_workspace):
        with pytest.raises(ValueError, match='no manifest.json'):
            read(folder)


def test_a_scaffold_that_holds_its_own_manifest_is_covered_like_any_file(tmp_path):
    """Only the workspace's own manifest is outside the digests.

    A source tree built by a tool that keeps a ``manifest.json`` of its own —
    a figure inventory, a build record — had that file skipped by name at
    every depth, so it was the one file in the manuscript that could be
    rewritten under a saved review and still verify.
    """
    folder = tmp_path / 'manuscript'
    folder.mkdir(parents=True)
    (folder / 'scaffold').mkdir()
    (folder / 'scaffold' / 'main.typ').write_text('= Replay during quiet rest\n',
                                                  encoding='utf-8')
    (folder / 'scaffold' / 'manifest.json').write_text('{"figures": ["raster-1.png"]}\n',
                                                       encoding='utf-8')
    manifest = workspace(folder, scaffold=False)

    assert 'scaffold/manifest.json' in manifest['files']
    # The workspace's own manifest is still outside the digests: it is what
    # holds them.
    assert 'manifest.json' not in manifest['files']
    peerreviewagents.verify_workspace(folder)

    (folder / 'scaffold' / 'manifest.json').write_text('{"figures": []}\n', encoding='utf-8')
    with pytest.raises(ValueError, match='Manuscript source changed: scaffold/manifest.json'):
        peerreviewagents.verify_workspace(folder)


def test_a_piece_that_would_render_nothing_is_refused_by_name(tmp_path):
    """The silent drop: twelve pieces in, eleven in the manuscript.

    A ``list`` piece that keeps its text under ``items`` renders to nothing,
    and nothing was written and nothing said — the map simply had one row
    fewer than the pieces the caller composed, and the counts agreed with
    themselves because they were taken over the rows.
    """
    pieces = [{'kind': 'title', 'text': 'Replay during quiet rest'},
              {'kind': 'list', 'text': '', 'section': 'Results'}]
    with pytest.raises(ValueError, match=r'Piece 2 of paper\.md \(kind "list"\) renders to no '
                                         r'line'):
        peerreviewagents.write_manuscript(tmp_path / 'listed', pieces, title='Replay')
    assert not (tmp_path / 'listed' / 'map.json').exists()
    # The reported shape, a list whose items are under `items`, is refused for
    # the field before it is refused for the rendering: either way the write
    # stops at the piece.
    with pytest.raises(ValueError, match='items'):
        peerreviewagents.write_manuscript(
            tmp_path / 'itemised',
            [pieces[0], {'kind': 'list', 'items': ['A first point.']}], title='Replay')

    # A supplement's pieces are checked the same way, and named by document.
    with pytest.raises(ValueError, match=r'Piece 1 of supplement-methods\.md'):
        peerreviewagents.write_manuscript(
            tmp_path / 'supplemented',
            {peerreviewagents.MAIN_DOCUMENT: PIECES,
             'supplement-methods.md': [{'kind': 'paragraph', 'text': '  '}]},
            title='Replay')

    # The list written as the format holds it — one paragraph per item — is
    # the manuscript the application meant.
    written = peerreviewagents.write_manuscript(
        tmp_path / 'spelled-out',
        [pieces[0], {'kind': 'paragraph', 'text': 'A first point.'},
         {'kind': 'paragraph', 'text': 'A second point.'}], title='Replay')
    assert written['counts']['pieces'] == 3


def test_a_field_the_format_has_no_column_for_is_refused_by_name(tmp_path):
    """An app's own key on a piece was dropped between the call and the map."""
    pieces = [dict(PIECES[0], anchor_id='t1'), *PIECES[1:]]
    with pytest.raises(ValueError, match='anchor_id') as refusal:
        peerreviewagents.write_manuscript(tmp_path / 'anchored', pieces, title='Replay')
    # It says what a piece may carry, because the answer to this refusal is
    # always to move the value into a field the format holds — or to drop it.
    assert 'page_mapping' in str(refusal.value) and 'citations' in str(refusal.value)

    # Every field the renderer and the map row do use is accepted, including
    # the ones only some kinds render from.
    manifest = workspace(tmp_path / 'full', [
        {'kind': 'title', 'text': 'Replay during quiet rest', 'section': 'Title'},
        {'kind': 'heading', 'text': 'Methods', 'level': 1, 'section': 'Methods'},
        {'kind': 'equation', 'text': '', 'latex': 'y = ax + b', 'number': '1',
         'confidence': 0.9, 'section': 'Methods'},
        {'kind': 'table', 'text': 'Table 1', 'section': 'Results',
         'table': {'rows': [['Batch', 'Mass'], ['A', '12 g']]}},
        {'kind': 'figure', 'text': 'Firing rate over time.', 'label': 'Figure 1',
         'image': 'figures/fig1.png', 'section': 'Results'},
        {'kind': 'paragraph', 'text': 'Rates rose.', 'section': 'Results',
         'source': 'ai-reconstructed', 'original': {'text': 'Rates r0se.'},
         'citations': ['r1'], 'page_mapping': 'typst', 'page': 1,
         'bbox': [72.0, 100.0, 540.0, 120.0]},
    ], pages=[{'page': 1, 'width': 612.0, 'height': 792.0}],
        ai_model='claude-sonnet-5-5')
    assert manifest['counts']['pieces'] == 6
    # The one reconstructed piece is counted and its author named, so a
    # reviewer served the line and an app reading the manifest are told the
    # same thing: a model wrote it, and which one.
    assert manifest['ai_read']['read'] is False
    assert manifest['ai_read']['ai_model'] == 'claude-sonnet-5-5'
    assert manifest['ai_read']['ai_pieces'] == 1
    assert 'claude-sonnet-5-5' in manifest['ai_read']['reason']


def test_every_piece_the_caller_gave_has_a_row(tmp_path):
    """The count the map reports is over the pieces, not over the rows."""
    folder = tmp_path / 'manuscript'
    manifest = workspace(folder)
    rows = peerreviewagents.read_manuscript(folder).rows
    assert len(rows) == len(PIECES) == manifest['counts']['pieces']


def test_what_the_writer_refuses(tmp_path):
    with pytest.raises(ValueError, match='needs a title'):
        peerreviewagents.write_manuscript(tmp_path / 'untitled',
                                          [{'kind': 'paragraph', 'text': 'A sentence.'}])
    with pytest.raises(ValueError, match=r'ends in \.md'):
        peerreviewagents.write_manuscript(
            tmp_path / 'named', {peerreviewagents.MAIN_DOCUMENT: PIECES, 'notes.txt': SUPPLEMENT})
    with pytest.raises(ValueError, match=peerreviewagents.MAIN_DOCUMENT):
        peerreviewagents.write_manuscript(tmp_path / 'headless',
                                          {'supplement-methods.md': SUPPLEMENT})
    with pytest.raises(ValueError, match='renders to no line'):
        peerreviewagents.write_manuscript(tmp_path / 'empty', [{'kind': 'paragraph', 'text': ''}],
                                          title='Nothing at all')
    with pytest.raises(ValueError, match='rendered to a line'):
        peerreviewagents.write_manuscript(tmp_path / 'none', [], title='Nothing at all')
    # A model wrote the line and nothing says which model. The reviewer is
    # served a marker naming the writer, so the name has to exist.
    with pytest.raises(ValueError, match='ai_model') as unnamed:
        peerreviewagents.write_manuscript(tmp_path / 'unnamed', [
            {'kind': 'title', 'text': 'Replay during quiet rest'},
            {'kind': 'paragraph', 'text': 'Rates rose.', 'source': 'ai'},
        ])
    assert 'Piece 2' in str(unnamed.value) and '"ai"' in str(unnamed.value)
    # And a reconstruction is a repair of something: without the original
    # there is nothing to compare the model's reading against, which is the
    # whole difference between `ai-reconstructed` and `ai`.
    with pytest.raises(ValueError, match='original') as bare:
        peerreviewagents.write_manuscript(tmp_path / 'bare', [
            {'kind': 'title', 'text': 'Replay during quiet rest'},
            {'kind': 'paragraph', 'text': 'Rates rose.', 'source': 'ai-reconstructed'},
        ], ai_model='claude-sonnet-5-5')
    assert 'ai-reconstructed' in str(bare.value) and 'source "ai"' in str(bare.value)
    # A workspace is written once: the second write would renumber every line a
    # saved review cites.
    folder = tmp_path / 'twice'
    workspace(folder)
    with pytest.raises(ValueError, match='immutable manuscript'):
        workspace(folder)


# -- a fixture of a read manuscript, written by the Read stage ---------------
# The other direction: not a manuscript an application wrote, but one the Read
# stage wrote, which an application has to render. What that stage leaves in a
# map row — a re-read part's `part` and `of`, the converter's reading of a
# piece folded into another — came out of a model run and nowhere else, so a
# consumer's fixtures for it were hand-built JSON that nothing held against
# the engine. `scripted_read` is that fixture, written by the engine.

#: What the converter left for the Read stage to repair, in the shape it
#: writes a piece: an equation with a brace it never closed, a heading that is
#: really the edge of that equation, a table it read as one paragraph of
#: run-together cells behind a sentence of the paper, and a figure it has the
#: caption of and no image.
CONVERTER_LEFT = [
    {'kind': 'equation', 'text': 'E = m c^{2', 'latex': 'E = m c^{2', 'number': '(1)',
     'section': '3 Results', 'confidence': 1.0},
    {'kind': 'heading', 'text': 'with $\\{$', 'section': '3 Results'},
    {'kind': 'paragraph', 'section': '3 Results',
     'text': 'The current was logged every hour. Hour Current (mA) 1 4.5 2 9.0'},
    {'kind': 'caption', 'text': 'Figure 1: The bench, the instrument and the widgets.',
     'section': '3 Results'},
]

DESCRIBED = ('A photograph of the bench with the instrument clamped at the left and three '
             'widgets in a row; the scale divisions are 0.5 mm apart.')

NOTE = 'The table under the current paragraph came back as prose and is put back as a table.'


def test_the_read_stage_is_driven_in_code_for_a_fixture_to_render(tmp_path):
    """A sealed manuscript with every mark of the Read stage on it, and no model.

    This is the whole of what a consumer needs: the stage's own tools, by their
    own names, writing a folder it can then read with the same exports its
    users' runs leave behind.
    """
    folder = tmp_path / 'run'

    def reader(slot):
        # The equation and the figure are what the draft says is unaccounted
        # for; the paragraph of cells is not, which is why the reader reads the
        # listing against the page rather than the other way round.
        left = slot.read_draft()['outstanding']
        assert [row['kind'] for row in left['pieces']] == ['equation', 'figure']
        assert left['pages'] == [1, 2]
        slot.view_page(1)
        slot.view_page(2)
        slot.replace_equation(slot.find('E = m c^{2'), 1, 'E = m c^{2}', number='(1)')
        slot.describe_figure(slot.find('Figure 1: The bench'), 1, DESCRIBED)
        folded = slot.reread_piece(slot.find('logged every hour'), 1, [
            {'kind': 'paragraph', 'keep': 'The current was logged every hour.'},
            {'kind': 'table', 'rows': [['Hour', 'Current (mA)'], ['1', '4.5'], ['2', '9.0']]},
            {'kind': 'caption', 'text': 'Table 1: Current by hour.'},
        ], absorb=[slot.find('with $\\{$')])
        assert folded['kinds'] == ['paragraph', 'table', 'caption']
        assert slot.seal(note=NOTE)['sealed'] is True

    read = peerreviewagents.scripted_read(FIXTURE, folder, reader, pieces=CONVERTER_LEFT)

    assert read.status == 'complete'
    assert read.sealed['statistics']['lines']['paper.md'] > 0
    base = read.base()
    assert base.folder == read.workspace == folder / peerreviewagents.MANUSCRIPT_DIRECTORY
    # The three marks a consumer renders. The parts of one re-read, each one
    # saying which part of what it is:
    parts = [row for row in base.rows if (row.get('original') or {}).get('of')]
    assert [(row['original']['part'], row['original']['of']) for row in parts] == [(1, 3), (2, 3),
                                                                                   (3, 3)]
    assert len({row['original']['piece'] for row in parts}) == 1
    # the converter's whole reading of the piece folded into one of them, on
    # one of them, so that a consumer shows one fold once:
    held = [row for row in base.rows if (row.get('original') or {}).get('absorbed')]
    assert len(held) == 1 and held[0]['kind'] == 'table'
    fold = held[0]['original']['absorbed']
    assert len(fold) == 1 and fold[0]['kind'] == 'heading' and fold[0]['text'] == 'with $\\{$'
    assert fold[0]['piece'] not in {row['original']['piece'] for row in parts}
    assert 'with $\\{$' not in base.text
    # and the reader's note, which is served above the manuscript.
    assert base.manifest['ai_read']['note'] == NOTE
    assert base.manifest['ai_read']['read'] is True


def test_a_refused_reader_call_is_raised_rather_than_answered(tmp_path):
    """A script has no turn to take again, so a refusal stops it.

    The tools answer a refusal to a model, which reads it and tries something
    else. A fixture that read past one would seal a manuscript missing the
    repair it thinks it made, so the refusal is an exception with the server's
    own answer on it.
    """
    def reader(slot):
        mangled = slot.find('E = m c^{2')
        # A repair is a transcription of a page, and this script has not opened
        # one yet.
        with pytest.raises(peerreviewagents.ReaderRefused) as refused:
            slot.replace_equation(mangled, 1, 'E = m c^{2}', number='(1)')
        assert refused.value.tool == 'replace_equation'
        assert 'request_rejected' in refused.value.answer
        # And a piece is named by what the converter read there, so a fixture
        # that names nothing, or two things, stops rather than working on
        # whichever came first.
        with pytest.raises(LookupError, match='in 0 pieces'):
            slot.find('a sentence this paper does not hold')
        slot.view_page(1)
        assert slot.replace_equation(mangled, 1, 'E = m c^{2}', number='(1)')['piece'] == mangled
        # Caught inside the script, the stage goes on and the run seals.
        scripted.DEFAULT_SCRIPTS['reader'](slot)

    read = peerreviewagents.scripted_read(FIXTURE, tmp_path / 'run', reader,
                                          pieces=CONVERTER_LEFT)

    assert read.sealed['sealed'] is True
    # A refusal the script does not catch is the exception the run ends on.
    def refuser(slot):
        slot.replace_equation(slot.find('E = m c^{2'), 1, 'E = m c^{2}', number='(1)')

    with pytest.raises(peerreviewagents.ReaderRefused, match='replace_equation'):
        peerreviewagents.scripted_read(FIXTURE, tmp_path / 'again', refuser,
                                       pieces=CONVERTER_LEFT)
