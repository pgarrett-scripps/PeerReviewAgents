"""Where a finding is, and what it rests on, are read from the manuscript.

The scorer's one job is to be the same rule every time, so these tests pin the
rule rather than the numbers: a finding is in the piece of the paper its cited
quote sits in, the piece's map row says what kind of thing that is and who
wrote it, and the reviewer's own prose decides none of it. A finding that names
Table 1 while quoting a sentence in the discussion is not a finding in Table 1,
and a finding that quotes a sentence the reader repaired is one whose evidence
came from the reader rather than from the converter — which is the whole point
of counting it.

The file is ``test_spot_score.py`` rather than ``test_score.py`` because
``tests/stage_a`` already holds a ``test_score.py`` and pytest imports both
under the basename alone.
"""

from __future__ import annotations

import json

import spot_score
from manuscripts import build_base

TABLE = {'kind': 'table', 'label': 'Table 1', 'section': 'Results', 'page': 4,
         'table': {'caption': 'Pharmacokinetic parameters',
                   'header': ['Parameter', 'Value'],
                   'rows': [['AUC', '-587.51'], ['LOQ', '-1780.34']]}}
PROSE = {'kind': 'paragraph', 'text': 'The pharmacokinetic profile was dose proportional '
                                      'across the range studied.', 'section': 'Discussion'}
REPAIRED = {'kind': 'paragraph', 'source': 'ai-reconstructed',
            'text': 'Plasma was sampled at 0, 2, 6, 12 and 24 hours after dosing.',
            'original': {'text': 'Plasma was samp1ed at O, 2, 6, l2 and 24 h0urs.'},
            'section': 'Methods'}
WRITTEN = {'kind': 'figure-description', 'source': 'ai',
           'text': 'Figure 1 shows the concentration-time curve falling below the limit of '
                   'quantification by 24 hours.', 'section': 'Results'}
#: A paragraph the reader only cut a running header out of: the source says
#: ai-reconstructed, and every word of it is the converter's own, in the
#: converter's order. Counting it as reader-contributed evidence beside a
#: rebuilt equation is what ``reader_did`` exists to stop.
TRIMMED = {'kind': 'paragraph', 'source': 'ai-reconstructed',
           'text': 'Clearance was estimated by non-linear regression.',
           'original': {'kind': 'paragraph', 'part': 1, 'of': 1,
                        'text': 'J Vet Pharmacol 2019 Clearance was estimated by '
                                'non-linear regression. 1427'},
           'section': 'Methods'}


def manuscript(folder):
    """The sealed manuscript a run was reviewed against, read by the reader."""
    return build_base(folder / 'manuscript', [PROSE, TABLE, REPAIRED, WRITTEN, TRIMMED],
                      ai_model='claude-opus-5', manifest={
        'ai_read': {'read': True, 'reader_model': 'claude-opus-5', 'reader_slot': 'reader',
                    'attempt': 1, 'pages_viewed': 1, 'pages': [1],
                    'actions': {'repaired': 2, 'described': 1}, 'gone': {},
                    'tool_calls': 4, 'note': '', 'lost_pages': []}})


def write_review(folder, findings, runtime=None):
    """The review half of a run folder, of the shape the plugin writes."""
    review = folder / 'review'
    review.mkdir(parents=True, exist_ok=True)
    review.joinpath('findings.jsonl').write_text(
        ''.join(json.dumps(row) + '\n' for row in findings), encoding='utf-8')
    review.joinpath('runtime.json').write_text(json.dumps(runtime or {
        'status': 'complete', 'wall_seconds': 600.0, 'tokens_total': 700000,
        'agents': [{'label': 'stats', 'tokens': 90000, 'toolCalls': 9, 'model': 'opus'}],
    }), encoding='utf-8')
    review.joinpath('progress.json').write_text(json.dumps({
        'status': 'complete', 'findings_delivered': len(findings),
        'quarantined_findings_count': 0}), encoding='utf-8')
    return folder


