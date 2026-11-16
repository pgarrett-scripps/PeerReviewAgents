"""Anchoring a reviewer's quote in the manuscript, and the refusals."""
from __future__ import annotations

from peerreviewagents.engine import paper_base, paper_text, quotes


def base_from(lines, rows=None, document=paper_base.MAIN_DOCUMENT):
    """A PaperBase over text written by hand, with one row per non-blank line."""
    text = '\n'.join(lines) + '\n'
    if rows is None:
        rows = [{'lines': [number, number], 'kind': 'paragraph', 'section': 'Body',
                 'page': 1, 'bbox': [0.0, 0.0, 1.0, 1.0], 'source': 'text',
                 'fingerprint': paper_text.fingerprint(line), 'document': document}
                for number, line in enumerate(lines, 1) if line.strip()]
    return paper_base.PaperBase('/nowhere', {document: text}, rows, {'title': 'Hand-written'})


def test_a_quote_is_placed_without_being_given_a_line():
    base = base_from(['The first paragraph says one thing.', '',
                      'The second paragraph says another thing.'])
    found = quotes.anchor(base, 'says another thing')['placement']
    assert found['line'] == 3
    assert found['end_line'] == 3
    assert found['start'] == 21
    assert base.line_text(3)[found['start']:found['end']] == 'says another thing'
    assert found['page'] == 1 and found['section'] == 'Body' and found['kind'] == 'paragraph'


def test_a_quote_typed_loosely_still_lands_on_the_printed_characters():
    # An en dash for the hyphen, a curly apostrophe, a non-breaking space, a
    # ligature and a soft hyphen inside a word: none of them is a difference a
    # reader can see, and none of them may decide whether a true quote cites.
    printed = 'The en­dash–capped ﬁlter’s gain is 3 dB.'
    base = base_from([printed])
    found = quotes.anchor(base, "The endash-capped filter's gain is 3 dB")
    assert 'refused' not in found, found.get('refused')
    place = found['placement']
    # The offsets are the file's, so the slice is what the author wrote.
    assert printed[place['start']:place['end']] == printed[:printed.index('.')]
    assert paper_text.normalised(place['text']).endswith('3 dB')
    assert place['quoted_exactly'] is True


def test_a_quote_that_differs_only_in_case_is_placed_and_marked_inexact():
    base = base_from(['The Gain Is 3 dB in the best case.'])
    place = quotes.anchor(base, 'the gain is 3 db')['placement']
    assert place['quoted_exactly'] is False
    # What is recorded is the manuscript's own characters, not the reviewer's.
    assert place['text'] == 'The Gain Is 3 dB'
    assert quotes.anchor(base, 'The Gain Is 3 dB')['placement']['quoted_exactly'] is True


def test_a_case_fold_that_lengthens_a_character_still_reports_the_right_span():
    # 'ß'.casefold() is two characters: a fold that forgot to carry the index
    # of each one back would report a span that slid along the line.
    base = base_from(['Die Strasse und die Straße sind gleich lang.'])
    assert quotes.anchor(base, 'DIE STRASSE UND')['placement']['text'] == 'Die Strasse und'
    # The second one is spelled with the ligature, and the span still ends on
    # the right word rather than sliding one character along.
    place = quotes.anchor(base, 'die STRASSE sind')['placement']
    assert place['text'] == 'die Straße sind'
    assert place['start'] == 16
    assert quotes.anchor(base, 'straße sind gleich')['placement']['text'] \
        == 'Straße sind gleich'
    # Folded, the two spellings are the same words, so quoting one of them
    # without a line is ambiguous and says so.
    assert quotes.anchor(base, 'DIE STRASSE')['lines'] == [1, 1]


def test_a_quote_over_a_line_break_is_one_quote():
    base = base_from(['| Method | Recall |', '| --- | --- |', '| ours | 0.91 |'])
    place = quotes.anchor(base, 'ours | 0.91')['placement']
    assert place['line'] == 3
    # And a span that genuinely crosses lines reports both ends.
    across = quotes.anchor(base, 'Recall | | --- | --- | | ours')['placement']
    assert (across['line'], across['end_line']) == (1, 3)
    assert '\n' in across['text']


