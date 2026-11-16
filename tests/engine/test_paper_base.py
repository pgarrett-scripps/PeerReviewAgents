"""The manuscript base: one Markdown file, one map row per piece, no ids.

What these tests hold in place is the thing the format exists for — that the
text a reviewer is handed is text, that every line of it belongs to exactly one
piece, and that the page, the box and the fingerprint are in the map where a
reader never has to look at them. Four of them pin faults that were real:

* a displayed equation reaches the Markdown. The converter returns an equation
  with an empty ``text`` and its LaTeX under ``math``, and the importer
  returned early on empty text, so every equation in every imported paper was
  silently absent — eleven of eleven in the antenna manuscript.
* the normal form folds what a reader cannot see, so a quote retyped with a
  hyphen where the paper printed a minus is the same quote.
* a folder of blocks is refused by name instead of half-read, because a line
  number taken from a folder that has no lines is invented.
* a supplement is a document of its own, which the importer passed through to
  nothing before.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from peerreviewagents.engine import ingest, paper_base, paper_text

FIXTURE = Path(__file__).resolve().parents[1] / 'fixtures' / 'single-column.pdf'

#: One document of a Word supplement: a paragraph and a two-row table.
DOCX_BODY = """<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
 <w:body>
  <w:p><w:r><w:t>Widgets were weighed on the bench scale.</w:t></w:r></w:p>
  <w:tbl>
   <w:tr><w:tc><w:p><w:r><w:t>Batch</w:t></w:r></w:p></w:tc>
         <w:tc><w:p><w:r><w:t>Mass</w:t></w:r></w:p></w:tc></w:tr>
   <w:tr><w:tc><w:p><w:r><w:t>A</w:t></w:r></w:p></w:tc>
         <w:tc><w:p><w:r><w:t>12 g</w:t></w:r></w:p></w:tc></w:tr>
  </w:tbl>
 </w:body>
