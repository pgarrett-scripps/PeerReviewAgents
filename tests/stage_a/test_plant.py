"""The planter has to plant an error that contradicts something, or Stage A measures nothing.

These tests are about the choosing and the bookkeeping, which is where a
planted error goes wrong: a number altered where nothing disagrees with it is
not an error a reviewer could catch, and a key that names the wrong place turns
a real catch into a miss. The PDF layer is exercised through its own pure
helpers; `plant()` itself needs pikepdf and a real paper, and is used against
the corpus rather than a fixture.

The manuscript each test plans against is written to disk by
``build_base``, the same way an import writes one, so the lines the key names
are the lines the engine would compute rather than numbers a fixture asserted.
"""

from __future__ import annotations

import plant
from manuscripts import build_base


def piece(text: str, **extra) -> dict:
    return {'kind': 'paragraph', 'text': text, 'page': 1, **extra}


def manuscript(folder, pieces, references=None, **manifest):
    """One manuscript on disk, loaded, in the shape a planner reads it."""
    return build_base(folder, pieces, references=references or [], manifest=manifest or None)


def test_core_keeps_the_count_and_drops_the_bracket():
    assert plant.core('3,457,173 (43.12)') == '3,457,173'
    assert plant.core('0.078') == '0.078'
    assert plant.core('12%') == '12'


def test_table_rows_reads_the_cells_back_out_of_the_markdown():
    text = '| Group | Events |\n| --- | --- |\n| Control | 384 |\n| Treated | 164 |'
    assert plant.table_rows(text) == [['Group', 'Events'], ['Control', '384'],
                                      ['Treated', '164']]


def test_percentage_is_altered_against_the_n_stated_beside_it(tmp_path):
    text = ('Among the responders (n = 4850), 2808 (57.9%) reported the symptom at '
            'least once during follow-up.')
    base = manuscript(tmp_path / 'ms', [piece(text)])
    plans = plant.plans_for(base, 'statistic')
    assert plans, 'a percentage with its own n on the line is plantable'
    plan = plans[0]
    assert plan['rule'] == 'percentage-against-the-stated-n'
    assert plan['edits'][0]['original'] == '57.9'
    assert plan['edits'][0]['altered'] != '57.9'
    # The plan names a place a reviewer can point at, not an id.
    assert (plan['document'], plan['line']) == ('paper.md', 1)
    # The key must name the altered percentage and the count it no longer fits.
    assert set(plan['must_mention']) == {plan['edits'][0]['altered'], '2808'}
    assert plan['check']['total'] == 4850


def test_a_p_value_is_only_planted_where_the_sentence_claims_significance(tmp_path):
    claimed = manuscript(tmp_path / 'claimed',
                         [piece('The effect was statistically significant (P = .003).')])
    denied = manuscript(tmp_path / 'denied',
                        [piece('The effect was not significant (P = .003).')])
    assert plant.plans_for(claimed, 'statistic')
    assert not plant.plans_for(denied, 'statistic')


def test_a_year_is_not_mistaken_for_a_count(tmp_path):
    text = 'Between 1990 and 2010 the registry grew, as others have reported (n = 2000).'
    assert not plant.plans_for(manuscript(tmp_path / 'ms', [piece(text)]), 'statistic')


def test_two_cells_of_one_column_are_swapped_and_the_prose_stated_one_comes_first(tmp_path):
    rows = [['Group', 'Events'], ['Control', '384'], ['Treated', '164'], ['Total', '548']]
    base = manuscript(tmp_path / 'ms', [
        piece('There were 384 events in the control group.'),
        {'kind': 'table', 'text': 'Table 1 Events by group', 'table': rows, 'page': 3},
    ])
    plans = plant.plans_for(base, 'table-cell')
    assert plans
    plan = plans[0]
    assert plan['check']['stated_in_prose'] == ['384']
    assert sorted(plan['must_mention']) == ['164', '384']
    assert [edit['original'] for edit in plan['edits']] == ['384', '164']
    assert [edit['altered'] for edit in plan['edits']] == ['164', '384']
    # The table's own first line, which is where its map row starts.
    assert plan['line'] == 3


def test_a_sample_size_needs_a_second_place_to_disagree_with(tmp_path):
    once = [piece('We enrolled n = 189 participants.')]
    assert not plant.plans_for(manuscript(tmp_path / 'once', once), 'sample-size')
    twice = once + [piece('Of the n = 189 enrolled, all completed the study.')]
    plans = plant.plans_for(manuscript(tmp_path / 'twice', twice), 'sample-size')
    assert plans
    plan = plans[0]
    assert plan['edits'][0]['original'] == '189'
    assert len(plan['edits'][0]['altered']) == 3
    agreeing = plan['check']['agreeing_lines']
    assert agreeing, 'the key records what still says 189'
    # The other place, named as a place: the second paragraph is on line 3.
    assert agreeing == [{'document': 'paper.md', 'line': 3}]
    assert 'line 3 of paper.md' in plan['reason']
    assert set(plan['must_mention']) == {'189', plan['edits'][0]['altered']}