def test_the_same_words_in_two_places_are_refused_until_a_line_says_which():
    base = base_from(['The gain is 3 dB.', '', 'Table 1. The gain is 3 dB.'])
    refusal = quotes.anchor(base, 'The gain is 3 dB')
    assert 'placement' not in refusal
    assert refusal['lines'] == [1, 3]
    assert 'occurs 2 times' in refusal['refused']
    assert quotes.anchor(base, 'The gain is 3 dB', line=3)['placement']['line'] == 3
    assert quotes.anchor(base, 'The gain is 3 dB', line=1, end_line=3)['lines'] == [1, 3]


def test_a_quote_absent_from_the_paper_is_refused_with_the_nearest_lines():
    base = base_from(['We evaluate on the held-out split of the corpus.', '',
                      'We report recall at a fixed precision of 0.9.'])
    refusal = quotes.anchor(base, 'we report precision at a fixed recall of 0.9')
    assert 'placement' not in refusal
    assert refusal['nearest'][0]['line'] == 3
    assert refusal['nearest'][0]['shared'] == 'at a fixed'
    assert 'not in paper.md' in refusal['refused']
    assert 'line 3 shares 3 words' in refusal['refused']


def test_a_quote_on_a_line_other_than_the_one_given_names_the_line_it_is_on():
    base = base_from(['The first line.', '', 'The second line.'])
    refusal = quotes.anchor(base, 'The second line', line=1)
    assert refusal['lines'] == [3]
    assert 'but not on the lines given (1). They are on line 3.' in refusal['refused']


def test_a_line_number_outside_the_document_refuses_rather_than_wrapping():
    base = base_from(['Only one line here.'])
    for line, end in ((0, None), (2, None), (1, 99), (1, 0)):
        refusal = quotes.anchor(base, 'one line here', line=line, end_line=end)
        assert 'placement' not in refusal, (line, end)
        assert 'lines given' in refusal['refused'] or 'not in paper.md' in refusal['refused']


def test_an_empty_quote_and_an_unknown_document_are_each_named():
    base = base_from(['Something to quote.'])
    assert 'has to quote the manuscript' in quotes.anchor(base, '   ')['refused']
    missing = quotes.anchor(base, 'Something', document='supplement-a.md')
    assert missing['documents'] == ['paper.md']
    assert 'no document named supplement-a.md' in missing['refused']


def test_a_quote_inside_a_table_or_an_equation_carries_that_piece():
    lines = ['| Method | Recall |', '| --- | --- |', '| ours | 0.91 |', '',
             '$$ E = mc^2 \\tag{1} $$']
    rows = [{'lines': [1, 3], 'kind': 'table', 'section': 'Results', 'page': 4,
             'bbox': [1.0, 2.0, 3.0, 4.0], 'source': 'text', 'document': 'paper.md',
             'fingerprint': 'x'},
            {'lines': [5, 5], 'kind': 'equation', 'section': 'Results', 'page': 5,
             'bbox': None, 'source': 'text', 'document': 'paper.md', 'fingerprint': 'y'}]
    base = base_from(lines, rows)
    row = quotes.anchor(base, 'ours | 0.91')['placement']
    assert (row['kind'], row['page'], row['lines']) == ('table', 4, [1, 3])
    equation = quotes.anchor(base, 'E = mc^2')['placement']
    assert (equation['kind'], equation['page']) == ('equation', 5)


def test_the_longest_shared_run_is_of_words_in_order():
    assert quotes.longest_run([], ['a']) == (0, 0)
    assert quotes.longest_run(['a', 'b', 'c'], ['x', 'a', 'b', 'y']) == (2, 0)
    assert quotes.longest_run(['a', 'b', 'c'], ['b', 'c', 'd']) == (2, 1)
    assert quotes.longest_run(['a', 'b'], ['b', 'a']) == (1, 0)


def test_the_projection_is_computed_once_per_document():
    base = base_from(['A line of text.'])
    first = base.projection()
    assert base.projection() is first
    normal, offsets, starts = first
    assert normal == 'A line of text.'
    assert starts == [0, 16]
    assert len(offsets) == len(normal)
