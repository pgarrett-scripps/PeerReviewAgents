"""Section-aware diff between two drafts of a manuscript.

A revision round could hand the agents both drafts in full and let them work
out what moved, but that doubles the most expensive block in every prompt to
answer a question ``difflib`` answers exactly and for free. So the diff is
computed locally and injected as a compact "what changed" block; the agents
read v2 in full and the *delta* as a summary.

The unit is the section, not the line. :mod:`.loader` already buckets text
into ``abstract`` / ``methods`` / ``results`` / …, and "Methods gained two
paragraphs about seed averaging" is what a reviewer needs — line-level
churn from re-flowed paragraphs is noise that would bury it.

Two consumers, two shapes:

* :func:`render_diff_block` — prose for a prompt.
* :func:`changed_sections` — the set of sections that moved, which the
  reviewer prompts use to separate "an issue the revision created" from
  "an issue I could have raised last round".
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass

# Sections whose churn is almost always cosmetic; reported, but never used to
# claim substantive change.
_LOW_SIGNAL = {"references", "bibliography", "acknowledgements"}

# Cap on quoted added/removed text per section, so a wholesale rewrite can't
# crowd out the manuscript itself.
_MAX_QUOTE_CHARS = 900
# Below this similarity a section counts as substantively rewritten.
_REWRITE_RATIO = 0.75


@dataclass(frozen=True)
class SectionDelta:
    """How one section changed between drafts."""

    name: str
    status: str          # "added" | "removed" | "changed" | "unchanged"
    similarity: float    # 0..1, 1.0 = identical
    added: str = ""      # text present in v2 but not v1
    removed: str = ""    # text present in v1 but not v2

    @property
    def substantive(self) -> bool:
        return self.status != "unchanged" and self.name not in _LOW_SIGNAL


@dataclass(frozen=True)
class ManuscriptDiff:
    """The whole v1 → v2 comparison."""

    deltas: tuple[SectionDelta, ...]
    # False when the previous draft could not be recovered (cache cleared);
    # the round then proceeds without a diff rather than failing.
    available: bool = True
    note: str = ""

    @property
    def changed(self) -> tuple[SectionDelta, ...]:
        return tuple(d for d in self.deltas if d.status != "unchanged")

    @property
    def substantive(self) -> tuple[SectionDelta, ...]:
        return tuple(d for d in self.deltas if d.substantive)

    def changed_section_names(self) -> set[str]:
        return {d.name for d in self.substantive}


def diff_sections(
    old: dict[str, str], new: dict[str, str]
) -> ManuscriptDiff:
    """Compare two section maps from :func:`.loader.load_manuscript`."""
    names = list(dict.fromkeys(list(new.keys()) + list(old.keys())))
    deltas: list[SectionDelta] = []
    for name in names:
        before, after = old.get(name), new.get(name)
        if before is None:
            deltas.append(SectionDelta(
                name=name, status="added", similarity=0.0,
                added=_clip(after or ""),
            ))
            continue
        if after is None:
            deltas.append(SectionDelta(
                name=name, status="removed", similarity=0.0,
                removed=_clip(before),
            ))
            continue
        if _normalize(before) == _normalize(after):
            deltas.append(SectionDelta(name=name, status="unchanged", similarity=1.0))
            continue
        added, removed = _word_delta(before, after)
        deltas.append(SectionDelta(
            name=name,
            status="changed",
            similarity=round(_similarity(before, after), 3),
            added=_clip(added),
            removed=_clip(removed),
        ))
    return ManuscriptDiff(deltas=tuple(deltas))


def unavailable(note: str) -> ManuscriptDiff:
    """A diff that could not be computed — the round proceeds without one."""
    return ManuscriptDiff(deltas=(), available=False, note=note)


def render_diff_block(diff: ManuscriptDiff) -> str:
    """Render the diff for an agent prompt, or '' when there is nothing to say."""
    if not diff.available:
        return (
            "## What changed since the previous draft\n\n"
            f"Not available ({diff.note}). Judge the manuscript as it now "
            "stands; do not assume any particular section was or wasn't revised."
        )
    if not diff.changed:
        return (
            "## What changed since the previous draft\n\n"
            "**Nothing.** The submitted text is identical to the draft reviewed "
            "in the previous round, section for section. Treat every previously "
            "raised point as still outstanding unless the text itself shows "
            "otherwise."
        )

    lines = ["## What changed since the previous draft", ""]
    for delta in diff.changed:
        if delta.status == "added":
            lines.append(f"### {delta.name} — new section")
        elif delta.status == "removed":
            lines.append(f"### {delta.name} — removed")
        else:
            pct = int(delta.similarity * 100)
            tag = "substantially rewritten" if delta.similarity < _REWRITE_RATIO else "edited"
            lines.append(f"### {delta.name} — {tag} ({pct}% unchanged)")
        if delta.added:
            lines += ["", "Added or rewritten:", f"> {delta.added}"]
        if delta.removed:
            lines += ["", "Removed:", f"> {delta.removed}"]
        lines.append("")

    unchanged = [d.name for d in diff.deltas if d.status == "unchanged"]
    if unchanged:
        lines.append(f"Unchanged sections: {', '.join(unchanged)}.")
    return "\n".join(lines).strip()


def changed_sections(diff: ManuscriptDiff) -> set[str]:
    """Sections that changed substantively — see :meth:`ManuscriptDiff.changed_section_names`."""
    return diff.changed_section_names()


# --- internals --------------------------------------------------------------


def _normalize(text: str) -> str:
    """Collapse whitespace so re-flowed paragraphs don't read as edits."""
    return re.sub(r"\s+", " ", text).strip()


def _similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(
        None, _normalize(a), _normalize(b), autojunk=False
    ).ratio()


def _word_delta(before: str, after: str) -> tuple[str, str]:
    """Words present on only one side, in order, as two readable strings."""
    old_words = _normalize(before).split()
    new_words = _normalize(after).split()
    matcher = difflib.SequenceMatcher(None, old_words, new_words, autojunk=False)
    added: list[str] = []
    removed: list[str] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag in ("replace", "insert"):
            added.extend(new_words[j1:j2])
        if tag in ("replace", "delete"):
            removed.extend(old_words[i1:i2])
    return " ".join(added), " ".join(removed)


def _clip(text: str) -> str:
    text = _normalize(text)
    if len(text) <= _MAX_QUOTE_CHARS:
        return text
    return text[:_MAX_QUOTE_CHARS] + " …"
