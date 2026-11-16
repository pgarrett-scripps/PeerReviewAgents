"""A page the converter returned nothing from is the reader's, or it refuses.

The pinned converter returns no block at all for a page whose ``/Rotate`` is
not zero, and none for some upright full-page tables either. A one-page paper
turned ninety degrees converted to an empty ``paper.md``; a twelve-page paper
with one landscape table page converted to eleven pages of manuscript and said
nothing about the twelfth.

Putting back what the converter dropped is the Read stage's work, so the import
no longer refuses: it records each lost page on the draft, the reader is told
what it is, and the seal is refused while one of them still has nothing on it.
With no reader — ``seal_as_converted``, or a run with the stage off — nothing is
going to put them back, so the refusal stands there, by page.

The PDFs here are written byte by byte rather than kept as fixtures, because
what is under test is one entry in the page dictionary: the same content
stream, the same fonts, ``/Rotate 0`` against ``/Rotate 90``.
"""
from __future__ import annotations

import json
import zlib
from pathlib import Path

import pytest

from peerreviewagents import names
from peerreviewagents.engine import ingest, paper_base
from peerreviewagents.engine import reader as read_stage
from peerreviewagents.plugin import server as srv

PAGES = [
    b'BT /F1 14 Tf 72 720 Td (An upright page) Tj ET\n'
    b'BT /F1 11 Tf 72 700 Td (This page is the right way up and holds a sentence of prose.) Tj ET\n'
    b'BT /F1 11 Tf 72 680 Td (It holds a second sentence so that the page is worth reading.) Tj ET\n'
    b'BT /F1 11 Tf 72 660 Td (A third sentence makes it a paragraph rather than a caption.) Tj ET\n',
    b'BT /F1 14 Tf 72 720 Td (A landscape table page) Tj ET\n'
    b'BT /F1 11 Tf 72 700 Td (This page is turned ninety degrees and carries the big table.) Tj ET\n'
    b'BT /F1 11 Tf 72 680 Td (Its rows are the measurements the whole result rests upon.) Tj ET\n'
    b'BT /F1 11 Tf 72 660 Td (Losing it loses the evidence for every number in the text.) Tj ET\n',
]

#: What the reader transcribes off the page the converter dropped.
TRANSCRIBED = ('A landscape table page. Day 1, 12.5 mm; day 2, 12.5 mm; day 3, 12.6 mm. '
               'The rows are the measurements the result rests upon.')


