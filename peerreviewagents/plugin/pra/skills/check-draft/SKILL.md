---
name: check-draft
description: Run chosen specialists over a draft of your own - statistics, internal consistency and methods reporting by default - with no reviewer track and no editor. Use when asked to check, audit or sanity-check a draft or manuscript before submission, rather than to peer review it.
---

# Check a draft

This is the author's pass, not peer review. The specialists audit their own
dimension and nothing pools their findings into a verdict, because a draft of
your own does not need one — it needs the list.

1. Take the PDF path, the run folder and, if the request names them, the
   tracks. The default is `stats`, `consistency`, `methods-reporting`.
   The six available are `stats`, `figures-tables`, `methods-reporting`,
   `citations`, `consistency`, `field`.
2. Call the `Workflow` tool with `name` set to `pra:review` (if that name is
   not found, try `review`) and `args` set to:

   ```json
   {"pdf": "<the PDF path>", "out": "<the run folder>", "arm": "draft-check",
    "tracks": ["stats", "consistency", "methods-reporting"]}
   ```

3. Wait for it, then report what it returns, and point at the per-track audits
   the run wrote: `<run folder>/review/audit_<track>.md`, one per track.

The `draft-check` arm runs no core reviewer and no editor, so every finding
stands on its own and all of them are in the review's body. There is no
verdict, and you should not invent one — if the request wants a judgement on
whether the paper would be accepted, that is `peer-review-manuscript` with the
`full-two-track` arm.

Do not read the PDF yourself, write files, or add findings of your own.