</w:document>"""


def fake_conversion(title='Antenna beamforming', number='1'):
    """What the pinned converter returns for a page holding one equation."""
    return '', {'title': title, 'blocks': [
        {'kind': {'type': 'title'}, 'text': title, 'page': 0, 'bbox': None},
        {'kind': {'type': 'heading', 'level': 1}, 'text': 'Methods', 'page': 0, 'bbox': None},
        {'kind': {'type': 'paragraph'}, 'text': 'The beamformer weights each tile.',
         'page': 0, 'bbox': {'x0': 10.0, 'y0': 20.0, 'x1': 30.0, 'y1': 40.0}},
        {'kind': {'type': 'equation'}, 'text': '', 'page': 0,
         'bbox': {'x0': 280.0, 'y0': 553.0, 'x1': 331.0, 'y1': 565.0},
         'math': {'latex': 'T_b^{BF} = \\phi x_b', 'number': number, 'confidence': 0.4}},
        {'kind': {'type': 'reference'}, 'text': 'A Author, A paper, 2020.',
         'page': 0, 'bbox': None, 'reference': {'label': '1', 'year': '2020'}},
    ]}


def write_docx(path, body=DOCX_BODY):
    """A Word file with just the part the importer reads."""
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('word/document.xml', body)
    return path


def test_the_normal_form_folds_what_a_reader_cannot_see():
    printed = ('The interval −10–−5 dB was efﬁcient; the author’s '
               'own soft­hyphen stayed.')
    typed = "The interval -10--5 dB was efficient; the author's own softhyphen stayed."
    assert paper_text.normalised(printed) == typed
    assert paper_text.fingerprint(printed) == paper_text.fingerprint(typed)
    # A decomposed accent and a precomposed one are one word, not two.
    assert paper_text.normalised('Néel') == paper_text.normalised('Néel')
    # Case is not folded: a quote is a quote, not something like it.
    assert paper_text.normalised('Widget') != paper_text.normalised('widget')


def test_offsets_point_back_at_the_printed_characters():
    printed = 'first row\n   second   row'
    normal, offsets = paper_text.normalise(printed)
    assert normal == 'first row second row'
    found = normal.index('row second')
    start, end = offsets[found], offsets[found + len('row second') - 1]
    assert printed[start:end + 1] == 'row\n   second'
    assert len(offsets) == len(normal)


def test_composed_lines_are_one_piece_each_and_carry_no_ids():
    markdown, rows = paper_base.compose([
        {'kind': 'title', 'text': 'A title', 'section': 'Front matter', 'page': 1},
        {'kind': 'heading', 'text': 'Methods', 'level': 1, 'section': 'Methods'},
        {'kind': 'paragraph', 'text': 'One sentence.\nBroken by the column gap.',
         'section': 'Methods', 'page': 2, 'bbox': {'x0': 1, 'y0': 2, 'x1': 3, 'y1': 4}},
        {'kind': 'equation', 'latex': 'a = b', 'number': '2', 'section': 'Methods',
         'confidence': 0.4},
        {'kind': 'figure', 'text': 'Figure 1. The array.', 'section': 'Methods'},
        {'kind': 'paragraph', 'text': '   ', 'section': 'Methods'},
    ])
    assert markdown.splitlines() == [
        '# A title', '', '## Methods', '', 'One sentence. Broken by the column gap.', '',
        '$$ a = b \\tag{2} $$', '', 'Figure: Figure 1. The array.']
    assert [row['lines'] for row in rows] == [[1, 1], [3, 3], [5, 5], [7, 7], [9, 9]]
    assert '{#' not in markdown and 'b0001' not in markdown
    assert rows[2]['bbox'] == [1.0, 2.0, 3.0, 4.0]
    assert rows[3]['confidence'] == 0.4
    assert all(row['source'] == 'text' for row in rows)
    assert rows[0]['fingerprint'] == paper_text.fingerprint('# A title')


def test_a_table_owns_every_row_it_printed(tmp_path):
    rows = paper_base.write_base(tmp_path, {paper_base.MAIN_DOCUMENT: [
        {'kind': 'paragraph', 'text': 'Before the table.', 'section': 'Results'},
        {'kind': 'table', 'text': '', 'section': 'Results', 'page': 3,
         'table': [['Batch', 'Mass'], ['A', '12 g'], ['B', '13 | g']]},
    ]})
    base = paper_base.load(tmp_path)
    assert base.text.splitlines()[2:] == [
        '| Batch | Mass |', '| --- | --- |', '| A | 12 g |', '| B | 13 \\| g |']
    assert rows[1]['lines'] == [3, 6]
    # Every line of the table answers with the table, so a citation of one row
    # still reports the page the table is printed on.
    assert {base.piece_at(line)['lines'][0] for line in (3, 4, 5, 6)} == {3}
    assert base.piece_at(6)['page'] == 3
    assert base.piece_at(1)['kind'] == 'paragraph'
    assert base.piece_at(2) is None
    assert base.piece_at(99) is None


def test_import_writes_a_markdown_base_over_the_fixture(tmp_path):
    folder = tmp_path / 'manuscript'
    ingest.import_paper(str(FIXTURE), folder)
    base = paper_base.load(folder)
    text = (folder / 'paper.md').read_bytes()
    assert b'\r' not in text
    assert base.title == 'Reliable Widget Measurements'
    assert base.piece_at(1)['kind'] == 'title'
    assert base.text.startswith('# Reliable Widget Measurements\n\n')
    # Every line that is not a separator belongs to exactly one piece, and the
    # piece's fingerprint is the fingerprint of the lines as written.
    for number, line in enumerate(base.text.splitlines(), 1):
        piece = base.piece_at(number)
        assert (piece is None) == (not line.strip())
    for row in base.rows:
        start, end = row['lines']
        assert row['fingerprint'] == paper_text.fingerprint(base.range_text(start, end))
    # The source is kept where the manifest says it is, byte for byte.
    manifest = base.manifest
    assert manifest['schema_version'] == paper_base.BASE_SCHEMA_VERSION
    assert (folder / manifest['source']['file']).read_bytes() == FIXTURE.read_bytes()
    assert manifest['source']['sha256'] == ingest.digest(FIXTURE.read_bytes())
    assert manifest['counts']['pieces'] == len(base.rows) == manifest['counts']['paragraph'] \
        + manifest['counts']['heading'] + manifest['counts']['title'] \
        + manifest['counts']['reference']
    assert manifest['pieces_per_source'] == {'text': len(base.rows)}
    assert manifest['documents'] == [paper_base.MAIN_DOCUMENT]
    for name, stamp in manifest['files'].items():
        assert ingest.digest((folder / name).read_bytes()) == stamp
    # References are one to a line, in their own section.
    references = [row for row in base.rows if row['kind'] == 'reference']
    assert references and all(row['section'] == 'References' for row in references)
    assert all(row['lines'][0] == row['lines'][1] for row in references)


def test_the_page_geometry_is_recorded_so_a_box_can_be_placed(tmp_path):
    """A box is four numbers, and four numbers without a page are unplaceable.

    An application showing a page image draws a highlight by scaling the box to
    the image, which it cannot do without the page's own size, and it has to
    know which corner the numbers are measured from. Both were missing from the
    manifest, so an app had to guess; the numbers asserted here were checked
    against ``pdftotext -bbox`` over this same file, which prints the page size
    and every word's box in the same points from the same corner.
    """
    folder = tmp_path / 'manuscript'
    ingest.import_paper(str(FIXTURE), folder)
    manifest = paper_base.load(folder).manifest
    assert manifest['pages'] == [
        {'page': 1, 'width': 612.0, 'height': 792.0, 'rotation': 0},
        {'page': 2, 'width': 612.0, 'height': 792.0, 'rotation': 0}]
    # One page record per page of the source, numbered as a piece numbers its
    # page, so a row's `page` indexes this list by its own number.
    assert len(manifest['pages']) == manifest['source']['pages']
    sizes = {page['page']: page for page in manifest['pages']}
    boxed = [row for row in paper_base.load(folder).rows if row['bbox']]
    assert boxed
    for row in boxed:
        page = sizes[row['page']]
        x0, y0, x1, y1 = row['bbox']
        assert 0 <= x0 < x1 <= page['width']
        assert 0 <= y0 < y1 <= page['height']
    # And the origin is the top-left corner: the title is printed above the
    # abstract, so its box is the one with the smaller y.
    first_page = [row for row in boxed if row['page'] == 1]
    title = next(row for row in first_page if row['kind'] == 'title')
    below = next(row for row in first_page if row['kind'] == 'heading')
    assert title['bbox'][1] < below['bbox'][1]
    assert 'top-left' in manifest['box_convention'] and 'points' in manifest['box_convention']


def test_an_equation_reaches_the_markdown_with_its_latex(tmp_path, monkeypatch):
    rustypaper = pytest.importorskip('rustypaper')
    monkeypatch.setattr(rustypaper, 'convert', lambda path, *a, **k: fake_conversion())
    folder = tmp_path / 'manuscript'
    ingest.import_paper(str(FIXTURE), folder)
    base = paper_base.load(folder)
    equations = [row for row in base.rows if row['kind'] == 'equation']
    assert len(equations) == 1
    start, end = equations[0]['lines']
    assert base.range_text(start, end) == '$$ T_b^{BF} = \\phi x_b \\tag{1} $$'
    # The converter's own confidence travels with the piece: it is what tells a
    # reader whether to open the page, and it is wrong often enough to matter.
    assert equations[0]['confidence'] == 0.4
    assert equations[0]['page'] == 1
    assert equations[0]['bbox'] == [280.0, 553.0, 331.0, 565.0]


def test_an_unnumbered_equation_is_written_without_a_tag(tmp_path, monkeypatch):
    rustypaper = pytest.importorskip('rustypaper')
    monkeypatch.setattr(rustypaper, 'convert', lambda path, *a, **k: fake_conversion(number=''))
    folder = tmp_path / 'manuscript'
    ingest.import_paper(str(FIXTURE), folder)
    base = paper_base.load(folder)
    equation = next(row for row in base.rows if row['kind'] == 'equation')
    assert base.range_text(*equation['lines']) == '$$ T_b^{BF} = \\phi x_b $$'


def test_a_reference_keeps_the_box_it_was_printed_in(tmp_path, monkeypatch):
    rustypaper = pytest.importorskip('rustypaper')
    _, document = fake_conversion()
    document['blocks'][-1]['bbox'] = {'x0': 50.0, 'y0': 60.0, 'x1': 70.0, 'y1': 80.0}
    monkeypatch.setattr(rustypaper, 'convert', lambda path, *a, **k: ('', document))
    folder = tmp_path / 'manuscript'
    ingest.import_paper(str(FIXTURE), folder)
    base = paper_base.load(folder)
    reference = next(row for row in base.rows if row['kind'] == 'reference')
    assert reference['bbox'] == [50.0, 60.0, 70.0, 80.0]
    assert reference['page'] == 1


def test_every_figure_caption_is_written_the_same_way():
    """The converter's two kinds for one thing do not reach the page.

    A figure's caption arrives as a ``figure`` piece or as a ``caption`` piece
    depending on what the converter saw around it — this paper's nine figures
    arrive eight one way and one the other — and a reader scanning for figures,
    or a search for one, should see nine alike. A caption that is not a
    figure's keeps its own word.
    """
    markdown, rows = paper_base.compose([
        {'kind': 'figure', 'text': 'Fig. 1: The array.', 'section': 'Results'},
        {'kind': 'caption', 'text': 'Fig. 2: The spectrum.', 'section': 'Results'},
        {'kind': 'caption', 'text': '(Figure 3) The mask.', 'section': 'Results'},
        {'kind': 'caption', 'text': 'Table 2: Measured gain by subcarrier.',
         'section': 'Results'},
        {'kind': 'figure', 'text': 'Fig. 4: The window.', 'section': 'Results',
         'image': 'figures/fig4.png', 'label': 'Figure 4'},
    ])
    assert [line for line in markdown.splitlines() if line] == [
        'Figure: Fig. 1: The array.',
        'Figure: Fig. 2: The spectrum.',
        'Figure: (Figure 3) The mask.',
        'Caption: Table 2: Measured gain by subcarrier.',
        '![Figure 4](figures/fig4.png)',
        'Figure: Fig. 4: The window.']
    # The kind the converter chose is still on the row, for a consumer that
    # needs it; what changed is only how the line reads.
    assert [row['kind'] for row in rows] == ['figure', 'caption', 'caption', 'caption', 'figure']


def test_a_figure_the_text_cites_with_no_caption_is_a_named_gap():
    captions = [{'kind': 'figure', 'text': 'Fig. 1: The array.'},
                {'kind': 'caption', 'text': 'Figure 2. The spectrum.'}]
    body = [{'kind': 'paragraph', 'text': 'As Fig. 1 and Figure 3 show, and see Figs. 2.'}]
    assert ingest.missing_figures(captions + body) == ([3], [1, 2])
    # Nothing is reported when every cited figure is described, whichever kind
    # the converter happened to give the caption.
    assert ingest.missing_figures(captions + [{'kind': 'paragraph', 'text': 'Fig. 2 shows.'}]) \
        == ([], [1, 2])


def test_a_folder_of_blocks_is_refused_by_name(tmp_path):
    (tmp_path / 'paper.json').write_text('{"blocks": []}', encoding='utf-8')
    with pytest.raises(ValueError) as refusal:
        paper_base.load(tmp_path)
    assert 'paper.json' in str(refusal.value) and 'Re-import' in str(refusal.value)
    with pytest.raises(ValueError) as empty:
        paper_base.load(tmp_path / 'nothing')
    assert 'Not a manuscript folder' in str(empty.value)


def test_an_import_refuses_to_overwrite_a_base(tmp_path):
    folder = tmp_path / 'manuscript'
    ingest.import_paper(str(FIXTURE), folder)
    with pytest.raises(ValueError):
        ingest.import_paper(str(FIXTURE), folder)


def test_a_supplement_is_its_own_document(tmp_path):
    supplements = tmp_path / 'supplements'
    supplements.mkdir()
    write_docx(supplements / 'S1_Table.docx')
    folder = tmp_path / 'manuscript'
    ingest.import_paper(str(FIXTURE), folder, supplements=str(supplements))
    base = paper_base.load(folder)
    name = 'supplement-S1_Table.md'
    assert base.document_names() == [paper_base.MAIN_DOCUMENT, name]
    assert base.manifest['documents'] == [paper_base.MAIN_DOCUMENT, name]
    assert name in base.manifest['files']
    rows = base.pieces(name)
    assert [row['kind'] for row in rows] == ['title', 'supplement', 'table']
    assert base.line_text(rows[1]['lines'][0], name) == 'Widgets were weighed on the bench scale.'
    assert base.range_text(*rows[2]['lines'], document=name).splitlines() == [
        '| Batch | Mass |', '| --- | --- |', '| A | 12 g |']
    # The supplement's lines are its own: nothing of it is in the main document.
    assert 'bench scale' not in base.text
    assert all(row['document'] == paper_base.MAIN_DOCUMENT
               for row in base.pieces(paper_base.MAIN_DOCUMENT))
