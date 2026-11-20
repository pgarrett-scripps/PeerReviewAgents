"""The manuscript builder the suite anchors against.

A finding is anchored to a line of ``paper.md``, so a test that needs a
manuscript needs the file and the map over it rather than a dict it made up.
:func:`build_base` writes both through
:func:`peerreviewagents.write_manuscript`, the call an application writes a
manuscript with, which is what keeps the line numbers in a test the same
numbers the engine would compute and the manifest in a test the same manifest a
real workspace holds.

This is a module beside the tests rather than a fixture in ``conftest.py``
because the suite holds two ``conftest.py`` files — this folder's and
``stage_a``'s — and Python imports both of them under the one name
``conftest``: whichever pytest loaded first is the one an
``from conftest import build_base`` reaches, which made the import work when a
subfolder was run on its own and fail when the whole suite was. The name here is
its own, so it means the same thing from every test. ``tests/conftest.py`` puts
this folder on ``sys.path``, and it is imported before any test module below it,
so the import works from a subfolder too.
"""

from __future__ import annotations

import zlib
from pathlib import Path

from peerreviewagents import read_manuscript, write_manuscript
from peerreviewagents.engine.storage import write
from peerreviewagents.plugin import server as srv

#: Why a test's manuscript was sealed with nothing read. The reason reaches the
#: manifest, so it says what it is rather than naming a test.
NOTHING_READ = 'no model reads anything in this suite, so the draft is sealed as converted'

#: The content streams :func:`pdf_with_rotations` writes its pages from: one
#: upright page of prose and one page of a table, each four lines of text.
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


def pdf_with_rotations(path, rotations):
    """A PDF of one page per entry in *rotations*, each turned that far.

    The pinned converter returns no block at all for a page whose ``/Rotate``
    is not zero, so a PDF written with one is a PDF the conversion loses a page
    of: the draft carries it as an outstanding page and the seal is refused
    while nothing has been put on it. That is the one real way a run reaches
    its Read stage's *second* failure — the reader did not seal and the
    converted seal is refused too, because the manuscript would be missing a
    page and nothing downstream could tell — so a test that needs a run
    blocked there writes one of these rather than deleting a file afterwards.

    It is written byte by byte rather than kept as a fixture because what makes
    it work is one entry in the page dictionary: the same content stream, the
    same fonts, ``/Rotate 0`` against ``/Rotate 90``.
    """
    path = Path(path)
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


def seal_converted(opened):
    """Seal a freshly ingested run as converted, and return the server's answer.

    Every PDF review plans a Read stage, and ``ingest`` leaves the manuscript a
    draft: there is no paper.md and no line to cite until a reader seals it. No
    test here runs a model, so a test about anything downstream of the seal
    writes the lines the one other way a run can — the recorder's
    ``seal_as_converted``, the path a run takes when its reader failed. The
    Read stage's own tests drive the reader's tools instead, and
    ``peerreviewagents.scripted_read`` is that path for an application's tests.
    """
    sealed = srv.seal_as_converted(opened['recorder_key'], reason=NOTHING_READ)
    assert 'request_rejected' not in sealed, sealed
    return sealed


def build_base(folder, pieces, documents=None, references=None, manifest=None, ai_model=''):
    """Write a manuscript workspace from pieces of text and load its base.

    *pieces* are the main document's, in order, in the shape a converter hands
    :func:`peerreviewagents.write_manuscript`: a ``kind``, a ``text``, and
    whatever else that kind renders from. *documents* adds further files by
    name — a supplement — each with its own pieces. *manifest* overrides keys of
    the written manifest, for a test about a manuscript that says something
    else about itself. *ai_model* names the model a piece whose ``source`` is
    ``ai`` or ``ai-reconstructed`` was written by, which
    :func:`peerreviewagents.write_manuscript` requires before it will write
    one: a manuscript cannot carry a model's words with nothing to say whose
    they are.

    What comes back is the loaded
    :class:`~peerreviewagents.engine.paper_base.PaperBase`, and what is left
    behind is a folder :func:`peerreviewagents.verify_workspace` accepts: it is
    written by the one call an application writes a workspace with, so a test
    that tampers with a file gets the real refusal instead of a fixture-shaped
    one, and a change to the manifest a workspace holds reaches the tests
    through the same code the application sees.
    """
    folder.mkdir(parents=True, exist_ok=True)
    # A workspace is written once: `write_manuscript` refuses a folder that
    # already holds one, because an application that wrote a second manuscript
    # over the first would renumber every line a saved review cites. A fixture
    # is built twice from one test often enough — the same text, the same
    # folder — so the map the last build left is cleared here, where the
    # rebuild is deliberate, rather than allowed for everyone.
    (folder / 'map.json').unlink(missing_ok=True)
    pages = {'paper.md': list(pieces or []), **{name: list(rows)
                                                for name, rows in (documents or {}).items()}}
    # A fixture names itself: the paper id and the one page are what the suite
    # asserts against, and a PDF-backed source is what most of these tests are
    # standing in for.
    record = write_manuscript(folder, pages, title='A manuscript', paper_id='paper-1',
                              parser='test', references=references, ai_model=ai_model,
                              source={'file': 'original/original.pdf', 'sha256': 'f' * 64,
                                      'pages': 1})
    if manifest:
        record.update(manifest)
        write(folder / 'manifest.json', record)
    return read_manuscript(folder)
