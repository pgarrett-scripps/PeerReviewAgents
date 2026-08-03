"""The venue's acceptance rate must not become a rejection quota.

Measured on C-07 (bioRxiv preprint of a paper Nature published after two
rounds of revision). The panel: four reviewers at 4/5, four at 3/5,
confidence-weighted 3.52, verdict distribution "4 minor, 4 major" — not one
reviewer in the reject bucket. The meta-reviewer returned *reject*, writing
that 3.52 "would nominally suggest a 'major revision' verdict, but for a
target venue with Nature's selectivity (~8% acceptance)…". The editor
followed.

The profile rendered `Approx. acceptance rate: ~8%` as a bare line. That
figure is a base rate over all submissions, most of which never reach a
referee — so applying it after review charges the manuscript twice for a
selection it already survived.
"""

from __future__ import annotations

from peerreviewagents.agents.synthesis import meta_reviewer
from peerreviewagents.journals import JournalProfile


def block(**kw) -> str:
    return JournalProfile(slug="nature", name="Nature", **kw).to_prompt_block()


def test_the_acceptance_rate_is_labelled_as_a_base_rate():
    text = block(acceptance_rate="~8%")
    assert "~8%" in text
    assert "base rate" in text.lower()


def test_it_says_the_manuscript_already_cleared_the_desk():
    """The specific misreading to block: treating a post-review verdict as if
    it still had to pass the desk-rejection filter."""
    assert "cleared the desk" in block(acceptance_rate="~8%").lower()


def test_it_forbids_lowering_a_verdict_to_match_the_number():
    assert "do not lower a verdict" in block(acceptance_rate="~8%").lower()


def test_a_profile_without_an_acceptance_rate_says_nothing_about_one():
    """Most profiles omit it; they must not gain a paragraph about a figure
    they do not carry."""
    text = block(field="Biology")
    assert "acceptance rate" not in text.lower()
    assert "base rate" not in text.lower()


def test_the_meta_reviewer_must_ground_a_verdict_harsher_than_the_panel():
    import inspect

    body = inspect.getsource(meta_reviewer)
    assert "harsher than every reviewer" in body
    # And specifically that venue selectivity is not the justification.
    assert "selectivity is not a" in body


def test_the_panel_is_named_as_already_venue_aware():
    """The double-count in one sentence: the reviewers were handed the same
    venue description, so applying it again at synthesis is not new evidence."""
    import inspect
    assert "given the same" in inspect.getsource(meta_reviewer)
