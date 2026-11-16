"""The manuscript as a reviewer is served it: numbered lines, in as few parts as fit.

What these tests hold in place is the reading itself. A reviewer has no file
access, so if the served text is wrong the paper is simply not read: a part cut
by the harness loses the lines after the cut and nobody reviews them, a part
that ends inside a table makes the reader stitch, and an equation served as
plain text gets a finding written about algebra the converter mangled.

* no part can be truncated, because the part size is under the length Claude
  Code never counts twice. The measurement is in the module docstring of
  ``paper_view``; what is pinned here is that the constant stays under it.
* a part boundary is a piece boundary, and the parts together are every line of
  the document exactly once.
* every equation line carries its warning in the served text and not one
  character of it reaches ``paper.md``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from peerreviewagents.engine import ingest, paper_base, paper_text, paper_view

FIXTURE = Path(__file__).resolve().parents[1] / 'fixtures' / 'single-column.pdf'

#: Claude Code 2.1.325 never counts a tool result twice, and so never truncates
#: one, at or under half of its default 25,000-token cap, estimating four
#: characters to the token. A part has to stay under this.
NEVER_TRUNCATED = 50000


def stamped(folder, **manifest):
    """The manifest the seal writes beside the base.

    A workspace is three files, and the stamp is the one a reader acts on
    before anything else, so a folder written without it is refused rather
    than read as the current shape — which is what these fixtures were
    relying on.
    """
    paper_base.write(Path(folder) / 'manifest.json',
                     {'schema_version': paper_base.BASE_SCHEMA_VERSION, **manifest})


def base_with(pieces, folder, document=paper_base.MAIN_DOCUMENT):
    paper_base.write_base(folder, {document: pieces})
    stamped(folder)
    return paper_base.load(folder)


def test_a_part_cannot_be_truncated_by_the_harness():
    assert paper_view.PART_CHARACTERS < NEVER_TRUNCATED


def test_an_imported_paper_is_served_whole_with_its_line_numbers(tmp_path):
    ingest.import_paper(FIXTURE, tmp_path / 'ms', title='Widgets')
    base = paper_base.load(tmp_path / 'ms')
    assert paper_view.served(base) == 1
    served = paper_view.render(base)
    assert served.startswith(paper_base.MAIN_DOCUMENT + ' - part 1 of 1 - lines 1-'
                             + str(base.line_count()) + ' of ' + str(base.line_count()))
    assert 'Read every line of this before anything else.' in served
    assert served.rstrip().endswith('End of paper.md, line ' + str(base.line_count()) + '.')
    # Every line of the file is in the answer, behind its own number.
    for number in (1, base.line_count() // 2, base.line_count()):
        assert '\n' + '{:>5}| '.format(number) + base.line_text(number) in served
    assert len(served) < NEVER_TRUNCATED


def test_the_parts_are_every_line_once_and_break_between_pieces(tmp_path):
    pieces = [{'kind': 'paragraph', 'text': 'Paragraph ' + str(n) + '. ' + 'word ' * 60,
               'section': 'Results'} for n in range(40)]
    pieces.insert(20, {'kind': 'table', 'section': 'Results',
                       'table': [['Batch', 'Mass'], ['A', '12 g'], ['B', '13 g']]})
    base = base_with(pieces, tmp_path)
    ranges = paper_view.parts(base, budget=4000)
    assert len(ranges) > 3
    # Together they are 1..N, in order, with nothing repeated and nothing lost.
    assert ranges[0][0] == 1 and ranges[-1][1] == base.line_count()
    assert all(later[0] == earlier[1] + 1 for earlier, later in zip(ranges, ranges[1:]))
    for start, _ in ranges[1:]:
        piece = base.piece_at(start)
        assert piece is not None and piece['lines'][0] == start, 'a part began inside a piece'
    table = next(row for row in base.pieces() if row['kind'] == 'table')
    assert not any(start > table['lines'][0] and start <= table['lines'][1]
                   for start, _ in ranges), 'the table was cut across two parts'


def test_a_part_that_is_not_the_last_sends_the_reader_to_the_next(monkeypatch, tmp_path):
    pieces = [{'kind': 'paragraph', 'text': 'Paragraph ' + str(n) + '. ' + 'word ' * 60,
               'section': 'Results'} for n in range(40)]
    base = base_with(pieces, tmp_path)
    monkeypatch.setattr(paper_view, 'PART_CHARACTERS', 4000)
    total = paper_view.served(base)
    assert total > 3
    first = paper_view.render(base, part=1)
    assert 'part 1 of ' + str(total) in first.splitlines()[0]
    assert 'Read it now: read_paper with part 2.' in first
    last = paper_view.render(base, part=total)
    assert 'End of paper.md, line ' + str(base.line_count()) + '.' in last
    seen = []
    for part in range(1, total + 1):
        seen += [line for line in paper_view.render(base, part=part).splitlines()
                 if line[:5].strip().isdigit()]
    assert len(seen) == base.line_count()


def test_an_equation_is_served_with_its_warning_and_the_page_to_open(tmp_path):
    base = base_with([
        {'kind': 'paragraph', 'text': 'The beamformer weights each tile.', 'section': 'Methods'},
        {'kind': 'equation', 'latex': 'T_b^{BF} = \\phi x_b', 'number': '1',
         'section': 'Methods', 'page': 4, 'confidence': 0.4},
    ], tmp_path)
    served = paper_view.render(base)
    equation = next(line for line in served.splitlines() if '$$' in line)
    assert equation.endswith('[machine-read equation, may be garbled; view_page 4]')
    assert 'inline $...$ maths were machine-read' in served
    # Not one character of the warning is in the file itself.
    paper = (tmp_path / paper_base.MAIN_DOCUMENT).read_text(encoding='utf-8')
    assert 'machine-read' not in paper and 'view_page' not in paper
    assert paper.splitlines()[2] == '$$ T_b^{BF} = \\phi x_b \\tag{1} $$'


def test_an_equation_with_no_page_still_carries_the_warning(tmp_path):
    base = base_with([{'kind': 'equation', 'latex': 'a = b', 'section': 'Methods'}], tmp_path)
    line = paper_view.render(base).splitlines()[-3]
    assert line.endswith('[machine-read equation, may be garbled]')


def read_base(pieces, folder, model='claude-sonnet-5-5'):
    """A base whose manifest says a model read the pages, as the seal writes it."""
    paper_base.write_base(folder, {paper_base.MAIN_DOCUMENT: pieces})
    stamped(folder, title='Widgets', ai_read={'read': True, 'reader_model': model})
    return paper_base.load(folder)


def test_an_equation_read_from_the_page_says_so_and_names_the_model(tmp_path):
    base = read_base([
        {'kind': 'equation', 'latex': 'E = m c^{2}', 'number': '1', 'section': 'Methods',
         'page': 4, 'source': 'ai-reconstructed', 'original': {'latex': 'E = m c^{2'}},
        {'kind': 'equation', 'latex': 'a = b', 'number': '2', 'section': 'Methods', 'page': 4},
    ], tmp_path)
    served = paper_view.render(base)
    repaired, untouched = (line for line in served.splitlines() if '$$' in line)
    assert repaired.endswith('[read from the page image of page 4 by claude-sonnet-5-5;'
                             " the converter's reading is in the map]")
    # An equation nobody had to repair still carries the converter's warning.
    assert untouched.endswith('[machine-read equation, may be garbled; view_page 4]')
    # And the markers are served, never written into the manuscript.
    paper = (tmp_path / paper_base.MAIN_DOCUMENT).read_text(encoding='utf-8')
    assert 'page image' not in paper and 'claude-sonnet-5-5' not in paper


def test_a_figure_description_is_served_as_a_model_reading_the_image(tmp_path):
    base = read_base([
        {'kind': 'caption', 'text': 'Figure 1: Gain against frequency.', 'section': 'Results',
         'page': 3},
        {'kind': 'figure-description', 'text': 'Gain in dBi on the vertical axis against '
         'frequency in GHz, rising from 2 to 9 dBi across 1 to 4 GHz.',
         'section': 'Results', 'page': 3, 'source': 'ai', 'original': {}},
    ], tmp_path)
    served = paper_view.render(base)
    caption, description = [line for line in served.splitlines()
                            if line[:5].strip().isdigit() and line[7:].strip()]
    assert caption.endswith('Figure 1: Gain against frequency.'), 'the caption is the authors\''
    assert 'Figure description: Gain in dBi' in description
    assert description.endswith('[AI description of the figure image of page 3 '
                                'by claude-sonnet-5-5]')
    assert 'not the authors’ words' in served.replace("'", '’'), 'the header says what it is'


def test_a_table_the_reader_added_is_marked_once_and_not_on_every_row(tmp_path):
    base = read_base([
        {'kind': 'caption', 'text': 'Table 1: Readings by day.', 'section': 'Results', 'page': 2},
        {'kind': 'table', 'table': [['Day', 'Reading'], ['1', '4.2'], ['2', '4.4']],
         'section': 'Results', 'page': 2, 'source': 'ai', 'original': {}},
    ], tmp_path)
    marked = [line for line in paper_view.render(base).splitlines()
              if 'the converter missed it' in line]
    assert len(marked) == 1, 'the marker belongs on the first line of the table, not every row'
    assert marked[0].endswith('[read from the page image of page 2 by claude-sonnet-5-5;'
                              ' the converter missed it]')
    assert 'Day' in marked[0]


def test_a_paragraph_off_a_lost_page_is_marked_like_everything_else(tmp_path):
    """The marker was served for equations, tables and descriptions only.

    A page the converter returned no text from is transcribed by the reader as
    paragraphs, captions and headings — ``add_piece`` writes exactly those —
    and those reached the reviewer looking like the converter's own text. The
    line a reviewer is least able to check is the one that said nothing about
    where it came from.
    """
    base = read_base([
        {'kind': 'heading', 'text': 'Discussion', 'level': 1, 'section': 'Discussion',
         'page': 7, 'source': 'ai', 'original': {}},
        {'kind': 'paragraph', 'text': 'The gain holds across the band.', 'section': 'Discussion',
         'page': 7, 'source': 'ai', 'original': {}},
        {'kind': 'caption', 'text': 'Figure 3: Gain against angle.', 'section': 'Discussion',
         'page': 7, 'source': 'ai', 'original': {}},
        {'kind': 'paragraph', 'text': 'The converter read this one.', 'section': 'Discussion',
         'page': 8},
    ], tmp_path)
    served = [line for line in paper_view.render(base).splitlines()
              if line[:5].strip().isdigit() and line[7:].strip()]
    transcribed, converted = served[:3], served[3]
    for line in transcribed:
        assert line.endswith('[read from the page image of page 7 by claude-sonnet-5-5;'
                             ' the converter missed it]'), line
    assert converted.endswith('The converter read this one.'), 'the authors\' text is unmarked'


def test_a_base_an_application_composed_with_a_model_says_so_in_its_header(tmp_path):
    """No stage here read a page, so the header cannot claim one did.

    ``write_manuscript`` takes pieces a model wrote and names the model. The
    reviewer still needs the marker and the header, but the Read stage's
    header would say a model looked at every page of this paper, which is
    false for a base composed from a source file nobody rendered.
    """
    paper_base.write_base(tmp_path, {paper_base.MAIN_DOCUMENT: [
        {'kind': 'paragraph', 'text': 'The authors wrote this.', 'section': 'Results'},
        {'kind': 'paragraph', 'text': 'A model wrote this one.', 'section': 'Results',
         'source': 'ai', 'original': {}},
    ]})
    stamped(tmp_path, title='Widgets',
            ai_read={'read': False, 'reason': 'composed by the caller',
                     'ai_model': 'claude-sonnet-5-5', 'ai_pieces': 1})
    base = paper_base.load(tmp_path)
    served = paper_view.render(base)
    assert 'No stage of this review read the pages' in served
    assert 'written by a model' in served
    marked = [line for line in served.splitlines() if 'claude-sonnet-5-5' in line]
    # Named, and no claim of a page image: nothing here opened one, so the
    # Read stage's wording would be telling the reviewer to go and compare
    # the line against a page that does not exist.
    assert len(marked) == 1 and marked[0].endswith('[written by claude-sonnet-5-5]'), marked
    assert 'page image' not in served.split('\n\n')[-1]


def test_a_part_is_measured_with_the_markers_it_will_be_served_with(tmp_path):
    pieces = [{'kind': 'equation', 'latex': 'a_{' + str(n) + '} = b', 'number': str(n),
               'section': 'Methods', 'page': 1, 'source': 'ai-reconstructed',
               'original': {'latex': 'a'}} for n in range(30)]
    base = read_base(pieces, tmp_path)
    widths = paper_view.line_width(base, paper_base.MAIN_DOCUMENT)
    served = [line for line in paper_view.render(base).splitlines()
              if line[:5].strip().isdigit()]
    assert [len(line) + 1 for line in served] == widths


def test_search_answers_with_the_line_and_what_it_belongs_to(tmp_path):
    base = base_with([
        {'kind': 'paragraph', 'text': 'The interval −10–−5 dB was efﬁcient.',
         'section': 'Results', 'page': 2},
        {'kind': 'paragraph', 'text': 'A second statement about widgets.',
         'section': 'Discussion', 'page': 3},
    ], tmp_path)
    # Typed with a hyphen and a plain "fi", as a reviewer retypes it.
    matches, more = paper_view.search(base, 'interval -10--5 dB was efficient')
    assert more is False
    assert [(row['line'], row['section'], row['page']) for row in matches] == [(1, 'Results', 2)]
    assert matches[0]['text'] == base.line_text(1)
    assert paper_view.search(base, 'WIDGETS')[0][0]['line'] == 3
    assert paper_view.search(base, 'a phrase the paper never printed') == ([], False)


def test_search_stops_at_its_limit_and_says_there_is_more(tmp_path):
    base = base_with([{'kind': 'paragraph', 'text': 'The widget was weighed.',
                       'section': 'Results'} for _ in range(6)], tmp_path)
    matches, more = paper_view.search(base, 'widget', limit=4)
    assert more is True and len(matches) == 4
    assert [row['line'] for row in matches] == [1, 3, 5, 7]


def test_a_supplement_is_served_by_name_and_the_main_paper_says_so(tmp_path):
    paper_base.write_base(tmp_path, {
        paper_base.MAIN_DOCUMENT: [{'kind': 'paragraph', 'text': 'See the supplement.',
                                    'section': 'Results'}],
        'supplement-s1.md': [{'kind': 'paragraph', 'text': 'Widgets were weighed on the bench.',
                              'section': 'S1'}]})
    stamped(tmp_path)
    base = paper_base.load(tmp_path)
    assert 'Also in this manuscript: supplement-s1.md' in paper_view.render(base)
    supplement = paper_view.render(base, 'supplement-s1.md')
    assert supplement.startswith('supplement-s1.md - part 1 of 1 - lines 1-1 of 1')
    assert '    1| Widgets were weighed on the bench.' in supplement


def test_a_document_or_a_part_that_is_not_there_says_what_is(tmp_path):
    base = base_with([{'kind': 'paragraph', 'text': 'One line.', 'section': 'Results'}], tmp_path)
    with pytest.raises(ValueError) as unknown:
        paper_view.render(base, 'supplement-s9.md')
    assert 'has no document named supplement-s9.md' in str(unknown.value)
    assert 'It holds: paper.md' in str(unknown.value)
    with pytest.raises(ValueError) as beyond:
        paper_view.render(base, part=2)
    assert 'served in 1 part, so there is no part 2' in str(beyond.value)


def test_the_served_text_quotes_the_file_character_for_character(tmp_path):
    """A quote is taken from what the reviewer read, so the two must agree.

    The gutter is the only thing added, and the line after it is the file's own
    line — not a normalised one, because the normal form is for matching and
    the manuscript's own characters are what a finding records.
    """
    printed = 'The interval −10–−5 dB was efﬁcient; the author’s own words.'
    base = base_with([{'kind': 'paragraph', 'text': printed, 'section': 'Results'}], tmp_path)
    body = [line for line in paper_view.render(base).splitlines() if line[:5].strip().isdigit()]
    assert body == ['    1| ' + printed]
    assert paper_text.normalised(body[0][7:]) == paper_text.normalised(base.line_text(1))
