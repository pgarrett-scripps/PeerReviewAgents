---
name: follow-up
description: Re-review a revised manuscript against an earlier run and say which findings are resolved, which are still open and which have changed. Use when asked to check a revision, re-review after revisions, or compare a new version of a paper against a previous review.
---

# Follow up on a revision

Two inputs: the earlier run folder and the revised PDF. The job is the
difference between them, finding by finding.

1. **Re-review the revision.** Start the workflow exactly as
   `peer-review-manuscript` does, with the same arm and the same reviewer count
   as the earlier run, and on the same models — read them from
   `<old run>/review/protocol.json` (`arm`, `reviewers`, `reviewer_models` and
   `models`) so the comparison is between versions, not between designs. Pass
   the old `reviewer_models` as `reviewer_models`, and the old `models.editor`
   and a specialist's `models` entry as `editor_model` and `specialist_model`.
   A run from before 0.9.0 recorded no models, so leave all three out and say
   in the write-up that the two runs may not have read on the same models.
   Write the new run to its own folder; never into the old one.
2. **Wait for it.** If the new run is `blocked`, stop and report that. There is
   nothing to compare.
3. **Match the findings on what they rest on, and never on `finding_uid`.**
   Read `<old run>/review/editor-package.json` and the new one. A
   `finding_uid` names a finding *inside one run*: it is a digest of the
   paper's id, the reviewer slot that filed it, the quote and the blocks
   cited. Two of those move between runs, so two runs never agree on a uid:

   * the paper's id is a digest of the PDF, so a revised file renames every
     finding in it — comparing uids across runs would report the whole first
     review resolved and the whole second one new, even for a revision that
     changed nothing;
   * the core slots are interchangeable, so one issue filed by `core-2` in the
     first run and `core-1` in the second is two uids for one worry. The
     specialist slots are fixed, and still carry the paper's id.

   Use the uid to name a finding in the write-up, and to follow
   `duplicate_uids` and `reviewers_raising` *within* a run.

   Match an old finding to a new one when it rests on the same passage and
   makes the same claim. The old finding's `quote` is the anchor worth
   searching for: look for it in the revised manuscript and among the new
   run's quotes, then read the titles and comments to confirm the claim is the
   same one. Block ids are positional, so they shift when text is added above
   them; do not match on them alone.

   * Matched in both runs → **still open**. Say what the revision did not
     address.
   * Only in the old run → **resolved, or moved**. Check the revised
     manuscript before you call it resolved: an issue restated over rewritten
     text is the same issue. Quote what changed.
   * Only in the new run → **new**. Say whether the revision introduced it or
     whether the first review missed it.
4. **Write the comparison** to `<new run>/review/follow-up.md`: the three lists
   above, each entry carrying the old `finding_uid`, the new one, and the quote
   the match was made on, so a reader can check the pairing; then a short note
   on how much of the first review the revision answered.

Do not edit the old run, do not re-judge the old findings on their merits, and
do not add findings of your own — a finding that is not in either run's ledger
has no place in the comparison.
