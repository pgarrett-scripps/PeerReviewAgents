---
name: follow-up
description: Re-review a revised manuscript against an earlier run and say which issues are resolved, which persist and which regressed. Use when asked to check a revision, re-review after revisions, or compare a new version of a paper against a previous review.
---

# Follow up on a revision

Two inputs: the earlier run folder and the revised PDF. The job is the
difference between them, issue by issue — and the run itself makes the
comparison, because the editor is handed the earlier round's issues and matches
against them. You start it and report what it found.

1. **Re-review the revision against the earlier round.** Start the workflow
   exactly as `peer-review-manuscript` does, with two additions:

   * `"prior_issues": "<old run>/review/issues.json"` — the earlier round's
     issues. The editor matches each issue it raises to one of those, keeps the
     matched id, and marks it `persisting` or `regressed`; every prior issue
     nothing matches is written into the new `issues.json` as `resolved`.
   * the same arm, reviewer count and models as the earlier run, read from
     `<old run>/review/protocol.json` (`arm`, `reviewers`, `reviewer_models` and
     `models`), so the comparison is between versions and not between designs.
     Pass the old `reviewer_models` as `reviewer_models`, and the old
     `models.editor` and a specialist's `models` entry as `editor_model` and
     `specialist_model`. A run from before 0.9.0 recorded no models, so leave
     all three out and say in the write-up that the two runs may not have read
     on the same models.

   Write the new run to its own folder; never into the old one.

2. **Wait for it.** If the new run is `blocked`, stop and report that. There is
   nothing to compare. An arm with no editor cannot match anything either: its
   issues are the server's one-per-finding synthesis, so a follow-up needs an
   arm that runs the editor.

3. **Read the new `issues.json`.** Every issue carries a `status`:

   * `persisting` — raised again, under the id it had before. Its
     `status_reason` says what the revision did not address.
   * `regressed` — worse than it was.
   * `new` — this round is the first to raise it. Say whether the revision
     introduced it or the first review missed it.
   * `resolved` — a prior issue this round did not raise. It is carried over
     from the earlier round with the statement it had then.

   Do not match anything yourself, and never compare `finding_uid`s across
   runs: a uid is a digest of the paper's id, the slot, the quote and the lines
   cited, so a revised PDF renames every finding in it and the core slots are
   interchangeable between runs. The ids that carry across rounds are the issue
   ids, and they carry because the editor matched them.

4. **Check what the run called resolved.** An issue is `resolved` because this
   round did not raise it, which is not the same as fixed: read the revised
   manuscript at the passages the prior issue's members quote and say, in your
   own words, that the text now answers it — or that it was dropped rather than
   addressed.

5. **Write the comparison** to `<new run>/review/follow-up.md`: the four lists
   above, each entry carrying the issue id, its name, and the quote you checked
   it against; then a short note on how much of the first review the revision
   answered, with the counts as `issues.json` gives them.

Do not edit the old run, do not re-judge the old issues on their merits, and do
not add issues of your own — an issue in neither round's `issues.json` has no
place in the comparison.
