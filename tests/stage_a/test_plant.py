"""The planter has to plant an error that contradicts something, or Stage A measures nothing.

These tests are about the choosing and the bookkeeping, which is where a
planted error goes wrong: a number altered where nothing disagrees with it is
not an error a reviewer could catch, and a key that names the wrong place turns
a real catch into a miss. The PDF layer is exercised through its own pure
helpers; `plant()` itself needs pikepdf and a real paper, and is used against
the corpus rather than a fixture.
"""

from __future__ import annotations

import plant


def block(identifier: str, text: str, **extra) -> dict:
    return {'id': identifier, 'text': text, 'kind': 'paragraph', 'page': 1, **extra}


def test_core_keeps_the_count_and_drops_the_bracket():
    assert plant.core('3,457,173 (43.12)') == '3,457,173'
    assert plant.core('0.078') == '0.078'
    assert plant.core('12%') == '12'


def test_percentage_is_altered_against_the_n_stated_beside_it():
    text = ('Among the responders (n = 4850), 2808 (57.9%) reported the symptom at '
            'least once during follow-up.')
    plans = plant.plans_for({'blocks': [block('b0001', text)]}, 'statistic')
    assert plans, 'a percentage with its own n in the block is plantable'
    plan = plans[0]
    assert plan['rule'] == 'percentage-against-the-stated-n'
    assert plan['edits'][0]['original'] == '57.9'
    assert plan['edits'][0]['altered'] != '57.9'
    # The key must name the altered percentage and the count it no longer fits.
    assert set(plan['must_mention']) == {plan['edits'][0]['altered'], '2808'}
    assert plan['check']['total'] == 4850


def test_a_p_value_is_only_planted_where_the_sentence_claims_significance():
    claimed = block('b0001', 'The effect was statistically significant (P = .003).')
    denied = block('b0002', 'The effect was not significant (P = .003).')
    assert plant.plans_for({'blocks': [claimed]}, 'statistic')
    assert not plant.plans_for({'blocks': [denied]}, 'statistic')


def test_a_year_is_not_mistaken_for_a_count():
    text = 'Between 1990 and 2010 the registry grew, as others have reported (n = 2000).'
    assert not plant.plans_for({'blocks': [block('b0001', text)]}, 'statistic')


def test_two_cells_of_one_column_are_swapped_and_the_prose_stated_one_comes_first():
    rows = [['Group', 'Events'], ['Control', '384'], ['Treated', '164'], ['Total', '548']]
    table = {'id': 'b0002', 'kind': 'table', 'page': 3,
             'text': 'Group Events Control 384 Treated 164 Total 548',
             'table': rows}
    prose = block('b0001', 'There were 384 events in the control group.')
    plans = plant.plans_for({'blocks': [prose, table]}, 'table-cell')
    assert plans
    plan = plans[0]
    assert plan['check']['stated_in_prose'] == ['384']
    assert sorted(plan['must_mention']) == ['164', '384']
    assert [edit['original'] for edit in plan['edits']] == ['384', '164']
    assert [edit['altered'] for edit in plan['edits']] == ['164', '384']


def test_a_sample_size_needs_a_second_place_to_disagree_with():
    once = [block('b0001', 'We enrolled n = 189 participants.')]
    assert not plant.plans_for({'blocks': once}, 'sample-size')
    twice = once + [block('b0002', 'Of the n = 189 enrolled, all completed the study.')]
    plans = plant.plans_for({'blocks': twice}, 'sample-size')
    assert plans
    plan = plans[0]
    assert plan['edits'][0]['original'] == '189'
    assert len(plan['edits'][0]['altered']) == 3
    assert plan['check']['agreeing_blocks'], 'the key records what still says 189'
    assert set(plan['must_mention']) == {'189', plan['edits'][0]['altered']}


def test_a_doi_keeps_its_prefix_and_its_length():
    reference = {'id': 'b0090', 'kind': 'reference', 'page': 9,
                 'text': 'Jolley KA et al. https://doi.org/10.12688/wellcomeopenres.14826.1'}
    plans = plant.plans_for({'blocks': [reference], 'references': [{'doi': ''}]}, 'fake-doi')
    assert plans
    plan = plans[0]
    original, altered = plan['edits'][0]['original'], plan['edits'][0]['altered']
    assert original.startswith('10.12688/') and altered.startswith('10.12688/')
    assert len(original) == len(altered) and original != altered
    # Either suffix counts: the printed one, which resolves to nothing, or the
    # real one, which is not in the altered paper and so comes from a lookup.
    assert plan['must_mention'] == [altered.split('/', 1)[1], original.split('/', 1)[1]]
    assert plan['mention_rule'] == 'any'


def test_a_doi_printed_twice_is_refused():
    body = block('b0001', 'See https://doi.org/10.12688/wellcomeopenres.14826.1 for the tool.')
    reference = {'id': 'b0090', 'kind': 'reference', 'page': 9,
                 'text': 'Jolley KA et al. https://doi.org/10.12688/wellcomeopenres.14826.1'}
    assert not plant.plans_for({'blocks': [body, reference], 'references': []}, 'fake-doi')


def test_plantable_counts_every_kind():
    paper = {'blocks': [block('b0001', 'Nothing numeric here at all.')], 'references': []}
    assert plant.plantable(paper) == {kind: 0 for kind in plant.KINDS}


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


def test_the_key_names_the_blocks_the_altered_text_landed_in(tmp_path):
    before = {'id': 'aaaa1111', 'blocks': [block('b0001', 'The count was 384 of 548.')]}
    after = {'id': 'bbbb2222', 'title': 'A paper',
             'blocks': [block('b0001', 'The count was 164 of 548.')]}
    plan = {'rule': 'two-cells-of-one-column-swapped', 'reason': 'swapped', 'block_id': 'b0001',
            'check': {}, 'must_mention': ['164', '384'], 'sentence': 'The count was 164 of 548.',
            'edits': [{'original': '384', 'altered': '164',
                       'context': 'The count was 384 of 548.', 'offset_in_context': 14}]}
    original, altered = tmp_path / 'a.pdf', tmp_path / 'b.pdf'
    original.write_bytes(b'%PDF-1.7 original')
    altered.write_bytes(b'%PDF-1.7 altered')
    key = plant.answer_key('slug', 'table-cell', original, altered, before, after, plan)
    assert key['detect']['blocks'] == ['b0001']
    assert key['detect']['mention_rule'] == 'all'
    assert sorted(key['detect']['must_mention']) == ['164', '384']
    assert key['detect']['quotes'] and '164' in key['detect']['quotes'][0]
    assert key['original_pdf_sha256'] != key['altered_pdf_sha256']
