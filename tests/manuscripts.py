"""The manuscript builder the suite anchors against.

A finding is anchored to a line of ``paper.md``, so a test that needs a
manuscript needs the file and the map over it rather than a dict it made up.
:func:`build_base` writes both through
:func:`peerreviewagents.engine.paper_base.write_base`, which is what keeps the
line numbers in a test the same numbers the engine would compute.

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

from peerreviewagents.engine import paper_base
from peerreviewagents.engine.storage import digest, relative_name, write


def build_base(folder, pieces, documents=None, references=None, manifest=None):
    """Write a manuscript workspace from pieces of text and load its base.

    *pieces* are the main document's, in order, in the shape the converter
    hands :func:`peerreviewagents.engine.paper_base.write_base`: a ``kind``, a
    ``text``, and whatever else that kind renders from. *documents* adds further
    files by name — a supplement — each with its own pieces.

    What comes back is the loaded
    :class:`~peerreviewagents.engine.paper_base.PaperBase`, and what is left
    behind is a folder :func:`peerreviewagents.engine.annotations.verify_workspace`
    accepts: every file is digested into ``manifest.json`` the way an import
    would, so a test that tampers with one gets the real refusal instead of a
    fixture-shaped one.
    """
    folder.mkdir(parents=True, exist_ok=True)
    pages = {'paper.md': list(pieces or []), **{name: list(rows)
                                                for name, rows in (documents or {}).items()}}
    paper_base.write_base(folder, pages)
    write(folder / 'references.json', list(references or []))
    record = {'schema_version': paper_base.BASE_SCHEMA_VERSION, 'paper_id': 'paper-1',
              'title': 'A manuscript', 'source_url': None,
              'source': {'file': 'original/original.pdf', 'sha256': 'f' * 64, 'pages': 1,
                         'xml': None, 'xml_sha256': None},
              'parser': 'test', 'documents': list(pages), 'assets': [],
              'statistics': {'pdf_pages': 1, 'reference_entries': len(references or ())},
              'warnings': [], 'known_gaps': {}}
    record.update(manifest or {})
    record['files'] = {relative_name(path, folder): digest(path.read_bytes())
                       for path in sorted(folder.rglob('*'))
                       if path.is_file() and path.name != 'manifest.json'}
    write(folder / 'manifest.json', record)
    return paper_base.load(folder)
