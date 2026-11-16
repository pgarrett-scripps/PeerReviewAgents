"""Conversion and sealing are two halves, and only the second writes the base.

An import used to be one call: convert the PDF and write ``paper.md`` in the
same breath, which left no moment at which a piece could still be corrected.
The converter gets equations wrong — eleven in the antenna manuscript, about
half of them mangled — and once the base is written the lines are the identity
of every citation and nothing may move. So the pieces now live in a draft until
something has had the chance to repair them, and one function writes the base
from whatever the draft holds at the end.

What these tests hold in place is that the draft is not a manuscript (nothing
cites it, nothing can load it as a base), that the seal happens once, and that
the manifest always says whether a model read the paper — including when none
did, which is the case this repository shipped for its whole life.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from peerreviewagents.engine import ingest, paper_base

FIXTURE = Path(__file__).resolve().parents[1] / 'fixtures' / 'single-column.pdf'


def test_conversion_writes_a_draft_and_no_base(tmp_path):
    draft = ingest.convert_paper(str(FIXTURE), tmp_path)
    assert draft['schema_version'] == ingest.DRAFT_SCHEMA_VERSION
    assert (tmp_path / ingest.DRAFT_FILE).is_file()
    # The three files that are the manuscript. None of them exists yet, so a
    # reviewer handed this folder has nothing to cite and an application that
    # opens manuscript folders finds none here.
    assert not (tmp_path / 'paper.md').exists()
    assert not (tmp_path / 'map.json').exists()
    assert not (tmp_path / 'manifest.json').exists()
    with pytest.raises(ValueError, match='Not a manuscript folder'):
        paper_base.load(tmp_path)
    # The identity is the PDF's, so it is known before a line exists: the run
    # can write protocol.json while the reader is still working.
    assert draft['paper_id'] == ingest.import_paper(
        str(FIXTURE), tmp_path / 'again')['paper_id']
    assert draft['page_count'] >= 1
    assert draft['documents'][paper_base.MAIN_DOCUMENT]


def test_sealing_writes_the_base_from_the_draft_and_happens_once(tmp_path):
    draft = ingest.convert_paper(str(FIXTURE), tmp_path)
    manifest = ingest.seal_draft(tmp_path, draft)
    base = paper_base.load(tmp_path)
    assert manifest['schema_version'] == paper_base.BASE_SCHEMA_VERSION
    assert base.title == 'Reliable Widget Measurements'
    # The draft is gone. A folder holding both would hold the manuscript twice
    # and an application would have to guess which one it was opening.
    assert not (tmp_path / ingest.DRAFT_FILE).exists()
    assert ingest.DRAFT_FILE not in manifest['files']
    with pytest.raises(ValueError, match='already contains an immutable manuscript'):
        ingest.seal_draft(tmp_path, draft)


def test_a_manuscript_nothing_read_says_so_in_its_manifest(tmp_path):
    manifest = ingest.import_paper(str(FIXTURE), tmp_path)
    assert manifest['ai_read']['read'] is False
    assert 'no reader ran' in manifest['ai_read']['reason']
    assert manifest['pieces_per_source'] == {'text': manifest['counts']['pieces']}
    # The import's own gaps stand: the figures are captions and the equation
    # confidences are the converter's, because nothing looked at a page.
    assert 'no figure images' in manifest['known_gaps']['figures']


def test_a_sealed_read_names_the_model_and_counts_what_it_did(tmp_path):
    draft = ingest.convert_paper(str(FIXTURE), tmp_path)
    manifest = ingest.seal_draft(tmp_path, draft, ai_read={
        'read': True, 'reader_model': 'claude-sonnet-5-5', 'pages_viewed': 2,
        'pieces_repaired': 3, 'pieces_confirmed': 4, 'pieces_added': 1})
    assert manifest['ai_read'] == {
        'read': True, 'reader_model': 'claude-sonnet-5-5', 'pages_viewed': 2,
        'pieces_repaired': 3, 'pieces_confirmed': 4, 'pieces_added': 1}
    # The two gaps the Read stage closes are rewritten rather than left saying
    # the figures have no descriptions and the equations were never checked.
    assert 'written by claude-sonnet-5-5' in manifest['known_gaps']['figures']
    assert 'checked against the page image' in manifest['known_gaps']['equation_confidence']


def test_a_draft_of_another_shape_is_refused_by_name(tmp_path):
    draft = ingest.convert_paper(str(FIXTURE), tmp_path)
    draft['schema_version'] = 'paper-draft-v0'
    with pytest.raises(ValueError, match='paper-draft-v0'):
        ingest.seal_draft(tmp_path, draft)


def test_an_application_source_is_sealed_at_once_and_needs_no_reader(tmp_path):
    manifest = ingest.write_manuscript(tmp_path, [
        {'kind': 'title', 'text': 'A composed paper', 'section': 'Front matter'},
        {'kind': 'paragraph', 'text': 'One sentence.', 'section': 'Body'},
    ])
    assert manifest['ai_read']['read'] is False
    assert 'needs no reading' in manifest['ai_read']['reason']
    assert paper_base.load(tmp_path).title == 'A composed paper'
    assert not (tmp_path / ingest.DRAFT_FILE).exists()
