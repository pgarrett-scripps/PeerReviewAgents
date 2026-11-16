"""An import that would lose a page of the paper is refused, by page.

The pinned converter returns no block at all for a page whose ``/Rotate`` is
not zero. A one-page paper turned ninety degrees converted to an empty
``paper.md``; a twelve-page paper with one landscape table page converted to
eleven pages of manuscript and said nothing about the twelfth. Neither is a
manuscript anyone can review, and neither is something this package can repair
— the text never reaches it — so the import refuses and names the pages.

The PDFs here are written byte by byte rather than kept as fixtures, because
what is under test is one entry in the page dictionary: the same content
stream, the same fonts, ``/Rotate 0`` against ``/Rotate 90``.
"""
from __future__ import annotations

import zlib
from pathlib import Path

import pytest

from peerreviewagents.engine import ingest

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


def test_a_page_the_converter_returns_nothing_from_refuses_the_import(tmp_path):
    """The twelfth page, silently missing, is the thing being prevented."""
    pdf = pdf_with_rotations(tmp_path / 'turned.pdf', [0, 90])
    with pytest.raises(ValueError) as refusal:
        ingest.import_paper(pdf, tmp_path / 'workspace')
    said = str(refusal.value)
    assert 'page(s) 2' in said and 'rotated 90' in said
    # It says whose fault it is, because the engine cannot fix this one: the
    # text never reaches it.
    assert 'rustypaper' in said and 'report it against' in said
    assert not (tmp_path / 'workspace' / 'paper.md').exists()
    assert not (tmp_path / 'workspace' / ingest.DRAFT_FILE).exists()


def test_a_rotated_page_alone_is_the_whole_manuscript_and_is_still_refused(tmp_path):
    """The one-page case that was reported: an import that wrote nothing at all."""
    pdf = pdf_with_rotations(tmp_path / 'one.pdf', [90])
    with pytest.raises(ValueError) as refusal:
        ingest.import_paper(pdf, tmp_path / 'workspace')
    assert 'page(s) 1' in str(refusal.value)


def test_pages_the_converter_reads_import_as_they_did(tmp_path):
    """The guard is about pages that are lost, not about pages that are there."""
    pdf = pdf_with_rotations(tmp_path / 'upright.pdf', [0, 0])
    manifest = ingest.import_paper(pdf, tmp_path / 'workspace')
    paper = (tmp_path / 'workspace' / 'paper.md').read_text(encoding='utf-8')
    assert 'the right way up' in paper and 'turned ninety degrees' in paper
    assert manifest['statistics']['pdf_pages'] == 2


def test_a_page_with_a_running_head_and_no_prose_is_not_a_lost_page():
    """A full-page figure is a page the converter reports little from, not a loss."""
    # The converter's own page numbers count from zero, so this block is on
    # page one: the figure page, which it read the caption off.
    blocks = [{'page': 0, 'text': 'Figure 3. The array, in plan view.'}]
    figure_page = 'Journal of Results 14 (2026) 221–230    229'
    prose_page = 'A page of prose. ' * 40
    assert ingest.unconverted_pages(blocks, [figure_page, prose_page]) == [2]
    assert ingest.unconverted_pages(blocks, [figure_page]) == []
