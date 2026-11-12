"""Scoring has to be the same rule every time, so these tests pin the rule down.

The cases that matter are the near misses. A reviewer that quotes the altered
sentence and says something else about it has not caught the planted error, and
a reviewer that names the numbers while pointing at another part of the paper
has not either. Both show up in Stage A as near misses, and if the scorer
counted them as catches the comparison between arms would flatter every arm
equally and tell us nothing.
"""

from __future__ import annotations

import json

import score

KEY = {
    'paper': 'trial-pmc8938265',
    'kind': 'sample-size',
    'rule': 'one-statement-of-n-against-another',
    'detect': {
        'blocks': ['b0010'],
        'quotes': ['A total of 548 patients were randomly assigned to treatment.'],
        'must_mention': ['548', '530'],
        'mention_rule': 'all',
    },
}


def write_run(folder, findings, runtime=None, progress=None):
    """A run folder of the shape the plugin writes."""
    review = folder / 'review'
    review.mkdir(parents=True, exist_ok=True)
    review.joinpath('findings.jsonl').write_text(
        ''.join(json.dumps(row) + '\n' for row in findings), encoding='utf-8')
    review.joinpath('runtime.json').write_text(json.dumps(runtime or {
        'status': 'complete', 'wall_seconds': 247.0, 'tokens_total': 636299,
        'agents': [{'label': 'stats', 'tokens': 90000, 'toolCalls': 9, 'model': 'opus'}],
    }), encoding='utf-8')
    review.joinpath('progress.json').write_text(json.dumps(progress or {
        'status': 'complete', 'findings_delivered': len(findings),
        'quarantined_findings_count': 0}), encoding='utf-8')
    return folder


def finding(**fields) -> dict:
    row = {'finding_uid': 'f1', 'title': '', 'comment': '', 'quote': '', 'severity': 'major',
           'slot': 'specialist-stats', 'role': 'specialist', 'track': 'stats', 'kind': 'error',
           'block_id': '', 'evidence_blocks': [], 'escalate': False}
    row.update(fields)
    return row


def test_naming_both_numbers_in_the_right_block_is_a_catch(tmp_path):
    run = write_run(tmp_path / 'run', [finding(
        block_id='b0010',
        title='Randomised total disagrees with the flow diagram',
        comment='The text says 548 randomised while the CONSORT diagram and Table 1 say 530.')])
    result = score.score(run, KEY)
    assert result['caught'] is True
    assert result['first_catch']['slot'] == 'specialist-stats'
    assert result['tokens'] == 636299 and result['minutes'] == 4.1
    assert result['findings_by_track'] == {'stats': 1}


def test_quoting_the_sentence_without_the_claim_is_only_a_near_miss(tmp_path):
    run = write_run(tmp_path / 'run', [finding(
        block_id='b0010',
        title='Randomisation is not described',
        comment='The allocation method is not stated anywhere in the methods.',
        quote='A total of 548 patients were randomly assigned to treatment.')])
    result = score.score(run, KEY)
    assert result['caught'] is False
    assert len(result['near_miss_right_place']) == 1
    assert not result['near_miss_right_content']


def test_naming_one_of_the_two_numbers_is_not_enough(tmp_path):
    run = write_run(tmp_path / 'run', [finding(
        block_id='b0010', comment='548 patients is a large trial for this indication.')])
    assert score.score(run, KEY)['caught'] is False


def test_the_right_claim_in_the_wrong_place_is_a_near_miss(tmp_path):
    run = write_run(tmp_path / 'run', [finding(
        block_id='b0400', comment='548 here and 530 there cannot both be the randomised total.')])
    result = score.score(run, KEY)
    assert result['caught'] is False
    assert len(result['near_miss_right_content']) == 1


def test_an_evidence_block_or_a_quote_can_establish_the_place(tmp_path):
    by_evidence = write_run(tmp_path / 'one', [finding(
        block_id='b0999', evidence_blocks=['b0010'],
        comment='548 in the text against 530 in the table.')])
    assert score.score(by_evidence, KEY)['caught'] is True
    by_quote = write_run(tmp_path / 'two', [finding(
        quote='A total of 548 patients were randomly assigned',
        comment='This disagrees with the 530 reported in Table 1; 548 cannot be right.')])
    assert score.score(by_quote, KEY)['caught'] is True


