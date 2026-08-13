"""What the loader does with a PDF, and what it refuses to do.

None of these tests need rustypaper installed: the converter is stubbed out,
because what is being checked is the loader's contract with it — what it
records, and that a missing converter stops the run rather than quietly
producing a worse manuscript.
"""

from __future__ import annotations

import os

import pytest

from peerreviewagents.ingest import cache, loader, structured

SAMPLE_MD = os.path.join(os.path.dirname(__file__), "sample_manuscript.md")


@pytest.fixture
def cache_dir(tmp_path):
    """A config that keeps every test's parse out of the user's real cache."""
    return {"cache_dir": str(tmp_path / "manuscripts")}


def _fake_pdf(tmp_path):
    """A file with a .pdf suffix. Never actually read — the converter is
    stubbed in the tests that use it."""
    path = tmp_path / "paper.pdf"
    path.write_bytes(b"%PDF-1.7\n")
    return str(path)


def _stub_convert(monkeypatch, markdown="# Real Title\n\n## Methods\n\nWe did it.\n"):
    def convert(path, caveman="off", kind="manuscript"):
        return structured.Converted(
            markdown=markdown, title="Real Title", tool="rustypaper 9.9.9"
        )

    monkeypatch.setattr(structured, "convert", convert)


def _stub_unavailable(monkeypatch, reason, exc_type=structured.Unavailable):
    def convert(path, caveman="off", kind="manuscript"):
        raise exc_type(reason)

    monkeypatch.setattr(structured, "convert", convert)


# --- there is one converter, and no fallback -------------------------------


def test_a_pdf_is_converted_to_markdown(tmp_path, monkeypatch, cache_dir):
    _stub_convert(monkeypatch)
    parsed = loader.load_manuscript_record(_fake_pdf(tmp_path), cache_dir)
    assert parsed.ingest["format"] == "markdown"
    assert parsed.ingest["tool"] == "rustypaper 9.9.9"
    # The converter's own title beats the loader's line-order heuristic.
    assert parsed.title == "Real Title"


def test_a_missing_converter_stops_the_run(tmp_path, monkeypatch, cache_dir):
    """No fallback, by design.

    The alternative reader flattens structure and fuses words across column
    boundaries, and a panel reading that reviews a document the authors did
    not write. Degrading silently would do that on exactly the runs nobody
    is watching, so this raises — with the install line in the message.
    """
    _stub_unavailable(
        monkeypatch,
        "rustypaper unavailable (ImportError: nope)",
        exc_type=structured.ConverterMissing,
    )
    with pytest.raises(RuntimeError) as excinfo:
        loader.load_manuscript_record(_fake_pdf(tmp_path), cache_dir)
    assert "ImportError" in str(excinfo.value)
    assert "pip install" in str(excinfo.value)


def test_an_unreadable_pdf_stops_the_run(tmp_path, monkeypatch, cache_dir):
    """A scan has no text layer. Neither reader can help, so say so."""
    _stub_unavailable(monkeypatch, "rustypaper produced only 12 characters")
    with pytest.raises(RuntimeError, match="only 12 characters"):
        loader.load_manuscript_record(_fake_pdf(tmp_path), cache_dir)


def test_a_scanned_pdf_is_not_told_to_install_the_converter(tmp_path, monkeypatch, cache_dir):
    """A user WITH rustypaper who submits a scan must not be told to install it.

    The install line and the scan diagnosis ask for completely different
    things next, and closing every failure with "pip install rustypaper"
    sent submitters to reinstall a package they already had.
    """
    _stub_unavailable(
        monkeypatch,
        "rustypaper produced only 12 characters, which is not a readable "
        "manuscript — the PDF is most likely scanned or image-only",
    )
    with pytest.raises(RuntimeError) as excinfo:
        loader.load_manuscript_record(_fake_pdf(tmp_path), cache_dir)
    assert "scanned or image-only" in str(excinfo.value)
    assert "pip install" not in str(excinfo.value)


