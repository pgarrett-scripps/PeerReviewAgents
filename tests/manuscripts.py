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

from peerreviewagents import read_manuscript, write_manuscript
from peerreviewagents.engine.storage import write


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