def test_a_quote_too_short_to_identify_a_place_does_not_count(tmp_path):
    run = write_run(tmp_path / 'run', [finding(
        quote='548', comment='548 against 530.')])
    assert score.score(run, KEY)['caught'] is False


def test_a_blocked_run_scores_as_a_miss_and_keeps_its_cause(tmp_path):
    run = write_run(tmp_path / 'run', [], runtime={
        'status': 'blocked', 'blocked_reason': 'usage limit reached', 'wall_seconds': 61,
        'tokens_total': 12000, 'agents': []},
        progress={'status': 'blocked', 'findings_delivered': 0})
    result = score.score(run, KEY)
    assert result['caught'] is False and result['findings_total'] == 0
    assert result['status'] == 'blocked'
    assert result['blocked_reason'] == 'usage limit reached'
    assert result['publishable'] is False


def test_a_limit_after_the_review_was_written_is_a_note_not_a_miss(tmp_path):
    """The arm still answered the paper, so the row must not read as blocked.

    This is the status rule the library decides: the review was written and the
    ledger says complete, so the run is complete and the limit that ended the
    session afterwards is a note. Scoring it as blocked would throw away a real
    catch and understate the arm.
    """
    run = write_run(tmp_path / 'run', [finding(
        block_id='b0010', comment='The text says 548 randomised while Table 1 says 530.')],
        runtime={'status': 'blocked', 'blocked_reason': 'rate limit: five_hour',
                 'wall_seconds': 300.0, 'tokens_total': 500000, 'agents': []})
    result = score.score(run, KEY)
    assert result['status'] == 'complete' and result['caught'] is True
    assert 'rate limit: five_hour' in (result['note'] or '')
    assert result['blocked_reason'] is None


def test_a_run_folder_with_nothing_in_it_does_not_raise(tmp_path):
    empty = tmp_path / 'run'
    empty.mkdir()
    result = score.score(empty, KEY)
    assert result['caught'] is False and result['tokens'] == 0
    assert result['status'] == 'unknown' and result['publishable'] is False
    assert result['problems'] and 'not a run folder' in result['problems'][0].lower()


def test_a_finding_the_editor_placed_twice_is_one_catch(tmp_path):
    """A uid in both the body and the appendix is one finding, so it is one catch.

    Stage A counts catches and findings per arm, and the body/appendix split is
    the editor's placement, not two findings: counting it twice would make the
    full two-track arm look more productive than it was.
    """
    caught = finding(finding_uid='f7', block_id='b0010',
                     comment='548 in the text against 530 in Table 1.')
    run = write_run(tmp_path / 'run', [caught])
    package = {'findings': [caught], 'appendix': [caught], 'review_guard_status': 'clean',
               'summary': 'one finding', 'verdict': 'major revision'}
    (run / 'review' / 'editor-package.json').write_text(json.dumps(package), encoding='utf-8')
    result = score.score(run, KEY)
    assert result['findings_total'] == 1 and result['findings_appendix'] == 0
    assert len(result['caught_by']) == 1
    assert result['findings_source'] == 'editor-package' and result['publishable'] is True


def test_a_control_counts_only_severe_allegations(tmp_path):
    run = write_run(tmp_path / 'run', [
        finding(finding_uid='f1', severity='major',
                comment='Table 2 does not match the text: the totals are inconsistent.'),
        finding(finding_uid='f2', severity='minor',
                comment='These numbers do not match the figure.'),
        finding(finding_uid='f3', severity='major',
                comment='The sampling frame could be described more clearly.'),
    ])
    result = score.score_control(run, 'genomics-pmc6927181')
    assert result['findings_total'] == 3
    assert result['planted_style_allegations'] == 1
    assert result['allegations'][0]['finding_uid'] == 'f1'


def test_a_fake_doi_key_is_matched_on_the_suffix(tmp_path):
    key = {'paper': 'epidemiology-pmc7611074', 'kind': 'fake-doi', 'rule': 'doi',
           'detect': {'blocks': ['b0094'], 'quotes': ['Reference 41. 10.1101/2020.04.23.20076486'],
                      'must_mention': ['2020.04.23.20076486'], 'mention_rule': 'all'}}
    run = write_run(tmp_path / 'run', [finding(
        block_id='b0094', track='literature', slot='specialist-literature',
        comment='The DOI 10.1101/2020.04.23.20076486 does not resolve to a registered record.')])
    assert score.score(run, key)['caught'] is True