def test_a_one_page_letter_is_not_a_failed_manuscript_scan(monkeypatch):
    """The manuscript floor (4000 chars) applied to every document the loader
    read, so a normal one-page author response letter (~2,500 chars) was
    refused as scanned and the revision run died."""
    import sys
    import types

    fake = types.ModuleType("rustypaper")
    fake.__version__ = "9.9.9"
    fake.to_markdown = lambda path, caveman: "Dear editor, thank you. " * 100
    fake.to_document = lambda path, caveman: {"title": "", "blocks": []}
    monkeypatch.setitem(sys.modules, "rustypaper", fake)

    with pytest.raises(structured.Unavailable, match="readable manuscript"):
        structured.convert("letter.pdf", kind="manuscript")
    out = structured.convert("letter.pdf", kind="letter")
    assert "Dear editor" in out.markdown


def test_the_refusal_names_the_document_kind(monkeypatch):
    import sys
    import types

    fake = types.ModuleType("rustypaper")
    fake.__version__ = "9.9.9"
    fake.to_markdown = lambda path, caveman: "x" * 10
    fake.to_document = lambda path, caveman: {"title": "", "blocks": []}
    monkeypatch.setitem(sys.modules, "rustypaper", fake)

    with pytest.raises(structured.Unavailable, match="readable letter"):
        structured.convert("letter.pdf", kind="letter")


def test_the_loader_threads_the_document_kind_to_the_converter(tmp_path, monkeypatch, cache_dir):
    seen = {}

    def convert(path, caveman="off", kind="manuscript"):
        seen["kind"] = kind
        return structured.Converted(
            markdown="Dear editor, thanks.", title="", tool="rustypaper 9.9.9"
        )

    monkeypatch.setattr(structured, "convert", convert)
    loader.load_manuscript_record(_fake_pdf(tmp_path), cache_dir, kind="letter")
    assert seen["kind"] == "letter"


def test_unknown_caveman_level_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="unknown caveman level"):
        structured.convert(_fake_pdf(tmp_path), caveman="medium")


# --- the cache key has to cover the ingest config ---------------------------


def test_cache_key_separates_compression_levels(tmp_path):
    path = _fake_pdf(tmp_path)
    keys = {
        cache.cache_key(path, {"caveman": level}) for level in ("off", "light", "hard")
    }
    assert len(keys) == 3


def test_cache_key_ignores_unrelated_config(tmp_path):
    """Only ingest knobs may move the key.

    The key is recorded in a round record and re-derived a round later to
    recover the previous draft. If an unrelated setting moved it, changing the
    reviewer model between rounds would silently cost every revision its
    baseline diff.
    """
    path = _fake_pdf(tmp_path)
    assert cache.cache_key(path, {"reasoning_model": "a"}) == cache.cache_key(
        path, {"reasoning_model": "b"}
    )


def test_a_converter_upgrade_moves_the_pdf_cache_key(tmp_path, monkeypatch):
    """The version lived only inside the ingest record, so upgrading
    rustypaper never invalidated anything: a paper first seen under an old
    converter was served its old conversion forever."""
    path = _fake_pdf(tmp_path)
    monkeypatch.setattr(structured, "converter_version", lambda: "0.1.0")
    old = cache.cache_key(path, {})
    monkeypatch.setattr(structured, "converter_version", lambda: "0.2.0")
    assert cache.cache_key(path, {}) != old


def test_a_converter_upgrade_does_not_move_a_markdown_key(tmp_path, monkeypatch):
    """Markdown never passes through the converter, so bumping rustypaper
    must not orphan those entries."""
    md = tmp_path / "paper.md"
    md.write_text("# T\n\nBody.\n", encoding="utf-8")
    monkeypatch.setattr(structured, "converter_version", lambda: "0.1.0")
    old = cache.cache_key(str(md), {})
    monkeypatch.setattr(structured, "converter_version", lambda: "0.2.0")
    assert cache.cache_key(str(md), {}) == old