def pdf_with_rotations(path: Path, rotations) -> Path:
    """A PDF of one page per entry in *rotations*, each turned that far."""
    streams = [zlib.compress(PAGES[index % len(PAGES)]) for index in range(len(rotations))]
    first_page, first_stream = 3, 3 + len(rotations)
    font = first_stream + len(rotations)
    kids = ' '.join('%d 0 R' % (first_page + index) for index in range(len(rotations)))
    objects = [b'<< /Type /Catalog /Pages 2 0 R >>',
               ('<< /Type /Pages /Kids [%s] /Count %d >>' % (kids, len(rotations))).encode()]
    for index, rotation in enumerate(rotations):
        objects.append(
            ('<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Rotate %d /Resources '
             '<< /Font << /F1 %d 0 R >> >> /Contents %d 0 R >>'
             % (rotation, font, first_stream + index)).encode())
    for stream in streams:
        objects.append(b'<< /Length %d /Filter /FlateDecode >>\nstream\n' % len(stream)
                       + stream + b'\nendstream')
    objects.append(b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>')
    out = bytearray(b'%PDF-1.4\n')
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b'%d 0 obj\n' % number + body + b'\nendobj\n'
    start = len(out)
    out += b'xref\n0 %d\n' % (len(objects) + 1) + b'0000000000 65535 f \n'
    for offset in offsets:
        out += b'%010d 00000 n \n' % offset
    out += (b'trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n'
            % (len(objects) + 1, start))
    path.write_bytes(bytes(out))
    return path


def test_the_lost_page_is_carried_on_the_draft_rather_than_refused(tmp_path):
    """The conversion itself goes through: the page is work, not an error."""
    pdf = pdf_with_rotations(tmp_path / 'turned.pdf', [0, 90])
    draft = ingest.convert_paper(str(pdf), tmp_path / 'workspace')
    assert [one['page'] for one in draft['lost_pages']] == [2]
    lost = draft['lost_pages'][0]
    assert lost['rotation'] == 90 and lost['words'] >= ingest.LOST_PAGE_WORDS
    # Nothing of page 2 is in the draft, which is the whole problem: the pieces
    # are page 1's and the manuscript would end there.
    assert {piece['page'] for piece in draft['documents'][paper_base.MAIN_DOCUMENT]} == {1}
    # Nothing has been added on it, so it is outstanding work for the reader.
    assert read_stage.outstanding(draft)['lost_pages'] == [
        {'page': 2, 'words': lost['words'], 'rotation': 90, 'added': []}]


def test_sealing_a_lost_page_with_no_reader_is_refused_by_page(tmp_path):
    """With no reader the page is not coming back, so nothing is written."""
    pdf = pdf_with_rotations(tmp_path / 'turned.pdf', [0, 90])
    with pytest.raises(ValueError) as refusal:
        ingest.import_paper(pdf, tmp_path / 'workspace')
    said = str(refusal.value)
    assert 'page(s) 2' in said and 'rotated 90' in said and 'words' in said
    # It says what to do instead, and who to report the page to.
    assert 'Read stage' in said and 'rustypaper' in said
    assert not (tmp_path / 'workspace' / 'paper.md').exists()


def test_a_rotated_page_alone_is_the_whole_manuscript_and_is_still_refused(tmp_path):
    """The one-page case that was reported: an import that wrote nothing at all."""
    pdf = pdf_with_rotations(tmp_path / 'one.pdf', [90])
    with pytest.raises(ValueError) as refusal:
        ingest.import_paper(pdf, tmp_path / 'workspace')
    assert 'Nothing was converted' in str(refusal.value)


def test_pages_the_converter_reads_import_as_they_did(tmp_path):
    """The guard is about pages that are lost, not about pages that are there."""
    pdf = pdf_with_rotations(tmp_path / 'upright.pdf', [0, 0])
    manifest = ingest.import_paper(pdf, tmp_path / 'workspace')
    paper = (tmp_path / 'workspace' / 'paper.md').read_text(encoding='utf-8')
    assert 'the right way up' in paper and 'turned ninety degrees' in paper
    assert manifest['statistics']['pdf_pages'] == 2
    assert 'converter_pages' not in manifest['known_gaps']


def test_a_page_with_a_running_head_and_no_prose_is_not_a_lost_page():
    """A full-page figure is a page the converter reports little from, not a loss."""
    # The converter's own page numbers count from zero, so this block is on
    # page one: the figure page, which it read the caption off.
    blocks = [{'page': 0, 'text': 'Figure 3. The array, in plan view.'}]
    figure_page = 'Journal of Results 14 (2026) 221–230    229'
    prose_page = 'A page of prose. ' * 40
    geometry = [{'page': 1, 'rotation': 0}, {'page': 2, 'rotation': 90}]
    assert ingest.unconverted_pages(blocks, [figure_page, prose_page], geometry) == [
        {'page': 2, 'words': 160, 'rotation': 90}]
    assert ingest.unconverted_pages(blocks, [figure_page], geometry) == []


@pytest.fixture
def reading(tmp_path, with_reader):
    """A run over the two-page PDF with page 2 lost, claimed by the reader."""
    srv.STATE['run'] = None
    pdf = pdf_with_rotations(tmp_path / 'turned.pdf', [0, 90])
    root = tmp_path / 'run'
    opened = srv.ingest(str(pdf), str(root), reviewers=1, arm='single')
    assert 'request_rejected' not in opened, opened
    codes = {row['slot']: row['claim_code'] for row in opened.get('claim_codes', [])}
    taken = srv.claim_agent('reader', codes['reader'])
    assert 'agent_token' in taken, taken
    yield opened, taken, taken['agent_token'], root
    srv.STATE['run'] = None


def test_the_reader_is_told_what_the_lost_page_is(reading):
    """It is not in the listing, so the listing says why and what to do."""
    opened, taken, token, _ = reading
    assert opened['draft']['to_account_for']['lost_pages'][0]['page'] == 2
    assert 'add what it prints with add_piece' in taken['read_first']
    assert 'unconverted page(s)' in taken['reminder']
    listing = srv.read_draft(token)
    assert 'request_rejected' not in listing, listing
    said = listing['unconverted_pages']
    assert 'page(s) 2' in said and 'rotated 90°' in said
    assert 'add_piece' in said
    assert listing['outstanding']['lost_pages'][0]['added'] == []


def test_the_seal_is_refused_while_a_lost_page_has_nothing_on_it(reading):
    """Viewing it is not enough: the page reaches the manuscript or it does not."""
    _, _, token, root = reading
    for page in (1, 2):
        assert 'request_rejected' not in srv.view_page(token, page)
    refused = srv.seal_manuscript(token)
    said = refused['request_rejected']
    assert 'returned nothing at all from page(s) 2' in said
    assert 'nowhere else' in said and 'add_piece' in said
    assert refused['outstanding']['lost_pages'][0]['page'] == 2
    assert not (root / names.MANUSCRIPT_DIRECTORY / 'paper.md').exists()


def test_what_the_reader_adds_off_a_lost_page_is_the_page_in_the_manuscript(reading):
    """The page is back, as the model's words, and the manifest says so."""
    _, _, token, root = reading
    for page in (1, 2):
        assert 'request_rejected' not in srv.view_page(token, page)
    last = srv.read_draft(token)['pieces'][-1]['piece']
    added = srv.add_piece(token, last, 'paragraph', 2, text=TRANSCRIBED)
    assert 'request_rejected' not in added, added
    assert added['source'] == 'ai'
    assert added['outstanding']['lost_pages'] == []
    sealed = srv.seal_manuscript(token)
    assert 'request_rejected' not in sealed, sealed
    folder = root / names.MANUSCRIPT_DIRECTORY
    paper = (folder / 'paper.md').read_text(encoding='utf-8')
    assert 'the measurements the result rests upon' in paper
    manifest = json.loads((folder / 'manifest.json').read_text(encoding='utf-8'))
    # A reader of the base is told which pages are the model's transcription
    # rather than the converter's reading, and which pieces came off them.
    account = manifest['ai_read']['lost_pages']
    assert account == [{'page': 2, 'words': account[0]['words'], 'rotation': 90,
                        'added': [added['piece']]}]
    gap = manifest['known_gaps']['converter_pages']
    assert 'page(s) 2' in gap and 'rotated 90°' in gap
    assert 'page 2 → ' + added['piece'] in gap
    assert manifest['ai_read']['actions']['added'] == 1


def test_sealing_as_converted_is_refused_while_a_page_is_lost(reading):
    """The recorder's fallback writes what the converter read, and it read none of it.

    This is the one case where the run cannot go on: a reader that failed
    leaves a manuscript missing a page, and writing it anyway is what the
    refusal exists to stop.
    """
    opened, _, _, root = reading
    refused = srv.seal_as_converted(opened['recorder_key'], reason='the reader died')
    said = refused['request_rejected']
    assert 'page(s) 2' in said and 'rotated 90' in said
    assert 'Sealing as converted is refused' in said
    assert not (root / names.MANUSCRIPT_DIRECTORY / 'paper.md').exists()


def test_prose_may_only_be_added_on_a_page_the_converter_lost(reading):
    """Everywhere else the words are the authors' as converted."""
    _, _, token, _ = reading
    for page in (1, 2):
        assert 'request_rejected' not in srv.view_page(token, page)
    first = srv.read_draft(token)['pieces'][0]['piece']
    refused = srv.add_piece(token, first, 'paragraph', 1, text=TRANSCRIBED)
    said = refused['request_rejected']
    assert 'Page 1 is not one of the pages' in said
    assert 'its text is already in the draft' in said
