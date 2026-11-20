---
name: check-draft
description: Run chosen specialists over a draft of your own - statistics, the paper's own numbers and methods reporting by default - with no reviewer track and no editor. Use when asked to check, audit or sanity-check a draft or manuscript before submission, rather than to peer review it.
---

# Check a draft

This is the author's pass, not peer review. The specialists audit their own
dimension and nothing pools their findings into a verdict, because a draft of
your own does not need one — it needs the list.

1. Take the PDF path, the run folder and, if the request names them, the
   tracks. The default is `stats`, `numbers`, `methods-reporting`.
   The six available are `stats`, `figures-tables`, `methods-reporting`,
   `citations`, `numbers`, `field`. Each audits one dimension and nothing
   another one owns, so naming a track decides what goes unchecked: `numbers`
   is every printed quantity and the arithmetic between them, `stats` the
   inference drawn from them, `figures-tables` what a panel shows,
   `methods-reporting` what is absent from the methods, `citations` the
   references, `field` what this kind of study is required to do.
2. Run it as one command, in the background — the specialists take tens of
   minutes and must not be waited on in the foreground:

   ```bash
   peerreview review "<the PDF path>" "<the run folder>" --arm draft-check \
       --track stats --track numbers --track methods-reporting
   ```

   `--track` is repeated once per track. Add `--specialist-model <a model>`
   only when the request names one; left out, every specialist runs on Sonnet.
   If it refuses to start because a model is not available on this login,
   report that line as it came and do not retry with another model.

3. Wait for it, then report the JSON object it prints, and point at the per-track audits
   the run wrote: `<run folder>/review/audit_<track>.md`, one per track. An
   audit may carry a **Seen, not mine** line: something that specialist noticed
   in another lane and did not file. Pass those on as they are written — with
   the track that owns them unrun, nothing else looked.

With `citations` among the tracks, the run also keeps a citation ledger —
`<run folder>/review/citations/ledger.jsonl`, one row per reference and one per
in-text use, with each reference looked up and each use judged against the
source before the specialist reads the table. It is the slowest track and the
only one that needs the network. Report its counts as the run gives them, and
keep "could not be checked" apart from "not found": a lookup that failed says
nothing about whether a reference exists.

The `draft-check` arm runs no core reviewer and no editor, so nothing is
grouped: the server writes `issues.json` with one issue per finding, keeping the
finding's own severity, so the review reads in the same shape as a full run and
says plainly that the issues came from the server rather than an editor. There
is no verdict, and you should not invent one — if the request wants a judgement on
whether the paper would be accepted, that is `peer-review-manuscript` with the
`full-two-track` arm.

Do not read the PDF yourself, write files, or add findings of your own.