def test_a_torn_manuscript_file_is_a_miss_not_a_manuscript(tmp_path, monkeypatch, cache_dir):
    """metadata.json was written atomically but manuscript.md was not, so a
    write interrupted midway read back cleanly as a shorter manuscript and
    was served on every later run."""
    _stub_convert(monkeypatch, markdown="# T\n\n" + "Words in the body here. " * 40)
    path = _fake_pdf(tmp_path)
    first = loader.load_manuscript_record(path, cache_dir)

    key = cache.cache_key(path, cache_dir)
    md_file = cache._entry_dir(key, cache_dir) / "manuscript.md"
    torn = md_file.read_text(encoding="utf-8")
    md_file.write_text(torn[: len(torn) // 2], encoding="utf-8")

    assert cache.get(key, cache_dir) is None
    # The loader recovers by re-parsing rather than serving the torn text.
    second = loader.load_manuscript_record(path, cache_dir)
    assert second.text == first.text


def test_cached_entry_reports_the_original_ingest(tmp_path, monkeypatch, cache_dir):
    """A cache hit must report the ingest record the original parse made."""
    _stub_convert(monkeypatch)
    path = _fake_pdf(tmp_path)
    first = loader.load_manuscript_record(path, cache_dir)

    def explode(*a, **k):
        raise AssertionError("second load should have come from the cache")

    monkeypatch.setattr(structured, "convert", explode)
    second = loader.load_manuscript_record(path, cache_dir)
    assert second.ingest == first.ingest
    assert second.text == first.text
    assert second.sections == first.sections


# --- section mapping --------------------------------------------------------


def test_numbered_heading_fused_into_its_paragraph_still_opens_a_section():
    """The converter fuses some headings into the text below them.

    ``I. Introduction LECTROMAGNETIC (EM) metasurfaces have…`` is one line in
    the converted Markdown of a real IEEE paper. The length guard that stops
    a body sentence from opening a section would drop it, and the paper loses
    its introduction — so numbering waives the guard.
    """
    body = "have attracted significant attention " * 4
    text = f"Title\n\nI. Introduction LECTROMAGNETIC (EM) metasurfaces {body}\n"
    sections = loader._split_sections(text)
    assert "introduction" in sections
    # The paragraph fused onto the heading is kept, not discarded with it.
    assert "attracted significant attention" in sections["introduction"]


def test_a_numbered_list_item_is_prose_not_a_heading():
    """"2. Results were consistent with prior work." is a list item.

    The numbered waiver used to read it as a heading: it opened a phantom
    `results` section and, being short enough to be "a heading and nothing
    else", the line was dropped from the section map entirely — silent text
    loss in the middle of a manuscript.
    """
    text = (
        "# Paper\n\nOur main findings were threefold.\n\n"
        "1. Methods were pre-registered.\n"
        "2. Results were consistent with prior work.\n"
        "3. Limitations remain in the follow-up cohort.\n"
    )
    sections = loader._split_sections(text)
    assert "results" not in sections
    assert "methods" not in sections
    joined = "\n".join(sections.values())
    assert "Results were consistent with prior work." in joined
    assert "Methods were pre-registered." in joined


def test_a_heading_inside_a_code_fence_is_code_not_a_section():
    text = (
        "# Paper\n\nBody text before the listing.\n\n"
        "```python\n# results\nprint(compute_results())\n```\n\n"
        "Body text after the listing.\n"
    )
    sections = loader._split_sections(text)
    assert "results" not in sections
    assert "# results" in sections["_preamble"]
    assert "print(compute_results())" in sections["_preamble"]
    assert "Body text after the listing." in sections["_preamble"]


def test_a_word_spelled_from_roman_letters_is_not_a_numbered_heading():
    # "Mild." is made entirely of [ivxlcdm]; with a case-insensitive roman
    # match it counted as numbered and waived the length guard.
    text = "Title\n\nMild. Discussion of the side effects follows below here.\n"
    assert "discussion" not in loader._split_sections(text)


def test_section_synonyms_collapse_to_one_bucket():
    """The diff matches sections by name, so `conclusion` vs `conclusions`
    must not be two different sections."""
    v1 = loader._split_sections("T\n\nConclusion\n\nWe conclude things.\n")
    v2 = loader._split_sections("T\n\nConclusions\n\nWe conclude things.\n")
    assert v1["conclusion"] == v2["conclusion"]

    mm = loader._split_sections("T\n\nMaterials and Methods\n\nWe pipetted.\n")
    meth = loader._split_sections("T\n\nMethodology\n\nWe pipetted.\n")
    assert mm["methods"] == meth["methods"] == "We pipetted."


def test_alias_targets_are_priority_sections():
    """fit_manuscript keeps a manuscript's core by _PRIORITY_SECTIONS name;
    an alias collapsing to a name outside that list would silently drop the
    section from every truncated prompt."""
    from peerreviewagents.agents.utils.agent_utils import _PRIORITY_SECTIONS

    for target in ("methods", "conclusion"):
        assert target in _PRIORITY_SECTIONS


def test_renaming_conclusion_between_rounds_is_not_a_change():
    from peerreviewagents.ingest import diff as ingest_diff

    old = loader._split_sections("T\n\nConclusion\n\nSame ending text.\n")
    new = loader._split_sections("T\n\nConclusions\n\nSame ending text.\n")
    d = ingest_diff.diff_sections(old, new)
    assert all(x.status == "unchanged" for x in d.deltas)


def test_long_unnumbered_line_does_not_open_a_section():
    text = (
        "Title\n\nDiscussion of these results is deferred to the companion "
        "paper, which treats the asymptotic regime in considerably more "
        "detail than is possible here.\n"
    )
    assert "discussion" not in loader._split_sections(text)


def test_reference_anchor_finds_a_bibliography_with_no_heading():
    """rustypaper fuses "References" into the first entry on some papers.

    The Markdown then contains no References heading at all, and the section
    map loses the bibliography — unless it is told where the typed reference
    blocks start.
    """
    text = (
        "# Paper\n\n## Introduction\n\nWe begin.\n\n"
        "ReferencesKevin Clark, Minh-Thang Luong, and others. 2019.\n"
        "Jacob Devlin and others. 2018. Something else.\n"
    )
    without = loader._split_sections(text)
    assert "references" not in without

    with_anchor = loader._split_sections(
        text, references_anchor="ReferencesKevin Clark, Minh-Thang"
    )
    assert "Kevin Clark" in with_anchor["references"]
    assert "Jacob Devlin" in with_anchor["references"]
    # The anchor line is a reference itself, so it is kept rather than eaten
    # the way a heading line is.
    assert "We begin." in with_anchor["introduction"]


def test_a_real_references_heading_still_wins(tmp_path):
    text = "# Paper\n\nBody.\n\nReferences\n\nSmith, J. 2020. A paper.\n"
    sections = loader._split_sections(text, references_anchor="Smith, J. 2020")
    assert sections["references"].startswith("Smith")


# --- text safety ------------------------------------------------------------


def test_lone_surrogates_do_not_abort_the_run(tmp_path, monkeypatch, cache_dir):
    """Unpaired surrogates are held by Python and then refused by its codec.

    One used to abort the review at the cache write — seconds in, with a
    UnicodeEncodeError naming a codec rather than a manuscript. A DOCX can
    still carry them.
    """
    path = tmp_path / "paper.md"
    path.write_text("Broken \ud835 glyph.", encoding="utf-8", errors="surrogatepass")
    parsed = loader.load_manuscript_record(str(path), cache_dir)
    parsed.text.encode("utf-8")  # must not raise
    assert "Broken" in parsed.text


# --- what the panel is told -------------------------------------------------


def test_compression_is_declared_to_every_agent():
    from peerreviewagents.agents.utils.agent_utils import manuscript_block

    state = {"manuscript_md": "Results show effect.", "config": {}, "ingest": {}}
    plain = manuscript_block(state)
    assert "machine-compressed" not in plain

    state["ingest"] = {"caveman": "light"}
    compressed = manuscript_block(state)
    assert "machine-compressed" in compressed
    # Criticising an author for the compressor's grammar is the failure this
    # notice exists to prevent, so it has to name that explicitly.
    assert "not of the authors' writing" in compressed

    state["ingest"] = {"caveman": "hard"}
    assert "prepositions and connectives" in manuscript_block(state)


def test_uncompressed_manuscript_block_is_byte_identical_to_before():
    """The block is the shared cached prefix. Adding an unconditional line to
    it would invalidate every provider-side cache entry for no benefit."""
    from peerreviewagents.agents.utils.agent_utils import manuscript_block

    state = {"manuscript_md": "Body.", "config": {}}
    assert manuscript_block(state) == "=== MANUSCRIPT ===\nBody.\n=== END MANUSCRIPT ==="