def finding(base, piece, **fields) -> dict:
    """A finding that cites one piece of the written manuscript."""
    row = base.rows[piece]
    start, end = row['lines']
    out = {'finding_uid': 'f1', 'title': '', 'comment': '', 'severity': 'major',
           'slot': 'specialist-stats', 'role': 'specialist', 'track': 'stats',
           'kind': 'error', 'document': 'paper.md', 'line': start, 'end_line': end,
           'evidence_lines': [], 'escalate': False,
           'quote': base.range_text(start, end).splitlines()[0]}
    out.update(fields)
    return out


def test_a_finding_in_a_table_is_a_finding_in_a_table(tmp_path):
    """The map row decides the kind, and the kind alone makes it a candidate.

    This finding never writes the words "Table 1": it quotes the table and says
    the numbers are impossible. The old rule, which looked for the key's phrases
    in the reviewer's sentences, would have missed it.
    """
    run = tmp_path / 'run'
    base = manuscript(run)
    write_review(run, [finding(
        base, 1, title='Two reported parameters are negative',
        comment='A quantity that is integrated over time cannot come out below zero.')])
    row = spot_score.score_paper(run, 'medicine-oxytetracycline')
    one = row['candidates'][0]
    assert one['piece']['kind'] == 'table' and one['piece']['page'] == 4
    assert one['at_kind'] is True and one['cites_display'] is True
    assert one['cites_kinds'] == ['table']
    assert row['candidates_at_kind'] == 1
    assert row['finding_pieces']['citing_display'] == 1
    assert row['finding_pieces']['by_piece_kind'] == {'table': 1}


def test_naming_the_place_in_the_review_is_not_being_in_it(tmp_path):
    """A phrase in the reviewer's prose is a cue, not a location.

    This finding is a candidate — its words carry one of the key's cues — and it
    is not at the location: it quotes a sentence in the discussion while
    writing "Table 1" and "AUC" in its own comment. Naming a place is not being
    in it, so ``at_location`` is false and the row counts it only as a cue.
    """
    run = tmp_path / 'run'
    base = manuscript(run)
    write_review(run, [finding(
        base, 0, title='Dose proportionality is asserted without support',
        comment='Table 1 reports AUC values below zero, and no test of proportionality '
                'is given here.')])
    row = spot_score.score_paper(run, 'medicine-oxytetracycline')
    one = row['candidates'][0]
    assert one['piece']['kind'] == 'paragraph' and one['piece']['section'] == 'Discussion'
    assert one['at_location'] is False and one['where_hits'] == []
    assert one['at_kind'] is False and one['cues'] == ['below zero']
    assert row['candidates_at_location'] == 0 and row['candidates_by_cue'] == 1


def test_a_finding_on_a_repaired_piece_says_so(tmp_path):
    """Whether the reader wrote the sentence a finding rests on is a map fact."""
    run = tmp_path / 'run'
    base = manuscript(run)
    write_review(run, [
        finding(base, 2, finding_uid='f1', title='The sampling schedule is incomplete'),
        finding(base, 3, finding_uid='f2', title='The curve is described, not shown'),
        finding(base, 0, finding_uid='f3', title='Dose proportionality is asserted')])
    row = spot_score.score_one(run, 'medicine-oxytetracycline')
    repaired, written, converter = row['all_findings']
    assert repaired['piece']['written'] == 'repaired by the reader'
    assert repaired['cites_reader_repaired'] is True and repaired['cites_reader_text'] is True
    assert written['piece']['written'] == 'written by the reader'
    assert written['cites_reader_written'] is True
    assert converter['piece']['written'] == 'the converter'
    assert converter['cites_reader_text'] is False
    assert row['finding_pieces']['on_reader_text'] == 2
    assert row['finding_pieces']['on_reader_repaired'] == 1
    assert row['finding_pieces']['on_reader_written'] == 1
    assert row['reader']['read'] is True
    assert row['reader']['actions'] == {'repaired': 2, 'described': 1}


