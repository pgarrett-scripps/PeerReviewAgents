"""Unit tests for the YAML-frontmatter parser used by every agent."""

from peerreviewagents.agents.utils.agent_utils import (
    body_only,
    coerce_int,
    split_frontmatter,
)


def test_split_frontmatter_happy_path():
    text = "---\nscore: 4\nconfidence: 5\n---\n# Body\n\nstuff\n"
    meta, body = split_frontmatter(text)
    assert meta == {"score": "4", "confidence": "5"}
    assert body == "# Body\n\nstuff\n"


def test_split_frontmatter_missing_block():
    text = "# Body only, no frontmatter\n"
    meta, body = split_frontmatter(text)
    assert meta == {}
    assert body == text


def test_split_frontmatter_unterminated_block():
    # No closing fence: treat the whole thing as body, don't lose content.
    text = "---\nscore: 4\nstill in block but no close\n"
    meta, body = split_frontmatter(text)
    assert meta == {}
    assert body == text


def test_split_frontmatter_strips_quotes():
    text = "---\nrole: \"advocate\"\ndecision: 'minor'\n---\nbody\n"
    meta, _ = split_frontmatter(text)
    assert meta["role"] == "advocate"
    assert meta["decision"] == "minor"


def test_split_frontmatter_crlf_line_endings():
    text = "---\r\nscore: 3\r\n---\r\n# Body\r\n"
    meta, body = split_frontmatter(text)
    assert meta == {"score": "3"}
    assert body.endswith("# Body\r\n")


def test_split_frontmatter_skips_comment_and_blank_lines():
    text = "---\n# this is a comment\n\nscore: 2\n---\nbody\n"
    meta, _ = split_frontmatter(text)
    assert meta == {"score": "2"}


def test_body_only():
    assert body_only("---\nx: 1\n---\nhello\n") == "hello\n"
    assert body_only("no frontmatter") == "no frontmatter"


def test_coerce_int_happy_path():
    assert coerce_int("4", default=3, lo=1, hi=5) == 4
    assert coerce_int(4.7, default=3, lo=1, hi=5) == 4


def test_coerce_int_clamps_out_of_range():
    assert coerce_int("9", default=3, lo=1, hi=5) == 5
    assert coerce_int("-2", default=3, lo=1, hi=5) == 1


def test_coerce_int_falls_back_on_garbage():
    assert coerce_int("not a number", default=3, lo=1, hi=5) == 3
    assert coerce_int(None, default=3, lo=1, hi=5) == 3