def test_a_doi_keeps_its_prefix_and_its_length(tmp_path):
    reference = {'kind': 'reference', 'page': 9,
                 'text': 'Jolley KA et al. https://doi.org/10.12688/wellcomeopenres.14826.1'}
    base = manuscript(tmp_path / 'ms', [reference], references=[{'doi': ''}])
    plans = plant.plans_for(base, 'fake-doi')
    assert plans
    plan = plans[0]
    original, altered = plan['edits'][0]['original'], plan['edits'][0]['altered']
    assert original.startswith('10.12688/') and altered.startswith('10.12688/')
    assert len(original) == len(altered) and original != altered
    assert plan['check']['line_kind'] == 'reference'
    # Either suffix counts: the printed one, which resolves to nothing, or the
    # real one, which is not in the altered paper and so comes from a lookup.
    assert plan['must_mention'] == [altered.split('/', 1)[1], original.split('/', 1)[1]]
    assert plan['mention_rule'] == 'any'


def test_a_doi_printed_twice_is_refused(tmp_path):
    body = piece('See https://doi.org/10.12688/wellcomeopenres.14826.1 for the tool.')
    reference = {'kind': 'reference', 'page': 9,
                 'text': 'Jolley KA et al. https://doi.org/10.12688/wellcomeopenres.14826.1'}
    base = manuscript(tmp_path / 'ms', [body, reference])
    assert not plant.plans_for(base, 'fake-doi')


def test_plantable_counts_every_kind(tmp_path):
    base = manuscript(tmp_path / 'ms', [piece('Nothing numeric here at all.')])
    assert plant.plantable(base) == {kind: 0 for kind in plant.KINDS}


def test_locate_refuses_a_page_that_says_the_same_thing_twice():
    strings = [(('x', 0), 'the dose was 240 mg in the first arm '),
               (('x', 1), 'the dose was 240 mg in the second arm')]
    flat, _ = plant.flatten(strings)
    context = 'the dose was 240 mg in the'
    assert plant.locate(flat, '240', context, 13) == -1


def test_locate_picks_the_occurrence_whose_context_matches():
    filler = 'Methods are described in the supplementary material of the report. ' * 6
    wanted = 'Events in the control arm of the trial numbered 384 overall. '
    strings = [(('x', 0), 'Reference 12 was published in volume 384 of the journal. '),
               (('x', 1), filler),
               (('x', 2), wanted)]
    flat, index = plant.flatten(strings)
    where = plant.locate(flat, '384', wanted, 48)
    assert where > len(strings[0][1]) + len(filler) - 1
    spliced = plant.splice(strings, index, where, '164')
    assert spliced[2][1].count('164') == 1 and spliced[0][1].count('384') == 1
    # Equal length, so nothing after the edit moves on the page.
    assert [len(text) for _, text in spliced] == [len(text) for _, text in strings]


def test_the_key_names_the_lines_the_altered_text_landed_in(tmp_path):
    before = manuscript(tmp_path / 'before', [piece('The count was 384 of 548.')],
                        paper_id='aaaa1111', title='A paper')
    after = manuscript(tmp_path / 'after', [piece('The count was 164 of 548.')],
                       paper_id='bbbb2222', title='A paper')
    plan = {'rule': 'two-cells-of-one-column-swapped', 'reason': 'swapped',
            'document': 'paper.md', 'line': 1,
            'check': {}, 'must_mention': ['164', '384'], 'sentence': 'The count was 164 of 548.',
            'edits': [{'original': '384', 'altered': '164',
                       'context': 'The count was 384 of 548.', 'offset_in_context': 14}]}
    original, altered = tmp_path / 'a.pdf', tmp_path / 'b.pdf'
    original.write_bytes(b'%PDF-1.7 original')
    altered.write_bytes(b'%PDF-1.7 altered')
    key = plant.answer_key('slug', 'table-cell', original, altered, before, after, plan)
    assert key['detect']['lines'] == [{'document': 'paper.md', 'line': 1}]
    assert key['document_before'] == 'paper.md' and key['line_before'] == 1
    assert key['original_paper_id'] == 'aaaa1111' and key['altered_paper_id'] == 'bbbb2222'
    assert key['detect']['mention_rule'] == 'all'
    assert sorted(key['detect']['must_mention']) == ['164', '384']
    assert key['detect']['quotes'] and '164' in key['detect']['quotes'][0]
    assert key['original_pdf_sha256'] != key['altered_pdf_sha256']


def test_the_key_names_every_line_of_a_table_the_edit_landed_in(tmp_path):
    rows = [['Group', 'Events'], ['Control', '384'], ['Treated', '164'], ['Total', '548']]
    swapped = [['Group', 'Events'], ['Control', '164'], ['Treated', '384'], ['Total', '548']]
    before = manuscript(tmp_path / 'before', [{'kind': 'table', 'text': 'Table 1',
                                               'table': rows, 'page': 3}])
    after = manuscript(tmp_path / 'after', [{'kind': 'table', 'text': 'Table 1',
                                             'table': swapped, 'page': 3}])
    plan = {'rule': 'two-cells-of-one-column-swapped', 'reason': 'swapped',
            'document': 'paper.md', 'line': 1, 'check': {}, 'must_mention': ['164', '384'],
            'sentence': 'Control 164 / Treated 384',
            'edits': [{'original': '384', 'altered': '164',
                       'context': '| Control | 384 |', 'offset_in_context': 12}]}
    original, altered = tmp_path / 'a.pdf', tmp_path / 'b.pdf'
    original.write_bytes(b'%PDF-1.7 original')
    altered.write_bytes(b'%PDF-1.7 altered')
    key = plant.answer_key('slug', 'table-cell', original, altered, before, after, plan)
    # A reviewer quoting the row that holds the swapped cell is pointing at the
    # error, so every line of the table is a place the key accepts.
    assert key['detect']['lines'] == [{'document': 'paper.md', 'line': number}
                                      for number in range(1, 6)]
