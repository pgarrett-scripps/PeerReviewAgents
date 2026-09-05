"""Exercise the installed converter on an original, committed PDF.

These tests are mandatory and need neither a sibling checkout nor an archive.
The optional full-paper corpus checks live in test_corpus_pdf_ingest.py.
"""
from pathlib import Path

import pytest
import rustypaper

from peerreviewagents.ingest.loader import load_manuscript_record

PAPER = Path(__file__).parent / "fixtures" / "single-column.pdf"


@pytest.fixture(scope="module")
def parsed(tmp_path_factory):
    return load_manuscript_record(
        str(PAPER), {"cache_dir": str(tmp_path_factory.mktemp("manuscripts"))}
    )


def test_title_and_sections_survive_real_conversion(parsed):
    assert parsed.title == "Reliable Widget Measurements"
    assert parsed.ingest["section_source"] == "document"
    assert list(parsed.sections) == [
        "_preamble", "abstract", "introduction", "methods", "results",
        "discussion", "conclusion", "references",
    ]
    for body in parsed.sections.values():
        assert body in parsed.text


def test_section_boundaries_follow_the_printed_headings(parsed):
    assert parsed.sections["introduction"].startswith("The purpose of this study")
    assert parsed.sections["methods"].startswith("We placed the widgets")
    assert parsed.sections["conclusion"].startswith("In this study, we found")
    assert "Ada Example" in parsed.sections["references"].splitlines()[0]
    assert "Ada Example" not in parsed.sections["conclusion"]


def test_bibliography_is_typed_without_invented_fields(parsed):
    assert len(parsed.references) == 3
    assert [r["label"] for r in parsed.references] == ["1", "2", "3"]
    assert [r["year"] for r in parsed.references] == [2020, 2021, 2022]
    assert parsed.references[0]["title"] == "Reliable widget placement"
    assert all("doi" not in r for r in parsed.references)


def test_conversion_health_and_statistics_are_measured(parsed):
    stats = parsed.ingest["prose"]
    assert stats["health"]["verdict"] == "clean"
    assert stats["counts"]["reference_entries"] == 3
    assert stats["counts"]["main_text_words"] < stats["counts"]["words"]
    assert "introduction" in stats["sections"]
    assert "references" not in stats["sections"]


def test_one_conversion_keeps_markdown_and_structure_together():
    result = rustypaper.convert(PAPER)
    assert result.markdown == rustypaper.to_markdown(str(PAPER), None)
    assert result.document == rustypaper.to_document(PAPER)


def test_cached_conversion_preserves_sections_and_references(tmp_path):
    config = {"cache_dir": str(tmp_path)}
    first = load_manuscript_record(str(PAPER), config)
    second = load_manuscript_record(str(PAPER), config)
    assert second.text == first.text
    assert second.sections == first.sections
    assert second.references == first.references
    assert second.ingest["text_sha256"] == first.ingest["text_sha256"]
