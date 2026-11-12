---
name: follow-up
description: Re-review a revised manuscript against an earlier run and say which findings are resolved, which are still open and which have changed. Use when asked to check a revision, re-review after revisions, or compare a new version of a paper against a previous review.
---

# Follow up on a revision

Two inputs: the earlier run folder and the revised PDF. The job is the
difference between them, finding by finding.

1. **Re-review the revision.** Start the workflow exactly as
   `peer-review-manuscript` does, with the same arm and the same reviewer count
   as the earlier run — read them from `<old run>/review/protocol.json`
   (`arm`, `reviewers`) so the comparison is between versions, not between
   designs. Write the new run to its own folder; never into the old one.
2. **Wait for it.** If the new run is `blocked`, stop and report that. There is
   nothing to compare.
3. **Compare by `finding_uid`.** Read `<old run>/review/editor-package.json`
   and the new one. A `finding_uid` is a hash of the paper, the track, the block
   and the quote, so it is stable when a finding genuinely recurs and changes
   when the text it rests on changes.

   * In both runs → **still open**. Say what the revision did not address.
   * Only in the old run → **resolved, or moved**. Check the new manuscript
     before you call it resolved: the same issue restated over new text gets a
     new uid. Quote what changed.
   * Only in the new run → **new**. Say whether the revision introduced it or
     whether the first review missed it.
4. **Write the comparison** to `<new run>/review/follow-up.md`: the three lists
   above, with the old and new `finding_uid` beside each entry, and a short note
   on how much of the first review the revision answered.

Do not edit the old run, do not re-judge the old findings on their merits, and
do not add findings of your own — a finding that is not in either run's ledger
has no place in the comparison.