def test_a_cut_paragraph_is_not_a_rebuilt_equation(tmp_path):
    """``ai-reconstructed`` is two different acts, and the split says which.

    The reader rebuilt the figure description and rewrote the sampling
    sentence; on the third paragraph it only took a journal running header
    out, leaving the converter's own words. All three say ``on_reader_text``,
    and a single number over them would read as reader-written evidence when
    one of them is a cut header.
    """
    run = tmp_path / 'run'
    base = manuscript(run)
    write_review(run, [
        finding(base, 4, finding_uid='f1', title='Clearance has no stated model'),
        finding(base, 3, finding_uid='f2', title='The curve is described, not shown'),
        finding(base, 2, finding_uid='f3', title='The sampling schedule is incomplete')])
    row = spot_score.score_one(run, 'medicine-oxytetracycline')
    cut, rebuilt, rewritten = row['all_findings']
    assert cut['piece']['did'] == 'cut' and cut['piece']['reader_did'] == 'trimmed by the reader'
    assert cut['cites_reader_cut'] is True and cut['cites_reader_rebuilt'] is False
    assert rebuilt['piece']['did'] == 'rebuilt' and rebuilt['cites_reader_rebuilt'] is True
    assert rewritten['piece']['did'] == 'rewrote' and rewritten['cites_reader_rewritten'] is True
    pieces = row['finding_pieces']
    assert pieces['on_reader_text'] == 3
    assert pieces['on_reader_rebuilt'] == 1
    assert pieces['on_reader_cut'] == 1
    assert pieces['on_reader_rewritten'] == 1


def test_a_run_whose_manuscript_cannot_be_read_is_not_scored(tmp_path):
    """No map, no location — so the row says that instead of reporting zeros."""
    run = tmp_path / 'run'
    review = run / 'review'
    review.mkdir(parents=True)
    review.joinpath('findings.jsonl').write_text(
        json.dumps({'finding_uid': 'f1', 'title': 'Something', 'severity': 'major',
                    'document': 'paper.md', 'line': 1, 'end_line': 1, 'quote': 'x'}) + '\n',
        encoding='utf-8')
    review.joinpath('runtime.json').write_text(json.dumps({
        'status': 'complete', 'wall_seconds': 60.0, 'tokens_total': 1000}), encoding='utf-8')
    row = spot_score.score_one(run, 'medicine-oxytetracycline')
    assert row['kind'] == 'not scored'
    assert 'candidates' not in row
    assert row['findings_total'] == 1 and row['findings_major'] == 1
    assert row['manuscript_refusal']
    assert row['reader']['read'] is False


def test_scoring_a_second_run_does_not_take_the_first_one_s_line(tmp_path):
    """``--out`` appends, because a comparison is scored one arm at a time."""
    run = tmp_path / 'run'
    base = manuscript(run)
    write_review(run, [finding(base, 1, title='Two reported parameters are negative')])
    out = tmp_path / 'results' / 'results.jsonl'
    for _ in range(2):
        assert spot_score.main([str(run), '--slug', 'medicine-oxytetracycline',
                                '--out', str(out)]) == 0
    rows = [json.loads(line) for line in out.read_text(encoding='utf-8').splitlines()]
    assert len(rows) == 2
    assert {row['paper'] for row in rows} == {'medicine-oxytetracycline'}


def test_the_folder_name_is_only_a_fallback_for_the_slug(tmp_path):
    """A comparison names its arm in the folder, and the arm is not the paper."""
    assert spot_score.slug_of(tmp_path / 'cmp-antenna-on') == 'network-beamforming'
    assert spot_score.slug_of(tmp_path / 'cmp-antenna-off') == 'network-beamforming'
    assert spot_score.slug_of(tmp_path / 'cmp-antibiotic-on') == 'medicine-oxytetracycline'
    assert spot_score.slug_of(tmp_path / 'cmp-education-off') == \
        'control-education-pmc7183109'
    assert spot_score.slug_of(tmp_path / 'oxytetracycline') == 'medicine-oxytetracycline'
    assert spot_score.slug_of(tmp_path / 'something-else') == 'something-else'
