---
name: peer-review-manuscript
description: Run a full two-track peer review of one manuscript PDF - N core reviewers and six specialists, then an editor who pools their findings into one review. Use when asked to peer review, referee or critique a manuscript, or to run PeerReviewAgents on a PDF.
---

# Peer review one manuscript

Start the workflow and report what it returns. Do nothing else: do not read the
PDF yourself, do not write files, do not summarise the manuscript, and do not
add findings of your own. The workflow owns the stages and the server owns the
files.

1. Take from the request: the PDF path, the run folder, and optionally how many
   core reviewers (default 3), the arm (default `full-two-track`) and a
   literature cutoff date.
2. Call the `Workflow` tool with `name` set to `pra:review` (if that name is
   not found, try `review`) and `args` set to:

   ```json
   {"pdf": "<the PDF path>", "out": "<the run folder>", "reviewers": 3,
    "arm": "full-two-track", "literature_cutoff": "<YYYY-MM-DD or omit>"}
   ```

3. The workflow runs in the background. Wait for it, then report the object it
   returns: the status, the findings by track, how many were escalated, what the
   editor placed in the body, and the run folder. If it throws, report the error
   text verbatim and do not try to finish the review another way.

## The arms

`arm` is a setting of the same workflow, recorded in `protocol.json` so two runs
of one arm are comparable:

| arm | what runs |
| --- | --- |
| `full-two-track` | N core reviewers, all six specialists, then the editor |
| `reviewers-only` | N core reviewers and the editor, no specialist track |
| `single` | one reviewer, one pass, no editor |
| `single-revision` | one reviewer, then the same brief revising its own findings |
| `manuscript-only` | one reviewer, manuscript text only: no pages, no lookups |

Use `full-two-track` unless the request names an arm. The others exist to
measure what each part of the design is worth, so do not mix them in one
comparison.

## What a blocked run means

A run is `blocked` when a stage returned nothing twice, when the editor never
produced an accepted review, or when the rate limit stopped it. Blocked is an
outcome: report it with its reason. Never fill the gap yourself.
