"""What a consumer of a run folder is promised about the citation ledger.

The applications that print a review — insilico, paper-workbench — read a run
folder with :func:`peerreviewagents.read_run` and print its ``problems`` lines
verbatim on their own pages. That makes three things part of the contract
rather than house style: a citation check never withholds a review, a problem
line is a sentence about the run folder the reader is holding rather than about
the machine that read it, and the ledger's rows are reachable without reaching
into ``peerreviewagents.engine``.

Every fixture here is a run folder written by hand, which is the supported way
to build one: the ledger is append-only JSON lines and the last row written for
a ``row_id`` is that row, so a test can write the two or three rows it needs
and nothing has to run. Nothing here starts Claude Code, Codex or any provider,
no model runs and no lookup leaves the machine.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from peerreviewagents import read_citation_ledger, read_run
from peerreviewagents.engine import citation_ledger as cl
from peerreviewagents.engine import run_report

PLANNED = {'arm': 'full-two-track', 'reviewers': 1, 'paper_id': 'abc',
           'plan': {'arm': 'full-two-track', 'reviewers': 1, 'editor': True,
                    'specialists': ['citations'], 'citation_workers': ['cite-1']}}


def reference(row_id='ref:r1', exists='yes', **resolution):
    """One reference row, filled and then resolved, as the ledger stores it."""
    row = {'kind': 'reference', 'row_id': row_id, 'reference_id': row_id.split(':', 1)[1],
           'label': '1', 'raw': 'A. Author. A title. A venue, 2020.',
           'printed': {'title': 'A title', 'year': 2020, 'first_author': 'Author',
                       'venue': 'A venue', 'doi': '10.1000/one'},
           'stage': 'fill'}
    if exists is not None:
        row['resolution'] = {'exists': exists, 'method': 'doi', **resolution}
    return row


def use(row_id='use:b1:1:r1', reference_id='r1', verdict='supports'):
    """One in-text use row, judged by a worker."""
    row = {'kind': 'use', 'row_id': row_id, 'reference_id': reference_id, 'block_id': 'b1',
           'section': 'Introduction', 'sentence_index': 1, 'marker': '[1]',
           'sentence': 'The method was first described elsewhere [1].', 'stage': 'fill'}
    if verdict:
        row.update({'verdict': verdict, 'stage': 'verdict', 'worker': 'cite-1'})
    return row


def run_folder(tmp_path, rows=(), stages=None, protocol=PLANNED):
    """A run folder holding exactly the ledger rows a test asks for."""
    review = Path(tmp_path) / 'run' / 'review'
    review.mkdir(parents=True, exist_ok=True)
    (review / 'protocol.json').write_text(json.dumps(protocol), encoding='utf-8')
    (review / 'progress.json').write_text(json.dumps({'status': 'complete'}), encoding='utf-8')
    (review / 'editor-package.json').write_text(
        json.dumps({'findings': [{'finding_uid': 'f1', 'title': 'A finding'}], 'appendix': [],
                    'review_guard_status': 'checked'}), encoding='utf-8')
    if rows:
        folder = review / cl.LEDGER_DIRECTORY
        folder.mkdir(parents=True, exist_ok=True)
        (folder / cl.LEDGER_FILE).write_text(
            ''.join(json.dumps(row) + '\n' for row in rows), encoding='utf-8')
        (folder / cl.STATE_FILE).write_text(json.dumps(stages or {
            'fill': {'done': True, 'references': 1, 'uses': 1},
            'resolve': {'done': True, 'lookups': 2}}), encoding='utf-8')
    return review.parent, review


# -- an unreadable ledger is reported, and never a veto ---------------------


def test_a_ledger_that_cannot_be_read_is_reported_and_does_not_hold_the_review(tmp_path,
                                                                               monkeypatch):
    """The strongest form of an unfinished table is still an unfinished table.

    A ledger present and unreadable used to go into ``problems`` before
    ``publishable`` was decided, which made it the one citation failure that
    withheld the whole review. The findings the specialist confirmed are real
    whether or not the table behind them could be read, and the rule the lane
    is built on is that a citation check never blocks publication.
    """
    root, review = run_folder(tmp_path, [reference(), use()])

    def unreadable(_ledger):
        raise OSError(13, 'Permission denied', str(review / cl.LEDGER_PATH))

    monkeypatch.setattr(cl, 'counts', unreadable)
    reading = read_run(root)
    line, = reading['citations']['problems']
    assert 'could not be read' in line
    assert reading['citations']['counts'] == {}
    assert line in reading['problems']
    assert reading['publishable'] is True, reading['not_publishable_reasons']
    assert all('citation' not in reason and 'ledger' not in reason
               for reason in reading['not_publishable_reasons'])


def test_the_unreadable_ledger_line_names_the_file_from_the_run_folder(tmp_path, monkeypatch):
    """A problem line is about the folder the reader has, not about this machine.

    ``str`` of the :class:`OSError` behind it ends in the absolute path of the
    file, so the line used to carry the whole of wherever this machine keeps
    its runs. Consumers print these lines verbatim beside a run folder the
    reader already opened.
    """
    root, review = run_folder(tmp_path, [reference(), use()])

    def unreadable(_ledger):
        raise OSError(13, 'Permission denied', str(review / cl.LEDGER_PATH))

    monkeypatch.setattr(cl, 'counts', unreadable)
    line, = read_run(root)['citations']['problems']
    assert line.startswith('review/citations/ledger.jsonl is present')
    assert 'Permission denied' in line, 'the reason the file could not be read is the point'
    assert str(root) not in line and str(review) not in line
    assert not Path(line.split(' ')[0]).is_absolute()


def test_a_real_unreadable_ledger_reads_the_same_way(tmp_path):
    """The same, through a file the process genuinely may not open.

    Only where the mode bits mean that. ``os.geteuid`` is POSIX-only, and on
    Windows ``chmod`` toggles the read-only attribute and nothing else, so the
    file stays readable and there is nothing here to refuse. The monkeypatched
    twin below pins the promise on every platform.
    """
    if not hasattr(os, 'geteuid'):
        pytest.skip('chmod does not make a file unreadable on this platform')
    if os.geteuid() == 0:
        pytest.skip('root may read a file with no permission bits, so there is nothing to refuse')
    root, review = run_folder(tmp_path, [reference(), use()])
    path = review / cl.LEDGER_PATH
    path.chmod(0o000)
    try:
        reading = read_run(root)
        rows = read_citation_ledger(root)
    finally:
        path.chmod(0o644)
    assert reading['publishable'] is True, reading['not_publishable_reasons']
    assert any('could not be read' in line for line in reading['citations']['problems'])
    # The reader meets the same wall and answers the same way. A page that
    # prints the table is the one most likely to be pointed at a ledger it may
    # not open, and it may not learn that through an exception.
    assert rows['references'] == [] and rows['uses'] == []
    assert rows['exists'] is True
    assert rows['ledger'] == str(path)
    line, = rows['problems']
    assert 'could not be read' in line
    assert line == reading['citations']['problems'][0]
    assert str(root) not in line and str(review) not in line
    assert not Path(line.split(' ')[0]).is_absolute()


def test_the_public_reader_never_raises_on_a_ledger_it_may_not_open(tmp_path, monkeypatch):
    """The same wall, reached through ``load`` rather than through a mode bit.

    The real-permissions test above cannot run as root, and this is the one
    promise the reader's docstring makes about raises, so it is pinned here
    too: a ledger that cannot be opened is empty rows and a problem line, never
    an exception out of a consumer's page.
    """
    root, review = run_folder(tmp_path, [reference(), use()])

    def unreadable(_ledger):
        raise OSError(13, 'Permission denied', str(review / cl.LEDGER_PATH))

    monkeypatch.setattr(cl.Ledger, 'load', unreadable)
    rows = read_citation_ledger(root)
    assert rows['references'] == [] and rows['uses'] == []
    assert rows['exists'] is True and rows['counts'] == {}
    assert rows['ledger'] == str(review / cl.LEDGER_PATH)
    line, = rows['problems']
    assert line.startswith('review/citations/ledger.jsonl is present')
    assert str(root) not in line and str(review) not in line


# -- a run with no ledger says only what is known ---------------------------


def test_a_planned_lane_with_no_ledger_claims_nothing_about_what_was_checked(tmp_path):
    """No table is a fact about the run. It is not a finding about the lane.

    "Not one reference or in-text use was checked" is a claim about what the
    specialist did, and a run whose ledger stage never ran holds no record of a
    reference anybody looked at or did not. What is known is that the arm asked
    for the lane and the folder has no table in it.
    """
    root, _ = run_folder(tmp_path)
    reading = read_run(root)
    line, = reading['citations']['problems']
    assert line == ('this run planned a citations lane and wrote no citation ledger, so there '
                    'are no ledger counts')
    assert 'was checked' not in line and 'not one' not in line
    assert reading['citations']['enabled'] is True
    assert reading['publishable'] is True, reading['not_publishable_reasons']


# -- the public reader ------------------------------------------------------


def test_the_public_reader_gives_the_rows_with_the_ledgers_own_last_row_rule(tmp_path):
    """One row per ``row_id``, the last one written, without the engine module.

    The file is append-only and a later row for an id supersedes the earlier
    one. Every consumer that wanted the rows re-implemented that against
    ``peerreviewagents.engine.citation_ledger``, which is not a public module,
    so each of them owned a copy of the one rule the format has.
    """
    root, review = run_folder(tmp_path, [
        reference(exists='could not check', note='the lookups did not answer'),
        reference(exists='yes', record={'title': 'A title'}),
        use(verdict=None),
        use(verdict='partly supports'),
    ])
    reading = read_citation_ledger(root)

    assert reading['run'] == str(root) and reading['review'] == str(review)
    assert reading['ledger'] == str(review / cl.LEDGER_PATH) and reading['exists'] is True
    resolved, = reading['references']
    judged, = reading['uses']
    assert resolved['resolution']['exists'] == 'yes', 'the last row for an id is the row'
    assert judged['verdict'] == 'partly supports'
    assert [row['row_id'] for row in reading['references']] == ['ref:r1']

    # The numbers are read_run's own, from the same functions, so a consumer
    # showing rows and a consumer showing counts cannot disagree about a run.
    assert reading['counts'] == read_run(root)['citations']['counts']
    assert reading['summary'] == read_run(root)['citations']['summary']
    assert reading['problems'] == read_run(root)['citations']['problems']
    assert reading['complete'] is True


def test_the_public_reader_leaves_out_the_source_text_unless_it_is_asked_for(tmp_path):
    """Someone else's prose is most of the file and is not what a page needs.

    A resolved reference carries the abstract and the opening of the full text
    a worker was shown. A caller that wants to know *whether* text was fetched
    is told so without being handed the text.
    """
    root, _ = run_folder(tmp_path, [
        reference(exists='yes', abstract='We show that the thing holds.',
                  full_text='We show that the thing holds. Here is how.'),
        reference(row_id='ref:r2', exists='could not check'),
        use(),
    ])
    quiet = read_citation_ledger(root)
    fetched, nothing = quiet['references']
    assert 'abstract' not in fetched['resolution'] and 'full_text' not in fetched['resolution']
    assert fetched['resolution']['has_source_text'] is True
    assert nothing['resolution']['has_source_text'] is False
    assert fetched['resolution']['exists'] == 'yes', 'the rest of the row is untouched'

    asked = read_citation_ledger(root, source_text=True)
    assert asked['references'][0]['resolution']['abstract'] == 'We show that the thing holds.'
    assert 'has_source_text' not in asked['references'][0]['resolution']


def test_the_public_reader_refuses_a_symlinked_ledger_and_degrades_on_damage(tmp_path):
    """read_run's guards, because it is the same files and the same writer.

    The server writes the ledger in place, so a link where the file belongs
    points at something another process owns, and following it would report
    that file's rows as this run's citations. Damaged content is the opposite
    case: a run killed mid-write is ordinary, and one torn line is one skipped
    row rather than a reading nobody gets.
    """
    root, review = run_folder(tmp_path, [reference(), use()])
    path = review / cl.LEDGER_PATH
    rows = path.read_text(encoding='utf-8')
    path.write_text(rows + '{"kind": "reference", "row_id": "ref:r', encoding='utf-8')
    torn = read_citation_ledger(root)
    assert len(torn['references']) == 1 and torn['counts']['unreadable_rows'] == 1
    assert any('could not be read and were skipped' in line for line in torn['problems'])

    elsewhere = Path(tmp_path) / 'somebody-elses-ledger.jsonl'
    elsewhere.write_text(rows, encoding='utf-8')
    path.unlink()
    try:
        path.symlink_to(elsewhere)
    except OSError as exc:  # Windows without the privilege. The guard is still there.
        pytest.skip(f'this machine cannot create a symlink to test against: {exc}')
    with pytest.raises(ValueError) as refused:
        read_citation_ledger(root)
    assert 'citations/ledger.jsonl' in str(refused.value), \
        'a run holds more than one file called ledger.jsonl, so the bare name says nothing'
    assert 'must not be a symlink' in str(refused.value)


def test_the_public_reader_says_nothing_is_there_for_an_arm_without_the_lane(tmp_path):
    """No table and no lane is not a damaged run, and raises nothing."""
    root, _ = run_folder(tmp_path, protocol={'arm': 'reviewers-only',
                                             'plan': {'arm': 'reviewers-only',
                                                      'specialists': []}})
    reading = read_citation_ledger(root)
    assert reading['exists'] is False and reading['ledger'] is None
    assert reading['references'] == [] and reading['uses'] == []
    assert reading['problems'] == [] and reading['counts'] == {}


# -- the fifth reference state ---------------------------------------------


def test_the_summary_sentence_carries_every_state_the_counts_hold(tmp_path):
    """``unresolved`` is a state, not the gap between the printed numbers.

    A reference the run never looked up is counted in ``counts`` and was left
    out of the sentence, so the four printed numbers did not add up to the
    total and the difference read as resolved.
    """
    root, _ = run_folder(tmp_path, [
        reference(exists='yes'),
        reference(row_id='ref:r2', exists=None),
        reference(row_id='ref:r3', exists=cl.NOT_LOCATED),
        use(),
    ], stages={'fill': {'done': True}, 'resolve': {'done': True, 'lookups': 2}})
    totals = read_run(root)['citations']['counts']
    assert (totals['unresolved'], totals['not_located']) == (1, 1)
    sentence = cl.summary_line(totals)
    assert '1 never looked up' in sentence
    assert '1 not located in either index' in sentence
    printed = sum(int(part.split(' ')[0]) for part in
                  sentence.split('references: ')[1].split('.')[0].split(', '))
    assert printed == totals['references'], 'the printed states have to add up to the total'


def test_the_report_page_prints_the_not_located_count_beside_the_others(tmp_path):
    """A state missing from the page is a state the reader has to infer."""
    _, review = run_folder(tmp_path, [reference(exists=cl.NOT_LOCATED), use()])
    fields = [block for block in run_report._citation_blocks(review) if block[0] == 'fields']
    labels = {key: value for key, value in fields[0][1]}
    for state in ('Resolved', 'Not found', 'Not located', 'Could not be checked',
                  'Never looked up', 'Retracted'):
        assert state in labels, f'the Citations section does not print {state}'
    assert labels['Not located'] == 1


# -- wording a consumer prints verbatim ------------------------------------


def test_no_line_a_consumer_prints_says_a_reference_does_not_exist(tmp_path):
    """Only the DOI resolver having no handle for a DOI is that strong.

    A lookup that did not answer and a title two indexes do not list say
    nothing about whether the work is real. Reusing the phrase for them was
    read by a page as the table making that claim.
    """
    root, _ = run_folder(tmp_path, [
        reference(exists='could not check'),
        reference(row_id='ref:r2', exists=cl.NOT_LOCATED),
        reference(row_id='ref:r3', exists=None),
        use(verdict=None),
    ])
    reading = read_run(root)
    lines = reading['problems'] + reading['citations']['problems']
    rows = cl.problem_rows(cl.Ledger(Path(reading['review'])))
    lines += [row['problem'] for row in rows['references'] + rows['uses']]
    assert lines
    for line in lines:
        assert 'do not exist' not in line and 'does not exist' not in line, line

    # The one row that is that strong keeps saying so, in the resolver's terms.
    no_handle, = cl.problem_rows(cl.Ledger(Path(run_folder(
        tmp_path / 'b', [reference(exists='no'), use()])[1])))['references']
    assert 'no handle for the DOI' in no_handle['problem']


def test_no_problem_line_a_consumer_prints_carries_a_semicolon(tmp_path):
    """insilico and paper-workbench ban them in authored text, and print these.

    A semicolon in a line this package writes arrives on their pages as one
    they did not write, which is why the lines are plain sentences joined with
    "and" instead.
    """
    root, review = run_folder(tmp_path, [
        reference(exists='could not check', error='the lookup raised'),
        reference(row_id='ref:r2', exists=cl.NOT_LOCATED, mismatched=['year', 'venue'],
                  printed_doi_registered=False, candidate=True, retracted=True),
        use(verdict=None),
    ])
    # Damage every file read_run names, so the sweep is over lines a real
    # consumer sees rather than over the handful a clean run produces.
    (review / 'manifest.json').write_text('{"code": [] ', encoding='utf-8')
    (review / 'runtime.json').write_bytes(b'{"status": "\xff\xfe"}')
    (review / 'findings.jsonl').write_text('{"finding_uid": "f2"}\n{"torn', encoding='utf-8')
    (review / 'startup.json').write_text(json.dumps({'probe': 'a string'}), encoding='utf-8')
    reading = read_run(root)
    rows = cl.problem_rows(cl.Ledger(review))
    lines = (reading['problems'] + reading['citations']['problems']
             + reading['not_publishable_reasons']
             + [row['problem'] for row in rows['references'] + rows['uses']]
             + cl.problems(reading['citations']['counts']))
    assert len(lines) > 8, 'the sweep has to see the damaged files it wrote'
    for line in lines:
        assert ';' not in line, line


def test_a_verdict_with_nothing_to_read_names_no_source_kind(tmp_path):
    """``None``, not the string ``none``, which a page printed as a kind."""
    root, _ = run_folder(tmp_path, [reference(exists='yes'), use(verdict='cannot tell')])
    window = cl.source_window(read_citation_ledger(root, source_text=True)['references'][0])
    assert window['source_kind'] is None and window['source_text'] == ''


def test_the_worker_instruction_names_the_role_rather_than_a_model(tmp_path):
    """A worker is a slot. Which model fills it is the plan's to say, not this.

    ``assigned_model`` is the plan and ``model`` is what was observed, and an
    instruction string that hardcodes one model family shows a reader a third
    thing that is neither.
    """
    root, _ = run_folder(tmp_path, [reference(exists='yes'), use()])
    rows = cl.supported_rows(cl.Ledger(Path(read_run(root)['review'])))
    text = json.dumps(rows)
    assert 'citation worker' in text
    assert 'Haiku' not in text and 'haiku' not in text
