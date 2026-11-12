---
name: peer-review-spike
description: Run the PeerReviewAgents 0.9 plugin spike on one PDF - ingest it, run one tool-restricted reviewer and check a finding was stored. Use when asked to run the spike, the plugin spike, or the prspike workflow.
---

# Run the 0.9 plugin spike

Start the workflow and report what it returns. Do nothing else: no reading the
PDF yourself, no writing files, no summarising the manuscript.

1. Take the PDF path and the run folder from the request.
2. Call the `Workflow` tool with `name` set to `prspike:spike` (if that name is
   not found, try `spike`) and `args` set to
   `{"pdf": "<the PDF path>", "out": "<the run folder>"}`.
3. The workflow returns an object with a `receipt` inside `finding`. Report that
   object as it is. If the workflow throws, report the error text verbatim and
   do not try to finish the review another way.

The workflow owns the stages. If a stage fails, the run is blocked, and saying
so is the correct outcome.
