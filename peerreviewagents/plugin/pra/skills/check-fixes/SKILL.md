---
name: check-fixes
description: Check a new draft against the problems a previous review raised - one verdict per problem, fixed, not-fixed or cannot-tell, each quoting the new draft - and write FIXES.md. Use when asked whether a revision addressed the review, or to check a revised manuscript against reviewer comments, rather than to review it.
---

# Check the fixes

This is the second half of a review, not a second review. The problems are
given; every one of them gets a verdict against the new draft and nothing new
is raised. If the request wants the revised paper reviewed, that is
`peer-review-manuscript`, and a fix check is not a substitute for one.

1. Take the PDF of the **new** draft and the run folder to write into. This is
   a fresh folder, not the folder the first review wrote: the two runs hold
   different manuscripts.
2. Find the problems file. The request may name one, or it may name the
   finished review instead — a run folder, or a `REVIEW.md` that came out of
   one. From a run folder, write the file from the review's own issues:

   ```bash
   python -c "import json,peerreviewagents as p; print(json.dumps(p.problems_from_run('<the first run folder>'), indent=2)[:400])"
   ```

   That writes `<the first run folder>/review/problems.json` and prints the
   start of what it holds. Use that path below. Where the problems come from
   somewhere else — a journal's reviewer comments, a list the authors were sent
   — write the file by hand in the same shape: a JSON object with
   `{"problems": [...]}`, each problem carrying `id`, `statement`, `severity`
   (`major`, `moderate` or `minor`), `evidence` as a list of
   `{"document": "", "quote": "<words of the OLD draft>"}` (`document` is
   empty for the manuscript itself, and the supplement's name otherwise), and
   optionally `author_reply`. The quotations are what makes a verdict
   checkable, so a problem with none can still be checked but has nothing
   behind it.
3. Call the `Workflow` tool with `name` set to `pra:check-fixes` (if that name
   is not found, try `check-fixes`) and `args` set to:

   ```json
   {"pdf": "<the new draft's PDF>", "out": "<the new run folder>",
    "problems": "<path to the problems file>"}
   ```

   Add `"specialist_model": "<a model>"` only when the request names one; left
   out, every checker runs on Sonnet. If the workflow refuses to start because
   a model is not available on this login, report that line as it came and do
   not retry with another model.

4. Wait for it, then report what it returns and point at
   `<the new run folder>/review/FIXES.md`, with `fix-check.json` beside it.

Read the three verdicts as they are meant, and do not round them off:

- **fixed** — the new draft no longer has the problem, and the row quotes the
  passage that fixes it. Where the fix was a deletion there is no quotation;
  the row says instead that the words the problem was raised against are gone
  and lists the searches that establish it.
- **not-fixed** — it still does, and the row quotes the passage in the new
  draft that still has it.
- **cannot-tell** — the draft does not settle it, with a reason. This is a real
  answer, not a failure: a fix that lives in the code, the data, a repeated
  experiment or a reply letter cannot be read out of the manuscript, and this
  check reads the manuscript only. Report the reason rather than the count
  alone.

`FIXES.md` also carries a **not checked** count where the file held more
problems than one run checks (the cap is 40). Those problems have no verdict
and are not "fixed by default" — say how many went unchecked, and that a
second run on the rest is how they get answered.

What the check does not judge: whether a fix is *scientifically adequate* —
whether the analysis the authors added was the right analysis. The draft
addresses a problem or it does not; the quality of the answer is a reviewer's
question, and nothing in this run asks it.

Do not read the PDF yourself, write files, or raise problems of your own.
