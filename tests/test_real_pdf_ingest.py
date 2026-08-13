"""Ingest against a real PDF, converted by the installed rustypaper.

Every other ingest test stubs the converter, because what they check is the
loader's contract with it. This one checks the join itself — that the section
tree the converter reports and the Markdown it emits line up well enough to
cut one into the other — and that cannot be checked against a stub, because a
stub is written by the same hand as the code under test.

Skipped unless a corpus PDF is present. The paper is rustypaper's own
``transformer.pdf``, whose 22 sections it reads correctly, and the path can be
pointed elsewhere with ``RUSTYPAPER_CORPUS``. Capabilities the installed
converter lacks are skipped per test rather than assumed: a rustypaper that
reports no section tree, or types reference blocks without parsing their
fields, is a supported configuration, and the fallback it puts the loader on
is covered by the stubbed tests in test_ingest_backend.

To run against a checkout of the converter rather than the installed wheel,
put its Python package first on the path::

    PYTHONPATH=/path/to/rustypaper/python pytest tests/test_real_pdf_ingest.py
"""

from __future__ import annotations

import os

import pytest

CORPUS = os.environ.get(
    "RUSTYPAPER_CORPUS", os.path.expanduser("~/Repos/rustypaper/corpus")
)
PAPER = os.path.join(CORPUS, "transformer.pdf")


def _converter_available() -> bool:
    try:
        import rustypaper  # noqa: F401
    except Exception:  # noqa: BLE001 - not installed is just a skip
        return False
    return True


pytestmark = [
    pytest.mark.skipif(
        not os.path.isfile(PAPER),
        reason=f"corpus PDF not present at {PAPER} (set RUSTYPAPER_CORPUS)",
    ),
    pytest.mark.skipif(not _converter_available(), reason="rustypaper is not installed"),
]


@pytest.fixture(scope="module")
def parsed(tmp_path_factory):
    from peerreviewagents.ingest import loader

    cache = {"cache_dir": str(tmp_path_factory.mktemp("manuscripts"))}
    return loader.load_manuscript_record(PAPER, cache)


def test_the_title_is_the_converters_own(parsed):
    assert parsed.title == "Attention Is All You Need"


def test_the_sections_are_read_from_the_document_model(parsed):
    if parsed.ingest.get("section_source") != "document":
        pytest.skip("installed converter reports no section tree")
    for name in ("abstract", "introduction", "conclusion", "references"):
        assert name in parsed.sections, name
    # Each section's text is a literal slice of the manuscript the panel
    # reads — not a re-rendering of the blocks, which could differ from it.
    for body in parsed.sections.values():
        assert body in parsed.text


def test_the_section_boundaries_are_where_the_paper_puts_them(parsed):
    assert "conclusion" in parsed.sections
    assert parsed.sections["conclusion"].startswith("In this work, we presented")
    # The bibliography starts at its first entry. The converter anchors each
    # one so in-text citations can link to it, so the entry's label is not
    # necessarily the first thing on the line.
    assert "[1] Jimmy Lei Ba" in parsed.sections["references"].splitlines()[0]
    # The heading line names the section and is not part of its text.
    assert not parsed.sections["introduction"].lstrip().startswith("#")


def test_the_bibliography_arrives_as_typed_entries(parsed):
    assert len(parsed.references) == 40
    first = parsed.references[0]
    assert first["raw"].startswith("Jimmy Lei Ba") or first["raw"].startswith("[1]")
    if "label" not in first:
        pytest.skip("installed converter types reference blocks but parses no fields")
    assert first["label"] == "1"
    assert first["authors"][0] == "Jimmy Lei Ba"
    assert first["year"] == 2016
    # Absent rather than wrong: this entry has no DOI printed, so none is
    # reported for it.
    assert "doi" not in first


def test_the_measured_statistics_come_out_of_a_clean_conversion(parsed):
    stats = parsed.ingest["prose"]
    assert stats["health"]["verdict"] == "clean"
    # The bibliography is typed, so its size is known even though this paper
    # sets its in-text citations as bracketed numerals the regex may miss.
    assert stats["counts"]["reference_entries"] == 40
    assert stats["counts"]["main_text_words"] < stats["counts"]["words"]
    # Per-section numbers exist for a paper with a section map, and the
    # bibliography is not among them.
    assert "introduction" in stats["sections"]
    assert "references" not in stats["sections"]


def test_one_conversion_produces_both_views():
    """The Markdown and the document model come from the same run, so the
    Markdown the loader keeps is the rendering of the model it read."""
    import rustypaper

    if not hasattr(rustypaper, "convert"):
        pytest.skip("installed converter has no single-call entry point")
    result = rustypaper.convert(PAPER)
    assert result.markdown == rustypaper.to_markdown(PAPER, None)
    assert [s["title"] for s in result.document["sections"]] == [
        s["title"] for s in rustypaper.to_document(PAPER)["sections"]
    ]
