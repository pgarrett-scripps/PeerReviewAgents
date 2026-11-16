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

import pytest

import peerreviewagents
from peerreviewagents import workspace as workspace_tools
from peerreviewagents.engine.investigation import Investigation

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
    with pytest.raises(ValueError, match='rendered to a line'):
        peerreviewagents.write_manuscript(tmp_path / 'empty', [{'kind': 'paragraph', 'text': ''}],
                                          title='Nothing at all')
    # A workspace is written once: the second write would renumber every line a
    # saved review cites.
    folder = tmp_path / 'twice'
    workspace(folder)
    with pytest.raises(ValueError, match='immutable manuscript'):
        workspace(folder)
